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
    "You are an expert novelist creating a detailed book outline.\n"
    "Output ONLY valid Markdown format — absolutely no JSON, no backticks, no other text.\n\n"
    "Required format:\n"
    "# Act 1: Setup\n"
    "## Chapter 1\n"
    "- Scene beat description here.\n"
    "- Another scene beat.\n"
    "## Chapter 2\n"
    "- Scene beat description here.\n\n"
    "CRITICAL RULES FOR SCENE BEATS:\n"
    "1. Every scene beat MUST be a complete action-verb sentence describing a specific event.\n"
    "   GOOD: 'Jaxon discovers a hidden compartment in the journal containing a coded map fragment.'\n"
    "   BAD: 'Narrative continues' or 'Scene three' or vague descriptions.\n"
    "2. Each beat must describe what CHANGES: what the protagonist discovers, loses, decides, or confronts.\n"
    "3. Beats must form a causal chain: each scene's outcome sets up the next scene's conflict.\n"
    "4. No two consecutive beats may describe the same emotional state or location without change.\n"
)


async def _generate_outline(config: dict) -> dict:
    """
    Generate a structured book outline using hierarchical generation.

    Small local LLMs cannot maintain coherence across 40+ sequential beats
    in a single pass — they start looping after ~15. This function solves that
    by generating in three focused phases:

    Phase 1 — SPINE: Generate 9 major plot turns (3 per act). Short, high-level.
              This is the story's backbone and never changes.

    Phase 2 — CHAPTERS: Expand each chapter individually with the spine +
              ALL previously accepted beats as explicit anti-repetition context.
              Each call generates only 2–3 beats, well within the model's
              coherence window.

    Phase 3 — VALIDATE: Run _validate_and_repair_outline to catch any
              near-duplicates that slipped through.

    Args:
        config: BookConfig dict with genre, premise, protagonist, etc.

    Returns:
        Validated outline dict with unique, causally chained scene beats.
    """
    n_chaps  = config["num_chapters"]
    spc      = config["scenes_per_chapter"]
    n_acts   = 3
    per_act  = max(1, n_chaps // n_acts)
    protagonist = config.get("protagonist") or "The protagonist"
    premise     = config.get("premise", "")
    genre       = config.get("genre", "fiction")
    setting     = config.get("setting") or "not specified"
    antagonist  = config.get("antagonist") or "none"

    provider = get_active_provider()

    # ── Phase 1: SPINE ────────────────────────────────────────────────────────
    # Generate 9 major story turns — 3 per act — as single sentences.
    # These anchor every chapter beat that follows.
    spine_prompt = (
        f"Genre: {genre}\nPremise: {premise}\n"
        f"Protagonist: {protagonist}\nAntagonist: {antagonist}\n"
        f"Setting: {setting}\n\n"
        f"Write the STORY SPINE for this {genre} novel: exactly 9 major plot turns, "
        f"3 per act (Setup / Confrontation / Resolution).\n"
        "Each turn is ONE sentence describing a concrete event that changes the story.\n"
        "Format exactly as:\n"
        "ACT 1:\n1. [event]\n2. [event]\n3. [event]\n"
        "ACT 2:\n4. [event]\n5. [event]\n6. [event]\n"
        "ACT 3:\n7. [event]\n8. [event]\n9. [event]\n"
        "No filler. No vague descriptions. Each event must be different."
    )

    spine_text = ""
    async for token in provider.stream(
        messages=[
            {"role": "system", "content": "You are a story architect. Generate a tight story spine."},
            {"role": "user",   "content": spine_prompt},
        ],
        max_tokens=600,
        temperature=0.75,
        stop=[],
    ):
        spine_text += token

    print(f"[Quill] Story spine generated ({len(spine_text.split())} words).")

    # ── Phase 2: CHAPTER-BY-CHAPTER EXPANSION ─────────────────────────────────
    # Build the chapter list (act assignment, chapter index)
    chapters_plan: list[tuple[int, int, str]] = []  # (act_idx 0-2, chap_num, chap_title)
    act_names = ["Act 1: Setup", "Act 2: Confrontation", "Act 3: Resolution"]

    chap_num = 1
    for act_i in range(n_acts):
        count = per_act if act_i < n_acts - 1 else (n_chaps - per_act * (n_acts - 1))
        count = max(1, count)
        for _ in range(count):
            chapters_plan.append((act_i, chap_num, f"Chapter {chap_num}"))
            chap_num += 1

    # Build acts structure
    acts: list[dict] = [
        {"name": act_names[i], "chapters": []}
        for i in range(n_acts)
    ]

    all_accepted_beats: list[str] = []  # global anti-repetition context

    for act_i, chap_num, chap_title in chapters_plan:
        act_name = act_names[act_i]
        prior_context = (
            "\n".join(f"- {b}" for b in all_accepted_beats[-16:])
            if all_accepted_beats else "None yet."
        )

        chap_prompt = (
            f"STORY SPINE:\n{spine_text}\n\n"
            f"STORY SO FAR (beats already written — do NOT repeat any):\n"
            f"{prior_context}\n\n"
            f"Now write ONLY the beats for: {act_name} — {chap_title}\n"
            f"Write exactly {spc} scene beat(s). Each beat:\n"
            f"  - Is a specific action-verb sentence (protagonist does/finds/loses/decides something concrete)\n"
            f"  - Is completely different from every beat in 'STORY SO FAR'\n"
            f"  - Advances the story from the last beat listed above\n"
            f"Format: one beat per line, starting with '- '\n"
            f"No headers, no numbering, no explanations. Just {spc} bullet line(s)."
        )

        chap_text = ""
        async for token in provider.stream(
            messages=[
                {"role": "system",
                 "content": (
                     "You are a story editor writing the next chapter's beats. "
                     "Never reuse a location, action, or discovery already listed. "
                     "Each beat must move the story forward."
                 )},
                {"role": "user", "content": chap_prompt},
            ],
            max_tokens=max(150, spc * 80),
            temperature=0.78,
            stop=[],
        ):
            chap_text += token

        # Parse beats from this chapter's response
        beats: list[str] = []
        for line in chap_text.strip().split("\n"):
            line = line.strip().lstrip("-•*123456789. ").strip()
            if len(line) > 20:
                beats.append(line)
            if len(beats) >= spc:
                break

        # Pad if model returned too few
        while len(beats) < spc:
            beats.append(
                f"{protagonist} faces an unexpected consequence from the previous event "
                f"and must adapt their approach in {setting}."
            )

        # Register accepted beats globally
        all_accepted_beats.extend(beats)

        acts[act_i]["chapters"].append({
            "title":  chap_title,
            "scenes": beats,
        })

        print(f"[Quill] Outline: {act_name} / {chap_title} — {len(beats)} beats generated.")

    outline = {"title": premise[:50] + "...", "acts": acts}

    # ── Phase 3: VALIDATE ─────────────────────────────────────────────────────
    outline = await _validate_and_repair_outline(outline, config, provider)
    return outline



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
        # --- Defensive Chapter Padding ---
        # If the local LLM ignores length constraints, pad with meaningful structural beats
        target_chaps = config.get("num_chapters", 0)
        spc = config.get("scenes_per_chapter", 3)
        total_gen = sum(len(a.get("chapters", [])) for a in acts)

        # Build meaningful fallback beats based on act position
        act_beat_templates = [
            # Act 1 beats
            [
                "{prot} arrives at the location and notices something is wrong.",
                "{prot} discovers the first clue and realizes the stakes are higher than expected.",
                "{prot} makes a decision that commits them to the main conflict.",
            ],
            # Act 2 beats
            [
                "{prot} pursues a lead but faces their first major setback.",
                "{prot} uncovers a hidden truth that recontextualizes everything they knew.",
                "{prot} reaches a point of no return and must confront the antagonist.",
            ],
            # Act 3 beats
            [
                "{prot} faces the climactic confrontation with everything on the line.",
                "{prot} resolves the central conflict and pays the cost of the journey.",
                "{prot} reaches a new equilibrium, changed by what they experienced.",
            ],
        ]
        prot = config.get("protagonist", "The protagonist")

        if total_gen > 0 and total_gen < target_chaps:
            missing = target_chaps - total_gen
            last_act = acts[-1]
            act_idx = min(len(acts) - 1, 2)
            beats = act_beat_templates[act_idx]
            for i in range(missing):
                chap_n = total_gen + i + 1
                chap_beats = [
                    beats[s % len(beats)].format(prot=prot)
                    for s in range(spc)
                ]
                last_act["chapters"].append({
                    "title":  f"Chapter {chap_n}",
                    "scenes": chap_beats,
                })

        return {"title": config.get("premise", "Untitled")[:50] + "...", "acts": acts}

    # Fallback if markdown parsing completely failed
    print("[Quill] Outline Parse Error: Could not extract markdown outline structure.")
    return _synthesise_outline(config)


def _beats_jaccard(a: str, b: str) -> float:
    """
    Compute Jaccard word-overlap between two beat strings.

    Args:
        a: First beat string.
        b: Second beat string.

    Returns:
        Float similarity in [0, 1].
    """
    stop = {"the", "and", "a", "an", "to", "in", "of", "he", "she", "they",
            "his", "her", "its", "at", "by", "for", "with", "from", "into"}
    wa = {w.lower() for w in re.findall(r"\b[a-zA-Z]{3,}\b", a) if w.lower() not in stop}
    wb = {w.lower() for w in re.findall(r"\b[a-zA-Z]{3,}\b", b) if w.lower() not in stop}
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


async def _validate_and_repair_outline(outline: dict, config: dict, provider) -> dict:
    """
    Validate that every scene beat in the outline is unique and causally distinct.

    Scans all beats across all acts/chapters for:
      - Exact duplicates
      - Near-duplicates (Jaccard > 0.65)

    Duplicate beats are regenerated individually by calling the LLM with the
    full list of already-accepted beats as context, ensuring no repeats.

    Args:
        outline:  Parsed outline dict from _parse_outline.
        config:   BookConfig dict for protagonist/setting/premise context.
        provider: Active LLM provider.

    Returns:
        Repaired outline dict with all duplicate beats replaced.
    """
    protagonist = config.get("protagonist", "The protagonist")
    premise     = config.get("premise", "")[:300]
    genre       = config.get("genre", "fiction")

    # Collect all beats in order with their location
    all_beats: list[tuple[int, int, int, str]] = []  # (act_i, chap_i, scene_i, beat)
    for ai, act in enumerate(outline.get("acts", [])):
        for ci, chap in enumerate(act.get("chapters", [])):
            for si, beat in enumerate(chap.get("scenes", [])):
                all_beats.append((ai, ci, si, beat))

    accepted:  list[str] = []
    changes    = 0

    for ai, ci, si, beat in all_beats:
        # Check exact duplicate
        is_exact = beat in accepted
        # Check near-duplicate against last 10 accepted beats
        is_near  = any(
            _beats_jaccard(beat, prev) >= 0.65
            for prev in accepted[-10:]
        ) if not is_exact else False

        if is_exact or is_near:
            # Regenerate this beat via LLM
            chap_title = outline["acts"][ai]["chapters"][ci].get("title", "")
            act_name   = outline["acts"][ai].get("name", "")
            context    = "\n".join(f"- {b}" for b in accepted[-8:])

            prompt = (
                f"Genre: {genre}\nPremise: {premise}\nProtagonist: {protagonist}\n"
                f"Current act: {act_name}\nCurrent chapter: {chap_title}\n\n"
                f"The following scene beats have already been written:\n{context}\n\n"
                f"The next beat was a duplicate: '{beat}'\n"
                "Write ONE new scene beat that:\n"
                "1. Is completely different from all beats listed above\n"
                "2. Advances the story forward from the last beat\n"
                "3. Is a specific action-verb sentence describing what the protagonist discovers, decides, or confronts\n"
                "4. Does NOT repeat any location, event, or character action already used\n"
                "Return ONLY the single scene beat sentence. No labels, no numbering."
            )

            new_beat = ""
            async for token in provider.stream(
                messages=[
                    {"role": "system",
                     "content": "You are a story editor. Generate ONE specific, unique scene beat."},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=120,
                temperature=0.85,
                stop=["\n", ".", "!", "?"],
            ):
                new_beat += token

            # Clean up and ensure it ends with a period
            new_beat = new_beat.strip().rstrip(".!?") + "."
            new_beat = re.sub(r"^[-\d\.\*]+\s*", "", new_beat)  # strip any numbering

            # Apply to outline
            outline["acts"][ai]["chapters"][ci]["scenes"][si] = new_beat
            accepted.append(new_beat)
            changes += 1
            print(f"[Quill] Outline repair: replaced duplicate beat in {act_name}/{chap_title} "
                  f"(sim={'exact' if is_exact else 'near'})")
        else:
            accepted.append(beat)

    if changes:
        print(f"[Quill] Outline validated: {changes} duplicate beat(s) repaired.")
    else:
        print("[Quill] Outline validated: all beats are unique. ✓")

    return outline



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
    "No chapter headers, scene numbers, or meta-commentary — just continuous prose.\n"
    "Target length: ~{words} words.\n\n"
    "STRICT RULES (violation breaks the story):\n"
    "1. Every sentence must advance the scene. Never restate what a prior sentence already said.\n"
    "2. Never write 'Approach', 'Option', 'Version', 'Method', or any numbered label.\n"
    "3. Never repeat a character's name more than twice per paragraph.\n"
    "4. The scene must END in a different emotional or physical state than it STARTS.\n"
    "5. No filler phrases: 'couldn't help but feel', 'heart pounding', 'mind racing' — find fresh language.\n\n"
    "STORY CONTEXT:\n"
    "Title: {title}\n"
    "Premise: {premise}\n"
    "{char_block}"
    "Setting: {setting}"
)

# Label patterns to strip from Phase 1 output before feeding to Phase 2
_OPEN_LABEL_RE = re.compile(
    r"^(?:Option|Event|Idea|Plot\s+Event|Possibility|Path|Choice|Alternative)\s*[\d#]+[:\.]?",
    re.IGNORECASE | re.MULTILINE,
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
    Stream prose tokens for a single scene using an improved two-phase workflow.

    Phase 1 (OPEN): Brainstorm 3 concrete PLOT EVENTS that could happen in this
    scene — what the characters do, discover, or lose. No style labels.
    Phase 2 (CLOSE): Pick the most dramatically compelling event and write the
    scene as continuous prose with strict anti-repetition rules enforced.

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
        genre      = config.get("genre", "fiction"),
        pov        = config.get("pov", "third person limited"),
        words      = words_per_scene,
        title      = book_title or "Untitled",
        premise    = config.get("premise", "")[:400],
        char_block = char_block,
        setting    = config.get("setting", "not specified"),
    )

    context_parts = []
    if rag_context:
        context_parts.append(f"STORY CONTEXT:\n{rag_context}")
    if prev_ending:
        context_parts.append(f"PREVIOUS SCENE ENDED:\n{prev_ending}")
    ctx = "\n\n".join(context_parts)

    provider = get_active_provider()

    # ── Phase 1: OPEN ── brainstorm 3 concrete PLOT EVENTS, no style labels ──
    open_msg = (
        f"{ctx}\n\n" if ctx else ""
    ) + (
        f"Chapter: {chapter_title}\n"
        f"Scene beat: {scene_beat}\n\n"
        "Brainstorm exactly 3 SPECIFIC PLOT EVENTS that could happen in this scene.\n"
        "A plot event is something concrete: a character discovers X, loses Y, confronts Z, "
        "makes a decision, finds a clue, or changes their situation in a measurable way.\n"
        "Do NOT describe style or tone. Do NOT write prose yet.\n"
        "Format — just three short bullet points, each starting with an action verb:\n"
        "• [event one]\n"
        "• [event two]\n"
        "• [event three]"
    )

    open_sys = (
        "You are an expert story planner. Generate specific, concrete plot events. "
        "Never use labels like 'Approach', 'Option', or 'Method'. "
        "Each bullet must describe what HAPPENS, not how it is written."
    )
    open_resp = ""
    async for token in provider.stream(
        messages=[{"role": "system", "content": open_sys},
                  {"role": "user",   "content": open_msg}],
        max_tokens=400,
        temperature=0.75,
        stop=[],
    ):
        open_resp += token

    # Strip any stray numbering/labels that leaked through
    clean_events = _OPEN_LABEL_RE.sub("", open_resp).strip()

    # ── Phase 2: CLOSE ── pick best event and write the scene ────────────────
    close_msg = (
        f"{ctx}\n\n" if ctx else ""
    ) + (
        f"Chapter: {chapter_title}\n"
        f"Scene beat: {scene_beat}\n\n"
        f"Three possible plot events for this scene:\n{clean_events}\n\n"
        "Select the plot event with the strongest dramatic impact. "
        "Do NOT mention which event you chose. "
        "Write the complete scene as continuous prose — no headers, no labels, no meta-commentary. "
        "The scene must end with the protagonist in a clearly different situation than at the start. "
        "Every sentence must advance the story forward. "
        "Write the scene now:"
    )

    max_tokens = min(int(words_per_scene * 1.5), cfg.SELF_WRITE_MAX_TOKENS)

    async for token in provider.stream(
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": close_msg},
        ],
        max_tokens=max_tokens,
        temperature=0.70,
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
