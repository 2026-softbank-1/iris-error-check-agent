import asyncio
import hashlib
import json
import time
from dataclasses import asdict
from datetime import UTC, datetime
from uuid import uuid4

from . import __version__
from .contracts import DiagnosisRequest
from .errors import DiagnosisError
from .preprocessing import prepare
from .runtime_contract import DiagnosisRuntime
from .validation import load_prompt, load_schema, validate_analysis


async def diagnose(request: DiagnosisRequest, runtime: DiagnosisRuntime) -> dict:
    """Run a local diagnosis. Persisting/authorizing jobs belongs to phase 2."""
    started = time.monotonic()
    started_at = datetime.now(UTC).isoformat()
    if request.model_profile_id != runtime.profile.profile_id:
        raise DiagnosisError("PROFILE_MISMATCH", "요청한 모델 프로필과 실행 프로필이 다릅니다.")
    bundle = prepare(request, runtime.profile.max_evidence_bytes)
    prompt, schema = load_prompt(), load_schema()
    return await execute_stage(
        request,
        runtime,
        bundle,
        prompt,
        schema,
        bundle.model_payload(),
        validate_analysis,
        started=started,
        started_at=started_at,
    )


async def execute_stage(
    request,
    runtime,
    bundle,
    prompt,
    schema,
    model_input,
    validator,
    *,
    started=None,
    started_at=None,
):
    """Execute and account for one bounded model call using a stage-specific validator."""
    started = time.monotonic() if started is None else started
    started_at = started_at or datetime.now(UTC).isoformat()
    analysis, error, model_metadata = None, None, {}
    job_status = "failed"
    try:
        async with asyncio.timeout(runtime.profile.timeout_seconds):
            response = await runtime.run(prompt, model_input, schema)
            model_metadata = response.metadata
            analysis = validator(response.text, bundle)
            # JSON validation is synchronous; check the deadline again before accepting it.
            if time.monotonic() - started >= runtime.profile.timeout_seconds:
                raise TimeoutError
            job_status = "succeeded"
    except TimeoutError:
        analysis = None
        job_status = "timed_out"
        error = {"code": "MODEL_TIMEOUT", "message": "진단 제한시간을 초과했습니다."}
    except DiagnosisError as exc:
        analysis = None
        job_status = "timed_out" if exc.code == "MODEL_TIMEOUT" else "failed"
        error = exc.as_dict()

    return {
        "schema_version": "diagnosis-result.v2",
        "diagnosis_id": "diag-" + uuid4().hex,
        "scope": {
            key: getattr(request, key)
            for key in ("tenant_id", "project_id", "deployment_id", "attempt_id")
        },
        "previous_diagnosis_id": request.previous_diagnosis_id,
        "deployment_context": request.context.model_dump(),
        "job_status": job_status,
        "analysis": analysis,
        "remediation_execution": "not_executed",
        "error": error,
        "evidence": [asdict(line) for line in bundle.lines],
        "input_limitations": list(bundle.limitations),
        "execution": {
            "started_at": started_at,
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "model_profile_id": request.model_profile_id,
            "model_settings_version": request.model_settings_version,
            "requested_model": {
                "provider_id": runtime.profile.provider_id,
                "model_id": runtime.profile.model_id,
            },
            "dispatched_model": {
                "provider_id": runtime.profile.provider_id,
                "model_id": runtime.profile.model_id,
            }
            if runtime.message_submissions
            else None,
            "reported_model": None,
            "verification": "dispatch_only" if runtime.message_submissions else "not_dispatched",
            "tokens": None,
            "cost": None,
            **model_metadata,
            "message_submissions": runtime.message_submissions,
            "provider_call_count": getattr(runtime, "provider_call_count", None),
            "runtime_version": runtime.runtime_version,
            "cleanup_status": runtime.cleanup_status,
            "abort_confirmed": runtime.abort_confirmed,
            "runtime_reusable": runtime.reusable,
            "snapshot_sha256": bundle.snapshot_sha256,
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "schema_sha256": hashlib.sha256(
                json.dumps(schema, sort_keys=True).encode("utf-8")
            ).hexdigest(),
            "service_version": __version__,
            "preprocessing_version": "masking.v1/full-input.v1",
        },
    }
