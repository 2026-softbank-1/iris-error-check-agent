import asyncio
import copy
import json
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from ai_error_check_agent.cli import main
from ai_error_check_agent.direct_api import (
    DirectModelProfile,
    OpenAIResponsesRuntime,
    generation_schema,
    load_direct_settings,
)
from ai_error_check_agent.errors import DiagnosisError
from ai_error_check_agent.service import diagnose
from ai_error_check_agent.validation import load_schema

ROOT = Path(__file__).resolve().parents[1]
FAKE_KEY = "test-only-secret-never-send"


def direct_profile(**kwargs):
    return DirectModelProfile(profile_id="profile-demo-a", model_id="gpt-6.1-sol", **kwargs)


def response_body(analysis):
    return {
        "status": "completed",
        "model": "gpt-6.1-sol",
        "error": None,
        "output": [
            {"type": "reasoning", "summary": []},
            {
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": json.dumps(analysis)}],
            },
        ],
        "usage": {
            "input_tokens": 100,
            "output_tokens": 200,
            "output_tokens_details": {"reasoning_tokens": 80},
        },
    }


def execute(request, handler, profile=None):
    async def run():
        runtime = OpenAIResponsesRuntime(
            profile or direct_profile(), SecretStr(FAKE_KEY), transport=httpx.MockTransport(handler)
        )
        try:
            return await diagnose(request, runtime)
        finally:
            await runtime.close()

    return asyncio.run(run())


def test_env_loading_quotes_bom_and_process_precedence(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        'LLM_API_KEY="file-secret"\nLLM_PROVIDER=openai\nLLM_MODEL=gpt-6.1-sol\n', "utf-8-sig"
    )
    profile, key = load_direct_settings(env, "profile-demo-a", {"LLM_API_KEY": FAKE_KEY})
    assert key.get_secret_value() == FAKE_KEY
    assert FAKE_KEY not in repr(key) + repr(profile)
    assert profile.model_id == "gpt-6.1-sol"
    assert profile.base_url == "https://api.openai.com/v1"
    assert profile.timeout_seconds == 60


def test_env_does_not_expand_other_credentials(tmp_path):
    env = tmp_path / ".env"
    env.write_text("LLM_API_KEY=${UNRELATED_SECRET}\nLLM_MODEL=gpt-6.1-sol", "utf-8")
    _, key = load_direct_settings(env, "profile-demo-a", {})
    assert key.get_secret_value() == "${UNRELATED_SECRET}"


@pytest.mark.parametrize(
    ("settings", "code"),
    [
        ({}, "MISSING_CONFIG"),
        ({"LLM_API_KEY": ""}, "MISSING_CONFIG"),
        ({"LLM_PROVIDER": "other"}, "INVALID_CONFIG"),
        ({"LLM_BASE_URL": "https://attacker.example/v1"}, "INVALID_CONFIG"),
        ({"LLM_BASE_URL": "https://api.openai.com/v1?redirect=other"}, "INVALID_CONFIG"),
        ({"LLM_TIMEOUT_SECONDS": "nan"}, "INVALID_CONFIG"),
        ({"LLM_REASONING_EFFORT": "none"}, "INVALID_CONFIG"),
        ({"LLM_API_KEY": "secret\nwith-newline"}, "INVALID_CONFIG"),
    ],
)
def test_bad_config_fails_without_echoing_values(tmp_path, settings, code):
    values = {"LLM_API_KEY": FAKE_KEY, "LLM_MODEL": "gpt-6.1-sol"} if settings else {}
    values.update(settings)
    with pytest.raises(DiagnosisError) as error:
        load_direct_settings(tmp_path / "missing.env", "profile-demo-a", values)
    assert error.value.code == code
    assert FAKE_KEY not in str(error.value)


