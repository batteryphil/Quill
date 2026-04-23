"""
Quill — Full Book AI Writer.

Generates a complete novel outline then writes it scene-by-scene,
maintaining narrative consistency via RAG context injection between scenes.

Each scene is saved as a real project scene so the user can read,
edit, and export the book at any time.

Endpoints
─────────
  POST /api/book/start                  Create + start a generation job
  GET  /api/book/jobs/{project_id}      List jobs for a project
  GET  /api/book/{job_id}               Job status / progress
  GET  /api/book/{job_id}/stream        SSE: live tokens + milestones
  POST /api/book/{job_id}/pause         Pause after current scene
  POST /api/book/{job_id}/resume        Resume a paused job
  POST /api/book/{job_id}/cancel        Cancel a running job
"""

import asyncio
import json
import yaml
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from . import config as cfg
from .providers import get_active_provider
from .rag import upsert_scene, build_rag_context

router = APIRouter(prefix="/api/book", tags=["bookwriter"])

# ---------------------------------------------------------------------------
# Configuration model
# ---------------------------------------------------------------------------

GENRES = [
    "literary fiction", "mystery", "thriller", "fantasy", "science fiction",
    "horror", "romance", "historical fiction", "adventure", "young adult",
    "crime", "dystopian", "magical realism",
]


class BookConfig(BaseModel):
    """Full book generation configuration."""

    project_id:        str   = Field(...,  description="Target project ID")
    premise:           str   = Field(...,  min_length=50, description="Book premise (1–3 paragraphs)")
    genre:             str   = Field("literary fiction")
    protagonist:       str   = Field("",   description="Main character name and brief description")
    antagonist:        str   = Field("",   description="Antagonist/conflict description")
    setting:           str   = Field("",   description="World/time/place description")
    tone:              str   = Field("balanced")
    pov:               str   = Field("third person limited")
    target_words:      int   = Field(50_000, ge=5_000,  le=150_000)
    num_chapters:      int   = Field(20,     ge=3,       le=60)
    scenes_per_chapter:int   = Field(3,      ge=1,       le=6)


# ---------------------------------------------------------------------------
# Job data structure
# ---------------------------------------------------------------------------


class BookJob:
    """In-process book generation job (not a dataclass to allow asyncio fields)."""

    def __init__(self, job_id: str, project_id: str, config: dict) -> None:
        """Initialise a new BookJob."""
        self.job_id      = job_id
        self.project_id  = project_id
        self.config      = config
        self.status      = "pending"   # pending|running|paused|done|error|cancelled
        self.outline:    dict  = {}    # {title, acts: [{name, chapters: [{title, scenes: [str]}]}]}
        self.total_scenes:  int = 0
        self.done_scenes:   int = 0
        self.total_words:   int = 0
        self.current_chapter: int = 0
        self.current_scene:   int = 0
        self.current_title:   str = ""
        self.current_tokens:  str = ""   # live tokens for streaming scene
        self.error:       str = ""
        self.created_at   = datetime.now(timezone.utc).isoformat()
        self.completed_at = ""
        self.start_time:  Optional[float] = None
        # Async control
        self._pause_event   = asyncio.Event()
        self._pause_event.set()   # set = running; cleared = paused
        self._cancel_flag   = False
        # SSE subscriber queues (one per open stream connection)
        self._subscribers:  list[asyncio.Queue] = []
        # Scene list for the dashboard [{id, chapter, scene, title, words}]
        self.scenes_written: list[dict] = []

    def to_dict(self) -> dict:
        """Serialise (without asyncio fields) for the status endpoint."""
        elapsed  = None
        rate_wpm = None
        eta_min  = None
        if self.start_time and self.done_scenes > 0:
            import time
            elapsed  = time.time() - self.start_time
            rate_wpm = self.total_words / (elapsed / 60)
            remaining_words = (self.total_scenes - self.done_scenes) * (
                self.total_words / self.done_scenes if self.done_scenes else 600
            )
            eta_min = int(remaining_words / rate_wpm) if rate_wpm > 0 else None

        return {
            "job_id":          self.job_id,
            "project_id":      self.project_id,
            "status":          self.status,
            "outline":         self.outline,
            "total_scenes":    self.total_scenes,
            "done_scenes":     self.done_scenes,
            "total_words":     self.total_words,
            "current_chapter": self.current_chapter,
            "current_scene":   self.current_scene,
            "current_title":   self.current_title,
            "created_at":      self.created_at,
            "completed_at":    self.completed_at,
            "elapsed_s":       elapsed,
            "eta_min":         eta_min,
            "scenes_written":  self.scenes_written,
            "error":           self.error,
        }


