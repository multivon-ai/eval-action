"""End-to-end integration test for the eval-action runner.

Validates the full pipeline locally — load a tiny suite, run it,
compare to a self-baseline, classify the gate, render the comment.
No GitHub Actions context required.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.compare import compare_reports  # noqa: E402
from src.comment import build_comment  # noqa: E402
from src.gate import classify_gate  # noqa: E402


# A minimal EvalReport-shaped dict for the test, with structure matching
# what multivon_eval.EvalReport.to_json() produces.
def _report(*, name: str, evaluators_per_case: list[dict[str, bool]],
            cost_usd: float | None = None) -> dict:
    total = len(evaluators_per_case)
    passed = sum(1 for evs in evaluators_per_case if all(evs.values()))
    return {
        "suite": name,
        "summary": {
            "total": total,
            "pass_rate": passed / total if total else 0.0,
            "passed": passed,
            "failed": total - passed,
            "runs_per_case": 1,
            "costs": {"total_cost_usd": cost_usd} if cost_usd is not None else None,
        },
        "cases": [
            {
                "input": f"q{i}",
                "output": "out",
                "passed": all(evs.values()),
                "score": sum(int(v) for v in evs.values()) / max(1, len(evs)),
                "evaluators": [
                    {"name": n, "passed": p, "score": 1.0 if p else 0.0, "reason": ""}
                    for n, p in evs.items()
                ],
            }
            for i, evs in enumerate(evaluators_per_case)
        ],
    }


def test_pipeline_pass_with_no_regression():
    """A run identical to baseline should produce a PASS gate."""
    cases = [{"faith": True, "hallucination": True}] * 5 + [{"faith": True, "hallucination": False}] * 5
    current = _report(name="t1", evaluators_per_case=cases, cost_usd=0.01)
    baseline = _report(name="t1", evaluators_per_case=cases, cost_usd=0.01)

    diff = compare_reports(current, baseline)
    gate = classify_gate(diff, policy_path="", fail_on="PR_NEEDS_REWORK")
    body = build_comment(current, baseline, diff, gate, lock_status="lock OK")

    assert gate.verdict == "PASS"
    assert not gate.should_fail
    assert "PASS" in body
    assert "🔒" in body


def test_pipeline_needs_rework_on_safety_regression():
    n = 10
    baseline_cases = [{"hallucination": True}] * n
    current_cases = [{"hallucination": False}] * n  # all hallu cases now fail
    current = _report(name="t2", evaluators_per_case=current_cases, cost_usd=0.02)
    baseline = _report(name="t2", evaluators_per_case=baseline_cases, cost_usd=0.02)

    diff = compare_reports(current, baseline)
    gate = classify_gate(diff, policy_path="", fail_on="PR_NEEDS_REWORK")
    body = build_comment(current, baseline, diff, gate)

    # "hallucination" matches the safety-bucket fuzzy match (substring).
    assert gate.verdict == "NEEDS_REWORK"
    assert gate.should_fail
    assert "NEEDS_REWORK" in body
    assert "regression" in body


def test_pipeline_fix_then_merge_on_cost_runaway():
    cases = [{"faith": True}] * 5
    current = _report(name="t3", evaluators_per_case=cases, cost_usd=0.10)
    baseline = _report(name="t3", evaluators_per_case=cases, cost_usd=0.02)

    diff = compare_reports(current, baseline)
    gate = classify_gate(diff, policy_path="", fail_on="PR_NEEDS_REWORK")
    body = build_comment(current, baseline, diff, gate)

    # 5× cost runaway should trigger FIX_THEN_MERGE under default rules.
    assert gate.verdict == "FIX_THEN_MERGE"
    assert "5.0×" in body or "5×" in body or "cost" in body.lower()


def test_pipeline_no_baseline_still_renders():
    cases = [{"faith": True, "hallucination": True}] * 5
    current = _report(name="t4", evaluators_per_case=cases, cost_usd=0.01)

    diff = compare_reports(current, None)
    gate = classify_gate(diff, policy_path="", fail_on="PR_NEEDS_REWORK")
    body = build_comment(current, None, diff, gate, lock_status="no lockfile configured")

    assert not diff.has_baseline
    assert gate.verdict == "PASS"
    # No baseline → no "Δ" column in the per-evaluator table
    assert "| Δ |" not in body
    assert "🔓" in body
