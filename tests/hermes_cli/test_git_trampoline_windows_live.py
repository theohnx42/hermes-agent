"""Live Windows E2E for the Git-for-Windows trampoline self-heal (#87876).

Runs ONLY on a real windows-latest runner (wine2e/** on-demand lane). No
mocked subprocess anywhere: the probes drive the REAL
``_git_is_trampoline`` / ``_locate_real_git`` / ``_ensure_non_trampoline_git``
helpers against the runner's genuine Git-for-Windows install plus a real
broken-trampoline stand-in (a .bat that reproduces the launcher's
"BUG (fork bomb)" guard output and refuses to run).

windows-latest ships full Git for Windows at ``C:\\Program Files\\Git`` with
the real ``mingw64\\libexec\\git-core\\git.exe`` — exactly the layout the
locator targets — so the swap path is exercised end to end.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.platforms("windows")  # live Windows trampoline E2E

_FORK_BOMB_BAT = (
    "@echo off\r\n"
    "echo BUG (fork bomb): tried to spawn itself, check your PATH 1>&2\r\n"
    "exit /b 1\r\n"
)


def _make_broken_trampoline(tmp_path: Path) -> Path:
    bat = tmp_path / "git.bat"
    bat.write_text(_FORK_BOMB_BAT, encoding="utf-8")
    return bat


def _locator_diagnostic() -> str:
    """Failure-only evidence for the locator's exact candidates; never dump provider output."""
    from hermes_cli.update_cmd_git import _GIT_TEXT_KW, _portable_git_candidates
    from hermes_cli.update_custody import run_git

    candidates = [Path(base) / "Git" / arch / "libexec" / "git-core" / "git.exe"
                  for base in (r"C:\Program Files", r"C:\Program Files (x86)")
                  for arch in ("clangarm64", "mingw64", "mingw32")]
    candidates += [Path(base) / "Git" / "ucrt64" / "bin" / "git.exe"
                   for base in (r"C:\Program Files", r"C:\Program Files (x86)")]
    candidates += _portable_git_candidates()
    rows = []
    for path in candidates[:12]:
        row = {"path": str(path)}
        try:
            row.update(exists=path.exists(), regular=path.is_file(), symlink=path.is_symlink())
            if row["regular"]:
                # Same custody runner/encoding/flags as _probe_fork_bomb, but a
                # tighter failure-diagnostic budget (at most twelve two-second probes).
                probe = run_git([str(path)], ["--version"], timeout=2, **_GIT_TEXT_KW)
                row.update(returncode=probe.returncode,
                           fork_bomb="fork bomb" in ((probe.stdout or "") + (probe.stderr or "")).lower())
        except Exception as error:
            row["error_class"] = type(error).__name__
        rows.append(row)
    return json.dumps(rows, sort_keys=True)


class TestGitTrampolineLive:
    def test_runner_git_is_not_a_trampoline(self):
        from hermes_cli import update_cmd

        # Healthy PATH git on the runner: probe must report False and the
        # probe itself must actually run git (sanity: git exists here).
        version = subprocess.run(
            ["git", "--version"], capture_output=True, text=True, timeout=30,
            check=False,
        )
        assert version.returncode == 0, version.stderr
        assert update_cmd._git_is_trampoline(["git"]) is False

    def test_broken_trampoline_detected_live(self, tmp_path):
        from hermes_cli import update_cmd

        bat = _make_broken_trampoline(tmp_path)
        assert update_cmd._git_is_trampoline([str(bat)]) is True

    def test_locate_real_git_finds_runner_git_core(self):
        from hermes_cli import update_cmd

        real = update_cmd._locate_real_git()
        # windows-latest ships Git for Windows under Program Files with the
        # canonical git-core layout the locator searches first.
        assert real is not None, "expected git-core git.exe on windows-latest: " + _locator_diagnostic()
        assert real.exists()
        assert real.name == "git.exe"
        probe = subprocess.run(
            [str(real), "--version"], capture_output=True, text=True, timeout=30,
            check=False,
        )
        assert probe.returncode == 0
        assert "git version" in probe.stdout.lower()

    def test_ensure_non_trampoline_git_swaps_live(self, tmp_path, capsys):
        from hermes_cli import update_cmd

        bat = _make_broken_trampoline(tmp_path)
        broken_cmd = [str(bat), "-c", "windows.appendAtomically=false"]
        healed = update_cmd._ensure_non_trampoline_git(broken_cmd)

        assert healed != broken_cmd, "broken trampoline must be swapped: " + _locator_diagnostic()
        assert healed[1:] == broken_cmd[1:], "git config args must survive"
        real = Path(healed[0])
        assert real.exists() and real.name == "git.exe"
        # The healed command must actually work.
        result = subprocess.run(
            healed[:1] + ["--version"], capture_output=True, text=True, timeout=30,
            check=False,
        )
        assert result.returncode == 0
        assert "switching to real git" in capsys.readouterr().out

    def test_healthy_git_command_untouched_live(self):
        from hermes_cli import update_cmd

        git_cmd = ["git", "-c", "windows.appendAtomically=false"]
        assert update_cmd._ensure_non_trampoline_git(git_cmd) == git_cmd