# In-memory registry (survives between requests, lost on server restart)
_JOBS: dict[str, BookJob] = {}


# ---------------------------------------------------------------------------
# Outline generation
# ---------------------------------------------------------------------------

_OUTLINE_SYSTEM = (
    "You are an expert novelist creating a detailed book outline. "
    "Output ONLY valid Markdown format — absolutely no JSON, no backticks, no other text.\n\n"
    "Required format:\n"
    "# Act 1: Setup\n"
    "## Chapter 1\n"
    "- Scene beat description here.\n"
    "- Another scene beat.\n"
    "## Chapter 2\n"
    "- Scene beat description here.\n"
)


async def _generate_outline(config: dict) -> dict:
    """
    Call the active LLM provider to generate a structured book outline.
    Uses the Open/Close workflow: 
    1. Generates 3 concepts.
    2. Evaluates and expands the best concept into JSON.
    """
    n_chaps  = config["num_chapters"]
    spc      = config["scenes_per_chapter"]
    n_acts   = 3
    per_act  = n_chaps // n_acts

    # --- Phase 1: OPEN ---
    open_msg = (
        f"Genre: {config['genre']}\n"
        f"Premise: {config['premise']}\n"
        f"Protagonist: {config.get('protagonist') or 'Not specified'}\n"
        f"Antagonist: {config.get('antagonist') or 'Not specified'}\n"
        f"Setting: {config.get('setting') or 'Not specified'}\n"
        f"Tone: {config.get('tone', 'balanced')}\n"
        f"POV: {config.get('pov', 'third person limited')}\n"
        f"Structure: {n_acts} acts, {n_chaps} chapters, {spc} scenes per chapter.\n"
        f"Chapters per act: roughly {per_act}.\n\n"
        "Generate 3 completely different structural concepts for this outline based on the premise.\n"
        "Just describe 3 different ways the plot could unfold across the acts. "
        "Number them Concept 1, Concept 2, and Concept 3."
    )

    provider = get_active_provider()
    open_resp = ""
    
    async for token in provider.stream(
        messages=[
            {"role": "system", "content": "You are an expert novelist brainstorming story arcs. Be highly creative."},
            {"role": "user", "content": open_msg}
        ],
        max_tokens=1500,
        temperature=0.8,
        stop=[],
    ):
        open_resp += token

    # --- Phase 2: CLOSE ---
    close_msg = (
        "Here are 3 structural concepts for the book:\n\n"
        f"{open_resp}\n\n"
        "Evaluate these 3 paths and select the one with the strongest emotional arc, richest conflict, and best pacing. "
        "Discard the other two. "
        "Map the selected path into a complete, highly detailed outline.\n\n"
        "Generate the complete outline in exactly the Markdown format requested by the system prompt. Do not output anything else."
    )

    full_text = ""
    # 40 scenes × ~50 words/scene in JSON overhead ≈ 3200 tokens needed
    async for token in provider.stream(
        messages=[
            {"role": "system", "content": _OUTLINE_SYSTEM},
            {"role": "user", "content": close_msg}
        ],
        max_tokens=3200,
        temperature=0.5,
        stop=[],
    ):
        full_text += token

    return _parse_outline(full_text, config)


def _parse_outline(text: str, config: dict) -> dict:
    """
    Parse a Markdown-style outline.
    Expected format:
    # Act...
    ## Chapter...
    - Scene...
    """
    lines = text.strip().split("\n")
    acts = []
    current_act = None
    current_chap = None
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
            
        if line.startswith("# "):
            if current_chap and current_act:
                current_act["chapters"].append(current_chap)
            if current_act:
                acts.append(current_act)
            current_act = {"name": line[2:].strip(), "chapters": []}
            current_chap = None
        elif line.startswith("## "):
            if current_chap and current_act:
                current_act["chapters"].append(current_chap)
            current_chap = {"title": line[3:].strip(), "scenes": []}
        elif line.startswith("- ") or line.startswith("* "):
            if current_chap:
                current_chap["scenes"].append(line[2:].strip())
                
    if current_chap and current_act:
        current_act["chapters"].append(current_chap)
    if current_act:
        acts.append(current_act)

    if acts:
        return {"title": config.get("premise", "Untitled")[:50] + "...", "acts": acts}

    # Fallback if markdown parsing completely failed
    print("[Quill] Outline Parse Error: Could not extract markdown outline structure.")
    return _synthesise_outline(config)
    return _synthesise_outline(config)


