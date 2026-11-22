"""Basic auth users: the target's htpasswd file and the .htaccess using it.

The htpasswd file lives where the web server is, so it is read and
written through the target's store, as is the scan of .htaccess files
that finds which users are still named by a ``Require user`` line.
Passwords are hashed here (bcrypt by htpasswd, else apr1 by openssl)
so only the hash travels to a remote host.
"""

from __future__ import annotations

import re
import secrets
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .config import Target
from .store import open_store

USER = re.compile(r"^[^\s:#]+$")


class PasswdError(Exception):
    pass


def _file(target: Target) -> str:
    if not target.htpasswd:
        raise PasswdError(f"target {target.name} has no htpasswd")
    return target.htpasswd


def check_user(user: str) -> str:
    if not USER.match(user):
        raise PasswdError(f"bad user name {user!r}")
    return user


def generate() -> str:
    return secrets.token_urlsafe(12)


def hash_password(password: str) -> str:
    if shutil.which("htpasswd"):
        cmd = ["htpasswd", "-niB", "user"]
    elif shutil.which("openssl"):
        cmd = ["openssl", "passwd", "-apr1", "-stdin"]
    else:
        raise PasswdError("need htpasswd or openssl to hash passwords")
    proc = subprocess.run(cmd, input=password, capture_output=True, text=True)
    if proc.returncode:
        raise PasswdError(f"{cmd[0]}: {proc.stderr.strip()}")
    return proc.stdout.strip().removeprefix("user:")


def parse(text: str) -> dict[str, str]:
    """User to hash, from htpasswd text."""
    out = {}
    for line in text.splitlines():
        user, sep, hashed = line.strip().partition(":")
        if sep and not user.startswith("#"):
            out[user] = hashed
    return out


def _edit(text: str, set_: dict[str, str], drop: set[str]) -> str:
    lines, seen = [], set()
    for line in text.splitlines():
        user, sep, _ = line.strip().partition(":")
        if sep and user in drop:
            continue
        if sep and user in set_:
            if user in seen:
                continue
            line = f"{user}:{set_[user]}"
            seen.add(user)
        lines.append(line)
    lines += [f"{u}:{h}" for u, h in set_.items() if u not in seen]
    return "\n".join(lines) + "\n" if lines else ""


def users(target: Target) -> list[str]:
    return list(parse(open_store(target).read_file(_file(target))))


def add(target: Target, user: str, password: str) -> bool:
    """Set USER's password, return True if USER is new."""
    path, store = _file(target), open_store(target)
    text = store.read_file(path)
    new = check_user(user) not in parse(text)
    store.write_file(path, _edit(text, {user: hash_password(password)}, set()))
    return new


def remove(target: Target, names: list[str]) -> list[str]:
    """Remove users, return those that were present."""
    path, store = _file(target), open_store(target)
    text = store.read_file(path)
    gone = [u for u in names if u in parse(text)]
    if gone:
        store.write_file(path, _edit(text, {}, set(gone)))
    return gone


def ensure(target: Target, names: list[str]) -> dict[str, str]:
    """Add the users that are missing with generated passwords, return those."""
    have = set(users(target))
    made = {}
    for user in names:
        if check_user(user) not in have and user not in made:
            made[user] = generate()
            add(target, user, made[user])
    return made


@dataclass
class Usage:
    """Which .htaccess files use the target's htpasswd, and how."""
    #: user to the .htaccess files naming it in "Require user".
    named: dict[str, list[str]] = field(default_factory=dict)
    #: .htaccess files with "Require valid-user", which admit any user.
    anyone: list[str] = field(default_factory=list)


def _same(a: str, b: str) -> bool:
    return Path(a).expanduser() == Path(b).expanduser()


def usage_of(htaccess: dict[str, str], htpasswd: str) -> Usage:
    use = Usage()
    for path, text in htaccess.items():
        authfile, require = None, []
        for line in text.splitlines():
            words = line.split()
            if len(words) >= 2 and words[0].lower() == "authuserfile":
                authfile = words[1].strip("\"'")
            elif len(words) >= 2 and words[0].lower() == "require":
                require.append(words[1:])
        if authfile is None or not _same(authfile, htpasswd):
            continue
        for words in require:
            if words[0].lower() == "valid-user":
                use.anyone.append(path)
            elif words[0].lower() == "user":
                for user in words[1:]:
                    use.named.setdefault(user, []).append(path)
    return use


def default_root(target: Target) -> str:
    """Where to look for .htaccess: the target's, or its drop dir's parent."""
    if target.htaccess_root:
        return target.htaccess_root
    return str(Path(target.dir).parent)


def usage(target: Target, root: str | None = None) -> Usage:
    store = open_store(target)
    return usage_of(store.htaccess(root or default_root(target)), _file(target))


def unreferenced(target: Target, use: Usage) -> list[str]:
    """Users in htpasswd that no "Require user" line names."""
    return [u for u in users(target) if u not in use.named]
