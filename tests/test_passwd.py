import os
from datetime import timedelta

import pytest

from drop import drops, passwd
from drop.config import Target


@pytest.fixture
def target(tmp_path):
    (tmp_path / "www").mkdir()
    pw = tmp_path / "htpasswd"
    pw.write_text("# keep me\nold:x\n")
    pw.chmod(0o640)
    return Target("t", str(tmp_path / "www" / "drop"), "http://h/drop", htpasswd=str(pw))


def test_add_update_remove(target):
    assert passwd.add(target, "alice", "sekrit")
    assert not passwd.add(target, "alice", "other")
    assert passwd.users(target) == ["old", "alice"]
    text = open(target.htpasswd).read()
    assert text.startswith("# keep me\n") and text.count("alice:") == 1
    assert os.stat(target.htpasswd).st_mode & 0o777 == 0o640
    assert passwd.remove(target, ["alice", "nobody"]) == ["alice"]
    assert passwd.users(target) == ["old"]
    with pytest.raises(passwd.PasswdError):
        passwd.add(target, "bad:name", "x")


def test_put_users(target, tmp_path):
    src = tmp_path / "a.txt"
    src.write_text("alpha")
    entry = drops.put(target, [src], timedelta(days=1), users=["bob", "old"])
    assert entry.auth and list(entry.passwords) == ["bob"]
    box = drops.open_store(target).root / entry.id
    assert "Require user bob old" in (box / ".htaccess").read_text()
    assert "passwords" not in entry.to_json()
    assert drops.entries(target)[0].users == ["bob", "old"]


def test_usage_prune(target, tmp_path):
    www = tmp_path / "www"
    (www / "p").mkdir()
    (www / "p" / ".htaccess").write_text(f'AuthUserFile "{target.htpasswd}"\nRequire valid-user\n')
    (www / "q").mkdir()
    (www / "q" / ".htaccess").write_text("AuthUserFile /elsewhere\nRequire user carol\n")
    passwd.add(target, "dave", "x")
    src = tmp_path / "a.txt"
    src.write_text("alpha")
    drops.put(target, [src], None, users=["erin"])

    assert passwd.default_root(target) == str(www)
    use = passwd.usage(target)
    assert use.anyone == [str(www / "p" / ".htaccess")]
    assert list(use.named) == ["erin"]
    assert passwd.unreferenced(target, use) == ["old", "dave"]
