"""
Quill Phase 4 — Export pipeline.

Compiles all scenes in structure order into a single Markdown document,
then optionally converts to EPUB, PDF, or DOCX via pandoc.

Markdown is always available. EPUB/PDF/DOCX require pandoc:
  sudo apt install pandoc   # on Debian/Ubuntu

The compiled Markdown format:
  # Title
  ### Genre

  ---

  # Act 1: Act Title

  ## Chapter 1: Chapter Title

  ### Scene Title

  Scene content...

  ---

  [next scene]

[Idea: ...] notes written by the brainstorm tool are stripped from all
exported formats by default.
"""

import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from .projects import _project_dir, _load_json

router = APIRouter(prefix="/api/export", tags=["export"])

# ---------------------------------------------------------------------------
# Pandoc availability check
# ---------------------------------------------------------------------------


def _pandoc_info() -> dict:
    """
    Check if pandoc is installed and return version info.

    Searches PATH and common install locations (/usr/bin, /usr/local/bin,
    ~/.local/bin, ~/.cabal/bin).

    Returns:
        Dict with keys: available (bool), version (str), formats (list).
    """
    import os
    candidates = ["pandoc"]
    extra_dirs = [
        "/usr/bin", "/usr/local/bin",
        str(Path.home() / ".local" / "bin"),
        str(Path.home() / ".cabal" / "bin"),
    ]
    for d in extra_dirs:
        p = Path(d) / "pandoc"
        if p.exists():
            candidates.insert(0, str(p))

    for candidate in candidates:
        try:
            result = subprocess.run(
                [candidate, "--version"],
                capture_output=True, text=True, timeout=5,
                env={**os.environ, "PATH": os.environ.get("PATH", "") +
                     ":" + ":".join(extra_dirs)},
            )
            if result.returncode == 0:
                version = result.stdout.split("\n")[0].replace("pandoc ", "")
                return {
                    "available": True,
                    "version":   version,
                    "formats":   ["markdown", "epub", "pdf", "docx"],
                    "binary":    candidate,
                }
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue

    return {
        "available": False,
        "version":   "",
        "formats":   ["markdown"],
        "install":   "sudo apt install pandoc  # or brew install pandoc",
    }


@router.get("/check")
async def export_check() -> dict:
    """Return export tool availability."""
    return _pandoc_info()


# ---------------------------------------------------------------------------
# Markdown compilation
# ---------------------------------------------------------------------------


_IDEA_PATTERN = re.compile(r"\[Idea:[^\]]*\]", re.IGNORECASE)


def _compile_markdown(
    project: dict,
    author: str,
    include_scene_headers: bool = True,
    strip_notes: bool = True,
) -> str:
    """
    Compile all scenes into a single Markdown document.

    Args:
        project: Project metadata + structure dict.
        author: Author name for frontmatter.
        include_scene_headers: Whether to emit ### Scene Title headers.
        strip_notes: Strip [Idea: ...] brainstorm notes.

    Returns:
        Full Markdown string.
    """
    proj_dir = _project_dir(project["id"])
    parts: list[str] = []

    # ── YAML frontmatter (pandoc-compatible) ───────────────────────────────
    parts.append(
        f"""---
title: "{project['title'].replace('"', "'")}"
author: "{author.replace('"', "'")}"
lang: en
---
"""
    )

    # ── Style guide as preface (if non-empty) ─────────────────────────────
    style_path = proj_dir / "style_guide.md"
    if style_path.exists():
        style_text = style_path.read_text(encoding="utf-8").strip()
        # Only include if writer actually filled it in
        if style_text and "Describe your writing" not in style_text:
            parts.append(f"\n{style_text}\n\n---\n")

    structure = project.get("structure") or _load_json(proj_dir / "structure.json")

    # ── Acts → Chapters → Scenes ──────────────────────────────────────────
    for act in structure.get("acts", []):
        act_title = act.get("title", f"Act {act['id']}")
        parts.append(f"\n# {act_title}\n")

        for chapter in act.get("chapters", []):
            chap_title = chapter.get("title", f"Chapter {chapter['id']}")
            parts.append(f"\n## {chap_title}\n")

            for scene in chapter.get("scenes", []):
                sid   = scene["id"]
                title = scene.get("title", sid)

                if include_scene_headers:
                    parts.append(f"\n### {title}\n")

                scene_path = proj_dir / "scenes" / f"{sid}.md"
                if scene_path.exists():
                    content = scene_path.read_text(encoding="utf-8").strip()
                else:
                    content = ""

                if not content:
                    parts.append("\n*[Empty scene]*\n")
                else:
                    if strip_notes:
                        content = _IDEA_PATTERN.sub("", content).strip()
                    parts.append(f"\n{content}\n")

                parts.append("\n---\n")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Request model
