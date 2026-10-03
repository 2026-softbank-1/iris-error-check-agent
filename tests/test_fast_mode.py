import asyncio
import json
import logging

import httpx
import pytest
from pydantic import SecretStr, ValidationError
from test_direct_api import FAKE_KEY, direct_profile, execute, response_body
from test_source_analysis import api_input, selection, source_response

from ai_error_check_agent.api import create_app
from ai_error_check_agent.direct_api import (
    DirectModelProfile,
    OpenAIResponsesRuntime,
    load_direct_settings,
)
from ai_error_check_agent.errors import DiagnosisError
from ai_error_check_agent.knowledge.reasoning import ReasoningService, ReasoningSettings
from ai_error_check_agent.model_catalog import load_catalog
from ai_error_check_agent.opencode_process import runtime_config
from ai_error_check_agent.source_analysis import diagnose_with_source
from ai_error_check_agent.source_contracts import DiagnoseAPIRequest, DiagnoseAPIResult


@pytest.mark.parametrize("tier", ["", "auto", "default", "fast", "priority"])
def test_tier_env_is_optional_and_process_overrides_file(tmp_path, tier):
    env = tmp_path / ".env"
    env.write_text(f"LLM_API_KEY={FAKE_KEY}\nLLM_MODEL=gpt-6.1-sol\nOPENAI_SERVICE_TIER=fast\n")
    profile, _ = load_direct_settings(env, "profile-demo-a", {"OPENAI_SERVICE_TIER": tier})
    assert profile.service_tier == (tier or None)
    assert load_direct_settings(env, "profile-demo-a", {})[0].service_tier == "fast"


def test_invalid_tier_fails_safely_and_sakana_cannot_receive_it(tmp_path):
    config = {"LLM_API_KEY": FAKE_KEY, "LLM_MODEL": "gpt-6.1-sol", "OPENAI_SERVICE_TIER": FAKE_KEY}
    for loader in (
        lambda: load_direct_settings(tmp_path / "absent", "test", config),
        lambda: load_catalog(tmp_path / "absent", environ=config),
    ):
        with pytest.raises(DiagnosisError) as failure:
            loader()
        assert failure.value.code == "INVALID_CONFIG"
        assert FAKE_KEY not in str(failure.value)
    with pytest.raises(ValidationError):
        DirectModelProfile(
            profile_id="test",
            provider_id="sakana",
            model_id="fugu",
            base_url="https://api.sakana.ai/v1",
            reasoning_effort="high",
            service_tier="fast",
        )


@pytest.mark.parametrize("default_provider", ["openai", "sakana"])
def test_catalog_and_managed_opencode_isolate_tier_to_openai(tmp_path, default_provider):
    config = {
        "LLM_PROVIDER": default_provider,
        "LLM_MODEL": "fugu" if default_provider == "sakana" else "gpt-6.1-sol",
        "OPENAI_API_KEY": FAKE_KEY,
        "SAKANA_API_KEY": "sakana-test-key",
        "OPENAI_MODELS": "gpt-6.1-sol,gpt-6-sol",
        "OPENAI_SERVICE_TIER": "fast",
    }
    catalog = load_catalog(tmp_path / "absent", environ=config)
    standard = load_catalog(tmp_path / "absent", environ={**config, "OPENAI_SERVICE_TIER": ""})
    assert catalog.public() == standard.public()
    managed = runtime_config(catalog.select().profile, catalog=catalog)
    for choice in catalog.choices.values():
        profile = choice.profile
        options = managed["provider"][profile.provider_id]["models"][profile.model_id]["options"]
        if profile.provider_id == "openai":
            assert profile.service_tier == "fast"
            assert options["serviceTier"] == "priority"
        else:
            assert profile.service_tier is None
            assert "serviceTier" not in options
    sakana = catalog.select("sakana/fugu")
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(400)

    async def run():
        runtime = OpenAIResponsesRuntime(
            sakana.profile, sakana.key, transport=httpx.MockTransport(handler)
        )
        try:
            with pytest.raises(DiagnosisError):
                await runtime.run("test", {}, {})
        finally:
            await runtime.close()

    asyncio.run(run())
    assert len(sent) == 1 and "service_tier" not in sent[0]


