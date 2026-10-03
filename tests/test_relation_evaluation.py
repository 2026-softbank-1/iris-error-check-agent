import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evaluation"))
from run_relation_ablation import offline
from run_synthetic_accuracy import load_dataset


def test_relation_datasets_are_valid_and_predeclared():
    dev, _ = load_dataset(ROOT / "evaluation/relation_reasoning.dev.v1.json")
    test, _ = load_dataset(ROOT / "evaluation/relation_reasoning.test.v1.json")
    assert len(dev["cases"]) == 24 and len(test["cases"]) == 60
    assert sum(c["family"] == "fault" for c in test["cases"]) == 24
    assert {c["id"] for c in dev["cases"]}.isdisjoint(c["id"] for c in test["cases"])
    for dataset in (dev, test):
        result = offline(dataset)
        assert result["passed"] == len(dataset["cases"])
        assert result["relation_scores"]["fp"] == result["relation_scores"]["fn"] == 0


def test_relation_grader_rejects_wrong_anchor_even_if_kind_matches():
    manifest, _ = load_dataset(ROOT / "evaluation/relation_reasoning.dev.v1.json")
    manifest["cases"] = copy.deepcopy(manifest["cases"][:1])
    manifest["cases"][0]["expected_relations"][0]["evidence_ids"] = ["EV999999"]
    report = offline(manifest)
    assert report["passed"] == 0
    assert report["relation_scores"]["fp"] > 0 and report["relation_scores"]["fn"] > 0


def test_live_runner_isolates_parallel_model_inputs_and_does_not_send_gold(
    monkeypatch, tmp_path, analysis_data
):
    import asyncio
    from types import SimpleNamespace

    import run_relation_ablation as module
    from pydantic import SecretStr
    from test_source_analysis import FakeRuntime, selection

    seen = []
    analysis_data = copy.deepcopy(analysis_data)
    analysis_data["remediation"]["plans"][0]["changes"][0]["target_known"] = False
    profile = FakeRuntime(None, []).profile
    monkeypatch.setattr(
        module,
        "load_direct_settings",
        lambda *_args, **_kwargs: (profile, SecretStr("test-secret-private")),
    )
    monkeypatch.setattr(
        module,
        "OpenAIResponsesRuntime",
        lambda *_args: FakeRuntime(selection(analysis_data, False), seen),
    )
    manifest, digest = load_dataset(ROOT / "evaluation/relation_reasoning.dev.v1.json")
    manifest["cases"] = manifest["cases"][:2]
    args = SimpleNamespace(
        env_file=tmp_path / "not-read.env", output_dir=tmp_path, limit=0, case_concurrency=2
    )
    assert asyncio.run(module.live(args, manifest, digest)) == 0
    assert len(seen) == 6
    assert all(
        r["http_status"] == 200
        for items in json.loads((tmp_path / "execution-rows.json").read_text()).values()
        for r in items
    )
    assert all("gold" not in data and "expected_relations" not in data for _, data, _ in seen)
    for arm in ("baseline", "facts", "rules"):
        for case in manifest["cases"]:
            captured = json.loads((tmp_path / arm / f"{case['id']}.model.json").read_text())
            assert len(captured) == 1
            data = captured[0]["input"]
            if arm == "baseline":
                assert "reasoning_context" not in data
            elif arm == "facts":
                assert (
                    data["reasoning_context"]["facts"]
                    and not data["reasoning_context"]["candidates"]
                )
            else:
                assert data["reasoning_context"]["candidates"]
