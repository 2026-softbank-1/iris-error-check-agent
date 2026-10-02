import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError
from test_api import KEY, send
from test_backend_api import envelope
from test_direct_api import response_body
from test_source_analysis import FakeRuntime, api_input, selection, source_response

from ai_error_check_agent.api import create_app
from ai_error_check_agent.direct_api import (
    DirectModelProfile,
    OpenAIResponsesRuntime,
    load_direct_settings,
)
from ai_error_check_agent.errors import DiagnosisError
from ai_error_check_agent.model_catalog import load_catalog, model_id
from ai_error_check_agent.opencode_process import (
    ManagedOpenCode,
    isolated_environment,
    runtime_config,
)


def catalog(tmp_path, sakana=True, **extra):
    return load_catalog(
        tmp_path / "absent.env",
        environ={
            "LLM_MODEL": "gpt-6.1-sol",
            "LLM_API_KEY": "OPENAI_CANARY",
            **({"SAKANA_API_KEY": "SAKANA_CANARY"} if sakana else {}),
            **extra,
        },
    )


def test_catalog_has_shared_ids_without_keys_and_missing_provider_is_disabled(tmp_path):
    value = catalog(tmp_path, False)
    assert value.default == "openai/gpt-6.1-sol"
    assert value.public()["models"][0]["available"]
    assert not next(c for c in value.public()["models"] if c["id"] == "sakana/fugu")["available"]
    assert "CANARY" not in json.dumps(value.public())
    with pytest.raises(DiagnosisError) as e:
        value.select("sakana/fugu")
    assert e.value.code == "MODEL_NOT_CONFIGURED"


@pytest.mark.parametrize(
    "changes",
    [
        {"SAKANA_MODELS": "fugu,fugu"},
        {"SAKANA_MODELS": "../outside"},
        {"SAKANA_API_KEY": "bad key"},
        {"SAKANA_REASONING_EFFORT": "low"},
        {"SAKANA_TIMEOUT_SECONDS": "500"},
        {"SAKANA_MAX_OUTPUT_TOKENS": "0"},
    ],
)
def test_bad_server_catalog_rejected(tmp_path, changes):
    with pytest.raises(DiagnosisError):
        catalog(tmp_path, **changes)


def test_provider_keys_and_endpoints_never_cross(tmp_path):
    value = catalog(tmp_path)
    default = value.select()
    env = isolated_environment(
        tmp_path / "run", default.profile, default.key, "test-password", {}, catalog=value
    )
    assert env["OPENAI_API_KEY"] == "OPENAI_CANARY"
    assert env["SAKANA_API_KEY"] == "SAKANA_CANARY"
    assert "CANARY" not in env["OPENCODE_CONFIG_CONTENT"]
    config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    assert config["provider"]["sakana"]["options"]["baseURL"] == "https://api.sakana.ai/v1"
    assert config["provider"]["sakana"]["options"]["apiKey"] == "{env:SAKANA_API_KEY}"
    assert config["provider"]["sakana"]["npm"] == "@ai-sdk/openai"
    variants = config["provider"]["sakana"]["models"]["fugu"]["variants"]
    assert variants["low"]["disabled"] is True
    assert variants["medium"]["disabled"] is True
    assert variants["high"]["reasoningEffort"] == "high"
    assert config["provider"]["openai"]["whitelist"] == ["gpt-6.1-sol"]
    assert config["agent"]["build"]["disable"] is True
    for provider, endpoint in [
        ("openai", "https://api.sakana.ai/v1"),
        ("sakana", "https://api.openai.com/v1"),
    ]:
        with pytest.raises(ValidationError):
            DirectModelProfile(
                profile_id="test",
                provider_id=provider,
                model_id="x",
                base_url=endpoint,
                reasoning_effort="high",
            )
    with pytest.raises(DiagnosisError) as e:
        load_direct_settings(
            tmp_path / "none",
            "test",
            {"LLM_PROVIDER": "sakana", "LLM_MODEL": "fugu", "LLM_API_KEY": "OPENAI_CANARY"},
        )
    assert e.value.code == "MISSING_CONFIG"


