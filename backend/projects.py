"""
Quill — project and scene CRUD.

All data stored as plain files:
  ~/.quill/projects/<project_id>/
    project.json       — metadata
    structure.json     — act/chapter/scene tree
    scenes/            — one .md file per scene
    snapshots/         — auto-snapshots before edit sessions
"""

import json
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import config

router = APIRouter(prefix="/api", tags=["projects"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _project_dir(project_id: str) -> Path:
    """Return project directory, raise 404 if not found."""
    path = config.PROJECTS_DIR / project_id
    if not path.exists():
        raise HTTPException(404, f"Project '{project_id}' not found")
    return path


def _load_json(path: Path) -> Any:
    """Load JSON file, return empty dict if missing."""
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _save_json(path: Path, data: Any) -> None:
    """Write JSON atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _snapshot(scene_path: Path) -> None:
    """Save a timestamped snapshot of a scene before overwriting."""
    if not scene_path.exists():
        return
    snap_dir = scene_path.parent.parent / "snapshots" / scene_path.stem
    snap_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    shutil.copy2(scene_path, snap_dir / f"{ts}.md")


def _word_count(text: str) -> int:
    """Count words in text."""
    return len(text.split()) if text.strip() else 0


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class ProjectCreate(BaseModel):
    """Create a new project."""

    title: str = Field(..., min_length=1, max_length=200)
    genre: str = Field("fiction", max_length=100)
    word_count_goal: int = Field(80_000, ge=1_000)


class SceneCreate(BaseModel):
    """Create a new scene."""

    act: int = Field(1, ge=1)
    chapter: int = Field(1, ge=1)
    title: str = Field("New Scene", max_length=200)


class SceneUpdate(BaseModel):
    """Update scene content."""

    content: str
    title: str | None = None


# ---------------------------------------------------------------------------
# Project endpoints
# ---------------------------------------------------------------------------


@router.get("/projects")
async def list_projects() -> list[dict]:
    """List all projects with summary metadata."""
    projects = []
    for d in sorted(config.PROJECTS_DIR.iterdir()):
        meta_path = d / "project.json"
        if d.is_dir() and meta_path.exists():
            meta = _load_json(meta_path)
            projects.append(meta)
    return projects


@router.post("/projects", status_code=201)
async def create_project(req: ProjectCreate) -> dict:
    """Create a new project with an empty structure."""
    project_id = str(uuid.uuid4())[:8]
    project_dir = config.PROJECTS_DIR / project_id

    meta = {
        "id": project_id,
        "title": req.title,
        "genre": req.genre,
        "word_count_goal": req.word_count_goal,
        "word_count": 0,
        "created": datetime.now(timezone.utc).isoformat(),
        "updated": datetime.now(timezone.utc).isoformat(),
    }

    structure = {
        "acts": [
            {
                "id": 1,
                "title": "Act 1",
                "chapters": [
                    {
                        "id": 1,
                        "title": "Chapter 1",
                        "scenes": [],
                    }
                ],
            }
        ]
    }

    _save_json(project_dir / "project.json", meta)
    _save_json(project_dir / "structure.json", structure)
    (project_dir / "scenes").mkdir(parents=True, exist_ok=True)
    (project_dir / "state").mkdir(parents=True, exist_ok=True)
    (project_dir / "snapshots").mkdir(parents=True, exist_ok=True)

    # Empty style guide
    (project_dir / "style_guide.md").write_text(
        "# Style Guide\n\nDescribe your writing voice, tone, and any rules here.\n",
        encoding="utf-8",
    )

    return meta


@router.get("/projects/{project_id}")
async def get_project(project_id: str) -> dict:
    """Get project metadata and structure."""
    proj_dir = _project_dir(project_id)
    meta = _load_json(proj_dir / "project.json")
    structure = _load_json(proj_dir / "structure.json")
    return {**meta, "structure": structure}


@router.delete("/projects/{project_id}", status_code=204)
async def delete_project(project_id: str) -> None:
    """Delete a project permanently."""
    proj_dir = _project_dir(project_id)
    shutil.rmtree(proj_dir)


# ---------------------------------------------------------------------------
# Scene endpoints
# ---------------------------------------------------------------------------


def _scene_id(act: int, chapter: int, scene_num: int) -> str:
    """Generate scene file stem, e.g. a1_c02_s003."""
    return f"a{act}_c{chapter:02d}_s{scene_num:03d}"


@router.get("/projects/{project_id}/scenes")
async def list_scenes(project_id: str) -> list[dict]:
    """List all scenes with metadata (no content)."""
    proj_dir = _project_dir(project_id)
    structure = _load_json(proj_dir / "structure.json")
    scenes = []
    for act in structure.get("acts", []):
        for chap in act.get("chapters", []):
            for scene in chap.get("scenes", []):
                scenes.append(
                    {
                        **scene,
                        "act": act["id"],
                        "act_title": act["title"],
                        "chapter": chap["id"],
                        "chapter_title": chap["title"],
                    }
                )
    return scenes


@router.post("/projects/{project_id}/scenes", status_code=201)
async def create_scene(project_id: str, req: SceneCreate) -> dict:
    """Create a new empty scene and register it in structure.json."""
    proj_dir = _project_dir(project_id)
    structure = _load_json(proj_dir / "structure.json")

    # Find or create the target act/chapter
    act_obj = next((a for a in structure["acts"] if a["id"] == req.act), None)
    if not act_obj:
        act_obj = {"id": req.act, "title": f"Act {req.act}", "chapters": []}
        structure["acts"].append(act_obj)

    chap_obj = next((c for c in act_obj["chapters"] if c["id"] == req.chapter), None)
    if not chap_obj:
        chap_obj = {"id": req.chapter, "title": f"Chapter {req.chapter}", "scenes": []}
        act_obj["chapters"].append(chap_obj)

    scene_num = len(chap_obj["scenes"]) + 1
    scene_id = _scene_id(req.act, req.chapter, scene_num)

    scene_meta = {
        "id": scene_id,
        "title": req.title,
        "word_count": 0,
        "status": "empty",
        "pov": "",
        "created": datetime.now(timezone.utc).isoformat(),
        "updated": datetime.now(timezone.utc).isoformat(),
    }

    chap_obj["scenes"].append(scene_meta)
    _save_json(proj_dir / "structure.json", structure)

    # Create empty scene file
    scene_path = proj_dir / "scenes" / f"{scene_id}.md"
    scene_path.write_text("", encoding="utf-8")

    return scene_meta


@router.get("/projects/{project_id}/scenes/{scene_id}")
async def get_scene(project_id: str, scene_id: str) -> dict:
    """Get scene content and metadata."""
    proj_dir = _project_dir(project_id)
    scene_path = proj_dir / "scenes" / f"{scene_id}.md"
    if not scene_path.exists():
        raise HTTPException(404, f"Scene '{scene_id}' not found")
    content = scene_path.read_text(encoding="utf-8")
    return {"id": scene_id, "content": content, "word_count": _word_count(content)}


@router.put("/projects/{project_id}/scenes/{scene_id}")
async def update_scene(project_id: str, scene_id: str, req: SceneUpdate) -> dict:
    """
    Save scene content.

    Auto-snapshots the previous version before overwriting.
    Updates word_count and updated timestamp in structure.json.
    """
    proj_dir = _project_dir(project_id)
    scene_path = proj_dir / "scenes" / f"{scene_id}.md"

    # Snapshot before overwriting (but not if empty)
    if scene_path.exists() and scene_path.stat().st_size > 0:
        _snapshot(scene_path)

    scene_path.write_text(req.content, encoding="utf-8")

    # Update metadata in structure.json
    structure = _load_json(proj_dir / "structure.json")
    wc = _word_count(req.content)
    now = datetime.now(timezone.utc).isoformat()

    for act in structure.get("acts", []):
        for chap in act.get("chapters", []):
            for scene in chap.get("scenes", []):
                if scene["id"] == scene_id:
                    scene["word_count"] = wc
                    scene["updated"] = now
                    if req.title:
                        scene["title"] = req.title
                    scene["status"] = "draft" if wc > 0 else "empty"
                    break

    _save_json(proj_dir / "structure.json", structure)

    # Update total word count in project.json
    meta = _load_json(proj_dir / "project.json")
    total = sum(
        _word_count((proj_dir / "scenes" / f"{s['id']}.md").read_text())
        for a in structure["acts"]
        for c in a["chapters"]
        for s in c["scenes"]
        if (proj_dir / "scenes" / f"{s['id']}.md").exists()
    )
    meta["word_count"] = total
    meta["updated"] = now
    _save_json(proj_dir / "project.json", meta)

    return {"id": scene_id, "word_count": wc, "status": structure}


@router.get("/projects/{project_id}/scenes/{scene_id}/snapshots")
async def list_snapshots(project_id: str, scene_id: str) -> list[str]:
    """List available snapshot timestamps for a scene."""
    proj_dir = _project_dir(project_id)
    snap_dir = proj_dir / "snapshots" / scene_id
    if not snap_dir.exists():
        return []
    return sorted(
        [f.stem for f in snap_dir.glob("*.md")], reverse=True
    )


@router.get("/projects/{project_id}/snapshots/{scene_id}/{timestamp}")
async def get_snapshot(project_id: str, scene_id: str, timestamp: str) -> dict:
    """Retrieve a specific snapshot."""
    proj_dir = _project_dir(project_id)
    snap_path = proj_dir / "snapshots" / scene_id / f"{timestamp}.md"
    if not snap_path.exists():
        raise HTTPException(404, "Snapshot not found")
    return {"timestamp": timestamp, "content": snap_path.read_text(encoding="utf-8")}


# ---------------------------------------------------------------------------
# Story Bible — Characters
# ---------------------------------------------------------------------------


@router.get("/projects/{project_id}/characters")
async def get_characters(project_id: str) -> dict:
    """Return full character database for a project."""
    proj_dir  = _project_dir(project_id)
    chars_path = proj_dir / "state" / "characters.json"
    return _load_json(chars_path) or {}


class CharacterUpdate(BaseModel):
    """Update a single character field."""

    field: str
    value: str


@router.patch("/projects/{project_id}/characters/{name}")
async def update_character(
    project_id: str, name: str, req: CharacterUpdate
) -> dict:
    """
    Update a single field for a character.

    Args:
        project_id: Project ID.
        name: Character name (URL-encoded if necessary).
        req: Field + value to update.

    Returns:
        Updated character dict.
    """
    proj_dir   = _project_dir(project_id)
    chars_path = proj_dir / "state" / "characters.json"
    chars_db: dict = _load_json(chars_path) or {}

    if name not in chars_db:
        chars_db[name] = {
            "name":       name,
            "first_seen": "manual",
            "updated":    datetime.now(timezone.utc).isoformat(),
        }

    chars_db[name][req.field]   = req.value
    chars_db[name]["updated"]   = datetime.now(timezone.utc).isoformat()
    _save_json(chars_path, chars_db)
    return chars_db[name]


# ---------------------------------------------------------------------------
# Story Bible — World Rules
# ---------------------------------------------------------------------------


@router.get("/projects/{project_id}/world_rules")
async def get_world_rules(project_id: str) -> list:
    """Return world rules list for a project."""
    proj_dir = _project_dir(project_id)
    rules_path = proj_dir / "state" / "world_rules.json"
    return _load_json(rules_path) or []


class WorldRuleCreate(BaseModel):
    """Add a new world rule."""

    fact:     str
    category: str = "lore"


@router.post("/projects/{project_id}/world_rules", status_code=201)
async def add_world_rule(project_id: str, req: WorldRuleCreate) -> dict:
    """Add a manual world rule."""
    proj_dir   = _project_dir(project_id)
    rules_path = proj_dir / "state" / "world_rules.json"
    rules: list = _load_json(rules_path) or []

    # Avoid duplicates
    if any(r["fact"] == req.fact for r in rules):
        return {"status": "exists"}

    rule = {
        "fact":         req.fact,
        "category":     req.category,
        "source_scene": "manual",
        "created":      datetime.now(timezone.utc).isoformat(),
    }
    rules.append(rule)
    _save_json(rules_path, rules)
    return rule


# ---------------------------------------------------------------------------
# Phase 3 — Scene metadata labels (POV, pacing, tension)
# ---------------------------------------------------------------------------


@router.get("/projects/{project_id}/scene_meta")
async def get_scene_meta(project_id: str) -> dict:
    """
    Return the full scene_meta index for a project.

    Keyed by scene_id. Each entry contains:
      pov, pacing, tension (1-5), compressed_summary, extracted_at.
    """
    proj_dir  = _project_dir(project_id)
    meta_path = proj_dir / "state" / "scene_meta.json"
    return _load_json(meta_path) or {}


# ---------------------------------------------------------------------------
# Phase 3 — Writing goal management
# ---------------------------------------------------------------------------


class GoalUpdate(BaseModel):
    """Update the word count goal for a project."""

    word_count_goal: int = Field(..., ge=1_000, le=2_000_000)


@router.patch("/projects/{project_id}/goal")
async def update_goal(project_id: str, req: GoalUpdate) -> dict:
    """
    Update the writer's word count goal.

    Returns the updated project metadata including new completion percentage.
    """
    proj_dir = _project_dir(project_id)
    meta     = _load_json(proj_dir / "project.json")
    meta["word_count_goal"] = req.word_count_goal
    meta["updated"]         = datetime.now(timezone.utc).isoformat()
    _save_json(proj_dir / "project.json", meta)
    pct = round((meta["word_count"] / req.word_count_goal) * 100, 1)
    return {**meta, "completion_pct": pct}
