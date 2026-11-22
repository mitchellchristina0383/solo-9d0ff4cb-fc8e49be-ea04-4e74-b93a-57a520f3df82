"""ImageFlex — single-instance disk-image expansion service.

Endpoints (interactive UI at /docs):
  GET  /image   download the currently published ext4 image
  POST /expand  grow the image + filesystem to a target capacity (MiB)
  GET  /status  current version, images on disk, in-flight downloads
"""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import expander
from .manager import BusyError, ImageManager, RejectedError

CHUNK = 1024 * 1024  # 1 MiB streaming chunks


class ExpandRequest(BaseModel):
    target_mib: int = Field(
        ..., gt=0, description="Target image capacity in MiB", examples=[128]
    )


def create_app() -> FastAPI:
    data_dir = Path(os.environ.get("DATA_DIR", "./data"))
    base_dir = Path(os.environ.get("BASE_DIR", "./base"))
    max_target_mib = int(os.environ.get("MAX_TARGET_MIB", "65536"))
    manager = ImageManager(data_dir, base_dir, max_target_mib)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await manager.start()
        yield

    app = FastAPI(
        title="ImageFlex",
        version="1.0.0",
        description=(
            "Single-instance disk-image expansion service. Download the "
            "current ext4 template image, or request a larger one: the "
            "service grows the image file and its filesystem, verifies "
            "capacity and sample-file integrity, then publishes it."
        ),
        lifespan=lifespan,
    )

    @app.get("/", include_in_schema=False)
    async def root():
        return RedirectResponse("/docs")

    @app.get(
        "/image",
        summary="Download the currently published image",
        response_class=StreamingResponse,
        responses={200: {"content": {"application/octet-stream": {}}}},
    )
    async def download_image():
        # The version, Content-Length and body are all pinned to whatever is
        # published at the moment the download starts; later expansions do
        # not affect this response.
        info = manager.acquire_download()
        try:
            fd = os.open(info.path, os.O_RDONLY)
        except OSError:
            manager.release_download(info)
            raise HTTPException(503, "image temporarily unavailable")
        size = os.fstat(fd).st_size
        f = os.fdopen(fd, "rb")

        async def stream():
            try:
                while True:
                    chunk = await asyncio.to_thread(f.read, CHUNK)
                    if not chunk:
                        break
                    yield chunk
            finally:
                f.close()
                manager.release_download(info)

        return StreamingResponse(
            stream(),
            media_type="application/octet-stream",
            headers={
                "Content-Length": str(size),
                "Content-Disposition": (
                    f'attachment; filename="imageflex-v{info.version}'
                    f'-{info.size_mib}MiB.img"'
                ),
                "X-Image-Version": str(info.version),
                "X-Image-Size-MiB": str(info.size_mib),
            },
        )

    @app.post(
        "/expand",
        summary="Expand the image and its filesystem to TARGET_MIB",
        responses={
            200: {"description": "Expanded and published, or already at target"},
            400: {"description": "Target below current capacity or above limit"},
            409: {"description": "Both image slots in use; retry later"},
            500: {"description": "Expansion failed; published image untouched"},
        },
    )
    async def expand(req: ExpandRequest):
        try:
            return await manager.expand(req.target_mib)
        except RejectedError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except BusyError as exc:
            raise HTTPException(
                status_code=409, detail=str(exc), headers={"Retry-After": "5"}
            )
        except expander.ExpansionError as exc:
            raise HTTPException(
                status_code=500,
                detail=f"expansion failed; published image untouched: {exc}",
            )

    @app.get("/status", summary="Service and image inventory status")
    async def status():
        return manager.status()

    return app


app = create_app()
