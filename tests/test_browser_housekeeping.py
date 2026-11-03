# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from vasco.fetch import browser_housekeeping as hk
from vasco.fetch import browser_server as server

OLD = "152.0.4-beta.29-1bea4b55"
NEW = "158.0.1-beta.41-3f2eb1d0"

SESSION_STATE = (
    "cookies.sqlite",
    "cookies.sqlite-wal",
    "key4.db",
    "logins.db",
    "permissions.sqlite",
    "prefs.js",
    "storage/default/https+++example.com/ls/data.sqlite",
    "sessionstore-backups/recovery.jsonlz4",
)
CACHES = ("cache2/entries/ABC", "startupCache/startupCache.8.little")


@pytest.fixture
def install(tmp_path, monkeypatch):
    """A camoufox cache holding an old and an active bundle, each with a fontconfig."""
    root = tmp_path / "camoufox"
    old, new = (root / "browsers" / "official" / v for v in (OLD, NEW))
    for bundle in (old, new):
        (bundle / "fonts").mkdir(parents=True)
        (bundle / "camoufox-bin").write_bytes(b"x" * 8192)
    conf = root / "fontconfig"
    conf.mkdir()
    (conf / "fonts-old.conf").write_text(f"<dir>{old}/fonts/LMW</dir>")
    (conf / "fonts-new.conf").write_text(f"<dir>{new}/fonts/LMW</dir>")
    (conf / "fonts-system.conf").write_text("<dir>/usr/share/fonts</dir>")
    monkeypatch.setattr(
        hk,
        "_camoufox_state",
        lambda: (root, new, [b for b in (old, new) if b.exists()]),
    )
    monkeypatch.setattr(hk, "_running_executables", set)
    return SimpleNamespace(root=root, old=old, new=new, conf=conf)


@pytest.fixture
def profile(tmp_path):
    path = tmp_path / "profile"
    for rel in (*SESSION_STATE, *CACHES):
        (path / rel).parent.mkdir(parents=True, exist_ok=True)
        (path / rel).write_bytes(b"x" * 8192)
    (path / ".parentlock").touch()
    return path


def _cfg(user_data_dir: Path | str = "") -> SimpleNamespace:
    return SimpleNamespace(browser=SimpleNamespace(user_data_dir=str(user_data_dir)))


def test_removes_old_bundle_and_its_fontconfig_only(install):
    report = hk.housekeep(_cfg())

    assert not install.old.exists()
    assert (install.new / "camoufox-bin").exists()
    assert not (install.conf / "fonts-old.conf").exists()
    assert (install.conf / "fonts-new.conf").exists()
    assert (install.conf / "fonts-system.conf").exists()
    assert report["freed_bytes"] > 0
    assert report["errors"] == 0


def test_keeps_old_bundle_while_a_process_runs_from_it(install, monkeypatch):
    # An upgraded install whose server hasn't restarted yet.
    monkeypatch.setattr(
        hk, "_running_executables", lambda: {install.old / "camoufox-bin"}
    )

    report = hk.housekeep(_cfg())

    assert install.old.exists()
    assert (install.conf / "fonts-old.conf").exists()
    assert str(install.old) in report["browsers"]["kept"]
    assert report["freed_bytes"] == 0


def test_removes_nothing_without_an_active_version(install, monkeypatch):
    # e.g. the release's pinned build isn't installed: the next launch downloads it.
    monkeypatch.setattr(
        hk, "_camoufox_state", lambda: (install.root, None, [install.old, install.new])
    )

    report = hk.housekeep(_cfg())

    assert report["browsers"]["status"] == "no_active_version"
    assert install.old.exists() and install.new.exists()
    assert (install.conf / "fonts-old.conf").exists()


def test_profile_cache_still_clears_without_the_browser_extra(profile, monkeypatch):
    monkeypatch.setattr(hk, "_camoufox_state", lambda: None)

    report = hk.housekeep(_cfg(profile))

    assert report["browsers"]["status"] == "camoufox_not_installed"
    assert not (profile / "cache2").exists()


def test_clears_profile_caches_and_preserves_session_state(install, profile):
    hk.housekeep(_cfg(profile))

    assert not (profile / "cache2").exists()
    assert not (profile / "startupCache").exists()
    for rel in SESSION_STATE:
        assert (profile / rel).exists(), rel


