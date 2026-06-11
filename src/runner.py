"""
multivon-ai/eval-action runner.

Reads the inputs (from positional args in the order action.yml lists
them), loads the user's suite, runs it, diffs against the baseline,
formats a PR comment, posts it, and exits with the gate verdict.

Designed to be runnable outside the container for unit-testing::

    python -m src.runner \
        --suite path/to/suite.py \
        --baseline main \
        --fail-on PR_NEEDS_REWORK \
        --runs-per-case 3 \
        --workers 4
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

from .compare import compare_reports
from .comment import build_comment, post_comment
from .gate import classify_gate, Gate


def _load_suite(suite_path: str):
    """Import the user's suite file and return the EvalSuite instance.

    The file must expose either a module-level ``suite`` variable or a
    ``build_suite()`` callable that returns an :class:`EvalSuite`.
    """
    path = Path(suite_path).resolve()
    if not path.exists():
        sys.exit(f"::error::suite file not found: {suite_path}")
    spec = importlib.util.spec_from_file_location("user_suite", path)
    if spec is None or spec.loader is None:
        sys.exit(f"::error::cannot import suite from {suite_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["user_suite"] = mod
    spec.loader.exec_module(mod)
    if hasattr(mod, "build_suite") and callable(mod.build_suite):
        return mod.build_suite()
    if hasattr(mod, "suite"):
        return mod.suite
    sys.exit(
        f"::error::suite file {suite_path} must expose `suite` or `build_suite()` "
        f"(see https://github.com/multivon-ai/eval-action#readme)"
    )


def _detect_base_ref() -> str:
    """Detect the PR base ref. Falls back to main."""
    base = os.environ.get("GITHUB_BASE_REF")
    if base:
        return base
    return "main"


def _maybe_load_lockfile(suite, lockfile_path: str) -> str:
    """If a lockfile path is set, verify against it. Returns a status string."""
    if not lockfile_path:
        return "no lockfile configured"
    p = Path(lockfile_path)
    if not p.exists():
        return f"lockfile not found at {lockfile_path}"
    try:
        suite.verify_lock(p)
        return "lock OK"
    except Exception as exc:
        return f"lock drift: {exc}"


def _model_fn():
    """Default no-op model fn for actions that don't need to call a real model.

    Most multivon-eval suites use deterministic / LLM-judge evaluators that
    *score the candidate output passed in the EvalCase*, not the model itself.
    For users who do want to run a model, override by exposing a
    ``model_fn(prompt) -> str`` in their suite file alongside ``suite``.
    """
    return lambda prompt: "[no model_fn configured — using candidate from EvalCase.expected_output]"


def _run_suite_on_ref(suite_path: str, ref: str, runs: int, workers: int) -> dict:
    """Run the user's suite at ``ref`` and return the EvalReport as a dict.

    We don't checkout the ref ourselves here — by the time the Action runs,
    GHA has already checked out the PR. For the baseline run, the orchestrator
    calls this with the runner pointing at a worktree of the baseline ref.
    """
    suite = _load_suite(suite_path)
    model_fn = _model_fn()
    report = suite.run(model_fn, runs=runs, workers=workers, verbose=False)
    raw = report.to_json()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        print(
            f"::error::EvalReport.to_json() returned non-JSON data "
            f"({type(exc).__name__}: {exc}). first 200 chars: {raw[:200]!r}",
            file=sys.stderr,
        )
        sys.exit(2)


def _clean_staleness_md(raw: str) -> str:
    """Tidy the staleness markdown for the step summary.

    The renderer ships its own "## Prompt staleness" heading (we add our
    warn-only one) and ends with an "_exit N_" line meant for terminal
    use — both read as defects in a GitHub step summary.
    """
    import re
    lines = raw.strip().splitlines()
    if lines and lines[0].strip() == "## Prompt staleness":
        lines = lines[1:]
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and re.fullmatch(r"_exit \d+_", lines[-1].strip()):
        lines.pop()
    return "\n".join(lines).strip()


def _maybe_staleness_summary(repo_path: str) -> None:
    """Append the prompt-drift staleness report to the job's step summary.

    Warn-only by contract: this function must NEVER fail the action — not on
    a missing baseline (exit 2), not on staleness findings (we don't pass
    --fail-on), not on a broken multivon-eval install. The gate verdict and
    the action's exit code are decided entirely elsewhere.
    """
    import subprocess
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "multivon_eval", "staleness", repo_path,
             "--format", "markdown"],
            capture_output=True, text=True, timeout=120,
        )
        report_md = _clean_staleness_md(proc.stdout)
        if not report_md:
            print(f"[runner] staleness: no output (exit {proc.returncode}); "
                  f"stderr: {proc.stderr.strip()[:200]}", file=sys.stderr)
            return
        summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary_path:
            with open(summary_path, "a", encoding="utf-8") as f:
                f.write("\n## Prompt-drift staleness (warn-only)\n\n")
                f.write(report_md)
                f.write("\n")
            print(f"[runner] staleness: report appended to step summary "
                  f"(exit {proc.returncode}, warn-only)", file=sys.stderr)
        else:
            # Local / non-Actions run: print instead of vanishing.
            print(report_md, file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 — warn-only means warn-only
        print(f"::warning::staleness check skipped ({type(exc).__name__}: {exc})",
              file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(prog="multivon-eval-action")
    ap.add_argument("--suite", required=True)
    ap.add_argument("--baseline", default="")
    ap.add_argument("--fail-on", default="PR_NEEDS_REWORK")
    ap.add_argument("--runs-per-case", type=int, default=3)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--evaluator-concurrency", default="")
    ap.add_argument("--comment-mode", default="replace",
                    choices=["replace", "append", "off"])
    ap.add_argument("--gate-policy", default="")
    ap.add_argument("--lockfile", default="")
    ap.add_argument("--staleness", default="")
    args = ap.parse_args()

    # ── 1. Load + verify lockfile if configured ────────────────────────────
    suite = _load_suite(args.suite)
    lock_status = _maybe_load_lockfile(suite, args.lockfile)
    print(f"[runner] lockfile: {lock_status}", file=sys.stderr)

    # ── 2. Run the suite on the PR head ────────────────────────────────────
    print("[runner] running suite on PR head …", file=sys.stderr)
    current_report = _run_suite_on_ref(args.suite, "HEAD",
                                       runs=args.runs_per_case,
                                       workers=args.workers)

    # ── 3. Run the suite on the baseline (if configured) ───────────────────
    baseline_ref = args.baseline or _detect_base_ref()
    baseline_report: dict | None = None
    if baseline_ref:
        print(f"[runner] running suite on baseline ref '{baseline_ref}' …", file=sys.stderr)
        baseline_report = _maybe_run_baseline(args.suite, baseline_ref,
                                              args.runs_per_case, args.workers)

    # ── 4. Diff + classify ─────────────────────────────────────────────────
    diff = compare_reports(current_report, baseline_report)
    gate = classify_gate(diff, args.gate_policy, args.fail_on)
    print(f"[runner] gate: {gate.verdict}  reason: {gate.reason}", file=sys.stderr)

    # ── 5. Comment ─────────────────────────────────────────────────────────
    comment_url = ""
    if args.comment_mode != "off":
        body = build_comment(current_report, baseline_report, diff, gate,
                             lock_status=lock_status)
        comment_url = post_comment(body, mode=args.comment_mode) or ""
        if comment_url:
            print(f"[runner] commented: {comment_url}", file=sys.stderr)

    # ── 5b. Prompt-drift staleness summary (warn-only) ──────────────────────
    if args.staleness:
        _maybe_staleness_summary(args.staleness)

    # ── 6. Emit GitHub Actions outputs ─────────────────────────────────────
    _set_output("gate", gate.verdict)
    _set_output("pass_rate", str(current_report.get("summary", {}).get("pass_rate", "")))
    cost = (current_report.get("summary", {}).get("costs") or {}).get("total_cost_usd")
    if cost is not None:
        _set_output("cost_usd", str(cost))
    if comment_url:
        _set_output("comment_url", comment_url)

    # ── 7. Exit ────────────────────────────────────────────────────────────
    if gate.should_fail:
        print(f"::error::eval gate FAILED — {gate.verdict}: {gate.reason}", file=sys.stderr)
        return 1
    return 0


def _maybe_run_baseline(suite_path: str, baseline_ref: str,
                        runs: int, workers: int) -> dict | None:
    """Best-effort baseline run.

    The runner expects to be invoked from a workspace where the PR head is
    checked out. We use ``git worktree`` to materialise the baseline ref in
    a sibling directory, then run the suite from inside it. If git is not
    available (e.g. the user pre-staged the baseline_report.json artifact),
    we fall back to reading it.

    Failures here never block the PR — we just skip the diff and report
    "no baseline" in the comment.
    """
    import subprocess

    # If the user staged a baseline report at $MULTIVON_BASELINE_REPORT, use it.
    pre = os.environ.get("MULTIVON_BASELINE_REPORT")
    if pre and Path(pre).exists():
        try:
            return json.loads(Path(pre).read_text())
        except Exception as exc:
            print(f"[runner] could not parse pre-staged baseline: {exc}",
                  file=sys.stderr)

    workdir = tempfile.mkdtemp(prefix="multivon-baseline-")
    try:
        subprocess.run(["git", "worktree", "add", "--detach", workdir, baseline_ref],
                       check=True, capture_output=True, text=True)
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        print(f"[runner] could not check out baseline {baseline_ref!r} via git worktree: {exc}",
              file=sys.stderr)
        return None

    try:
        baseline_suite = Path(workdir) / Path(suite_path).name \
            if "/" not in suite_path else Path(workdir) / suite_path
        if not baseline_suite.exists():
            print(f"[runner] suite file missing on baseline ref: {baseline_suite}",
                  file=sys.stderr)
            return None
        return _run_suite_on_ref(str(baseline_suite), baseline_ref,
                                 runs=runs, workers=workers)
    finally:
        try:
            subprocess.run(["git", "worktree", "remove", "--force", workdir],
                           check=False, capture_output=True)
        except FileNotFoundError:
            pass


def _set_output(key: str, value: str) -> None:
    """Write a step output line per GitHub Actions' file-based protocol."""
    out_file = os.environ.get("GITHUB_OUTPUT")
    if not out_file:
        return
    with open(out_file, "a") as f:
        f.write(f"{key}={value}\n")


if __name__ == "__main__":
    raise SystemExit(main())
