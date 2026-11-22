"""End-to-end tests for the ImageFlex service.

Two layers:
  * in-process tests via FastAPI's TestClient (fast, 16 MiB base image)
  * tests against a real uvicorn subprocess (64 MiB base image) for
    download/expansion interleaving, the two-image disk cap and restarts
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[1]
MIB = 1024 * 1024


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
def make_base(out_dir: Path, size_mib: int) -> None:
    subprocess.run(
        ["bash", str(REPO / "scripts" / "make_base_image.sh"), str(out_dir), str(size_mib)],
        check=True,
        capture_output=True,
        text=True,
    )


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fs_capacity_mib(image: Path) -> int:
    out = subprocess.run(
        ["dumpe2fs", "-h", str(image)], check=True, capture_output=True, text=True
    ).stdout
    blocks = int(re.search(r"^Block count:\s+(\d+)", out, re.M).group(1))
    bsize = int(re.search(r"^Block size:\s+(\d+)", out, re.M).group(1))
    return blocks * bsize // MIB


def wait_for(predicate, timeout: float = 10.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


# ----------------------------------------------------------------------
# in-process tests (TestClient)
# ----------------------------------------------------------------------
@pytest.fixture()
def env(tmp_path, monkeypatch):
    base_dir = tmp_path / "base"
    make_base(base_dir, 16)
    data_dir = tmp_path / "data"
    monkeypatch.setenv("BASE_DIR", str(base_dir))
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    from app.main import create_app

    with TestClient(create_app()) as client:
        yield SimpleNamespace(client=client, data_dir=data_dir, base_dir=base_dir)


def test_initial_download_matches_published_image(env):
    r = env.client.get("/image")
    assert r.status_code == 200
    assert r.headers["content-length"] == str(16 * MIB)
    assert r.headers["x-image-version"] == "1"
    assert r.headers["x-image-size-mib"] == "16"
    assert hashlib.sha256(r.content).hexdigest() == sha256_of(env.data_dir / "image-v1.img")


def test_expand_publishes_verified_image(env, tmp_path):
    r = env.client.post("/expand", json={"target_mib": 32})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "published"
    assert body["version"] == 2
    assert body["size_mib"] == 32
    assert body["fs_capacity_mib"] >= 32
    assert len(body["samples_verified"]) == 3

    # only the published image may remain on disk (no downloads in flight)
    assert sorted(p.name for p in env.data_dir.iterdir()) == ["image-v2.img", "state.json"]

    # what clients download must itself be a valid, grown ext4 whose sample
    # files still match the manifest
    dl = env.client.get("/image")
    assert dl.headers["x-image-version"] == "2"
    downloaded = tmp_path / "downloaded.img"
    downloaded.write_bytes(dl.content)
    assert fs_capacity_mib(downloaded) >= 32
    manifest = json.loads((env.base_dir / "manifest.json").read_text())
    for i, entry in enumerate(manifest["files"]):
        out = tmp_path / f"sample-{i}"
        subprocess.run(
            ["debugfs", "-R", f"dump {entry['path']} {out}", str(downloaded)],
            check=True,
            capture_output=True,
        )
        assert sha256_of(out) == entry["sha256"], entry["path"]


def test_expand_equal_target_returns_current(env):
    assert env.client.post("/expand", json={"target_mib": 32}).status_code == 200
    r = env.client.post("/expand", json={"target_mib": 32})
    assert r.status_code == 200
    assert r.json()["status"] == "current"
    assert r.json()["version"] == 2
    # no extra image was produced
    assert len(list(env.data_dir.glob("image-v*.img*"))) == 1


def test_expand_smaller_target_rejected(env):
    r = env.client.post("/expand", json={"target_mib": 8})
    assert r.status_code == 400
    assert "below current capacity" in r.json()["detail"]
    # published image untouched
    r = env.client.get("/image")
    assert r.headers["x-image-version"] == "1"


def test_expand_failure_keeps_published_and_retry_succeeds(env, monkeypatch):
    assert env.client.post("/expand", json={"target_mib": 32}).status_code == 200

    import app.expander

    def boom(src, part, target_mib, manifest):
        raise app.expander.ExpansionError("injected failure for testing")

    monkeypatch.setattr(app.expander, "expand_image", boom)
    r = env.client.post("/expand", json={"target_mib": 48})
    assert r.status_code == 500
    assert "injected failure" in r.json()["detail"]

    # last published image is still served, no working copy left behind
    r = env.client.get("/image")
    assert r.status_code == 200
    assert r.headers["x-image-version"] == "2"
    assert len(r.content) == 32 * MIB
    assert list(env.data_dir.glob("*.part")) == []
    assert env.client.get("/status").json()["expansion_in_progress"] is False

    # retry with the pipeline healthy again
    monkeypatch.undo()
    r = env.client.post("/expand", json={"target_mib": 48})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "published"
    assert r.json()["version"] == 3
    assert env.client.get("/image").headers["x-image-size-mib"] == "48"


def test_concurrent_expansion_rejected(env, monkeypatch):
    import app.expander

    original = app.expander.expand_image

    def slow(src, part, target_mib, manifest):
        time.sleep(1.0)
        return original(src, part, target_mib, manifest)

    monkeypatch.setattr(app.expander, "expand_image", slow)

    first: dict = {}

    def run_first():
        first["resp"] = env.client.post("/expand", json={"target_mib": 32})

    t = threading.Thread(target=run_first)
    t.start()
    try:
        # wait until the first expansion is actually running
        assert wait_for(
            lambda: env.client.get("/status").json()["expansion_in_progress"], timeout=10
        )
        # a second expansion finds the working-copy slot occupied -> 409
        r = env.client.post("/expand", json={"target_mib": 48})
        assert r.status_code == 409
        # equal-to-current is answered from the published image, not queued
        r = env.client.post("/expand", json={"target_mib": 16})
        assert r.status_code == 200 and r.json()["status"] == "current"
    finally:
        t.join(timeout=60)
    assert first["resp"].status_code == 200
    assert first["resp"].json()["version"] == 2


def test_restart_recovers_published_and_cleans_strays(tmp_path, monkeypatch):
    base_dir = tmp_path / "base"
    make_base(base_dir, 16)
    data_dir = tmp_path / "data"
    monkeypatch.setenv("BASE_DIR", str(base_dir))
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    from app.main import create_app

    with TestClient(create_app()) as c1:
        assert c1.post("/expand", json={"target_mib": 32}).status_code == 200

    # simulate crash leftovers: an expansion working copy and an old version
    (data_dir / "image-v3.img.part").write_bytes(b"junk")
    (data_dir / "image-v1.img").write_bytes(b"junk")

    with TestClient(create_app()) as c2:
        st = c2.get("/status").json()
        assert st["current_version"] == 2
        assert st["current_size_mib"] == 32
        r = c2.get("/image")
        assert r.status_code == 200
        assert len(r.content) == 32 * MIB
        assert sorted(p.name for p in data_dir.iterdir()) == ["image-v2.img", "state.json"]


# ----------------------------------------------------------------------
# tests against a real uvicorn subprocess
# ----------------------------------------------------------------------
class UvicornServer:
    def __init__(self, base_dir: Path, data_dir: Path):
        self.data_dir = data_dir
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        sock.close()
        env = dict(os.environ)
        env.update(
            {
                "BASE_DIR": str(base_dir),
                "DATA_DIR": str(data_dir),
                "MAX_TARGET_MIB": "8192",
                "PYTHONPATH": str(REPO),
            }
        )
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
            ],
            cwd=REPO,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.url = f"http://127.0.0.1:{self.port}"
        self._wait_ready()

    def _wait_ready(self, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"server exited early:\n{self.proc.stdout.read()}")
            try:
                with httpx.Client() as c:
                    if c.get(f"{self.url}/status", timeout=1).status_code == 200:
                        return
            except httpx.TransportError:
                time.sleep(0.1)
        raise RuntimeError("server did not become ready")

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=10)


@pytest.fixture()
def server_factory(tmp_path):
    base_dir = tmp_path / "base"
    make_base(base_dir, 64)
    data_dir = tmp_path / "data"
    servers: list[UvicornServer] = []

    def start() -> UvicornServer:
        s = UvicornServer(base_dir, data_dir)
        servers.append(s)
        return s

    yield start
    for s in servers:
        s.stop()


def test_download_expand_interleaved_and_two_image_cap(server_factory):
    server = server_factory()
    v1 = server.data_dir / "image-v1.img"
    expected_sha = sha256_of(v1)
    expected_len = v1.stat().st_size
    assert expected_len == 64 * MIB

    with httpx.Client(base_url=server.url, timeout=60) as dl, httpx.Client(
        base_url=server.url, timeout=60
    ) as api:
        # start a download of v1 and read only a little: the server keeps
        # streaming (backpressure), so v1 stays referenced for a while
        with dl.stream("GET", "/image") as resp:
            assert resp.status_code == 200
            assert resp.headers["content-length"] == str(expected_len)
            assert resp.headers["x-image-version"] == "1"
            chunks = resp.iter_bytes(65536)
            head = b"".join(next(chunks) for _ in range(4))

            # expand while the download is in flight: downloads keep working
            r = api.post("/expand", json={"target_mib": 128})
            assert r.status_code == 200, r.text
            assert r.json()["status"] == "published"
            assert r.json()["version"] == 2

            # v1 (still downloading) + v2 (published) = two images on disk,
            # so a further expansion must be refused for now
            st = api.get("/status").json()
            assert st["images_on_disk"] == 2
            r = api.post("/expand", json={"target_mib": 192})
            assert r.status_code == 409

            # the in-flight response is unaffected: length and content are
            # exactly the v1 image as of download start
            rest = b"".join(chunks)
        body = head + rest
        assert len(body) == expected_len
        assert hashlib.sha256(body).hexdigest() == expected_sha

        # once the v1 download drained, the old image is dropped and the
        # previously refused expansion can be retried successfully
        assert wait_for(
            lambda: api.get("/status").json()["images_on_disk"] == 1, timeout=15
        )
        r = api.post("/expand", json={"target_mib": 192})
        assert r.status_code == 200, r.text
        assert r.json()["version"] == 3

        r = api.get("/image")
        assert r.headers["x-image-version"] == "3"
        assert len(r.content) == 192 * MIB
        assert hashlib.sha256(r.content).hexdigest() == sha256_of(
            server.data_dir / "image-v3.img"
        )
        assert sorted(p.name for p in server.data_dir.iterdir()) == [
            "image-v3.img",
            "state.json",
        ]


def test_restart_keeps_last_published_downloadable(server_factory):
    server = server_factory()
    with httpx.Client(base_url=server.url, timeout=60) as c:
        assert c.post("/expand", json={"target_mib": 128}).status_code == 200
    server.stop()

    restarted = server_factory()
    with httpx.Client(base_url=restarted.url, timeout=60) as c:
        st = c.get("/status").json()
        assert st["current_version"] == 2
        assert st["current_size_mib"] == 128
        r = c.get("/image")
        assert r.status_code == 200
        assert r.headers["x-image-version"] == "2"
        assert len(r.content) == 128 * MIB
