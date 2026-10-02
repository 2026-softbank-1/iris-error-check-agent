"""Sequential development evaluation. Expected labels never enter model inputs."""

import argparse
import asyncio
import json
import math
import sys
from pathlib import Path

from pydantic import ValidationError

from .cli import configure_console, read_text, run_live, write_result
from .contracts import DiagnosisRequest
from .errors import DiagnosisError
from .runtime import ModelProfile
from .validation import strict_json


async def evaluate(
    manifest_path: Path,
    profile: ModelProfile | None,
    repeats: int,
    *,
    env_file=Path(".env"),
) -> dict:
    manifest = strict_json(read_text(manifest_path))
    if (
        not isinstance(manifest, dict)
        or not isinstance(manifest.get("cases"), list)
        or not manifest["cases"]
    ):
        raise DiagnosisError("INVALID_EVALUATION", "평가 사례 목록이 없습니다.")
    cases = []
    for case in manifest["cases"]:
        if (
            not isinstance(case, dict)
            or not isinstance(case.get("id"), str)
            or not isinstance(case.get("request"), str)
            or case.get("expected_status")
            not in {"diagnosed", "insufficient_evidence", "no_failure_evidence"}
        ):
            raise DiagnosisError("INVALID_EVALUATION", "평가 사례 규격을 확인하세요.")
        request = DiagnosisRequest.model_validate_json(
            read_text(manifest_path.parent / case["request"])
        )
        cases.append((case, request))
    runs = []
    stopped = False
    for case, request in cases:
        for repetition in range(1, repeats + 1):
            # Only the request object goes to the live runtime, never the case/labels.
            if profile is None:
                result = await run_live(request, env_file=env_file)
            else:
                result = await run_live(request, profile)
            predicted = result["analysis"]["analysis_status"] if result["analysis"] else None
            runs.append(
                {
                    "case_id": case["id"],
                    "repetition": repetition,
                    "expected_status": case["expected_status"],
                    "predicted_status": predicted,
                    "status_correct": predicted == case["expected_status"],
                    "result": result,
                }
            )
            if not result["execution"]["runtime_reusable"]:
                stopped = True
                break
        if stopped:
            break
    times = sorted(run["result"]["execution"]["elapsed_ms"] for run in runs)
    return {
        "dataset_kind": manifest.get("dataset_kind", "unspecified"),
        "planned_runs": len(cases) * repeats,
        "completed_runs": len(runs),
        "stopped_for_runtime_cleanup": stopped,
        "metrics": {
            "status_accuracy_on_completed_runs": sum(run["status_correct"] for run in runs)
            / len(runs),
            "execution_errors": sum(run["result"]["job_status"] != "succeeded" for run in runs),
            "p95_elapsed_ms_nearest_rank": times[math.ceil(len(times) * 0.95) - 1],
        },
        "limitations": [
            "개발 사례의 상태 일치율입니다. 실제 장애의 원인 정확도를 뜻하지 않습니다.",
            "주장과 근거의 의미적 일치·다음 확인의 적합성은 사람이 별도로 평가해야 합니다.",
        ],
        "runs": runs,
    }


def main(argv=None) -> int:
    configure_console()
    parser = argparse.ArgumentParser(description="AI_Error_Check_Agent development evaluation")
    parser.add_argument("--manifest", type=Path, required=True)
    options = parser.add_mutually_exclusive_group()
    options.add_argument("--profile", type=Path)
    options.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--repeats", type=int, choices=range(1, 11), default=3)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.output.exists():
            raise DiagnosisError(
                "OUTPUT_EXISTS", "출력 파일이 이미 있습니다. 새 경로를 지정하세요."
            )
        profile = (
            ModelProfile.model_validate_json(read_text(args.profile, 16_384))
            if args.profile
            else None
        )
        report = asyncio.run(evaluate(args.manifest, profile, args.repeats, env_file=args.env_file))
        write_result(report, args.output)
        return (
            1
            if report["stopped_for_runtime_cleanup"]
            or report["metrics"]["execution_errors"]
            or report["metrics"]["status_accuracy_on_completed_runs"] < 1
            else 0
        )
    except DiagnosisError as exc:
        error = exc.as_dict()
    except (ValidationError, ValueError, RecursionError, OSError):
        error = {
            "code": "INVALID_EVALUATION",
            "message": "평가 입력·프로필·출력 경로를 확인하세요.",
        }
    print(json.dumps({"error": error}, ensure_ascii=False), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
