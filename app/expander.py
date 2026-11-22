"""Synchronous image-expansion pipeline.

Everything here is blocking (subprocess + file I/O) and is meant to be run
in a worker thread via ``asyncio.to_thread``. The pipeline never touches the
published image: it grows a private working copy (``*.img.part``) and only
succeeds once the filesystem inside has been resized and verified.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

MIB = 1024 * 1024


class ExpansionError(RuntimeError):
    """A pipeline step failed; the caller discards the working copy."""


def _run(cmd: list[str], ok: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if proc.returncode not in ok:
        output = (proc.stdout + proc.stderr).strip()
        raise ExpansionError(f"{' '.join(cmd)} exited {proc.returncode}: {output[:2000]}")
    return proc


def fs_capacity_bytes(image: Path) -> int:
    """Total ext4 capacity (block count * block size) of the image."""
    out = _run(["dumpe2fs", "-h", str(image)]).stdout
    blocks = re.search(r"^Block count:\s+(\d+)", out, re.M)
    bsize = re.search(r"^Block size:\s+(\d+)", out, re.M)
    if not blocks or not bsize:
        raise ExpansionError("could not parse dumpe2fs header")
    return int(blocks.group(1)) * int(bsize.group(1))


def verify_samples(image: Path, files: list[dict]) -> list[dict]:
    """Dump every manifest file out of the image and check its SHA-256."""
    checked = []
    with tempfile.TemporaryDirectory(prefix="imageflex-verify-") as td:
        for i, entry in enumerate(files):
            out = Path(td) / f"sample-{i}"
            # NB: debugfs exits 0 even when the lookup fails, so the missing
            # output file / hash mismatch below is what actually gates us.
            _run(["debugfs", "-R", f"dump {entry['path']} {out}", str(image)])
            if not out.exists():
                raise ExpansionError(f"sample {entry['path']} missing from image")
            digest = hashlib.sha256(out.read_bytes()).hexdigest()
            if digest != entry["sha256"]:
                raise ExpansionError(
                    f"sample {entry['path']} corrupted: sha256 {digest} != {entry['sha256']}"
                )
            checked.append({"path": entry["path"], "sha256": digest})
    return checked


def expand_image(src: Path, part: Path, target_mib: int, manifest: dict) -> dict:
    """Grow *src* into the new file *part* and resize + verify the ext4 inside.

    Returns a report dict on success, raises ExpansionError otherwise.
    """
    target_bytes = target_mib * MIB

    # 1. private working copy; the published image is never modified.
    shutil.copyfile(src, part)

    # 2. grow the image file to the requested capacity.
    os.truncate(part, target_bytes)

    # 3. check, then resize the filesystem to fill the enlarged file.
    _run(["e2fsck", "-fy", str(part)], ok=(0, 1))  # 1 = errors corrected
    _run(["resize2fs", str(part)])

    # 4. verify: filesystem capacity must reach the requested target.
    capacity = fs_capacity_bytes(part)
    if capacity < target_bytes:
        raise ExpansionError(
            f"filesystem capacity {capacity // MIB} MiB below target {target_mib} MiB"
        )

    # 5. verify: sample payload must be byte-identical to the manifest.
    checked = verify_samples(part, manifest.get("files", []))

    # 6. final read-only fsck must find a clean filesystem.
    _run(["e2fsck", "-fn", str(part)], ok=(0,))

    fd = os.open(part, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

    return {
        "fs_capacity_mib": capacity // MIB,
        "samples_verified": checked,
    }
