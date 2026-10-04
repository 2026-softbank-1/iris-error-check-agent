"""Paired adaptive vs graph_compact live evaluation with a frozen fixture dataset.

No model judge, no reuse of another arm's generated output. Semantic quality
is reviewed separately against the predeclared case rubric. All API input,
model/reasoning/tier/output limits and source bytes are identical per pair.
"""

import argparse
import copy
import hashlib
import json
import re
import statistics
import time
from pathlib import Path

import httpx
from run_synthetic_accuracy import STOP_CODES, fixture_archive, save

from ai_error_check_agent.api import create_app
from ai_error_check_agent.diagnosis_routing import DiagnosisSettings
from ai_error_check_agent.direct_api import OpenAIResponsesRuntime, load_direct_settings
from ai_error_check_agent.errors import DiagnosisError
from ai_error_check_agent.source_contracts import DiagnoseAPIResult

MODES = ("adaptive", "graph_compact")


def summary(rows, metadata):
    groups = {}
    for subset in ("all", "compact_eligible", "fallback_controls"):
        groups[subset] = {}
        for mode in metadata.get("modes", MODES):
            selected = [
                r
                for r in rows
                if r["mode"] == mode
                and (
                    subset == "all"
                    or (r["expected_route"] == "compact") == (subset == "compact_eligible")
                )
            ]
            ok = [r for r in selected if r["http_status"] == 200]
            groups[subset][mode] = {
                "n": len(selected),
                "succeeded": len(ok),
                "median_ms": statistics.median([r["elapsed_ms"] for r in ok]) if ok else None,
                "max_ms": max((r["elapsed_ms"] for r in ok), default=None),
                "within_10s": sum(r["within_10s"] for r in ok),
                "status_matches": sum(r["status_matches"] for r in selected),
                "korean_summary": sum(r["korean_summary"] for r in selected),
                "mean_input_tokens": statistics.mean([r["tokens"]["input"] for r in ok])
                if ok
                else None,
                "mean_output_tokens": statistics.mean([r["tokens"]["output"] for r in ok])
                if ok
                else None,
            }
    return {**metadata, "groups": groups, "rows": rows}


