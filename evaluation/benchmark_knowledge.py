"""Measure isolated shadow graph jobs using an existing v3 result; no LLM calls."""

import argparse
import asyncio
import copy
import gzip
import json
import math
import platform
import statistics
from pathlib import Path
from uuid import uuid4

from ai_error_check_agent.knowledge.shadow import KnowledgeSettings, ShadowObserver
from ai_error_check_agent.source_contracts import DiagnoseAPIResult


def distribution(values):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    return {
        "p50": statistics.median(values),
        "p95": values[math.ceil(0.95 * len(values)) - 1],
        "min": values[0],
        "max": values[-1],
    }


async def benchmark(input_path, output_dir, *, runs=10, stage_record=None):
    if not 3 <= runs <= 100:
        raise ValueError("runs must be between 3 and 100")
    if input_path.stat().st_size > 1_048_576:
        raise ValueError("result input too large")
    result = json.loads(input_path.read_text("utf-8"))
    result = result.get("result", result)
    DiagnoseAPIResult.model_validate_json(json.dumps(result))
    stages = []
    if stage_record is not None:
        with gzip.open(stage_record, "rb") as handle:
            raw = handle.read(1_048_577)
        if len(raw) > 1_048_576:
            raise ValueError("stage record too large")
        record = json.loads(raw)
        if record["diagnosis_id"] != result["diagnosis_id"]:
            raise ValueError("stage record and public result must come from the same diagnosis")
        stages = record["stages"]
    output_dir.mkdir(parents=True, exist_ok=False)
    observer = ShadowObserver(KnowledgeSettings(mode="shadow", output_dir=output_dir / "jobs"))
    samples = []
    try:
        for index in range(runs):
            replay = copy.deepcopy(result)
            replay["diagnosis_id"] = "benchmark-" + uuid4().hex
            status = await observer.observe(replay, stages)
            row = {"index": index, "status": status}
            if status == "recorded":
                metadata_path = Path(observer.last_report["path"]) / "metadata.json"
                metadata = json.loads(metadata_path.read_text("utf-8"))
                row.update(
                    elapsed_ms=observer.last_report["observer_elapsed_ms"],
                    worker_reused=observer.last_report.get("worker_reused", False),
                    cpu_ms=metadata.get("worker_job_cpu_ms"),
                    process_cpu_ms=metadata["worker_cpu_ms"],
                    peak_rss_bytes=metadata["worker_peak_rss_bytes"],
                    total_artifact_bytes=sum(
                        p.stat().st_size for p in metadata_path.parent.iterdir()
                    ),
                    triples=metadata["diagnosis"]["triples"],
                    conforms=metadata["diagnosis"]["conforms"],
                    timing=metadata["timing"],
                )
            samples.append(row)
    finally:
        # The compatibility branch permits replaying the saved pre-optimization observer.
        if hasattr(observer, "aclose"):
            await observer.aclose()
    metrics = ("elapsed_ms", "cpu_ms", "peak_rss_bytes", "total_artifact_bytes")
    summary = {
        "scope": "Saved result replay; graph overhead only; no model invocation or accuracy claim",
        "environment": {"platform": platform.platform(), "python": platform.python_version()},
        "planned": runs,
        "recorded": observer.stats["recorded"],
        "stats": observer.stats,
        "distributions": {key: distribution([s.get(key) for s in samples]) for key in metrics},
        "warm_distributions": {
            key: distribution([s.get(key) for s in samples if s.get("worker_reused")])
            for key in metrics
        },
        "stage_history_complete": bool(stages),
        "samples": samples,
        "limitations": [
            "Cold elapsed time includes process startup; warm jobs reuse one isolated interpreter.",
            "cpu_ms is job CPU; process_cpu_ms and peak RSS are process lifetime samples.",
            "Both stages are measured only when --stage-record supplies the matching online record.",
            "Repeated one-case measurements are not a deployment capacity or accuracy estimate.",
        ],
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2), "utf-8")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--stage-record", type=Path)
    args = parser.parse_args()
    report = asyncio.run(
        benchmark(args.input, args.output_dir, runs=args.runs, stage_record=args.stage_record)
    )
    print(json.dumps({"recorded": report["recorded"], "distributions": report["distributions"]}))
    raise SystemExit(0 if report["recorded"] == args.runs else 1)
