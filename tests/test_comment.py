"""Unit tests for the PR-comment builder."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.compare import SuiteDiff, EvaluatorDiff  # noqa: E402
from src.comment import build_comment, _COMMENT_MARKER  # noqa: E402
from src.gate import Gate  # noqa: E402


def _stub_report(*, pass_rate: float, cost: float | None = None,
                 cases: int = 10) -> dict:
    return {
        "suite": "stub",
        "summary": {
            "total": cases,
            "pass_rate": pass_rate,
            "runs_per_case": 3,
            "costs": {"total_cost_usd": cost} if cost is not None else None,
        },
        "cases": [],
    }


def _stub_diff(*, has_baseline: bool = True) -> SuiteDiff:
    return SuiteDiff(
        current_pass_rate=0.85,
        baseline_pass_rate=0.90 if has_baseline else None,
        current_cost_usd=0.0345,
        baseline_cost_usd=0.0200 if has_baseline else None,
        cost_delta_x=1.7 if has_baseline else None,
        evaluators=[
            EvaluatorDiff(
                name="faithfulness", current_pass_rate=0.9,
                baseline_pass_rate=0.95 if has_baseline else None,
                delta_pp=-5.0 if has_baseline else None,
                p_value=0.31 if has_baseline else None,
                current_ci=(0.78, 0.96),
                baseline_ci=(0.85, 0.99) if has_baseline else None,
                cohens_h=-0.15 if has_baseline else None,
            ),
            EvaluatorDiff(
                name="toxicity", current_pass_rate=0.5,
                baseline_pass_rate=1.0 if has_baseline else None,
                delta_pp=-50.0 if has_baseline else None,
                p_value=0.001 if has_baseline else None,
                current_ci=(0.32, 0.68),
                baseline_ci=(0.92, 1.00) if has_baseline else None,
                is_regression=has_baseline,
            ),
        ],
        has_baseline=has_baseline,
    )


class TestBuildComment:
    def test_carries_marker_for_replace_mode_lookup(self):
        body = build_comment(_stub_report(pass_rate=0.85), None,
                             _stub_diff(has_baseline=False),
                             Gate(verdict="PASS", reason="ok", should_fail=False))
        assert _COMMENT_MARKER in body

    def test_renders_verdict_badge(self):
        body = build_comment(_stub_report(pass_rate=0.5), None,
                             _stub_diff(has_baseline=False),
                             Gate(verdict="NEEDS_REWORK",
                                  reason="safety regression",
                                  should_fail=True))
        assert "NEEDS_REWORK" in body
        assert "safety regression" in body

    def test_renders_per_evaluator_rows(self):
        body = build_comment(_stub_report(pass_rate=0.85), _stub_report(pass_rate=0.90),
                             _stub_diff(),
                             Gate(verdict="FIX_THEN_MERGE",
                                  reason="toxicity regression",
                                  should_fail=False))
        assert "faithfulness" in body
        assert "toxicity" in body
        assert "regression" in body  # tox row
        assert "noise" in body or "≈ unchanged" in body  # faith row

    def test_renders_cost(self):
        body = build_comment(_stub_report(pass_rate=0.85, cost=0.0345),
                             _stub_report(pass_rate=0.90, cost=0.0200),
                             _stub_diff(),
                             Gate(verdict="FIX_THEN_MERGE",
                                  reason="", should_fail=False))
        assert "$0.0345" in body
        assert "1.7×" in body

    def test_lock_drift_surfaces_warning(self):
        body = build_comment(_stub_report(pass_rate=1.0), None,
                             _stub_diff(has_baseline=False),
                             Gate(verdict="PASS", reason="ok", should_fail=False),
                             lock_status="lock drift: evaluator added")
        assert "Lock drift" in body
        assert "evaluator added" in body

    def test_lock_ok_surfaces_check(self):
        body = build_comment(_stub_report(pass_rate=1.0), None,
                             _stub_diff(has_baseline=False),
                             Gate(verdict="PASS", reason="ok", should_fail=False),
                             lock_status="lock OK")
        assert "🔒" in body

    def test_no_baseline_omits_delta_column(self):
        body = build_comment(_stub_report(pass_rate=1.0), None,
                             _stub_diff(has_baseline=False),
                             Gate(verdict="PASS", reason="ok", should_fail=False))
        # The baseline-aware table has "Δ |" headers; the no-baseline table doesn't.
        assert "| Δ |" not in body
