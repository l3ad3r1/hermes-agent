"""Behavioral coverage for required Node dependency installation."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


_DEFAULT_NPM_BODY = """#!/bin/sh
if [ "${1:-}" = "--version" ]; then
    echo 12.0.0
    exit 0
fi
printf '%s\\n' "$PWD" >> "$NPM_CALLS"
if [ -n "${NPM_FAIL_DIRECTORY:-}" ] && [ "$PWD" = "$NPM_FAIL_DIRECTORY" ]; then
    echo "simulated npm lifecycle failure" >&2
    exit 37
fi
exit 0
"""


def _run_node_deps_stage(
    tmp_path: Path,
    *,
    fail_directory: str | None,
    npm_body: str | None = None,
    venv_python_body: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path, list[str]]:
    install_dir = tmp_path / "install"
    tui_dir = install_dir / "ui-tui"
    bin_dir = tmp_path / "bin"
    hermes_home = tmp_path / "home"
    managed_bin = hermes_home / "bin"
    npm_calls = tmp_path / "npm-calls"

    tui_dir.mkdir(parents=True)
    bin_dir.mkdir()
    managed_bin.mkdir(parents=True)
    (install_dir / "package.json").write_text(
        '{"name":"installer-regression-probe","private":true}\n',
        encoding="utf-8",
    )
    (tui_dir / "package.json").write_text(
        '{"name":"tui-regression-probe","private":true}\n',
        encoding="utf-8",
    )
    _write_executable(bin_dir / "node", "#!/bin/sh\necho v26.0.0\n")
    _write_executable(bin_dir / "npm", npm_body or _DEFAULT_NPM_BODY)
    _write_executable(managed_bin / "uv", "#!/bin/sh\necho 'uv probe'\n")

    if venv_python_body is not None:
        venv_bin = install_dir / "venv" / "bin"
        venv_bin.mkdir(parents=True)
        _write_executable(venv_bin / "python", venv_python_body)

    env = os.environ.copy()
    env.update(
        {
            "HERMES_HOME": str(hermes_home),
            "HERMES_INSTALL_DIR": str(install_dir),
            "NPM_CALLS": str(npm_calls),
            "NPM_FAIL_DIRECTORY": fail_directory or "",
            "PATH": f"{bin_dir}:{env['PATH']}",
        }
    )
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        [
            "bash",
            str(INSTALL_SH),
            "--stage",
            "node-deps",
            "--json",
            "--skip-browser",
            "--skip-computer-use",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    calls = npm_calls.read_text(encoding="utf-8").splitlines()
    return proc, install_dir, calls


def _stage_result(proc: subprocess.CompletedProcess[str]) -> dict[str, object]:
    return json.loads(proc.stdout.splitlines()[-1])


def test_root_node_dependency_failure_is_fatal(tmp_path: Path) -> None:
    install_dir = tmp_path / "install"
    proc, actual_install_dir, calls = _run_node_deps_stage(
        tmp_path,
        fail_directory=str(install_dir),
    )

    assert actual_install_dir == install_dir
    assert proc.returncode != 0
    assert _stage_result(proc) == {
        "ok": False,
        "stage": "node-deps",
        "skipped": False,
        "reason": "exit code 1",
    }
    assert calls == [str(install_dir)]
    assert "Node.js dependencies installed" not in proc.stdout
    assert "TUI dependencies installed" not in proc.stdout
    assert not (install_dir / "node_modules").exists()


def test_tui_node_dependency_failure_is_fatal(tmp_path: Path) -> None:
    install_dir = tmp_path / "install"
    tui_dir = install_dir / "ui-tui"
    proc, _, calls = _run_node_deps_stage(
        tmp_path,
        fail_directory=str(tui_dir),
    )

    assert proc.returncode != 0
    assert _stage_result(proc)["ok"] is False
    assert calls == [str(install_dir), str(tui_dir)]
    assert "Node.js dependencies installed" in proc.stdout
    assert "TUI dependencies installed" not in proc.stdout


def test_node_dependency_success_remains_successful(tmp_path: Path) -> None:
    proc, install_dir, calls = _run_node_deps_stage(
        tmp_path,
        fail_directory=None,
    )

    assert proc.returncode == 0, proc.stderr
    assert _stage_result(proc) == {
        "ok": True,
        "stage": "node-deps",
        "skipped": False,
    }
    assert calls == [str(install_dir), str(install_dir / "ui-tui")]
    assert "Node.js dependencies installed" in proc.stdout
    assert "TUI dependencies installed" in proc.stdout


def test_root_ebadengine_triggers_one_repair_and_retry(tmp_path: Path) -> None:
    """An npm engine mismatch gets the same one repair+retry `hermes update` has.

    The installer re-run path used to hard-fail on a raw EBADENGINE (#85297
    made a failed npm install fatal but only the updater got the recovery
    rung). The bridge to hermes_cli.npm_engine must fire exactly once — repair,
    retry, and no loop.
    """
    install_dir = tmp_path / "install"
    count_file = tmp_path / "ebadengine-count"

    # EBADENGINE on the first install at the root only; every later call is fine
    # — the model for "npm was upgraded in place and is now in range".
    npm_body = """#!/bin/sh
