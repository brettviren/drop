"""Drop entries: putting, listing, removing and expiring them.

An entry is one or more files shared together.  Its id names either a
directory holding the files (--hash dir, or none with a readable name) or,
for a single file, the file itself (--hash file, like the old "see").
"""

from __future__ import annotations

import getpass
import hashlib
import json
import shutil
import socket
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import passwd
from .config import Target
from .store import SIDECAR, open_store

HASHES = ("dir", "file", "none")
INDEXES = ("empty", "open")
IMAGES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".heic"}

HTACCESS_AUTH = """\
AuthType Basic
AuthName "drop {id}"
AuthUserFile {htpasswd}
Require {require}
# Files sections merge after directories, so a FilesMatch granting files
# higher up (see GRANT) would otherwise override the Require above, and a
# server's "Satisfy Any" would let a granted host in without a login.
<FilesMatch "">
  Require {require}
  <IfModule mod_access_compat.c>
    Satisfy All
  </IfModule>
</FilesMatch>
<IfModule mod_access_compat.c>
  Satisfy All
</IfModule>
"""

GRANT_MARK = "# Written by drop: admit files"
GRANT = GRANT_MARK + """ the server would not, never dotfiles
# (.htaccess, the .htdrop-*.json sidecars).  Both 2.4 Require and the old
# Allow, which a mod_access_compat Deny needs.
<FilesMatch "^[^.]">
  Require all granted
  <IfModule mod_access_compat.c>
    Order deny,allow
    Allow from all
  </IfModule>
</FilesMatch>
"""


class DropError(Exception):
    pass


@dataclass
class Entry:
    id: str
    target: str
    url: str
    files: list[str]
    kind: str  # "dir" or "file"
    created: str
    expires: str | None = None
    note: str = ""
    index: str = "empty"
    auth: bool = False
    owner: str = ""
    urls: list[str] = field(default_factory=list)
    #: Users "Require user" admits, empty for any valid user.
    users: list[str] = field(default_factory=list)
    #: Passwords of users put made for this entry, never saved.
    passwords: dict[str, str] = field(default_factory=dict, repr=False)

    @property
    def sidecar(self) -> str:
        return SIDECAR.format(id=self.id)

    def expiry(self) -> datetime | None:
        return datetime.fromisoformat(self.expires) if self.expires else None

    def expired(self, now: datetime | None = None) -> bool:
        exp = self.expiry()
        return exp is not None and exp <= (now or datetime.now(timezone.utc))

    def to_json(self) -> str:
        data = asdict(self)
        del data["passwords"]
        return json.dumps(data, separators=(",", ":"))

    @classmethod
    def from_dict(cls, data: dict) -> "Entry":
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


def digest(paths: list[Path]) -> str:
    """A content hash of files (and directory trees), in order."""
    hasher = hashlib.sha256()
    for path in paths:
        files = sorted(p for p in path.rglob("*") if p.is_file()) if path.is_dir() else [path]
        for file in files:
            hasher.update(str(file.relative_to(path.parent)).encode())
            with open(file, "rb") as fp:
                for chunk in iter(lambda: fp.read(1 << 20), b""):
                    hasher.update(chunk)
    return hasher.hexdigest()[:16]


def strip_metadata(path: Path) -> bool:
    """Orient and strip EXIF from an image copy in place, if mogrify exists."""
    if path.suffix.lower() not in IMAGES or not shutil.which("mogrify"):
        return False
    subprocess.run(["mogrify", "-auto-orient", "-strip", str(path)], check=True)
    return True


def _stage(sources: list[Path], tmp: Path, strip: bool) -> list[Path]:
    staged = []
    for src in sources:
        dest = tmp / src.name
        if src.is_dir():
            shutil.copytree(src, dest)
            if strip:
                for p in dest.rglob("*"):
                    if p.is_file():
                        strip_metadata(p)
        else:
            shutil.copyfile(src, dest)
            if strip:
                strip_metadata(dest)
        staged.append(dest)
    return staged


def grant(store, target: Target) -> bool:
    """Write the top GRANT .htaccess unless one not made by drop is there.
    Return True if the drop directory has drop's grant."""
    path = f"{target.dir}/.htaccess"
    have = store.read_file(path)
    if have and not have.startswith(GRANT_MARK):
        return False
    if have != GRANT:
        store.write(".htaccess", GRANT)
    return True