def _synthesise_outline(config: dict) -> dict:
    """
    Build a generic 3-act outline when LLM output can't be parsed.

    Args:
        config: BookConfig dict with num_chapters and scenes_per_chapter.

    Returns:
        Outline dict with placeholder chapter/scene titles.
    """
    n_chaps = config["num_chapters"]
    spc     = config["scenes_per_chapter"]
    acts    = []
    chap_n  = 0
    act_defs = [
        ("Act 1: Setup",           n_chaps // 4),
        ("Act 2: Confrontation",   n_chaps // 2),
        ("Act 3: Resolution",      n_chaps - n_chaps // 4 - n_chaps // 2),
    ]
    for act_name, count in act_defs:
        chapters = []
        for i in range(count):
            chap_n += 1
            chapters.append({
                "title":  f"Chapter {chap_n}",
                "scenes": [f"Scene {s+1} of chapter {chap_n}" for s in range(spc)],
            })
        acts.append({"name": act_name, "chapters": chapters})

    return {"title": "Untitled Novel", "acts": acts}


# ---------------------------------------------------------------------------
# Scene writing
# ---------------------------------------------------------------------------

_SCENE_SYSTEM_TMPL = (
    "You are a skilled {genre} novelist writing in {pov} POV. "
    "Write vivid, immersive, character-driven prose. Show, don't tell. "
    "No chapter headers or scene numbers — just continuous prose. "
    "Target length: ~{words} words.\n\n"
    "STORY CONTEXT:\n"
    "Title: {title}\n"
    "Premise: {premise}\n"
    "{char_block}"
    "Setting: {setting}"
)


async def _write_scene(
    config:          dict,
    chapter_title:   str,
    scene_beat:      str,
    prev_ending:     str,
    rag_context:     str,
    words_per_scene: int,
    book_title:      str = "",
) -> AsyncIterator[str]:
    """
    Stream prose tokens for a single scene.

    Args:
        config:          BookConfig dict.
        chapter_title:   Title of the current chapter.
        scene_beat:      One-sentence description of what must happen.
        prev_ending:     Last ~150 words of the previous scene.
        rag_context:     RAG-assembled character + prior scene context.
        words_per_scene: Target word count for this scene.
        book_title:      Outline title for the system prompt.

    Yields:
        Raw text tokens from the LLM provider.
    """
    protagonist = config.get("protagonist", "").strip()
    antagonist  = config.get("antagonist", "").strip()
    char_block  = ""
    if protagonist:
        char_block += f"Protagonist: {protagonist}\n"
    if antagonist:
        char_block += f"Antagonist:  {antagonist}\n"
    if char_block:
        char_block += "\n"

    system = _SCENE_SYSTEM_TMPL.format(
        genre   = config.get("genre", "fiction"),
        pov     = config.get("pov", "third person limited"),
        words   = words_per_scene,
        title   = book_title or "Untitled",
        premise = config.get("premise", "")[:400],   # cap to ~300 tokens
        char_block = char_block,
        setting = config.get("setting", "not specified"),
    )

    context_parts = []
    if rag_context:
        context_parts.append(f"STORY CONTEXT:\n{rag_context}")
    if prev_ending:
        context_parts.append(f"PREVIOUS SCENE ENDED:\n{prev_ending}")

    provider = get_active_provider()

    # --- Phase 1: OPEN ---
    open_msg = "\n\n".join(context_parts) + (
        f"\n\nChapter: {chapter_title}\n"
        f"Scene: {scene_beat}\n\n"
        "Generate 3 distinct narrative approaches to execute this scene beat (e.g., action-heavy vs introspective vs dialogue-driven). "
        "Do NOT write the full scene yet. Just outline 3 different ways to approach it stylistically and narratively. "
        "Number them Approach 1, Approach 2, and Approach 3."
    )

    open_sys = "You are an expert novelist experimenting with narrative choices. Be creative."
    open_resp = ""
    async for token in provider.stream(
        messages=[{"role": "system", "content": open_sys}, {"role": "user", "content": open_msg}],
        max_tokens=1000,
        temperature=0.8,
        stop=[],
    ):
        open_resp += token

    # --- Phase 2: CLOSE ---
    close_msg = "\n\n".join(context_parts) + (
        f"\n\nChapter: {chapter_title}\n"
        f"Scene: {scene_beat}\n\n"
        "Here are 3 possible narrative approaches to execute this scene:\n\n"
        f"{open_resp}\n\n"
        "Evaluate them and select the absolute best approach with the strongest emotional impact and distinctive voice. "
        "Now, write the complete scene using ONLY the selected approach. "
        "Write the scene now:"
    )

    max_tokens = min(int(words_per_scene * 1.5), cfg.SELF_WRITE_MAX_TOKENS)

    async for token in provider.stream(
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": close_msg},
        ],
        max_tokens=max_tokens,
        temperature=0.72,
        stop=["---", "***", "# ", "## "],
    ):
        yield token


# ---------------------------------------------------------------------------
# Project filesystem helpers
# ---------------------------------------------------------------------------


def _project_dir(project_id: str) -> Path:
    """Return path to project root directory."""
    return cfg.PROJECTS_DIR / project_id


def _scene_id(act: int, chapter: int, scene_num: int) -> str:
    """Build scene ID string, e.g. a2_c05_s003."""
    return f"a{act}_c{chapter:02d}_s{scene_num:03d}"


def _save_scene_to_project(
    project_id: str,
    act: int,
    chapter_num: int,
    scene_num: int,
    chapter_title: str,
    scene_title: str,
    content: str,
) -> str:
    """
    Write scene content to the project filesystem and update structure.json.

    Args:
        project_id:    Project identifier.
        act:           Act number (1–3).
        chapter_num:   Chapter number.
        scene_num:     Scene number within chapter.
        chapter_title: Title of the chapter.
        scene_title:   Title/beat of the scene.
        content:       Full prose text.

    Returns:
        The generated scene_id string.
    """
    proj_dir  = _project_dir(project_id)
    sid       = _scene_id(act, chapter_num, scene_num)
    scene_dir = proj_dir / "scenes"
    scene_dir.mkdir(parents=True, exist_ok=True)

    # Write prose
    scene_file = scene_dir / f"{sid}.md"
    scene_file.write_text(content, encoding="utf-8")

    # Update structure.json
    struct_path = proj_dir / "structure.json"
    structure   = json.loads(struct_path.read_text()) if struct_path.exists() else {"acts": []}

    # Ensure act exists
    while len(structure["acts"]) < act:
        a_idx = len(structure["acts"]) + 1
        structure["acts"].append({"id": a_idx, "title": f"Act {a_idx}", "chapters": []})

    act_data = structure["acts"][act - 1]

    # Ensure chapter exists within act
    while len(act_data["chapters"]) < chapter_num:
        c_idx = len(act_data["chapters"]) + 1
        act_data["chapters"].append({"id": c_idx, "title": f"Chapter {c_idx}", "scenes": []})

    chap_data = act_data["chapters"][chapter_num - 1]
    if chapter_title and chap_data.get("title", "").startswith("Chapter "):
        chap_data["title"] = chapter_title

    # Ensure scene slot exists
    scene_entry = {
        "id":         sid,
        "title":      scene_title[:80],
        "word_count": len(content.split()),
        "pov":        "",
        "pacing":     "",
        "tension":    3,
        "brainstorm": "",
    }
    existing_ids = [s["id"] for s in chap_data.get("scenes", [])]
    if sid not in existing_ids:
        chap_data.setdefault("scenes", []).append(scene_entry)
    else:
        chap_data["scenes"] = [
            scene_entry if s["id"] == sid else s
            for s in chap_data["scenes"]
        ]

    struct_path.write_text(json.dumps(structure, indent=2), encoding="utf-8")

    # Update project word count
    meta_path = proj_dir / "project.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text())
        meta["word_count"] = meta.get("word_count", 0) + len(content.split())
        meta["updated"]    = datetime.now(timezone.utc).isoformat()
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    return sid


# ---------------------------------------------------------------------------
# Job event broadcasting
# ---------------------------------------------------------------------------


def _seed_project_context(project_id: str, config: dict) -> None:
    """
    Write protagonist, antagonist, and world facts into the project state
    so that build_rag_context() has content to inject into every scene prompt.

    Creates/overwrites:
        state/characters.json  — character cards for protagonist + antagonist
        style_guide.md         — premise, setting, genre, tone

    Args:
        project_id: Target project ID.
        config:     BookConfig dict with protagonist, antagonist, setting, etc.
    """
    proj_dir   = _project_dir(project_id)
    state_dir  = proj_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    # ── Character cards ───────────────────────────────────────────────────
    chars: dict = {}

    protagonist = config.get("protagonist", "").strip()
    antagonist  = config.get("antagonist", "").strip()

    if protagonist:
        # Extract just the name (first token before comma)
        name = protagonist.split(",")[0].split("—")[0].strip()
        chars[name] = {
            "name":        name,
            "role":        "protagonist",
            "description": protagonist,
            "traits":      "",
            "arc":         "",
        }

    if antagonist:
        name = antagonist.split(",")[0].split("—")[0].strip()
        chars[name] = {
            "name":        name,
            "role":        "antagonist",
            "description": antagonist,
            "traits":      "",
            "arc":         "",
        }

    char_path = state_dir / "characters.json"
    char_path.write_text(json.dumps(chars, indent=2, ensure_ascii=False), encoding="utf-8")

    # ── Style guide (world facts, premise, tone) ──────────────────────────
    style_guide = (
        f"# Book Bible\n\n"
        f"## Genre\n{config.get('genre', 'fiction')}\n\n"
        f"## Tone\n{config.get('tone', 'balanced')}\n\n"
        f"## Point of View\n{config.get('pov', 'third person limited')}\n\n"
        f"## Setting\n{config.get('setting', 'Not specified')}\n\n"
        f"## Premise\n{config.get('premise', '')}\n\n"
    )
    if protagonist:
        style_guide += f"## Protagonist\n{protagonist}\n\n"
    if antagonist:
        style_guide += f"## Antagonist\n{antagonist}\n\n"

    (proj_dir / "style_guide.md").write_text(style_guide, encoding="utf-8")



def _emit(job: BookJob, event: dict) -> None:
    """
    Push an SSE event to all current subscribers of this job.

    Args:
        job:   The BookJob instance.
        event: Dict to serialise as SSE data payload.
    """
    for q in list(job._subscribers):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass


# ---------------------------------------------------------------------------
# Main generation coroutine
# ---------------------------------------------------------------------------


async def _run_book_job(job_id: str) -> None:
    """
    Background coroutine: generate outline → write all scenes → done.

    Checks pause/cancel flags between every scene.
    Saves each scene to the project as it's written.
    """
    import time

    job = _JOBS.get(job_id)
    if not job:
        return

    job.status     = "running"
    job.start_time = time.time()

    try:
        # ── 1. Generate outline ───────────────────────────────────────────
        _emit(job, {"type": "status", "status": "outline"})
        try:
            outline = await _generate_outline(job.config)
        except Exception as outline_err:
            import traceback
            print(f"[Quill] Outline generation error: {outline_err}")
            traceback.print_exc()
            outline = _synthesise_outline(job.config)
        job.outline = outline
        _emit(job, {"type": "outline_ready", "outline": outline, "title": outline.get("title", "")})

        # ── 2. Seed characters + world facts into project state ────────────
        try:
            _seed_project_context(job.project_id, job.config)
        except Exception as seed_err:
            print(f"[Quill] Seed context error (non-fatal): {seed_err}")

        # Flatten scenes
        flat_scenes: list[dict] = []
        for act_idx, act in enumerate(outline.get("acts", []), start=1):
            for chap_idx, chap in enumerate(act.get("chapters", []), start=1):
                for scene_idx, beat in enumerate(chap.get("scenes", []), start=1):
                    flat_scenes.append({
                        "act":    act_idx,
                        "chapter": chap_idx,
                        "scene":  scene_idx,
                        "chapter_title": chap.get("title", f"Chapter {chap_idx}"),
                        "beat":   beat if isinstance(beat, str) else str(beat),
                    })

        job.total_scenes = len(flat_scenes)
        _emit(job, {"type": "total_scenes", "total": job.total_scenes})

        words_per_scene = max(200, job.config.get("target_words", 50_000) // max(len(flat_scenes), 1))
        words_per_scene = min(words_per_scene, 800)  # cap for 2B model sanity

        prev_ending = ""

        # ── 2. Write scenes ───────────────────────────────────────────────
        for s in flat_scenes:
            # ── Pause checkpoint ─────────────────────────────────────────
            await job._pause_event.wait()

            # ── Cancel checkpoint ────────────────────────────────────────
            if job._cancel_flag:
                job.status = "cancelled"
                _emit(job, {"type": "cancelled"})
                return

            job.current_chapter = s["chapter"]
            job.current_scene   = s["scene"]
            job.current_title   = s["beat"][:80]
            job.current_tokens  = ""

            _emit(job, {
                "type":    "scene_start",
                "act":     s["act"],
                "chapter": s["chapter"],
                "scene":   s["scene"],
                "chapter_title": s["chapter_title"],
                "beat":    s["beat"],
                "done":    job.done_scenes,
                "total":   job.total_scenes,
            })

            # ── Build RAG context ─────────────────────────────────────────
            try:
                rag_ctx = await build_rag_context(
                    project_id=job.project_id,
                    query_text=s["beat"],
                    active_characters=[],
                )
            except Exception:
                rag_ctx = ""

            # ── Stream scene tokens ───────────────────────────────────────
            scene_text = ""
            _rep_check_buf = ""   # sliding window for duplicate detection
            async for token in _write_scene(
                config=job.config,
                chapter_title=s["chapter_title"],
                scene_beat=s["beat"],
                prev_ending=prev_ending,
                rag_context=rag_ctx,
                words_per_scene=words_per_scene,
                book_title=job.outline.get("title", ""),
            ):
                scene_text         += token
                job.current_tokens += token
                _rep_check_buf     += token
                _emit(job, {"type": "token", "text": token})

                # ── Repetition detector ───────────────────────────────────
                # Every ~80 tokens check if the latest paragraph is a copy
                if len(_rep_check_buf) > 300:
                    paragraphs = [p.strip() for p in scene_text.split("\n") if len(p.strip()) > 60]
                    if len(paragraphs) >= 2:
                        last_para = paragraphs[-1]
                        # If the last paragraph appears verbatim earlier, stop
                        if last_para in "\n".join(paragraphs[:-1]):
                            # Trim the repeated tail before saving
                            idx = scene_text.rfind(last_para)
                            if idx > 0:
                                scene_text = scene_text[:idx].rstrip()
                            break
                    _rep_check_buf = ""   # reset window

                # Cancel mid-scene support
                if job._cancel_flag:
                    break

            # ── Save scene to project ─────────────────────────────────────
            if scene_text.strip():
                sid = _save_scene_to_project(
                    project_id=job.project_id,
                    act=s["act"],
                    chapter_num=s["chapter"],
                    scene_num=s["scene"],
                    chapter_title=s["chapter_title"],
                    scene_title=s["beat"][:80],
                    content=scene_text.strip(),
                )

                # Index in RAG for future scenes
                try:
                    # Clean markdown and grab first ~200 chars for semantic search
                    import re
                    clean_text = re.sub(r'#.*?\n', '', scene_text)
                    clean_text = re.sub(r'\*+', '', clean_text)
                    summary = clean_text.strip()[:200]
                    if not summary:
                        summary = s["beat"]
                        
                    await upsert_scene(
                        project_id=job.project_id,
                        scene_id=sid,
                        summary=summary,
                        characters=[],
                    )
                except Exception:
                    pass

                wc = len(scene_text.split())
                job.done_scenes += 1
                job.total_words += wc
                job.scenes_written.append({
                    "id":      sid,
                    "act":     s["act"],
                    "chapter": s["chapter"],
                    "scene":   s["scene"],
                    "title":   s["beat"][:60],
                    "words":   wc,
                })

                prev_ending = " ".join(scene_text.split()[-150:])

                _emit(job, {
                    "type":        "scene_done",
                    "scene_id":    sid,
                    "words":       wc,
                    "total_words": job.total_words,
                    "done":        job.done_scenes,
                    "total":       job.total_scenes,
                })

        # ── 3. Done ───────────────────────────────────────────────────────
        job.status       = "done"
        job.completed_at = datetime.now(timezone.utc).isoformat()
        _emit(job, {
            "type":        "book_done",
            "total_words": job.total_words,
            "total_scenes": job.done_scenes,
            "title":       job.outline.get("title", "Untitled"),
        })

    except Exception as exc:
        job.status = "error"
        job.error  = str(exc)
        _emit(job, {"type": "error", "message": str(exc)})


# ---------------------------------------------------------------------------
# SSE streaming helper
# ---------------------------------------------------------------------------


async def _job_stream(job: BookJob) -> AsyncIterator[str]:
    """
    Yield SSE-formatted events for the given job.

    Bootstraps with current status, then subscribes to live events.
    """
    q: asyncio.Queue = asyncio.Queue(maxsize=500)
    job._subscribers.append(q)

    # Send current state immediately
    yield f"data: {json.dumps({'type': 'snapshot', 'job': job.to_dict()})}\n\n"

    try:
        while job.status in ("pending", "running", "paused"):
            try:
                event = await asyncio.wait_for(q.get(), timeout=5.0)
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("type") in ("book_done", "cancelled", "error"):
                    break
            except asyncio.TimeoutError:
                # Heartbeat to keep connection alive
                yield "data: {\"type\":\"ping\"}\n\n"

        # Drain remaining buffered events
        while not q.empty():
            event = q.get_nowait()
            yield f"data: {json.dumps(event)}\n\n"

    finally:
        job._subscribers.discard(q) if hasattr(job._subscribers, 'discard') else None
        try:
            job._subscribers.remove(q)
        except ValueError:
            pass


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------


@router.post("/start", status_code=201)
async def start_book(req: BookConfig) -> dict:
    """
    Start a full-book generation job.

    Creates a background asyncio task that generates an outline then
    writes every scene, saving each one to the target project.

    Returns:
        Dict with job_id for status polling / SSE streaming.
    """
    # Verify project exists
    proj_dir = _project_dir(req.project_id)
    if not proj_dir.exists():
        raise HTTPException(404, f"Project '{req.project_id}' not found")

    job_id = str(uuid.uuid4())[:12]
    job    = BookJob(
        job_id=job_id,
        project_id=req.project_id,
        config=req.model_dump(),
    )
    _JOBS[job_id] = job

    asyncio.ensure_future(_run_book_job(job_id))

    return {"job_id": job_id, "project_id": req.project_id, "status": "pending"}


@router.get("/jobs/{project_id}")
async def list_jobs(project_id: str) -> list[dict]:
    """List all book generation jobs for a project."""
    return [
        j.to_dict()
        for j in _JOBS.values()
        if j.project_id == project_id
    ]


@router.get("/{job_id}")
async def get_job(job_id: str) -> dict:
    """Get current job status and progress."""
    job = _JOBS.get(job_id)
    if not job:
        raise HTTPException(404, f"Job '{job_id}' not found")
    return job.to_dict()


@router.get("/{job_id}/stream")
async def stream_job(job_id: str) -> StreamingResponse:
    """
    SSE stream of live progress events for a job.

    Events types: snapshot, outline_ready, total_scenes, scene_start,
                  token, scene_done, book_done, cancelled, error, ping.
    """
    job = _JOBS.get(job_id)
    if not job:
        raise HTTPException(404, f"Job '{job_id}' not found")
    return StreamingResponse(
        _job_stream(job),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/{job_id}/pause")
async def pause_job(job_id: str) -> dict:
    """Pause the job after the current scene finishes."""
    job = _JOBS.get(job_id)
    if not job:
        raise HTTPException(404, f"Job '{job_id}' not found")
    if job.status == "running":
        job._pause_event.clear()
        job.status = "paused"
        _emit(job, {"type": "paused"})
    return {"status": job.status}


@router.post("/{job_id}/resume")
async def resume_job(job_id: str) -> dict:
    """Resume a paused job."""
    job = _JOBS.get(job_id)
    if not job:
        raise HTTPException(404, f"Job '{job_id}' not found")
    if job.status == "paused":
        job.status = "running"
        job._pause_event.set()
        _emit(job, {"type": "resumed"})
    return {"status": job.status}


@router.post("/{job_id}/cancel")
async def cancel_job(job_id: str) -> dict:
    """Cancel a running or paused job."""
    job = _JOBS.get(job_id)
    if not job:
        raise HTTPException(404, f"Job '{job_id}' not found")
    job._cancel_flag = True
    job._pause_event.set()  # Unblock if paused
    return {"status": "cancelling"}
