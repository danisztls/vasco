# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Reclaim disk from the browser tier without touching session state.

Three targets, all regenerated on demand:

- Camoufox browser bundles other than the active one. An upgrade installs the
  new bundle beside the old one and never removes it.
- Runtime fontconfig files generated for those bundles.
- The persistent profile's caches, named by an allowlist so that anything
  unrecognized (cookies, logins, ``storage/``) stays by construction.

Nothing here assumes the browser server is stopped: a bundle with a live process
is kept, and the profile is left alone while Firefox holds its lock.
"""

from __future__ import annotations

import contextlib
import fcntl
import logging
import os
import re
import shutil
import struct
from pathlib import Path
from typing import Any

from .browser_server import _resolve_user_data_dir

log = logging.getLogger(__name__)

# Everything Firefox regenerates by itself. An allowlist, not a denylist: a file
# added by a future Firefox must default to surviving.
_PROFILE_CACHE_DIRS = (
    "cache2",
    "startupCache",
    "shader-cache",
    "thumbnails",
    "OfflineCache",
)

# Linux `struct flock`: l_type, l_whence, l_start, l_len, l_pid.
_FLOCK = "hhqqi"

_FONTCONFIG_DIR = re.compile(r"<dir>([^<]+)</dir>")


def _camoufox_state() -> tuple[Path, Path | None, list[Path]] | None:
    """(install dir, bundle the next launch uses, installed bundles); None without the extra.

    Asks camoufox rather than reading its config.json: a release's pinned build
    outranks ``active_version``, so only camoufox knows which bundle launches.
    """
    try:
        from camoufox.multiversion import get_active_path, list_installed
        from camoufox.pkgman import INSTALL_DIR
    except ImportError:
        return None
    return Path(INSTALL_DIR), get_active_path(), [i.path for i in list_installed()]


def _running_executables() -> set[Path]:
    """Executables of every process we can see."""
    found: set[Path] = set()
    for proc in Path("/proc").iterdir():
        if proc.name.isdigit():
            with contextlib.suppress(OSError):
                found.add(Path(os.readlink(proc / "exe")))
    return found


def _profile_in_use(profile: Path) -> bool:
    """Whether a Firefox holds this profile, by the POSIX lock it keeps on .parentlock.

    Not the ``lock`` symlink's pid: that goes stale after a crash and can't be
    checked across PID namespaces, whereas a file lock dies with its holder and
    conflicts across them. F_GETLK only asks, so it can't get in a launch's way.
    If the answer can't be had, assume the profile is busy.
    """
    try:
        fd = os.open(profile / ".parentlock", os.O_RDONLY)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    try:
        query = struct.pack(_FLOCK, fcntl.F_WRLCK, os.SEEK_SET, 0, 0, 0)
        answer = fcntl.fcntl(fd, fcntl.F_GETLK, query)
        return struct.unpack(_FLOCK, answer)[0] != fcntl.F_UNLCK
    except OSError:
        return True
    finally:
        os.close(fd)


def _disk_usage(path: Path) -> int:
    """Bytes on disk, without following symlinks."""
    total = 0
    with contextlib.suppress(OSError):
        total = path.lstat().st_blocks * 512
    if path.is_symlink():
        return total
    for root, dirs, files in os.walk(path):
        for name in dirs + files:
            with contextlib.suppress(OSError):
                total += os.lstat(os.path.join(root, name)).st_blocks * 512
    return total


def _target(path: Path) -> dict[str, Any]:
    return {"path": str(path), "bytes": _disk_usage(path), "removed": False}


def _remove(target: dict[str, Any]) -> None:
    path = Path(target["path"])
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
        target["removed"] = True
    except OSError as exc:
        target["error"] = str(exc)


def _stale_fontconfigs(install_dir: Path, kept: set[Path]) -> list[Path]:
    """Generated configs pointing at a bundle we aren't keeping.

    A config for a kept bundle stays even when unused: it's what a live browser
    was told to read, and it's small.
    """
    browsers = install_dir / "browsers"
    stale: list[Path] = []
    for conf in sorted((install_dir / "fontconfig").glob("fonts-*.conf")):
        try:
            dirs = _FONTCONFIG_DIR.findall(conf.read_text(errors="replace"))
        except OSError:
            continue
        bundles = {
            browsers.joinpath(*Path(d).relative_to(browsers).parts[:2])
            for d in dirs
            if Path(d).is_relative_to(browsers)
        }
        if bundles and not bundles & kept:
            stale.append(conf)
    return stale


def _plan_browsers() -> dict[str, Any]:
    state = _camoufox_state()
    if state is None:
        return {"status": "camoufox_not_installed", "targets": []}
    install_dir, active, installed = state
    if active is None:
        # Without knowing what launches next, every bundle might be the one needed.
        return {"status": "no_active_version", "targets": []}
    running = _running_executables()
    kept = {
        active,
        *(b for b in installed if any(exe.is_relative_to(b) for exe in running)),
    }
    return {
        "status": "ok",
        "active": str(active),
        "kept": sorted(str(b) for b in kept),
        "targets": [
            *(_target(b) for b in installed if b not in kept),
            *(_target(c) for c in _stale_fontconfigs(install_dir, kept)),
        ],
    }


def _plan_profile(cfg: Any | None) -> dict[str, Any]:
    browser = getattr(cfg, "browser", None)
    raw = str(getattr(browser, "user_data_dir", "") or "")
    if not raw:
        return {"status": "ephemeral", "targets": []}
    profile = Path(_resolve_user_data_dir(raw))
    if not profile.is_dir():
        return {"status": "missing", "path": str(profile), "targets": []}
    report: dict[str, Any] = {
        "status": "in_use" if _profile_in_use(profile) else "ok",
        "path": str(profile),
        "targets": [
            _target(profile / name)
            for name in _PROFILE_CACHE_DIRS
            if (profile / name).is_dir() and not (profile / name).is_symlink()
        ],
    }
    if report["status"] == "in_use":
        report["hint"] = (
            "the browser is using this profile, so its cache was left alone; "
            "stop vasco-browser.service and run this again to clear it"
        )
    return report


def prune_old_browsers() -> None:
    """Delete bundles no process uses or will launch; the browser server's startup step.

    Disk housekeeping must never take the browser tier down, so nothing raises.
    """
    try:
        plan = _plan_browsers()
        if plan["status"] != "ok":
            log.info("not pruning browser installs: %s", plan["status"])
            return
        for target in plan["targets"]:
            _remove(target)
            if "error" in target:
                log.warning("could not remove %s: %s", target["path"], target["error"])
            else:
                log.info("removed old browser data: %s", target["path"])
    except Exception:
        log.exception("pruning old browser installs failed")


def housekeep(cfg: Any | None = None, *, dry_run: bool = False) -> dict[str, Any]:
    """Delete old browser bundles and profile caches; session state always survives.

    Each target carries its size and whether it was removed. A failure lands in
    that target's ``error`` rather than stopping the rest.
    """
    browsers = _plan_browsers()
    profile = _plan_profile(cfg)
    if not dry_run:
        for section in (browsers, profile):
            if section["status"] == "ok":
                for target in section["targets"]:
                    _remove(target)
    targets = browsers["targets"] + profile["targets"]
    return {
        "dry_run": dry_run,
        "browsers": browsers,
        "profile": profile,
        "reclaimable_bytes": sum(t["bytes"] for t in targets),
        "freed_bytes": sum(t["bytes"] for t in targets if t["removed"]),
        "errors": sum("error" in t for t in targets),
    }
