"""Public preparation notices; never expose arbitrary provider exception text."""

from temporalio.exceptions import ApplicationError

PREPARATION_BLOCKED_TYPE = "ProductPreparationBlocked"
MESSAGES = {
    "workspace_checkpoint_missing": (
        "This conversation's computer expired without a current, verified checkpoint. "
        "Saved messages, approvals and downloads are retained. No action was replayed "
        "and no empty replacement computer was started."
    ),
    "workspace_uncertain_effect": (
        "An earlier action still has an uncertain outcome. Resolve its existing receipt "
        "before restoring this computer; recovery will not repeat the action."
    ),
    "workspace_in_use": (
        "The computer is still controlled by a person or has an unsettled desktop action. "
        "Release control and let the action settle before restoring it."
    ),
    "recovery_collection_incomplete": (
        "The earlier recovery collector could not read every visible file. "
        "The old computer has not been drained. Review its original recovery package "
        "and move the missing files before retrying continuation."
    ),
}


def preparation_blocked(code: str) -> ApplicationError:
    return ApplicationError(MESSAGES[code], code, type=PREPARATION_BLOCKED_TYPE, non_retryable=True)


def public_preparation_message(error: BaseException) -> str | None:
    """Unwrap Temporal's activity failure without trusting its message/details."""
    seen: set[int] = set()
    current: BaseException | None = error
    for _ in range(8):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        if isinstance(current, ApplicationError) and current.type == PREPARATION_BLOCKED_TYPE:
            if len(current.details) == 1 and isinstance(current.details[0], str):
                return MESSAGES.get(current.details[0])
            return None
        cause = getattr(current, "cause", None) or current.__cause__
        current = cause if isinstance(cause, BaseException) else None
    return None
