"""
Quill Phase 2 — Structured fact extraction from scene text.

Uses the llama-server (BitNet/Qwen) to extract:
  - Characters present and any updates to their facts
  - Scene events (summary, who, where)
  - World facts (rules, places, objects, lore)
  - A compressed 2-sentence scene summary for RAG indexing

All results are stored in the project's state/ directory as JSON.
ChromaDB embeddings are managed by rag.py.
"""

import json
import re
import asyncio
from pathlib import Path
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

from . import config
from .projects import _project_dir, _load_json, _save_json

router = APIRouter(prefix="/api/extract", tags=["extract"])

# ---------------------------------------------------------------------------
# Extraction prompt
# ---------------------------------------------------------------------------

EXTRACT_SYSTEM = """\
You are a story fact extractor. Read the scene and extract structured information.
Output ONLY valid JSON. No explanation, no markdown, no preamble.
Be specific and concise. If you are unsure, omit the field."""

EXTRACT_USER_TMPL = """\
Extract facts from this scene. Output exactly this JSON structure:

{{
  "characters_present": ["name1", "name2"],
  "character_updates": [
    {{"name": "Character Name", "field": "appearance|location|relationship|trait|arc_state", "value": "new value"}}
  ],
  "events": [
    {{"summary": "one sentence", "who": ["name1"], "location": "place name"}}
  ],
  "world_facts": [
    {{"fact": "concise fact", "category": "rule|place|object|lore|timeline"}}
  ],
  "scene_summary": "2-3 sentence summary of what happened and why it matters"
}}

SCENE TEXT:
{scene_text}"""

