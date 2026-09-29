# CLAUDE.md

Agentic end-to-end testing framework: runs existing Gherkin `.feature` files as live browser E2E tests with **no step definitions, page objects, selectors, or waits**. An executor agent performs each step against a headless browser through a Playwright MCP server; an evaluator agent judges the result; deterministic Python owns the loop, the assertions, and the record.

- **Spec source:** PRD A in `.llm/agentic-e2e-testing-prd.md`. That file is gitignored, so this document is the committed spec. PRD B (guardrails) and PRD C (LLM wiki) in the same file are **out of scope** for this repo.
- **Status:** PRD build tasks 1–10 and 12 are implemented, and `make ci` passes. The browser layer is verified against the real Playwright MCP server and Chromium (`make test-browser`). The full model-driven run (`make test-live`) needs Claude API credentials and has not been run in this repo yet. Still missing: the CI workflow (task 11) and `.devcontainer/devcontainer.json`. When a command or path here stops matching reality, update this file in the same change.

## Non-negotiable invariants

These are the PRD acceptance criteria. Every change must preserve them, and each one has a unit test.

1. **The `.feature` file is the whole test.** Zero step-definition or glue code, ever. pytest-bdd is used only to parse; `test_architecture.py` fails if anything imports its step-definition APIs.
2. **Selectors, waits and MCP tool names live only in `browser/`.** Ruff's banned-api rule (`TID251` in `pyproject.toml`) blocks `mcp`, `time.sleep` and `asyncio.sleep` outside `browser/`. `test_architecture.py` also rejects `browser_*` tool names in string literals, sleeps and timed waits, and selector APIs anywhere else in `src/`. The executor's `wait_for_text` tool is intent-level: the waiting itself happens in `browser/client.py`.
3. **One step at a time.** The executor gets exactly one step per conversation. It never sees the whole scenario.
4. **Bounded retries.** Each step gets at most `max_attempts = 2` (the first try plus one retry), then stops. See [Decisions](#decisions-on-prd-ambiguities).
5. **Fail fast.** The first real (non-retryable) failure ends the scenario. The next scenario still runs.
6. **Nothing silently dropped.** Every step starts as `SKIPPED` in the report and only changes when it runs, so steps after a failure or a blocked feature are always recorded, with a reason.
7. **The model never carries asserted data.** Models locate on-screen elements. Code reads their values and compares them. Values are compared as exact strings: `272.00` never becomes `272` or `272.0`, and there is no `float()`, no rounding and no reformatting. The executor is shown only a table's header row, never the expected values.
8. **Every run leaves evidence:** per-step status, trace, token counts, screenshots and `report.json`. The suite also gets an aggregated summary.

## Architecture

```
.feature ─► validate ─► parse ─► ┌── per-step loop (the ONLY agentic part) ──┐ ─► artifacts
(Gherkin)   (pytest-bdd) (Steps)  │ executor (acts) ─► Browser MCP            │    report.json
                                  │ evaluator (judges) / assertion engine     │    trace.jsonl
                                  └───────────────────────────────────────────┘    screenshots/
```

Everything outside the loop box is plain, deterministic, unit-testable Python. The only outbound model calls are from `agents/executor.py` and `agents/evaluator.py`, through `agents/llm.py`.

### Layout

```
src/agentic_e2e/
  cli.py                 # `agentic-e2e validate|run|summarize`
  config.py              # frozen dataclass + AGENTIC_E2E_* env overrides
  models.py              # Step, Scenario, Feature, ExecutorResult/Run, Verdict, Mismatch, TokenUsage
  trace.py               # Trace protocol the agents and runner write events to
  features/loader.py     # discover .feature files, validate via pytest-bdd's parser
  features/parser.py     # -> ordered, typed Step objects; outline expansion; literals
  browser/base.py        # Browser protocol, ActionOutcome, BrowserError, ElementReadError
  browser/client.py      # BrowserClient + launch(): the ONLY home of MCP tool names, JS, waits
  agents/llm.py          # request shape, usage mapping, stop_reason handling, API errors
  agents/executor.py     # one step -> intent-level tool calls -> ExecutorRun
  agents/evaluator.py    # step + actions + post-action snapshot -> Verdict
  agents/prompts/*.md    # system prompts, byte-stable (prompt caching)
  assertions/engine.py   # deterministic table / literal comparison
  runner/loop.py         # FeatureRunner: the per-step loop controller
  artifacts/report.py    # report.json schema
  artifacts/writer.py    # streamed trace.jsonl, snapshots/, screenshots/, report.json
  artifacts/summary.py   # suite summary (JSON + Markdown for CI)
tests/fakes.py           # FakeBrowser, FakeMessages, scripted executor/evaluator
tests/unit/              # deterministic core; no network, browser or API
tests/integration/       # `browser` marker: real MCP + Chromium; `live` marker: real agents too
tests/fixtures/features/ # sample .feature files, including malformed ones
features/refund.feature  # the PRD example flow, runnable against the demo site
examples/demo-site/      # static app: hover-revealed refund link, async status table
```

`.gitignore` excludes directories named `agent/`, `.agents/`, `.llm/` and `.claude/skills/`. Don't put project code in any of them. The package directory is `agents/`, plural, and that is fine. Run output goes to `artifacts/`, which is also ignored.

## Feature loading and parsing

- **Validate early.** `load_feature` parses with pytest-bdd's `FeatureParser`, which uses the official Gherkin parser. Malformed Gherkin raises `FeatureValidationError` with file and line before any browser or model starts. Empty files, files without `Feature:`, features without scenarios, scenarios without steps and duplicate scenario names are rejected too. pytest-bdd keys scenarios by name, so without that check a duplicate would silently replace the first. `agentic-e2e validate` runs only this stage.
- **Step model:** `Step(index, line, keyword, effective_type, text, data_table, docstring, table, expected_literals)`.
  - `effective_type` is `context`, `action` or `outcome`. `And`/`But` inherit the type of the previous step.
  - `Background` steps (feature and rule level) are prepended to every scenario.
  - Each `Scenario Outline` `Examples` row becomes its own scenario, named `Name [#1: k=v]`. An outline without rows is rejected.
- **Tables.** Native data tables and a pipe table inside a `"""` docstring both become `table: list[list[str]]`. Cells are stripped and otherwise kept exactly. Gherkin escapes (`\|`, `\\`, `\n`) are honoured, and pytest-bdd's backslash doubling is undone. On an outcome step the table is expected data. On any other step it is input for the executor.
- **Expected literals** (outcome steps only): double-quoted strings, standalone numbers written as-is (`272.00`, `1,040.00`, `-5`; not the digits inside `P-1001`, `v2` or `2nd`), and a docstring that is not a table. Bare numbers count, so counts phrased as prose ("3 items") are checked as on-screen text too.
- `step.has_expected_data` is true when an outcome step has a table or literals. Those steps are judged by code, never by the evaluator.

## The per-step loop (`runner/loop.py`)

The loop is deterministic Python. This is the contract:

```python
for scenario in feature.scenarios:              # fresh browser session per scenario
    stopped = False
    for step in scenario.steps:
        if stopped:
            record(step, SKIPPED); continue
        for attempt in range(1, cfg.max_attempts + 1):
            result = await executor.run(step, browser)              # agentic
            if step.has_expected_data:                              # table or literals
                verdict = assertions.check(step, result, browser)   # code decides
            else:
                verdict = await evaluator.judge(step, result, await browser.snapshot())
            await artifacts.screenshot(step, attempt)
            if verdict.passed:
                record(step, PASSED, attempts=attempt); break
            if not verdict.retryable or attempt == cfg.max_attempts:
                record(step, FAILED, attempts=attempt, verdict=verdict); stopped = True; break
```

- **Code beats the model.** When the assertion engine produced a verdict, the evaluator is never called.
- **Failure kinds:**

  | `failure_kind` | Examples | Retryable? |
  |---|---|---|
  | `defect` | Wrong page or state; assertion mismatch on a rendered element | No |
  | `execution` | Timing or async hiccup; element not located, not rendered yet or unreadable; executor hit the tool-call cap; `refusal`, `max_tokens` or invalid JSON from a model | Yes |
  | `infrastructure` | API error after SDK retries; missing credentials; MCP server or browser crash; browser failed to start | No. The feature is marked `blocked`. |

- **Statuses.** Steps are `PASSED`, `FAILED` or `SKIPPED`, with `attempts` and `retried` fields. Scenarios are `passed`, `failed`, `blocked` or `skipped`. Features are `passed`, `failed` or `blocked`. A blocked feature still writes `report.json`. An unexpected exception (a bug) also blocks the feature as `internal error: …`, with the traceback in the trace.
- **Clean state.** Each scenario gets its own Playwright MCP server process and browser, so state never leaks between scenarios. Only one browser runs at a time.

## Agents (`agents/`)

**SDK:** the official `anthropic` Python SDK (1.x, `AsyncAnthropic`) with a hand-written tool loop over tools that `BrowserClient` exposes. Don't use the beta tool runner, `async_mcp_tool` or the API's `mcp_servers` connector. The loop controller needs every request, tool call and `usage` object in hand for the trace, per-step token accounting and the tool-call cap. Handing raw MCP tools to the model would also leak MCP tool names past the wrapper and break invariant 2.

**Request shape** (`AnthropicMessages.request`):
- Uses `client.beta.messages.create`, because server-side refusal fallbacks (`fallbacks="default"`, beta `server-side-fallback-2026-07-01`) are on by default. Set `AGENTIC_E2E_REFUSAL_FALLBACKS=false` on platforms without them (Bedrock, Vertex, Foundry).
- Structured output goes through `output_config.format` with `anthropic.transform_schema(PydanticModel)`, and the text is validated with Pydantic. Don't use `messages.parse(output_format=...)`: the SDK marks `output_format` deprecated, and it raises before `stop_reason` can be checked.
- `output_config.effort` is set explicitly. Top-level `cache_control` gives automatic prompt caching.
- The SDK signals missing credentials with a `TypeError` at request time. `llm.api_error` maps it, and all `anthropic` API errors, to an actionable `InfrastructureError`.
- **Preflight:** before any browser starts, `agentic-e2e run` calls `llm.preflight`, which runs `client.models.retrieve` for each configured model. That catches missing or rejected credentials and unavailable models without spending tokens. If it fails, every feature gets a blocked `report.json` with all steps `SKIPPED`, and the exit code is 2.

**Models:** configured in `config.py` and overridable by environment variable. Both default to `claude-opus-5-5`. Use exact model ID strings with no date suffix. Effort defaults to `medium` for both roles; tune it by measurement, not by guesswork.

**Executor** (`executor.py`)
- **Input:** the single step text, the step type, the current accessibility snapshot, the application base URL (`--base-url` / `AGENTIC_E2E_BASE_URL`), and a byte-stable system prompt from `prompts/executor.md`. For an outcome step with a table it gets only the header row. For other steps it gets any input table or docstring.
- **Tools:** `navigate`, `click`, `hover`, `type_text`, `select_option`, `press_key`, `go_back`, `find_text`, `wait_for_text` and `snapshot`. Arguments are Pydantic models turned into `strict: True` schemas. Elements are addressed by snapshot ref plus a plain-words description.
- **Cap:** at most `max_tool_calls_per_attempt` tool calls per attempt (default 15). Hitting the cap is an `execution` failure.
- **Output:** `ExecutorResult` with `completed: bool`, `notes: str` and `target_ref: str | None`. `target_ref` is the element holding asserted data. The executor never returns asserted values.
- After a mid-response refusal fallback, the declined model's `thinking`/`tool_use` blocks before the last `fallback` block are neither echoed back nor executed (`llm.echo_content`).

**Evaluator** (`evaluator.py`)
- **Input:** the step, the executor's report, its browser actions with one-line results, and the post-action snapshot. No tools.
- **Output:** `EvaluatorAnswer` with `outcome`, `retryable`, `failure_kind` and `reason`. The failure kind decides retryability: a `defect` never retries and an `execution` issue always may, whatever `retryable` says.
- **Scope:** it judges only steps without expected data. It never sees expected table values.

**Opus 5.5 API rules (a 400 or a silent bug if ignored)**
- Forced `tool_choice` (`any` or `tool`) returns 400, so the request leaves it unset (`auto`). `auto` may return no tool call; the executor then treats the text as its final answer.
- `temperature`, `top_p`, `budget_tokens` and `thinking: {type: "disabled"}` all return 400. Effort is the only control, which is one more reason determinism lives in code.
- No assistant prefill. Use structured outputs for format control.
- Always check `stop_reason` before reading `content` (`llm.stop_problem`). `refusal` and `max_tokens` are `execution` failures, never a pass.
- Within an attempt, keep the message history append-only. Append `response.content` unchanged, including thinking blocks, and never edit earlier turns.
- Tool inputs are validated from the SDK objects with Pydantic, never parsed by string matching.
- `max_tokens=16000` for these non-streaming calls.

**Prompt caching:** `TOOL_PARAMS` and the system prompts are built once and are byte-identical across calls. Don't add timestamps, run IDs or unsorted JSON to them. Cached share of the prompt is a reported metric, so a drop means something invalidated the prefix.

## Browser MCP client (`browser/client.py`)

- `launch(cfg, workdir)` starts `npx -y @playwright/mcp@<pin> --isolated --browser chromium --image-responses omit --output-dir <workdir>/mcp [--headless]` over stdio, using the `mcp` 2.x client (`stdio_client`, `StdioServerParameters`, `ClientSession`).
  - The pin is `PLAYWRIGHT_MCP_VERSION` in the `Makefile` (currently `0.0.83`), which must equal `Config.playwright_mcp_version`. A unit test checks this, and the Makefile exports the pin as `AGENTIC_E2E_PLAYWRIGHT_MCP_VERSION`.
  - Without `--browser chromium`, the server uses the Chrome channel, which isn't installed.
  - `make browser` installs Chromium with the Playwright CLI bundled in the pinned package, so the browser build matches.
- The server runs with the feature's artifact directory as its working directory. The server only writes files under its working directory and `--output-dir`, so this is what lets screenshots land in `screenshots/`. Its stderr goes to `mcp-server.log` there.
- On start, `launch` checks that every tool in `REQUIRED_TOOLS` exists, so a pin mismatch fails loudly as a `BrowserError`. It also unwraps the `ExceptionGroup`s the MCP client's task groups put around errors raised inside a session.
- In 0.0.83, tools take `target` (a snapshot ref such as `e12`, or `f2e7` inside a frame) plus an `element` description. `BrowserClient` refuses anything that isn't a ref, so the model can never drive the page by selector.
- Every action result is `OK` or `ERROR: …` followed by a fresh snapshot, with the server's code-generation sections stripped. MCP protocol errors and tool errors go back to the model as tool errors. Any other exception (dead process) is a `BrowserError`, which counts as infrastructure.
- **`read_table(ref)` / `read_element_text(ref)`** run fixed JavaScript through `browser_evaluate` and return `innerText` values. These values go to the assertion engine and never to a model. A missing or stale ref raises `ElementReadError`, which is retryable.
- **`screenshot(path)`** writes the PNG straight to disk. Screenshots are human evidence and are never sent to the model (`--image-responses omit`).

## Deterministic assertions (`assertions/engine.py`)

- **Tables:** compare header and rows cell by cell as trimmed strings. Report every mismatch with its row, column, expected value and actual value, not only the first one. Header order and column count must match. Row order matches too, unless an explicit option is added later.
- **Placeholders:** an expected cell that is an unrendered `<name>` matches any non-empty value, for dynamic IDs such as the PRD's `<ref>`. Outline parameters are rendered before this point.
- **Literals:** each expected literal must appear exactly (case-sensitive) in the located element's text. A numeric literal must stand alone: `272.00` is not found in `1272.00` or `272.001`.
- Never call `float()`, `int()` or `round()` on asserted values. A test checks this. If numeric semantics are ever needed, use `decimal.Decimal` **and** also require the string forms to match.
- A table or element that wasn't located, is empty, or isn't rendered yet is an `execution` failure and can be retried. Rendered-but-wrong values are a `defect` and can't be retried.

## Artifacts and `report.json`

Per feature, under `artifacts/<feature-slug>/`:

```
report.json            # written in `finally`: on pass, fail, blocked and crash paths
trace.jsonl            # one event per line, flushed immediately; context: scenario, step, attempt
snapshots/             # strings over 2000 chars from trace events, referenced as {"$ref": ...}
screenshots/s01-st03-a1.png   # scenario, step, attempt; taken after every attempt
mcp/, mcp-server.log   # Playwright MCP output directory and server stderr
.agentic-e2e           # marker: only directories carrying it are ever wiped for a rerun
```

The slug is the feature's path relative to the directory given to `run` (`checkout/refund.feature` becomes `checkout-refund`), or the file stem for a single file. Stream to disk as events happen, and never hold screenshots, snapshots or the full trace in memory, because the container has about 8 GB of RAM. `ArtifactWriter` refuses to overwrite a non-empty directory it didn't create.

`report.json` (`schema_version: 1`, schema in `artifacts/report.py`):

```json
{
  "schema_version": 1,
  "feature": {"path": "features/refund.feature", "name": "Refund a payment", "slug": "refund"},
  "status": "passed | failed | blocked",
  "error": null,
  "models": {"executor": "claude-opus-5-5", "evaluator": "claude-opus-5-5"},
  "started_at": "…", "duration_s": 0.0,
  "scenarios": [{
    "name": "…", "line": 4, "status": "passed | failed | blocked | skipped",
    "steps": [{
      "index": 0, "line": 5, "keyword": "Given", "text": "…",
      "status": "PASSED | FAILED | SKIPPED", "attempts": 1, "retried": false,
      "failure_kind": null, "reason": null,
      "mismatches": [], "screenshots": ["screenshots/s01-st00-a1.png"],
      "tokens": {"prompt": 0, "completion": 0, "cache_read": 0, "cache_write": 0, "reasoning": 0},
      "tokens_by_role": {"executor": {}, "evaluator": {}},
      "duration_s": 0.0
    }]
  }],
  "totals": {"steps_total": 0, "steps_passed": 0, "steps_failed": 0, "steps_skipped": 0, "tokens": {}}
}
```

### Token accounting (from `response.usage`, summed in code)

| Report field | Messages API source |
|---|---|
| `prompt` | `input_tokens + cache_read_input_tokens + cache_creation_input_tokens` (`input_tokens` alone is only the uncached part) |
| `completion` | `output_tokens`, which already includes thinking tokens |
| `cache_read` | `cache_read_input_tokens`. Cached % of prompt = `cache_read / prompt`. |
| `cache_write` | `cache_creation_input_tokens` |
| `reasoning` | `output_tokens_details.thinking_tokens`. `null` when any contributing response lacks the breakdown: an unknown part makes the sum unknown, and it is never estimated. |

Counts are aggregated per request, then per attempt, per step (and per role), per feature and per suite. Treat these numbers as a quality signal, not only a cost figure.

## CI (`.github/workflows/adk-agentic-e2e-suite.yaml`, not written yet)

- Trigger: `workflow_dispatch`.
- `discover` job: `agentic-e2e validate features/ --json` prints `[{"path", "slug"}]` for the matrix.
- `run` job: a matrix with one job per feature, `max-parallel: 2` and `fail-fast: false`. GitHub's matrix fail-fast is off so every feature reports; scenario-level fail-fast is separate and still on. Run `make browser` first, then upload `artifacts/<slug>/` as that job's artifact. `upload-artifact` skips hidden files, so the marker file won't travel, and that is fine. The job needs the `ANTHROPIC_API_KEY` secret.
- `summary` job (`if: always()`): download all artifacts into `artifacts/`, run `agentic-e2e summarize artifacts/ --features features/ --max-parallel 2 --matrix-result <result>`, and append `artifacts/suite-summary.md` to `$GITHUB_STEP_SUMMARY`. It shows:
  - features scheduled, max parallel jobs and matrix result;
  - features passed, failed and blocked (🟢/🔴/🟠 per feature);
  - features missing a report (⚪), meaning the job died before writing one;
  - total steps passed (for example `47/47`);
  - tokens: total, prompt, completion, cached % of prompt, and reasoning (`n/a` when null);
  - total duration.

## Target environment: 2-CPU GitHub Codespace

- Parallelism is 2 or less everywhere. `agentic-e2e run` executes features **sequentially**; in CI the matrix is capped at 2. Only one browser is open per process.
- The browser runs headless, and Chromium's OS dependencies are installed by `make browser` (`--with-deps`, uses sudo).
- `.devcontainer/devcontainer.json` (not written yet) must make a fresh Codespace ready to run: Python, Node (for `npx`) and uv, then `make setup` as the post-create command. No manual steps.
- Long runs stream artifacts to disk instead of accumulating them.

## Commands

The `Makefile` wraps everything, and `make help` lists the targets.

```bash
make setup                                # uv sync + Chromium for the pinned Playwright MCP
make ci                                   # locked install + lint + format-check + mypy --strict + unit tests + validate
make check                                # the same gates without the locked install and validation
make test PYTEST_ARGS="tests/unit/test_loop.py -k retry"   # single test
make test-browser                         # real Playwright MCP + Chromium against the demo site; no API
make test-live                            # full pipeline against the demo site; needs Claude API credentials
make format                               # ruff import sort + format
make validate FEATURES=features/          # Gherkin validation only
make demo-site                            # serve examples/demo-site on http://127.0.0.1:8765/
make run FEATURES=features/refund.feature BASE_URL=http://127.0.0.1:8765/   # with demo-site running
make summarize OUT=artifacts/             # suite summary JSON + Markdown
make lock                                 # after editing dependencies in pyproject.toml
```

`run` and `test-live` need Claude API credentials. The SDK reads `ANTHROPIC_API_KEY` (or `ANTHROPIC_AUTH_TOKEN`, or a profile from the separate `ant` CLI, which isn't installed in the Codespace). Use `export ANTHROPIC_API_KEY=...` for the current shell, or a Codespaces secret named `ANTHROPIC_API_KEY` for every terminal (it takes effect after the codespace restarts). Never commit a key: `.env` files are gitignored, but nothing here loads them.

The exit code of `run` and `summarize` is `0` when everything passed, `1` when any feature failed, and `2` when any feature was blocked, a report is missing, or the input was invalid.

## Testing

- The deterministic core is tested with fakes from `tests/fakes.py`:
  - `FakeBrowser`: canned snapshots, tables and texts.
  - `FakeMessages`: queued real `BetaMessage` objects, so the SDK types stay honest.
  - `ScriptedExecutor` and `ScriptedEvaluator`.

  Unit tests never touch the network, a browser or the API.
- Coverage (PRD task 12):
  - **Parser:** keyword inheritance, Background prepending, both table forms with escapes, literal extraction, outline expansion. **Loader:** every malformed-feature case, with file and line.
  - **Assertion engine:** `272.00` vs `272`, `272.0` and `272.00 ` (the trailing space is stripped, so that one passes); every mismatch reported; header, shape, missing-row and extra-row mismatches; placeholders; number-aware literal matching.
  - **Loop controller:**
    - a pass on attempt 2 records `retried: true`;
    - a retryable failure twice leads to `FAILED` with `attempts=2`;
    - a non-retryable failure stops immediately, and the steps after it are `SKIPPED` and never executed;
    - a code verdict overrides the evaluator;
    - infrastructure errors and browser start failures lead to a `blocked` feature with `report.json` still written;
    - there is a fresh browser per scenario;
    - token accounting is correct per role.
  - **Agents:** request shape (no `tool_choice`/`temperature`, strict tools, effort, fallbacks); tool errors; the tool-call cap; refusal and `max_tokens` handling; the executor never receiving expected values; fallback echo rules.
  - **Browser client:** parsing of real 0.0.83 responses, selector refusal, error mapping.
  - **Summary, writer, CLI and config**, including the Makefile/config pin check.
  - **Architecture test:** no `mcp` import, MCP tool names, sleeps/timed waits or selector APIs outside `browser/`; no step-definition imports; no numeric conversion in the assertion engine.
- Markers: `browser` (real MCP + Chromium, no API) and `live` (real agents, spends tokens) are deselected by default. They run against `examples/demo-site` served on a free local port by the `demo_site_url` fixture. Never run them against third-party sites.

## Conventions

- Python ≥ 3.12 (the container has 3.14), managed with `uv` through `pyproject.toml`, with a `src/` layout. Commit `uv.lock`. Use ruff for linting and formatting. Type-hint everything; `src/` must pass `mypy --strict` with the pydantic plugin.
- Dependencies have major-version upper bounds. `mcp` recently moved from 1.x to 2.x (snake_case fields such as `is_error`, `input_schema`). `anthropic` 1.x uses `httpx2`, not `httpx`. Check the changelog before raising a bound.
- Pydantic models for every structured boundary: `Step`, `ExecutorResult`, `Verdict`, the report schema.
- Use `asyncio` for the runner, agents and browser. Keep the parser, the assertions and the summary as pure sync functions.
- `ANTHROPIC_API_KEY` (or an `ant auth` profile) comes only from the environment. Never write it to artifacts or logs. Traces record typed text, so features must use test-only credentials.
- Keep the agentic surface minimal. If logic can be deterministic, it belongs in code, not in a prompt.

## Build order

Follow PRD section 8. Each step lands with its tests.

1. ~~Scaffold~~: done (`pyproject.toml`, `Makefile`, `config.py`, CLI). The devcontainer is still to do.
2. ~~Feature loader and validator~~
3. ~~Step parser~~
4. ~~`BrowserClient` MCP wrapper~~
5. ~~Executor agent~~
6. ~~Evaluator agent~~
7. ~~Deterministic assertion engine~~
8. ~~Per-step loop controller~~
9. ~~Artifact writer~~
10. ~~Suite summary~~
11. CI workflow: still to do (see [CI](#ci-githubworkflowsadk-agentic-e2e-suiteyaml-not-written-yet)).
12. ~~Unit-test coverage~~

## Decisions on PRD ambiguities

Change these deliberately, update this section, and adjust the tests.

- **Retry count.** The PRD says "max 2 attempts per step" in two places and "retries at most twice" in one. This repo uses **2 total attempts**. It is a single value, `max_attempts`, in `config.py`.
- **Models.** The PRD doesn't name models, so both roles default to `claude-opus-5-5`. They are separate config keys so the evaluator can change on its own.
- **Reasoning tokens.** Reported from `usage.output_tokens_details.thinking_tokens`, and `null` only when the API omits the breakdown. (An earlier draft said the API never reports them. The 1.x SDK does.)
- **Docstring tables.** They are treated like data tables, because that's how the PRD example is written.
- **Placeholders in expected tables.** `<ref>`-style cells match any non-empty value, because the PRD example asserts on dynamic IDs that way.
- **Bare numbers in `Then` steps** are exact on-screen literals. Quote a value to make intent explicit, and avoid numbers in prose that aren't on screen.
- **Refusal fallbacks** are on by default (`fallbacks="default"`); `AGENTIC_E2E_REFUSAL_FALLBACKS=false` turns them off.
- **Infrastructure failures.** They mark a feature `blocked`, not `failed`. They say nothing about the product and would otherwise feed the flake tax.
- **CI file name.** It stays `adk-agentic-e2e-suite.yaml`, as the PRD names it.
