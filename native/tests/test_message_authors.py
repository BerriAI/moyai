import pytest

from moyai.message_authors import author_name
from moyai.models import User


@pytest.mark.parametrize(("identity", "username", "name", "expected"), [
    ("google:alice", "product:opaque", "Alice Example", "Alice Example"),
    ("google:alice", "product:opaque", "alice@example.test", "alice@example.test"),
    ("native-alice", "alice", " ", "alice"),
    ("google:alice", "product:opaque", " ", None),
    ("slack:TTEST0001:UTEST0001", "product:opaque", "UTEST0001", None),
    ("slack:TTEST0001:UTEST0001", "product:opaque", "Slack UTEST0001", None),
    ("slack:TTEST0001:UTEST0001", "product:opaque", "slack:TTEST0001:UTEST0001", None),
    ("slack:TTEST0001:UTEST0001", "product:opaque", " Alice Slack ", "Alice Slack"),
])
def test_author_names_use_profiles_without_exposing_internal_ids(identity, username, name, expected):
    assert author_name(User(id=identity, username=username, display_name=name)) == expected


def test_missing_author_remains_unknown():
    assert author_name(None) is None
