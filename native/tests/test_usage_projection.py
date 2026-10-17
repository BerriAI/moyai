from __future__ import annotations

import json
from decimal import Decimal

import pytest

from moyai.usage_projection import ProjectionError, UsageProjection


@pytest.mark.parametrize("size", [1, 7, 4096, 65536])
def test_projection_skips_content_and_preserves_structural_paths(size):
    original = {
        "type": "response.completed",
        "response": {
            "output": [{"content": '雪😀 \\u1234 \\" {"usage":{"cost":"999"}}' * 20_000}],
            "metadata": {"usage": {"cost": "999"}},
            "usage": {"input_tokens": 7, "cost": "0.00000001",
                      "input_tokens_details": {"cached_tokens": 2}},
            "id": "response-one",
        },
    }
    wire = json.dumps(original, ensure_ascii=False).encode()
    parser = UsageProjection()
    for start in range(0, len(wire), size):
        parser.feed(wire[start:start + size])
        assert len(parser.token) < 300
        assert len(parser.frames) < 10
    assert parser.finish() == {
        "type": "response.completed",
        "response": {"usage": original["response"]["usage"], "id": "response-one"},
    }
    assert parser.retained < 200


@pytest.mark.parametrize("wire", [
    b'{"usage":{"cost":0.1},"output":"bad\\q"}',
    b'{"usage":{"cost":0.1},"output":"bad\n"}',
    b'{"usage":{"cost":0.1},"output":[1,]}',
    b'{"usage":{"cost":0.1},"output":{"a":1,}}',
    b'{"usage":{"cost":0.1},"output":[true false]}',
    b'{"usage":{"cost":0.1},"output":01}',
    b'{"usage":{"cost":0.1},"output":NaN}',
    b'{"usage":{"cost":0.1},"output":"\xff"}',
    b'{"usage":{"cost":0.1}} trailing',
    b'{"usage":{"cost":0.1},"output":"unterminated',
])
def test_invalid_discarded_content_cannot_certify_a_cost(wire):
    parser = UsageProjection()
    with pytest.raises((ValueError, UnicodeError)):
        for byte in wire:
            parser.feed(bytes([byte]))
        parser.finish()


def test_duplicate_keys_follow_json_semantics_and_numbers_keep_precision():
    parser = UsageProjection()
    parser.feed(b'{"usage":{"cost":1.234567890123456789e-10},"usage":'
                b'{"cost":0.000000000000000000123456789,"input_tokens":0}}')
    assert parser.finish() == {
        "usage": {"cost": Decimal("0.000000000000000000123456789"), "input_tokens": 0},
    }


def test_selected_metadata_and_nesting_are_still_bounded():
    parser = UsageProjection(max_bytes=100)
    with pytest.raises(ProjectionError, match="oversized_usage_metadata"):
        parser.feed(b'{"usage":{"cost":"' + b"1" * 101 + b'"}}')
    parser = UsageProjection(max_depth=8)
    with pytest.raises(ProjectionError, match="usage_nesting_limit"):
        parser.feed(b'{"output":' + b"[" * 8)


def test_unknown_long_keys_do_not_consume_receipt_budget():
    parser = UsageProjection(max_bytes=100)
    wire = json.dumps({"x" * 10_000: "ignored", "usage": {"cost": "0.1"}}).encode()
    for start in range(0, len(wire), 97):
        parser.feed(wire[start:start + 97])
    assert parser.finish() == {"usage": {"cost": "0.1"}}