def test_models_endpoint_requires_auth_and_errors_do_not_call_model(tmp_path):
    value = catalog(tmp_path, False)

    def never(*_):
        pytest.fail("No model call expected")

    app = create_app(never, api_key=KEY, model_catalog=value, model_runtime_factory=never)
    assert send(app, "GET", "/models").status_code == 401
    response = send(app, "GET", "/models", headers={"X-API-Key": KEY})
    assert response.json() == value.public()
    for model, status in [("sakana/fugu", 503), ("unknown/model", 422), ("", 422)]:
        response = send(
            app,
            path="/diagnose",
            params={"model": model},
            json=envelope(),
            headers={"X-API-Key": KEY},
        )
        assert response.status_code == status
    assert "CANARY" not in response.text
    schema = app.openapi()
    assert schema["paths"]["/models"]["get"]["security"]
    assert any(p["name"] == "model" for p in schema["paths"]["/diagnose"]["post"]["parameters"])


def test_concurrent_two_stage_requests_keep_their_selected_models(
    tmp_path, request_data, analysis_data
):
    value = catalog(tmp_path)
    created = []
    counts = {}
    gate = asyncio.Event()

    class Slow(FakeRuntime):
        async def run(self, *args):
            if len(created) < 2:
                await gate.wait()
            else:
                gate.set()
            await asyncio.sleep(0)
            return await super().run(*args)

    def create(choice):
        name = model_id(choice.profile)
        index = counts.get(name, 0)
        counts[name] = index + 1
        runtime = Slow(
            selection(analysis_data) if index == 0 else source_response(analysis_data), []
        )
        runtime.profile = choice.profile
        created.append(runtime)
        return runtime

    app = create_app(lambda: None, dev=True, model_catalog=value, model_runtime_factory=create)

    async def call():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as c:
            return await asyncio.gather(
                *[
                    c.post("/diagnose", params={"model": name}, json=api_input(request_data))
                    for name in ["openai/gpt-6.1-sol", "sakana/fugu"]
                ]
            )

    responses = asyncio.run(call())
    for response, provider in zip(responses, ["openai", "sakana"], strict=True):
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["source_analysis"]["status"] == "analyzed"
        assert len(result["execution"]["stages"]) == 2
        assert all(
            s["requested_model"]["provider_id"] == provider for s in result["execution"]["stages"]
        )
    assert counts == {"openai/gpt-6.1-sol": 2, "sakana/fugu": 2}
    assert all(r.closed for r in created)


@pytest.mark.parametrize("raw", [False, True])
def test_backend_envelope_and_raw_data_use_selected_profile(tmp_path, analysis_data, raw):
    value = catalog(tmp_path)

    def create(choice):
        runtime = FakeRuntime(selection(analysis_data, False), [])
        runtime.profile = choice.profile
        return runtime

    app = create_app(lambda: None, dev=True, model_catalog=value, model_runtime_factory=create)
    payload = envelope()
    payload["data"]["source"] = None
    response = send(app, params={"model": "sakana/fugu"}, json=payload["data"] if raw else payload)
    assert response.status_code == 200, response.text
    assert response.json()["execution"]["stages"][0]["requested_model"] == {
        "provider_id": "sakana",
        "model_id": "fugu",
    }


def test_managed_profiles_are_independent_and_ui_uses_same_catalog(tmp_path):
    value = catalog(tmp_path)
    choice = value.select()
    manager = ManagedOpenCode(choice.profile, choice.key, root=tmp_path, catalog=value)
    a = manager.runtime(value.select("sakana/fugu").profile)
    b = manager.runtime(value.select().profile)
    assert a.profile.provider_id == "sakana"
    assert b.profile.provider_id == manager.profile.provider_id == "openai"
    ui = runtime_config(choice.profile, catalog=value, interactive=True)
    assert "대화형 진단" in ui["agent"]["iris_diagnosis"]["prompt"]
    assert ui["provider"] == runtime_config(choice.profile, catalog=value)["provider"]

    async def close():
        await a.close()
        await b.close()

    asyncio.run(close())


def test_sakana_responses_adapter_uses_sakana_key_and_reports_provider(tmp_path, analysis_data):
    choice = catalog(tmp_path).select("sakana/fugu-max")

    def handler(request):
        assert str(request.url) == "https://api.sakana.ai/v1/responses"
        assert request.headers["authorization"] == "Bearer SAKANA_CANARY"
        body = json.loads(request.content)
        assert body["model"] == "fugu-max"
        assert body["reasoning"]["effort"] == "high"
        response = response_body(analysis_data)
        response["model"] = "fugu-max-v1.0"
        return httpx.Response(200, json=response)

    async def run():
        runtime = OpenAIResponsesRuntime(
            choice.profile, choice.key, transport=httpx.MockTransport(handler)
        )
        try:
            result = await runtime.run("test", {}, {})
            assert result.metadata["reported_model"] == {
                "provider_id": "sakana",
                "model_id": "fugu-max-v1.0",
            }
        finally:
            await runtime.close()

    asyncio.run(run())
