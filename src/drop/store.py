"""Where drops live: a local directory or one reached by ssh.

Both present the same small set of operations on the drop directory.
Every entry is described by a sidecar ``.htdrop-<id>.json`` in the top of
the drop directory.  Apache refuses to serve ``.ht*`` files by default,
so sidecars stay private while letting anything with access to the
directory (this host, or the web server host itself) list and expire
entries without any other record.
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
from pathlib import Path

from .config import Target

SIDECAR = ".htdrop-{id}.json"


class StoreError(Exception):
    pass


class LocalStore:
    def __init__(self, root: str) -> None:
        self.root = Path(root)

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        index = self.root / "index.html"
        if not index.exists():
            index.write_text("")

    def upload(self, staged: Path, rel: str) -> None:
        dest = self.root / rel
        if staged.is_dir():
            shutil.copytree(staged, dest, dirs_exist_ok=True)
        else:
            shutil.copyfile(staged, dest)

    def write(self, rel: str, text: str) -> None:
        (self.root / rel).write_text(text)

    def sidecars(self) -> list[dict]:
        out = []
        for path in sorted(self.root.glob(SIDECAR.format(id="*"))):
            try:
                out.append(json.loads(path.read_text()))
            except (OSError, json.JSONDecodeError):
                continue
        return out

    def read_file(self, path: str) -> str:
        """Text of a file named outside the drop directory, '' if missing."""
        path = Path(path).expanduser()
        return path.read_text() if path.exists() else ""

    def write_file(self, path: str, text: str) -> None:
        """Rewrite a file named outside the drop directory.  In place, not
        by rename, to keep its mode, owner and any symlink or bind mount."""
        Path(path).expanduser().write_text(text)

    def htaccess(self, root: str) -> dict[str, str]:
        """Every .htaccess under root, path to text."""
        root = Path(root).expanduser()
        out = {}
        for path in sorted(root.rglob(".htaccess")) if root.is_dir() else []:
            try:
                out[str(path)] = path.read_text()
            except OSError:
                continue
        return out

    def remove(self, rels: list[str]) -> None:
        for rel in rels:
            path = self.root / rel
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            elif path.exists() or path.is_symlink():
                path.unlink()


class SshStore:
    def __init__(self, host: str, root: str) -> None:
        self.host = host
        self.root = root.rstrip("/") or "."

    def _sh(self, script: str, text: str | None = None) -> str:
        proc = subprocess.run(
            ["ssh", self.host, script], input=text, capture_output=True, text=True
        )
        if proc.returncode:
            raise StoreError(f"ssh {self.host}: {proc.stderr.strip()}")
        return proc.stdout

    def _path(self, rel: str) -> str:
        return shlex.quote(f"{self.root}/{rel}")

    def ensure(self) -> None:
        root = shlex.quote(self.root)
        self._sh(f"mkdir -p {root} && {{ test -e {root}/index.html || : > {root}/index.html; }}")

    def upload(self, staged: Path, rel: str) -> None:
        src = f"{staged}/" if staged.is_dir() else str(staged)
        dest = f"{self.host}:{self.root}/{rel}" + ("/" if staged.is_dir() else "")
        proc = subprocess.run(["rsync", "-a", src, dest], capture_output=True, text=True)
        if proc.returncode:
            raise StoreError(f"rsync to {dest}: {proc.stderr.strip()}")

    def write(self, rel: str, text: str) -> None:
        self._sh(f"cat > {self._path(rel)}", text)

    def sidecars(self) -> list[dict]:
        pattern = SIDECAR.format(id="*")
        out = self._sh(
            f"cd {shlex.quote(self.root)} 2>/dev/null || exit 0; "
            f"for f in {pattern}; do test -f \"$f\" && cat \"$f\" && echo; done"
        )
        entries = []
        for line in out.splitlines():
            if line.strip():
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return entries

    def read_file(self, path: str) -> str:
        path = shlex.quote(path)
        return self._sh(f"test -e {path} || exit 0; cat {path}")

    def write_file(self, path: str, text: str) -> None:
        # cat into the existing file keeps its mode and owner.
        self._sh(f"cat > {shlex.quote(path)}", text)

    def htaccess(self, root: str) -> dict[str, str]:
        out = self._sh(
            f"cd {shlex.quote(root)} 2>/dev/null || exit 0; "
            "find \"$PWD\" -name .htaccess -type f | sort | "
            "while read -r f; do printf '\\0%s\\0' \"$f\"; cat \"$f\"; done"
        )
        parts = out.split("\0")[1:]
        return dict(zip(parts[0::2], parts[1::2]))

    def remove(self, rels: list[str]) -> None:
        if rels:
            self._sh("rm -rf -- " + " ".join(self._path(r) for r in rels))


def open_store(target: Target) -> LocalStore | SshStore:
    if target.host:
        return SshStore(target.host, target.dir)
    return LocalStore(target.dir)
