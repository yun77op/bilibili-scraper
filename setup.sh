#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REQ_FILE="$SCRIPT_DIR/requirements.txt"

echo "=== setup: install deps ==="

# Detect Python
PYTHON=""
for cmd in python3 python; do
    if command -v "$cmd" &>/dev/null; then
        PYTHON="$cmd"
        break
    fi
done

if [ -z "$PYTHON" ]; then
    echo "ERROR: Python not found. Please install Python 3.10+ first."
    exit 1
fi

echo "Python: $(command -v "$PYTHON") ($($PYTHON --version))"
echo "Requirements: $REQ_FILE"

echo "Installing..."
"$PYTHON" -m pip install -r "$REQ_FILE"

echo ""
echo "Downloading Whisper model (base)..."
WHISPER_MODEL="${WHISPER_MODEL:-base}"
"$PYTHON" -c "
import os, sys
os.environ.setdefault('HF_HOME', 'models/huggingface')
os.environ.setdefault('HUGGINGFACE_HUB_CACHE', 'models/huggingface/hub')
os.environ.setdefault('HF_HUB_DISABLE_XET', '1')

model_name = os.environ.get('WHISPER_MODEL', 'base')
repo_id = model_name if '/' in model_name else f'Systran/faster-whisper-{model_name}'
target = f'models/faster-whisper/{repo_id.replace(chr(47), chr(45))}'
required = ['config.json', 'model.bin', 'tokenizer.json', 'vocabulary.txt']

# Skip if already downloaded
if all(os.path.exists(f'{target}/{f}') and os.path.getsize(f'{target}/{f}') > 0 for f in required):
    print(f'Model already exists: {target}')
    sys.exit(0)

from huggingface_hub import snapshot_download
print(f'Downloading {repo_id} -> {target} ...')
snapshot_download(
    repo_id=repo_id,
    local_dir=target,
    cache_dir='models/hf-download-cache',
    allow_patterns=required,
    max_workers=1,
)
print('Model download complete.')
"

echo ""
echo "Downloading KaTeX assets (for PDF math rendering)..."
KATEX_VERSION="${KATEX_VERSION:-0.16.11}"
KATEX_DIR="$SCRIPT_DIR/vendor/katex"

if [ -f "$KATEX_DIR/katex.min.css" ] && [ -f "$KATEX_DIR/katex.min.js" ] \
    && [ -f "$KATEX_DIR/auto-render.min.js" ] && ls "$KATEX_DIR/fonts/"*.woff2 &>/dev/null; then
    echo "KaTeX assets already exist: $KATEX_DIR"
else
    if ! command -v curl &>/dev/null; then
        echo "ERROR: curl not found. Please install curl first."
        exit 1
    fi
    TMP_KATEX="$(mktemp -d)"
    trap 'rm -rf "$TMP_KATEX"' EXIT
    DOWNLOADED=""
    for url in \
        "https://registry.npmmirror.com/katex/-/katex-${KATEX_VERSION}.tgz" \
        "https://registry.npmjs.org/katex/-/katex-${KATEX_VERSION}.tgz"; do
        echo "Trying $url"
        if curl -fsSL --connect-timeout 8 --max-time 120 "$url" -o "$TMP_KATEX/katex.tgz"; then
            DOWNLOADED="yes"
            break
        fi
    done
    if [ -z "$DOWNLOADED" ]; then
        echo "ERROR: failed to download KaTeX ${KATEX_VERSION} from all mirrors."
        exit 1
    fi
    tar -xzf "$TMP_KATEX/katex.tgz" -C "$TMP_KATEX"
    mkdir -p "$KATEX_DIR/fonts"
    cp "$TMP_KATEX/package/dist/katex.min.css" "$KATEX_DIR/"
    cp "$TMP_KATEX/package/dist/katex.min.js" "$KATEX_DIR/"
    cp "$TMP_KATEX/package/dist/contrib/auto-render.min.js" "$KATEX_DIR/"
    cp "$TMP_KATEX/package/dist/fonts/"*.woff2 "$KATEX_DIR/fonts/"
    echo "KaTeX ${KATEX_VERSION} installed to $KATEX_DIR"
fi

echo ""
echo "Downloading Mermaid assets (for diagram rendering)..."
MERMAID_VERSION="${MERMAID_VERSION:-10.9.3}"
MERMAID_DIR="$SCRIPT_DIR/vendor/mermaid"

if [ -f "$MERMAID_DIR/mermaid.min.js" ]; then
    echo "Mermaid assets already exist: $MERMAID_DIR"
else
    mkdir -p "$MERMAID_DIR"
    DOWNLOADED=""
    for url in \
        "https://registry.npmmirror.com/mermaid/-/mermaid-${MERMAID_VERSION}.tgz" \
        "https://registry.npmjs.org/mermaid/-/mermaid-${MERMAID_VERSION}.tgz"; do
        echo "Trying $url"
        TMP_MERMAID="$(mktemp -d)"
        if curl -fsSL --connect-timeout 8 --max-time 180 "$url" -o "$TMP_MERMAID/mermaid.tgz" \
            && tar -xzf "$TMP_MERMAID/mermaid.tgz" -C "$TMP_MERMAID" \
            && cp "$TMP_MERMAID/package/dist/mermaid.min.js" "$MERMAID_DIR/"; then
            DOWNLOADED="yes"
            rm -rf "$TMP_MERMAID"
            break
        fi
        rm -rf "$TMP_MERMAID"
    done
    if [ -z "$DOWNLOADED" ]; then
        echo "WARNING: failed to download Mermaid ${MERMAID_VERSION}; diagram rendering will fall back to CDN."
    else
        echo "Mermaid ${MERMAID_VERSION} installed to $MERMAID_DIR"
    fi
fi

echo ""
echo "Done."
