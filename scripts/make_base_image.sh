#!/usr/bin/env bash
# Build the seed ext4 disk image bundled with the service.
# The image is a bare ext4 filesystem in a regular file (never mounted);
# sample files are injected with debugfs, and a SHA-256 manifest is emitted
# next to the image so the service can re-verify them after every expansion.
#
# usage: make_base_image.sh OUT_DIR [SIZE_MIB]
set -euo pipefail

OUT_DIR="${1:?usage: make_base_image.sh OUT_DIR [SIZE_MIB]}"
SIZE_MIB="${2:-${BASE_SIZE_MIB:-64}}"

command -v mkfs.ext4 >/dev/null || { echo "mkfs.ext4 (e2fsprogs) is required" >&2; exit 1; }
command -v debugfs    >/dev/null || { echo "debugfs (e2fsprogs) is required" >&2; exit 1; }

mkdir -p "$OUT_DIR"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# ---- sample payload -------------------------------------------------------
mkdir -p "$WORK/samples"

cat > "$WORK/samples/hello.txt" <<'EOF'
Hello from the ImageFlex template disk!
This file belongs to the sample payload that is re-verified after every
online image expansion. If you can read this, the filesystem survived.
EOF

: > "$WORK/samples/lorem.txt"
i=1
while [ "$i" -le 200 ]; do
  echo "Line $i: Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor incididunt ut labore." >> "$WORK/samples/lorem.txt"
  i=$((i + 1))
done

# 1 MiB of non-repeating binary data so checksum verification is meaningful.
head -c 1048576 /dev/urandom > "$WORK/samples/random.bin"

# ---- image ----------------------------------------------------------------
IMG="$WORK/base.img"
truncate -s "${SIZE_MIB}M" "$IMG"
mkfs.ext4 -q -F -L imageflex "$IMG"
debugfs -w -R "mkdir /sample" "$IMG" >/dev/null 2>&1
for f in hello.txt lorem.txt random.bin; do
  debugfs -w -R "write $WORK/samples/$f /sample/$f" "$IMG" >/dev/null 2>&1
done

# ---- manifest -------------------------------------------------------------
python3 - "$WORK" "$OUT_DIR" "$SIZE_MIB" <<'PY'
import hashlib, json, pathlib, sys

work, out_dir, size_mib = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), int(sys.argv[3])
files = [
    {
        "path": f"/sample/{p.name}",
        "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
        "size": p.stat().st_size,
    }
    for p in sorted((work / "samples").iterdir())
]
(out_dir / "manifest.json").write_text(
    json.dumps({"size_mib": size_mib, "files": files}, indent=2) + "\n"
)
PY

mv "$IMG" "$OUT_DIR/base.img"
echo "built $OUT_DIR/base.img (${SIZE_MIB} MiB) and $OUT_DIR/manifest.json"
