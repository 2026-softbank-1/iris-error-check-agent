"""Evaluate real LLM diagnoses against frozen synthetic labels; never use a model judge."""

import argparse
import asyncio
import copy
import hashlib
import io
import json
import math
import re
import statistics
import tarfile
import time
from collections import Counter
from pathlib import Path

import httpx

from ai_error_check_agent.api import create_app
from ai_error_check_agent.backend_contracts import BackendEnvelope, adapt_backend, prepare_backend
from ai_error_check_agent.direct_api import OpenAIResponsesRuntime, load_direct_settings
from ai_error_check_agent.knowledge.shadow import KnowledgeSettings
from ai_error_check_agent.source_archive import ArchivePolicy, read_archive

ROOT = Path(__file__).resolve().parents[1]
STOP_CODES = {
    "MODEL_AUTH_ERROR",
    "MODEL_RATE_LIMIT",
    "MODEL_NOT_FOUND",
    "MODEL_REQUEST_ERROR",
    "MODEL_CONNECTION_ERROR",
    "MODEL_TIMEOUT",
}


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_dataset(path):
    raw = path.read_bytes()
    manifest = json.loads(raw)
    if manifest.get("version") != "synthetic-incidents.v1":
        raise ValueError("Unknown synthetic dataset version")
    cases = manifest["cases"]
    if not cases or len({c["id"] for c in cases}) != len(cases):
        raise ValueError("Dataset must have unique cases")
    for case in cases:
        envelope = BackendEnvelope.model_validate_json(json.dumps(case["payload"]))
        gold = case["gold"]
        if gold["status"] not in {"diagnosed", "no_failure_evidence", "insufficient_evidence"}:
            raise ValueError("Unknown gold status")
        if gold["status"] == "diagnosed" and not all(
            gold[k] for k in ("categories", "statement_pattern_groups", "critical_log_patterns")
        ):
            raise ValueError("Root-cause scoring needs category, claim, and evidence labels")
        for pattern in gold["critical_log_patterns"]:
            re.compile(pattern, re.IGNORECASE)
            if not any(
                re.search(pattern, e["text"], re.IGNORECASE)
                for e in case["payload"]["data"]["logs"]
            ):
                raise ValueError("Critical evidence label does not exist in input")
        for group in gold["statement_pattern_groups"]:
            if not group:
                raise ValueError("Empty claim pattern group")
            for pattern in group:
                re.compile(pattern, re.IGNORECASE)
        fault = gold["source_fault"]
        if fault:
            matching = [f for f in case["source_files"] if f["path"] == fault["path"]]
            if len(matching) != 1 or not 1 <= fault["line"] <= len(
                matching[0]["content"].splitlines()
            ):
                raise ValueError("Source fault label is outside the supplied source")
            trace = "\n".join(e["text"] for e in case["payload"]["data"]["logs"])
            if f'File "{fault["path"]}"' not in trace:
                raise ValueError("Source traceback must use the supplied project-relative path")
            request = adapt_backend(envelope.data, profile_id="profile-demo-a").diagnosis
            bundle = prepare_backend(envelope.data, request, 16_384)
            snapshot, _, _, _ = read_archive(
                fixture_archive(case),
                envelope.data.source,
                [
                    {
                        "path": fault["path"],
                        "start_line": 1,
                        "end_line": fault["line"],
                        "reason": "Fixture preflight only",
                        "evidence_ids": [bundle.lines[0].id],
                    }
                ],
                bundle,
                ArchivePolicy(),
            )
            if snapshot is None or not any(f.path == fault["path"] for f in snapshot.files):
                raise ValueError("Synthetic archive does not expose the expected source file")
    return manifest, hashlib.sha256(raw).hexdigest()


def fixture_archive(case):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for file in case["source_files"]:
            raw = file["content"].encode()
            entry = tarfile.TarInfo("eval-repo-abcdef1/" + file["path"])
            entry.size = len(raw)
            archive.addfile(entry, io.BytesIO(raw))
    return buffer.getvalue()


