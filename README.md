# Quill ✒

**An AI-first, local-first book writing environment.**

Quill is a distraction-free writing platform with deep AI assistance at every layer — sentence completion, prose expansion, consistency auditing, and plot brainstorming — all running locally via [BitNet 2B](https://github.com/microsoft/BitNet) over `llama-server`.

![Quill screenshot](docs/screenshot.png)

---

## Features

| Phase | What it does |
|---|---|
| **Core Loop** | ProseMirror editor · Ghost-text sentence completion (SSE streaming, Tab to accept) · Auto-save · Focus mode |
| **Consistency Engine** | Automated fact extraction per scene · ChromaDB RAG (MiniLM-L6-v2 embeddings) · Story Bible (characters + world rules) · Contradiction auditor |
| **Polish Tools** | 💡 Brainstorm panel (5 plot ideas streamed as cards) · ✨ Describe popover (Sensory / Action / Emotional / Setting expansion) · Scene auto-labels (POV, pacing, tension 1-5) · Writing progress bar |
| **Export** | Markdown · EPUB · PDF · DOCX (via pandoc) |

---

## Stack

| Layer | Technology |
|---|---|
| **Frontend** | Vanilla HTML + JS, ProseMirror, Inter/Lora fonts |
| **Backend** | Python FastAPI, Server-Sent Events |
| **AI** | `llama-server` serving BitNet 2B (or any GGUF model) |
| **RAG** | ChromaDB + `sentence-transformers/all-MiniLM-L6-v2` |
| **Storage** | Local filesystem (`~/.quill/projects/`) — Markdown source of truth |

---

## Quick Start

### 1. Clone

```bash
git clone https://github.com/batteryphil/Quill.git
cd Quill
```

### 2. Python environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. LLM server (BitNet 2B or any GGUF model)

```bash
# Download BitNet 2B GGUF from HuggingFace, then:
llama-server \
  --model models/bitnet-2b.gguf \
  --port 8081 \
  --ctx-size 2048 \
  --n-predict 200
```

> Any `llama.cpp`-compatible model works. BitNet 2B is recommended for CPU speed.

### 4. Start Quill

```bash
bash run.sh
# → open http://127.0.0.1:8000
```

---

## Project Structure

```
quill/
├── backend/
│   ├── main.py          # FastAPI app, router registration
│   ├── generate.py      # SSE generation endpoints (complete, continue, rephrase, brainstorm, describe)
│   ├── projects.py      # Project + scene CRUD, Story Bible, goals
│   ├── extract.py       # Fact extraction pipeline (LLM → characters, events, auto-labels)
│   ├── rag.py           # ChromaDB vector store + MiniLM embeddings
│   ├── audit.py         # Contradiction auditor
│   ├── export.py        # Markdown / EPUB / PDF / DOCX export
│   └── config.py        # Shared config (LLM URL, paths)
├── frontend/
│   ├── index.html       # Main layout (binder · editor · Bible sidebar)
│   ├── css/
│   │   └── main.css     # Full design system (dark mode)
│   └── js/
│       ├── editor.js    # ProseMirror view, word count, progress bar
│       ├── ghost.js     # Ghost-text plugin (Decoration.widget)
│       ├── stream.js    # SSE streaming helper
│       ├── binder.js    # Left sidebar — project tree
│       ├── bible.js     # Right sidebar — Characters + World Rules
│       ├── consistency.js # Contradiction auditor panel
│       ├── brainstorm.js  # 💡 Idea panel
│       ├── describe.js    # ✨ Describe popover
│       └── export.js      # Export modal
├── quill_test.py        # Integration test suite (53 checks)
├── run.sh               # One-command start
└── requirements.txt
```

---

## Keyboard Shortcuts

| Key | Action |
|---|---|
| `Tab` | Accept ghost text |
| `Esc` | Dismiss ghost text |
| `Alt+]` | Request new ghost suggestion |
| `Ctrl+Enter` | Continue writing (full paragraph) |
| `Ctrl+D` | Describe selected text |

---

## Export

Markdown export is always available. For EPUB, PDF, and DOCX:

```bash
sudo apt install pandoc   # Debian/Ubuntu
brew install pandoc       # macOS
```

---

## Tests

```bash
# With server running (bash run.sh &)
python3 quill_test.py
# → 53 checks, 0 failures
```

---

## Architecture Notes

- **Source of truth**: Markdown files under `~/.quill/projects/`. ChromaDB is ephemeral and rebuildable.
- **Prompt budget**: Strict ≤930 token context injection (character cards + RAG summaries + world facts) to keep the 2B model coherent.
- **Ghost text**: Implemented as a ProseMirror `Decoration.widget` so the undo/redo stack is never corrupted.
- **Extraction**: Background fact extraction fires after every scene save; Bible panels poll every 30s.

---

## License

MIT
