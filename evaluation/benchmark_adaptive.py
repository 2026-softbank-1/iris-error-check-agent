"""Bounded real-LLM A/B smoke test. Fixture S3; unchanged model/reasoning/token cap.

This measures local end-to-end latency, not deployed S3 or production p95.
Semantic quality must also be reviewed using each case's review rubric.
"""

import argparse
import asyncio
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


async def benchmark(args):
    profile, key = load_direct_settings(args.env_file, "profile-demo-a", environ={})
    if args.service_tier:
        if profile.provider_id != "openai":
            raise ValueError("service tier override requires OpenAI")
        profile = profile.model_copy(update={"service_tier": args.service_tier})
    dataset = json.loads(args.dataset.read_text())
    args.output_dir.mkdir(parents=True, exist_ok=False)
    rows = []
    for i, case in enumerate(dataset["cases"][: args.limit]):
        # Alternate order to reduce simple first-run/cache/order bias.
        modes = args.modes if i % 2 == 0 else list(reversed(args.modes))
        for mode in modes:
            runtimes = []

            def factory(runtimes=runtimes):
                runtime = OpenAIResponsesRuntime(profile, key)
                runtimes.append(runtime)
                return runtime

            blob = fixture_archive(case)
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
                transport=httpx.ASGITransport(app),
                base_url="http://evaluation",
            ) as client:
                response = await client.post("/diagnose", json=case["payload"])
            elapsed = round((time.monotonic() - started) * 1000)
            result = response.json()
            save(args.output_dir / f"{case['id']}.{mode}.json", result)
            analysis = result.get("analysis") or {}
            source = result.get("source_analysis") or {}
            row = {
                "case": case["id"],
                "mode": mode,
                "http_status": response.status_code,
                "elapsed_ms": elapsed,
                "within_10s": response.status_code == 200 and elapsed <= 10000,
                "status": analysis.get("analysis_status"),
                "status_matches": analysis.get("analysis_status") == case["expected_status"],
                "korean_summary": bool(re.search(r"[가-힣]", analysis.get("summary", ""))),
                "provider_calls": result.get("execution", {}).get("provider_call_count"),
                "tokens": result.get("execution", {}).get("tokens"),
                "source_status": source.get("status"),
                "requested_tier": profile.service_tier,
                "reported_tiers": [r.reported_service_tier for r in runtimes],
                "error": (result.get("error") or {}).get("code"),
                "source_error": (source.get("error") or {}).get("code"),
                "semantic_review": "pending",
            }
            rows.append(row)
            save(
                args.output_dir / "summary.json",
                {
                    "model": profile.model_id,
                    "reasoning_effort": profile.reasoning_effort,
                    "max_output_tokens": profile.max_output_tokens,
                    "note": dataset["note"],
                    "rows": rows,
                    "latency_by_mode": {
                        m: {
                            "n": len(values),
                            "median_ms": statistics.median(values),
                            "max_ms": max(values),
                        }
                        for m in args.modes
                        if (values := [r["elapsed_ms"] for r in rows if r["mode"] == m])
                    },
                },
            )
            print(json.dumps(row, ensure_ascii=False), flush=True)
            if row["error"] in STOP_CODES or row["source_error"] in STOP_CODES:
                return 2
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live", action="store_true", required=True, help="Authorize paid API calls"
    )
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument(
        "--dataset", type=Path, default=Path(__file__).with_name("adaptive_smoke.v1.json")
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--limit", type=int, choices=range(1, 4), default=3)
    parser.add_argument(
        "--modes", nargs="+", choices=("standard", "adaptive"), default=["standard", "adaptive"]
    )
    parser.add_argument("--service-tier", choices=("default", "fast", "priority"))
    args = parser.parse_args()
    raise SystemExit(asyncio.run(benchmark(args)))


if __name__ == "__main__":
    main()
