import pytest
from app.slack_format import slack_text, split_reply


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("**bold**", "*bold*"),
        ("**`x`**", "*`x`*"),
        ("**see `x` now**", "*see `x` now*"),
        ("**a** x **b** y **c**", "*a* x *b* y *c*"),
        (
            "- **Model mapping:** x\n- **HTTP 200** model\n**Pricing caveat:** the",
            "- *Model mapping:* x\n- *HTTP 200* model\n*Pricing caveat:* the",
        ),
        ("**a** **", "*a* **"),
        ("**a** `**code**` **b**", "*a* `**code**` *b*"),
        ("`**x**`", "`**x**`"),
        ("```python\n**x**\n```", "```\n**x**\n```"),
        ("```python\n**x**", "```\n**x**"),
    ],
)
def test_slack_text_normalizes_bold_outside_code(answer, expected):
    assert slack_text(answer) == expected


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("# Summary", "*Summary*"),
        ("## Use `main` now", "*Use `main` now*"),
        ("### **Model** `name` is **available**", "*Model `name` is available*"),
        ("#### `**literal**` and **bold**", "*`**literal**` and bold*"),
        ("# [Docs](https://example.com)", "*<https://example.com|Docs>*"),
        ("# First\n## Second `name`\nNormal", "*First*\n*Second `name`*\nNormal"),
        ("##\nNormal", "##\nNormal"),
        ("### \nNormal", "### \nNormal"),
        ("###   \nNormal", "###   \nNormal"),
        ("# Glob: **/*.py", "*Glob: **/*.py*"),
        ("# Math: 2**10", "*Math: 2**10*"),
        ("#tag", "#tag"),
        ("```markdown\n## **Keep** `this`\n```", "```\n## **Keep** `this`\n```"),
    ],
)
def test_slack_headings_cover_the_whole_line_without_nested_bold(answer, expected):
    assert slack_text(answer) == expected


@pytest.mark.parametrize("info", ["python", "json", "c++", "bash title=example", " python "])
@pytest.mark.parametrize("closing", ["\n```", ""])
def test_fence_metadata_is_removed_without_rewriting_the_code(info, closing):
    code = '# **literal** [link](https://example.com)\nvalue = "`x`"\nprint(value)'
    assert slack_text("```" + info + "\n" + code + closing) == "```\n" + code + closing


def test_plain_fences_multiple_blocks_and_crlf():
    answer = "```\npython\n```\n\n**Example**\n```json\r\n{}\r\n```"
    assert slack_text(answer) == "```\npython\n```\n\n*Example*\n```\n{}\r\n```"
    assert slack_text("```same line``` and `python`") == "```same line``` and `python`"


def test_formatting_keeps_mentions_literal_and_file_links_authenticated():
    answer = (
        "# **Result** `main`\n"
        "<!channel> <@U12345678> <@U87654321> & <tag>\n"
        "[Report](report.md) and `notes.md`\n"
        "```python\n# **raw** <@U12345678> [Report](report.md)\n```"
    )
    value = slack_text(answer, {"U12345678"}, public_url="https://moyai.example", run_id="a" * 32)
    assert value.startswith("*Result `main`*\n")
    assert "&lt;!channel&gt; <@U12345678> &lt;@U87654321&gt; &amp; &lt;tag&gt;" in value
    assert "<https://moyai.example/#run=" + "a" * 32 + "&amp;file=report.md|Report>" in value
    assert "<https://moyai.example/#run=" + "a" * 32 + "&amp;file=notes.md|notes.md>" in value
    assert value.endswith("```\n# **raw** &lt;@U12345678&gt; [Report](report.md)\n```")


def test_file_link_policy_stays_in_moyai_after_provider_formatting_moves():
    answer = "[Allowed](report.md) [Parent](../secret.md) `notes.md` [Script](javascript:alert)"
    assert slack_text(answer) == answer
    value = slack_text(answer, public_url="https://moyai.example", run_id="a" * 32)
    assert "&amp;file=report.md|Allowed>" in value
    assert "&amp;file=notes.md|notes.md>" in value
    assert "[Parent](../secret.md)" in value
    assert "[Script](javascript:alert)" in value


def test_reply_chunks_keep_authenticated_links_intact_and_legacy_empty_fallback():
    value = slack_text("🌊" * 2580 + "[Report](report.md)", public_url="https://moyai.example", run_id="a" * 32)
    link = value[value.index("<https://"):]
    chunks = split_reply(value)
    assert "".join(chunks) == value
    assert any(link in chunk for chunk in chunks)
    assert split_reply("") == ["No response text was returned."]
    assert split_reply("abcdefghij\n\nklmnopqrst", limit=12) == ["abcdefghij\n", "klmnopqrst"]


def test_reply_chunks_do_not_split_fence_markers():
    value = "a" * 2599 + "```\n" + "🦀" * 20 + "\n```"
    chunks = split_reply(value)
    assert all(chunk.count("```") % 2 == 0 for chunk in chunks)
    assert sum(chunk.count("🦀") for chunk in chunks) == 20
