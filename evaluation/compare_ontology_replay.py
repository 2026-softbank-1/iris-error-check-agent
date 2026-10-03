"""Compare pre-ontology code and shadow code with frozen actual LLM answers, no paid calls."""

import argparse
import asyncio
import base64
import copy
import gzip
import hashlib
import io
import json
import os
import statistics
import subprocess
import sys
import tarfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


async def replay_worker(args):
    import httpx

    from ai_error_check_agent import api
    from ai_error_check_agent.direct_api import DirectModelProfile
    from ai_error_check_agent.runtime import ModelResponse

    package_source = Path(api.__file__).resolve().parent.parent
    assert package_source == args.source_root.resolve()
    tapes = json.loads(args.tapes.read_text())
    active = {"responses": [], "seen": [], "archive": b""}

    class ReplayRuntime:
        def __init__(self):
            self.response = active["responses"].pop(0)
            self.profile = DirectModelProfile(profile_id="profile-demo-a", model_id="gpt-6.1-sol")
            self.message_submissions = 0
            self.provider_call_count = 0
            self.runtime_version = "saved-model-answer-replay.v1"
            self.cleanup_status = "not_needed"
            self.abort_confirmed = None
            self.reusable = True

        async def run(self, prompt, data, schema):
            self.message_submissions = 1
            active["seen"].append({"prompt": prompt, "input": data, "schema": schema})
            return ModelResponse(json.dumps(self.response, ensure_ascii=False), {})

        async def close(self):
            pass

    options = {}
    if args.mode == "shadow":
        from ai_error_check_agent.knowledge.shadow import KnowledgeSettings

        options["knowledge_settings"] = KnowledgeSettings(
            mode="shadow", output_dir=args.output_dir / "knowledge"
        )
    app = api.create_app(
        ReplayRuntime,
        dev=True,
        archive_transport=httpx.MockTransport(
            lambda _: httpx.Response(200, content=active["archive"])
        ),
        **options,
    )
    args.output_dir.mkdir(parents=True, exist_ok=False)
    rows = []
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://replay") as client,
    ):
        save(args.output_dir / "openapi.json", (await client.get("/openapi.json")).json())
        for tape in tapes:
            active.update(
                responses=copy.deepcopy(tape["responses"]),
                seen=[],
                archive=base64.b64decode(tape["archive"]),
            )
            started = time.perf_counter()
            response = await client.post("/diagnose", json=tape["payload"])
            elapsed = (time.perf_counter() - started) * 1000
            assert response.status_code == 200, response.text
            assert not active["responses"], "Saved response was not consumed"
            result = response.json()
            save(args.output_dir / f"{tape['id']}.json", result)
            observer = getattr(app.state, "knowledge_observer", None)
            if observer is not None:
                await observer.drain()
            row = {
                "id": tape["id"],
                "http_status": response.status_code,
                "http_ms": elapsed,
                "model_submissions": len(active["seen"]),
                "model_inputs_sha256": hashlib.sha256(
                    json.dumps(active["seen"], ensure_ascii=False, sort_keys=True).encode()
                ).hexdigest(),
            }
            if observer is not None:
                row["graph"] = copy.deepcopy(observer.last_report)
            rows.append(row)
    save(
        args.output_dir / "worker-summary.json",
        {
            "source_root": str(package_source),
            "mode": args.mode,
            "cases": rows,
            "actual_provider_calls": 0,
            "http_median_ms": statistics.median(r["http_ms"] for r in rows),
        },
    )


