"""Command line entry point.  Argument parsing, output and exit only."""

from __future__ import annotations

import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import click

from . import __version__, drops, passwd, setup as setting
from .config import Config, ConfigError, load, parse_ttl
from .store import StoreError

CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"], "max_content_width": 96}
ERRORS = (ConfigError, drops.DropError, passwd.PasswdError, StoreError, FileExistsError, OSError)

pass_config = click.make_pass_decorator(Config)


def fail(err: Exception) -> None:
    click.secho(f"drop: {err}", fg="red", err=True)
    sys.exit(1)


@click.group(context_settings=CONTEXT_SETTINGS, no_args_is_help=True)
@click.option(
    "-c", "--config", "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Configuration file instead of ~/.config/drop/config.toml.",
)
@click.version_option(__version__, "-V", "--version")
@click.pass_context
def cli(ctx: click.Context, config_path: Path | None) -> None:
    """Share files at web URLs that expire.

    A target is a directory served by a web server, local or reached by
    ssh ("host:path"), with the URL that serves it.  Each drop is recorded
    by a .htdrop-<id>.json beside it which the web server does not serve,
    so any host that can reach the directory can list and expire drops.
    """
    try:
        ctx.obj = load(config_path)
    except ConfigError as err:
        fail(err)


def _copy(text: str) -> bool:
    for cmd in (["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "-ib"]):
        if shutil.which(cmd[0]):
            return subprocess.run(cmd, input=text, text=True).returncode == 0
    return False


@cli.command(no_args_is_help=True)
@click.argument("files", nargs=-1, required=True, type=click.Path(exists=True, path_type=Path))
@click.option("-t", "--target", help="Target name.  Default from config.")
@click.option("--ttl", help="Time to live: 30m, 12h, 7d, 2w or never.  Default from config.")
@click.option("--hash", "hashing", type=click.Choice(drops.HASHES), help="Hash names a directory (dir), the file itself (file), or nothing (none, needs --name).")
@click.option("-n", "--name", default="", help="Readable name, appended to a hash dir or used alone with --hash none.")
@click.option("--index", type=click.Choice(drops.INDEXES), help="empty: an empty index.html blocks listing.  open: allow listing.")
@click.option("-a", "--auth", is_flag=True, help="Require a password (.htaccess basic auth).")
@click.option("-u", "--user", "users", multiple=True, help="Admit only this user, may repeat, implies --auth.  A new user gets a generated password.")
@click.option("-m", "--note", default="", help="Why or for whom, kept with the drop.")
@click.option("--strip/--no-strip", default=None, help="Strip image metadata (needs mogrify).")
@click.option("-y", "--copy", "copy", is_flag=True, help="Copy the URL to the clipboard.")
@pass_config
def put(cfg: Config, files, target, ttl, hashing, name, index, auth, users, note, strip, copy):
    """Drop FILES (or directories) at a new URL and print it."""
    try:
        entry = drops.put(
            cfg.get(target), list(files), parse_ttl(ttl or cfg.ttl),
            hash=hashing or cfg.hash, name=name, index=index or cfg.index,
            auth=auth, note=note, strip=cfg.strip if strip is None else strip,
            users=list(users),
        )
    except ERRORS as err:
        fail(err)
    urls = entry.urls if len(entry.urls) == 1 or entry.index != "open" else [entry.url]
    for url in urls:
        click.echo(url)
    for user, password in entry.passwords.items():
        click.echo(f"new user {user} password {password}", err=True)
    if copy and not _copy("\n".join(urls)):
        click.echo("no clipboard tool found", err=True)


def _left(entry: drops.Entry, now: datetime) -> str:
    exp = entry.expiry()
    if exp is None:
        return "never"
    secs = (exp - now).total_seconds()
    if secs <= 0:
        return "expired"
    for unit, size in (("w", 604800), ("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return f"{int(secs // size)}{unit}"
    return "<1m"


@cli.command("ls")
@click.option("-t", "--target", "targets", multiple=True, help="Target name, may repeat.  Default from config.")
@click.option("-A", "--all", "every", is_flag=True, help="All configured targets.")
@click.option("-l", "--long", "long_", is_flag=True, help="Show files, note and owner.")
@pass_config
def ls_(cfg: Config, targets, every, long_):
    """List drops with time left."""
    now = datetime.now(timezone.utc)
    names = list(cfg.targets) if every else (list(targets) or [cfg.target])
    try:
        for name in names:
            for e in sorted(drops.entries(cfg.get(name)), key=lambda e: e.created):
                flags = ("A" if e.auth else "-") + ("O" if e.index == "open" else "-")
                click.echo(f"{flags} {_left(e, now):>7}  {e.created[:10]}  {e.url}")
                if long_:
                    click.echo(f"      files: {', '.join(e.files)}  owner: {e.owner}"
                               + (f"\n      users: {', '.join(e.users)}" if e.users else "")
                               + (f"\n      note: {e.note}" if e.note else ""))
    except ERRORS as err:
        fail(err)


@cli.command(no_args_is_help=True)
@click.argument("ids", nargs=-1, required=True)
@click.option("-t", "--target", help="Target name.  Default from config.")
@pass_config
def rm(cfg: Config, ids, target):
    """Remove drops by id (the URL's last part)."""
    try:
        for entry in drops.remove(cfg.get(target), [i.rstrip("/").rsplit("/", 1)[-1] for i in ids]):
            click.echo(f"removed {entry.url}")
    except ERRORS as err:
        fail(err)


@cli.command()
@click.argument("ident")
@click.argument("ttl")
@click.option("-t", "--target", help="Target name.  Default from config.")
@pass_config
def extend(cfg: Config, ident, ttl, target):
    """Give drop IDENT a new TTL counted from now (or "never")."""
    try:
        entry = drops.extend(cfg.get(target), ident.rstrip("/").rsplit("/", 1)[-1], parse_ttl(ttl))
    except ERRORS as err:
        fail(err)
    click.echo(f"{entry.url} expires {entry.expires or 'never'}")


@cli.command()
@click.option("-t", "--target", "targets", multiple=True, help="Target name, may repeat.  Default all.")
@click.option("-n", "--dry-run", is_flag=True, help="Only say what would be removed.")
@click.option("-q", "--quiet", is_flag=True)
@pass_config
def gc(cfg: Config, targets, dry_run, quiet):
    """Remove expired drops."""
    failed = False
    for name in list(targets) or list(cfg.targets):
        try:
            for entry in drops.gc(cfg.get(name), dry_run=dry_run):
                if not quiet:
                    click.echo(("would remove " if dry_run else "removed ") + entry.url)
        except ERRORS as err:
            click.secho(f"drop: {name}: {err}", fg="red", err=True)
            failed = True
    sys.exit(1 if failed else 0)


@cli.group("passwd", no_args_is_help=True)
def passwd_():
    """Manage users of a target's htpasswd file.

    "drop put --user" names users in a drop's .htaccess, "Require user".
    A user no .htaccess names, under the target's htaccess_root (default
    the drop directory's parent), is unreferenced and "prune" offers to
    remove it.  An .htaccess with "Require valid-user" admits every user
    so removing any user also shuts them out of it.
    """


@passwd_.command("add", no_args_is_help=True)
@click.argument("user")
@click.option("-t", "--target", help="Target name.  Default from config.")
@click.option("-g", "--generate", is_flag=True, help="Generate and print a password instead of prompting.")
@pass_config
def passwd_add(cfg: Config, user, target, generate):
    """Add USER or set its password."""
    password = passwd.generate() if generate else click.prompt(
        f"password for {user}", hide_input=True, confirmation_prompt=True)
    try:
        new = passwd.add(cfg.get(target), user, password)
    except ERRORS as err:
        fail(err)
    click.echo(f"{'added' if new else 'updated'} {user}"
               + (f" password {password}" if generate else ""))


@passwd_.command("rm", no_args_is_help=True)
@click.argument("users", nargs=-1, required=True)
@click.option("-t", "--target", help="Target name.  Default from config.")
@pass_config
def passwd_rm(cfg: Config, users, target):
    """Remove USERS."""
    try:
        gone = passwd.remove(cfg.get(target), list(users))
    except ERRORS as err:
        fail(err)
    for user in users:
        click.echo(f"removed {user}" if user in gone else f"no user {user}")


def _usage(cfg: Config, target: str | None, root: str | None):
    tgt = cfg.get(target)
    return tgt, passwd.usage(tgt, root)


@passwd_.command("ls")
@click.option("-t", "--target", help="Target name.  Default from config.")
@click.option("-r", "--root", help="Directory to scan for .htaccess.  Default from target.")
@click.option("-l", "--long", "long_", is_flag=True, help="Show the .htaccess files naming each user.")
@pass_config
def passwd_ls(cfg: Config, target, root, long_):
    """List users and how many .htaccess files name them."""
    try:
        tgt, use = _usage(cfg, target, root)
        for user in passwd.users(tgt):
            where = use.named.get(user, [])
            click.echo(f"{len(where):>3}  {user}")
            if long_:
                for path in where:
                    click.echo(f"       {path}")
        if use.anyone:
            click.echo(f"any user is admitted by {len(use.anyone)} .htaccess with valid-user"
                       + "".join(f"\n       {p}" for p in use.anyone if long_))
    except ERRORS as err:
        fail(err)


@passwd_.command("prune")
@click.option("-t", "--target", help="Target name.  Default from config.")
@click.option("-r", "--root", help="Directory to scan for .htaccess.  Default from target.")
@click.option("-n", "--dry-run", is_flag=True, help="Only list unreferenced users.")
@click.option("-y", "--yes", is_flag=True, help="Remove all unreferenced users without asking.")
@pass_config
def passwd_prune(cfg: Config, target, root, dry_run, yes):
    """Offer to remove users that no .htaccess names."""
    try:
        tgt, use = _usage(cfg, target, root)
        stale = passwd.unreferenced(tgt, use)
        if not stale:
            click.echo("no unreferenced users")
            return
        if use.anyone:
            click.echo(f"note: {len(use.anyone)} .htaccess admit any user (valid-user), eg {use.anyone[0]}")
        if dry_run:
            for user in stale:
                click.echo(f"unreferenced {user}")
            return
        chosen = [u for u in stale if yes or click.confirm(f"remove unreferenced {u}?", default=not use.anyone)]
        for user in passwd.remove(tgt, chosen):
            click.echo(f"removed {user}")
    except ERRORS as err:
        fail(err)


@cli.command(no_args_is_help=True)
@click.argument("method", type=click.Choice(["systemd", "cron"]))
@click.option("-t", "--target", "targets", multiple=True, help="Target to collect, may repeat.  Default all.")
@click.option("-w", "--when", help="systemd OnCalendar or cron schedule.  Default hourly.")
@click.option("-p", "--print", "just_print", is_flag=True, help="Print what would be installed.")
@click.option("-f", "--force", is_flag=True, help="Overwrite files not made by drop.")
@click.option("--unitdir", type=click.Path(file_okay=False, path_type=Path), default=Path("~/.config/systemd/user").expanduser(), show_default=True)
def setup(method, targets, when, just_print, force, unitdir):
    """Arrange for "drop gc" to run periodically by systemd or cron."""
    try:
        if method == "cron":
            line = setting.cron_line(list(targets), when or "@hourly")
            if just_print:
                click.echo(line)
            else:
                setting.install_cron(line)
                click.echo(f"installed in crontab: {line}")
            return
        units = setting.systemd(list(targets), when or "hourly", unitdir)
        if just_print:
            for path, text in units.items():
                click.echo(f"### {path}\n{text}")
            return
        for path in setting.write(units, force=force):
            click.echo(f"wrote {path}")
        click.echo("enable with: systemctl --user daemon-reload && "
                   "systemctl --user enable --now drop-gc.timer")
    except ERRORS as err:
        fail(f"{err} (use --print to see what to install)")


def main() -> None:
    cli()
