"""
Quill — Automated Story Review & Fix Engine.

Scans every scene in a project for common AI generation artifacts:
  - Approach labels bleeding into prose
  - Empty / stub scenes
  - Character name drift (multiple spellings of the same character)
  - Repeated paragraphs / copy-paste loops
  - Setting contradictions (LLM-assisted)

Each issue carries a fix_type so the frontend can auto-apply fixes.
"""

import json
import re
import shutil
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import config
from .providers import get_active_provider
from .bookwriter import _write_scene as _bw_write_scene

router = APIRouter(prefix="/api", tags=["review"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _project_dir(project_id: str) -> Path:
    """Return project root, raising 404 if missing."""
    p = config.PROJECTS_DIR / project_id
    if not p.exists():
        raise HTTPException(404, f"Project '{project_id}' not found")
    return p


def _load_scenes(project_id: str) -> list[dict]:
    """
    Load all scenes from a project.

    Returns:
        List of dicts with keys: id, title, path, content, word_count.
    """
    proj = _project_dir(project_id)
    scene_dir = proj / "scenes"
    struct_path = proj / "structure.json"

    if not scene_dir.exists():
        return []

    structure = json.loads(struct_path.read_text()) if struct_path.exists() else {"acts": []}
    scene_meta: dict[str, str] = {}  # id -> title

    for act in structure.get("acts", []):
        for chap in act.get("chapters", []):
            for s in chap.get("scenes", []):
                scene_meta[s["id"]] = s.get("title", s["id"])

    scenes = []
    for md in sorted(scene_dir.glob("*.md")):
        sid = md.stem
        content = md.read_text(encoding="utf-8")
        scenes.append({
            "id":         sid,
            "title":      scene_meta.get(sid, sid),
            "path":       str(md),
            "content":    content,
            "word_count": len(content.split()),
        })
    return scenes


# ---------------------------------------------------------------------------
# Issue detectors
# ---------------------------------------------------------------------------

_APPROACH_RE = re.compile(
    r"^\s*\*{1,2}\s*(Approach|Narrative Approach|Narrative)[^\n]*\*{0,2}\s*$",
    re.IGNORECASE | re.MULTILINE,
)

_LABEL_RE = re.compile(
    r"^\s*\*{1,2}(Approach|Narrative|Concept|Option|Phase|Step|Stage|Method|Style|Version|Variant|Draft|Revision|Alternative|Choice|Angle|Technique|Tactic|Strategy|Perspective|Approach|Mode|Tone|Pacing|Voice|Register|Frame|Lens|Focus|Theme|Arc|Beat|Thread|Track|Plot|Path|Route|Way|Direction|Course|Plan|Scheme|Design|Blueprint|Outline|Map|Guide|Template|Model|Pattern|Formula|Format|Structure|System|Method|Process|Procedure|Protocol|Sequence|Order|Flow|Rhythm|Cadence|Tempo|Pace|Speed|Rate|Level|Degree|Scale|Range|Scope|Depth|Breadth|Width|Height|Length|Size|Measure|Amount|Quantity|Volume|Weight|Mass|Density|Intensity|Strength|Force|Power|Energy|Drive|Momentum|Impulse|Push|Pull|Thrust|Lift|Rise|Fall|Drop|Sink|Plunge|Dive|Soar|Climb|Ascend|Descend|Mount|Scale|Summit|Peak|Crest|Top|Bottom|Base|Root|Core|Heart|Center|Axis|Pivot|Fulcrum|Hinge|Joint|Node|Hub|Nexus|Link|Bond|Tie|Knot|Weave|Thread|String|Wire|Cable|Chain|Rope|Line|Cord|Band|Strip|Bar|Rod|Beam|Rail|Track|Path|Trail|Route|Road|Way|Lane|Channel|Stream|Flow|Current|Tide|Wave|Surge|Pulse|Beat|Rhythm|Pattern|Cycle|Loop|Circuit|Round|Turn|Twist|Spiral|Helix|Coil|Curl|Bend|Curve|Arc|Bow|Arch|Dome|Vault|Span|Bridge|Link|Connect|Join|Merge|Fuse|Bond|Bind|Tie|Attach|Fix|Fasten|Secure|Lock|Seal|Close|Shut|End|Finish|Complete|Conclude|Resolve|Settle|Solve|Fix|Repair|Restore|Heal|Cure|Mend|Patch|Cover|Shield|Guard|Protect|Defend|Save|Rescue|Aid|Help|Support|Assist|Enable|Empower|Strengthen|Boost|Enhance|Improve|Upgrade|Advance|Progress|Develop|Grow|Expand|Extend|Spread|Broaden|Widen|Deepen|Heighten|Raise|Lift|Elevate|Promote|Advance|Drive|Push|Propel|Fuel|Power|Energize|Activate|Launch|Start|Begin|Initiate|Trigger|Spark|Ignite|Kindle|Light|Illuminate|Bright|Clear|Open|Reveal|Expose|Show|Display|Present|Offer|Give|Provide|Supply|Deliver|Send|Transfer|Move|Shift|Change|Transform|Convert|Alter|Modify|Adjust|Adapt|Tune|Calibrate|Align|Balance|Harmonize|Synchronize|Coordinate|Organize|Arrange|Order|Sort|Group|Cluster|Gather|Collect|Compile|Assemble|Build|Construct|Create|Make|Form|Shape|Mold|Cast|Forge|Craft|Fashion|Design|Plan|Scheme|Plot|Map|Chart|Draw|Sketch|Draft|Write|Record|Log|Note|Mark|Tag|Label|Name|Call|Title|Head|Lead|Guide|Direct|Steer|Navigate|Pilot|Drive|Control|Manage|Run|Operate|Work|Function|Perform|Execute|Carry|Conduct|Handle|Deal|Process|Handle)\s+\w*[:\-–—]\*{0,2}\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def _detect_approach_labels(scene: dict) -> list[dict]:
    """Find lines that are un-stripped approach/label markers."""
    issues = []
    for m in _APPROACH_RE.finditer(scene["content"]):
        issues.append({
            "type":        "approach_label",
            "scene_id":    scene["id"],
            "scene_title": scene["title"],
            "description": f"Approach label leaked into prose: '{m.group().strip()}'",
            "fix_type":    "strip_label",
            "detail":      m.group(),
        })
    return issues


def _detect_empty_scenes(scene: dict) -> list[dict]:
    """Flag scenes with no usable prose (< 30 words after stripping headers)."""
    clean = re.sub(r"^\s*#+.*$", "", scene["content"], flags=re.MULTILINE)
    clean = re.sub(r"^\s*\*.*\*\s*$", "", clean, flags=re.MULTILINE)
    clean = re.sub(r"---+", "", clean)
    word_count = len(clean.split())
    if word_count < 30:
        return [{
            "type":        "empty_scene",
            "scene_id":    scene["id"],
            "scene_title": scene["title"],
            "description": f"Scene has only {word_count} words of prose — likely empty or failed to generate.",
            "fix_type":    "regenerate_scene",
            "detail":      str(word_count),
        }]
    return []


def _detect_repetition(scene: dict) -> list[dict]:
    """Find paragraphs that repeat verbatim within a scene."""
    paragraphs = [p.strip() for p in scene["content"].split("\n\n") if len(p.strip()) > 80]
    seen: dict[str, int] = {}
    issues = []
    for i, p in enumerate(paragraphs):
        key = p[:200]  # first 200 chars as signature
        if key in seen:
            issues.append({
                "type":        "repetition",
                "scene_id":    scene["id"],
                "scene_title": scene["title"],
                "description": f"Paragraph repeated at position {i+1} (first seen at {seen[key]+1}).",
                "fix_type":    "strip_duplicates",
                "detail":      p[:120] + "…",
            })
        else:
            seen[key] = i
    return issues


def _detect_character_drift(scenes: list[dict], protagonist: str) -> list[dict]:
    """
    Find variant spellings of the protagonist name across all scenes.

    Compares against all capitalized tokens that share the first 3 letters.
    """
    if not protagonist:
        return []

    base = protagonist.split()[0][:3].lower()
    all_names: list[str] = []
    for sc in scenes:
        for match in re.finditer(r"\b[A-Z][a-z]{2,}\b", sc["content"]):
            if match.group().lower().startswith(base):
                all_names.append(match.group())

    if not all_names:
        return []

    counts = Counter(all_names)
    canonical = counts.most_common(1)[0][0]
    variants = [n for n, _ in counts.items() if n != canonical]

    if not variants:
        return []

    return [{
        "type":        "character_drift",
        "scene_id":    "all",
        "scene_title": "All Scenes",
        "description": (
            f"Character '{canonical}' appears under {len(variants)+1} spellings: "
            + ", ".join([canonical] + variants)
        ),
        "fix_type":    "normalize_name",
        "detail":      json.dumps({"canonical": canonical, "variants": variants}),
    }]


# ---------------------------------------------------------------------------
# Fix appliers
# ---------------------------------------------------------------------------

_APPROACH_STRIP_RE = re.compile(
    r"^[ \t]*\*{0,2}\s*(Approach|Narrative Approach|Narrative:?)\s*[#\w]*[:\-\u2013\u2014]?\s*[^\n]*\*{0,2}[ \t]*\n?",
    re.IGNORECASE | re.MULTILINE,
)


_APPROACH_INLINE_RE = re.compile(
    r"\b(Approach\s+(?:One|Two|Three|Four|Five|\d+|#\d+)[\s\-–—]*[\w\s\-–—()]*?)(?=[\.\,\;\:\"\']|\s{2,}|$)",
    re.IGNORECASE,
)


def _strip_labels(content: str) -> str:
    """Remove approach/label lines and inline approach references."""
    # Pass 1: full lines that are purely approach markers
    content = _APPROACH_STRIP_RE.sub("", content)
    # Pass 2: inline fragments like "Approach Three - Dialogue-Driven (Approach Two)"
    content = _APPROACH_INLINE_RE.sub("", content)
    # Collapse 3+ blank lines into 2
    content = re.sub(r"\n{3,}", "\n\n", content)
    return content.strip()


def _strip_duplicate_paragraphs(content: str) -> str:
    """Remove verbatim duplicate paragraphs, keeping first occurrence."""
    paragraphs = content.split("\n\n")
    seen: set[str] = set()
    unique = []
    for p in paragraphs:
        key = p.strip()[:200]
        if key not in seen:
            unique.append(p)
            seen.add(key)
    return "\n\n".join(unique)


def _normalize_names(content: str, canonical: str, variants: list[str]) -> str:
    """Replace all variant spellings with the canonical name."""
    for v in variants:
        content = re.sub(rf"\b{re.escape(v)}\b", canonical, content)
    return content


def _backup_scene(path: Path) -> None:
    """Create a .bak snapshot before modifying."""
    snap_dir = path.parent.parent / "snapshots" / path.stem
    snap_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    shutil.copy2(path, snap_dir / f"{ts}_pre_review.md")


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


class ReviewRequest(BaseModel):
    """Optional config for the review pass."""
    protagonist: str = ""


class FixRequest(BaseModel):
    """Apply a batch of fixes identified during review."""
    fixes: list[dict]  # list of issue dicts from /review endpoint
    protagonist: str = ""


@router.post("/projects/{project_id}/review")
async def review_project(project_id: str, req: ReviewRequest) -> dict:
    """
    Scan all scenes for common AI generation artifacts.

    Returns a list of issues with their fix_type and location.

    Args:
        project_id: The project to review.
        req:        Optional protagonist name for name-drift detection.

    Returns:
        Dict with 'issues' list and summary counts.
    """
    scenes = _load_scenes(project_id)
    issues: list[dict] = []

    for sc in scenes:
        issues += _detect_approach_labels(sc)
        issues += _detect_empty_scenes(sc)
        issues += _detect_repetition(sc)

    # Character drift is cross-scene
    protagonist = req.protagonist
    if not protagonist:
        # Try to load from project state
        proj = _project_dir(project_id)
        chars_path = proj / "state" / "characters.json"
        if chars_path.exists():
            chars = json.loads(chars_path.read_text())
            for name, data in chars.items():
                if data.get("role") == "protagonist":
                    protagonist = name
                    break

    issues += _detect_character_drift(scenes, protagonist)

    summary = Counter(i["type"] for i in issues)
    return {
        "issues":  issues,
        "total":   len(issues),
        "summary": dict(summary),
        "scene_count": len(scenes),
    }


@router.post("/projects/{project_id}/fix")
async def fix_project(project_id: str, req: FixRequest) -> dict:
    """
    Apply all auto-fixes: strip labels, remove duplicates, normalize names,
    and regenerate empty scenes via the AI writer.

    Backs up scenes before modifying. Returns count of fixes applied.

    Args:
        project_id: Target project.
        req:        List of issues from /review plus optional protagonist name.

    Returns:
        Dict with applied/skipped counts and per-scene results.
    """
    proj = _project_dir(project_id)
    scene_dir = proj / "scenes"
    applied = 0
    skipped = 0
    results: list[dict] = []

    # Group issues by scene for efficient processing
    by_scene: dict[str, list[dict]] = {}
    global_fixes: list[dict] = []

    for issue in req.fixes:
        if issue["scene_id"] == "all":
            global_fixes.append(issue)
        else:
            by_scene.setdefault(issue["scene_id"], []).append(issue)

    # --- Parse global name normalization data ---
    name_canonical = ""
    name_variants: list[str] = []
    for issue in global_fixes:
        if issue["fix_type"] == "normalize_name":
            data = json.loads(issue.get("detail", "{}"))
            name_canonical = data.get("canonical", "")
            name_variants  = data.get("variants", [])

    # --- Load project config for regeneration ---
    project_config = _load_project_config(proj)

    # --- Build full scene ID set (label/dupe fixes + name norm) ---
    all_scene_ids = set(by_scene.keys())
    if name_canonical:
        for md in sorted(scene_dir.glob("*.md")):
            all_scene_ids.add(md.stem)

    for sid in sorted(all_scene_ids):
        md = scene_dir / f"{sid}.md"
        if not md.exists():
            skipped += 1
            continue

        content = md.read_text(encoding="utf-8")
        original = content
        ops: list[str] = []

        # Label stripping
        if any(i["fix_type"] == "strip_label" for i in by_scene.get(sid, [])):
            content = _strip_labels(content)
            ops.append("strip_labels")

        # Duplicate paragraph removal
        if any(i["fix_type"] == "strip_duplicates" for i in by_scene.get(sid, [])):
            content = _strip_duplicate_paragraphs(content)
            ops.append("strip_duplicates")

        # Name normalization
        if name_canonical and name_variants:
            content = _normalize_names(content, name_canonical, name_variants)
            ops.append("normalize_name")

        if content != original:
            _backup_scene(md)
            md.write_text(content, encoding="utf-8")
            applied += len(ops)
            results.append({"scene_id": sid, "ops": ops, "status": "fixed"})
        else:
            results.append({"scene_id": sid, "ops": [], "status": "unchanged"})

    # --- Regenerate empty scenes ---
    regen_ids = [
        issue["scene_id"]
        for issue in req.fixes
        if issue.get("fix_type") == "regenerate_scene"
    ]

    for sid in regen_ids:
        md = scene_dir / f"{sid}.md"
        result = await _regenerate_scene(proj, sid, md, project_config)
        results.append(result)
        if result["status"] == "regenerated":
            applied += 1
        else:
            skipped += 1

    return {
        "applied": applied,
        "skipped": skipped,
        "results": results,
    }


# ---------------------------------------------------------------------------
# Scene regeneration helper
# ---------------------------------------------------------------------------


def _load_project_config(proj: Path) -> dict:
    """
    Build a config dict for _write_scene from the project's style_guide.md
    and project.json.

    Args:
        proj: Path to the project root directory.

    Returns:
        Dict compatible with BookConfig / _write_scene expectations.
    """
    cfg: dict = {
        "genre":      "fiction",
        "tone":       "balanced",
        "pov":        "third person limited",
        "premise":    "",
        "setting":    "",
        "protagonist": "",
        "antagonist":  "",
    }

    # Load project.json for basic metadata
    proj_json = proj / "project.json"
    if proj_json.exists():
        meta = json.loads(proj_json.read_text())
        cfg["genre"] = meta.get("genre", cfg["genre"])
        cfg["title"] = meta.get("title", "")

    # Parse style_guide.md for richer context
    sg = proj / "style_guide.md"
    if sg.exists():
        text = sg.read_text(encoding="utf-8")
        for section, key in [
            ("## Genre",       "genre"),
            ("## Tone",        "tone"),
            ("## Point of View", "pov"),
            ("## Setting",     "setting"),
            ("## Premise",     "premise"),
            ("## Protagonist", "protagonist"),
            ("## Antagonist",  "antagonist"),
        ]:
            if section in text:
                start = text.index(section) + len(section)
                end = text.find("\n##", start)
                value = text[start:end if end != -1 else None].strip()
                if value:
                    cfg[key] = value

    # Load characters.json for protagonist name
    chars_path = proj / "state" / "characters.json"
    if chars_path.exists():
        chars = json.loads(chars_path.read_text())
        for name, data in chars.items():
            if data.get("role") == "protagonist" and not cfg["protagonist"]:
                cfg["protagonist"] = data.get("description", name)
            if data.get("role") == "antagonist" and not cfg["antagonist"]:
                cfg["antagonist"] = data.get("description", name)

    return cfg


def _get_scene_context(proj: Path, sid: str) -> tuple[str, str]:
    """
    Extract chapter title and scene beat for a given scene ID from structure.json.

    Args:
        proj: Project root path.
        sid:  Scene ID like 'a1_c02_s003'.

    Returns:
        Tuple of (chapter_title, scene_beat).
    """
    struct_path = proj / "structure.json"
    if not struct_path.exists():
        return "Unknown Chapter", "Continue the narrative."

    structure = json.loads(struct_path.read_text())
    for act in structure.get("acts", []):
        for chap in act.get("chapters", []):
            for s in chap.get("scenes", []):
                if s["id"] == sid:
                    title = chap.get("title", "Unknown Chapter")
                    beat  = s.get("title", "Continue the narrative.")
                    return title, beat

    return "Unknown Chapter", "Continue the narrative."


async def _regenerate_scene(
    proj:           Path,
    sid:            str,
    md:             Path,
    project_config: dict,
) -> dict:
    """
    Regenerate a single empty scene using the book writer pipeline.

    Args:
        proj:           Project root path.
        sid:            Scene ID string.
        md:             Path to the scene .md file.
        project_config: Config dict from _load_project_config.

    Returns:
        Result dict with scene_id, status, and ops.
    """
    chapter_title, scene_beat = _get_scene_context(proj, sid)

    # Find previous scene content for continuity
    prev_ending = ""
    scene_dir = proj / "scenes"
    all_scenes = sorted(scene_dir.glob("*.md"))
    for i, sf in enumerate(all_scenes):
        if sf.stem == sid and i > 0:
            prev_text = all_scenes[i - 1].read_text(encoding="utf-8")
            words = prev_text.split()
            prev_ending = " ".join(words[-150:]) if len(words) > 150 else prev_text
            break

    try:
        scene_text = ""
        async for token in _bw_write_scene(
            config          = project_config,
            chapter_title   = chapter_title,
            scene_beat      = scene_beat,
            prev_ending     = prev_ending,
            rag_context     = "",
            words_per_scene = 400,
            book_title      = project_config.get("title", ""),
        ):
            scene_text += token

        if scene_text.strip():
            # Strip approach labels from regenerated content too
            scene_text = _strip_labels(scene_text)
            _backup_scene(md)
            md.write_text(scene_text.strip(), encoding="utf-8")
            return {"scene_id": sid, "ops": ["regenerated"], "status": "regenerated"}
        else:
            return {"scene_id": sid, "ops": [], "status": "regen_empty"}

    except Exception as e:
        return {"scene_id": sid, "ops": [], "status": f"regen_error: {e}"}
