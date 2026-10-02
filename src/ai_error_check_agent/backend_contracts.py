"""IRIS camelCase envelope and its lossless log-evidence adapter."""

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic.alias_generators import to_camel

from .contracts import DiagnosisRequest, Identifier, Stage, StrictModel
from .errors import DiagnosisError
from .preprocessing import EvidenceBundle, EvidenceLine, normalize, redact
from .source_contracts import DiagnoseAPIRequest, safe_source_path

BackendId = Identifier | Annotated[int, Field(ge=0)]


def aware(value):
    if value is not None and value.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return value


class BackendModel(StrictModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, alias_generator=to_camel)


class LogRange(BackendModel):
    start: datetime | None = Field(alias="from", default=None)
    end: datetime | None = Field(alias="to", default=None)
    is_complete: bool

    _aware = field_validator("start", "end")(aware)

    @model_validator(mode="after")
    def ordered(self):
        if self.start and self.end and self.start > self.end:
            raise ValueError("log range is reversed")
        return self


class BackendLog(BackendModel):
    id: BackendId
    timestamp: datetime | None = None
    stage: Stage | None = None
    source_id: Annotated[str, Field(min_length=1, max_length=200)]
    stream: Literal["stdout", "stderr", "combined", "unknown"] | None = None
    sequence: Annotated[int, Field(ge=0)] | None = None
    text: Annotated[str, Field(max_length=1_048_576)]

    _aware = field_validator("timestamp")(aware)

    @field_validator("stage", "stream", mode="before")
    @classmethod
    def lowercase(cls, value):
        return value.lower() if isinstance(value, str) else value


class SourceArchive(BackendModel):
    format: Literal["tar.gz"]
    download_url: SecretStr
    expires_at: datetime | None = None
    commit_sha: Annotated[str, Field(pattern=r"^[a-fA-F0-9]{40}$")] | None = None
    root_directory: Annotated[str, Field(max_length=200)] | None = "."

    _aware = field_validator("expires_at")(aware)

    @field_validator("download_url")
    @classmethod
    def bounded_url(cls, value):
        if not 1 <= len(value.get_secret_value()) <= 8192:
            raise ValueError("download URL size is invalid")
        return value

    @field_validator("root_directory")
    @classmethod
    def relative_root(cls, value):
        return "." if value in {None, "", "."} else safe_source_path(value)


class BackendData(BackendModel):
    project_id: BackendId
    service_id: BackendId
    deployment_id: BackendId
    attempt_id: BackendId
    deployment_status: (
        Literal[
            "FAILED",
            "CRASHED",
            "SUCCEEDED",
            "RUNNING",
            "QUEUED",
            "INITIALIZING",
            "BUILDING",
            "DEPLOYING",
            "ROLLED_BACK",
            "MANUAL_INTERVENTION",
            "SUPERSEDED",
            "UNKNOWN",
        ]
        | None
    )
    failed_stage: Stage | None
    exit_code: int | None = None
    log_range: LogRange
    logs: Annotated[list[BackendLog], Field(min_length=1, max_length=2000)]
    source: SourceArchive | None = None

    @field_validator("failed_stage", mode="before")
    @classmethod
    def lowercase(cls, value):
        return value.lower() if isinstance(value, str) else value

    @field_validator("deployment_status", mode="before")
    @classmethod
    def uppercase(cls, value):
        return value.upper() if isinstance(value, str) else value

    @model_validator(mode="after")
    def consistent_logs(self):
        if len({str(log.id) for log in self.logs}) != len(self.logs):
            raise ValueError("duplicate log id")
        sequences = [
            (log.source_id, log.stage or "unknown", log.stream or "unknown", log.sequence)
            for log in self.logs
            if log.sequence is not None
        ]
        if len(set(sequences)) != len(sequences):
            raise ValueError("duplicate sequence within a log stream")
        for log in self.logs:
            if log.timestamp and (
                (self.log_range.start and log.timestamp < self.log_range.start)
                or (self.log_range.end and log.timestamp > self.log_range.end)
            ):
                raise ValueError("log timestamp is outside logRange")
        if sum(len(log.text.encode("utf-8")) for log in self.logs) > 1_048_576:
            raise ValueError("log text exceeds 1 MiB")
        return self


class BackendEnvelope(BackendModel):
    success: bool
    message: Annotated[str, Field(max_length=2000)] | None = None
    data: BackendData | None

    @model_validator(mode="after")
    def successful_data(self):
        if self.success and self.data is None:
            raise ValueError("successful envelope requires data")
        return self


@dataclass(frozen=True)
class BackendEvidenceLine(EvidenceLine):
    log_id: str
    timestamp: str | None
    sequence: int | None
    event_line: int


