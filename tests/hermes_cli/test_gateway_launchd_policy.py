"""Pure plist policy regression for the previously deployed KESTREL I/O fix."""

import json
import plistlib
import shlex
from pathlib import Path

from hermes_cli import _launchers, gateway, resource_limits


def test_interactive_io_policy_preserves_managed_launch_chain(tmp_path, monkeypatch):
    home = tmp_path / "profile with spaces"
    home.mkdir()
    project = tmp_path / "managed source"
    launcher = str(project / ".hermes/bin/hermes")
    python = str(tmp_path / "selected Python")
    working = tmp_path / "stable cwd"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(gateway, "PROJECT_ROOT", project)
    monkeypatch.setattr(gateway, "get_hermes_home", lambda: home)
    monkeypatch.setattr(gateway, "get_python_path", lambda: python)
    monkeypatch.setattr(gateway, "get_launchd_label", lambda: "ai.hermes.gateway-reviewer")
    monkeypatch.setattr(gateway, "_profile_arg", lambda: "--profile reviewer")
    monkeypatch.setattr(gateway, "_stable_service_working_dir", lambda: working)
    monkeypatch.setattr(gateway, "_build_service_path_dirs", lambda: ["/selected/bin"])
    monkeypatch.setattr(gateway, "_append_node_dir_for_service", lambda dirs: None)
    monkeypatch.setattr(gateway, "_gateway_run_command", lambda: ["unused", "--replace"])
    monkeypatch.setattr(_launchers, "resolve_store_python", lambda root: Path(python))
    monkeypatch.setattr(resource_limits, "configured_nofile_soft_limit", lambda: 4096)

    parsed = plistlib.loads(gateway.generate_launchd_plist().encode())
    assert parsed["ProcessType"] == "Interactive"
    assert parsed["LowPriorityIO"] is False
    assert parsed["Label"] == "ai.hermes.gateway-reviewer"
    assert parsed["WorkingDirectory"] == str(working)
    assert parsed["EnvironmentVariables"] == {
        "PATH": "/selected/bin:/usr/bin:/bin",
        "HERMES_HOME": str(home),
        "HERMES_SUPERVISED_CHILD": "1",
    }
    assert parsed["KeepAlive"] == {"SuccessfulExit": False}
    assert parsed["RunAtLoad"] is True
    assert parsed["ThrottleInterval"] == 30
    assert parsed["ExitTimeOut"] == 60
    assert parsed["SoftResourceLimits"] == {"NumberOfFiles": 4096}
    stdout = str(home / "logs/gateway.log")
    stderr = str(home / "logs/gateway.error.log")
    assert parsed["StandardOutPath"] == stdout
    assert parsed["StandardErrorPath"] == stderr
    args = parsed["ProgramArguments"]
    assert args[:4] == ["/usr/bin/osascript", "-l", "JavaScript", "-e"]
    assert len(args) == 5
    start = args[4].index("$.system(") + len("$.system(")
    shell, _ = json.JSONDecoder().raw_decode(args[4], start)
    assert shlex.split(shell) == [
        "exec", launcher, "--run-module", "hermes_cli.stderr_timestamp",
        "--error-log", stderr, "--", launcher, "--profile", "reviewer",
        "gateway", "run", "--external-supervisor", ">>", stdout, "2>>", stderr,
    ]
    assert "$.exit(" in args[4] and "status >> 8" in args[4]
