import shutil
import subprocess

import pytest

from drop.store import remote_command

SCRIPTS = [
    "mkdir -p d && { test -e d/index.html || : > d/index.html; }; ls d",
    "for f in a b; do test -n \"$f\" && echo \"$f\"; done",
    "printf '\\0%s\\0' \"it's\"; echo",
]


@pytest.mark.parametrize("shell", ["sh", "bash", "fish", "tcsh"])
@pytest.mark.parametrize("script", SCRIPTS)
def test_remote_command_any_login_shell(shell, script, tmp_path):
    if not shutil.which(shell):
        pytest.skip(f"{shell} not installed")
    want = subprocess.run(["sh", "-c", script], cwd=tmp_path, capture_output=True).stdout
    shutil.rmtree(tmp_path / "d", ignore_errors=True)
    got = subprocess.run(
        [shell, "-c", remote_command(script)], cwd=tmp_path, capture_output=True
    )
    assert got.returncode == 0, got.stderr
    assert got.stdout == want