async def benchmark(args):
    profile, key = load_direct_settings(args.env_file, "profile-demo-a", environ={})
    if args.service_tier:
        if profile.provider_id != "openai":
            raise ValueError("tier override is OpenAI-only")
        profile = profile.model_copy(update={"service_tier": args.service_tier})
    raw = args.dataset.read_bytes()
    dataset = json.loads(raw)
    if dataset["version"] != "graph-compact-eval.v1":
        raise ValueError("unknown dataset")
    args.output_dir.mkdir(parents=True, exist_ok=False)
    metadata = {
        "dataset_sha256": hashlib.sha256(raw).hexdigest(),
        "model": profile.model_id,
        "reasoning_effort": profile.reasoning_effort,
        "max_output_tokens": profile.max_output_tokens,
        "requested_tier": profile.service_tier,
        "modes": list(args.modes),
        "note": dataset["note"],
        "semantic_review": "pending; structural/status checks are NOT diagnosis accuracy",
        "code_sha256": {
            str(p.relative_to(Path(__file__).resolve().parents[1])): hashlib.sha256(
                p.read_bytes()
            ).hexdigest()
            for p in sorted(
                (Path(__file__).resolve().parents[1] / "src/ai_error_check_agent").rglob("*.py")
            )
        },
        "assets_sha256": {
            str(p.relative_to(Path(__file__).resolve().parents[1])): hashlib.sha256(
                p.read_bytes()
            ).hexdigest()
            for p in sorted(
                (Path(__file__).resolve().parents[1] / "src/ai_error_check_agent").rglob("*")
            )
            if p.suffix in {".json", ".ttl", ".rq", ".md"}
        },
    }
    rows, calls_started = [], 0
    for case_index, case in enumerate(dataset["cases"][: args.limit]):
        blob = fixture_archive(case)
        modes = args.modes if case_index % 2 == 0 else tuple(reversed(args.modes))
        for mode in modes:
            captures, runtimes = [], []

            def factory(captures=captures, runtimes=runtimes):
                runtime = OpenAIResponsesRuntime(profile, key)
                runtimes.append(runtime)
                original = runtime.run

                async def recorded(prompt, data, schema):
                    nonlocal calls_started
                    if calls_started >= 32:
                        raise DiagnosisError(
                            "EVALUATION_CALL_LIMIT", "실제 모델 호출 상한에 도달했습니다."
                        )
                    calls_started += 1
                    started = time.monotonic()
                    entry = {
                        "compact": "candidate_id" in schema.get("properties", {}),
                        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                        "input": copy.deepcopy(data),
                        "schema": schema,
                    }
                    captures.append(entry)
                    try:
                        output = await original(prompt, data, schema)
                        entry.update(response=output.text, metadata=output.metadata)
                        return output
                    finally:
                        entry.update(
                            elapsed_ms=round((time.monotonic() - started) * 1000),
                            reported_tier=runtime.reported_service_tier,
                        )

                runtime.run = recorded
                return runtime

            app = create_app(
                factory,
                dev=True,
                diagnosis_settings=DiagnosisSettings(mode),
                archive_transport=httpx.MockTransport(
                    lambda _, blob=blob: httpx.Response(200, content=blob)
                ),
            )
            started = time.monotonic()
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://evaluation"
            ) as client:
                response = await client.post("/diagnose", json=case["payload"])
            elapsed = round((time.monotonic() - started) * 1000)
            result = response.json()
            save(args.output_dir / f"{case['id']}.{mode}.json", result)
            save(args.output_dir / f"{case['id']}.{mode}.model.json", captures)
            contract_valid = True
            try:
                DiagnoseAPIResult.model_validate(result)
            except ValueError:
                contract_valid = False
            report = app.state.compact_service.last_report if app.state.compact_service else None
            if report:
                save(args.output_dir / f"{case['id']}.{mode}.graph.json", report)
            analysis, source = result.get("analysis") or {}, result.get("source_analysis") or {}
            row = {
                "case": case["id"],
                "mode": mode,
                "expected_route": case["expected_route"],
                "http_status": response.status_code,
                "elapsed_ms": elapsed,
                "within_10s": response.status_code == 200 and elapsed <= 10000,
                "contract_valid": contract_valid,
                "status": analysis.get("analysis_status"),
                "status_matches": analysis.get("analysis_status") in case["acceptable_statuses"],
                "korean_summary": bool(re.search(r"[가-힣]", analysis.get("summary", ""))),
                "provider_calls": result.get("execution", {}).get("provider_call_count"),
                "tokens": result.get("execution", {}).get("tokens"),
                "actual_tiers": [r.reported_service_tier for r in runtimes],
                "compact_calls": sum(c["compact"] for c in captures),
                "graph_status": (report or {}).get("status"),
                "graph_metrics": (report or {}).get("metrics"),
                "graph_decision": (report or {}).get("decision_status"),
                "error": (result.get("error") or {}).get("code"),
                "source_error": (source.get("error") or {}).get("code"),
                "semantic_review": "pending",
            }
            rows.append(row)
            save(args.output_dir / "summary.json", summary(rows, metadata))
            print(json.dumps(row, ensure_ascii=False), flush=True)
            if (
                row["error"] in STOP_CODES | {"EVALUATION_CALL_LIMIT"}
                or row["source_error"] in STOP_CODES
            ):
                return 2
    return 0


def main():
    import asyncio

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--dataset", type=Path, default=Path(__file__).with_name("graph_compact.v1.json")
    )
    parser.add_argument("--limit", type=int, choices=range(1, 9), default=8)
    parser.add_argument("--service-tier", choices=("default", "fast", "priority"))
    parser.add_argument("--modes", nargs="+", choices=MODES, default=MODES)
    raise SystemExit(asyncio.run(benchmark(parser.parse_args())))


if __name__ == "__main__":
    main()
