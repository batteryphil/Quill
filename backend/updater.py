"""
Quill — Self-update system.

Compares the locally installed git commit against the GitHub remote and
applies updates in-place via git pull + pip install, then restarts
the server using os.execv (clean in-process replace, no PID file needed).

Endpoints
─────────
  GET  /api/update/check    Compare local SHA vs GitHub remote
  POST /api/update/apply    Stream update progress (SSE), then restart
  GET  /api/update/status   Current local version info
"""

import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import APIRouter
from fastapi.responses import StreamingResponse

router = APIRouter(prefix="/api/update", tags=["update"])

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GITHUB_REPO  = "batteryphil/Quill"
GITHUB_OWNER = "batteryphil"
GITHUB_BRANCH = "main"

# Resolve quill install directory (two levels up from this file)
QUILL_DIR = Path(__file__).parent.parent.resolve()

# ---------------------------------------------------------------------------
# Git helpers (sync, run in executor to avoid blocking event loop)
# ---------------------------------------------------------------------------


def _git(*args: str) -> str:
    """
    Run a git command in QUILL_DIR and return stdout (stripped).

    Args:
        *args: Git sub-command arguments.

    Returns:
        stdout string on success, empty string on failure.
    """
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(QUILL_DIR),
            capture_output=True,
            text=True,
            timeout=15,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


def _local_sha() -> str:
    """Return the current HEAD commit SHA (full 40-char)."""
    return _git("rev-parse", "HEAD")


def _local_short_sha() -> str:
    """Return abbreviated 7-char local SHA."""
    return _git("rev-parse", "--short", "HEAD")


def _local_commit_date() -> str:
    """Return ISO commit date of HEAD."""
    return _git("log", "-1", "--format=%ci", "HEAD")


def _local_commit_message() -> str:
    """Return first-line commit message of HEAD."""
    return _git("log", "-1", "--pretty=%s", "HEAD")


def _is_git_repo() -> bool:
    """Return True if QUILL_DIR is a git repository."""
    return bool(_git("rev-parse", "--show-toplevel"))


# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------


async def _remote_sha() -> str:
    """
    Fetch the latest commit SHA from GitHub API for the configured branch.

    Returns:
        40-char SHA or empty string on failure.
    """
    url = f"https://api.github.com/repos/{GITHUB_REPO}/commits/{GITHUB_BRANCH}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(url, headers={"Accept": "application/vnd.github.v3+json"})
            if r.status_code == 200:
                return r.json().get("sha", "")
    except Exception:
        pass
    return ""


async def _commits_between(base_sha: str, head_sha: str) -> list[dict]:
    """
    Fetch the list of commits between base_sha and head_sha via GitHub compare API.

    Args:
        base_sha:  Local (older) commit SHA.
        head_sha:  Remote (newer) commit SHA.

    Returns:
        List of dicts with keys: sha, message, date, author.
    """
    if base_sha == head_sha:
        return []
    url = f"https://api.github.com/repos/{GITHUB_REPO}/compare/{base_sha}...{head_sha}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(url, headers={"Accept": "application/vnd.github.v3+json"})
            if r.status_code == 200:
                data = r.json()
                commits = []
                for c in reversed(data.get("commits", [])):
                    msg     = c["commit"]["message"].split("\n")[0]  # first line only
                    date    = c["commit"]["committer"]["date"]
                    author  = c["commit"]["author"]["name"]
                    commits.append({
                        "sha":     c["sha"][:7],
                        "message": msg,
                        "date":    date,
                        "author":  author,
                    })
                return commits
    except Exception:
        pass
    return []


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/status")
async def get_status() -> dict:
    """
    Return the local version of Quill (git commit info).

    Returns:
        Dict with sha, short_sha, date, message, is_git_repo.
    """
    if not _is_git_repo():
        return {"is_git_repo": False, "sha": "", "short_sha": "", "date": "", "message": ""}

    loop = asyncio.get_event_loop()
    sha, short, date, msg = await asyncio.gather(
        loop.run_in_executor(None, _local_sha),
        loop.run_in_executor(None, _local_short_sha),
        loop.run_in_executor(None, _local_commit_date),
        loop.run_in_executor(None, _local_commit_message),
    )
    return {
        "is_git_repo": True,
        "sha":         sha,
        "short_sha":   short,
        "date":        date,
        "message":     msg,
        "repo":        GITHUB_REPO,
        "branch":      GITHUB_BRANCH,
    }