COMPRESS_TMPL = """\
Compress this scene summary to one precise sentence (max 25 words):
{summary}"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _state_dir(project_id: str) -> Path:
    """Return the state/ directory for a project."""
    return _project_dir(project_id) / "state"


def _chroma_dir(project_id: str) -> Path:
    """Return ChromaDB directory for a project."""
    return _state_dir(project_id) / "chroma"


async def _call_llm(prompt: str, system: str, max_tokens: int = 400) -> str:
    """
    Call llama-server synchronously and return the full response text.

    Args:
        prompt: User message content.
        system: System message.
        max_tokens: Maximum tokens to generate.

    Returns:
        Decoded response string.
    """
    payload = {
        "model": "quill",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.1,   # near-deterministic for extraction
        "stream": False,
    }
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            f"{config.LLAMA_SERVER_URL}/v1/chat/completions",
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]


def _extract_json(text: str) -> Any:
    """
    Robustly extract JSON from LLM output.

    The model may wrap JSON in markdown fences or add preamble text.
    This strips common noise before parsing.

    Args:
        text: Raw LLM output string.

    Returns:
        Parsed JSON object (dict or list).

    Raises:
        ValueError: If no valid JSON found.
    """
    # Strip markdown code fences
    text = re.sub(r"```(?:json)?", "", text).strip()

    # Try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Find first {...} or [...] block
    for pattern in (r"\{.*\}", r"\[.*\]"):
        match = re.search(pattern, text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                continue

    raise ValueError(f"No valid JSON found in: {text[:200]}")


def _merge_character(existing: dict, updates: list[dict]) -> dict:
    """
    Apply a list of field updates to an existing character dict.

    Args:
        existing: Current character data.
        updates: List of {field, value} dicts.

    Returns:
        Updated character dict.
    """
    for upd in updates:
        field = upd.get("field", "").strip()
        value = upd.get("value", "").strip()
        if field and value:
            existing[field] = value
    existing["updated"] = datetime.now(timezone.utc).isoformat()
    return existing


# ---------------------------------------------------------------------------
# Core extraction logic (runs in background after scene save)
# ---------------------------------------------------------------------------


async def extract_scene_facts(project_id: str, scene_id: str, text: str) -> dict:
    """
    Extract structured facts from scene text and persist them.

    Steps:
      1. Call LLM to extract JSON facts
      2. Merge character updates into characters.json
      3. Append world facts to world_rules.json
      4. Compress summary for RAG embedding
      5. Register in scene_meta.json

    Args:
        project_id: Project identifier.
        scene_id: Scene identifier.
        text: Raw scene text.

    Returns:
        Extracted facts dict.
    """
    if len(text.strip()) < 50:
        return {}   # not enough content to extract

    state_dir = _state_dir(project_id)
    state_dir.mkdir(parents=True, exist_ok=True)

    # ── Step 1: Extract facts ─────────────────────────────────────────────
    prompt = EXTRACT_USER_TMPL.format(scene_text=text[:3000])   # cap input
    try:
        raw = await _call_llm(prompt, EXTRACT_SYSTEM, max_tokens=450)
        facts = _extract_json(raw)
    except Exception as exc:
        print(f"[Quill extract] LLM parse error for {scene_id}: {exc}")
        return {}

    # ── Step 2: Update character DB ───────────────────────────────────────
    chars_path = state_dir / "characters.json"
    chars_db: dict = _load_json(chars_path) or {}

    for name in facts.get("characters_present", []):
        if name not in chars_db:
            chars_db[name] = {
                "name":       name,
                "appearance": "",
                "location":   "",
                "trait":      "",
                "arc_state":  "",
                "relationship": "",
                "first_seen": scene_id,
                "updated":    datetime.now(timezone.utc).isoformat(),
            }

    for upd in facts.get("character_updates", []):
        name = upd.get("name", "").strip()
        if name and name in chars_db:
            chars_db[name] = _merge_character(chars_db[name], [upd])
        elif name:
            chars_db[name] = _merge_character(
                {"name": name, "first_seen": scene_id}, [upd]
            )

    _save_json(chars_path, chars_db)

    # ── Step 3: Append world facts ────────────────────────────────────────
    rules_path = state_dir / "world_rules.json"
    rules: list = _load_json(rules_path) or []

    for wf in facts.get("world_facts", []):
        fact  = wf.get("fact",     "").strip()
        cat   = wf.get("category", "lore")
        if fact and not any(r["fact"] == fact for r in rules):
            rules.append({"fact": fact, "category": cat, "source_scene": scene_id})

    _save_json(rules_path, rules)

    # ── Step 4: Compress summary for RAG ─────────────────────────────────
    raw_summary = facts.get("scene_summary", "")
    compressed  = raw_summary
    if raw_summary:
        try:
            compressed = await _call_llm(
                COMPRESS_TMPL.format(summary=raw_summary),
                "Output ONLY the compressed sentence. No explanation.",
                max_tokens=40,
            )
            compressed = compressed.strip().strip('"')
        except Exception:
            compressed = raw_summary[:200]

    # ── Step 4.5: Auto-label (POV, pacing, tension) ────────────────────────
    pov     = ""
    pacing  = "medium"
    tension = 3
    if raw_summary:
        label_prompt = (
            f'Label this scene. Output ONLY JSON: {{"pov": "character name or unknown", '
            f'"pacing": "fast|medium|slow", "tension": 1}}.\n\n'
            f"Scene summary: {raw_summary[:400]}"
        )
        try:
            label_raw = await _call_llm(
                label_prompt,
                "Output valid JSON only. No explanation.",
                max_tokens=50,
            )
            labels = _extract_json(label_raw)
            if isinstance(labels, dict):
                pov     = str(labels.get("pov", ""))[:40]
                pacing  = str(labels.get("pacing", "medium"))
                tension = int(labels.get("tension", 3))
                tension = max(1, min(5, tension))
        except Exception as exc:
            print(f"[Quill extract] Label error for {scene_id}: {exc}")

    # ── Step 5: Update scene meta ───────────────────────────────────────────────────────
    meta_path = state_dir / "scene_meta.json"
    meta: dict = _load_json(meta_path) or {}
    meta[scene_id] = {
        "summary":            raw_summary,
        "compressed_summary": compressed,
        "characters_present": facts.get("characters_present", []),
        "events":             facts.get("events", []),
        "pov":                pov,
        "pacing":             pacing,
        "tension":            tension,
        "extracted_at":       datetime.now(timezone.utc).isoformat(),
    }
    _save_json(meta_path, meta)

    # ── Step 6: Index in ChromaDB ─────────────────────────────────────────
    if compressed:
        try:
            from .rag import upsert_scene
            await upsert_scene(
                project_id=project_id,
                scene_id=scene_id,
                summary=compressed,
                characters=facts.get("characters_present", []),
            )
        except Exception as exc:
            print(f"[Quill RAG] Index error for {scene_id}: {exc}")

    # ── Step 7: Check if audit is due (every 5 extractions) ──────────────
    total_extracted = len(meta)
    if total_extracted > 0 and total_extracted % 5 == 0:
        try:
            from .audit import run_consistency_audit
            await run_consistency_audit(project_id)
        except Exception as exc:
            print(f"[Quill audit] Auto-audit error: {exc}")

    return facts


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------


class ExtractRequest(BaseModel):
    """Request to extract facts from a scene."""

    scene_id: str
    text:     str


@router.post("/scene_facts")
async def api_extract_scene_facts(
    project_id: str,
    req: ExtractRequest,
    background_tasks: BackgroundTasks,
) -> dict:
    """
    Trigger background fact extraction for a scene.

    Returns immediately with a job_id. Extraction runs asynchronously
    (typically 15–30s). Poll /api/extract/status/{scene_id} for results.
    """
    background_tasks.add_task(
        extract_scene_facts, project_id, req.scene_id, req.text
    )
    return {"status": "queued", "scene_id": req.scene_id}


@router.get("/status/{project_id}/{scene_id}")
async def extraction_status(project_id: str, scene_id: str) -> dict:
    """Check whether a scene has been extracted."""
    state_dir = _state_dir(project_id)
    meta_path  = state_dir / "scene_meta.json"
    meta       = _load_json(meta_path) or {}
    if scene_id in meta:
        return {"status": "done", "data": meta[scene_id]}
    return {"status": "pending"}
