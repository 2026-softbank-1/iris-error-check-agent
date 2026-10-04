"""Render recorded evaluation metrics; no model calls or agent imports.

Usage: python evaluation/plot_ieee_metrics.py [--output-dir DIR]
The checked-in JSON is a metrics-only snapshot with original source hashes.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import statistics
import tempfile
from pathlib import Path

os.environ.setdefault("MPLCONFIGDIR", str(Path(tempfile.gettempdir()) / "iris-ieee-mpl"))

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib import font_manager, ticker

ROOT = Path(__file__).resolve().parents[1]
WIDTH = 7.16  # IEEE two-column width, inches; do not use tight-bbox resizing.
BLUE = "#345B7E"
GRAY = "#B9BEC3"
INK = "#202020"
LIGHT = "#EFF3F6"
MODES = ("adaptive", "graph_compact")
ARMS = ("baseline", "facts", "rules")
ARM_LABELS = ("Baseline", "Facts", "Rules")
FILES: list[str] = []


def configure_style() -> str:
    font = "STIXGeneral"
    for candidate in ("Times New Roman", "Times", "STIXGeneral"):
        try:
            font_manager.findfont(candidate, fallback_to_default=False)
            font = candidate
            break
        except ValueError:
            continue
    plt.rcParams.update({
        "font.family": "serif", "font.serif": [font], "font.size": 9,
        "axes.titlesize": 10, "axes.labelsize": 9, "xtick.labelsize": 9,
        "ytick.labelsize": 9, "legend.fontsize": 9, "axes.linewidth": 0.65,
        "lines.linewidth": 1, "patch.linewidth": 0.6,
        "xtick.major.width": 0.6, "ytick.major.width": 0.6,
        "text.color": INK, "axes.labelcolor": INK,
        "axes.spines.top": False, "axes.spines.right": False,
        "figure.facecolor": "white", "axes.facecolor": "white",
        "savefig.facecolor": "white", "pdf.fonttype": 42, "ps.fonttype": 42,
        "svg.fonttype": "path", "svg.hashsalt": "iris-ieee-20261004",
        "hatch.linewidth": 0.5,
    })
    return font


def clean(ax, axis="y"):
    ax.grid(axis=axis, color="#DDDDDD", linewidth=0.45, zorder=0)
    ax.set_axisbelow(True)
    ax.tick_params(direction="out", length=3)


def frame(height=3.2, note=""):
    fig, axes = plt.subplots(1, 2, figsize=(WIDTH, height))
    fig.subplots_adjust(left=0.095, right=0.975, bottom=0.24, top=0.78, wspace=0.36)
    if note:
        fig.text(0.5, 0.96, note, ha="center", va="top", fontsize=9)
    return fig, axes


def save(fig, directory, name):
    # Fixed canvas preserves the intended physical width in PDF.
    fig.canvas.draw()
    for suffix in ("png", "pdf", "svg"):
        path = directory / f"{name}.{suffix}"
        options = {"dpi": 900} if suffix == "png" else {}
        if suffix == "pdf":
            options["metadata"] = {"CreationDate": None, "ModDate": None}
        if suffix == "svg":
            options["metadata"] = {"Date": None}
        fig.savefig(path, **options)
        if suffix == "svg":
            path.write_text("\n".join(line.rstrip() for line in path.read_text().splitlines()) + "\n")
        FILES.append(path.name)
    plt.close(fig)


def totals(row):
    return row["tokens"]["input"] + row["tokens"]["output"]


def aggregate(rows):
    return {
        "n": len(rows),
        "median_ms": statistics.median(r["elapsed_ms"] for r in rows),
        "mean_input_tokens": statistics.mean(r["tokens"]["input"] for r in rows),
        "mean_output_tokens": statistics.mean(r["tokens"]["output"] for r in rows),
        "mean_total_tokens": statistics.mean(totals(r) for r in rows),
        "within_10s": sum(r["elapsed_ms"] <= 10000 for r in rows),
        "contract_valid": sum(r["contract_valid"] for r in rows),
        "status_matches": sum(r["status_matches"] for r in rows),
        "provider_calls": sum(r["provider_calls"] for r in rows),
    }


def validate(data):
    paired = data["paired"]
    rows = paired["rows"]
    assert len(rows) == 16 and len({(r["case"], r["mode"]) for r in rows}) == 16
    groups = {}
    for name, predicate in (
        ("all", lambda r: True),
        ("compact_eligible", lambda r: r["expected_route"] == "compact"),
        ("fallback_controls", lambda r: r["expected_route"] != "compact"),
    ):
        groups[name] = {}
        for mode in MODES:
            result = aggregate([r for r in rows if r["mode"] == mode and predicate(r)])
            reported = paired["reported_groups"][name][mode]
            for key in ("n", "median_ms", "mean_input_tokens", "mean_output_tokens", "within_10s", "status_matches"):
                assert np.isclose(result[key], reported[key]), (name, mode, key)
            groups[name][mode] = result
    for row in data["history"]["stages"]:
        assert row["total_tokens"] == row["input_tokens"] + row["output_tokens"]
    for row in data["packaging"]["rows"]:
        reference = next(r for r in data["packaging"]["summary_rows"] if r["case"] == row["case"])
        assert totals(row) == reference["total_tokens"]
        assert row["elapsed_ms"] / 1000 == reference["elapsed_s"]
    for row in data["graph_microbenchmarks"]["shadow"]:
        assert statistics.median(row["before"]["samples_ms"]) == row["before"]["median_ms"]
        assert statistics.median(row["after"]["warm_samples_ms"]) == row["after"]["warm_median_ms"]
    return groups


def paired_figure(data, directory):
    cases = [
        ("missing-task-data", "S1  Missing file"),
        ("renamed-file", "S2  Renamed file"),
        ("repeated-stack", "S3  Repeated stack"),
        ("direct-literal", "S4  Literal path"),
        ("recovered-same-stream", "C1  Recovered"),
        ("other-stream-healthy", "C2  Other app healthy"),
        ("source-target-mismatch", "C3  Source mismatch"),
        ("expected-test-error", "C4  Expected test error"),
    ]
    fig, axes = frame(4.5, "Matched inputs and model; Fast tier; one run per case and mode")
    fig.subplots_adjust(left=0.21, bottom=0.15, top=0.79, wspace=0.25)
    y = np.arange(len(cases))
    rows = data["paired"]["rows"]
    for idx, ax in enumerate(axes):
        ax.axhspan(-0.5, 3.5, color=LIGHT, zorder=0)
        ax.axhline(3.5, color="#888888", linewidth=0.7, linestyle="--")
        for j, (mode, color, hatch) in enumerate(zip(MODES, (GRAY, BLUE), ("///", ""))):
            selected = [next(r for r in rows if r["case"] == case and r["mode"] == mode) for case, _ in cases]
            values = [r["elapsed_ms"] / 1000 if idx == 0 else totals(r) / 1000 for r in selected]
            ax.barh(y + (j - 0.5) * 0.32, values, height=0.29, color=color,
                    edgecolor=INK, hatch=hatch, label=mode, zorder=3)
        ax.set_yticks(y, [name for _, name in cases] if idx == 0 else [""] * len(cases))
        ax.set_ylim(7.6, -0.6)
        ax.set_xlim(0, 45 if idx == 0 else 22)
        ax.set_xlabel("Latency (s)" if idx == 0 else "Total tokens (thousands)")
        ax.set_title("(a) End-to-end latency" if idx == 0 else "(b) Input + output tokens", pad=9)
        clean(ax, "x")
    axes[0].axvline(10, color=INK, linestyle=":", linewidth=1, zorder=4)
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", bbox_to_anchor=(0.59, 0.91),
               ncol=2, frameon=False, handlelength=1.8)
    fig.text(0.5, 0.025, "S1-S4: compact-eligible. C1-C4: controls. Local API with fixture S3.", ha="center")
    save(fig, directory, "fig01_matched_performance")


def latency_bars(ax, values, labels, title, limit, decimals=3):
    bars = ax.bar(np.arange(len(values)), values, width=0.55,
                  color=(GRAY, "#7E92A2", BLUE), edgecolor=INK, zorder=3)
    bars[0].set_hatch("///")
    ax.bar_label(bars, labels=[f"{v:.{decimals}f}" for v in values], padding=4, fontsize=9)
    ax.set_xticks(np.arange(len(values)), labels)
    ax.set_ylim(0, limit)
    ax.set_ylabel("Latency (s)")
    ax.set_title(title, pad=10)
    clean(ax)


def stacked_tokens(ax, inputs, outputs, labels, title, scale=1000, limit=None):
    x = np.arange(len(inputs))
    a, b = np.array(inputs) / scale, np.array(outputs) / scale
    ax.bar(x, a, width=0.55, color=GRAY, edgecolor=INK, label="Input", zorder=3)
    ax.bar(x, b, width=0.55, bottom=a, color=BLUE, edgecolor=INK, hatch="///", label="Output", zorder=3)
    for pos, value in zip(x, a + b):
        ax.annotate(f"{value * scale:,.0f}", (pos, value), xytext=(0, 4),
                    textcoords="offset points", ha="center", fontsize=9)
    ax.set_xticks(x, labels)
    ax.set_ylabel("Token count")
    ax.yaxis.set_major_formatter(ticker.FuncFormatter(lambda value, _: f"{value:g}k" if value else "0"))
    ax.set_ylim(0, limit or max(a + b) * 1.28)
    ax.set_title(title, pad=10)
    clean(ax)


def history_figure(data, directory):
    rows = data["history"]["stages"]
    labels = ["A1\nStandard\n(default)", "A2\nCompact\n(Fast)", "A3\nDeployment\n(Fast)"]
    fig, axes = frame(3.5, "Historical runs; conditions differ; n = 1 per version")
    latency_bars(axes[0], [r["elapsed_s"] for r in rows], labels, "(a) Recorded latency", 84)
    stacked_tokens(axes[1], [r["input_tokens"] for r in rows], [r["output_tokens"] for r in rows],
                   labels, "(b) Recorded token usage", limit=23)
    handles, names = axes[1].get_legend_handles_labels()
    fig.legend(handles, names, loc="upper right", bbox_to_anchor=(0.98, 0.92), frameon=False, ncol=2)
    fig.text(0.5, 0.025, "Not a controlled three-way ablation. Measurements: 2026-10-03.", ha="center")
    save(fig, directory, "fig02_architecture_history")


def packaging_figure(data, directory):
    rows = data["packaging"]["rows"]
    labels = ["Missing\nCOPY", "COPY\npresent", "Ignored\nfile"]
    fig, axes = frame(3.4, "Deployment extension; Fast tier; one run per case")
    latency_bars(axes[0], [r["elapsed_ms"] / 1000 for r in rows], labels,
                 "(a) Recorded latency", 23)
    axes[0].axhline(10, color=INK, linestyle=":", linewidth=1)
    axes[0].text(-0.27, 10.7, "10 s target", ha="left")
    stacked_tokens(axes[1], [r["tokens"]["input"] for r in rows],
                   [r["tokens"]["output"] for r in rows], labels, "(b) Token usage", limit=12)
    handles, names = axes[1].get_legend_handles_labels()
    fig.legend(handles, names, loc="upper right", bbox_to_anchor=(0.98, 0.92), frameon=False, ncol=2)
    fig.text(0.5, 0.025, "Compact accepted: 1/3. Status match: 2/3. Under 10 s: 1/3.", ha="center")
    save(fig, directory, "fig03_deployment_cases")


def forest(ax, metrics, labels, color=BLUE):
    y = np.arange(len(metrics))
    values = np.array([r["rate"] * 100 for r in metrics])
    bounds = np.array([r["wilson_95"] for r in metrics]) * 100
    err = np.maximum(0, np.vstack((values - bounds[:, 0], bounds[:, 1] - values)))
    ax.errorbar(values, y, xerr=err, fmt="o", color=color, ecolor=INK,
                capsize=3, markersize=4, linewidth=0.85)
    ax.set_yticks(y, labels)
    for pos, row in zip(y, metrics):
        ax.text(108, pos, f"{row['numerator']}/{row['denominator']}", va="center", fontsize=9)
    ax.set_xlim(-3, 125)
    ax.set_xticks([0, 25, 50, 75, 100])
    ax.set_ylim(len(metrics) - 0.5, -0.65)
    ax.spines["left"].set_visible(False)
    ax.spines["bottom"].set_bounds(0, 100)
    ax.tick_params(axis="y", length=0)
    ax.set_xlabel("Observed proportion (%)")
    clean(ax, "x")


def synthetic_figure(data, directory):
    metrics = data["synthetic"]["metrics"]
    fig, axes = frame(3.25, "Historical log/source evaluation with shadow; 20 synthetic cases")
    fig.subplots_adjust(left=0.16, right=0.98, bottom=0.23, top=0.77, wspace=0.7)
    forest(axes[0], [metrics[k] for k in ("status_accuracy", "root_cause_accuracy", "critical_evidence_hit_rate", "source_location_hit_rate")],
           ["Status match", "Cause rubric", "Evidence hit", "Source location"])
    forest(axes[1], [metrics[k] for k in ("healthy_false_positive_rate", "insufficient_overclaim_rate")],
           ["False positive", "Overclaim"], color=INK)
    axes[0].set_title("(a) Success metrics", pad=10)
    axes[1].set_title("(b) Error metrics", pad=10)
    fig.text(0.5, 0.025, "Bars: descriptive Wilson 95% intervals; curated cases are not a random incident sample.", ha="center")
    save(fig, directory, "fig04_synthetic_quality")


def relation_figure(data, directory):
    arms = data["relation_ablation"]["arms"]
    fig, axes = frame(3.4, "Earlier relation-assist ablation; 60 authored cases per arm")
    fig.subplots_adjust(left=0.1, wspace=0.4)
    forest(axes[0], [arms[a]["metrics"]["root_cause_accuracy"] for a in ARMS], ARM_LABELS)
    axes[0].set_title("(a) Frozen cause rubric (n = 24)", pad=10)
    stacked_tokens(axes[1], [arms[a]["tokens"]["input"] for a in ARMS],
                   [arms[a]["tokens"]["output"] for a in ARMS],
                   ["Baseline", "Facts", "Rules*"], "(b) Confirmed usage across 60 cases", limit=700)
    handles, labels = axes[1].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper right", bbox_to_anchor=(0.98, 0.92), frameon=False, ncol=2)
    fig.text(0.5, 0.063, "*Rules: one execution error; its unconfirmed usage is excluded from token totals.", ha="center")
    fig.text(0.5, 0.018, "Wilson intervals are descriptive. Frozen wording checks are not semantic accuracy.", ha="center")
    save(fig, directory, "fig05_relation_ablation")


def graph_figure(data, directory):
    graphs = data["graph_microbenchmarks"]
    fig, axes = frame(3.65, "Local graph microbenchmarks; no model or network latency")
    fig.subplots_adjust(bottom=0.25, top=0.73, wspace=0.36)
    x = np.arange(2)
    configurations = [
        ("Before (n=10)", [r["before"]["median_ms"] for r in graphs["shadow"]], GRAY, "///"),
        ("After cold (n=1)", [r["after"]["cold_ms"][0] for r in graphs["shadow"]], "white", "..."),
        ("After warm (n=9)", [r["after"]["warm_median_ms"] for r in graphs["shadow"]], BLUE, ""),
    ]
    for j, (label, values, color, hatch) in enumerate(configurations):
        bars = axes[0].bar(x + (j - 1) * 0.25, values, width=0.22, color=color,
                          edgecolor=INK, hatch=hatch, label=label, zorder=3)
        axes[0].bar_label(bars, labels=[f"{v:.1f}" for v in values], fontsize=9, padding=3, rotation=90)
    axes[0].set_xticks(x, ["Build error\n249 triples", "Config error\n281 triples"])
    axes[0].set_ylim(0, 930)
    axes[0].set_ylabel("Graph processing time (ms)")
    axes[0].set_title("(a) Shadow pipeline optimization", pad=10)
    axes[0].legend(loc="upper left", bbox_to_anchor=(-0.08, 1.47), frameon=False,
                   ncol=1, labelspacing=0.15, handlelength=1.5)
    for j, (key, label, color, hatch) in enumerate([
        ("p50_ms", "p50", GRAY, "///"), ("p95_ms", "p95", BLUE, "")
    ]):
        values = [r["sequential"][key] for r in graphs["reasoning"]]
        bars = axes[1].bar(x + (j - 0.5) * 0.3, values, width=0.27, color=color,
                          hatch=hatch, edgecolor=INK, label=label, zorder=3)
        axes[1].bar_label(bars, labels=[f"{v:.2f}" for v in values], fontsize=9, padding=3)
    axes[1].set_xticks(x, ["49 triples", "294 triples"])
    axes[1].set_ylim(0, 35)
    axes[1].set_ylabel("Inference time (ms)")
    axes[1].set_title("(b) Rule worker, 40 warm runs", pad=10)
    axes[1].legend(loc="upper center", bbox_to_anchor=(0.5, 1.36), ncol=2, frameon=False)
    for ax in axes:
        clean(ax)
    fig.text(0.5, 0.065, "(a) Cold includes process start; warm reuses a worker. (b) Graph sizes and revisions differ.", ha="center")
    fig.text(0.5, 0.023, "These measurements are separate from the graph_compact online pipeline.", ha="center")
    save(fig, directory, "fig06_graph_overhead")


def write_tables(data, directory, groups):
    rows = []
    for name, modes in groups.items():
        for mode, metrics in modes.items():
            rows.append({"subset": name, "mode": mode, **metrics})
    with (directory / "paired_summary.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    flat = []
    for experiment in ("paired", "packaging"):
        for row in data[experiment]["rows"]:
            flat.append({"experiment": experiment, "case": row["case"], "mode": row["mode"],
                         "elapsed_ms": row["elapsed_ms"], "input_tokens": row["tokens"]["input"],
                         "output_tokens": row["tokens"]["output"], "total_tokens": totals(row),
                         "provider_calls": row["provider_calls"], "status_match": row["status_matches"]})
    with (directory / "case_metrics.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(flat[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(flat)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=ROOT / "evaluation/paper_metrics.json")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "docs/figures/evaluation")
    args = parser.parse_args()
    data = json.loads(args.data.read_text())
    groups = validate(data)
    font = configure_style()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for renderer in (paired_figure, history_figure, packaging_figure, synthetic_figure, relation_figure, graph_figure):
        renderer(data, args.output_dir)
    write_tables(data, args.output_dir, groups)
    manifest = {
        "data_sha256": hashlib.sha256(args.data.read_bytes()).hexdigest(),
        "matplotlib": matplotlib.__version__, "numpy": np.__version__,
        "font": font, "width_inches": WIDTH, "png_dpi": 900,
        "files": {name: hashlib.sha256((args.output_dir / name).read_bytes()).hexdigest() for name in FILES},
        "validation": "Recomputed paired aggregates and token totals match recorded summaries.",
    }
    (args.output_dir / "render_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Created 6 figures in PNG/PDF/SVG, 2 CSV tables and a manifest. Font: {font}.")


if __name__ == "__main__":
    main()
