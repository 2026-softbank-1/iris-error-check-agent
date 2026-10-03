"""Offline rule checks and optional independently called baseline/facts/rules LLM comparison."""

import argparse
import asyncio
import copy
import json
import statistics
import time
from pathlib import Path

import httpx
from run_synthetic_accuracy import STOP_CODES, fixture_archive, grade, load_dataset, save, summarize

from ai_error_check_agent.api import create_app
from ai_error_check_agent.backend_contracts import BackendEnvelope, adapt_backend, prepare_backend
from ai_error_check_agent.direct_api import OpenAIResponsesRuntime, load_direct_settings
from ai_error_check_agent.knowledge.reasoner import run_reasoning
from ai_error_check_agent.knowledge.reasoning import ReasoningSettings
from ai_error_check_agent.knowledge.shadow import KnowledgeSettings

ROOT = Path(__file__).resolve().parents[1]


def offline(manifest):
    rows = []
    for case in manifest["cases"]:
        envelope = BackendEnvelope.model_validate_json(json.dumps(case["payload"]))
        request = adapt_backend(envelope.data, profile_id="profile-demo-a").diagnosis
        bundle = prepare_backend(envelope.data, request, 16384)
        sources = []
        for f in case["source_files"]:
            for line, text in enumerate(f["content"].splitlines(), 1):
                sources.append(
                    {
                        "id": f"SC{len(sources) + 1:06d}",
                        "path": f["path"],
                        "line": line,
                        "text": text,
                    }
                )
        data = {
            "scope": {"attempt_id": request.attempt_id},
            "logs": bundle.model_payload()["logs"],
            "source_evidence": sources,
        }
        report = run_reasoning(data)
        actual = sorted({c["kind"] for c in report["candidates"]})

        def relation_key(r):
            return (r["kind"], tuple(r["evidence_ids"]), tuple(r["source_evidence_ids"]))

        predicted = {relation_key(c) for c in report["candidates"]}
        expected = {relation_key(c) for c in case["expected_relations"]}
        rows.append(
            {
                "case_id": case["id"],
                "expected": case["expected_relation_kinds"],
                "actual": actual,
                "passed": predicted == expected,
                "metrics": report["metrics"],
                "tp": len(predicted & expected),
                "fp": len(predicted - expected),
                "fn": len(expected - predicted),
            }
        )
    times = [r["metrics"]["total_ms"] for r in rows]
    tp, fp, fn = (sum(r[k] for r in rows) for k in ("tp", "fp", "fn"))
    return {
        "relation_scores": {
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "precision": tp / (tp + fp) if tp + fp else None,
            "recall": tp / (tp + fn) if tp + fn else None,
            "f1": 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else None,
        },
        "cases": len(rows),
        "passed": sum(r["passed"] for r in rows),
        "rows": rows,
        "note": "Derived relation and EV/SC anchor checks on authored scenarios; not diagnosis accuracy or all-ontology edge F1.",
        "latency_ms": {"median": statistics.median(times), "max": max(times)},
    }


