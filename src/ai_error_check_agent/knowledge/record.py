"""Small, masked record assembled without importing graph libraries."""

from ..preprocessing import redact_object


def record_from_result(result, stages=()):
    source = result.get("source_analysis") or {}
    default_stage = "source" if source.get("status") == "analyzed" else "logs"
    return redact_object(
        {
            "diagnosis_id": result["diagnosis_id"],
            "scope": result["scope"],
            "backend_context": result.get("backend_context"),
            "deployment_context": result["deployment_context"],
            "job_status": result["job_status"],
            "evidence": result.get("evidence", []),
            "source": {
                key: source.get(key)
                for key in (
                    "status",
                    "commit_sha",
                    "commit_verification",
                    "archive_sha256",
                    "read_ranges",
                    "evidence",
                    "findings",
                )
            },
            "stages": list(stages)
            or [{"stage": default_stage, "analysis": result.get("analysis")}],
            "stage_history_complete": bool(stages),
            "execution": result.get("execution", {}),
        }
    )
