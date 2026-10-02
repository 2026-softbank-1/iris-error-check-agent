"""Five synthetic API scenarios, including real LLM calls. No production source is used."""

import argparse
import asyncio
import copy
import json
import statistics
from pathlib import Path

import httpx

from ai_error_check_agent.api import create_app
from ai_error_check_agent.direct_api import OpenAIResponsesRuntime, load_direct_settings

ROOT = Path(__file__).resolve().parents[1]


def cases():
    configuration = json.loads((ROOT / "examples/configuration.request.json").read_text("utf-8"))
    compile_request = copy.deepcopy(configuration)
    compile_request["context"].update(reported_stage="build", exit_code=2)
    compile_request["logs"][0].update(
        stage="build",
        source_line_start=1,
        text=(
            "> tsc --noEmit\n"
            "src/server.ts(2,7): error TS2322: Type 'string' is not assignable to type 'number'.\n"
            "ERROR npm run build exited with code 2"
        ),
    )
    compile_payload = {
        "diagnosis": compile_request,
        "source_snapshot": {
            "commit_sha": "a" * 40,
            "files": [
                {
                    "path": "src/server.ts",
                    "content": (
                        "import { createServer } from 'node:http';\n"
                        "const port: number = process.env.PORT ?? '3000';\n"
                        "const server = createServer((_req, res) => res.end('ok'));\n"
                        "server.listen(port, '0.0.0.0');\n"
                    ),
                },
                {"path": "src/unrelated.ts", "content": "// UNRELATED_SECRET_CANARY\n"},
            ],
        },
    }
    runtime_request = copy.deepcopy(configuration)
    runtime_request["logs"][0]["text"] = (
        'Traceback (most recent call last):\n  File "src/config.py", line 4, in database_url\n'
        "    return os.environ[\"DATABASE_URl\"]\nKeyError: 'DATABASE_URl'\n"
        "ERROR Application startup failed"
    )
    runtime_payload = {
        "diagnosis": runtime_request,
        "source_snapshot": {
            "commit_sha": "b" * 40,
            "files": [
                {
                    "path": "src/config.py",
                    "content": (
                        "# Deployment contract: DATABASE_URL provides the database connection string.\n"
                        'import os\ndef database_url():\n    return os.environ["DATABASE_URl"]\n'
                        "database_url()\n"
                    ),
                }
            ],
        },
    }
    recovered = copy.deepcopy(configuration)
    recovered["context"].update(deployment_status="succeeded", exit_code=0)
    recovered["logs"][0]["text"] = (
        "WARN Connection refused on initial database attempt\n"
        "INFO Retry succeeded; database connection established\n"
        "INFO Application ready; health check passed; deployment completed successfully"
    )
    return [
        ("compile-source", compile_payload, "diagnosed", "analyzed", "src/server.ts"),
        ("runtime-source", runtime_payload, "diagnosed", "analyzed", "src/config.py"),
        ("configuration-log-only", {"diagnosis": configuration}, "diagnosed", "not_needed", None),
        ("missing-source", {"diagnosis": compile_request}, "diagnosed", "unavailable", None),
        (
            "recovered-skip-source",
            {"diagnosis": recovered, "source_snapshot": compile_payload["source_snapshot"]},
            "no_failure_evidence",
            "not_needed",
            None,
        ),
    ]


async def evaluate(env_file, output_dir):
    output_dir.mkdir(parents=True, exist_ok=False)
    profile, key = load_direct_settings(env_file, "profile-demo-a")
    app = create_app(lambda: OpenAIResponsesRuntime(profile, key), dev=True)
    rows = []
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test", timeout=150
    ) as client:
        for name, payload, expected, source_status, path in cases():
            response = await client.post("/diagnose", json=payload)
            result = response.json()
            (output_dir / f"{name}.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            source = result.get("source_analysis", {})
            analysis = result.get("analysis") or {}
            plans = analysis.get("remediation", {}).get("plans", [])
            checks = {
                "http_success": response.status_code == 200,
                "analysis_status": analysis.get("analysis_status") == expected,
                "source_status": source.get("status") == source_status,
                "code_location": not path
                or any(f["path"] == path for f in source.get("findings", [])),
                "detailed_remediation": expected != "diagnosed"
                or bool(plans)
                and all(p["changes"] and p["verification"] and p["rollback"] for p in plans),
                "source_target": not path
                or any(
                    c["target"] == path and c["target_known"] for p in plans for c in p["changes"]
                ),
                "no_execution": result.get("remediation_execution") == "not_executed",
                "unrelated_not_returned": "UNRELATED_SECRET_CANARY" not in response.text,
            }
            row = {
                "case": name,
                "passed": all(checks.values()),
                "checks": checks,
                "http_status": response.status_code,
                "elapsed_ms": result.get("execution", {}).get("elapsed_ms"),
                "execution": result.get("execution", {}),
            }
            rows.append(row)
            print(
                json.dumps({k: row[k] for k in ("case", "passed", "checks", "elapsed_ms")}),
                flush=True,
            )
            if any(
                s.get("cleanup_status") == "remote_completion_unknown"
                for s in row["execution"].get("stages", [])
            ):
                break
    durations = [r["elapsed_ms"] for r in rows if r["elapsed_ms"] is not None]
    summary = {
        "dataset": "synthetic development scenarios, not production accuracy",
        "model": profile.model_id,
        "planned": len(cases()),
        "completed": len(rows),
        "passed": sum(r["passed"] for r in rows),
        "median_ms": statistics.median(durations),
        "max_ms": max(durations),
        "cases": rows,
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0 if len(rows) == len(cases()) and all(r["passed"] for r in rows) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(evaluate(args.env_file, args.output_dir)))
