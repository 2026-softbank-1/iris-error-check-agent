"""Shared contract for direct API and optional OpenCode execution."""

from typing import Protocol

from .runtime import ModelResponse


class RuntimeProfile(Protocol):
    profile_id: str
    provider_id: str
    model_id: str
    timeout_seconds: float
    max_evidence_bytes: int
    max_prompt_bytes: int


class DiagnosisRuntime(Protocol):
    profile: RuntimeProfile
    message_submissions: int
    runtime_version: str | None
    cleanup_status: str
    abort_confirmed: bool | None
    reusable: bool

    async def run(
        self, system_prompt: str, model_input: dict, response_schema: dict
    ) -> ModelResponse: ...

    async def close(self): ...