if [ "${1:-}" = "--version" ]; then echo 12.0.0; exit 0; fi
printf '%s\\n' "$PWD" >> "$NPM_CALLS"
if [ "$PWD" = "$NPM_EBADENGINE_DIRECTORY" ]; then
    n=$(cat "$NPM_EBADENGINE_COUNT" 2>/dev/null || echo 0)
    n=$((n + 1)); printf '%s' "$n" > "$NPM_EBADENGINE_COUNT"
    if [ "$n" -eq 1 ]; then
        echo "npm error code EBADENGINE" >&2
        echo 'npm error notsup Required: {"npm":">=12.0.0"}' >&2
        exit 1
    fi
fi
exit 0
"""
    # Stand-in for `python -m hermes_cli.npm_engine`: on an EBADENGINE log it
    # echoes the npm to retry with (here the same one, now "in range"); any
    # other python call the stage makes is a no-op.
    venv_python_body = """#!/bin/sh
case " $* " in
  *" hermes_cli.npm_engine "*)
    log=$(cat)
    case "$log" in
      *EBADENGINE*) command -v npm; exit 0 ;;
      *) exit 1 ;;
    esac
    ;;
esac
exit 0
"""

    proc, actual_install_dir, calls = _run_node_deps_stage(
        tmp_path,
        fail_directory=None,
        npm_body=npm_body,
        venv_python_body=venv_python_body,
        extra_env={
            "NPM_EBADENGINE_DIRECTORY": str(install_dir),
            "NPM_EBADENGINE_COUNT": str(count_file),
        },
    )

    assert proc.returncode == 0, proc.stderr
    assert _stage_result(proc)["ok"] is True
    # first attempt (EBADENGINE) + one retry at the root, then the TUI install
    assert calls == [
        str(install_dir),
        str(install_dir),
        str(install_dir / "ui-tui"),
    ]
    assert "Node.js dependencies installed" in proc.stdout
    assert count_file.read_text().strip() == "2"  # exactly one retry, no loop


def test_non_engine_failure_does_not_invoke_the_repair_bridge(tmp_path: Path) -> None:
    """A plain npm failure must not fork the Python repair — only EBADENGINE does."""
    install_dir = tmp_path / "install"
    bridge_calls = tmp_path / "bridge-calls"
    venv_python_body = f"""#!/bin/sh
printf 'called\\n' >> {bridge_calls}
exit 1
"""

    proc, _, calls = _run_node_deps_stage(
        tmp_path,
        fail_directory=str(install_dir),
        venv_python_body=venv_python_body,
    )

    assert proc.returncode != 0
    assert calls == [str(install_dir)]  # no retry
    assert not bridge_calls.exists()  # grep gate skipped the fork
    # #87340's captured npm output still surfaces on the failure path
    assert "simulated npm lifecycle failure" in (proc.stdout + proc.stderr)