def compare(args):
    from run_synthetic_accuracy import fixture_archive, grade, load_dataset, summarize

    manifest, dataset_hash = load_dataset(args.dataset)
    live = json.loads((args.live_results / "summary.json").read_text())
    assert live["dataset_sha256"] == dataset_hash and live["completed"] == len(manifest["cases"])
    args.output_dir.mkdir(parents=True, exist_ok=False)
    tapes = []
    for case in manifest["cases"]:
        public = json.loads((args.live_results / f"{case['id']}.json").read_text())
        row = next(r for r in live["cases"] if r["case_id"] == case["id"])
        path = Path(row["knowledge"]["report"]["path"])
        path = path if path.is_absolute() else ROOT / path
        record = json.loads(gzip.decompress((path / "record.json.gz").read_bytes()))
        first = copy.deepcopy(record["stages"][0]["analysis"])
        source = public["source_analysis"]
        first["source_request"] = {
            "needed": source["status"] != "not_needed",
            "reason": source["reason"],
            "files": source["requested_files"],
        }
        responses = [first]
        if source["status"] == "analyzed":
            second = copy.deepcopy(public["analysis"])
            second["source_findings"] = {"findings": source["findings"]}
            responses.append(second)
        tapes.append(
            {
                "id": case["id"],
                "payload": case["payload"],
                "responses": responses,
                "archive": base64.b64encode(fixture_archive(case)).decode(),
            }
        )
    tapes_path = args.output_dir / "replay-tapes.json"
    save(tapes_path, tapes)
    commit = subprocess.run(
        ["git", "rev-parse", args.baseline_ref],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    archive = subprocess.run(
        ["git", "archive", commit, "src"], cwd=ROOT, check=True, capture_output=True
    ).stdout
    baseline = ROOT / ".runtime/ontology-comparison" / commit
    baseline.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(baseline, filter="data")
    for mode, source_root in (("before", baseline / "src"), ("shadow", ROOT / "src")):
        subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--worker",
                "--mode",
                mode,
                "--source-root",
                str(source_root),
                "--tapes",
                str(tapes_path),
                "--output-dir",
                str(args.output_dir / mode),
            ],
            cwd=ROOT,
            env={**os.environ, "PYTHONPATH": str(source_root)},
            check=True,
        )
    summaries = {
        mode: json.loads((args.output_dir / mode / "worker-summary.json").read_text())
        for mode in ("before", "shadow")
    }
    equality, scored = [], {}
    for mode in ("before", "shadow"):
        rows = []
        for case, replay in zip(manifest["cases"], summaries[mode]["cases"], strict=True):
            result = json.loads((args.output_dir / mode / f"{case['id']}.json").read_text())
            rows.append(
                {
                    "case_id": case["id"],
                    "grade": grade(case, result, replay["http_status"]),
                    "execution": result["execution"],
                    "http_elapsed_ms": replay["http_ms"],
                }
            )
        scored[mode] = summarize(
            manifest["cases"],
            rows,
            dataset_sha256=dataset_hash,
            model="frozen actual gpt-6.1-sol answers",
            knowledge_mode=mode,
        )
    for case, before, after in zip(
        manifest["cases"], summaries["before"]["cases"], summaries["shadow"]["cases"], strict=True
    ):
        left = json.loads((args.output_dir / "before" / f"{case['id']}.json").read_text())
        right = json.loads((args.output_dir / "shadow" / f"{case['id']}.json").read_text())
        for result in (left, right):
            result.pop("diagnosis_id")
            result.pop("execution")
        equality.append(
            {
                "id": case["id"],
                "public_result_equal": left == right,
                "model_inputs_equal": before["model_inputs_sha256"] == after["model_inputs_sha256"],
                "model_submissions_equal": before["model_submissions"]
                == after["model_submissions"],
            }
        )
    before_api = json.loads((args.output_dir / "before/openapi.json").read_text())
    after_api = json.loads((args.output_dir / "shadow/openapi.json").read_text())
    report = {
        "scope": "Pre-ontology Git snapshot vs current shadow; frozen actual answer replay, not independent live A/B",
        "baseline_commit": commit,
        "dataset_sha256": dataset_hash,
        "completed": len(equality),
        "actual_provider_calls": 0,
        "public_results_equal": sum(r["public_result_equal"] for r in equality),
        "model_inputs_equal": sum(r["model_inputs_equal"] for r in equality),
        "model_submissions_equal": sum(r["model_submissions_equal"] for r in equality),
        "openapi_equal": before_api == after_api,
        "before_metrics": scored["before"]["metrics"],
        "shadow_metrics": scored["shadow"]["metrics"],
        "same_metrics_as_live_run": scored["before"]["metrics"]
        == live["metrics"]
        == scored["shadow"]["metrics"],
        "shadow_graphs_conform": sum(
            r.get("graph", {}).get("conforms") is True for r in summaries["shadow"]["cases"]
        ),
        "cases": equality,
        "limitations": [
            "Source selection envelopes reconstructed from stored records and public results.",
            "Diagnosis identifiers and execution metadata excluded from output equality.",
            "Replay removes model randomness and provider waiting; it proves compatibility, not an accuracy gain.",
        ],
    }
    save(args.output_dir / "summary.json", report)
    assert report["openapi_equal"] and report["same_metrics_as_live_run"]
    assert all(
        all(
            row[k] for k in ("public_result_equal", "model_inputs_equal", "model_submissions_equal")
        )
        for row in equality
    )
    print(
        json.dumps(
            {
                k: report[k]
                for k in (
                    "completed",
                    "public_results_equal",
                    "model_inputs_equal",
                    "model_submissions_equal",
                    "openapi_equal",
                    "same_metrics_as_live_run",
                    "shadow_graphs_conform",
                    "actual_provider_calls",
                )
            },
            ensure_ascii=False,
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-ref", default="40d39767fcd209c24e84dd88aebeb9979d0a6e9b")
    parser.add_argument(
        "--dataset", type=Path, default=ROOT / "evaluation/synthetic_incidents.v1.json"
    )
    parser.add_argument(
        "--live-results", type=Path, default=ROOT / "results/synthetic-accuracy-final-20261003"
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--mode", choices=("before", "shadow"))
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--tapes", type=Path)
    args = parser.parse_args()
    if args.worker:
        asyncio.run(replay_worker(args))
    else:
        compare(args)


if __name__ == "__main__":
    main()
