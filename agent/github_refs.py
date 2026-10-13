"""Pure Git branch and checkout-ref validation shared by broker and sandbox."""
import re


def validate_branch_name(value: str) -> str:
    if (not isinstance(value, str) or not 0 < len(value) <= 1024
            or value in {'@', 'HEAD'} or value.startswith('-') or value.endswith('.')
            or any(character.isspace() or ord(character) < 32 or ord(character) == 127
                   or character in '~^:?*[\\' for character in value)
            or '..' in value or '@{' in value
            or any(not part or part.startswith('.') or part.endswith('.lock') for part in value.split('/'))):
        raise ValueError('Use a valid Git branch name, not a ref expression.')
    return value


def validate_checkout_ref(value: str) -> str:
    if isinstance(value, str) and re.fullmatch(r'[0-9a-f]{40}', value):
        return value
    return validate_branch_name(value)
