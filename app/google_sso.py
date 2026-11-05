"""Google Workspace adapter for the shared OIDC flow."""
from .oidc_sso import OIDCSignIn, SignInRedirect, SignInStart  # noqa: F401

AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
KEYS_URL = "https://www.googleapis.com/oauth2/v3/certs"
COOKIE = "google_login"


class GoogleSignIn(OIDCSignIn):
    def __init__(self, settings, security, store):
        super().__init__(settings, security, store, 'google')
