import pytest

from app.slack_format import slack_text


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("**bold**", "*bold*"),
        ("**`x`**", "*`x`*"),
        ("**see `x` now**", "*see `x` now*"),
        ("`**x**`", "`**x**`"),
        ("```python\n**x**\n```", "```python\n**x**\n```"),
        ("```python\n**x**", "```python\n**x**"),
    ],
)
def test_slack_text_normalizes_bold_outside_code(answer, expected):
    assert slack_text(answer) == expected
