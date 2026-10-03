"""Measure real inference subprocess latency and bounded admission without model calls."""

import argparse
import asyncio
import copy
import json
import math
import resource
import statistics
import sys
import time
from dataclasses import asdict
from pathlib import Path

from ai_error_check_agent.backend_contracts import BackendEnvelope, adapt_backend, prepare_backend
from ai_error_check_agent.knowledge.reasoning import ReasoningService, ReasoningSettings


def distribution(values):
    return {
        "n": len(values),
        "p50_ms": statistics.median(values),
        "p95_ms": sorted(values)[math.ceil(0.95 * len(values)) - 1],
    }


async def measure(dataset, copies=1):
    case = json.loads(dataset.read_text())["cases"][0]
    env = BackendEnvelope.model_validate_json(json.dumps(case["payload"]))
    request = adapt_backend(env.data, profile_id="profile-demo-a").diagnosis
    bundle = prepare_backend(env.data, request, 16384)
    data = {"scope": {"attempt_id": request.attempt_id}, "logs": [asdict(x) for x in bundle.lines]}
    seed = data["logs"]
    data["logs"] = []
    for replica in range(copies):
        for line in seed:
            row = copy.deepcopy(line)
            row["id"] = f"EV{len(data['logs']) + 1:06d}"
            row["text"] = row["text"].replace("worker-test-0", f"worker-test-0-{replica}")
            row["sequence"] += replica * 1000
            data["logs"].append(row)
    worker = ReasoningService(ReasoningSettings(mode="assist"))
    result = {}
    try:
        started = time.perf_counter()
        assert await worker.wait_ready()
        result["warmup_ms"] = (time.perf_counter() - started) * 1000
        elapsed = []
        for _ in range(40):
            started = time.perf_counter()
            report = await worker.infer(data)
            elapsed.append((time.perf_counter() - started) * 1000)
            assert report["status"] == "ok"
        result["sequential"] = distribution(elapsed)
        result["triple_count"] = report["metrics"]["triples"]
        for count in (1, 2, 4):
            statuses = []
            times = []
            for _ in range(8):
                assert await worker.wait_ready()

                async def one(times=times):
                    start = time.perf_counter()
                    r = await worker.infer(data)
                    times.append((time.perf_counter() - start) * 1000)
                    return r["status"]

                statuses.extend(await asyncio.gather(*(one() for _ in range(count))))
            result[f"concurrent_{count}"] = {
                "latency": distribution(times),
                "statuses": {s: statuses.count(s) for s in set(statuses)},
            }
        result["note"] = (
            "Inference wall time only, no LLM/HTTP. Busy fallback is counted separately; one worker per API process."
        )
        return result
    finally:
        await worker.aclose()
        usage = resource.getrusage(resource.RUSAGE_CHILDREN)
        result["worker_cpu_ms"] = (usage.ru_utime + usage.ru_stime) * 1000
        result["worker_peak_rss_bytes"] = (
            usage.ru_maxrss if sys.platform == "darwin" else usage.ru_maxrss * 1024
        )


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--copies", type=int, choices=range(1, 7), default=1)
    args = p.parse_args()
    report = asyncio.run(
        measure(Path(__file__).with_name("relation_reasoning.test.v1.json"), args.copies)
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
