"""
Quill Phase 2 — Consistency auditor.

Runs every 5 scene extractions (automatic) or on-demand via the API.
Compares recent scene facts against the established character database
and surfaces contradictions as actionable warning cards.

Contradiction cards are stored in state/contradictions.json and
surfaced to the frontend via the /api/audit/contradictions endpoint.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import APIRouter
from pydantic import BaseModel

from . import config
from .extract import _call_llm, _extract_json
from .projects import _project_dir, _load_json, _save_json

router = APIRouter(prefix="/api/audit", tags=["audit"])

# ---------------------------------------------------------------------------
# Audit prompt
# ---------------------------------------------------------------------------

AUDIT_SYSTEM = """\
You are a story consistency checker. Your job is to find factual contradictions
between established character/world facts and recent scene text.
Only report genuine contradictions with direct evidence from both sources.
Output ONLY a JSON array. If no contradictions found, output [].
Do not invent contradictions. Do not paraphrase — quote directly."""

AUDIT_USER_TMPL = """\
Find contradictions between the established facts and the recent scenes.

ESTABLISHED CHARACTER FACTS:
{characters}

ESTABLISHED WORLD FACTS:
{world_rules}

RECENT SCENE SUMMARIES (last 5):
{recent_summaries}

Output a JSON array of contradictions:
[
  {{
    "id": "unique short id",
    "field": "which character/world field contradicts",
    "established": "what was established (with scene reference)",
    "contradicting": "what the recent scene says",
    "scene_id": "the scene_id where the contradiction appears",
    "severity": "high|medium|low"
  }}
]

Output [] if there are no genuine contradictions."""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _contradictions_path(project_id: str) -> Path:
    """Return path to contradictions store."""
    return _project_dir(project_id) / "state" / "contradictions.json"


# ---------------------------------------------------------------------------
# Core audit logic
# ---------------------------------------------------------------------------


async def run_consistency_audit(project_id: str) -> list[dict]:
    """
    Run a full consistency audit for a project.

    Compares the character DB and world rules against the last 5 scene
    summaries extracted. Persists results to contradictions.json.

    Args:
        project_id: Project identifier.

    Returns:
        List of contradiction dicts (may be empty).
    """
    state_dir = _project_dir(project_id) / "state"

    chars_db:    dict  = _load_json(state_dir / "characters.json") or {}
    world_rules: list  = _load_json(state_dir / "world_rules.json") or []
    scene_meta:  dict  = _load_json(state_dir / "scene_meta.json") or {}

    if not chars_db and not world_rules:
        return []

    # Get last 5 extracted scene summaries
    recent = sorted(
        scene_meta.items(),
        key=lambda x: x[1].get("extracted_at", ""),
        reverse=True,
    )[:5]

    if not recent:
        return []

    recent_text = "\n".join(
        f"[{sid}]: {data.get('scene_summary', data.get('compressed_summary', ''))}"
        for sid, data in recent
    )

    # Build compact character snapshot (field: value, ignore empty)
    char_lines = []
    for name, c in chars_db.items():
        fields = {k: v for k, v in c.items()
                  if v and k not in ("name", "first_seen", "updated")}
        if fields:
            char_lines.append(
                f"{name}: " + "; ".join(f"{k}={v}" for k, v in fields.items())
            )

    rules_text = "\n".join(f"• {r['fact']}" for r in world_rules[:10])

    prompt = AUDIT_USER_TMPL.format(
        characters="\n".join(char_lines) or "None established yet.",
        world_rules=rules_text or "None established yet.",
        recent_summaries=recent_text,
    )

    try:
        raw = await _call_llm(prompt, AUDIT_SYSTEM, max_tokens=500)
        contradictions = _extract_json(raw)
        if not isinstance(contradictions, list):
            contradictions = []
    except Exception as exc:
        print(f"[Quill audit] Parse error: {exc}")
        contradictions = []

    # Load existing, merge (avoid duplicates by id)
    existing: list = _load_json(_contradictions_path(project_id)) or []
    existing_ids = {c.get("id") for c in existing}

    now = datetime.now(timezone.utc).isoformat()
    new_items = []
    for item in contradictions:
        if not item.get("id"):
            item["id"] = f"c{now[:10]}-{len(existing_ids)}"
        if item["id"] not in existing_ids:
            item["status"]     = "open"
            item["created_at"] = now
            new_items.append(item)
            existing_ids.add(item["id"])

    merged = existing + new_items
    _save_json(_contradictions_path(project_id), merged)

    return new_items


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------


@router.post("/run/{project_id}")
async def run_audit(project_id: str) -> dict:
    """
    Run the consistency auditor immediately for a project.

    Returns the list of newly found contradictions.
    """
    new_items = await run_consistency_audit(project_id)
    return {"new_contradictions": len(new_items), "items": new_items}


@router.get("/contradictions/{project_id}")
async def get_contradictions(project_id: str) -> list[dict]:
    """Return all open contradiction cards for a project."""
    all_items: list = _load_json(_contradictions_path(project_id)) or []
    return [c for c in all_items if c.get("status") == "open"]


class ContradictionAction(BaseModel):
    """Action to take on a contradiction card."""

    action: str   # "dismiss" | "fix_noted" | "update_card"


@router.patch("/contradictions/{project_id}/{contradiction_id}")
async def resolve_contradiction(
    project_id: str,
    contradiction_id: str,
    body: ContradictionAction,
) -> dict:
    """
    Resolve a contradiction card.

    Actions:
      dismiss     — mark as ignored
      fix_noted   — mark as known, writer will fix the scene
      update_card — mark as resolved (writer updated the character card)
    """
    all_items: list = _load_json(_contradictions_path(project_id)) or []
    for item in all_items:
        if item.get("id") == contradiction_id:
            item["status"]     = "resolved"
            item["resolution"] = body.action
            item["resolved_at"] = datetime.now(timezone.utc).isoformat()
            break

    _save_json(_contradictions_path(project_id), all_items)
    return {"status": "resolved", "action": body.action}
