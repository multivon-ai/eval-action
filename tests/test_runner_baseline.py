"""Baseline resolution in _maybe_run_baseline + reserved-input note.

Covers the three baseline sources (env var, .json file path, git ref)
and the graceful-failure contract: parse errors print to stderr and
return None — they never block the PR.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.runner import (  # noqa: E402
    _maybe_run_baseline,
    _note_reserved_evaluator_concurrency,
)


_REPORT = {"suite": "t", "summary": {"total": 1, "pass_rate": 1.0}, "cases": []}


# ── baseline: a path to an existing .json file ──────────────────────────────

def test_file_path_baseline_loads_saved_report(tmp_path, monkeypatch):
    monkeypatch.delenv("MULTIVON_BASELINE_REPORT", raising=False)
    report_file = tmp_path / "baseline_report.json"
    report_file.write_text(json.dumps(_REPORT))
    with mock.patch("subprocess.run") as run:
        result = _maybe_run_baseline("evals/suite.py", str(report_file),
                                     runs=3, workers=4)
    assert result == _REPORT
    run.assert_not_called()                    # no git worktree attempted


def test_malformed_json_file_degrades_to_none(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("MULTIVON_BASELINE_REPORT", raising=False)
    report_file = tmp_path / "baseline_report.json"
    report_file.write_text("{not valid json")
    with mock.patch("subprocess.run") as run:
        result = _maybe_run_baseline("evals/suite.py", str(report_file),
                                     runs=3, workers=4)
    assert result is None                      # never blocks the PR
    assert "could not parse baseline report file" in capsys.readouterr().err
    run.assert_not_called()                    # a .json path is not retried as a ref


def test_nonexistent_json_path_falls_through_to_git_ref(monkeypatch, capsys):
    # A .json-looking value that isn't a file on disk is treated as a ref;
    # the (failing) worktree path keeps the existing graceful contract.
    monkeypatch.delenv("MULTIVON_BASELINE_REPORT", raising=False)
    err = subprocess.CalledProcessError(128, ["git"], stderr="bad ref")
    with mock.patch("subprocess.run", side_effect=err) as run:
        result = _maybe_run_baseline("evals/suite.py", "no/such/report.json",
                                     runs=3, workers=4)
    assert result is None
    assert "could not check out baseline" in capsys.readouterr().err
    assert run.call_args[0][0][:3] == ["git", "worktree", "add"]


# ── env var still works and takes precedence ────────────────────────────────

def test_env_var_baseline_still_works(tmp_path, monkeypatch):
    pre = tmp_path / "pre_staged.json"
    pre.write_text(json.dumps(_REPORT))
    monkeypatch.setenv("MULTIVON_BASELINE_REPORT", str(pre))
    with mock.patch("subprocess.run") as run:
        result = _maybe_run_baseline("evals/suite.py", "origin/main",
                                     runs=3, workers=4)
    assert result == _REPORT
    run.assert_not_called()


def test_env_var_takes_precedence_over_file_path(tmp_path, monkeypatch):
    env_report = dict(_REPORT, suite="from-env")
    pre = tmp_path / "pre_staged.json"
    pre.write_text(json.dumps(env_report))
    monkeypatch.setenv("MULTIVON_BASELINE_REPORT", str(pre))
    other = tmp_path / "baseline_report.json"
    other.write_text(json.dumps(_REPORT))
    result = _maybe_run_baseline("evals/suite.py", str(other),
                                 runs=3, workers=4)
    assert result["suite"] == "from-env"


# ── git-ref behavior unchanged ──────────────────────────────────────────────

def test_git_ref_baseline_unchanged(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("MULTIVON_BASELINE_REPORT", raising=False)
    err = subprocess.CalledProcessError(128, ["git"], stderr="unknown revision")
    with mock.patch("subprocess.run", side_effect=err) as run:
        result = _maybe_run_baseline("evals/suite.py", "origin/main",
                                     runs=3, workers=4)
    assert result is None
    assert "could not check out baseline 'origin/main'" in capsys.readouterr().err
    cmd = run.call_args[0][0]
    assert cmd[:4] == ["git", "worktree", "add", "--detach"]
    assert cmd[-1] == "origin/main"


# ── evaluator-concurrency: reserved-input note ──────────────────────────────

def test_evaluator_concurrency_note_printed_when_set(capsys):
    _note_reserved_evaluator_concurrency("4")
    err = capsys.readouterr().err
    assert "[runner] note: evaluator-concurrency is reserved" in err


def test_evaluator_concurrency_silent_when_unset(capsys):
    _note_reserved_evaluator_concurrency("")
    assert capsys.readouterr().err == ""


def test_save_report_writes_current_report(tmp_path, monkeypatch):
    """--save-report writes the current run's report JSON — the documented
    way to produce baseline_report.json (release-readiness finding F5)."""
    import sys
    from unittest import mock
    from src import runner as runner_mod

    suite_file = tmp_path / "suite.py"
    suite_file.write_text(SUITE_SRC)
    out = tmp_path / "baseline_report.json"
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "gh_out"))
    monkeypatch.delenv("GITHUB_BASE_REF", raising=False)
    argv = ["prog", "--suite", str(suite_file), "--comment-mode", "off",
            "--baseline", "", "--save-report", str(out)]
    with mock.patch.object(sys, "argv", argv):
        with mock.patch.object(runner_mod, "_maybe_run_baseline", return_value=None):
            code = runner_mod.main()
    assert code == 0
    assert out.is_file()
    data = json.loads(out.read_text())
    assert "summary" in data
