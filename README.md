# Agentic E2E Testing

Run your existing Gherkin `.feature` files as live browser end-to-end tests, with **no step definitions, page objects, selectors or waits**. The `.feature` file is the whole test.

For each step, an **executor** agent (Claude) works out the intent and acts on a real headless browser through the [Playwright MCP server](https://github.com/microsoft/playwright-mcp). An **evaluator** agent judges the result. Deterministic Python owns everything else: the loop, the retry policy, the assertions and the record. The model decides and drives; code holds the data.

> **Status:** early, but working end to end. The bundled demo passes all 7 steps against the real Claude API and a headless browser, and [its recorded results](examples/demo-results/) are in the repo. `make ci` passes: lint, strict typing, 239 unit tests and Gherkin validation. The CI workflow and devcontainer are not written yet.

## Why

End-to-end tests are the only layer that checks what a user actually does, and they are also the most fragile layer. Tests are coupled to UI structure rather than user intent, async timing causes flakes, test data drifts, and a failure report says "failed on step 9" with little else. BDD made scenarios readable, but the step definitions underneath still hold the selectors and waits, so you maintain two artifacts instead of one.

This framework drops the step definitions. A model reads each step as intent ("hovers over payment P-1001, finds the refund link, clicks it") and carries it out against the live page. Models are good at deciding what to do next and bad at reciting exact values, so asserted data never passes through the model: it only *locates* the element, and code reads the values and compares them exactly. `272.00` never becomes `272`.

## How it works

```
.feature ─► validate ─► parse ─► ┌── per-step loop (the only agentic part) ──┐ ─► artifacts
(Gherkin)   (pytest-bdd) (steps)  │ executor (acts) ─► Playwright MCP ─► Chromium │    report.json
                                  │ evaluator (judges) / assertion engine (code) │    trace.jsonl
                                  └──────────────────────────────────────────────┘    screenshots/
```

- **One step at a time.** The executor sees exactly one step and the current accessibility snapshot, never the whole scenario.
- **Intent-level tools only.** The executor can navigate, click, hover, type, select, press keys, search the page and wait for text. It addresses elements by snapshot ref (`e12`). CSS and XPath selectors are refused.
- **Code decides data checks.** A `Then` step with a table or specific values is judged by the assertion engine, never by the evaluator: exact strings, every mismatch reported.
- **Bounded, honest retries.** Each step gets at most 2 attempts. The first real failure ends the scenario, and every step after it is recorded as `SKIPPED`, never silently dropped.
- **Failures are classified.**
  - `defect`: the product is wrong. Not retried.
  - `execution`: a flaky attempt. Retried.
  - `infrastructure`: the API or browser is down. The feature is marked `blocked`, not failed.
- **Evidence for every step:** status, reason, a screenshot per attempt, a full trace of tool calls, and token counts.

## Quick start

You need Python 3.12+ with [uv](https://docs.astral.sh/uv/) (`pip install uv` if it's missing), Node.js with `npx`, and a Claude API key. The default GitHub Codespaces image has Python and Node.

```bash
make setup                                # Python deps + Chromium for the pinned Playwright MCP (uses sudo for OS packages)
export ANTHROPIC_API_KEY=sk-ant-...       # from https://console.anthropic.com/settings/keys
```

In GitHub Codespaces, store the key as a [Codespaces secret](https://docs.github.com/en/codespaces/managing-your-codespaces/managing-your-account-specific-secrets-for-github-codespaces) named `ANTHROPIC_API_KEY`. **A secret added while the codespace is running does not reach terminals that are already open:** stop and restart the codespace, then check with `echo ${ANTHROPIC_API_KEY:+set}`. Never commit a key.

Run the bundled example, the refund flow from the design doc, in one command:

```bash
make demo           # serves examples/demo-site, runs features/refund.feature, writes examples/demo-results/
```

Or run the steps yourself:

```bash
make demo-site                            # terminal 1: serves examples/demo-site on http://127.0.0.1:8765/
make run FEATURES=features/refund.feature BASE_URL=http://127.0.0.1:8765/   # terminal 2
make summarize                            # suite summary from artifacts/
```

Before it starts a browser, `run` checks that your credentials work and that the configured models are available. The check spends no tokens. Without a key it stops in a couple of seconds with instructions and writes a `blocked` report.

### Recorded demo run

[`examples/demo-results/`](examples/demo-results/) holds a real run of the refund feature:

- **Result:** 7/7 steps passed on the first attempt in 59 s.
- **Tokens:** 48,859 prompt (71% read from cache) and 1,245 completion.
- **Files:**
  - [`refund/report.json`](examples/demo-results/refund/report.json): per-step results;
  - [`refund/trace.jsonl`](examples/demo-results/refund/trace.jsonl): every model call, browser action and verdict;
  - [`suite-summary.json`](examples/demo-results/suite-summary.json) and its readable rendering, [`suite-summary.md`](examples/demo-results/suite-summary.md);
  - one PNG screenshot per step in [`refund/screenshots/`](examples/demo-results/refund/screenshots/).

The last step's table was checked in code against `272.00` exactly. The reference `R-6931` is generated fresh on every run, so the expected table's `<ref>` placeholder matched it.

![Final step: the refund status table](examples/demo-results/refund/screenshots/s01-st06-a1.png)

## Writing features

```gherkin
Feature: Refund a payment

  Scenario: A support agent refunds a successful payment
    Given a user is on the dashboard page
    When the user opens the Payments section
    And the user hovers over payment P-1001, finds the refund link, clicks it to open the refund page
    Then the refund page for payment "P-1001" should be visible
    When the user submits the refund request
    Then the refund status page should be visible
    Then the refunds table should be shown with following values
      """
      | Status     | Payment | Reference | Amount |
      | Successful | P-1001  | <ref>     | 272.00 |
      """
```

- **Describe intent, not implementation.** No selectors, waits or element IDs. A unit test checks the files in `features/` for them.
- **Pages:** a `Given` step that names a page ("the dashboard page") is resolved against `--base-url`. Full URLs work too.
- **What code checks:** a `Then` step is checked in code when it carries any of these:
  - a table: a native Gherkin data table, or a pipe table inside `"""`;
  - a double-quoted value (`"P-1001"`);
  - a standalone number (`272.00`, `1,040.00`);
  - a plain `"""` text block.

  Other `Then` steps ("should be visible") are judged by the evaluator.
- **Exact values.** Cells and values are compared as strings after trimming surrounding whitespace. Bare numbers in a `Then` step must appear on screen as written, so `3 items` is checked as on-screen text, not as a count.
- **Dynamic values:** an expected table cell written as `<name>` (like `<ref>` above) matches any non-empty value.
- **Scenario Outlines** are expanded, one scenario per `Examples` row. `Background` steps run before every scenario.
- **Input data:** a table or text block on a `Given`/`When` step is handed to the executor as input.
- **Secrets:** traces record typed text, so use test-only credentials in features.

Check files without a browser or model:

```bash
make validate FEATURES=features/          # or: uv run agentic-e2e validate features/ --json
```

## Results

Each feature writes `artifacts/<feature>/`:

| Path | Contents |
|---|---|
| `report.json` | Per step: status (`PASSED`/`FAILED`/`SKIPPED`), attempts, failure kind, reason, table mismatches, screenshots, tokens (total and per agent) and duration. Written even when a run is blocked or crashes. |
| `trace.jsonl` | Every model response, tool call, tool result, read and verdict. It is newline-delimited JSON, one object per line, streamed as it happens so a crash loses nothing. |
| `snapshots/*.json` | Long page snapshots and tool results referenced from the trace (`{"$ref": ...}`). |
| `screenshots/*.png` | `s01-st03-a1.png`: scenario 1, step 3, attempt 1. Taken after every attempt and checked to be real PNG files. |
| `mcp-server-log.json` | Browser-server error output. Only present if the server printed anything. |

Results are always JSON and visuals always PNG. The browser server's own side files (page `.yml` snapshots, console logs) go to a temporary directory that is deleted after each scenario.

`agentic-e2e summarize` aggregates these into `suite-summary.md` and `suite-summary.json`. The summary shows features passed, failed, blocked and missing a report, steps passed (such as `47/47`), and tokens (prompt, completion, cached share of the prompt, reasoning) with durations. It is ready for a CI job summary.

Exit codes for `run` and `summarize`: `0` all passed, `1` a feature failed, `2` a feature was blocked, a report is missing, or input was invalid.

## Configuration

Command-line options: `run PATH... [--out DIR] [--base-url URL] [--headed]`. Everything else is an environment variable:

| Variable | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | none | Claude API key; required for `run` |
| `AGENTIC_E2E_BASE_URL` | none | Application under test (same as `--base-url`) |
| `AGENTIC_E2E_MAX_ATTEMPTS` | `2` | Attempts per step, including the first |
| `AGENTIC_E2E_MAX_TOOL_CALLS_PER_ATTEMPT` | `15` | Browser actions the executor may take per attempt |
| `AGENTIC_E2E_EXECUTOR_MODEL` / `AGENTIC_E2E_EVALUATOR_MODEL` | `claude-opus-5-5` | Model per agent |
| `AGENTIC_E2E_EXECUTOR_EFFORT` / `AGENTIC_E2E_EVALUATOR_EFFORT` | `medium` | `low`, `medium`, `high`, `xhigh` or `max` |
| `AGENTIC_E2E_MAX_TOKENS` | `16000` | Output cap per model response |
| `AGENTIC_E2E_REFUSAL_FALLBACKS` | `true` | Server-side refusal fallbacks; set `false` on Bedrock, Vertex or Foundry |
| `AGENTIC_E2E_HEADLESS` | `true` | `false` (or `--headed`) shows the browser |
| `AGENTIC_E2E_BROWSER_CALL_TIMEOUT_S` | `60` | Timeout per browser call |
| `AGENTIC_E2E_PLAYWRIGHT_MCP_VERSION` | `0.0.83` | Pinned `@playwright/mcp`. With make, set `PLAYWRIGHT_MCP_VERSION=` instead, then rerun `make browser` |

Runs are sequential, with one browser at a time, sized for a 2-CPU Codespace.

## Development

```bash
make ci             # what CI runs: locked install, ruff, format check, mypy --strict, unit tests, validate
make test           # unit tests only: no network, browser or API key
make test-browser   # real Playwright MCP + Chromium against the demo site; no API key
make test-live      # the whole pipeline against the demo site; spends tokens
make demo           # the recorded demo: refreshes examples/demo-results/ (DEMO_OUT=... to write elsewhere)
make article-figures  # rebuild the article's charts and numbers from examples/demo-results/
make help           # every target
```

The code lives in `src/agentic_e2e/`, with tests in `tests/`. Only `src/agentic_e2e/browser/` may talk to the MCP server; ruff and an architecture test enforce that. [CLAUDE.md](CLAUDE.md) is the design spec: invariants, module map, report schema and the decisions taken where the original PRD was ambiguous. Read it before changing behaviour. Claude Code loads it automatically.

**Not done yet:** the GitHub Actions suite workflow (one job per feature, at most 2 in parallel, then a summary job) and a `.devcontainer`. Only Chromium is supported.

## Article

[Your End-to-End Tests Do Not Need Step Definitions](docs/article/your-end-to-end-tests-do-not-need-step-definitions.md) walks through the recorded demo run. Its charts are PNGs in `docs/article/figures/`, and every number it quotes is in `docs/article/numbers.json`. `scripts/article_figures.py` generates both from `examples/demo-results/`.

## Related projects

Other projects of mine on [GitHub](https://github.com/Dima806) that touch the same themes: agents, verifying model output, and retrieval.

- [llm_judge_benchmark](https://github.com/Dima806/llm_judge_benchmark): a systematic comparison of local LLM judges against human annotation on the same RAG answers. It asks the question behind this repo's evaluator: who checks the model's work?
- [financial_research_agent](https://github.com/Dima806/financial_research_agent): a LangGraph-based multi-agent deep-research system for financial document analysis.
- [energy_grid_research_agent](https://github.com/Dima806/energy_grid_research_agent): a multi-framework agentic RAG system for analysing industrial technical documents.
- [graphrag_vs_vectorrag](https://github.com/Dima806/graphrag_vs_vectorrag): a side-by-side comparison of graph RAG and vector RAG.
- [retrieval_arena](https://github.com/Dima806/retrieval_arena): a head-to-head comparison of TF-IDF, BM25, dense embeddings, hybrid rank fusion and cross-encoder reranking on several corpora.
- [secure_doc_chat_example](https://github.com/Dima806/secure_doc_chat_example): fully offline RAG chat over your own documents.
- [model_drift_detector](https://github.com/Dima806/model_drift_detector): drift detection for machine-learning models in production.

## License

[Apache License 2.0](LICENSE)
