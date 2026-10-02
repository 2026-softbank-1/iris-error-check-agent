"""Real LLM evaluation with synthetic backend JSON and an in-memory S3 HTTP substitute."""

import argparse
import asyncio
import copy
import io
import json
import statistics
import tarfile
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from run_api_source import cases as old_cases

from ai_error_check_agent.api import create_app
from ai_error_check_agent.direct_api import OpenAIResponsesRuntime, load_direct_settings
from ai_error_check_agent.opencode_process import ManagedOpenCode

URL = "https://iris-example.s3.ap-northeast-2.amazonaws.com/snapshots/demo.tar.gz?X-Amz-Signature=FAKE_SIGNATURE_CANARY"


def backend_payload(old):
    request = old["diagnosis"]
    return {
        "success": True,
        "message": "진단용 데이터 조회 완료",
        "data": {
            "projectId": "project-123",
            "serviceId": "service-456",
            "deploymentId": "deploy-789",
            "attemptId": "attempt-1",
            "deploymentStatus": request["context"]["deployment_status"].upper(),
            "failedStage": request["context"]["reported_stage"],
            "exitCode": request["context"]["exit_code"],
            "logRange": {
                "from": "2026-10-02T07:00:00Z",
                "to": "2026-10-02T07:05:00Z",
                "isComplete": True,
            },
            "logs": [
                {
                    "id": f"log-{n}",
                    "timestamp": "2026-10-02T07:01:00Z",
                    "stage": log["stage"],
                    "sourceId": log["source_id"],
                    "stream": log["stream"],
                    "sequence": n,
                    "text": log["text"],
                }
                for n, log in enumerate(request["logs"], 1)
            ],
            "source": {
                "format": "tar.gz",
                "downloadUrl": URL,
                "expiresAt": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                "commitSha": (old.get("source_snapshot") or {}).get("commit_sha"),
                "rootDirectory": ".",
            },
        },
    }


def fixture_archive(old):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for file in (old.get("source_snapshot") or {}).get("files", []):
            raw = file["content"].encode()
            entry = tarfile.TarInfo("owner-repo-abcdef1/" + file["path"])
            entry.size = len(raw)
            archive.addfile(entry, io.BytesIO(raw))
    return buffer.getvalue()


async def evaluate(env_file, output_dir, runtime_factory=None, runtime_name="direct"):
    output_dir.mkdir(parents=True, exist_ok=False)
    profile, key = load_direct_settings(env_file, "profile-demo-a")
    previous = old_cases()
    scenarios = []
    for n in (0, 1, 2, 4):
        name, old, status, source_status, path = previous[n]
        payload = backend_payload(old)
        if n == 1:
            payload["data"]["source"]["commitSha"] = None
        if n == 2:
            payload["data"]["logs"][0]["text"] = (
                "ERROR Missing required configuration: DATABASE_URL"
            )
        scenarios.append((name, payload, fixture_archive(old), status, source_status, path))
    expired = copy.deepcopy(scenarios[0])
    expired[1]["data"]["source"]["expiresAt"] = "2020-01-01T00:00:00Z"
    scenarios.append(("expired-archive", expired[1], expired[2], "diagnosed", "failed", None))
    rows = []
    for name, payload, archive, expected, source_status, path in scenarios:
        downloads = []

        def download(request, received=downloads, body=archive):
            received.append(True)
            return httpx.Response(200, content=body)

        app = create_app(
            runtime_factory or (lambda: OpenAIResponsesRuntime(profile, key)),
            dev=True,
            archive_transport=httpx.MockTransport(download),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            response = await client.post("/diagnose", json=payload)
        result = response.json()
        (output_dir / f"{name}.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        analysis, source = result.get("analysis") or {}, result.get("source_analysis") or {}
        checks = {
            "http": response.status_code == 200,
            "status": analysis.get("analysis_status") == expected,
            "source": source.get("status") == source_status,
            "download_only_when_needed": len(downloads) == (1 if path else 0),
            "location": not path or any(f["path"] == path for f in source.get("findings", [])),
            "remediation": expected != "diagnosed"
            or bool(analysis.get("remediation", {}).get("plans")),
            "log_identity": all(e.get("log_id") for e in result.get("evidence", [])),
            "url_not_disclosed": "FAKE_SIGNATURE_CANARY" not in response.text,
            "cleanup": all(
                s.get("cleanup_status") == "deleted" and s.get("runtime_reusable") is True
                for s in result.get("execution", {}).get("stages", [])
            )
            if runtime_name == "opencode"
            else True,
        }
        row = {
            "case": name,
            "passed": all(checks.values()),
            "checks": checks,
            "downloads": len(downloads),
            "execution": result.get("execution", {}),
        }
        rows.append(row)
        print(
            json.dumps({k: row[k] for k in ("case", "passed", "checks", "downloads")}), flush=True
        )
        if any(
            s.get("cleanup_status")
            in {"remote_completion_unknown", "failed", "session_creation_unconfirmed"}
            or s.get("runtime_reusable") is False
            for s in row["execution"].get("stages", [])
        ):
            break
    durations = [r["execution"]["elapsed_ms"] for r in rows if "elapsed_ms" in r["execution"]]
    summary = {
        "model": profile.model_id,
        "runtime": runtime_name,
        "planned": len(scenarios),
        "completed": len(rows),
        "passed": sum(row["passed"] for row in rows),
        "cases": rows,
        "median_ms": statistics.median(durations) if durations else None,
        "max_ms": max(durations) if durations else None,
        "scope": "Synthetic logs and tar.gz; actual LLM; S3 transport simulated, no live bucket",
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0 if len(rows) == len(scenarios) and all(row["passed"] for row in rows) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--runtime", choices=("direct", "opencode"), default="direct")
    parser.add_argument("--opencode-port", type=int, default=4096)
    args = parser.parse_args()
    profile, key = load_direct_settings(args.env_file, "profile-demo-a")
    manager = (
        ManagedOpenCode(profile, key, root=Path.cwd(), port=args.opencode_port)
        if args.runtime == "opencode"
        else nullcontext()
    )
    with manager as server:
        raise SystemExit(
            asyncio.run(
                evaluate(
                    args.env_file, args.output_dir, server.runtime if server else None, args.runtime
                )
            )
        )
