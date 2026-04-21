#!/usr/bin/env bash
# Generate Python FlatBuffers bindings from the motion-control-protocol schemas.
# Output goes to ruvion_client/proto/ and is gitignored.
#
# Prerequisites:
#   macOS:         brew install flatbuffers
#   Ubuntu/Debian: sudo apt install flatbuffers-compiler
#   Windows:       winget install Google.FlatBuffers
#
# Run once after cloning:
#   git submodule update --init --recursive
#   bash scripts/generate_proto.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCHEMAS_DIR="$ROOT/../submodules/motion-control-protocol/schemas"
OUT_DIR="$ROOT/ruvion_client/proto"

if ! command -v flatc >/dev/null 2>&1; then
    cat >&2 <<'EOF'
Error: flatc not found. Install with:
  macOS:          brew install flatbuffers
  Ubuntu/Debian:  sudo apt install flatbuffers-compiler
  Windows:        winget install Google.FlatBuffers
EOF
    exit 1
fi

if [ ! -d "$SCHEMAS_DIR" ]; then
    echo "Error: schemas dir not found at $SCHEMAS_DIR" >&2
    echo "Run: git submodule update --init --recursive" >&2
    exit 1
fi

mkdir -p "$OUT_DIR"

# Clean generated namespace dirs but preserve the hand-written __init__.py
find "$OUT_DIR" -mindepth 1 -maxdepth 1 -type d -exec rm -rf {} +

echo "Generating Python bindings from $SCHEMAS_DIR -> $OUT_DIR"
flatc --python -o "$OUT_DIR" "$SCHEMAS_DIR"/*.fbs

# Ensure every generated package dir has an __init__.py
find "$OUT_DIR" -mindepth 2 -type d -exec touch {}/__init__.py \;

# Re-create top-level __init__.py only if missing
if [ ! -f "$OUT_DIR/__init__.py" ]; then
    cat > "$OUT_DIR/__init__.py" <<'EOF'
import sys as _sys
from pathlib import Path as _Path
_this = str(_Path(__file__).resolve().parent)
if _this not in _sys.path:
    _sys.path.insert(0, _this)
EOF
fi

echo "Done."