@pytest.mark.parametrize("tier", [None, "auto", "default", "fast", "priority"])
def test_http_request_only_adds_tier_and_public_result_is_preserved(
    diagnosis_request, analysis_data, tier
):
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        body = response_body(analysis_data)
        body["service_tier"] = "default"
        return httpx.Response(200, json=body)

    baseline = execute(diagnosis_request, handler)
    enabled = execute(diagnosis_request, handler, direct_profile(service_tier=tier))
    expected = dict(sent[0])
    if tier is not None:
        expected["service_tier"] = tier
    assert sent[1] == expected
    assert enabled["analysis"] == baseline["analysis"]
    assert enabled.keys() == baseline.keys()
    assert "service_tier" not in json.dumps(enabled)
    options = runtime_config(direct_profile(service_tier=tier))["provider"]["openai"]["models"][
        "gpt-6.1-sol"
    ]["options"]
    assert options.get("serviceTier") == ("priority" if tier == "fast" else tier)
    if tier is None:
        assert "serviceTier" not in options


@pytest.mark.parametrize(
    "reported", ["fast", "priority", "default", None, "untrusted\nCANARY", {"bad": True}]
)
def test_actual_tier_is_internal_and_never_assumed(analysis_data, caplog, reported):
    caplog.set_level(logging.INFO, logger="ai_error_check_agent.direct_api")

    def handler(_):
        body = response_body(analysis_data)
        body["service_tier"] = reported
        return httpx.Response(200, json=body)

    async def run():
        runtime = OpenAIResponsesRuntime(
            direct_profile(service_tier="fast"),
            SecretStr(FAKE_KEY),
            transport=httpx.MockTransport(handler),
        )
        try:
            result = await runtime.run("test", {}, {})
            assert "service_tier" not in result.metadata
            assert runtime.reported_service_tier == (
                reported if reported in ("fast", "priority", "default") else None
            )
        finally:
            await runtime.close()

    asyncio.run(run())
    assert "requested=fast" in caplog.text
    assert "CANARY" not in caplog.text and FAKE_KEY not in caplog.text
    assert (
        f"reported={reported if reported in ('fast', 'priority', 'default') else 'unknown'}"
        in caplog.text
    )


@pytest.mark.parametrize("reasoning_mode", ["off", "assist"])
def test_fast_mode_reaches_both_stages_without_changing_openapi(
    request_data, analysis_data, reasoning_mode
):
    sent = []
    responses = [selection(analysis_data), source_response(analysis_data)]

    def handler(request):
        body = json.loads(request.content)
        assert body["service_tier"] == "fast"
        assert body["reasoning"] == {"effort": "low"}
        result = response_body(responses[len(sent)])
        result["service_tier"] = "priority"
        sent.append(body)
        return httpx.Response(200, json=result)

    profile = direct_profile(service_tier="fast")

    def factory():
        return OpenAIResponsesRuntime(
            profile, SecretStr(FAKE_KEY), transport=httpx.MockTransport(handler)
        )

    async def run():
        service = ReasoningService(ReasoningSettings(mode=reasoning_mode))
        try:
            if reasoning_mode == "assist":
                assert await service.wait_ready()
            result = await diagnose_with_source(
                DiagnoseAPIRequest.model_validate_json(json.dumps(api_input(request_data))),
                factory,
                reasoning_service=service,
            )
            DiagnoseAPIResult.model_validate(result)
            assert result["source_analysis"]["status"] == "analyzed"
            assert len(result["execution"]["stages"]) == 2
            assert "service_tier" not in json.dumps(result)
        finally:
            await service.aclose()

    asyncio.run(run())
    assert len(sent) == 2
    baseline = create_app(
        lambda: OpenAIResponsesRuntime(direct_profile(), SecretStr(FAKE_KEY)), dev=True
    )
    enabled = create_app(
        factory, dev=True, reasoning_settings=ReasoningSettings(mode=reasoning_mode)
    )
    assert baseline.openapi() == enabled.openapi()


def test_fast_rejection_is_not_retried(diagnosis_request):
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(400, json={"error": {"message": FAKE_KEY}})

    result = execute(diagnosis_request, handler, direct_profile(service_tier="fast"))
    assert result["error"]["code"] == "MODEL_REQUEST_ERROR"
    assert len(sent) == 1 and sent[0]["service_tier"] == "fast"
    assert FAKE_KEY not in json.dumps(result)
