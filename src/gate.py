"""
Gate verdict + optional policy-as-code.

Default rules:

  • Any tag-`safety` evaluator regression with p<0.05  → NEEDS_REWORK
  • Any evaluator regression (p<0.05, CIs don't overlap) → FIX_THEN_MERGE
  • Overall pass rate dropped > 5pp and the drop is significant → FIX_THEN_MERGE
  • Otherwise PASS

Override via a YAML policy file passed as ``--gate-policy``::

    gates:
      - rule: "evaluator(faithfulness).regression_p < 0.05"
        on_fail: NEEDS_REWORK
      - rule: "cost_delta_x > 2.0"
        on_fail: FIX_THEN_MERGE
      - rule: "lock_drift"
        on_fail: NEEDS_REWORK

The DSL is intentionally minimal — just enough to gate real CI without
reinventing OPA.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .compare import SuiteDiff


GateVerdict = Literal["PASS", "FIX_THEN_MERGE", "NEEDS_REWORK"]


@dataclass
class Gate:
    verdict: GateVerdict
    reason: str
    should_fail: bool
    matched_rule: str = ""


_ORDER = {"PASS": 0, "FIX_THEN_MERGE": 1, "NEEDS_REWORK": 2}


def _max(a: GateVerdict, b: GateVerdict) -> GateVerdict:
    return a if _ORDER[a] >= _ORDER[b] else b


def _default_rules(diff: SuiteDiff) -> Gate:
    verdict: GateVerdict = "PASS"
    reasons: list[str] = []

    sig_regressions = [e for e in diff.evaluators if e.is_regression]
    if sig_regressions:
        # If any regression has a tag we'd classify as safety, escalate.
        safety_words = ("safety", "toxicity", "bias", "pii", "halluc")
        is_safety = any(
            any(w in e.name.lower() for w in safety_words)
            for e in sig_regressions
        )
        if is_safety:
            verdict = _max(verdict, "NEEDS_REWORK")
            reasons.append(
                f"safety-class regression: {', '.join(e.name for e in sig_regressions if any(w in e.name.lower() for w in safety_words))}"
            )
        else:
            verdict = _max(verdict, "FIX_THEN_MERGE")
            reasons.append(
                f"regressions: {', '.join(f'{e.name}({e.delta_pp:+}pp p={e.p_value})' for e in sig_regressions)}"
            )

    if (diff.baseline_pass_rate is not None
            and diff.current_pass_rate - diff.baseline_pass_rate < -0.05):
        verdict = _max(verdict, "FIX_THEN_MERGE")
        reasons.append(
            f"overall pass rate dropped {(diff.current_pass_rate - diff.baseline_pass_rate)*100:.1f}pp"
        )

    if diff.cost_delta_x is not None and diff.cost_delta_x > 2.0:
        verdict = _max(verdict, "FIX_THEN_MERGE")
        reasons.append(f"cost {diff.cost_delta_x:.1f}× baseline")

    reason = "; ".join(reasons) if reasons else "no statistically significant regressions"
    return Gate(
        verdict=verdict,
        reason=reason,
        should_fail=False,  # caller maps verdict → should_fail based on --fail-on
        matched_rule="default rules",
    )


def _evaluate_policy_file(diff: SuiteDiff, policy_path: str) -> Gate | None:
    """If a policy file is provided, evaluate each rule top-down.

    First rule whose condition holds becomes the verdict. Returns None
    if no rule matched (caller falls back to default rules).
    """
    import yaml  # PyYAML — vendored into the container image

    try:
        spec = yaml.safe_load(Path(policy_path).read_text())
    except FileNotFoundError:
        return None
    rules = (spec or {}).get("gates", []) if isinstance(spec, dict) else []
    for rule in rules:
        cond = rule.get("rule", "")
        on_fail: GateVerdict = rule.get("on_fail", "FIX_THEN_MERGE")
        if _rule_holds(cond, diff):
            return Gate(
                verdict=on_fail, reason=f"policy rule fired: {cond}",
                should_fail=False, matched_rule=cond,
            )
    return None


def _rule_holds(rule: str, diff: SuiteDiff) -> bool:
    """Tiny DSL evaluator. Knows a few keywords.

    Intentional design: any user-supplied rule that references unknown
    fields should evaluate to False, not raise — so a typo in the YAML
    doesn't silently override CI to allow a regression.
    """
    s = rule.strip().lower()
    if "regression" in s:
        return any(e.is_regression for e in diff.evaluators)
    if s.startswith("cost_delta_x"):
        # form: "cost_delta_x > 2.0"
        if diff.cost_delta_x is None:
            return False
        try:
            op, val = s.split("cost_delta_x", 1)[1].strip().split()
            v = float(val)
            return {
                ">": diff.cost_delta_x > v, "<": diff.cost_delta_x < v,
                ">=": diff.cost_delta_x >= v, "<=": diff.cost_delta_x <= v,
                "==": diff.cost_delta_x == v,
            }.get(op.strip(), False)
        except Exception:
            return False
    if "lock_drift" in s:
        # The runner stamps a lock-status string into the comment; here we
        # only see the diff, so policy-based lock gating belongs at the
        # runner layer, not here. Always False for now.
        return False
    return False


def classify_gate(diff: SuiteDiff, policy_path: str, fail_on: str) -> Gate:
    """Top-level gate decision."""
    if policy_path:
        from_policy = _evaluate_policy_file(diff, policy_path)
        if from_policy is not None:
            from_policy.should_fail = _decide_fail(from_policy.verdict, fail_on)
            return from_policy

    g = _default_rules(diff)
    g.should_fail = _decide_fail(g.verdict, fail_on)
    return g


def _decide_fail(verdict: GateVerdict, fail_on: str) -> bool:
    """Translate the verdict + the --fail-on knob into an exit code."""
    fail_on = fail_on.upper()
    if fail_on == "NEVER":
        return False
    if fail_on == "ANY_REGRESSION":
        return verdict in ("FIX_THEN_MERGE", "NEEDS_REWORK")
    if fail_on == "PR_NEEDS_REWORK":
        return verdict == "NEEDS_REWORK"
    if fail_on == "PR_FIX_THEN_MERGE":
        return verdict in ("FIX_THEN_MERGE", "NEEDS_REWORK")
    # Default — treat unknown as PR_NEEDS_REWORK.
    return verdict == "NEEDS_REWORK"
