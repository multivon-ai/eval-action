# multivon-ai/eval-action

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![multivon-eval](https://img.shields.io/badge/built%20on-multivon--eval-emerald)](https://github.com/multivon-ai/multivon-eval)
[![CI](https://github.com/multivon-ai/eval-action/actions/workflows/ci.yml/badge.svg)](https://github.com/multivon-ai/eval-action/actions/workflows/ci.yml)

[Engine: multivon-eval](https://github.com/multivon-ai/multivon-eval) · [Docs](https://docs.multivon.ai/guides/ci-cd) · Apache 2.0

> Run a [multivon-eval](https://github.com/multivon-ai/multivon-eval)
> suite on every pull request. Posts a PR comment with Wilson confidence
> intervals, McNemar p-values, cost in dollars, and an opinionated gate
> verdict.

The comment looks like this:

```markdown
## multivon-eval — ⚠️ FIX_THEN_MERGE
_toxicity regression (−18.0pp, p=0.012); cost 1.7× baseline_

Pass rate 85.4% · Cost $0.0345 · Cases 50 · Runs/case 3 · Δ vs baseline −4.6pp · Cost Δ 1.7×

### Per-evaluator
| Evaluator      | Pass rate (95% CI)   | Baseline | Δ        | p (McNemar) | Verdict        |
|---             |---                   |---       |---       |---          |---             |
| `faithfulness` | 90.0% [0.78–0.96]    | 95.0%    | −5.0pp   | 0.31        | noise          |
| `toxicity`     | 50.0% [0.32–0.68]    | 100.0%   | −50.0pp  | 0.001       | 🔻 regression  |
| `pii_detection`| 100.0% [0.93–1.00]   | 100.0%   | 0.0pp    | —           | ≈ unchanged    |

🔒 **Lock:** lock OK
```

## Why use this

PR comment is where engineers actually read CI eval results. Replacing
"the suite passed" with **"the suite passed *with these intervals*"**
turns eval from a vibes check into a statistical gate. Specifically:

- **Wilson 95% CI** on every per-evaluator pass rate so you can tell
  noise from signal at small n.
- **McNemar paired test** on the verdict deltas — flags only the
  evaluators whose change is statistically real.
- **Cost report.** Every comment shows the dollar cost of the run.
  Procurement won't approve a CI tool whose spend it can't predict.
- **Lockfile check.** Verifies `suite.lock` hasn't drifted (catches
  silent prompt changes).
- **Default gate ladder.** `PASS` / `FIX_THEN_MERGE` / `NEEDS_REWORK`
  with safety-class regressions escalating automatically.

## Quick start

```yaml
# .github/workflows/eval.yml
on:
  pull_request:
    paths: [src/**, evals/**]
jobs:
  eval:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: multivon-ai/eval-action@v1
        with:
          suite: evals/production.py
        env:
          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
```

`evals/production.py`:

```python
from multivon_eval import EvalSuite, EvalCase
from multivon_eval.evaluators.llm_judge import Faithfulness, Hallucination

def build_suite() -> EvalSuite:
    suite = EvalSuite.eu_ai_act_high_risk(jurisdiction="gdpr")
    suite.add_cases([
        EvalCase(input="Summarize this contract.", context=open("evals/contract.txt").read()),
        # …
    ])
    return suite
```

## Inputs

| Input | Default | Description |
|---|---|---|
| `suite` | (required) | Path to a Python file that exposes `suite` or `build_suite()`. |
| `baseline` | base branch of the PR | Git ref to diff against. |
| `fail-on` | `PR_NEEDS_REWORK` | When to exit non-zero. One of `NEVER`, `PR_NEEDS_REWORK`, `PR_FIX_THEN_MERGE`, `ANY_REGRESSION`. |
| `runs-per-case` | `3` | Multi-run flakiness detection. Higher = more confidence, more cost. |
| `workers` | `4` | Concurrent cases. |
| `evaluator-concurrency` | unbounded | Concurrent evaluators per case. |
| `comment-mode` | `replace` | `replace` (rewrite our previous comment), `append`, or `off`. |
| `gate-policy` | (none) | Path to a YAML policy file overriding the default gate rules. |
| `lockfile` | (none) | Path to a saved `suite.lock`. If set, drift causes a warning in the comment. |
| `github-token` | `${{ github.token }}` | Token with PR `comments: write` permission. |

## Outputs

| Output | Example |
|---|---|
| `gate` | `PASS`, `FIX_THEN_MERGE`, `NEEDS_REWORK` |
| `pass_rate` | `0.854` |
| `cost_usd` | `0.0345` |
| `comment_url` | `https://github.com/…/issues/1284#issuecomment-...` |

## Gate policy

Defaults handle 90% of cases. Override per repo with:

```yaml
# .multivon/gate-policy.yaml
gates:
  - rule: "regression"
    on_fail: FIX_THEN_MERGE
  - rule: "cost_delta_x > 2.0"
    on_fail: FIX_THEN_MERGE
```

Pass `gate-policy: .multivon/gate-policy.yaml` to the Action.

### Supported rule tokens

The parser is intentionally minimal. These are the exact tokens it matches:

| Token | What it does |
|---|---|
| `regression` | Fires if any evaluator regressed (`is_regression=True` after paired McNemar at p<0.05). |
| `cost_delta_x <op> <number>` | Supports `>`, `<`, `>=`, `<=`, `==`. Compares observed cost ratio to the threshold. |
| `lock_drift` | Reserved. Currently a no-op at the gate layer (lockfile drift is enforced one level up in the runner). |

Anything else silently falls through to the default rules. **For per-evaluator targeting** (e.g. faithfulness-only or safety-class gating), the default rules already route safety-class regressions (toxicity/bias/pii/hallucination by name match) to `NEEDS_REWORK` at p<0.05 — you don't need a custom rule for that.

A typed DSL with `evaluator(name).<field>` accessors is tracked as [post-launch work](https://github.com/multivon-ai/eval-action/issues) — earlier versions of this README advertised that syntax but the parser never landed it.

## How the verdict is computed

Default rules, applied top-down:

| Condition | Verdict |
|---|---|
| Any evaluator with `safety`/`toxicity`/`bias`/`pii`/`hallucination` in its name regresses with p<0.05 | `NEEDS_REWORK` |
| Any evaluator regresses with p<0.05 (CIs don't overlap) | `FIX_THEN_MERGE` |
| Overall pass rate dropped >5pp | `FIX_THEN_MERGE` |
| Cost > 2× baseline | `FIX_THEN_MERGE` |
| Otherwise | `PASS` |

Verdict → exit code is driven by the `fail-on` input.

## What it doesn't do

- **Replaces neither pytest nor your existing CI.** The Action is a
  layer on top — pair it with `pytest -q` in another job.
- **Does not check out arbitrary refs.** Uses git worktree against
  whatever refs your checkout step staged.
- **Does not handle PRs from forks** that lack `secrets.OPENAI_API_KEY`.
  Standard GitHub limitation; document a `workflow_run` pattern for
  fork support.

## Cost expectations

For a 50-case suite × 3 runs × 5 sub-calls/case on `gpt-4o-mini`:
roughly **$0.03 per PR** at default settings, and the comment shows
the actual number every time.

Pair with multivon-eval's built-in judge cache (`JudgeConfig(cache=True)`)
to amortize across repeated PRs on the same baseline.

## The Multivon ecosystem

Five public + one early-access package, all built on a shared evaluation engine:

| Repo | What it is |
|---|---|
| [multivon-eval](https://github.com/multivon-ai/multivon-eval) | Python SDK — the engine eval-action runs on every PR |
| [pdfhell](https://github.com/multivon-ai/pdfhell) | Adversarial PDFs — also emits JUnit output, also gates merges |
| [multivon-mcp](https://github.com/multivon-ai/multivon-mcp) | MCP server — call the same evals from inside Claude / Cursor / Cline |
| **eval-action** (you are here) | GitHub Action wrapper |
| [eval-framework-benchmark](https://github.com/multivon-ai/eval-framework-benchmark) | Reproducible head-to-head benchmark vs DeepEval + RAGAS |
| multivon-guard *(early access)* | Local proxy that catches LLM coding agents leaking secrets / PII |

## License

Apache 2.0.

---

Maintained by [Multivon](https://multivon.ai). Issues + PRs welcome.