def test_dry_run_deletes_nothing_but_reports_the_same_targets(install, profile):
    report = hk.housekeep(_cfg(profile), dry_run=True)

    assert install.old.exists()
    assert (install.conf / "fonts-old.conf").exists()
    assert (profile / "cache2").exists()
    assert report["freed_bytes"] == 0
    assert report["reclaimable_bytes"] > 0
    paths = {t["path"] for t in report["browsers"]["targets"]}
    assert str(install.old) in paths


def test_profile_locked_by_another_process_is_left_alone(install, profile):
    # Another process: POSIX locks never conflict within the one holding them.
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl, sys\n"
            "f = open(sys.argv[1], 'w')\n"
            "fcntl.lockf(f, fcntl.LOCK_EX)\n"
            "print('held', flush=True)\n"
            "sys.stdin.read()\n",
            str(profile / ".parentlock"),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "held"
        report = hk.housekeep(_cfg(profile))
        assert report["profile"]["status"] == "in_use"
        assert "hint" in report["profile"]
        assert (profile / "cache2").exists()
        assert report["profile"]["targets"][0]["removed"] is False
        # The browser's lock doesn't hold back the bundle pruning.
        assert not install.old.exists()
    finally:
        holder.stdin.close()
        holder.wait()

    # The lock dies with its holder; a stale `lock` symlink wouldn't.
    assert hk.housekeep(_cfg(profile))["profile"]["status"] == "ok"
    assert not (profile / "cache2").exists()


def test_unreadable_lock_counts_as_in_use(profile, monkeypatch):
    def denied(*_args, **_kwargs):
        raise PermissionError

    monkeypatch.setattr(fcntl, "fcntl", denied)

    assert hk._profile_in_use(profile) is True


def test_symlinked_cache_dir_is_not_followed(install, profile, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "keep").touch()
    (profile / "shader-cache").symlink_to(elsewhere)

    hk.housekeep(_cfg(profile))

    assert (elsewhere / "keep").exists()


def test_prune_removes_old_bundle_and_nothing_in_the_profile(install, profile):
    hk.prune_old_browsers()

    assert not install.old.exists()
    assert not (install.conf / "fonts-old.conf").exists()
    assert (install.new / "camoufox-bin").exists()
    assert (profile / "cache2").exists()


def test_prune_never_raises(monkeypatch):
    def broken():
        raise RuntimeError("camoufox changed its layout")

    monkeypatch.setattr(hk, "_camoufox_state", broken)

    hk.prune_old_browsers()


async def _run_server_with(monkeypatch, tmp_path, *, launch_fails: bool) -> list[str]:
    """Drive run_server with a stub browser, recording launch and prune order."""
    events: list[str] = []

    class Supervisor:
        def __init__(self, *_args):
            pass

        async def start(self):
            events.append("launch")
            if launch_fails:
                raise RuntimeError("bad upgrade")

        async def close(self):
            pass

    monkeypatch.setitem(sys.modules, "camoufox", ModuleType("camoufox"))
    monkeypatch.setitem(sys.modules, "camoufox.async_api", ModuleType("async_api"))
    monkeypatch.setattr(server, "_BrowserSupervisor", Supervisor)
    monkeypatch.setattr(server, "_socket_path", lambda: tmp_path / "browser.sock")
    monkeypatch.setattr(hk, "prune_old_browsers", lambda: events.append("prune"))

    task = asyncio.create_task(server.run_server(None))
    while not (tmp_path / "browser.sock").exists() and not task.done():
        await asyncio.sleep(0.01)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, RuntimeError):
        await task
    return events


async def test_server_prunes_after_a_successful_launch(monkeypatch, tmp_path):
    events = await _run_server_with(monkeypatch, tmp_path, launch_fails=False)

    assert events == ["launch", "prune"]


async def test_failed_launch_keeps_the_old_install(monkeypatch, tmp_path):
    events = await _run_server_with(monkeypatch, tmp_path, launch_fails=True)

    assert events == ["launch"]


def test_ephemeral_profile_is_skipped(install):
    assert hk.housekeep(_cfg())["profile"]["status"] == "ephemeral"


def test_failed_removal_is_reported_and_the_rest_continues(
    install, profile, monkeypatch
):
    real_rmtree = hk.shutil.rmtree

    def rmtree(path):
        if Path(path) == install.old:
            raise PermissionError("nope")
        real_rmtree(path)

    monkeypatch.setattr(hk.shutil, "rmtree", rmtree)

    report = hk.housekeep(_cfg(profile))

    assert report["errors"] == 1
    assert install.old.exists()
    assert not (profile / "cache2").exists()
