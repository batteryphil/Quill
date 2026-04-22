"""Quill backend configuration."""

from pathlib import Path

# ---------------------------------------------------------------------------
# LLM inference
# ---------------------------------------------------------------------------

# Primary: BitNet 2B dedicated server (start with run_bitnet.sh)
# Fallback: existing Qwen 3B server if BitNet isn't running yet
LLAMA_SERVER_URL: str = "http://127.0.0.1:8081"  # swap to 8082 for BitNet

# Generation defaults
DEFAULT_TEMPERATURE: float = 0.7
DEFAULT_TOP_P: float      = 0.9
GHOST_MAX_TOKENS: int     = 25
CONTINUE_MAX_TOKENS: int  = 350
SELF_WRITE_MAX_TOKENS: int = 650

# Hard prompt budget (keeps 2B model coherent)
MAX_PROMPT_TOKENS: int = 930

# Stop sequences — don't generate past scene breaks
STOP_SEQUENCES: list[str] = ["\n\n\n", "---", "***", "# ", "## "]

# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

PROJECTS_DIR: Path = Path.home() / ".quill" / "projects"
PROJECTS_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

HOST: str = "127.0.0.1"
PORT: int  = 8000
