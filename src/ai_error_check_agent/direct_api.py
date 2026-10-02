"""Single-request OpenAI Responses adapter; no OpenCode process required."""

import asyncio
import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Literal

import httpx
from dotenv import dotenv_values
from pydantic import Field, SecretStr, ValidationError, model_validator

from .contracts import Identifier, StrictModel
from .errors import DiagnosisError
from .runtime import ModelResponse


class DirectModelProfile(StrictModel):
    profile_id: Identifier
    provider_id: Literal["openai"] = "openai"
    model_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]
    base_url: str = "https://api.openai.com/v1"
    timeout_seconds: Annotated[float, Field(gt=0, le=300)] = 60.0
    max_output_tokens: Annotated[int, Field(ge=1024, le=16_384)] = 4096
    reasoning_effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"
    max_evidence_bytes: int = 16_384
    max_prompt_bytes: int = 32_768

    @model_validator(mode="after")
    def official_endpoint(self):
        # Never send an OpenAI credential to a configured third-party host.
        if self.base_url.rstrip("/") != "https://api.openai.com/v1":
            raise ValueError("only the official OpenAI API endpoint is supported")
        return self


def load_direct_settings(
    env_file: Path, profile_id: str, environ: Mapping[str, str] | None = None
) -> tuple[DirectModelProfile, SecretStr]:
    """Read the explicit .env; process variables take precedence. Never log values."""
    env = os.environ if environ is None else environ
    values = {}
    if env_file.exists():
        if env_file.stat().st_size > 65_536:
            raise DiagnosisError("INVALID_CONFIG", ".env 파일 크기가 설정 한도를 초과했습니다.")
        values = dotenv_values(env_file, encoding="utf-8-sig", interpolate=False)

    def value(name, default=""):
        return str(env.get(name, values.get(name) or default)).strip()

    api_key = value("LLM_API_KEY")
    model = value("LLM_MODEL")
    if not api_key or not model:
        raise DiagnosisError("MISSING_CONFIG", ".env의 LLM_API_KEY와 LLM_MODEL을 입력하세요.")
    if not api_key.isascii() or any(character.isspace() for character in api_key):
        raise DiagnosisError("INVALID_CONFIG", "LLM_API_KEY에 공백 또는 잘못된 문자가 있습니다.")
    try:
        profile = DirectModelProfile(
            profile_id=profile_id,
            provider_id=value("LLM_PROVIDER", "openai").lower(),
            model_id=model,
            base_url=value("LLM_BASE_URL", "https://api.openai.com/v1"),
            timeout_seconds=float(value("LLM_TIMEOUT_SECONDS", "60")),
            max_output_tokens=int(value("LLM_MAX_OUTPUT_TOKENS", "4096")),
            reasoning_effort=value("LLM_REASONING_EFFORT", "low"),
        )
    except (ValidationError, ValueError):
        raise DiagnosisError(
            "INVALID_CONFIG", ".env의 제공자·모델·API 주소·시간·출력 제한 설정을 확인하세요."
        ) from None
    return profile, SecretStr(api_key)


def generation_schema(schema):
    """Conservative API subset; the full original schema still validates the result."""
    if isinstance(schema, dict):
        return {
            key: generation_schema(value)
            for key, value in schema.items()
            if key not in {"$schema", "minLength", "maxLength", "uniqueItems"}
        }
    if isinstance(schema, list):
        return [generation_schema(value) for value in schema]
    return schema


