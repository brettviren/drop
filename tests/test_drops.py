import json
from datetime import datetime, timedelta, timezone

import pytest

from drop import drops
from drop.config import ConfigError, Target, load, parse_ttl

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def target(tmp_path):
    return Target("t", str(tmp_path / "www" / "drop"), "http://h/drop", htpasswd="/pw")


@pytest.fixture
def files(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("alpha")
    (src / "b.tar.gz").write_bytes(b"\x1f\x8b beta")
    return src


def test_parse_ttl():
    assert parse_ttl("7d") == timedelta(days=7)
    assert parse_ttl("30m") == timedelta(minutes=30)
    assert parse_ttl("never") is None
    with pytest.raises(ConfigError):
        parse_ttl("soon")


def test_target_host():
    assert Target("r", "me@host:www/drop", "u").host == "me@host"
    assert Target("r", "me@host:www/drop", "u").dir == "www/drop"
    assert Target("l", "/srv/www", "u").host == ""


def test_load_defaults(tmp_path):
    cfg = load(tmp_path / "none.toml", env={})
    assert cfg.get().name == "local"
    assert cfg.get().url.endswith("/drop")


def test_put_hash_dir(target, files):
    entry = drops.put(target, [files / "a.txt"], timedelta(days=1), now=NOW)
    root = drops.open_store(target).root
    assert (root / "index.html").read_text() == ""
    assert (root / entry.id / "a.txt").read_text() == "alpha"
    assert (root / entry.id / "index.html").exists()
    assert entry.urls == [f"http://h/drop/{entry.id}/a.txt"]
    side = json.loads((root / entry.sidecar).read_text())
    assert side["expires"] == "2026-01-02T00:00:00+00:00"
    # Same content, same place.
    again = drops.put(target, [files / "a.txt"], timedelta(days=1), now=NOW)
    assert again.id == entry.id


def test_put_hash_file(target, files):
    entry = drops.put(target, [files / "b.tar.gz"], None, hash="file", now=NOW)
    assert entry.id.endswith(".tar.gz") and entry.kind == "file"
    assert entry.expires is None
    assert (drops.open_store(target).root / entry.id).is_file()


def test_put_named_open_auth(target, files):
    entry = drops.put(
        target, [files / "a.txt", files / "b.tar.gz"], timedelta(hours=1),
        hash="none", name="talk", index="open", auth=True, now=NOW,
    )
    box = drops.open_store(target).root / "talk"
    assert "AuthUserFile /pw" in (box / ".htaccess").read_text()
    assert not (box / "index.html").exists()
    assert entry.url == "http://h/drop/talk/"


def test_put_errors(target, files):
    with pytest.raises(drops.DropError):
        drops.put(target, [files / "a.txt"], None, hash="none")
    with pytest.raises(drops.DropError):
        drops.put(target, [files / "a.txt", files / "b.tar.gz"], None, hash="file")
    with pytest.raises(drops.DropError):
        drops.put(Target("x", target.path, "u"), [files / "a.txt"], None, auth=True)


def test_ls_rm_gc_extend(target, files):
    old = drops.put(target, [files / "a.txt"], timedelta(days=1), now=NOW)
    keep = drops.put(target, [files / "b.tar.gz"], None, now=NOW)
    assert {e.id for e in drops.entries(target)} == {old.id, keep.id}

    later = NOW + timedelta(days=2)
    assert [e.id for e in drops.gc(target, now=later, dry_run=True)] == [old.id]
    assert len(drops.entries(target)) == 2
    drops.extend(target, old.id, timedelta(days=7), now=later)
    assert drops.gc(target, now=later) == []

    gone = drops.gc(target, now=later + timedelta(days=8))
    assert [e.id for e in gone] == [old.id]
    root = drops.open_store(target).root
    assert not (root / old.id).exists() and not (root / old.sidecar).exists()

    drops.remove(target, [keep.id])
    assert drops.entries(target) == []
    with pytest.raises(drops.DropError):
        drops.remove(target, ["nope"])


def test_grant(target, files):
    target.grant = True
    drops.put(target, [files / "a.txt"], None, now=NOW)
    top = drops.open_store(target).root / ".htaccess"
    assert top.read_text() == drops.GRANT
    assert '<FilesMatch "^[^.]">' in drops.GRANT
    # An .htaccess drop did not write is left alone.
    top.write_text("# mine\n")
    drops.put(target, [files / "b.tar.gz"], None, now=NOW)
    assert top.read_text() == "# mine\n"


def test_auth_htaccess_survives_grants(target, files):
    drops.put(target, [files / "a.txt"], None, hash="none", name="x", auth=True, now=NOW)
    text = (drops.open_store(target).root / "x" / ".htaccess").read_text()
    assert text.count("Require valid-user") == 2 and text.count("Satisfy All") == 2
