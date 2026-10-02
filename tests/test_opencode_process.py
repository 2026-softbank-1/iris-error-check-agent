import asyncio
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr
from test_runtime import FakeOpenCode
from test_source_analysis import api_input, selection, source_response

from ai_error_check_agent import api
from ai_error_check_agent.direct_api import DirectModelProfile
from ai_error_check_agent.errors import DiagnosisError
from ai_error_check_agent.opencode_process import (
    ManagedOpenCode,
    isolated_environment,
)
from ai_error_check_agent.runtime import OpenCodeRuntime
from ai_error_check_agent.source_analysis import diagnose_with_source
from ai_error_check_agent.source_contracts import DiagnoseAPIRequest


def direct_profile():
    return DirectModelProfile(profile_id="profile-demo-a", model_id="test-model")


def test_child_environment_and_model_credentials_are_isolated(tmp_path):
    env = isolated_environment(
        tmp_path,
        direct_profile(),
        SecretStr("KEY_CANARY"),
        "PASS_CANARY",
        {
            "PATH": "system-bin",
            "SYSTEMROOT": "system",
            "OPENCODE_CONFIG": "personal.json",
            "ANTHROPIC_API_KEY": "OTHER_KEY",
            "OPENAI_API_KEY": "WRONG_KEY",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "https://outside.invalid",
            "OPENCODE_PERMISSION": "allow",
        },
    )
    assert env["OPENAI_API_KEY"] == "KEY_CANARY"
    assert env["OPENCODE_SERVER_PASSWORD"] == "PASS_CANARY"
    assert (
        not {
            "OPENCODE_CONFIG",
            "ANTHROPIC_API_KEY",
            "OTEL_EXPORTER_OTLP_ENDPOINT",
            "OPENCODE_PERMISSION",
        }
        & env.keys()
    )
    assert env["PATH"] == "system-bin"
    assert env["OPENCODE_DISABLE_PROJECT_CONFIG"] == "true"
    assert env["OPENCODE_PURE"] == "true"
    assert "KEY_CANARY" not in env["OPENCODE_CONFIG_CONTENT"]
    config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    assert config["model"] == "openai/test-model"
    assert config["provider"]["openai"]["options"]["baseURL"] == "https://api.openai.com/v1"
    assert config["agent"]["iris_diagnosis"]["permission"] == "deny"
    for key in (
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
        "OPENCODE_TEST_HOME",
    ):
        assert Path(env[key]).is_relative_to(tmp_path)
    assert not any(p.is_file() for p in tmp_path.rglob("*"))


def test_missing_install_never_starts_process(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("unexpected process"))
    with pytest.raises(DiagnosisError, match="먼저 설치"):
        ManagedOpenCode(direct_profile(), SecretStr("key"), root=tmp_path).start()


@pytest.mark.parametrize("port", [0, -1, 65536])
def test_invalid_port(tmp_path, port):
    with pytest.raises(DiagnosisError):
        ManagedOpenCode(direct_profile(), SecretStr("key"), root=tmp_path, port=port)


def fake_binary(tmp_path, suffix=None):
    suffix = suffix or ("opencode.exe" if os.name == "nt" else "opencode")
    binary = tmp_path / ".runtime/opencode-tooling/node_modules/opencode-ai/bin" / suffix
    binary.parent.mkdir(parents=True)
    binary.touch()


@pytest.mark.parametrize("filename", ["opencode.exe", "opencode"])
def test_version_mismatch_stops_before_start(tmp_path, monkeypatch, filename):
    fake_binary(tmp_path, filename)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=b"unexpected"))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("unexpected process"))
    with pytest.raises(DiagnosisError) as failure:
        ManagedOpenCode(direct_profile(), SecretStr("key"), root=tmp_path).start()
    assert failure.value.code == "RUNTIME_VERSION_MISMATCH"


def test_stop_kills_only_owned_process_after_timeout(tmp_path):
    server = ManagedOpenCode(direct_profile(), SecretStr("key"), root=tmp_path)
    events = []

    def wait(timeout):
        events.append("wait")
        if len(events) == 2:
            raise subprocess.TimeoutExpired("owned", timeout)

    server.process = SimpleNamespace(
        poll=lambda: None,
        terminate=lambda: events.append("terminate"),
        wait=wait,
        kill=lambda: events.append("kill"),
    )
    server.stop()
    assert events == ["terminate", "wait", "kill", "wait"]