# ---------------------------------------------------------------------------


class ExportRequest(BaseModel):
    """Export configuration."""

    format: Literal["markdown", "epub", "pdf", "docx"] = "markdown"
    author: str = Field("", description="Author name for metadata")
    include_scene_headers: bool = Field(True, description="Emit ### scene title headers")
    strip_notes: bool = Field(True, description="Strip [Idea: ...] brainstorm notes")
    toc: bool = Field(True, description="Include table of contents")


# ---------------------------------------------------------------------------
# MIME types + extensions
# ---------------------------------------------------------------------------

_MIME = {
    "markdown": "text/markdown; charset=utf-8",
    "epub":     "application/epub+zip",
    "pdf":      "application/pdf",
    "docx":     "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

_EXT = {
    "markdown": "md",
    "epub":     "epub",
    "pdf":      "pdf",
    "docx":     "docx",
}

# ---------------------------------------------------------------------------
# Export endpoint
# ---------------------------------------------------------------------------


@router.post("/{project_id}")
async def export_project(project_id: str, req: ExportRequest) -> Response:
    """
    Compile and export the full project.

    Returns the file as a downloadable binary/text response.

    If pandoc is not installed and a non-markdown format is requested,
    returns HTTP 422 with an install instruction.
    """
    proj_dir = _project_dir(project_id)
    meta      = _load_json(proj_dir / "project.json")
    structure = _load_json(proj_dir / "structure.json")
    project   = {**meta, "structure": structure}

    author = req.author.strip() or "Unknown Author"
    title  = meta.get("title", "Untitled")
    slug   = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")

    # ── Compile Markdown ───────────────────────────────────────────────────
    md_text = _compile_markdown(
        project,
        author=author,
        include_scene_headers=req.include_scene_headers,
        strip_notes=req.strip_notes,
    )

    if req.format == "markdown":
        return Response(
            content=md_text.encode("utf-8"),
            media_type=_MIME["markdown"],
            headers={
                "Content-Disposition": f'attachment; filename="{slug}.md"',
            },
        )

    # ── Pandoc formats ─────────────────────────────────────────────────────
    info = _pandoc_info()
    if not info["available"]:
        raise HTTPException(
            422,
            detail=(
                f"pandoc is not installed. Install it with: {info.get('install', 'install pandoc')}. "
                "Markdown export is available without pandoc."
            ),
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        input_md   = tmp / "manuscript.md"
        output_file = tmp / f"{slug}.{_EXT[req.format]}"

        input_md.write_text(md_text, encoding="utf-8")

        cmd = [
            info.get("binary", "pandoc"),
            str(input_md),
            "-o", str(output_file),
            "--metadata", f"title={title}",
            "--metadata", f"author={author}",
            "--standalone",
        ]

        if req.toc and req.format in ("epub", "docx"):
            cmd += ["--toc", "--toc-depth=2"]

        if req.format == "pdf":
            # Use a robust PDF engine if available
            cmd += ["--pdf-engine=wkhtmltopdf"]
            # Fallback: try without engine flag if wkhtmltopdf missing
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            except Exception:
                result = None
            if result is None or result.returncode != 0:
                cmd_fallback = [c for c in cmd if "--pdf-engine" not in c]
                result = subprocess.run(
                    cmd_fallback, capture_output=True, text=True, timeout=60
                )
        else:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

        if result.returncode != 0:
            raise HTTPException(
                500,
                detail=f"pandoc error: {result.stderr[:500]}"
            )

        if not output_file.exists():
            raise HTTPException(500, detail="pandoc produced no output file")

        file_bytes = output_file.read_bytes()

    return Response(
        content=file_bytes,
        media_type=_MIME[req.format],
        headers={
            "Content-Disposition": f'attachment; filename="{slug}.{_EXT[req.format]}"',
        },
    )
