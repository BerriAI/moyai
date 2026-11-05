"""Pinned provider identities for a stable Moyai account; never merge by email."""

from .security import digest

GOOGLE_ISSUER = "https://accounts.google.com"
PROVIDERS = ("google", "okta")


class IdentityLinkRequired(ValueError):
    pass


def identity_key(provider, identity):
    issuer = GOOGLE_ISSUER if provider == "google" else identity["issuer"]
    subject = identity["sub"]
    if provider not in PROVIDERS or not isinstance(subject, str) or not subject:
        raise ValueError("Invalid provider identity")
    return provider, issuer, subject


def legacy_user_id(provider, identity):
    _, issuer, subject = identity_key(provider, identity)
    return "google:" + subject if provider == "google" else "okta:" + digest(issuer) + ":" + digest(subject)


def eligible_binding(settings, binding):
    provider, issuer = binding['provider'], binding['issuer']
    if settings.cloudflare_access_login or provider not in PROVIDERS:
        return False
    expected = GOOGLE_ISSUER if provider == 'google' else settings.okta_issuer
    return (getattr(settings, provider + '_enabled')() and issuer == expected
            and binding['email'].rpartition('@')[2] in getattr(settings, provider + '_domains')())


def active_user(user):
    return user and user['kind'] in PROVIDERS and not any(
        user.get(k) for k in ('disabled', 'disabled_at', 'deleted_at', 'revoked_at')
    ) and all(user.get(k, True) in (True, 1) for k in ('enabled', 'is_active'))


def resolve_in(conn, provider, identity, *, require_binding=False):
    """Read-only resolution. Legacy cookies may use their original account only."""
    key = identity_key(provider, identity)
    email = identity['email'].strip().lower()
    binding = conn.execute('SELECT * FROM oidc_identities WHERE provider=? AND issuer=? AND subject=?', key).fetchone()
    if require_binding and not binding:
        raise ValueError('The sign-in binding is no longer available.')
    user_id = binding['user_id'] if binding else legacy_user_id(provider, identity)
    user = conn.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()
    user = dict(user) if user else None
    if not active_user(user) or user['email'] != email or (binding and binding['email'] != email):
        raise ValueError('Sign-in identity changed; account review is required.')
    if not binding:
        if user['kind'] != provider or conn.execute(
            'SELECT 1 FROM oidc_identities WHERE provider=? AND issuer=? AND user_id=?',
            (provider, key[1], user_id),
        ).fetchone():
            raise ValueError('Sign-in identity is not bound to this account.')
    return user_id


def enroll_in(conn, provider, identity, stamp, *, link_info=None):
    key = identity_key(provider, identity)
    email = identity['email'].strip().lower()
    binding = conn.execute('SELECT * FROM oidc_identities WHERE provider=? AND issuer=? AND subject=?', key).fetchone()
    target = resolve_in(conn, link_info['method'], link_info['identity'], require_binding=link_info.get('oidc_bound', False)) if link_info else None
    if binding:
        user_id = resolve_in(conn, provider, identity)
        if target and user_id != target:
            raise IdentityLinkRequired('This provider already belongs to another account.')
    else:
        original = legacy_user_id(provider, identity)
        existing = conn.execute('SELECT * FROM users WHERE id=?', (original,)).fetchone()
        matches = conn.execute("SELECT id FROM users WHERE lower(email)=? AND kind IN ('google','okta','cloudflare')", (email,)).fetchall()
        if target:
            owner = conn.execute('SELECT * FROM users WHERE id=?', (target,)).fetchone()
            if owner['email'] != email or any(row['id'] != target for row in matches) or (existing and original != target):
                raise IdentityLinkRequired('Email or account conflict requires administrator review.')
            user_id = target
        elif existing:
            user_id = resolve_in(conn, provider, identity)
        elif matches:
            raise IdentityLinkRequired('Sign in with your existing method, then link this provider in Settings > Sign-in.')
        else:
            user_id = original
            conn.execute('INSERT INTO users(id,kind,email,name,created_at,updated_at) VALUES(?,?,?,?,?,?)',
                         (user_id, provider, email, identity.get('name') or email, stamp, stamp))
        # A different subject cannot replace a pinned identity, even with the
        # same verified email. The global writer lock serializes both providers.
        if conn.execute('SELECT 1 FROM oidc_identities WHERE provider=? AND issuer=? AND (user_id=? OR email=?)',
                        (provider, key[1], user_id, email)).fetchone():
            raise IdentityLinkRequired('A different provider identity is already linked.')
        conn.execute('INSERT INTO oidc_identities(provider,issuer,subject,email,user_id,created_at) VALUES(?,?,?,?,?,?)',
                     (*key, email, user_id, stamp))
        if target:
            conn.execute('INSERT INTO identity_audit(actor_id,source_id,target_id,created_at) VALUES(?,?,?,?)',
                         (target, original, target, stamp))
    conn.execute('UPDATE users SET updated_at=? WHERE id=?', (stamp, user_id))
    return user_id
