"""Shared Google/Okta authorization-code flow and explicit account linking."""

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
from pydantic import BaseModel, ConfigDict, Field

from .db import database
from .file_links import valid_file_return_path
from .oidc_identity import GOOGLE_ISSUER, IdentityLinkRequired
from .public_url import app_path, cookie_name
from .security import Throttle, digest


class SignInStart(BaseModel):
    return_to: str = Field(default="/#tasks", max_length=1600)
    link: bool = False


class SignInRedirect(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    url: str


class OIDCSignIn:
    def __init__(self, settings, security, store, provider):
        self.settings, self.security, self.store, self.provider = settings, security, store, provider
        self.cookie_name = cookie_name(provider + "_login", settings.public_url)
        self.cookie_path = app_path(settings.public_url, "/auth/" + provider)
        self.metadata, self.keys = {}, {}
        self.metadata_expire = self.keys_expire = self.last_key_fetch = 0
        self.lock = asyncio.Lock()
        self.attempts = Throttle(30)

    @property
    def issuer(self):
        return GOOGLE_ISSUER if self.provider == "google" else self.settings.okta_issuer

    @property
    def client_id(self):
        return getattr(self.settings, self.provider + "_client_id")

    @property
    def client_secret(self):
        return getattr(self.settings, self.provider + "_client_secret")

    def enabled(self):
        return not self.settings.cloudflare_access_login and getattr(self.settings, self.provider + "_enabled")()

    @property
    def callback(self):
        return self.settings.public_url.rstrip("/") + "/auth/" + self.provider + "/callback"

    def state_hash(self, state):
        return digest("\0".join((self.provider, self.issuer, self.client_id, state)))

    async def discovery(self):
        if self.provider == "google":
            return {"authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
                    "token_endpoint": "https://oauth2.googleapis.com/token",
                    "jwks_uri": "https://www.googleapis.com/oauth2/v3/certs"}
        async with self.lock:
            if time.monotonic() < self.metadata_expire:
                return self.metadata
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(self.issuer + "/.well-known/openid-configuration")
                response.raise_for_status()
            metadata = response.json()
            if not isinstance(metadata, dict) or metadata.get("issuer") != self.issuer:
                raise ValueError("Unexpected issuer")
            origin = urlsplit(self.issuer)
            for field in ("authorization_endpoint", "token_endpoint", "jwks_uri", "userinfo_endpoint"):
                if not isinstance(metadata.get(field), str):
                    raise ValueError("Missing endpoint")
                endpoint = urlsplit(metadata[field])
                if (endpoint.scheme != "https" or endpoint.netloc != origin.netloc
                        or endpoint.username or endpoint.password or endpoint.fragment or endpoint.query):
                    raise ValueError("Unexpected endpoint")
            if "RS256" not in metadata.get("id_token_signing_alg_values_supported", []):
                raise ValueError("RS256 ID tokens required")
            if "client_secret_basic" not in metadata.get("token_endpoint_auth_methods_supported", []):
                raise ValueError("A confidential web client is required")
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
                    raise ValueError("Invalid signing keys")
                self.keys = {item["kid"]: jwt.PyJWK.from_dict(item).key for item in document["keys"]
                             if isinstance(item, dict) and isinstance(item.get("kid"), str)
                             and item.get("kty") == "RSA" and item.get("alg", "RS256") == "RS256"
                             and item.get("use", "sig") == "sig"}
                self.keys_expire, self.last_key_fetch = now + 3600, now
            if kid not in self.keys:
                raise ValueError("Unknown signing key")
            return self.keys[kid]

    async def verify(self, token, nonce):
        if not isinstance(token, str) or len(token) > 16000:
            raise ValueError("Invalid ID token")
        header = jwt.get_unverified_header(token)
        if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
            raise ValueError("Invalid signing algorithm")
        claims = jwt.decode(token, await self.signing_key(header["kid"]), algorithms=["RS256"],
                            audience=self.client_id,
                            issuer=[GOOGLE_ISSUER, "accounts.google.com"] if self.provider == "google" else self.issuer,
                            leeway=30, options={"require": ["exp", "iat", "iss", "aud", "sub", "nonce"]})
        if not isinstance(claims["nonce"], str) or not hmac.compare_digest(claims["nonce"], nonce):
            raise ValueError("Invalid nonce")
        if not isinstance(claims["sub"], str) or not claims["sub"] or len(claims["sub"]) > 255:
            raise ValueError("Invalid subject")
        if claims.get("azp", self.client_id) != self.client_id:
            raise ValueError("Invalid authorized party")
        if isinstance(claims["aud"], list) and len(claims["aud"]) > 1 and "azp" not in claims:
            raise ValueError("Missing authorized party")
        return self.profile(claims, claims["sub"]) if self.provider == "google" else claims

    def profile(self, profile, subject):
        if not isinstance(profile, dict):
            raise ValueError("Invalid profile")
        email = profile.get("email")
        domains = getattr(self.settings, self.provider + "_domains")()
        if (profile.get("sub") != subject or profile.get("email_verified") is not True
                or not isinstance(email, str) or len(email) > 254
                or not re.fullmatch(r"[^\s@]+@[^\s@]+", email)
                or email.lower().rpartition("@")[2] not in domains):
            raise ValueError("Not an allowed verified identity")
        identity = {"sub": subject, "email": email.lower(), "name": str(profile.get("name") or email)[:160]}
        if self.provider == "google":
            domain = profile.get("hd")
            if not isinstance(domain, str) or domain.lower() != email.lower().rpartition("@")[2]:
                raise ValueError("A verified Google Workspace domain is required")
            identity["domain"] = domain.lower()
        else:
            identity["issuer"] = self.issuer
        return identity

    async def exchange(self, code, verifier, nonce):
        metadata = await self.discovery()
        data = {"grant_type": "authorization_code", "code": code, "redirect_uri": self.callback, "code_verifier": verifier}
        auth = None
        if self.provider == "google":
            data.update(client_id=self.client_id, client_secret=self.client_secret)
        else:
            auth = httpx.BasicAuth(quote_plus(self.client_id), quote_plus(self.client_secret))
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(metadata["token_endpoint"], auth=auth, data=data)
            response.raise_for_status()
            tokens = response.json()
            if not isinstance(tokens, dict):
                raise ValueError("Invalid token response")
            claims = await self.verify(tokens.get("id_token"), nonce)
            if self.provider == "google":
                return claims
            access_token = tokens.get("access_token")
            if not isinstance(access_token, str) or not access_token or len(access_token) > 16000:
                raise ValueError("Missing access token")
            response = await client.get(metadata["userinfo_endpoint"], headers={"Authorization": "Bearer " + access_token})
            response.raise_for_status()
            return self.profile(response.json(), claims["sub"])

    def save_state(self, state, browser, nonce, verifier, return_to, client, link_user_id=None, link_session_hash=None):
        with self.store.connect() as conn:
            conn.begin_write()
            conn.execute("DELETE FROM login_states WHERE expires<?", (time.time(),))
            conn.execute("DELETE FROM login_states WHERE client=? AND state_hash NOT IN "
                         "(SELECT state_hash FROM login_states WHERE client=? ORDER BY expires DESC LIMIT 4)", (client, client))
            conn.execute("INSERT INTO login_states(state_hash,browser_hash,nonce,verifier,return_path,expires,client,link_user_id,link_session_hash) "
                         "VALUES(?,?,?,?,?,?,?,?,?)", (self.state_hash(state), digest(browser), nonce, verifier,
                                                       return_to, time.time() + 600, client, link_user_id, link_session_hash))

    def consume_state(self, state, browser):
        with self.store.connect() as conn:
            row = conn.execute("DELETE FROM login_states WHERE state_hash=? AND browser_hash=? AND expires>? RETURNING *",
                               (self.state_hash(state), digest(browser), time.time())).fetchone()
            return dict(row) if row else None

    async def link_actor(self, request, authorize, *, starting=False):
        self.security.require(request, mutation=starting)
        info = self.security.session_info(request)
        if not info or info['method'] not in {'google', 'okta'}:
            raise HTTPException(403, "Sign in with your existing Google or Okta account before linking.")
        # A shared password or a stale stolen session is not proof of ownership.
        self.security.require_recent_sign_in(request, max_age=300 if starting else 900)
        try:
            user_id = await database(self.store.oidc_actor, info)
        except ValueError:
            raise HTTPException(401, "The sign-in account mapping requires review.") from None
        if authorize is not None:
            principal = await authorize(request, mutation=starting)
            if principal.id != user_id:
                raise HTTPException(403, "The signed-in account changed.")
        return user_id, info

    def routes(self, authorize=None):
        router = APIRouter()
        title = self.provider.title()

        @router.post("/api/auth/" + self.provider + "/start", operation_id="start" + title + "SignIn")
        async def start(body: SignInStart, request: Request, response: Response) -> SignInRedirect:
            self.security.check_origin(request)
            if not self.enabled():
                if self.provider == 'google' and self.settings.cloudflare_access_login and not body.link:
                    self.security.require(request)
                    return SignInRedirect(url=app_path(self.settings.public_url, '/#tasks'))
                raise HTTPException(409, title + " sign-in is not enabled.")
            link_user_id = link_session_hash = None
            if body.link:
                link_user_id, info = await self.link_actor(request, authorize, starting=True)
                link_session_hash = digest(info['sid'])
            client = self.security.client_key(request)
            if self.attempts.blocked(client):
                raise HTTPException(429, "Too many sign-in attempts. Wait a minute.")
            self.attempts.record(client)
            try:
                metadata = await self.discovery()
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                raise HTTPException(503, title + " sign-in is temporarily unavailable.") from None
            state, browser, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(4))
            allowed = re.fullmatch(r"/#(?:tasks|connections|runtime|spend|users|run=[a-f0-9]{32}(?:&credential=[a-f0-9]{32}&generation=(?:0|[1-9][0-9]{0,14}))?)", body.return_to)
            return_to = '/#settings/sign-in' if body.link else body.return_to if allowed or valid_file_return_path(body.return_to) else '/#tasks'
            await database(self.save_state, state, browser, nonce, verifier, return_to, client, link_user_id, link_session_hash)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            params = {"client_id": self.client_id, "redirect_uri": self.callback, "response_type": "code",
                      "scope": "openid email profile", "state": state, "nonce": nonce,
                      "code_challenge": challenge, "code_challenge_method": "S256"}
            if self.provider == 'google':
                domains = sorted(self.settings.google_domains())
                params.update(prompt='select_account', hd=domains[0] if len(domains) == 1 else '*')
            if body.link:
                params['prompt'] = 'login' if self.provider == 'okta' else 'select_account'
            response.set_cookie(self.cookie_name, browser, max_age=600, httponly=True,
                                secure=not self.security.local, samesite="lax", path=self.cookie_path)
            return SignInRedirect(url=metadata["authorization_endpoint"] + "?" + urlencode(params))

        @router.get("/auth/" + self.provider + "/callback", response_class=RedirectResponse, include_in_schema=False)
        async def callback(request: Request, state: str = "", code: str = "", error: str = "") -> RedirectResponse:
            if not self.enabled():
                raise HTTPException(409, title + " sign-in is not enabled.")
            if len(state) > 200 or len(code) > 4096:
                raise HTTPException(400, "Invalid sign-in request.")
            browser = request.cookies.get(self.cookie_name, "")
            row = await database(self.consume_state, state, browser)
            return_path = row['return_path'] if row else '/#tasks'
            def redirect(result):
                response = RedirectResponse(app_path(self.settings.public_url, '/?signin=' + result + return_path[1:]), status_code=303)
                response.delete_cookie(self.cookie_name, path=self.cookie_path)
                return response
            if not row or not browser or error or not code:
                return redirect('cancelled' if error else 'failed')
            try:
                link_info = None
                if row.get('link_user_id'):
                    user_id, link_info = await self.link_actor(request, authorize)
                    if user_id != row['link_user_id'] or not hmac.compare_digest(digest(link_info['sid']), row['link_session_hash']):
                        return redirect('failed')
                identity = await self.exchange(code, row['verifier'], row['nonce'])
                # Validate the original login again after provider network I/O.
                if link_info is not None:
                    await self.link_actor(request, authorize)
                await database(self.store.oidc_identity, self.provider, identity, link_info=link_info)
            except IdentityLinkRequired:
                return redirect('link_required')
            except (httpx.HTTPError, jwt.PyJWTError, HTTPException, ValueError, KeyError, TypeError):
                return redirect('failed')
            role = await database(self.security.google_role, identity['email'])
            response = redirect('linked') if link_info else RedirectResponse(app_path(self.settings.public_url, return_path), status_code=303)
            response.delete_cookie(self.cookie_name, path=self.cookie_path)
            self.security.new_session(response, role, identity=identity, method=self.provider, oidc_bound=True)
            return response

        return router
