"""Unit tests for the diff machinery — runs without docker or a real API."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make the action's `src/` importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.compare import (  # noqa: E402
    SuiteDiff,
    compare_reports,
    _cohens_h,
    _mcnemar_p_value,
    _wilson_ci,
)


def _make_report(*, pass_rate: float, cost: float | None = None,
                 cases: list[tuple[str, dict[str, bool]]] | None = None,
                 runs: int = 1) -> dict:
    """Build a minimal EvalReport-shaped dict for testing.

    cases is a list of (case_input, {evaluator_name: passed}).
    """
    cases = cases or []
    case_rows = []
    for inp, evs in cases:
        case_rows.append({
            "input": inp,
            "output": "out",
            "passed": all(evs.values()),
            "score": sum(int(v) for v in evs.values()) / max(1, len(evs)),
            "evaluators": [
                {"name": n, "passed": p, "score": 1.0 if p else 0.0, "reason": ""}
                for n, p in evs.items()
            ],
        })
    return {
        "suite": "test",
        "summary": {
            "total": len(cases),
            "pass_rate": pass_rate,
            "runs_per_case": runs,
            "costs": {"total_cost_usd": cost} if cost is not None else None,
        },
        "cases": case_rows,
    }


class TestWilsonCI:
    def test_full_pass_returns_high_ci(self):
        lo, hi = _wilson_ci(10, 10)
        assert lo > 0.6 and hi == pytest.approx(1.0, abs=1e-6)

    def test_zero_pass_returns_low_ci(self):
        lo, hi = _wilson_ci(0, 10)
        assert lo == 0.0 and hi < 0.4

    def test_balanced_centered(self):
        lo, hi = _wilson_ci(5, 10)
        assert 0.2 < lo < 0.5
        assert 0.5 < hi < 0.85

    def test_zero_total_is_safe(self):
        assert _wilson_ci(0, 0) == (0.0, 0.0)


class TestCohensH:
    def test_zero_for_equal(self):
        assert abs(_cohens_h(0.5, 0.5)) < 1e-9

    def test_sign_follows_direction(self):
        assert _cohens_h(0.8, 0.5) > 0
        assert _cohens_h(0.5, 0.8) < 0


class TestMcNemar:
    def test_no_changes_returns_high_p(self):
        # No discordant pairs → p == 1.0
        assert _mcnemar_p_value(0, 0) == 1.0

    def test_asymmetric_changes_lower_p(self):
        # 10 cases regressed, 0 improved → significant.
        p = _mcnemar_p_value(10, 0)
        assert p < 0.05

    def test_symmetric_changes_are_not_significant(self):
        p = _mcnemar_p_value(5, 5)
        assert p > 0.05


class TestCompareReports:
    def test_no_baseline_emits_evaluator_rows_with_no_delta(self):
        current = _make_report(pass_rate=0.8, cases=[
            ("q1", {"faithfulness": True, "hallucination": True}),
            ("q2", {"faithfulness": True, "hallucination": False}),
        ])
        diff = compare_reports(current, None)
        assert isinstance(diff, SuiteDiff)
        assert not diff.has_baseline
        names = {e.name for e in diff.evaluators}
        assert names == {"faithfulness", "hallucination"}
        for ev in diff.evaluators:
            assert ev.delta_pp is None
            assert ev.p_value is None
            assert ev.baseline_pass_rate is None

    def test_perfect_baseline_match_no_regression(self):
        cases = [("q1", {"f": True}), ("q2", {"f": True})]
        cur = _make_report(pass_rate=1.0, cases=cases)
        base = _make_report(pass_rate=1.0, cases=cases)
        diff = compare_reports(cur, base)
        assert diff.has_baseline
        ev = diff.evaluators[0]
        assert ev.delta_pp == 0
        assert not ev.is_regression
        assert not ev.is_improvement

    def test_significant_regression_flagged(self):
        # 10 baseline cases all pass; current — same 10 cases, 8 fail.
        n = 10
        base_cases = [(f"q{i}", {"f": True}) for i in range(n)]
        cur_cases = [(f"q{i}", {"f": i >= 8}) for i in range(n)]  # 2 pass, 8 fail
        base = _make_report(pass_rate=1.0, cases=base_cases)
        cur = _make_report(pass_rate=0.2, cases=cur_cases)
        diff = compare_reports(cur, base)
        ev = diff.evaluators[0]
        assert ev.p_value < 0.05
        assert ev.is_regression
        assert ev.delta_pp < 0
        assert ev.cases_changed == 8

    def test_significant_improvement_flagged(self):
        n = 10
        base_cases = [(f"q{i}", {"f": False}) for i in range(n)]
        cur_cases = [(f"q{i}", {"f": True}) for i in range(n)]  # all flip up
        base = _make_report(pass_rate=0.0, cases=base_cases)
        cur = _make_report(pass_rate=1.0, cases=cur_cases)
        diff = compare_reports(cur, base)
        ev = diff.evaluators[0]
        assert ev.is_improvement
        assert ev.delta_pp > 0

    def test_cost_delta_computed(self):
        cur = _make_report(pass_rate=1.0, cost=0.04, cases=[("q", {"f": True})])
        base = _make_report(pass_rate=1.0, cost=0.02, cases=[("q", {"f": True})])
        diff = compare_reports(cur, base)
        assert diff.cost_delta_x == 2.0

    def test_missing_baseline_cost_does_not_break(self):
        cur = _make_report(pass_rate=1.0, cost=0.04, cases=[("q", {"f": True})])
        base = _make_report(pass_rate=1.0, cost=None, cases=[("q", {"f": True})])
        diff = compare_reports(cur, base)
        assert diff.cost_delta_x is None
