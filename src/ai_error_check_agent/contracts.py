from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")]
Stage = Literal["build", "release", "deploy", "runtime", "unknown"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class DeploymentContext(StrictModel):
    reported_stage: Stage
    deployment_status: Literal["failed", "succeeded", "running", "unknown"]
    exit_code: int | None


class LogChunk(StrictModel):
    chunk_id: Identifier
    source_id: Identifier
    stage: Stage
    stream: Literal["stdout", "stderr", "combined", "unknown"]
    source_line_start: Annotated[int, Field(ge=1)] | None
    captured_at: datetime | None
    is_complete: bool
    text: str

    @model_validator(mode="after")
    def timezone_required(self):
        if self.captured_at is not None and self.captured_at.tzinfo is None:
            raise ValueError("captured_at requires a timezone")
        return self


class DiagnosisRequest(StrictModel):
    schema_version: Literal["diagnosis-request.v1"]
    tenant_id: Identifier
    project_id: Identifier
    deployment_id: Identifier
    attempt_id: Identifier
    trigger: Literal["deployment_failed", "user_requested"]
    context: DeploymentContext
    logs: Annotated[list[LogChunk], Field(min_length=1, max_length=20)]
    model_profile_id: Identifier
    model_settings_version: Identifier
    previous_diagnosis_id: Identifier | None = None

    @model_validator(mode="after")
    def input_limits(self):
        if len({chunk.chunk_id for chunk in self.logs}) != len(self.logs):
            raise ValueError("chunk_id must be unique")
        if not any(chunk.text.strip() for chunk in self.logs):
            raise ValueError("logs must contain non-whitespace text")
        if sum(len(chunk.text.encode("utf-8")) for chunk in self.logs) > 1024 * 1024:
            raise ValueError("log text exceeds 1 MiB")
        if sum(len(chunk.text.splitlines()) for chunk in self.logs) > 10_000:
            raise ValueError("logs exceed 10,000 physical lines")
        return self
