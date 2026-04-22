#!/usr/bin/env bash
# ─── Quill launcher ─────────────────────────────────────────────────────────
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "╔══════════════════════════════════╗"
echo "║          Q U I L L               ║"
echo "║   AI-first book writing env      ║"
echo "╚══════════════════════════════════╝"
echo ""

# ── Install Python deps if needed ──────────────────────────────────────────
if ! python3 -c "import fastapi" &>/dev/null; then
  echo "→ Installing backend dependencies..."
  pip3 install -q -r backend/requirements.txt
fi

# ── Check llama-server ──────────────────────────────────────────────────────
if ! curl -sf http://127.0.0.1:8081/health &>/dev/null; then
  echo "⚠  No LLM server found at :8081"
  echo "   Start llama-server separately, or point config.py to your server."
  echo "   Continuing anyway — some AI features will return errors."
else
  echo "✓  LLM server detected at :8081"
fi

echo ""
echo "→ Starting Quill backend on http://127.0.0.1:8000"
echo "   Open http://127.0.0.1:8000 in your browser"
echo "   Press Ctrl+C to stop"
echo ""

# ── Start FastAPI ────────────────────────────────────────────────────────────
python3 -m uvicorn backend.main:app \
  --host 127.0.0.1 \
  --port 8000 \
  --reload \
  --log-level warning