@pytest.mark.parametrize("mode", ["direct", "opencode"])
@pytest.mark.parametrize("server_error", [False, True])
def test_api_runtime_choice_and_shutdown(tmp_path, monkeypatch, mode, server_error):
    import uvicorn

    events = []

    class Managed:
        def __init__(self, *args, **kwargs):
            events.append("init")

        def runtime(self):
            raise AssertionError("No LLM requested")

        def start(self):
            events.append("start")

        def stop(self):
            events.append("stop")

    def run(app, **kwargs):
        async def serve():
            async with app.router.lifespan_context(app):
                events.append("serve")
                if server_error:
                    raise RuntimeError("test shutdown")

        asyncio.run(serve())

    monkeypatch.setattr(api, "ManagedOpenCode", Managed)
    monkeypatch.setattr(
        api, "load_direct_settings", lambda *a: (direct_profile(), SecretStr("key"))
    )
    monkeypatch.setattr(uvicorn, "run", run)
    monkeypatch.setattr(api, "dotenv_values", lambda *a, **k: {"AGENT_RUNTIME": mode})
    monkeypatch.delenv("AGENT_RUNTIME", raising=False)
    args = ["--env-file", str(tmp_path / ".env"), "--dev"]
    if server_error:
        with pytest.raises(RuntimeError):
            api.main(args)
    else:
        api.main(args)
    assert events == (["init", "start", "serve", "stop"] if mode == "opencode" else ["serve"])


def test_both_diagnosis_stages_use_new_opencode_sessions(request_data, profile, analysis_data):
    fakes = [FakeOpenCode(selection(analysis_data)), FakeOpenCode(source_response(analysis_data))]
    runtimes = []

    def factory():
        runtime = OpenCodeRuntime(profile, transport=httpx.MockTransport(fakes[len(runtimes)]))
        runtimes.append(runtime)
        return runtime

    result = asyncio.run(
        diagnose_with_source(
            DiagnoseAPIRequest.model_validate_json(json.dumps(api_input(request_data))), factory
        )
    )
    assert result["source_analysis"]["status"] == "analyzed"
    assert len(runtimes) == 2
    assert all(r.cleanup_status == "deleted" and r.message_submissions == 1 for r in runtimes)
    first = json.dumps(fakes[0].submitted_body)
    second = json.dumps(fakes[1].submitted_body)
    assert "DATABASE_URl" not in first
    assert "DATABASE_URl" in second
    assert "UNRELATED_CONTENT_MUST_NOT_BE_SENT" not in first + second


def test_runtime_flag_overrides_env_and_invalid_env_fails_before_start(tmp_path, monkeypatch):
    import uvicorn

    served = []
    monkeypatch.setattr(
        api, "load_direct_settings", lambda *a: (direct_profile(), SecretStr("key"))
    )
    monkeypatch.setattr(api, "dotenv_values", lambda *a, **k: {"AGENT_RUNTIME": "invalid"})
    monkeypatch.delenv("AGENT_RUNTIME", raising=False)
    monkeypatch.setattr(api, "ManagedOpenCode", lambda *a, **k: pytest.fail("unexpected process"))
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: served.append(True))
    args = ["--env-file", str(tmp_path / ".env"), "--dev"]
    with pytest.raises(SystemExit) as failure:
        api.main(args)
    assert failure.value.code == 2
    assert not served
    api.main([*args, "--runtime", "direct"])
    assert served == [True]


@pytest.mark.parametrize("field", ["snapshot", "autoupdate", "compaction", "title", "summary"])
def test_preflight_rejects_extra_background_work(profile, analysis_data, field):
    fake = FakeOpenCode(analysis_data)

    async def handler(request):
        response = await fake(request)
        if request.url.path == "/config":
            config = response.json()
            if field in {"title", "summary"}:
                config["agent"][field]["disable"] = False
            elif field == "compaction":
                config[field]["auto"] = True
            else:
                config[field] = True
            return httpx.Response(200, json=config)
        return response

    async def execute():
        runtime = OpenCodeRuntime(profile, transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(DiagnosisError) as failure:
                await runtime.preflight()
            assert failure.value.code == "UNSAFE_RUNTIME"
        finally:
            await runtime.close()

    asyncio.run(execute())
    assert ("POST", "/session") not in fake.calls
