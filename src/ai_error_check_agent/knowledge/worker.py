"""Isolated local graph worker; one job or bounded sequential jobs in serve mode."""

import argparse
import gzip
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from ..preprocessing import redact_object
from .graph import build_graphs
from .validation import validate_graph

try:
    import resource
except ImportError:  # Windows: graph processing works, RSS telemetry is unavailable.
    resource = None


def persist(record, graphs, summary, reports, output_dir, *, cpu_started=None, max_bytes=4_194_304):
    started = time.perf_counter()
    key = hashlib.sha256(record["diagnosis_id"].encode()).hexdigest()
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = root / key
    if destination.exists():
        raise FileExistsError("knowledge record already exists")
    blobs = {
        "record.json.gz": gzip.compress(json.dumps(record, ensure_ascii=False).encode(), mtime=0),
        "evidence.ttl.gz": gzip.compress(
            graphs.evidence.serialize(format="turtle").encode(), mtime=0
        ),
        "diagnosis.ttl.gz": gzip.compress(
            graphs.diagnosis.serialize(format="turtle").encode(), mtime=0
        ),
        "evidence-validation.ttl.gz": gzip.compress(
            reports[0].serialize(format="turtle").encode(), mtime=0
        ),
        "diagnosis-validation.ttl.gz": gzip.compress(
            reports[1].serialize(format="turtle").encode(), mtime=0
        ),
    }
    if record.get("reasoning"):
        stages = record["reasoning"]
        artifacts = {
            "reasoning-facts.json.gz": [
                {"stage": s.get("stage"), "facts": s.get("facts", [])} for s in stages
            ],
            "reasoning-trace.json.gz": [
                {
                    k: s.get(k)
                    for k in (
                        "stage",
                        "status",
                        "trace",
                        "limitations",
                        "versions",
                        "input_sha256",
                        "applied_context",
                        "variant",
                    )
                }
                for s in stages
            ],
        }
        for name, value in artifacts.items():
            blobs[name] = gzip.compress(json.dumps(value, ensure_ascii=False).encode(), mtime=0)
        blobs["reasoning-metrics.json"] = json.dumps(
            [
                {k: s.get(k) for k in ("stage", "status", "metrics", "observer_elapsed_ms")}
                for s in stages
            ],
            ensure_ascii=False,
        ).encode()
    summary["artifact_bytes"] = sum(map(len, blobs.values()))
    summary["artifacts_sha256"] = {
        name: hashlib.sha256(blob).hexdigest() for name, blob in blobs.items()
    }
    summary["timing"]["serialize_ms"] = round((time.perf_counter() - started) * 1000, 3)
    # Sampled after serialization, before file writes and process exit.
    summary["worker_cpu_ms"] = round(time.process_time() * 1000, 3)
    summary["worker_job_cpu_ms"] = (
        round((time.process_time() - cpu_started) * 1000, 3) if cpu_started is not None else None
    )
    peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss if resource else None
    summary["worker_peak_rss_bytes"] = (
        peak_rss if peak_rss is None or sys.platform == "darwin" else peak_rss * 1024
    )
    blobs["metadata.json"] = json.dumps(summary, ensure_ascii=False, indent=2).encode()
    if sum(map(len, blobs.values())) > max_bytes:
        raise ValueError("knowledge artifact byte limit exceeded")
    staging = Path(tempfile.mkdtemp(prefix=".pending-", dir=root))
    try:
        for name, blob in blobs.items():
            with os.fdopen(
                os.open(staging / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb"
            ) as handle:
                handle.write(blob)
        os.rename(staging, destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return str(destination)


def process_record(record, output_dir, *, max_triples=10_000, mode="shadow"):
    started = time.perf_counter()
    cpu_started = time.process_time()
    record = redact_object(record)
    graphs = build_graphs(record, max_triples=max_triples)
    built = time.perf_counter()
    evidence, first_report = validate_graph(graphs.evidence, graphs.root)
    diagnosis, final_report = validate_graph(graphs.diagnosis, graphs.root)
    checked = time.perf_counter()
    summary = {
        "record_schema": "iris-knowledge-record.v1",
        "diagnosis_id": record["diagnosis_id"],
        "mode": mode,
        "created_at": datetime.now(UTC).isoformat(),
        "evidence": evidence,
        "diagnosis": diagnosis,
        "timing": {
            "build_ms": round((built - started) * 1000, 3),
            "validation_ms": round((checked - built) * 1000, 3),
        },
        "limitations": [
            "SHACL conformance is not root cause correctness.",
            "Only supplied, masked evidence and accepted model assertions are projected.",
            "Hypotheses remain unverified; commit identity remains caller-supplied.",
        ],
    }
    path = persist(
        record, graphs, summary, (first_report, final_report), output_dir, cpu_started=cpu_started
    )
    return {
        "status": "recorded",
        "path": path,
        "conforms": diagnosis["conforms"],
        "coverage": diagnosis["coverage"],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--max-triples", type=int, default=10_000)
    parser.add_argument("--serve", action="store_true")
    args = parser.parse_args()
    while True:
        raw = sys.stdin.buffer.readline(262_146) if args.serve else sys.stdin.buffer.read(262_145)
        if args.serve and not raw:
            return 0
        try:
            if len(raw.rstrip(b"\n")) > 262_144 or (args.serve and not raw.endswith(b"\n")):
                raise ValueError("knowledge record too large")
            result = process_record(json.loads(raw), args.output_dir, max_triples=args.max_triples)
        except Exception:  # noqa: BLE001 - isolate failures and never print secret details
            result = {"status": "failed", "reason": "worker_error"}
        print(json.dumps(result), flush=True)
        if not args.serve:
            return 0 if result["status"] == "recorded" else 1
        if result["status"] != "recorded":
            # Failed workers are discarded rather than carrying uncertain state to the next job.
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