def grade(case, result, http_status):
    gold = case["gold"]
    analysis = result.get("analysis") or {}
    hypotheses = analysis.get("hypotheses") or []
    primary = hypotheses[0] if hypotheses else {}
    executed = http_status == 200 and result.get("job_status") == "succeeded"
    statement = primary.get("statement", "")
    refs = set(primary.get("evidence_ids", []))
    cited = [e for e in result.get("evidence", []) if e["id"] in refs]
    category = primary.get("category") in gold["categories"]
    patterns = all(
        any(re.search(pattern, statement, re.IGNORECASE) for pattern in group)
        for group in gold["statement_pattern_groups"]
    )
    anchors = all(
        any(re.search(pattern, e["text"], re.IGNORECASE) for e in cited)
        for pattern in gold["critical_log_patterns"]
    )
    status_correct = executed and analysis.get("analysis_status") == gold["status"]
    root_correct = status_correct and bool(primary) and category and patterns and anchors
    source_hit = None
    fault = gold["source_fault"]
    source = result.get("source_analysis") or {}
    if fault:
        source_by_id = {e["id"]: e for e in source.get("evidence", [])}
        source_hit = executed and any(
            f["path"] == fault["path"]
            and f["start_line"] <= fault["line"] <= f["end_line"]
            and primary.get("id") in f.get("hypothesis_ids", [])
            and any(
                source_by_id.get(ref, {}).get("path") == fault["path"]
                and source_by_id.get(ref, {}).get("line") == fault["line"]
                for ref in f.get("source_evidence_ids", [])
            )
            for f in source.get("findings", [])
        )
    return {
        "execution_ok": executed,
        "status_correct": status_correct,
        "predicted_status": analysis.get("analysis_status") if executed else "execution_error",
        "primary_category": primary.get("category"),
        "primary_statement": statement,
        "root_cause_correct": root_correct if gold["status"] == "diagnosed" else None,
        "root_checks": {
            "category": category,
            "claim_patterns": patterns,
            "critical_evidence_cited": anchors,
        }
        if gold["status"] == "diagnosed"
        else None,
        "critical_evidence_hit": bool(primary) and anchors
        if gold["status"] == "diagnosed"
        else None,
        "source_location_hit": source_hit,
        "healthy_false_positive": executed and analysis.get("analysis_status") == "diagnosed"
        if gold["status"] == "no_failure_evidence"
        else None,
        "insufficient_overclaim": executed and analysis.get("analysis_status") == "diagnosed"
        if gold["status"] == "insufficient_evidence"
        else None,
        "advisory_only": result.get("remediation_execution") == "not_executed",
    }


def ratio(successes, total):
    if total == 0:
        return {"numerator": successes, "denominator": 0, "rate": None, "wilson_95": None}
    p, z = successes / total, 1.959963984540054
    denominator = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denominator
    radius = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return {
        "numerator": successes,
        "denominator": total,
        "rate": p,
        "wilson_95": [max(0, center - radius), min(1, center + radius)],
    }


def summarize(cases, rows, *, dataset_sha256, model, knowledge_mode, stop_reason=None):
    gold_by_id = {c["id"]: c["gold"] for c in cases}
    metrics = {}
    for name, field in [
        ("status_accuracy", "status_correct"),
        ("root_cause_accuracy", "root_cause_correct"),
        ("critical_evidence_hit_rate", "critical_evidence_hit"),
        ("source_location_hit_rate", "source_location_hit"),
        ("healthy_false_positive_rate", "healthy_false_positive"),
        ("insufficient_overclaim_rate", "insufficient_overclaim"),
    ]:
        applicable = [r["grade"][field] for r in rows if r["grade"][field] is not None]
        metrics[name] = ratio(sum(bool(value) for value in applicable), len(applicable))
    confusion = {}
    for row in rows:
        expected = gold_by_id[row["case_id"]]["status"]
        bucket = confusion.setdefault(expected, {})
        predicted = row["grade"]["predicted_status"]
        bucket[predicted] = bucket.get(predicted, 0) + 1
    tokens = {
        kind: sum((r["execution"].get("tokens") or {}).get(kind) or 0 for r in rows)
        for kind in ("input", "output", "reasoning")
    }
    times = [r["http_elapsed_ms"] for r in rows]
    graph_reports = [r["knowledge"]["report"] for r in rows if r.get("knowledge", {}).get("report")]
    calls = [r["execution"].get("provider_call_count") for r in rows]
    return {
        "dataset_kind": "synthetic_curated_not_production",
        "dataset_sha256": dataset_sha256,
        "model": model,
        "knowledge_mode": knowledge_mode,
        "planned": len(cases),
        "completed": len(rows),
        "stop_reason": stop_reason,
        "coverage": ratio(len(rows), len(cases)),
        "planned_status_counts": dict(Counter(c["gold"]["status"] for c in cases)),
        "execution_errors": sum(not r["grade"]["execution_ok"] for r in rows),
        "metrics": metrics,
        "status_confusion": confusion,
        "provider_calls": sum(c for c in calls if isinstance(c, int)),
        "provider_call_count_complete": all(isinstance(c, int) for c in calls),
        "tokens": tokens,
        "tokens_total": tokens["input"] + tokens["output"],
        "latency_ms": {
            "median": statistics.median(times) if times else None,
            "p95_nearest_rank": sorted(times)[math.ceil(len(times) * 0.95) - 1] if times else None,
        },
        "graphs": {
            "recorded": sum(
                r.get("knowledge", {}).get("stats", {}).get("recorded", 0) for r in rows
            ),
            "conforms": sum(report.get("conforms") is True for report in graph_reports),
        },
        "cases": rows,
        "limitations": [
            "Synthetic curated cases, not production incident accuracy or an independent test set.",
            (
                "Root-cause score is a frozen category + primary-claim regex + critical-evidence rubric; "
                "it is not a complete semantic or remediation-correctness assessment."
            ),
            "Four code faults are reproduced locally; other scenarios are authored logs.",
            "One real model run per case; no automatic retry, model judge, or prompt tuning.",
            "Failures count as incorrect on attempted cases; unattempted cases stay unmeasured.",
            (
                "Wilson intervals describe finite-sample uncertainty only; curated cases are not a "
                "random sample of production incidents."
            ),
            "Graph shadow mode does not change the model context or diagnosis.",
        ],
    }


