import asyncio
import json

import httpx
import pytest

from ai_error_check_agent.errors import DiagnosisError
from ai_error_check_agent.runtime import OpenCodeRuntime
from ai_error_check_agent.service import diagnose


class FakeOpenCode:
    """HTTP contract double; never calls an actual model/provider."""

    def __init__(
        self,
        analysis,
        *,
        version="test-version",
        config_permission="deny",
        delay=0,
        message_status=200,
        model="test-model",
        finish="stop",
        cleanup_ok=True,
        tool_call=False,
        session_delay=0,
    ):
        self.analysis = analysis
        self.version = version
        self.permission = config_permission
        self.delay = delay
        self.message_status = message_status
        self.model = model
        self.finish = finish
        self.cleanup_ok = cleanup_ok
        self.tool_call = tool_call
        self.session_delay = session_delay
        self.calls = []
        self.submitted_body = None

    async def __call__(self, request):
        path = request.url.path
        self.calls.append((request.method, path))
        if path == "/global/health":
            data = {"healthy": True, "version": self.version}
        elif path == "/config":
            data = {
                "permission": "deny",
                "share": "disabled",
                "agent": {
                    "iris_diagnosis": {"permission": self.permission, "mode": "primary", "steps": 1}
                },
            }
        elif path == "/experimental/tool/ids":
            data = ["bash", "read", "edit", "webfetch", "task", "custom_mcp_tool"]
        elif path == "/session":
            await asyncio.sleep(self.session_delay)
            data = {"id": "ses_test"}
        elif path.endswith("/message"):
            self.submitted_body = json.loads(request.content)
            await asyncio.sleep(self.delay)
            if self.message_status != 200:
                return httpx.Response(
                    self.message_status, json={"private": "SHOULD_NEVER_BE_LOGGED"}
                )
            parts = [{"type": "text", "text": json.dumps(self.analysis)}]
            if self.tool_call:
                parts.append({"type": "tool", "tool": "bash"})
            data = {
                "info": {
                    "sessionID": "ses_test",
                    "role": "assistant",
                    "finish": self.finish,
                    "providerID": "test-provider",
                    "modelID": self.model,
                    "tokens": {"input": 123, "output": 456},
                },
                "parts": parts,
            }
        elif path.endswith("/abort"):
            data = True
        elif request.method == "DELETE":
            data = self.cleanup_ok
        else:
            raise AssertionError(f"Unexpected runtime route: {request.method} {path}")
        return httpx.Response(200, json=data)


def run(request, profile, fake):
    async def execute():
        runtime = OpenCodeRuntime(profile, transport=httpx.MockTransport(fake))
        try:
            return await diagnose(request, runtime)
        finally:
            await runtime.close()

    return asyncio.run(execute())


def test_success_runs_one_session_and_denies_every_tool(diagnosis_request, profile, analysis_data):
    fake = FakeOpenCode(analysis_data)
    result = run(diagnosis_request, profile, fake)
    assert result["job_status"] == "succeeded"
    assert result["deployment_context"]["deployment_status"] == "failed"
    assert result["execution"]["message_submissions"] == 1
    assert result["execution"]["provider_call_count"] is None
    assert result["execution"]["cost"] is None
    assert result["execution"]["cleanup_status"] == "deleted"
    assert result["execution"]["tokens"]["input"] == 123
    assert fake.calls[-1] == ("DELETE", "/session/ses_test")
    assert all(value is False for value in fake.submitted_body["tools"].values())
    assert fake.submitted_body["model"] == {"providerID": "test-provider", "modelID": "test-model"}
    assert "team-iris" not in json.dumps(fake.submitted_body)
    assert "untrusted_log_data" in fake.submitted_body["parts"][0]["text"]


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"version": "unexpected-version"}, "RUNTIME_VERSION_MISMATCH"),
        ({"config_permission": "allow"}, "UNSAFE_RUNTIME"),
        ({"message_status": 401}, "MODEL_AUTH_ERROR"),
        ({"message_status": 503}, "RUNTIME_ERROR"),
        ({"model": "another-model"}, "MODEL_MISMATCH"),
        ({"finish": "length"}, "INCOMPLETE_RESPONSE"),
        ({"tool_call": True}, "UNEXPECTED_TOOL_CALL"),
    ],
)
def test_runtime_failures_never_become_diagnoses(
    diagnosis_request, profile, analysis_data, kwargs, code
):
    fake = FakeOpenCode(analysis_data, **kwargs)
    result = run(diagnosis_request, profile, fake)
    assert result["job_status"] == "failed"
    assert result["analysis"] is None
    assert result["error"]["code"] == code
    assert result["deployment_context"]["deployment_status"] == "failed"
    assert result["execution"]["message_submissions"] <= 1
    assert "SHOULD_NEVER_BE_LOGGED" not in json.dumps(result)
    if code in {"RUNTIME_VERSION_MISMATCH", "UNSAFE_RUNTIME"}:
        assert ("POST", "/session") not in fake.calls
    else:
        assert ("POST", "/session/ses_test/abort") in fake.calls
        assert fake.calls[-1] == ("DELETE", "/session/ses_test")


