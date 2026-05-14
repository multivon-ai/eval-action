"""Unit tests for the gate verdict + policy DSL."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.compare import SuiteDiff, EvaluatorDiff  # noqa: E402
from src.gate import classify_gate, _default_rules, _decide_fail  # noqa: E402


def _diff(*, regressions: list[str] = (), cost_delta: float | None = None,
          overall_drop_pp: float = 0.0, has_baseline: bool = True) -> SuiteDiff:
    evs: list[EvaluatorDiff] = []
    for name in regressions:
        evs.append(EvaluatorDiff(
            name=name, current_pass_rate=0.5, baseline_pass_rate=0.9,
            delta_pp=-40.0, p_value=0.001, is_regression=True,
        ))
    return SuiteDiff(
        current_pass_rate=1.0 - overall_drop_pp / 100,
        baseline_pass_rate=1.0,
        cost_delta_x=cost_delta,
        evaluators=evs,
        has_baseline=has_baseline,
    )


class TestDefaultRules:
    def test_no_regressions_returns_pass(self):
        g = _default_rules(_diff())
        assert g.verdict == "PASS"

    def test_non_safety_regression_is_fix_then_merge(self):
        g = _default_rules(_diff(regressions=["context_precision"]))
        assert g.verdict == "FIX_THEN_MERGE"

    def test_safety_regression_is_needs_rework(self):
        g = _default_rules(_diff(regressions=["toxicity"]))
        assert g.verdict == "NEEDS_REWORK"

    def test_hallucination_regression_is_needs_rework(self):
        g = _default_rules(_diff(regressions=["hallucination"]))
        assert g.verdict == "NEEDS_REWORK"

    def test_overall_drop_escalates(self):
        g = _default_rules(_diff(overall_drop_pp=10))
        assert g.verdict in ("FIX_THEN_MERGE", "NEEDS_REWORK")

    def test_cost_runaway_escalates(self):
        g = _default_rules(_diff(cost_delta=3.0))
        assert g.verdict in ("FIX_THEN_MERGE", "NEEDS_REWORK")


class TestFailOn:
    def test_never_never_fails(self):
        assert _decide_fail("PASS", "NEVER") is False
        assert _decide_fail("FIX_THEN_MERGE", "NEVER") is False
        assert _decide_fail("NEEDS_REWORK", "NEVER") is False

    def test_pr_needs_rework_only_fails_on_needs_rework(self):
        assert _decide_fail("PASS", "PR_NEEDS_REWORK") is False
        assert _decide_fail("FIX_THEN_MERGE", "PR_NEEDS_REWORK") is False
        assert _decide_fail("NEEDS_REWORK", "PR_NEEDS_REWORK") is True

    def test_pr_fix_then_merge_fails_on_both(self):
        assert _decide_fail("PASS", "PR_FIX_THEN_MERGE") is False
        assert _decide_fail("FIX_THEN_MERGE", "PR_FIX_THEN_MERGE") is True
        assert _decide_fail("NEEDS_REWORK", "PR_FIX_THEN_MERGE") is True

    def test_any_regression_fails_on_both(self):
        assert _decide_fail("PASS", "ANY_REGRESSION") is False
        assert _decide_fail("FIX_THEN_MERGE", "ANY_REGRESSION") is True
        assert _decide_fail("NEEDS_REWORK", "ANY_REGRESSION") is True

    def test_unknown_fail_on_defaults_to_needs_rework(self):
        assert _decide_fail("FIX_THEN_MERGE", "BOGUS") is False
        assert _decide_fail("NEEDS_REWORK", "BOGUS") is True


class TestPolicyFile:
    def test_policy_rule_fires(self, tmp_path):
        policy = tmp_path / "policy.yaml"
        policy.write_text("""
gates:
  - rule: "cost_delta_x > 1.5"
    on_fail: NEEDS_REWORK
""")
        diff = _diff(cost_delta=2.0)
        g = classify_gate(diff, str(policy), fail_on="PR_NEEDS_REWORK")
        assert g.verdict == "NEEDS_REWORK"
        assert "cost_delta_x" in g.matched_rule

    def test_no_matching_policy_falls_back_to_defaults(self, tmp_path):
        policy = tmp_path / "policy.yaml"
        policy.write_text("""
gates:
  - rule: "cost_delta_x > 100"
    on_fail: NEEDS_REWORK
""")
        diff = _diff(cost_delta=1.0, regressions=["faithfulness"])
        g = classify_gate(diff, str(policy), fail_on="PR_NEEDS_REWORK")
        # Default rules should classify faithfulness regression as
        # NEEDS_REWORK (it matches the "halluc" safety bucket fuzzily).
        # Either way it should not be PASS.
        assert g.verdict != "PASS"
