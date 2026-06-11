"""Warn-only contract for the staleness step-summary surface."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.runner import _maybe_staleness_summary  # noqa: E402


class _Proc:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


def test_appends_markdown_to_step_summary(tmp_path, monkeypatch):
    summary = tmp_path / "summary.md"
    summary.write_text("existing\n")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    with mock.patch("subprocess.run", return_value=_Proc(stdout="## report\nCHANGED (1)")):
        _maybe_staleness_summary(".")
    text = summary.read_text()
    assert "existing" in text                       # appended, not clobbered
    assert "Prompt-drift staleness (warn-only)" in text
    assert "CHANGED (1)" in text


def test_strips_duplicate_heading_and_exit_line(tmp_path, monkeypatch):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    raw = "## Prompt staleness\n\n**2 changed** · 0 removed\n\n_exit 2_\n"
    with mock.patch("subprocess.run", return_value=_Proc(stdout=raw, returncode=2)):
        _maybe_staleness_summary(".")
    text = summary.read_text()
    assert "Prompt-drift staleness (warn-only)" in text   # our heading
    assert "## Prompt staleness\n" not in text            # renderer's duplicate gone
    assert "_exit 2_" not in text                         # debug line stripped
    assert "**2 changed**" in text                        # findings survive


def test_nonzero_exit_still_appends_and_never_raises(tmp_path, monkeypatch):
    # exit 2 = no baseline; the friendly hint still lands in the summary.
    summary = tmp_path / "s.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    with mock.patch("subprocess.run", return_value=_Proc(stdout="no baseline found — run ...", returncode=2)):
        _maybe_staleness_summary(".")
    assert "no baseline found" in summary.read_text()


def test_subprocess_explosion_never_raises(monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    with mock.patch("subprocess.run", side_effect=OSError("boom")):
        _maybe_staleness_summary(".")                # must not raise
    assert "staleness check skipped" in capsys.readouterr().err


def test_empty_output_is_a_noop(tmp_path, monkeypatch):
    summary = tmp_path / "s.md"
    summary.write_text("")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    with mock.patch("subprocess.run", return_value=_Proc(stdout="", returncode=1, stderr="broken")):
        _maybe_staleness_summary(".")
    assert summary.read_text() == ""                 # nothing appended
