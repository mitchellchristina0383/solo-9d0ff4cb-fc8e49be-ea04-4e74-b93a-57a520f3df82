"""In-process state machine for the published image and its predecessors.

Concurrency model: the service runs as a single process on a single event
loop. Every critical section below is plain synchronous code with no
``await`` between check and mutation, so sections are atomic with respect to
other coroutines; the only suspension point is the blocking expansion
pipeline, which runs in a worker thread while downloads continue.

Disk policy: at most two images exist in the data directory at any time —
  1. the currently published image (always present), and
  2. either an expansion working copy, or a previous published image that is
     still being streamed to a client ("retained").
A retained image is deleted as soon as its last download finishes. If both
slots are occupied, new expansion requests are rejected with 409 until a
slot frees up.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from . import expander

MIB = 1024 * 1024
STATE_FILE = "state.json"


class BusyError(Exception):
    """Both image slots are in use; the caller may retry later."""


class RejectedError(Exception):
    """The requested target capacity is not acceptable."""


@dataclass
class VersionInfo:
    version: int
    path: Path
    size_bytes: int

    @property
    def size_mib(self) -> int:
        return self.size_bytes // MIB


class ImageManager:
    def __init__(
        self,
        data_dir: Path,
        base_dir: Path,
        max_target_mib: int = 65536,
    ):
        self._data_dir = Path(data_dir)
        self._base_dir = Path(base_dir)
        self._base_image = self._base_dir / "base.img"
        self._manifest: dict = {}  # loaded from base_dir on start()
        self._max_target_mib = max_target_mib
        self._current: VersionInfo | None = None
        self._retained: dict[int, VersionInfo] = {}  # old versions with live downloads
        self._downloads: dict[int, int] = {}  # version -> in-flight download count
        self._expanding = False

    # ------------------------------------------------------------------
    # startup / crash recovery
    # ------------------------------------------------------------------
    async def start(self) -> None:
        await asyncio.to_thread(self._recover)

    def _recover(self) -> None:
        self._manifest = json.loads((self._base_dir / "manifest.json").read_text())
        self._data_dir.mkdir(parents=True, exist_ok=True)
        current: VersionInfo | None = None

        state = self._read_state()
        if state:
            path = self._data_dir / state.get("file", "")
            try:
                size = path.stat().st_size
            except OSError:
                size = None
            if size is not None:
                current = VersionInfo(int(state["version"]), path, size)

        if current is None:
            # First boot (or state lost): seed version 1 from the bundled
            # base image so the service always has something published.
            path = self._data_dir / "image-v1.img"
            shutil.copyfile(self._base_image, path)
            current = VersionInfo(1, path, path.stat().st_size)

        self._current = current
        self._write_state()

        # A previous process may have died mid-expansion or while old
        # versions were still being downloaded. In-flight downloads do not
        # survive a restart, so nothing except the published image may stay.
        for p in self._data_dir.iterdir():
            if p.is_file() and p.name != STATE_FILE and p.name != current.path.name:
                p.unlink()

    def _read_state(self) -> dict | None:
        try:
            return json.loads((self._data_dir / STATE_FILE).read_text())
        except (OSError, ValueError, KeyError):
            return None

    def _write_state(self) -> None:
        cur = self._current
        payload = {
            "version": cur.version,
            "file": cur.path.name,
            "size_bytes": cur.size_bytes,
            "published_at": time.time(),
        }
        tmp = self._data_dir / f"{STATE_FILE}.tmp"
        tmp.write_text(json.dumps(payload, indent=2) + "\n")
        os.replace(tmp, self._data_dir / STATE_FILE)  # atomic publish of state

    # ------------------------------------------------------------------
    # downloads (refcounted; old versions are dropped when drained)
    # ------------------------------------------------------------------
    def acquire_download(self) -> VersionInfo:
        info = self._current
        self._downloads[info.version] = self._downloads.get(info.version, 0) + 1
        return info

    def release_download(self, info: VersionInfo) -> None:
        remaining = self._downloads.get(info.version, 0) - 1
        if remaining > 0:
            self._downloads[info.version] = remaining
            return
        self._downloads.pop(info.version, None)
        if info.version != self._current.version:
            self._retained.pop(info.version, None)
            self._unlink(info.path)

    # ------------------------------------------------------------------
    # expansion
    # ------------------------------------------------------------------
    async def expand(self, target_mib: int) -> dict:
        # --- critical section: no awaits, atomic on the event loop ---
        cur = self._current
        if target_mib < cur.size_mib:
            raise RejectedError(
                f"target {target_mib} MiB is below current capacity "
                f"{cur.size_mib} MiB; shrinking is not supported"
            )
        if target_mib == cur.size_mib:
            return {
                "status": "current",
                "detail": "target equals current capacity; nothing to do",
                "version": cur.version,
                "size_mib": cur.size_mib,
            }
        if target_mib > self._max_target_mib:
            raise RejectedError(
                f"target {target_mib} MiB exceeds configured limit "
                f"{self._max_target_mib} MiB"
            )
        if self._expanding or self._retained:
            raise BusyError(
                "two images are already in use (published image plus "
                + ("an expansion copy" if self._expanding else "a previous version still being downloaded")
                + "); retry once a slot frees up"
            )
        self._expanding = True
        new_version = cur.version + 1
        src = cur.path
        part = self._data_dir / f"image-v{new_version}.img.part"
        final = self._data_dir / f"image-v{new_version}.img"
        # --- end of critical section ---

        started = time.monotonic()
        try:
            report = await asyncio.to_thread(
                expander.expand_image, src, part, target_mib, self._manifest
            )
        except Exception:
            # Failure: discard the working copy, keep the published image.
            self._unlink(part)
            self._expanding = False
            raise

        # --- critical section: publish atomically ---
        os.replace(part, final)
        old = self._current
        info = VersionInfo(new_version, final, target_mib * MIB)
        self._current = info
        if self._downloads.get(old.version, 0) > 0:
            # Old version is still being streamed: keep it (second slot)
            # until its downloads drain.
            self._retained[old.version] = old
        else:
            self._downloads.pop(old.version, None)
            self._unlink(old.path)
        self._write_state()
        self._expanding = False
        # --- end of critical section ---

        return {
            "status": "published",
            "version": new_version,
            "size_mib": target_mib,
            "previous_version": old.version,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            **report,
        }

    # ------------------------------------------------------------------
    # observability
    # ------------------------------------------------------------------
    def status(self) -> dict:
        images = [
            {
                "version": self._current.version,
                "role": "published",
                "size_mib": self._current.size_mib,
                "active_downloads": self._downloads.get(self._current.version, 0),
            }
        ]
        for version, info in sorted(self._retained.items()):
            images.append(
                {
                    "version": version,
                    "role": "retained (download in flight)",
                    "size_mib": info.size_mib,
                    "active_downloads": self._downloads.get(version, 0),
                }
            )
        on_disk = len(images) + (1 if self._expanding else 0)
        return {
            "current_version": self._current.version,
            "current_size_mib": self._current.size_mib,
            "expansion_in_progress": self._expanding,
            "images_on_disk": on_disk,
            "images": images,
            "max_target_mib": self._max_target_mib,
        }

    # ------------------------------------------------------------------
    @staticmethod
    def _unlink(path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