@router.get("/check")
async def check_update() -> dict:
    """
    Compare the installed version against the GitHub remote.

    Returns:
        Dict with has_update (bool), local_sha, remote_sha,
        and commits (list) of new commits available.
    """
    if not _is_git_repo():
        return {
            "has_update":   False,
            "error":        "Not a git repository — manual install may not support auto-update",
            "local_sha":    "",
            "remote_sha":   "",
            "commits":      [],
        }

    loop       = asyncio.get_event_loop()
    local_sha  = await loop.run_in_executor(None, _local_sha)
    remote_sha = await _remote_sha()

    if not remote_sha:
        return {
            "has_update":   False,
            "error":        "Could not reach GitHub — check internet connection",
            "local_sha":    local_sha,
            "remote_sha":   "",
            "commits":      [],
        }

    has_update = local_sha != remote_sha
    commits    = await _commits_between(local_sha, remote_sha) if has_update else []

    return {
        "has_update":   has_update,
        "local_sha":    local_sha[:7],
        "remote_sha":   remote_sha[:7],
        "commits":      commits,
        "checked_at":   datetime.now(timezone.utc).isoformat(),
    }


@router.post("/apply")
async def apply_update() -> StreamingResponse:
    """
    Pull the latest code and restart the server.

    Streams progress as SSE events:
        data: {"step": "git"|"pip"|"done"|"error", "line": "..."}
        data: RESTART   — signals client to poll until server is back

    After RESTART is emitted, the server reloads itself via os.execv.
    """
    return StreamingResponse(
        _update_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


async def _update_stream():
    """
    Generator that runs git pull + pip install and streams progress.

    Yields:
        SSE-formatted byte strings.
    """
    def _emit(step: str, line: str) -> str:
        return f"data: {json.dumps({'step': step, 'line': line})}\n\n"

    if not _is_git_repo():
        yield _emit("error", "✗ Not a git repository — cannot auto-update")
        yield "data: DONE\n\n"
        return

    # ── Step 1: git pull ────────────────────────────────────────────────────
    yield _emit("git", f"🔄 Pulling from {GITHUB_REPO} ({GITHUB_BRANCH})…")

    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "pull", "origin", GITHUB_BRANCH,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(QUILL_DIR),
        )
        async for raw_line in proc.stdout:
            line = raw_line.decode(errors="replace").rstrip()
            if line:
                yield _emit("git", line)
        await proc.wait()

        if proc.returncode != 0:
            yield _emit("error", "✗ git pull failed — check the log above")
            yield "data: DONE\n\n"
            return

        yield _emit("git", "✓ Code updated")
    except FileNotFoundError:
        yield _emit("error", "✗ git not found — install git and try again")
        yield "data: DONE\n\n"
        return

    # ── Step 2: pip install ─────────────────────────────────────────────────
    yield _emit("pip", "📦 Installing / updating dependencies…")

    pip_exe = str(Path(sys.executable).parent / "pip")
    if not Path(pip_exe).exists():
        pip_exe = sys.executable  # fallback: python -m pip
        pip_cmd = [pip_exe, "-m", "pip", "install", "-r", "requirements.txt", "-q"]
    else:
        pip_cmd = [pip_exe, "install", "-r", "requirements.txt", "-q"]

    try:
        proc2 = await asyncio.create_subprocess_exec(
            *pip_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(QUILL_DIR),
        )
        async for raw_line in proc2.stdout:
            line = raw_line.decode(errors="replace").rstrip()
            if line:
                yield _emit("pip", line)
        await proc2.wait()

        if proc2.returncode != 0:
            yield _emit("pip", "⚠ pip install returned non-zero — continuing anyway")
        else:
            yield _emit("pip", "✓ Dependencies up to date")
    except Exception as exc:
        yield _emit("pip", f"⚠ pip failed: {exc} — continuing")

    # ── Step 3: Restart ─────────────────────────────────────────────────────
    yield _emit("done", "🚀 Restarting server… reconnecting in a few seconds.")
    yield "data: RESTART\n\n"

    # Schedule restart after the stream response closes
    asyncio.ensure_future(_restart_server())


async def _restart_server() -> None:
    """
    Replace the current process with a fresh uvicorn instance.

    Uses os.execv so the PID stays the same (helpful for process managers).
    Waits 1.5 s to let the SSE response flush before replacing.
    """
    await asyncio.sleep(1.5)
    os.execv(
        sys.executable,
        [
            sys.executable, "-m", "uvicorn",
            "backend.main:app",
            "--host", "127.0.0.1",
            "--port", "8000",
            "--log-level", "warning",
        ],
    )