def put(
    target: Target,
    sources: list[Path],
    ttl: timedelta | None,
    hash: str = "dir",
    name: str = "",
    index: str = "empty",
    auth: bool = False,
    note: str = "",
    strip: bool = True,
    users: list[str] | None = None,
    now: datetime | None = None,
) -> Entry:
    """Drop sources at a new URL.  Users imply auth and those missing from
    the htpasswd are added with generated passwords, given in the entry."""
    users = [passwd.check_user(u) for u in users or []]
    auth = auth or bool(users)
    if not sources:
        raise DropError("nothing to drop")
    for src in sources:
        if not src.exists():
            raise DropError(f"no such file: {src}")
    if hash not in HASHES or index not in INDEXES:
        raise DropError(f"hash must be one of {HASHES}, index one of {INDEXES}")
    if hash == "file" and (len(sources) != 1 or sources[0].is_dir()):
        raise DropError("--hash file needs exactly one file")
    if hash == "file" and (auth or index == "open"):
        raise DropError("--hash file can not have --auth or --index open, use --hash dir")
    if hash == "none" and not name:
        raise DropError("--hash none needs a --name")
    if auth and not target.htpasswd:
        raise DropError(f"target {target.name} has no htpasswd for --auth")

    now = now or datetime.now(timezone.utc)
    store = open_store(target)
    with tempfile.TemporaryDirectory(prefix="drop-") as tmpname:
        staged = _stage(sources, Path(tmpname), strip)
        dig = digest(staged)
        if hash == "file":
            src = staged[0]
            ident = dig[:12] + "".join(src.suffixes)
            kind = "file"
        else:
            ident = name if hash == "none" else (f"{dig[:12]}-{name}" if name else dig[:12])
            kind = "dir"
        if "/" in ident or ident.startswith("."):
            raise DropError(f"bad drop name {ident!r}")
        entry = Entry(
            id=ident,
            target=target.name,
            url=f"{target.url}/{ident}" + ("/" if kind == "dir" else ""),
            files=[s.name for s in staged],
            kind=kind,
            created=now.isoformat(timespec="seconds"),
            expires=(now + ttl).isoformat(timespec="seconds") if ttl else None,
            note=note,
            index=index,
            auth=auth,
            owner=f"{getpass.getuser()}@{socket.gethostname()}",
            users=users,
        )
        if kind == "file":
            entry.urls = [entry.url]
        else:
            entry.urls = [entry.url + s.name + ("/" if s.is_dir() else "") for s in staged]
            box = Path(tmpname) / "__entry__"
            box.mkdir()
            for s in staged:
                s.rename(box / s.name)
            if index == "empty":
                (box / "index.html").write_text("")
            # "open" relies on the server allowing listings, as Apache's
            # userdir does by default.  Setting Options in .htaccess would
            # need AllowOverride Options, which is often not granted.
            if auth:
                (box / ".htaccess").write_text(
                    HTACCESS_AUTH.format(
                        id=ident, htpasswd=target.htpasswd,
                        require="user " + " ".join(users) if users else "valid-user",
                    )
                )
            staged = [box]
        store.ensure()
        if target.grant:
            grant(store, target)
        entry.passwords = passwd.ensure(target, users)
        store.upload(staged[0], ident)
        store.write(entry.sidecar, entry.to_json() + "\n")
    return entry


def entries(target: Target) -> list[Entry]:
    return [Entry.from_dict(d) for d in open_store(target).sidecars()]


def remove(target: Target, ids: list[str]) -> list[Entry]:
    have = {e.id: e for e in entries(target)}
    missing = [i for i in ids if i not in have]
    if missing:
        raise DropError(f"no such drop in {target.name}: {', '.join(missing)}")
    gone = [have[i] for i in ids]
    _remove(target, gone)
    return gone


def _remove(target: Target, gone: list[Entry]) -> None:
    rels = []
    for entry in gone:
        rels += [entry.id, entry.sidecar]
    open_store(target).remove(rels)


def gc(target: Target, now: datetime | None = None, dry_run: bool = False) -> list[Entry]:
    """Remove the expired entries of a target, return them."""
    gone = [e for e in entries(target) if e.expired(now)]
    if gone and not dry_run:
        _remove(target, gone)
    return gone


def extend(target: Target, ident: str, ttl: timedelta | None, now: datetime | None = None) -> Entry:
    """Give entry IDENT a new time to live counted from now."""
    have = {e.id: e for e in entries(target)}
    if ident not in have:
        raise DropError(f"no such drop in {target.name}: {ident}")
    entry = have[ident]
    now = now or datetime.now(timezone.utc)
    entry.expires = (now + ttl).isoformat(timespec="seconds") if ttl else None
    open_store(target).write(entry.sidecar, entry.to_json() + "\n")
    return entry