async def live(args, manifest, digest):
    profile, key = load_direct_settings(args.env_file, "profile-demo-a", environ={})
    rows = {arm: [] for arm in ("baseline", "facts", "rules")}
    cases = manifest["cases"][: args.limit] if args.limit else manifest["cases"]
    for arm in rows:
        (args.output_dir / arm).mkdir(exist_ok=True)

    async def one(case, arm):
        calls = []

        def factory():
            runtime = OpenAIResponsesRuntime(profile, key)
            original = runtime.run

            async def recorded(prompt, data, schema):
                response = await original(prompt, data, schema)
                calls.append(
                    {
                        "prompt": prompt,
                        "input": data,
                        "schema": schema,
                        "response": response.text,
                        "metadata": response.metadata,
                    }
                )
                return response

            runtime.run = recorded
            return runtime

        archive = fixture_archive(case)
        app = create_app(
            factory,
            dev=True,
            archive_transport=httpx.MockTransport(lambda _: httpx.Response(200, content=archive)),
            knowledge_settings=KnowledgeSettings(
                mode="shadow", output_dir=args.output_dir / arm / "knowledge"
            ),
            reasoning_settings=ReasoningSettings(
                mode="off" if arm == "baseline" else "assist",
                variant="facts" if arm == "facts" else "rules",
            ),
        )
        async with app.router.lifespan_context(app):
            service = app.state.reasoning_service
            if service and not await service.wait_ready():
                raise RuntimeError("Worker warmup failed; case not submitted to model")
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://eval"
            ) as client:
                started = time.perf_counter()
                response = await client.post("/diagnose", json=copy.deepcopy(case["payload"]))
                elapsed = (time.perf_counter() - started) * 1000
        result = response.json()
        if key.get_secret_value() in json.dumps([result, calls], ensure_ascii=False):
            raise RuntimeError("Credential detected; no output saved")
        save(args.output_dir / arm / f"{case['id']}.json", result)
        save(args.output_dir / arm / f"{case['id']}.model.json", calls)
        code = (result.get("error") or {}).get("code") or (
            (result.get("source_analysis") or {}).get("error") or {}
        ).get("code")
        row = {
            "case_id": case["id"],
            "expected_status": case["gold"]["status"],
            "family": case["family"],
            "http_status": response.status_code,
            "http_elapsed_ms": elapsed,
            "grade": grade(case, result, response.status_code),
            "execution": result.get("execution", {}),
            "reasoning": service.last_report if service else None,
            "knowledge": {
                "stats": app.state.knowledge_observer.stats,
                "report": app.state.knowledge_observer.last_report,
            },
            "stop_code": code if code in STOP_CODES else None,
        }
        rows[arm].append(row)
        rows[arm].sort(key=lambda r: r["case_id"])
        summary = {
            name: summarize(
                cases,
                items,
                dataset_sha256=digest,
                model=profile.model_id,
                knowledge_mode="shadow",
                stop_reason=next((r["stop_code"] for r in items if r["stop_code"]), None),
            )
            for name, items in rows.items()
        }
        save(args.output_dir / "live-summary.json", summary)
        # Keep internal inference data out of public responses, but retain it in evaluation.
        save(args.output_dir / "execution-rows.json", rows)
        print(
            json.dumps(
                {
                    "case": case["id"],
                    "arm": arm,
                    "status_correct": row["grade"]["status_correct"],
                    "root_correct": row["grade"]["root_cause_correct"],
                    "error": code,
                    "elapsed_ms": round(elapsed),
                }
            ),
            flush=True,
        )
        return bool(row["stop_code"]) or any(
            stage.get("runtime_reusable") is False
            for stage in result.get("execution", {}).get("stages", [])
        )

    # Separate app/worker per task avoids busy fallback corrupting an accuracy comparison.
    # Bounded cohorts; finish and record in-flight requests before stopping on provider errors.
    for offset in range(0, len(cases), args.case_concurrency):
        work = []
        for index, case in enumerate(cases[offset : offset + args.case_concurrency], offset):
            arms = list(rows)
            for arm in arms[index % 3 :] + arms[: index % 3]:
                work.append(one(case, arm))
        stopped = await asyncio.gather(*work)
        if any(stopped):
            return 1
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset", type=Path, default=ROOT / "evaluation/relation_reasoning.test.v1.json"
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--case-concurrency", type=int, choices=(1, 2), default=1)
    args = parser.parse_args()
    if args.limit < 0:
        parser.error("limit must be nonnegative")
    manifest, digest = load_dataset(args.dataset)
    args.output_dir.mkdir(parents=True, exist_ok=False)
    save(args.output_dir / "dataset-frozen.json", manifest)
    report = offline(manifest)
    save(args.output_dir / "offline.json", report)
    print(
        json.dumps(
            {
                "offline_passed": report["passed"],
                "cases": report["cases"],
                "latency_ms": report["latency_ms"],
            }
        ),
        flush=True,
    )
    if report["passed"] != report["cases"]:
        return 1
    return asyncio.run(live(args, manifest, digest)) if args.live else 0


if __name__ == "__main__":
    raise SystemExit(main())
