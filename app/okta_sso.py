"""Okta adapter for the shared OIDC flow."""
from .oidc_identity import legacy_user_id
from .oidc_sso import OIDCSignIn


def okta_user_id(issuer: str, subject: str) -> str:
    return legacy_user_id('okta', {'issuer': issuer, 'sub': subject})


class OktaSignIn(OIDCSignIn):
    def __init__(self, settings, security, store):
        super().__init__(settings, security, store, 'okta')
