"""Server-owned routing hints for provider prompt-cache reuse."""
from hashlib import sha256


def session_routing(model: str, run) -> dict:
    # Fireworks caches prefixes per replica. Keep requests from one requester
    # in one conversation sticky without exposing their identity or accepting
    # arbitrary upstream headers from the sandbox. Applies to all its models.
    if not model.startswith('fireworks_ai/'):
        return {}
    scope = f"{run['id']}:{run['active_user_id']}"
    return {'extra_headers': {'x-session-affinity': sha256(scope.encode()).hexdigest()}}
