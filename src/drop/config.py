"""Configuration from ~/.config/drop/config.toml, environment and options.

Layered last-one-wins: config file, then environment (DROP_TARGET,
DROP_TTL), then command line options.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path


class ConfigError(Exception):
    pass


def xdg_dir(kind: str) -> Path:
    var, default = {
        "config": ("XDG_CONFIG_HOME", "~/.config"),
        "state": ("XDG_STATE_HOME", "~/.local/state"),
    }[kind]
    return Path(os.environ.get(var) or default).expanduser() / "drop"


@dataclass
class Target:
    name: str
    #: A local directory, or "host:path" reached by ssh and rsync.
    path: str
    #: The URL at which path is served, no trailing slash.
    url: str
    #: AuthUserFile, as the web server sees it, for --auth drops.
    htpasswd: str = ""

    @property
    def host(self) -> str:
        """The ssh host, '' if path is local."""
        head, sep, _ = self.path.partition(":")
        return head if sep and "/" not in head else ""

    @property
    def dir(self) -> str:
        if self.host:
            return self.path.partition(":")[2]
        return str(Path(self.path).expanduser())


@dataclass
class Config:
    target: str = "local"
    ttl: str = "7d"
    hash: str = "dir"
    index: str = "empty"
    strip: bool = True
    targets: dict[str, Target] = field(default_factory=dict)

    def get(self, name: str | None = None) -> Target:
        name = name or self.target
        try:
            return self.targets[name]
        except KeyError:
            known = ", ".join(self.targets) or "none, see drop -h"
            raise ConfigError(f"unknown target {name!r}, known: {known}") from None


DEFAULT_TARGETS = {
    "local": {"path": "~/public_html/drop", "url": "http://localhost/~{user}/drop"},
}


def load(path: Path | None = None, env: dict | None = None) -> Config:
    env = os.environ if env is None else env
    path = path or Path(env.get("DROP_CONFIG") or xdg_dir("config") / "config.toml")
    data = {}
    if path.exists():
        try:
            data = tomllib.loads(path.read_text())
        except tomllib.TOMLDecodeError as err:
            raise ConfigError(f"{path}: {err}") from None
    cfg = Config()
    cfg.target = data.get("default_target", cfg.target)
    for key in ("ttl", "hash", "index", "strip"):
        if key in data:
            setattr(cfg, key, data[key])
    cfg.target = env.get("DROP_TARGET") or cfg.target
    cfg.ttl = env.get("DROP_TTL") or cfg.ttl
    user = os.environ.get("USER", "")
    for name, tcfg in (data.get("target") or DEFAULT_TARGETS).items():
        try:
            cfg.targets[name] = Target(
                name=name,
                path=tcfg["path"],
                url=tcfg["url"].format(user=user).rstrip("/"),
                htpasswd=tcfg.get("htpasswd", ""),
            )
        except KeyError as err:
            raise ConfigError(f"target {name} lacks {err}") from None
    return cfg


TTL = re.compile(r"^(\d+)\s*([mhdw])$")
UNITS = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


def parse_ttl(text: str) -> timedelta | None:
    """Parse eg 30m, 12h, 7d, 2w.  "never" or "0" means no expiry, None."""
    text = text.strip().lower()
    if text in ("never", "0", "inf", "none"):
        return None
    match = TTL.match(text)
    if not match:
        raise ConfigError(f"bad ttl {text!r}, give eg 30m, 12h, 7d, 2w or never")
    return timedelta(**{UNITS[match[2]]: int(match[1])})
