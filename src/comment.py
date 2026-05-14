"""
Build + post the PR comment.

The comment is the visible product. It must:

  • Lead with a verdict line so a reviewer can read just the title
    notification and know whether to merge.
  • Show per-evaluator pass rates with Wilson 95% CIs and the delta vs.
    baseline. Frame the delta as "inside noise" / "statistically real"
    so the reader doesn't have to do mental statistics.
  • Show the cost in dollars. Procurement-blocking otherwise.
  • Show whether the suite.lock matches.
  • Stay under ~100 lines so it doesn't swallow the PR description.

The post-side uses the GitHub REST API directly (no extra dep) and
honors the comment-mode setting: replace existing multivon comment,
append, or skip.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

from .compare import SuiteDiff, EvaluatorDiff
from .gate import Gate


_COMMENT_MARKER = "<!-- multivon-eval-action -->"


def build_comment(
    current: dict,
    baseline: dict | None,
    diff: SuiteDiff,
    gate: Gate,
    *,
    lock_status: str = "",
) -> str:
    """Compose the Markdown comment body."""
    lines: list[str] = [_COMMENT_MARKER, ""]

    # Header
    summary = current.get("summary", {})
    verdict_badge = _verdict_badge(gate.verdict)
    lines.append(f"## multivon-eval — {verdict_badge}")
    if gate.reason:
        lines.append(f"_{gate.reason}_")
    lines.append("")

    # Top metrics row
    pass_rate = summary.get("pass_rate")
    cost = (summary.get("costs") or {}).get("total_cost_usd")
    cost_str = f"${cost:.4f}" if cost is not None else "—"
    metrics = [
        f"**Pass rate** {pass_rate:.1%}" if pass_rate is not None else "**Pass rate** —",
        f"**Cost** {cost_str}",
        f"**Cases** {summary.get('total', '—')}",
        f"**Runs/case** {summary.get('runs_per_case', '—')}",
    ]
    if diff.has_baseline and diff.baseline_pass_rate is not None:
        delta_pp = (diff.current_pass_rate - diff.baseline_pass_rate) * 100
        sign = "+" if delta_pp >= 0 else ""
        metrics.append(f"**Δ vs baseline** {sign}{delta_pp:.1f}pp")
    if diff.cost_delta_x is not None:
        metrics.append(f"**Cost Δ** {diff.cost_delta_x:.1f}×")
    lines.append(" · ".join(metrics))
    lines.append("")

    # Per-evaluator table
    if diff.evaluators:
        lines.append("### Per-evaluator")
        if diff.has_baseline:
            lines.append(
                "| Evaluator | Pass rate (95% CI) | Baseline | Δ | p (McNemar) | Verdict |"
            )
            lines.append("|---|---|---|---|---|---|")
            for ev in diff.evaluators:
                lines.append(_eval_row_with_baseline(ev))
        else:
            lines.append("| Evaluator | Pass rate (95% CI) |")
            lines.append("|---|---|")
            for ev in diff.evaluators:
                lines.append(_eval_row_no_baseline(ev))
        lines.append("")

    # Lockfile section
    if lock_status:
        if "OK" in lock_status:
            lines.append(f"🔒 **Lock:** {lock_status}")
        elif "no lockfile" in lock_status:
            lines.append(f"🔓 **Lock:** {lock_status}")
        else:
            lines.append(f"⚠️ **Lock drift:** {lock_status}")
        lines.append("")

    # Footer
    lines.append("---")
    lines.append("_Computed with [multivon-ai/eval-action](https://github.com/multivon-ai/eval-action). "
                 "Wilson 95% CIs and McNemar p-values per evaluator. "
                 "Re-run with `runs-per-case: 5` to tighten._")
    return "\n".join(lines)


def _verdict_badge(verdict: str) -> str:
    if verdict == "PASS":
        return "✅ **PASS**"
    if verdict == "FIX_THEN_MERGE":
        return "⚠️ **FIX_THEN_MERGE**"
    if verdict == "NEEDS_REWORK":
        return "🛑 **NEEDS_REWORK**"
    return f"**{verdict}**"


def _ci_str(ci: tuple[float, float] | None) -> str:
    if ci is None:
        return "—"
    return f"[{ci[0]:.2f}–{ci[1]:.2f}]"


def _eval_row_with_baseline(ev: EvaluatorDiff) -> str:
    delta = "—"
    if ev.delta_pp is not None:
        sign = "+" if ev.delta_pp >= 0 else ""
        delta = f"{sign}{ev.delta_pp:.1f}pp"
    p = f"{ev.p_value}" if ev.p_value is not None else "—"
    if ev.is_regression:
        verdict = "🔻 regression"
    elif ev.is_improvement:
        verdict = "🟢 improvement"
    elif ev.delta_pp is not None and abs(ev.delta_pp) < 1.0:
        verdict = "≈ unchanged"
    else:
        verdict = "noise"
    base = (f"{ev.baseline_pass_rate:.1%}"
            if ev.baseline_pass_rate is not None else "—")
    return (f"| `{ev.name}` "
            f"| {ev.current_pass_rate:.1%} {_ci_str(ev.current_ci)} "
            f"| {base} "
            f"| {delta} "
            f"| {p} "
            f"| {verdict} |")


def _eval_row_no_baseline(ev: EvaluatorDiff) -> str:
    return (f"| `{ev.name}` "
            f"| {ev.current_pass_rate:.1%} {_ci_str(ev.current_ci)} |")


# ── Posting ─────────────────────────────────────────────────────────────────


def post_comment(body: str, *, mode: str = "replace") -> str | None:
    """Post the comment via the GitHub REST API.

    Returns the URL of the posted comment, or None if running outside a
    GitHub Actions context (e.g. local testing).
    """
    token = os.environ.get("INPUT_GITHUB-TOKEN") or os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not token or not repo or not event_path:
        print("[comment] not running on GitHub Actions or no token; skipping post.",
              file=sys.stderr)
        return None

    try:
        event = json.loads(open(event_path).read())
    except Exception as exc:
        print(f"[comment] could not read $GITHUB_EVENT_PATH: {exc}",
              file=sys.stderr)
        return None

    pr_number = (event.get("pull_request") or {}).get("number")
    if not pr_number:
        print("[comment] no pull_request.number in event; skipping post.",
              file=sys.stderr)
        return None

    base_url = f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    if mode == "replace":
        # Find an existing multivon comment and update or delete-and-recreate.
        existing = _find_existing_comment(base_url, headers)
        if existing is not None:
            return _update_comment(repo, existing, body, headers)
    return _create_comment(base_url, body, headers)


def _find_existing_comment(base_url: str, headers: dict) -> dict | None:
    try:
        req = urllib.request.Request(base_url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=30) as r:
            comments = json.loads(r.read())
    except Exception as exc:
        print(f"[comment] could not list comments: {exc}", file=sys.stderr)
        return None
    for c in comments:
        if _COMMENT_MARKER in (c.get("body") or ""):
            return c
    return None


def _create_comment(base_url: str, body: str, headers: dict) -> str | None:
    try:
        req = urllib.request.Request(
            base_url,
            data=json.dumps({"body": body}).encode(),
            headers={**headers, "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
            return data.get("html_url")
    except urllib.error.HTTPError as exc:
        print(f"[comment] POST failed: {exc.code} {exc.reason}", file=sys.stderr)
    except Exception as exc:
        print(f"[comment] POST failed: {exc}", file=sys.stderr)
    return None


def _update_comment(repo: str, existing: dict, body: str, headers: dict) -> str | None:
    cid = existing.get("id")
    if cid is None:
        return None
    url = f"https://api.github.com/repos/{repo}/issues/comments/{cid}"
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps({"body": body}).encode(),
            headers={**headers, "Content-Type": "application/json"},
            method="PATCH",
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
            return data.get("html_url")
    except Exception as exc:
        print(f"[comment] PATCH failed: {exc}", file=sys.stderr)
    return None
