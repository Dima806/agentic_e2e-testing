"""Build the figures and numbers from the recorded demo run.

Reads examples/demo-results/ (report.json, trace.jsonl, screenshots) and features/refund.feature,
writes PNG figures and numbers.json to docs/article/. Every number the article quotes is in
numbers.json, so prose and figures cannot drift from the committed run.

    make article-figures      # uv run --with matplotlib python scripts/article_figures.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.image as mpimg
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, Patch

from agentic_e2e.features import load_feature

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "examples" / "demo-results" / "refund"
OUT = ROOT / "docs" / "article"
FIGURES = OUT / "figures"

SHORT = [
    "Open the dashboard",
    "Open Payments",
    "Hover row, click Refund",
    "Check refund page",
    "Submit the refund",
    "Check status page",
    "Check refunds table",
]
CODE, MODEL, BROWSER, OUTPUT = "#4c72b0", "#dd8452", "#8c8c8c", "#55a868"
LIGHT_CODE, LIGHT_MODEL = "#c7d3e8", "#f3cfb3"
DPI = 150

# Claude Opus 5.5 list prices, USD per million tokens (cache write = 1.25x input, 5-minute TTL).
PRICES = {"input": 4.00, "output": 20.00, "cache_write": 5.00, "cache_read": 0.20}


def load() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    report = json.loads((RUN / "report.json").read_text())
    trace = [json.loads(line) for line in (RUN / "trace.jsonl").read_text().splitlines()]
    return report, trace


def per_step(report: dict[str, Any], trace: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for step in report["scenarios"][0]["steps"]:
        events = [e for e in trace if e.get("step") == step["index"]]
        executor = step["tokens_by_role"]["executor"]
        evaluator = step["tokens_by_role"]["evaluator"]
        rows.append(
            {
                "index": step["index"],
                "label": SHORT[step["index"]],
                "text": f"{step['keyword']} {step['text']}",
                "status": step["status"],
                "attempts": step["attempts"],
                "duration_s": step["duration_s"],
                "judge": "evaluator" if evaluator["prompt"] else "code",
                "actions": [e["tool"] for e in events if e["event"] == "tool_call"],
                "model_calls": {
                    "executor": sum(
                        e["event"] == "llm_response" and e["role"] == "executor" for e in events
                    ),
                    "evaluator": sum(
                        e["event"] == "llm_response" and e["role"] == "evaluator" for e in events
                    ),
                },
                "executor": executor,
                "evaluator": evaluator,
                "screenshot": step["screenshots"][-1],
            }
        )
    return rows


def numbers(report: dict[str, Any], steps: list[dict[str, Any]]) -> dict[str, Any]:
    tokens = report["totals"]["tokens"]
    uncached = tokens["prompt"] - tokens["cache_read"] - tokens["cache_write"]
    by_role = {
        role: {
            key: sum(s[role][key] for s in steps)
            for key in ("prompt", "completion", "cache_read", "cache_write")
        }
        for role in ("executor", "evaluator")
    }
    cost = (
        uncached * PRICES["input"]
        + tokens["cache_write"] * PRICES["cache_write"]
        + tokens["cache_read"] * PRICES["cache_read"]
        + tokens["completion"] * PRICES["output"]
    ) / 1_000_000
    assert sum(r["prompt"] for r in by_role.values()) == tokens["prompt"]
    return {
        "source": "examples/demo-results/refund (report.json, trace.jsonl)",
        "started_at": report["started_at"],
        "models": report["models"],
        "status": report["status"],
        "steps_passed": report["totals"]["steps_passed"],
        "steps_total": report["totals"]["steps_total"],
        "retries": sum(s["attempts"] - 1 for s in steps),
        "duration_s": report["duration_s"],
        "model_calls": {
            role: sum(s["model_calls"][role] for s in steps) for role in ("executor", "evaluator")
        },
        "browser_actions": [a for s in steps for a in s["actions"]],
        "steps_without_browser_action": [s["index"] + 1 for s in steps if not s["actions"]],
        "judged_by_code": [s["index"] + 1 for s in steps if s["judge"] == "code"],
        "judged_by_evaluator": [s["index"] + 1 for s in steps if s["judge"] == "evaluator"],
        "tokens": {**tokens, "uncached_prompt": uncached},
        "cache_read_share_of_prompt": round(tokens["cache_read"] / tokens["prompt"], 3),
        "tokens_by_role": by_role,
        "evaluator_cache_reads": by_role["evaluator"]["cache_read"],
        "heaviest_step": max(steps, key=lambda s: s["executor"]["prompt"])["index"] + 1,
        "step_durations_s": [s["duration_s"] for s in steps],
        "estimated_cost_usd": round(cost, 3),
        "price_assumptions_usd_per_mtok": PRICES,
    }


def box(ax: Any, x: float, y: float, w: float, h: float, text: str, color: str) -> None:
    ax.add_patch(
        FancyBboxPatch(
            (x, y), w, h, boxstyle="round,pad=0.02,rounding_size=0.08", fc=color, ec="none"
        )
    )
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center", fontsize=10.5, color="white")


def arrow(ax: Any, start: tuple[float, float], end: tuple[float, float]) -> None:
    ax.annotate(
        "", xy=end, xytext=start, arrowprops={"arrowstyle": "-|>", "color": "#444", "lw": 1.4}
    )


def fig_pipeline() -> None:
    fig, ax = plt.subplots(figsize=(12, 5.0))
    ax.set_xlim(0, 12)
    ax.set_ylim(-0.5, 4.6)
    ax.axis("off")
    ax.add_patch(
        FancyBboxPatch(
            (4.55, 0.35),
            4.9,
            3.75,
            boxstyle="round,pad=0.02,rounding_size=0.15",
            fc="#fbf1e8",
            ec=MODEL,
            lw=1.4,
            ls="--",
        )
    )
    ax.text(
        7.0,
        3.85,
        "per-step loop: the only agentic part",
        ha="center",
        fontsize=10.5,
        color="#8a4b1c",
    )
    box(ax, 0.2, 1.75, 1.8, 1.1, ".feature file\n(plain Gherkin)", CODE)
    box(ax, 2.45, 1.75, 1.8, 1.1, "validate + parse\n(pytest-bdd)", CODE)
    box(ax, 4.8, 2.35, 2.0, 1.1, "executor\n(Claude)", MODEL)
    box(ax, 7.2, 2.35, 2.0, 1.1, "Playwright MCP\n+ Chromium", BROWSER)
    box(ax, 4.8, 0.65, 2.0, 1.1, "evaluator\n(Claude)", MODEL)
    box(ax, 7.2, 0.65, 2.0, 1.1, "assertion engine\n(Python, exact)", CODE)
    box(ax, 9.9, 1.75, 1.9, 1.1, "report.json\ntrace.jsonl\nscreenshots", CODE)
    arrow(ax, (2.0, 2.3), (2.45, 2.3))
    arrow(ax, (4.25, 2.3), (4.8, 2.9))
    arrow(ax, (6.8, 3.05), (7.2, 3.05))
    arrow(ax, (7.2, 2.75), (6.8, 2.75))
    arrow(ax, (5.8, 2.35), (5.8, 1.75))
    arrow(ax, (8.2, 2.35), (8.2, 1.75))
    arrow(ax, (9.45, 2.3), (9.9, 2.3))
    ax.text(5.9, 1.95, "no expected data", fontsize=8.5, color="#555")
    ax.text(8.3, 1.95, "table or\nvalues", fontsize=8.5, color="#555", va="center")
    ax.legend(
        handles=[
            Patch(color=CODE, label="deterministic Python"),
            Patch(color=MODEL, label="model call"),
            Patch(color=BROWSER, label="real browser"),
        ],
        loc="lower center",
        bbox_to_anchor=(0.5, 0.0),
        frameon=False,
        fontsize=9.5,
        ncol=3,
    )
    save(fig, "01_pipeline.png")


def fig_contact_sheet(steps: list[dict[str, Any]], nums: dict[str, Any]) -> None:
    fig, axes = plt.subplots(2, 4, figsize=(15, 5.0))
    for ax, step in zip(axes.flat, steps, strict=False):
        image = mpimg.imread(RUN / step["screenshot"])
        ax.imshow(image[:300, :720])
        ax.set_xticks([])
        ax.set_yticks([])
        color = MODEL if step["judge"] == "evaluator" else CODE
        for spine in ax.spines.values():
            spine.set_edgecolor(color)
            spine.set_linewidth(2.5)
        ax.set_title(
            f"{step['index'] + 1}. {step['label']}\n"
            f"{step['status']}, judged by {step['judge']}, {step['duration_s']:.1f} s",
            fontsize=10,
        )
    last = axes.flat[-1]
    last.axis("off")
    calls = nums["model_calls"]
    last.text(
        0.05,
        0.5,
        f"{nums['steps_passed']} of {nums['steps_total']} steps passed\n"
        f"{nums['retries']} retries\n{nums['duration_s']:.1f} s in total\n"
        f"{calls['executor'] + calls['evaluator']} model calls\n"
        f"{len(nums['browser_actions'])} browser actions",
        fontsize=13,
        va="center",
        linespacing=1.7,
    )
    fig.tight_layout()
    save(fig, "02_steps_contact_sheet.png")


def fig_durations(steps: list[dict[str, Any]]) -> None:
    fig, ax = plt.subplots(figsize=(10, 4.4))
    labels = [f"{s['index'] + 1}. {s['label']}" for s in steps][::-1]
    values = [s["duration_s"] for s in steps][::-1]
    colors = [MODEL if s["judge"] == "evaluator" else CODE for s in steps][::-1]
    bars = ax.barh(labels, values, color=colors)
    for bar, step in zip(bars, steps[::-1], strict=True):
        actions = ", ".join(step["actions"]) or "no browser action"
        ax.text(
            bar.get_width() + 0.2,
            bar.get_y() + bar.get_height() / 2,
            f"{step['duration_s']:.1f} s  ({actions})",
            va="center",
            fontsize=9,
        )
    ax.set_xlim(0, max(values) * 1.6)
    ax.set_xlabel("seconds per step (executor + judge + screenshot)")
    ax.spines[["top", "right"]].set_visible(False)
    ax.legend(
        handles=[
            Patch(color=MODEL, label="judged by the evaluator (model)"),
            Patch(color=CODE, label="judged by code (exact comparison)"),
        ],
        loc="lower right",
        frameon=False,
        fontsize=9,
    )
    fig.tight_layout()
    save(fig, "03_step_durations_by_judge.png")


def fig_table_check(trace: list[dict[str, Any]]) -> None:
    feature = load_feature(ROOT / "features" / "refund.feature")
    expected = feature.scenarios[0].steps[-1].table
    assert expected is not None
    actual = next(e["rows"] for e in trace if e["event"] == "read_table")
    header = expected[0]
    verdicts = []
    for want, got in zip(expected[1], actual[1], strict=True):
        placeholder = want.startswith("<") and want.endswith(">")
        if placeholder:
            verdicts.append("placeholder:\nany non-empty" if got.strip() else "MISMATCH")
        else:
            verdicts.append("exact match" if want == got.strip() else "MISMATCH")
    cells = [expected[1], actual[1], verdicts]
    fig, ax = plt.subplots(figsize=(10, 2.6))
    ax.axis("off")
    table = ax.table(
        cellText=cells,
        colLabels=header,
        rowLabels=["expected (.feature)", "read from page (code)", "comparison"],
        loc="center",
        cellLoc="center",
    )
    table.auto_set_font_size(False)
    table.set_fontsize(11)
    table.scale(1, 2.3)
    for (row, col), cell in table.get_celld().items():
        cell.set_edgecolor("#bbbbbb")
        if row == 0:
            cell.set_text_props(weight="bold")
        if row == 3 and col >= 0:
            placeholder = verdicts[col].startswith("placeholder")
            cell.set_facecolor(LIGHT_MODEL if placeholder else "#cfe8d5")
    save(fig, "04_table_check.png")


def fig_tokens(steps: list[dict[str, Any]]) -> None:
    fig, ax = plt.subplots(figsize=(10, 4.6))
    labels = [f"{s['index'] + 1}. {s['label']}" for s in steps][::-1]
    series = [
        ("executor: read from cache", lambda s: s["executor"]["cache_read"], CODE),
        ("executor: written to cache", lambda s: s["executor"]["cache_write"], LIGHT_CODE),
        ("evaluator: written to cache", lambda s: s["evaluator"]["cache_write"], MODEL),
        (
            "not cached",
            lambda s: sum(
                s[r]["prompt"] - s[r]["cache_read"] - s[r]["cache_write"]
                for r in ("executor", "evaluator")
            ),
            BROWSER,
        ),
    ]
    left = [0] * len(steps)
    for name, value, color in series:
        widths = [value(s) for s in steps][::-1]
        ax.barh(labels, widths, left=left, color=color, label=name)
        left = [a + b for a, b in zip(left, widths, strict=True)]
    for y, step in enumerate(steps[::-1]):
        prompt = step["executor"]["prompt"] + step["evaluator"]["prompt"]
        out = step["executor"]["completion"] + step["evaluator"]["completion"]
        ax.text(left[y] + 150, y, f"{prompt:,} in / {out:,} out", va="center", fontsize=9)
    ax.set_xlim(0, max(left) * 1.35)
    ax.set_xlabel("prompt tokens per step")
    ax.xaxis.set_major_formatter(lambda value, _: f"{value:,.0f}")
    ax.spines[["top", "right"]].set_visible(False)
    ax.legend(loc="lower right", frameon=False, fontsize=9)
    fig.tight_layout()
    save(fig, "05_tokens_per_step.png")


def save(fig: Any, name: str) -> None:
    fig.savefig(FIGURES / name, dpi=DPI, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"wrote {(FIGURES / name).relative_to(ROOT)}")


def main() -> None:
    FIGURES.mkdir(parents=True, exist_ok=True)
    report, trace = load()
    steps = per_step(report, trace)
    nums = numbers(report, steps)
    (OUT / "numbers.json").write_text(json.dumps(nums, indent=2) + "\n")
    print(f"wrote {(OUT / 'numbers.json').relative_to(ROOT)}")
    fig_pipeline()
    fig_contact_sheet(steps, nums)
    fig_durations(steps)
    fig_table_check(trace)
    fig_tokens(steps)


if __name__ == "__main__":
    main()
