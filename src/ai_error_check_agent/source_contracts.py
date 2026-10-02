"""Backend-supplied snapshots; paths are identifiers, never filesystem instructions."""

import re
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from .contracts import DiagnosisRequest, StrictModel
from .preprocessing import normalize, redact

Text = Annotated[str, Field(min_length=1, max_length=600)]
LogRefs = Annotated[
    list[Annotated[str, Field(pattern=r"^EV[0-9]{6}$")]], Field(min_length=1, max_length=8)
]


def safe_source_path(path: str) -> str:
    parts = path.split("/")
    if (
        len(path) > 200
        or not path
        or any(p in {"", ".", ".."} for p in parts)
        or re.search(r'[\\:\x00-\x20<>"|?*\x7f]', path)
        or normalize(path) != path
        or redact(path) != path
    ):
        raise ValueError("invalid relative source path")
    forbidden = {".git", ".ssh", ".aws", "id_rsa", "id_ed25519", ".npmrc", ".pypirc"}
    for part in parts:
        lower = part.lower()
        if (
            lower in forbidden
            or lower.endswith((".pem", ".key", ".p12", ".pfx"))
            or (lower.startswith(".env") and lower != ".env.example")
        ):
            raise ValueError("secret file is not an allowed source")
    return path


class SourceFile(StrictModel):
    path: Annotated[str, Field(min_length=1, max_length=200)]
    content: Annotated[str, Field(max_length=65_536)]

    _path = field_validator("path")(safe_source_path)

    @field_validator("content")
    @classmethod
    def bounded_text(cls, value):
        if "\x00" in value or len(value.encode("utf-8")) > 65_536:
            raise ValueError("source must be bounded UTF-8 text")
        return value


class SourceSnapshot(StrictModel):
    commit_sha: Annotated[str, Field(pattern=r"^[0-9a-fA-F]{40}$")]
    files: Annotated[list[SourceFile], Field(min_length=1, max_length=20)]

    @model_validator(mode="after")
    def bounded_unique(self):
        if len({f.path for f in self.files}) != len(self.files):
            raise ValueError("duplicate source path")
        if sum(len(f.content.encode("utf-8")) for f in self.files) > 262_144:
            raise ValueError("snapshot exceeds 256 KiB")
        return self


class DiagnoseAPIRequest(StrictModel):
    diagnosis: DiagnosisRequest
    source_snapshot: SourceSnapshot | None = None


class SourceRange(StrictModel):
    path: Annotated[str, Field(min_length=1, max_length=200)]
    start_line: Annotated[int, Field(ge=1)]
    end_line: Annotated[int, Field(ge=1)]
    reason: Text
    evidence_ids: LogRefs

    _path = field_validator("path")(safe_source_path)

    @model_validator(mode="after")
    def bounded_range(self):
        if not 0 <= self.end_line - self.start_line < 120:
            raise ValueError("range must contain 1 to 120 lines")
        return self


class SourceRequest(StrictModel):
    needed: bool
    reason: Text
    files: Annotated[list[SourceRange], Field(max_length=3)]

    @model_validator(mode="after")
    def consistent(self):
        if not self.needed and self.files:
            raise ValueError("unneeded source cannot have requested files")
        if len({f.path for f in self.files}) != len(self.files):
            raise ValueError("request at most one range per file")
        return self


class CodeFinding(StrictModel):
    path: Annotated[str, Field(min_length=1, max_length=200)]
    start_line: Annotated[int, Field(ge=1)]
    end_line: Annotated[int, Field(ge=1)]
    explanation: Text
    evidence_ids: LogRefs
    source_evidence_ids: Annotated[
        list[Annotated[str, Field(pattern=r"^SC[0-9]{6}$")]], Field(min_length=1, max_length=12)
    ]
    hypothesis_ids: Annotated[
        list[Annotated[str, Field(pattern=r"^H[1-3]$")]], Field(min_length=1, max_length=3)
    ]


class SourceFindings(StrictModel):
    findings: Annotated[list[CodeFinding], Field(max_length=5)]


class SourceAnalysisResult(StrictModel):
    status: Literal["not_needed", "unavailable", "analyzed", "failed"]
    reason: str
    commit_sha: str | None
    commit_verification: Literal["caller_supplied", "not_supplied"]
    requested_files: list[dict]
    read_ranges: list[dict]
    evidence: list[dict]
    findings: list[CodeFinding]
    limitations: list[str]
    error: dict | None


class DiagnoseAPIResult(StrictModel):
    schema_version: Literal["diagnosis-result.v3"]
    diagnosis_id: str
    scope: dict
    previous_diagnosis_id: str | None
    deployment_context: dict
    job_status: Literal["succeeded", "failed", "timed_out"]
    analysis: dict | None
    remediation_execution: Literal["not_executed"]
    error: dict | None
    evidence: list[dict]
    input_limitations: list[str]
    execution: dict
    source_analysis: SourceAnalysisResult
