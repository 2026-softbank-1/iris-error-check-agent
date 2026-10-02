"""OpenCode HTTP adapter. One message submission; no application-level retries."""

import asyncio
import json
import math
import re
from dataclasses import dataclass
from typing import Annotated
from urllib.parse import urlparse

import httpx
from pydantic import Field, model_validator

from .contracts import Identifier, StrictModel
from .errors import DiagnosisError


class ModelProfile(StrictModel):
    profile_id: Identifier
    base_url: str
    provider_id: Annotated[str, Field(min_length=1, max_length=128)]
    model_id: Annotated[str, Field(min_length=1, max_length=256)]
    expected_runtime_version: Annotated[str, Field(min_length=1, max_length=128)]
    agent: str = "iris_diagnosis"
    timeout_seconds: Annotated[float, Field(gt=0, le=120)] = 30.0
    max_evidence_bytes: Annotated[int, Field(ge=1024, le=65_536)] = 16_384
    max_prompt_bytes: Annotated[int, Field(ge=4096, le=131_072)] = 32_768

    @model_validator(mode="after")
    def local_runtime_only(self):
        parsed = urlparse(self.base_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("phase 1 requires a dedicated loopback HTTP runtime")
        if self.agent != "iris_diagnosis":
            raise ValueError("dedicated iris_diagnosis agent required")
        return self


@dataclass(frozen=True)
class ModelResponse:
    text: str
    metadata: dict


class OpenCodeRuntime:
    def __init__(
        self,
        profile: ModelProfile,
        *,
        username: str = "opencode",
        password: str | None = None,
        transport=None,
    ):
        self.profile = profile
        self.client = httpx.AsyncClient(
            base_url=profile.base_url.rstrip("/"),
            auth=(username, password) if password else None,
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(profile.timeout_seconds),
        )
        self.message_submissions = 0
        self.cleanup_status = "not_needed"
        self.runtime_version = None
        self.abort_confirmed = None
        self.reusable = True

    async def close(self):
        await self.client.aclose()

    async def _request(self, method, path, **kwargs):
        try:
            # Bound response memory; error bodies are never exposed to callers.
            async with self.client.stream(method, path, **kwargs) as response:
                if response.status_code in {401, 403}:
                    raise DiagnosisError(
                        "MODEL_AUTH_ERROR", "OpenCode 인증 또는 접근이 거부됐습니다."
                    )
                if response.status_code < 200 or response.status_code >= 300:
                    raise DiagnosisError("RUNTIME_ERROR", "OpenCode 요청이 실패했습니다.")
                payload = bytearray()
                async for chunk in response.aiter_bytes():
                    payload.extend(chunk)
                    if len(payload) > 2 * 1024 * 1024:
                        raise DiagnosisError(
                            "RUNTIME_ERROR", "OpenCode 응답 크기가 한도를 초과했습니다."
                        )
            return json.loads(payload)
        except httpx.TimeoutException:
            raise DiagnosisError("MODEL_TIMEOUT", "모델 실행 제한시간을 초과했습니다.") from None
        except (httpx.HTTPError, ValueError):
            raise DiagnosisError("RUNTIME_ERROR", "OpenCode 응답을 읽을 수 없습니다.") from None

    async def preflight(self):
        health = await self._request("GET", "/global/health")
        if not isinstance(health, dict) or health.get("healthy") is not True:
            raise DiagnosisError("RUNTIME_ERROR", "OpenCode 상태 확인에 실패했습니다.")
        if health.get("version") != self.profile.expected_runtime_version:
            raise DiagnosisError(
                "RUNTIME_VERSION_MISMATCH", "지정한 OpenCode 버전과 실행 버전이 다릅니다."
            )
        self.runtime_version = self.profile.expected_runtime_version
        config = await self._request("GET", "/config")
        if not isinstance(config, dict):
            raise DiagnosisError("UNSAFE_RUNTIME", "실행 설정을 확인할 수 없습니다.")
        agents = config.get("agent", {})
        agent = agents.get(self.profile.agent) if isinstance(agents, dict) else None
        compaction = config.get("compaction")

        def deny_all(permission):
            return permission == "deny" or permission == {"*": "deny"}

        if (
            not isinstance(agent, dict)
            or not deny_all(agent.get("permission"))
            or not deny_all(config.get("permission"))
            or agent.get("mode") != "primary"
            or agent.get("steps") != 1
            or agent.get("disable") is True
            or config.get("plugin")
            or config.get("mcp")
            or config.get("instructions")
            or config.get("share") != "disabled"
            or config.get("snapshot") is not False
            or config.get("autoupdate") is not False
            or not isinstance(compaction, dict)
            or compaction.get("auto") is not False
            or compaction.get("prune") is not False
            or any(
                not isinstance(agents.get(name), dict) or agents[name].get("disable") is not True
                for name in ("title", "summary")
            )
        ):
            raise DiagnosisError(
                "UNSAFE_RUNTIME", "전용 에이전트의 권한·외부 설정 조건을 만족하지 않습니다."
            )
        tool_ids = await self._request("GET", "/experimental/tool/ids")
        if not isinstance(tool_ids, list) or not all(isinstance(item, str) for item in tool_ids):
            raise DiagnosisError("UNSAFE_RUNTIME", "도구 목록을 확인할 수 없습니다.")
        return dict.fromkeys(tool_ids, False)

    async def _cleanup(self, session_id: str, abort: bool):
        self.cleanup_status = "failed"
        try:
            async with asyncio.timeout(3):
                if abort:
                    self.abort_confirmed = (
                        await self._request("POST", f"/session/{session_id}/abort") is True
                    )
                    if not self.abort_confirmed:
                        self.reusable = False
                deleted = await self._request("DELETE", f"/session/{session_id}")
                if deleted is True:
                    self.cleanup_status = "deleted"
                else:
                    self.reusable = False
        except (DiagnosisError, TimeoutError):
            self.reusable = False
        finally:
            # Also runs when the outer deadline interrupts cleanup itself.
            if self.cleanup_status != "deleted":
                self.reusable = False

    async def run(
        self, system_prompt: str, model_input: dict, response_schema: dict
    ) -> ModelResponse:
        if not self.reusable or self.message_submissions:
            raise DiagnosisError(
                "RUNTIME_NOT_REUSABLE", "작업마다 새 실행 어댑터를 사용해야 합니다."
            )
        tools = await self.preflight()
        # No raw tenant IDs or credentials are sent as model context.
        system = (
            system_prompt
            + "\n\n[analysis JSON Schema]\n"
            + json.dumps(response_schema, ensure_ascii=False)
        )
        user_text = json.dumps({"untrusted_log_data": model_input}, ensure_ascii=False)
        if len((system + user_text).encode("utf-8")) > self.profile.max_prompt_bytes:
            raise DiagnosisError(
                "INPUT_TOO_LARGE", "전체 프롬프트가 설정한 바이트 예산을 초과했습니다."
            )
        try:
            session = await self._request("POST", "/session", json={"title": "IRIS log diagnosis"})
        except (DiagnosisError, asyncio.CancelledError):
            self.reusable = False
            self.cleanup_status = "session_creation_unconfirmed"
            raise
        session_id = session.get("id") if isinstance(session, dict) else None
        if not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", session_id):
            self.reusable = False
            raise DiagnosisError("RUNTIME_ERROR", "유효한 세션 ID를 받지 못했습니다.")
        completed = False
        try:
            self.message_submissions += 1
            response = await self._request(
                "POST",
                f"/session/{session_id}/message",
                json={
                    "agent": self.profile.agent,
                    "model": {
                        "providerID": self.profile.provider_id,
                        "modelID": self.profile.model_id,
                    },
                    "system": system,
                    "tools": tools,
                    "parts": [{"type": "text", "text": user_text}],
                },
            )
            result = self._parse_response(response, session_id)
            completed = True
            return result
        finally:
            await self._cleanup(session_id, abort=not completed)

    def _parse_response(self, response, session_id) -> ModelResponse:
        if not isinstance(response, dict) or not isinstance(response.get("info"), dict):
            raise DiagnosisError("RUNTIME_ERROR", "모델 응답 메타데이터가 없습니다.")
        info = response["info"]
        if info.get("sessionID") != session_id or info.get("role") != "assistant":
            raise DiagnosisError("RUNTIME_ERROR", "응답의 세션 또는 역할이 맞지 않습니다.")
        if info.get("error"):
            raise DiagnosisError("MODEL_ERROR", "모델 제공자가 오류를 반환했습니다.")
        if info.get("finish") not in {"stop", "end_turn"}:
            raise DiagnosisError(
                "INCOMPLETE_RESPONSE", "모델 응답이 정상적으로 완료되지 않았습니다."
            )
        if (
            info.get("providerID") != self.profile.provider_id
            or info.get("modelID") != self.profile.model_id
        ):
            raise DiagnosisError("MODEL_MISMATCH", "요청 모델과 응답 메타데이터의 모델이 다릅니다.")
        parts = response.get("parts")
        if not isinstance(parts, list) or not all(isinstance(part, dict) for part in parts):
            raise DiagnosisError("RUNTIME_ERROR", "모델 응답 부분의 형식이 잘못됐습니다.")
        if any(part.get("type") == "tool" for part in parts):
            self.reusable = False
            raise DiagnosisError("UNEXPECTED_TOOL_CALL", "진단 중 업무 도구 호출이 발견됐습니다.")
        text_parts = [part.get("text") for part in parts if part.get("type") == "text"]
        if not text_parts or not all(isinstance(part, str) for part in text_parts):
            raise DiagnosisError("INCOMPLETE_RESPONSE", "모델의 텍스트 결과가 없습니다.")

        def number(value):
            return (
                value
                if type(value) in {int, float} and math.isfinite(value) and value >= 0
                else None
            )

        tokens = info.get("tokens") if isinstance(info.get("tokens"), dict) else {}
        return ModelResponse(
            "".join(text_parts),
            {
                "reported_model": {"provider_id": info["providerID"], "model_id": info["modelID"]},
                "verification": "reported",
                "tokens": {
                    key: number(tokens.get(key)) for key in ("input", "output", "reasoning")
                },
                "cost": number(info.get("cost")),
            },
        )