def adapt_backend(data, *, profile_id, settings_version="backend.v1", tenant_id="iris"):
    status = {
        "FAILED": "failed",
        "CRASHED": "failed",
        "SUCCEEDED": "succeeded",
        "RUNNING": "running",
        "QUEUED": "running",
        "INITIALIZING": "running",
        "BUILDING": "running",
        "DEPLOYING": "running",
    }.get(data.deployment_status, "unknown")
    # This object carries the legacy diagnosis context. The evidence builder below preserves
    # every event's identity and timeline instead of flattening these logs for the model.
    request = DiagnosisRequest.model_validate_json(
        json.dumps(
            {
                "schema_version": "diagnosis-request.v1",
                "tenant_id": tenant_id,
                "project_id": str(data.project_id),
                "deployment_id": str(data.deployment_id),
                "attempt_id": str(data.attempt_id),
                "trigger": "user_requested",
                "context": {
                    "reported_stage": data.failed_stage or "unknown",
                    "deployment_status": status,
                    "exit_code": data.exit_code,
                },
                "logs": [
                    {
                        "chunk_id": "backend-input",
                        "source_id": "backend-input",
                        "stage": "unknown",
                        "stream": "unknown",
                        "source_line_start": None,
                        "captured_at": None,
                        "is_complete": data.log_range.is_complete,
                        "text": "\n".join(log.text for log in data.logs),
                    }
                ],
                "model_profile_id": profile_id,
                "model_settings_version": settings_version,
            }
        )
    )
    return DiagnoseAPIRequest(diagnosis=request)


def backend_context(data):
    return {
        "project_id": str(data.project_id),
        "service_id": str(data.service_id),
        "deployment_id": str(data.deployment_id),
        "attempt_id": str(data.attempt_id),
        "deployment_status": data.deployment_status,
        "failed_stage": data.failed_stage,
        "log_range": data.log_range.model_dump(mode="json", by_alias=True),
        "tenant_scope": "server_configured; not user authorization",
    }


def prepare_backend(data, request, max_evidence_bytes):
    groups = defaultdict(list)
    for log in data.logs:
        groups[(log.source_id, log.stage or "unknown", log.stream or "unknown")].append(log)
    lines, limits = [], []
    if not data.log_range.is_complete:
        limits.append("백엔드가 조회 시간 범위 내 로그의 누락 또는 잘림을 표시했습니다.")
    for group_index, ((source_id, stage, stream), events) in enumerate(groups.items(), 1):
        if all(e.sequence is not None for e in events):
            events.sort(key=lambda e: e.sequence)
        elif all(e.timestamp is not None for e in events):
            events.sort(key=lambda e: e.timestamp)
            limits.append("일부 순서 번호가 없어 같은 출처의 로그를 시간순으로 정렬했습니다.")
        else:
            limits.append("순서·시간 정보가 부족한 출처는 전달된 목록 순서를 유지했습니다.")
        raw, positions = [], []
        for event in events:
            physical = normalize(event.text).split("\n")
            if physical[-1] == "":
                physical.pop()
            for number, text in enumerate(physical, 1):
                raw.append(text)
                positions.append((event, number))
        if not raw:
            continue
        original = "\n".join(raw)
        masked = redact(original)
        if original != masked:
            limits.append("비밀값을 마스킹했으므로 일부 내용을 확인할 수 없습니다.")
        for number, (text, (event, event_line)) in enumerate(
            zip(masked.split("\n"), positions, strict=True), 1
        ):
            lines.append(
                BackendEvidenceLine(
                    id=f"EV{len(lines) + 1:06d}",
                    chunk_id=f"backend-{group_index:04d}",
                    source_id=redact(normalize(source_id)),
                    stage=stage,
                    stream=stream,
                    chunk_line=number,
                    source_line=None,
                    text=text,
                    log_id=str(event.id),
                    timestamp=event.timestamp.astimezone(UTC).isoformat()
                    if event.timestamp
                    else None,
                    sequence=event.sequence,
                    event_line=event_line,
                )
            )
    if not any(line.text.strip() for line in lines):
        raise DiagnosisError("EMPTY_LOGS", "분석할 로그 원문이 없습니다.")
    if len(lines) > 10_000:
        raise DiagnosisError("INPUT_TOO_LARGE", "로그가 10,000줄을 초과했습니다.")
    bundle = EvidenceBundle(
        context=request.context.model_dump(),
        lines=tuple(lines),
        limitations=tuple(dict.fromkeys(limits)),
        snapshot_sha256="",
    )
    encoded = json.dumps(bundle.model_payload(), ensure_ascii=False, sort_keys=True).encode("utf-8")
    if len(encoded) > max_evidence_bytes:
        raise DiagnosisError(
            "INPUT_TOO_LARGE", "로그와 시간·출처 정보가 진단 입력 예산을 초과했습니다."
        )
    return replace(bundle, snapshot_sha256=hashlib.sha256(encoded).hexdigest())
