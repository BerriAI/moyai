"""Direct Okta OIDC sign-in. Provider tokens are never retained in a session."""

import asyncio
import base64
import hashlib
import hmac
import re
import secrets
import time
from urllib.parse import quote_plus, urlencode, urlsplit

import httpx
import jwt
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import RedirectResponse

from .db import database
from .file_links import valid_file_return_path
from .google_sso import SignInRedirect, SignInStart
from .public_url import app_path, cookie_name
from .security import Throttle, digest


def okta_user_id(issuer: str, subject: str) -> str:
    """Keep both the issuer and subject in the canonical account identity."""
    return "okta:" + digest(issuer) + ":" + digest(subject)


class OktaSignIn:
    def __init__(self, settings, security, store):
        self.settings, self.security, self.store = settings, security, store
        self.cookie_name = cookie_name("okta_login", settings.public_url)
        self.cookie_path = app_path(settings.public_url, "/auth/okta")
        self.metadata = {}
        self.metadata_expire = 0
        self.keys = {}
        self.keys_expire = 0
        self.last_key_fetch = 0
        self.lock = asyncio.Lock()
        self.attempts = Throttle(30)

    @property
    def callback(self):
        return self.settings.public_url.rstrip("/") + "/auth/okta/callback"

    def state_hash(self, state):
        # A Google state, another tenant, or a changed client cannot be redeemed here.
        return digest("\0".join(("okta", self.settings.okta_issuer, self.settings.okta_client_id, state)))

    async def discovery(self):
        async with self.lock:
            if time.monotonic() < self.metadata_expire:
                return self.metadata
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(self.settings.okta_issuer + "/.well-known/openid-configuration")
                response.raise_for_status()
            metadata = response.json()
            if not isinstance(metadata, dict) or metadata.get("issuer") != self.settings.okta_issuer:
                raise ValueError("Unexpected Okta issuer")
            origin = urlsplit(self.settings.okta_issuer)
            for field in ("authorization_endpoint", "token_endpoint", "jwks_uri", "userinfo_endpoint"):
                if not isinstance(metadata.get(field), str):
                    raise ValueError("Missing Okta endpoint")
                endpoint = urlsplit(metadata[field])
                if (endpoint.scheme != "https" or endpoint.netloc != origin.netloc
                        or endpoint.username or endpoint.password or endpoint.fragment):
                    raise ValueError("Unexpected Okta endpoint")
            if "RS256" not in metadata.get("id_token_signing_alg_values_supported", []):
                raise ValueError("Okta must support RS256 ID tokens")
            if "client_secret_basic" not in metadata.get("token_endpoint_auth_methods_supported", []):
                raise ValueError("Configure an Okta confidential web client")
            self.metadata, self.metadata_expire = metadata, time.monotonic() + 3600
            return metadata

    async def signing_key(self, kid):
        metadata = await self.discovery()
        async with self.lock:
            now = time.monotonic()
            if now >= self.keys_expire or (kid not in self.keys and now - self.last_key_fetch > 60):
                async with httpx.AsyncClient(timeout=15) as client:
                    response = await client.get(metadata["jwks_uri"])
                    response.raise_for_status()
                document = response.json()
                if not isinstance(document, dict) or not isinstance(document.get("keys"), list):
                    raise ValueError("Invalid Okta signing keys")
                self.keys = {item["kid"]: jwt.PyJWK.from_dict(item).key
                             for item in document["keys"]
                             if isinstance(item, dict) and isinstance(item.get("kid"), str)
                             and item.get("kty") == "RSA" and item.get("alg", "RS256") == "RS256"
                             and item.get("use", "sig") == "sig"}
                self.keys_expire, self.last_key_fetch = now + 3600, now
            if kid not in self.keys:
                raise ValueError("Unknown Okta signing key")
            return self.keys[kid]

    async def verify(self, token, nonce):
        if not isinstance(token, str) or len(token) > 16000:
            raise ValueError("Invalid ID token")
        header = jwt.get_unverified_header(token)
        if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
            raise ValueError("Invalid signing algorithm")
        claims = jwt.decode(token, await self.signing_key(header["kid"]), algorithms=["RS256"],
                            audience=self.settings.okta_client_id, issuer=self.settings.okta_issuer,
                            leeway=30, options={"require": ["exp", "iat", "iss", "aud", "sub", "nonce"]})
        if not isinstance(claims["nonce"], str) or not hmac.compare_digest(claims["nonce"], nonce):
            raise ValueError("Invalid nonce")
        if not isinstance(claims["sub"], str) or not claims["sub"] or len(claims["sub"]) > 255:
            raise ValueError("Invalid subject")
        if claims.get("azp", self.settings.okta_client_id) != self.settings.okta_client_id:
            raise ValueError("Invalid authorized party")
        if isinstance(claims["aud"], list) and len(claims["aud"]) > 1 and "azp" not in claims:
            raise ValueError("Missing authorized party")
        return claims

    async def exchange(self, code, verifier, nonce):
        metadata = await self.discovery()
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(metadata["token_endpoint"], auth=httpx.BasicAuth(
                quote_plus(self.settings.okta_client_id), quote_plus(self.settings.okta_client_secret)), data={
                    "grant_type": "authorization_code", "code": code,
                    "redirect_uri": self.callback, "code_verifier": verifier,
                })
            response.raise_for_status()
            tokens = response.json()
            if not isinstance(tokens, dict):
                raise ValueError("Invalid Okta token response")
            claims = await self.verify(tokens.get("id_token"), nonce)
            access_token = tokens.get("access_token")
            if not isinstance(access_token, str) or not access_token or len(access_token) > 16000:
                raise ValueError("Missing Okta access token")
            # Org authorization servers may return a thin ID token. Fetch the
            # profile only from the discovered same-origin endpoint, and bind it
            # to the already verified ID token before trusting email or name.
            response = await client.get(metadata["userinfo_endpoint"],
                                        headers={"Authorization": "Bearer " + access_token})
            response.raise_for_status()
            profile = response.json()
        if not isinstance(profile, dict):
            raise ValueError("Invalid Okta profile")
        email = profile.get("email")
        if (profile.get("sub") != claims["sub"] or profile.get("email_verified") is not True
                or not isinstance(email, str) or len(email) > 254
                or not re.fullmatch(r"[^\s@]+@[^\s@]+", email)
                or email.lower().rpartition("@")[2] not in self.settings.okta_domains()):
            raise ValueError("Not an allowed verified Okta identity")
        return {"issuer": self.settings.okta_issuer, "sub": claims["sub"], "email": email.lower(),
                "name": str(profile.get("name") or email)[:160]}

    def save_state(self, state, browser, nonce, verifier, return_to, client):
        with self.store.connect() as conn:
            conn.begin_write()
            conn.execute("DELETE FROM login_states WHERE expires<?", (time.time(),))
            conn.execute("DELETE FROM login_states WHERE client=? AND state_hash NOT IN "
                         "(SELECT state_hash FROM login_states WHERE client=? ORDER BY expires DESC LIMIT 4)",
                         (client, client))
            conn.execute("INSERT INTO login_states(state_hash,browser_hash,nonce,verifier,return_path,expires,client) "
                         "VALUES(?,?,?,?,?,?,?)", (self.state_hash(state), digest(browser), nonce, verifier,
                                                  return_to, time.time() + 600, client))

    def consume_state(self, state, browser):
        with self.store.connect() as conn:
            row = conn.execute("DELETE FROM login_states WHERE state_hash=? AND browser_hash=? AND expires>? RETURNING *",
                               (self.state_hash(state), digest(browser), time.time())).fetchone()
            return dict(row) if row else None

    def routes(self):
        router = APIRouter()

        @router.post("/api/auth/okta/start", operation_id="startOktaSignIn")
        async def start(body: SignInStart, request: Request, response: Response) -> SignInRedirect:
            self.security.check_origin(request)
            if self.settings.cloudflare_access_login or not self.settings.okta_enabled():
                raise HTTPException(409, "Okta sign-in is not enabled.")
            client = self.security.client_key(request)
            if self.attempts.blocked(client):
                raise HTTPException(429, "Too many sign-in attempts. Wait a minute.")
            self.attempts.record(client)
            try:
                metadata = await self.discovery()
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                raise HTTPException(503, "Okta sign-in is temporarily unavailable.") from None
            state, browser, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(4))
            allowed = re.fullmatch(r"/#(?:tasks|connections|runtime|spend|users|run=[a-f0-9]{32}(?:&credential=[a-f0-9]{32}&generation=(?:0|[1-9][0-9]{0,14}))?)", body.return_to)
            return_to = body.return_to if allowed or valid_file_return_path(body.return_to) else "/#tasks"
            await database(self.save_state, state, browser, nonce, verifier, return_to, client)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            params = {"client_id": self.settings.okta_client_id, "redirect_uri": self.callback,
                      "response_type": "code", "scope": "openid email profile", "state": state,
                      "nonce": nonce, "code_challenge": challenge, "code_challenge_method": "S256"}
            response.set_cookie(self.cookie_name, browser, max_age=600, httponly=True,
                                secure=not self.security.local, samesite="lax", path=self.cookie_path)
            return SignInRedirect(url=metadata["authorization_endpoint"] + "?" + urlencode(params))

        @router.get("/auth/okta/callback", response_class=RedirectResponse, include_in_schema=False)
        async def callback(request: Request, state: str = "", code: str = "", error: str = "") -> RedirectResponse:
            if self.settings.cloudflare_access_login or not self.settings.okta_enabled():
                raise HTTPException(409, "Okta sign-in is not enabled.")
            if len(state) > 200 or len(code) > 4096:
                raise HTTPException(400, "Invalid sign-in request.")
            browser = request.cookies.get(self.cookie_name, "")
            row = await database(self.consume_state, state, browser)
            result = "cancelled" if error else "failed"
            return_hash = row["return_path"][1:] if row else "#tasks"
            response = RedirectResponse(app_path(self.settings.public_url, f"/?signin={result}{return_hash}"), status_code=303)
            response.delete_cookie(self.cookie_name, path=self.cookie_path)
            if not row or not browser or error or not code:
                return response
            try:
                identity = await self.exchange(code, row["verifier"], row["nonce"])
            except (httpx.HTTPError, jwt.PyJWTError, ValueError, KeyError, TypeError):
                return response  # Never echo provider tokens, codes, claims, or errors.
            role = await database(self.security.google_role, identity["email"])
            await database(self.store.identity, {"method": "okta", "identity": identity, "role": role})
            response = RedirectResponse(app_path(self.settings.public_url, row["return_path"]), status_code=303)
            response.delete_cookie(self.cookie_name, path=self.cookie_path)
            self.security.new_session(response, role, identity=identity, method="okta")
            return response

        return router