def test_responses_request_and_result(diagnosis_request, analysis_data):
    sent = []

    def handler(request):
        assert str(request.url) == "https://api.openai.com/v1/responses"
        assert request.headers["authorization"] == "Bearer " + FAKE_KEY
        body = json.loads(request.content)
        sent.append(body)
        assert body["model"] == "gpt-6.1-sol"
        assert body["store"] is False
        assert body["tools"] == []
        assert body["text"]["format"]["strict"] is True
        assert body["max_output_tokens"] == 4096
        assert "untrusted_log_data" in body["input"][0]["content"]
        assert FAKE_KEY not in request.content.decode()
        assert "team-iris" not in request.content.decode()
        return httpx.Response(200, json=response_body(analysis_data))

    result = execute(diagnosis_request, handler)
    assert len(sent) == 1
    assert result["job_status"] == "succeeded"
    assert result["analysis"]["analysis_status"] == "diagnosed"
    assert result["schema_version"] == "diagnosis-result.v2"
    assert result["remediation_execution"] == "not_executed"
    assert result["analysis"]["remediation"]["status"] == "proposed"
    assert result["execution"]["provider_call_count"] == 1
    assert result["execution"]["tokens"] == {"input": 100, "output": 200, "reasoning": 80}
    assert result["deployment_context"]["deployment_status"] == "failed"
    assert FAKE_KEY not in json.dumps(result)


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, "MODEL_AUTH_ERROR"),
        (403, "MODEL_AUTH_ERROR"),
        (404, "MODEL_NOT_FOUND"),
        (429, "MODEL_RATE_LIMIT"),
        (400, "MODEL_REQUEST_ERROR"),
        (503, "MODEL_ERROR"),
        (302, "MODEL_ERROR"),
    ],
)
def test_http_failures_are_safe_and_not_retried(diagnosis_request, status, code):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            status,
            json={"error": {"message": FAKE_KEY}},
            headers={"Location": "https://attacker.example"},
        )

    result = execute(diagnosis_request, handler)
    assert result["error"]["code"] == code
    assert result["analysis"] is None
    assert FAKE_KEY not in json.dumps(result)
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (lambda d: d.update(status="incomplete"), "INCOMPLETE_RESPONSE"),
        (lambda d: d.update(model="another-model"), "MODEL_MISMATCH"),
        (lambda d: d.update(output=[{"type": "function_call"}]), "UNEXPECTED_TOOL_CALL"),
        (
            lambda d: d["output"][1].update(content=[{"type": "refusal", "refusal": FAKE_KEY}]),
            "MODEL_REFUSAL",
        ),
        (lambda d: d["output"][1].update(status="in_progress"), "INCOMPLETE_RESPONSE"),
        (lambda d: d.update(output=[]), "INCOMPLETE_RESPONSE"),
    ],
)
def test_invalid_response_never_becomes_diagnosis(diagnosis_request, analysis_data, mutation, code):
    body = response_body(analysis_data)
    mutation(body)
    result = execute(diagnosis_request, lambda _: httpx.Response(200, json=body))
    assert result["job_status"] == "failed"
    assert result["error"]["code"] == code
    assert result["analysis"] is None
    assert FAKE_KEY not in json.dumps(result)


def test_direct_result_still_enforces_full_local_schema(diagnosis_request, analysis_data):
    analysis_data["observations"][0]["evidence_ids"] = ["EV000001", "EV000001"]
    result = execute(
        diagnosis_request, lambda _: httpx.Response(200, json=response_body(analysis_data))
    )
    assert result["error"]["code"] == "INVALID_SCHEMA"
    assert result["analysis"] is None


def test_deadline_does_not_claim_remote_cancellation(diagnosis_request):
    async def handler(request):
        await asyncio.sleep(0.2)
        raise AssertionError("Should be cancelled locally")

    result = execute(diagnosis_request, handler, direct_profile(timeout_seconds=0.02))
    assert result["job_status"] == "timed_out"
    assert result["execution"]["abort_confirmed"] is False
    assert result["execution"]["runtime_reusable"] is False


def test_generation_schema_leaves_validation_schema_unchanged():
    original = load_schema()
    snapshot = copy.deepcopy(original)
    api_schema = generation_schema(original)
    assert original == snapshot
    assert "uniqueItems" not in json.dumps(api_schema)
    assert "maxLength" in json.dumps(original)


def test_cli_no_profile_uses_env_and_direct_adapter(tmp_path, monkeypatch, analysis_data, capsys):
    env = tmp_path / ".env"
    env.write_text(f"LLM_API_KEY={FAKE_KEY}\nLLM_MODEL=gpt-6.1-sol", "utf-8")
    for key in ("LLM_API_KEY", "LLM_PROVIDER", "LLM_MODEL", "LLM_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    original = OpenAIResponsesRuntime

    def factory(profile, key):
        return original(
            profile,
            key,
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json=response_body(analysis_data))
            ),
        )

    monkeypatch.setattr("ai_error_check_agent.cli.OpenAIResponsesRuntime", factory)
    assert (
        main(
            [
                "diagnose",
                "--request",
                str(ROOT / "examples/configuration.request.json"),
                "--env-file",
                str(env),
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["execution"]["runtime_version"] == "openai-responses.v1"
