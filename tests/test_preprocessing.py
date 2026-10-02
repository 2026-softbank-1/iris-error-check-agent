import json

import pytest
from pydantic import ValidationError

from ai_error_check_agent.contracts import DiagnosisRequest
from ai_error_check_agent.errors import DiagnosisError
from ai_error_check_agent.preprocessing import prepare, redact


def parse(data):
    return DiagnosisRequest.model_validate_json(json.dumps(data))


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("PASSWORD=hunter123", "hunter123"),
        ('{"api_key": "not-for-the-model"}', "not-for-the-model"),
        ('password="two word value"', "two word value"),
        ("DATABASE_URL=postgres://alice:topsecret@database:5432/app", "topsecret"),
        ("Authorization: Bearer abcdefghij", "abcdefghij"),
        ("Bearer abc.def.ghi", "abc.def.ghi"),
        ("Cookie: session=private; user=private", "private"),
        ("request /?access_token=hide-me&mode=1", "hide-me"),
        ("-----BEGIN PRIVATE KEY-----\nabc123\ndef456\n-----END PRIVATE KEY-----", "abc123"),
        ("-----BEGIN RSA PRIVATE KEY-----\nunclosed-secret", "unclosed-secret"),
        ("key=AKIAABCDEFGHIJKLMNOP", "AKIAABCDEFGHIJKLMNOP"),
    ],
)
def test_secrets_masked_without_losing_lines(text, secret):
    masked = redact(text)
    assert secret not in masked
    assert masked.count("\n") == text.count("\n")


def test_original_positions_survive_multiline_masking(request_data):
    request_data["logs"][0]["text"] = (
        "-----BEGIN PRIVATE KEY-----\r\nabc123\r\n-----END PRIVATE KEY-----\r\n"
        "\x1b[31mERROR missing DATABASE_URL\x1b[0m\r\n"
    )
    bundle = prepare(parse(request_data))
    assert len(bundle.lines) == 4
    assert bundle.lines[-1].id == "EV000004"
    assert bundle.lines[-1].source_line == 44
    assert bundle.lines[-1].text == "ERROR missing DATABASE_URL"
    assert "abc123" not in json.dumps(bundle.model_payload())
    assert bundle.limitations


def test_env_name_and_incomplete_marker_preserved(request_data):
    request_data["logs"][0]["is_complete"] = False
    bundle = prepare(parse(request_data))
    assert "DATABASE_URL" in bundle.lines[0].text
    assert "누락" in bundle.limitations[0]
    assert "tenant_id" not in bundle.model_payload()


@pytest.mark.parametrize(
    "text",
    ["", "   \n\t", "a" * (1024 * 1024 + 1), "x\n" * 10_001],
    ids=["empty", "whitespace", "byte_limit", "line_limit"],
)
def test_empty_or_oversized_input_rejected(request_data, text):
    request_data["logs"][0]["text"] = text
    with pytest.raises(ValidationError):
        parse(request_data)


def test_controls_only_rejected(request_data):
    request_data["logs"][0]["text"] = "\x1b[31m\x00"
    with pytest.raises(DiagnosisError, match="분석할 내용"):
        prepare(parse(request_data))


def test_duplicate_chunks_and_extra_fields_rejected(request_data):
    request_data["logs"].append(request_data["logs"][0])
    with pytest.raises(ValidationError):
        parse(request_data)
    request_data["logs"].pop()
    request_data["url"] = "https://untrusted.example"
    with pytest.raises(ValidationError):
        parse(request_data)


def test_large_model_input_rejected_without_silent_truncation(request_data):
    request_data["logs"][0]["text"] = "INFO first\n" + "x" * 20_000 + "\nERROR last"
    with pytest.raises(DiagnosisError) as error:
        prepare(parse(request_data))
    assert error.value.code == "INPUT_TOO_LARGE"


def test_repeated_evidence_retains_distinct_positions(request_data):
    request_data["logs"][0]["text"] = "ERROR retry\nINFO recovered\nERROR retry"
    bundle = prepare(parse(request_data))
    assert [line.id for line in bundle.lines] == ["EV000001", "EV000002", "EV000003"]
    assert bundle.lines[1].text == "INFO recovered"


def test_snapshot_hash_includes_context_and_input_completeness(request_data):
    original = prepare(parse(request_data)).snapshot_sha256
    request_data["context"]["exit_code"] = 137
    changed_context = prepare(parse(request_data)).snapshot_sha256
    request_data["logs"][0]["is_complete"] = False
    changed_completeness = prepare(parse(request_data)).snapshot_sha256
    assert len({original, changed_context, changed_completeness}) == 3