async def evaluate(args, *, runtime_factory=None):
    manifest, digest = load_dataset(args.dataset)
    cases = manifest["cases"]
    if args.limit:
        cases = cases[: args.limit]
    # Read the explicitly selected file. Environment credentials cannot override this run.
    profile, key = load_direct_settings(args.env_file, "profile-demo-a", environ={})
    args.output_dir.mkdir(parents=True, exist_ok=False)
    save(args.output_dir / "dataset-frozen.json", manifest)
    rows, stop_reason = [], None
    factory = runtime_factory or (lambda: OpenAIResponsesRuntime(profile, key))
    for case in cases:
        archive = fixture_archive(case)
        downloads = []

        def download(_request, body=archive, received=downloads):
            received.append(True)
            return httpx.Response(200, content=body)

        app = create_app(
            factory,
            dev=True,
            archive_transport=httpx.MockTransport(download),
            knowledge_settings=KnowledgeSettings(
                mode=args.knowledge_mode, output_dir=args.output_dir / "knowledge"
            ),
        )
        started = time.perf_counter()
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://eval") as client,
        ):
            # Only the existing backend envelope reaches the application, never gold or name.
            response = await client.post("/diagnose", json=copy.deepcopy(case["payload"]))
            http_elapsed = (time.perf_counter() - started) * 1000
        result = response.json()
        # Refuse to save any provider credential if an unexpected adapter error exposes it.
        serialized = json.dumps(result, ensure_ascii=False)
        if key.get_secret_value() in serialized:
            raise RuntimeError("Credential detected in result; no output saved")
        save(args.output_dir / f"{case['id']}.json", result)
        row = {
            "case_id": case["id"],
            "name": case["name"],
            "family": case["family"],
            "expected_status": case["gold"]["status"],
            "http_status": response.status_code,
            "http_elapsed_ms": round(http_elapsed, 3),
            "downloads": len(downloads),
            "grade": grade(case, result, response.status_code),
            "execution": result.get("execution", {}),
            "error": result.get("error"),
        }
        if app.state.knowledge_observer:
            observer = app.state.knowledge_observer
            row["knowledge"] = {"stats": observer.stats, "report": observer.last_report}
        rows.append(row)
        code = (result.get("error") or {}).get("code")
        if not code:
            code = ((result.get("source_analysis") or {}).get("error") or {}).get("code")
        if code in STOP_CODES or any(
            s.get("runtime_reusable") is False for s in row["execution"].get("stages", [])
        ):
            stop_reason = code or "runtime_not_reusable"
        save(
            args.output_dir / "summary.json",
            summarize(
                cases,
                rows,
                dataset_sha256=digest,
                model=profile.model_id,
                knowledge_mode=args.knowledge_mode,
                stop_reason=stop_reason,
            ),
        )
        print(
            json.dumps(
                {
                    "case": case["id"],
                    "completed": len(rows),
                    "planned": len(cases),
                    "status_correct": row["grade"]["status_correct"],
                    "root_cause_correct": row["grade"]["root_cause_correct"],
                    "source_location_hit": row["grade"]["source_location_hit"],
                    "error_code": code,
                    "elapsed_ms": round(http_elapsed),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        if stop_reason:
            break
    return 0 if len(rows) == len(cases) and not stop_reason else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset", type=Path, default=ROOT / "evaluation/synthetic_incidents.v1.json"
    )
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--knowledge-mode", choices=("off", "shadow"), default="shadow")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    if not 0 <= args.limit <= 100:
        parser.error("limit must be 0..100")
    return asyncio.run(evaluate(args))


if __name__ == "__main__":
    raise SystemExit(main())
