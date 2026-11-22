FROM python:3.12-slim

# e2fsprogs provides mkfs.ext4 / resize2fs / e2fsck / debugfs / dumpe2fs.
RUN apt-get update \
 && apt-get install -y --no-install-recommends e2fsprogs \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts

# Build the bundled seed image (64 MiB ext4 with sample files + manifest).
# The image is a plain file and is never mounted.
RUN bash scripts/make_base_image.sh /app/base 64

ENV DATA_DIR=/data \
    BASE_DIR=/app/base \
    MAX_TARGET_MIB=65536

EXPOSE 8000
VOLUME ["/data"]

# Single process, single event loop: the concurrency model relies on it.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
