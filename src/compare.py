"""
Statistical diff between two EvalReport JSONs.

The interesting comparison isn't "is the new F1 lower than the old F1?"
— it's "is the difference outside the noise band?". We compute Wilson
confidence intervals on the per-evaluator pass rate at both refs and
flag a per-evaluator regression only when the two CIs don't overlap,
or when a paired McNemar test rejects equality at p < 0.05.

For users without a baseline (first run on the repo), we still produce
a per-evaluator row — just without a delta column.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict


@dataclass
class EvaluatorDiff:
    name: str
    current_pass_rate: float
    baseline_pass_rate: float | None = None
    delta_pp: float | None = None
    current_ci: tuple[float, float] | None = None
    baseline_ci: tuple[float, float] | None = None
    p_value: float | None = None  # McNemar p
    cohens_h: float | None = None
    is_regression: bool = False
    is_improvement: bool = False
    cases_changed: int = 0


@dataclass
class SuiteDiff:
    current_pass_rate: float
    baseline_pass_rate: float | None = None
    current_cost_usd: float | None = None
    baseline_cost_usd: float | None = None
    cost_delta_x: float | None = None  # current / baseline
    evaluators: list[EvaluatorDiff] = field(default_factory=list)
    has_baseline: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


# ── Stats ───────────────────────────────────────────────────────────────────


def _wilson_ci(passed: int, total: int, confidence: float = 0.95) -> tuple[float, float]:
    """Wilson score 95% CI on a proportion. Symmetric around the MLE-ish point."""
    if total == 0:
        return (0.0, 0.0)
    z = 1.959963984540054 if confidence == 0.95 else 1.96
    p = passed / total
    denom = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denom
    margin = (z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


def _cohens_h(p1: float, p2: float) -> float:
    """Cohen's h on two proportions. 0.2 small, 0.5 medium, 0.8 large."""
    a = 2 * math.asin(math.sqrt(max(0.0, min(1.0, p1))))
    b = 2 * math.asin(math.sqrt(max(0.0, min(1.0, p2))))
    return a - b


def _norm_cdf(x: float) -> float:
    """Standard normal CDF via erf."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2)))


def _mcnemar_p_value(b: int, c: int) -> float:
    """Two-sided continuity-corrected McNemar p-value on a 2×2 paired table.

    b = baseline-pass, current-fail
    c = baseline-fail, current-pass
    """
    n = b + c
    if n == 0:
        return 1.0
    chi = (abs(b - c) - 1) ** 2 / n
    return 2 * (1 - _norm_cdf(math.sqrt(chi)))


# ── Diff ────────────────────────────────────────────────────────────────────


def _evaluator_pass_rates(report: dict) -> dict[str, dict]:
    """Extract per-evaluator (passed, total) tallies from a serialized report."""
    out: dict[str, dict] = {}
    for case in report.get("cases", []):
        for ev in case.get("evaluators", []):
            slot = out.setdefault(ev["name"], {"passed": 0, "total": 0,
                                                "verdicts": {}})
            slot["total"] += 1
            if ev["passed"]:
                slot["passed"] += 1
            slot["verdicts"][_case_key(case)] = bool(ev["passed"])
    return out


def _case_key(case: dict) -> str:
    """Stable key for matching cases between current + baseline.

    Falls back to a hash of the input when no explicit case id exists.
    """
    cid = case.get("id") or case.get("case_id")
    if cid:
        return str(cid)
    import hashlib
    return hashlib.sha256(case.get("input", "").encode()).hexdigest()[:16]


def compare_reports(current: dict, baseline: dict | None) -> SuiteDiff:
    """Build the SuiteDiff from two EvalReport dicts.

    Either argument may be ``None`` if the run only has one side.
    """
    cur_summary = current.get("summary", {})
    diff = SuiteDiff(
        current_pass_rate=float(cur_summary.get("pass_rate", 0.0)),
        current_cost_usd=(cur_summary.get("costs") or {}).get("total_cost_usd"),
        has_baseline=baseline is not None,
    )

    cur_evs = _evaluator_pass_rates(current)
    if baseline is not None:
        base_summary = baseline.get("summary", {})
        diff.baseline_pass_rate = float(base_summary.get("pass_rate", 0.0))
        diff.baseline_cost_usd = (base_summary.get("costs") or {}).get("total_cost_usd")
        if (diff.baseline_cost_usd and diff.baseline_cost_usd > 0
                and diff.current_cost_usd is not None):
            diff.cost_delta_x = round(diff.current_cost_usd / diff.baseline_cost_usd, 2)
        base_evs = _evaluator_pass_rates(baseline)
    else:
        base_evs = {}

    all_names = sorted(set(cur_evs) | set(base_evs))
    for name in all_names:
        cur = cur_evs.get(name, {"passed": 0, "total": 0, "verdicts": {}})
        bas = base_evs.get(name)
        ed = EvaluatorDiff(
            name=name,
            current_pass_rate=(cur["passed"] / cur["total"] if cur["total"] else 0.0),
            current_ci=_wilson_ci(cur["passed"], cur["total"]),
        )
        if bas is not None:
            base_rate = bas["passed"] / bas["total"] if bas["total"] else 0.0
            ed.baseline_pass_rate = base_rate
            ed.baseline_ci = _wilson_ci(bas["passed"], bas["total"])
            ed.delta_pp = round((ed.current_pass_rate - base_rate) * 100, 1)
            ed.cohens_h = round(_cohens_h(ed.current_pass_rate, base_rate), 3)

            # Paired McNemar across matched case ids.
            common = set(cur["verdicts"].keys()) & set(bas["verdicts"].keys())
            b = sum(1 for k in common
                    if bas["verdicts"][k] and not cur["verdicts"][k])
            c = sum(1 for k in common
                    if not bas["verdicts"][k] and cur["verdicts"][k])
            ed.cases_changed = b + c
            ed.p_value = round(_mcnemar_p_value(b, c), 4)

            # Statistically-significant regression rule: McNemar p<0.05
            # AND delta is negative AND CI ranges don't overlap.
            ci_overlap = (ed.current_ci[1] >= ed.baseline_ci[0]
                          and ed.baseline_ci[1] >= ed.current_ci[0])
            if ed.delta_pp < 0 and ed.p_value < 0.05 and not ci_overlap:
                ed.is_regression = True
            if ed.delta_pp > 0 and ed.p_value < 0.05 and not ci_overlap:
                ed.is_improvement = True
        diff.evaluators.append(ed)

    return diff
