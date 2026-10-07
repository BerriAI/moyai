"""Google OpenID Connect for organization sign-in; no Google API access is retained."""
import asyncio
import base64
import hashlib
import hmac
import re
import secrets
import time
from urllib.parse import urlencode

import httpx
import jwt
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from .security import digest

AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
KEYS_URL = "https://www.googleapis.com/oauth2/v3/certs"
COOKIE = "google_login"


class SignInStart(BaseModel):
    return_to: str = Field(default="/#tasks", max_length=200)


class GoogleSignIn:
    def __init__(self, settings, security, store):
        self.settings, self.security, self.store = settings, security, store
        self.keys = {}
        self.keys_expire = 0
        self.last_key_fetch = 0
        self.key_lock = asyncio.Lock()
        self.attempts = []

    @property
    def callback(self):
        return self.security.origin + "/auth/google/callback"

    async def signing_key(self, kid):
        async with self.key_lock:
            now = time.monotonic()
            if now >= self.keys_expire or (kid not in self.keys and now - self.last_key_fetch > 60):
                async with httpx.AsyncClient(timeout=15) as client:
                    response = await client.get(KEYS_URL)
                    response.raise_for_status()
                self.keys = {item["kid"]: jwt.PyJWK.from_dict(item).key
                             for item in response.json()["keys"]
                             if item.get("alg") == "RS256" and item.get("use") == "sig"}
                self.keys_expire = now + 3600
                self.last_key_fetch = now
            if kid not in self.keys:
                raise ValueError("Unknown Google signing key")
            return self.keys[kid]

    async def verify(self, token, nonce):
        if not isinstance(token, str) or len(token) > 16000:
            raise ValueError("Invalid ID token")
        header = jwt.get_unverified_header(token)
        if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
            raise ValueError("Invalid signing algorithm")
        claims = jwt.decode(token, await self.signing_key(header["kid"]), algorithms=["RS256"],
                            audience=self.settings.google_client_id,
                            issuer=["https://accounts.google.com", "accounts.google.com"], leeway=30,
                            options={"require": ["exp", "iat", "iss", "aud", "sub", "nonce", "email", "email_verified", "hd"]})
        if not isinstance(claims["nonce"], str) or not hmac.compare_digest(claims["nonce"], nonce):
            raise ValueError("Invalid nonce")
        if claims.get("azp", self.settings.google_client_id) != self.settings.google_client_id:
            raise ValueError("Invalid authorized party")
        if isinstance(claims["aud"], list) and len(claims["aud"]) > 1 and "azp" not in claims:
            raise ValueError("Missing authorized party")
        email, domain = claims["email"], claims["hd"]
        if (claims["email_verified"] is not True or not isinstance(email, str)
                or not isinstance(domain, str) or not claims["sub"]
                or domain.lower() not in self.settings.google_domains()
                or email.lower().rpartition("@")[2] != domain.lower()):
            raise ValueError("Not an allowed Google Workspace identity")
        return {"sub": claims["sub"], "email": email.lower(), "domain": domain.lower(),
                "name": str(claims.get("name", email))[:160]}

    async def exchange(self, code, verifier, nonce):
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(TOKEN_URL, data={
                "grant_type": "authorization_code", "code": code,
                "client_id": self.settings.google_client_id, "client_secret": self.settings.google_client_secret,
                "redirect_uri": self.callback, "code_verifier": verifier,
            })
            response.raise_for_status()
            token = response.json().get("id_token")
        return await self.verify(token, nonce)

    def routes(self):
        router = APIRouter()

        @router.post("/api/auth/google/start")
        async def start(body: SignInStart, request: Request):
            self.security.check_origin(request)
            if not self.settings.google_enabled():
                raise HTTPException(409, "Google sign-in is not configured yet.")
            now = time.monotonic()
            self.attempts[:] = [stamp for stamp in self.attempts if stamp > now - 60]
            if len(self.attempts) >= 30:
                raise HTTPException(429, "Too many sign-in attempts. Wait a minute.")
            self.attempts.append(now)
            state, browser, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(4))
            return_to = body.return_to if re.fullmatch(r"/#(?:tasks|connections|runtime|spend|users|run=[a-f0-9]{32}(?:&credential=[a-f0-9]{32}&generation=(?:0|[1-9][0-9]{0,14}))?)", body.return_to) else "/#tasks"
            self.store.execute("DELETE FROM login_states WHERE expires<?", (time.time(),))
            self.store.execute("INSERT INTO login_states VALUES(?,?,?,?,?,?)",
                               (digest(state), digest(browser), nonce, verifier, return_to, time.time() + 600))
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            domains = sorted(self.settings.google_domains())
            params = {"client_id": self.settings.google_client_id, "redirect_uri": self.callback,
                      "response_type": "code", "scope": "openid email profile", "state": state,
                      "nonce": nonce, "code_challenge": challenge, "code_challenge_method": "S256",
                      "prompt": "select_account", "hd": domains[0] if len(domains) == 1 else "*"}
            response = JSONResponse({"url": AUTHORIZE_URL + "?" + urlencode(params)})
            response.set_cookie(COOKIE, browser, max_age=600, httponly=True,
                                secure=not self.security.local, samesite="lax", path="/auth/google")
            return response

        @router.get("/auth/google/callback")
        async def callback(request: Request, state: str = "", code: str = "", error: str = ""):
            if not self.settings.google_enabled():
                raise HTTPException(409, "Google sign-in is not configured yet.")
            if len(state) > 200 or len(code) > 4096:
                raise HTTPException(400, "Invalid sign-in request.")
            browser = request.cookies.get(COOKIE, "")
            with self.store.connect() as conn:
                row = conn.execute("DELETE FROM login_states WHERE state_hash=? AND browser_hash=? AND expires>? RETURNING *",
                                   (digest(state), digest(browser), time.time())).fetchone()
            result = "cancelled" if error else "failed"
            return_hash = row["return_path"][1:] if row else "#tasks"
            response = RedirectResponse(f"/?signin={result}{return_hash}", status_code=303)
            response.delete_cookie(COOKIE, path="/auth/google")
            if not row or not browser or error or not code:
                return response
            try:
                identity = await self.exchange(code, row["verifier"], row["nonce"])
            except (httpx.HTTPError, jwt.PyJWTError, ValueError, KeyError, TypeError):
                # Never echo provider errors, codes, tokens, or claims into the UI/logs.
                return response
            response = RedirectResponse(row["return_path"], status_code=303)
            response.delete_cookie(COOKIE, path="/auth/google")
            role = self.security.google_role(identity['email'])
            self.store.identity({'method': 'google', 'identity': identity, 'role': role})
            self.security.new_session(response, role, identity=identity)
            return response

        return router