def test_invalid_model_evidence_is_failed_result(diagnosis_request, profile, analysis_data):
    analysis_data["observations"][0]["evidence_ids"] = ["EV999999"]
    result = run(diagnosis_request, profile, FakeOpenCode(analysis_data))
    assert result["job_status"] == "failed"
    assert result["error"]["code"] == "INVALID_EVIDENCE"
    assert result["execution"]["message_submissions"] == 1


def test_timeout_aborts_deletes_and_preserves_deployment(diagnosis_request, profile, analysis_data):
    profile = profile.model_copy(update={"timeout_seconds": 0.02})
    fake = FakeOpenCode(analysis_data, delay=0.2)
    result = run(diagnosis_request, profile, fake)
    assert result["job_status"] == "timed_out"
    assert result["analysis"] is None
    assert result["execution"]["abort_confirmed"] is True
    assert result["execution"]["cleanup_status"] == "deleted"
    assert result["deployment_context"]["deployment_status"] == "failed"
    assert ("POST", "/session/ses_test/abort") in fake.calls


def test_unknown_session_creation_disables_reuse(diagnosis_request, profile, analysis_data):
    profile = profile.model_copy(update={"timeout_seconds": 0.02})
    fake = FakeOpenCode(analysis_data, session_delay=0.2)
    result = run(diagnosis_request, profile, fake)
    assert result["job_status"] == "timed_out"
    assert result["execution"]["cleanup_status"] == "session_creation_unconfirmed"
    assert result["execution"]["runtime_reusable"] is False
    assert result["execution"]["message_submissions"] == 0


def test_cleanup_failure_visible_and_not_reusable(diagnosis_request, profile, analysis_data):
    result = run(diagnosis_request, profile, FakeOpenCode(analysis_data, cleanup_ok=False))
    assert result["execution"]["cleanup_status"] == "failed"
    assert result["execution"]["runtime_reusable"] is False


def test_deadline_during_cleanup_marks_runtime_unusable(
    diagnosis_request, profile, analysis_data, monkeypatch
):
    fake = FakeOpenCode(analysis_data)
    original_timeout = asyncio.timeout
    deadline = None

    def capture_deadline(delay):
        nonlocal deadline
        manager = original_timeout(delay)
        if deadline is None:
            deadline = manager
        return manager

    monkeypatch.setattr("ai_error_check_agent.service.asyncio.timeout", capture_deadline)

    async def slow_cleanup(request):
        if request.method == "DELETE":
            # Expire only after cleanup begins; a 20 ms deadline can fire during setup.
            deadline.reschedule(asyncio.get_running_loop().time())
            await asyncio.Future()
        return await fake(request)

    result = run(diagnosis_request, profile, slow_cleanup)
    assert result["job_status"] == "timed_out"
    assert result["analysis"] is None
    assert result["execution"]["cleanup_status"] == "failed"
    assert result["execution"]["runtime_reusable"] is False


def test_profile_mismatch_does_not_call_runtime(diagnosis_request, profile, analysis_data):
    fake = FakeOpenCode(analysis_data)
    profile = profile.model_copy(update={"profile_id": "another-profile"})
    with pytest.raises(DiagnosisError) as error:
        run(diagnosis_request, profile, fake)
    assert error.value.code == "PROFILE_MISMATCH"
    assert fake.calls == []


def test_insufficient_evidence_is_successful_execution(diagnosis_request, profile, analysis_data):
    analysis_data.update(analysis_status="insufficient_evidence", hypotheses=[])
    analysis_data["remediation"] = {
        "status": "needs_more_evidence",
        "reason": "종료 원인을 설명할 로그가 필요합니다.",
        "plans": [],
    }
    analysis_data["next_checks"][0]["hypothesis_ids"] = []
    result = run(diagnosis_request, profile, FakeOpenCode(analysis_data))
    assert result["job_status"] == "succeeded"
    assert result["analysis"]["analysis_status"] == "insufficient_evidence"
