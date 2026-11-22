"""Arrange for "drop gc" to run periodically by systemd or cron."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

MARK = "# drop-managed"


def gc_command(targets: list[str]) -> str:
    exe = shutil.which("drop")
    cmd = [exe] if exe else [sys.executable, "-m", "drop"]
    args = []
    for name in targets:
        args += ["-t", name]
    return shlex.join([*cmd, "gc", *args])


def systemd(targets: list[str], when: str, unitdir: Path) -> dict[Path, str]:
    service = f"""\
{MARK}
[Unit]
Description=Remove expired drops

[Service]
Type=oneshot
Environment=PATH={os.environ.get("PATH", "")}
ExecStart={gc_command(targets)}
"""
    timer = f"""\
{MARK}
[Unit]
Description=Periodically remove expired drops

[Timer]
OnCalendar={when}
Persistent=true

[Install]
WantedBy=timers.target
"""
    return {unitdir / "drop-gc.service": service, unitdir / "drop-gc.timer": timer}


def cron_line(targets: list[str], when: str) -> str:
    path = shlex.quote(os.environ.get("PATH", ""))
    return f"{when} PATH={path} {gc_command(targets)} >/dev/null 2>&1 {MARK}"


def install_cron(line: str) -> None:
    proc = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    old = proc.stdout if proc.returncode == 0 else ""
    keep = [l for l in old.splitlines() if not l.endswith(MARK)]
    subprocess.run(["crontab", "-"], input="\n".join([*keep, line]) + "\n", text=True, check=True)


def write(units: dict[Path, str], force: bool = False) -> list[Path]:
    for path in units:
        if path.exists() and MARK not in path.read_text() and not force:
            raise FileExistsError(f"{path} exists and is not managed by drop")
    for path, text in units.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return list(units)