class OpenAIResponsesRuntime:
    def __init__(self, profile: DirectModelProfile, api_key: SecretStr, *, transport=None):
        self.profile = profile
        self.client = httpx.AsyncClient(
            base_url=profile.base_url.rstrip("/") + "/",
            headers={"Authorization": "Bearer " + api_key.get_secret_value()},
            timeout=httpx.Timeout(profile.timeout_seconds),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )
        self.message_submissions = 0
        self.provider_call_count = None
        self.runtime_version = "openai-responses.v1"
        self.cleanup_status = "not_needed"
        self.abort_confirmed = None
        self.reusable = True

    async def close(self):
        await self.client.aclose()

    async def run(
        self, system_prompt: str, model_input: dict, response_schema: dict
    ) -> ModelResponse:
        if self.message_submissions:
            raise DiagnosisError(
                "RUNTIME_NOT_REUSABLE", "진단마다 새 실행 어댑터를 사용해야 합니다."
            )
        instructions = (
            system_prompt
            + "\n\n[Full validation schema]\n"
            + json.dumps(response_schema, ensure_ascii=False)
        )
        user_text = json.dumps({"untrusted_log_data": model_input}, ensure_ascii=False)
        if len((instructions + user_text).encode("utf-8")) > self.profile.max_prompt_bytes:
            raise DiagnosisError("INPUT_TOO_LARGE", "전체 프롬프트가 입력 예산을 초과했습니다.")
        payload = {
            "model": self.profile.model_id,
            "instructions": instructions,
            "input": [{"role": "user", "content": user_text}],
            "reasoning": {"effort": self.profile.reasoning_effort},
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "iris_diagnosis",
                    "strict": True,
                    "schema": generation_schema(response_schema),
                }
            },
            "tools": [],
            "store": False,
            "max_output_tokens": self.profile.max_output_tokens,
        }
        self.message_submissions = 1
        try:
            async with self.client.stream("POST", "responses", json=payload) as response:
                if response.status_code in {401, 403}:
                    raise DiagnosisError(
                        "MODEL_AUTH_ERROR", "OpenAI API 키 또는 모델 접근 권한을 확인하세요."
                    )
                if response.status_code == 429:
                    raise DiagnosisError(
                        "MODEL_RATE_LIMIT", "OpenAI API 사용 한도·결제 잔액·요청 제한을 확인하세요."
                    )
                if response.status_code == 404:
                    raise DiagnosisError(
                        "MODEL_NOT_FOUND", "모델 ID와 해당 모델의 API 접근 권한을 확인하세요."
                    )
                if response.status_code == 400:
                    raise DiagnosisError(
                        "MODEL_REQUEST_ERROR",
                        "OpenAI가 요청 규격을 거절했습니다. 모델·스키마 설정을 확인하세요.",
                    )
                if response.status_code != 200:
                    raise DiagnosisError("MODEL_ERROR", "OpenAI API 요청이 실패했습니다.")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 2 * 1024 * 1024:
                        raise DiagnosisError("MODEL_ERROR", "모델 응답 크기가 한도를 초과했습니다.")
            data = json.loads(body)
            self.provider_call_count = 1
            return self._parse(data)
        except (asyncio.CancelledError, httpx.TimeoutException) as exc:
            # Closing the HTTP connection does not confirm cancellation at the provider.
            self.abort_confirmed = False
            self.cleanup_status = "remote_completion_unknown"
            self.reusable = False
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise DiagnosisError("MODEL_TIMEOUT", "OpenAI 응답 제한시간을 초과했습니다.") from None
        except httpx.HTTPError:
            raise DiagnosisError(
                "MODEL_CONNECTION_ERROR", "OpenAI API 연결에 실패했습니다."
            ) from None
        except (ValueError, RecursionError):
            raise DiagnosisError("MODEL_ERROR", "OpenAI 응답 JSON을 읽을 수 없습니다.") from None

    def _parse(self, data) -> ModelResponse:
        if not isinstance(data, dict) or data.get("status") != "completed" or data.get("error"):
            raise DiagnosisError(
                "INCOMPLETE_RESPONSE",
                "모델이 응답을 완료하지 못했습니다. 출력 한도와 실행 상태를 확인하세요.",
            )
        reported = data.get("model")
        model_pattern = re.escape(self.profile.model_id) + r"(?:-\d{4}-\d{2}-\d{2})?"
        if not isinstance(reported, str) or not re.fullmatch(model_pattern, reported):
            raise DiagnosisError("MODEL_MISMATCH", "요청 모델과 응답 모델이 다릅니다.")
        output = data.get("output")
        if not isinstance(output, list):
            raise DiagnosisError("INCOMPLETE_RESPONSE", "모델 출력 목록이 없습니다.")
        texts = []
        for item in output:
            if not isinstance(item, dict):
                raise DiagnosisError("MODEL_ERROR", "모델 출력 형식이 잘못됐습니다.")
            if item.get("type") == "reasoning":
                continue
            if item.get("type") != "message":
                raise DiagnosisError(
                    "UNEXPECTED_TOOL_CALL", "진단에서 허용하지 않은 출력이 반환됐습니다."
                )
            content = item.get("content")
            if (
                item.get("role") != "assistant"
                or item.get("status") != "completed"
                or not isinstance(content, list)
            ):
                raise DiagnosisError("INCOMPLETE_RESPONSE", "완료된 진단 메시지가 없습니다.")
            for part in content:
                if not isinstance(part, dict):
                    raise DiagnosisError("MODEL_ERROR", "모델 출력 형식이 잘못됐습니다.")
                if part.get("type") == "refusal":
                    raise DiagnosisError("MODEL_REFUSAL", "모델이 이 입력의 진단을 거절했습니다.")
                if part.get("type") != "output_text" or not isinstance(part.get("text"), str):
                    raise DiagnosisError("MODEL_ERROR", "지원하지 않는 모델 출력 형식입니다.")
                texts.append(part["text"])
        if len(texts) != 1 or not texts[0].strip():
            raise DiagnosisError("INCOMPLETE_RESPONSE", "단일 진단 JSON 응답을 받지 못했습니다.")
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        details = usage.get("output_tokens_details")
        details = details if isinstance(details, dict) else {}

        def count(value):
            return value if type(value) is int and value >= 0 else None

        return ModelResponse(
            texts[0],
            {
                "reported_model": {"provider_id": "openai", "model_id": reported},
                "verification": "reported",
                "tokens": {
                    "input": count(usage.get("input_tokens")),
                    "output": count(usage.get("output_tokens")),
                    "reasoning": count(details.get("reasoning_tokens")),
                },
                "cost": None,
            },
        )
