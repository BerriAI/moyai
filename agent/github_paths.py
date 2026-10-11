"""Publication path policy shared by the computer packager and trusted broker."""

ENV_TEMPLATES = frozenset({'.env.example', '.env.sample', '.env.template'})


def publishable_path(path):
    parts = path.split('/')
    lower = path.lower()
    return not (
        any(p in {'', '.', '..'} or p.lower() in {'.git', '.ssh', 'codeowners'} for p in parts)
        or any(p.lower().startswith('.env') for p in parts[:-1])
        or (parts[-1].lower().startswith('.env') and parts[-1].lower() not in ENV_TEMPLATES)
        or '\\' in path or any(ord(c) < 32 or ord(c) == 127 for c in path)
        or lower in {'.github', '.github/workflows', '.github/actions'}
        or lower.endswith(('.pem', '.key'))
    )
