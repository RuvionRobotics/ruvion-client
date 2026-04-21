#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$REPO_ROOT/ruvion-client"
VENV_DIR="$REPO_ROOT/.venv"
EXAMPLES_DIR="$PROJECT_DIR/examples"

# ── venv ─────────────────────────────────────────────────────────────────────
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment..."
    python3 -m venv "$VENV_DIR"
fi

PYTHON="$VENV_DIR/bin/python"
PIP="$VENV_DIR/bin/pip"

# ── dependencies ──────────────────────────────────────────────────────────────
echo "Updating dependencies..."
"$PIP" install --quiet --upgrade pip
"$PIP" install --quiet -e "$PROJECT_DIR"

# ── proto generation ──────────────────────────────────────────────────────────
SCHEMAS_DIR="$REPO_ROOT/submodules/motion-control-protocol/schemas"

if [ ! -d "$SCHEMAS_DIR" ]; then
    echo "Error: submodule not initialised. Run:"
    echo "  git submodule update --init --recursive"
    exit 1
fi

if ! command -v flatc >/dev/null 2>&1; then
    echo "Error: flatc not found. Install with:"
    echo "  brew install flatbuffers"
    exit 1
fi

echo "Generating FlatBuffers bindings..."
bash "$PROJECT_DIR/scripts/generate_proto.sh"

# ── config ───────────────────────────────────────────────────────────────────
CONFIG_FILE="$EXAMPLES_DIR/config.py"
if [ ! -f "$CONFIG_FILE" ]; then
    cp "$EXAMPLES_DIR/config.example.py" "$CONFIG_FILE"
    echo ""
    echo "Created $CONFIG_FILE from template — edit CERT_DIR before running examples."
    exit 0
fi

# ── example selection ─────────────────────────────────────────────────────────
examples=()
while IFS= read -r -d '' f; do
    examples+=("$f")
done < <(find "$EXAMPLES_DIR" -maxdepth 1 -name "*.py" \
    ! -name "config.py" ! -name "config.example.py" -print0 | sort -z)

if [ ${#examples[@]} -eq 0 ]; then
    echo "No examples found in $EXAMPLES_DIR"
    exit 1
fi

echo ""
echo "Available examples:"
for i in "${!examples[@]}"; do
    printf "  [%d] %s\n" "$((i+1))" "$(basename "${examples[$i]}")"
done
echo ""

while true; do
    read -rp "Select example [1-${#examples[@]}]: " choice
    if [[ "$choice" =~ ^[0-9]+$ ]] && [ "$choice" -ge 1 ] && [ "$choice" -le "${#examples[@]}" ]; then
        break
    fi
    echo "Invalid choice, try again."
done

selected="${examples[$((choice-1))]}"
echo ""
echo "Running $(basename "$selected")..."
echo "────────────────────────────────────────"
"$PYTHON" "$selected"
