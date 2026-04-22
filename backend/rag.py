"""
Quill Phase 2 — ChromaDB vector store + MiniLM embedding.

Uses sentence-transformers/all-MiniLM-L6-v2 (~80 MB, CPU-friendly)
to embed scene summaries for semantic retrieval.

ChromaDB is ephemeral — always rebuildable from characters.json and
scene_meta.json if the chroma/ directory is deleted.

Downloads the MiniLM model on first use (~80 MB from Hugging Face).
Subsequent runs load from local cache (~50ms).
"""

import asyncio
from functools import lru_cache
from pathlib import Path
from typing import Optional

import chromadb
from chromadb.config import Settings

from . import config
from .projects import _project_dir, _load_json

# ---------------------------------------------------------------------------
# Embedder (singleton — loaded once, reused)
# ---------------------------------------------------------------------------

_embedder = None
_embedder_ready = False


def _get_embedder():
    """
    Load MiniLM embedder lazily on first call.

    Returns:
        SentenceTransformer instance.
    """
    global _embedder, _embedder_ready
    if _embedder is None:
        from sentence_transformers import SentenceTransformer
        print("[Quill RAG] Loading MiniLM embedder... (first run may download ~80 MB)")
        _embedder = SentenceTransformer("all-MiniLM-L6-v2")
        _embedder_ready = True
        print("[Quill RAG] Embedder ready.")
    return _embedder


def embed(text: str) -> list[float]:
    """
    Embed a text string using MiniLM-L6-v2.

    Args:
        text: Input string.

    Returns:
        384-dimensional float vector.
    """
    enc = _get_embedder().encode(text, convert_to_numpy=True)
    return enc.tolist()


# ---------------------------------------------------------------------------
# ChromaDB client (per-project, PersistentClient)
# ---------------------------------------------------------------------------


def _chroma_path(project_id: str) -> Path:
    """Return ChromaDB storage path for a project."""
    return _project_dir(project_id) / "state" / "chroma"


def _get_client(project_id: str) -> chromadb.PersistentClient:
    """
    Get or create a ChromaDB PersistentClient for a project.

    Args:
        project_id: Project identifier.

    Returns:
        ChromaDB client instance.
    """
    path = _chroma_path(project_id)
    path.mkdir(parents=True, exist_ok=True)
    return chromadb.PersistentClient(
        path=str(path),
        settings=Settings(anonymized_telemetry=False),
    )


def _get_collection(project_id: str) -> chromadb.Collection:
    """
    Get or create the 'scenes' collection for a project.

    Args:
        project_id: Project identifier.

    Returns:
        ChromaDB Collection.
    """
    client = _get_client(project_id)
    return client.get_or_create_collection(
        name="scenes",
        metadata={"hnsw:space": "cosine"},
    )


# ---------------------------------------------------------------------------
# Core RAG operations
# ---------------------------------------------------------------------------


async def upsert_scene(
    project_id: str,
    scene_id: str,
    summary: str,
    characters: list[str],
) -> None:
    """
    Embed and upsert a scene summary into ChromaDB.

    Runs the CPU embedding in a thread pool to avoid blocking the
    async event loop.

    Args:
        project_id: Project ID.
        scene_id: Scene ID (used as ChromaDB document ID).
        summary: Compressed scene summary (~1 sentence).
        characters: List of character names present in the scene.
    """
    loop = asyncio.get_event_loop()
    embedding = await loop.run_in_executor(None, embed, summary)

    col = _get_collection(project_id)
    col.upsert(
        ids=[scene_id],
        embeddings=[embedding],
        documents=[summary],
        metadatas=[{
            "scene_id":   scene_id,
            "characters": ",".join(characters),
        }],
    )


async def query_relevant(
    project_id: str,
    query_text: str,
    n: int = 3,
    exclude_scene_id: Optional[str] = None,
) -> list[dict]:
    """
    Retrieve the top-n most relevant scene summaries for a query.

    Args:
        project_id: Project ID.
        query_text: The current scene / user intent to match against.
        n: Number of results.
        exclude_scene_id: Optionally exclude the current scene from results.

    Returns:
        List of dicts with keys: scene_id, summary, characters.
    """
    col = _get_collection(project_id)
    count = col.count()
    if count == 0:
        return []

    loop = asyncio.get_event_loop()
    q_vec = await loop.run_in_executor(None, embed, query_text)

    n_query = min(n + 1, count)   # fetch one extra in case we exclude one
    results = col.query(
        query_embeddings=[q_vec],
        n_results=n_query,
        include=["documents", "metadatas"],
    )

    hits = []
    for doc, meta in zip(
        results["documents"][0],
        results["metadatas"][0],
    ):
        sid = meta.get("scene_id", "")
        if sid == exclude_scene_id:
            continue
        hits.append({
            "scene_id":   sid,
            "summary":    doc,
            "characters": meta.get("characters", "").split(","),
        })
        if len(hits) >= n:
            break

    return hits


async def build_rag_context(
    project_id: str,
    query_text: str,
    active_characters: list[str],
    current_scene_id: Optional[str] = None,
) -> str:
    """
    Build a RAG context string within the ≤930 token budget.

    Returns a formatted string ready for injection into LLM prompts.

    Budget breakdown:
      Style guide:        ≤150 tokens  (enforced by caller)
      Character cards:    ≤80 tokens each × max 3 = 240 tokens
      RAG summaries:      ≤30 tokens each × 3 = 90 tokens
      Last scene excerpt: ≤400 tokens  (enforced by caller)
      Instruction:        ≤50 tokens
      ─────────────────────────────────────────────────────
      This function handles character cards + RAG summaries.

    Args:
        project_id: Project ID.
        query_text: Text used to query relevant scenes.
        active_characters: Characters present in the current scene.
        current_scene_id: Exclude from RAG results.

    Returns:
        Context string to prepend to LLM prompt.
    """
    chars_db: dict = _load_json(
        _project_dir(project_id) / "state" / "characters.json"
    ) or {}

    world_rules: list = _load_json(
        _project_dir(project_id) / "state" / "world_rules.json"
    ) or []

    sections: list[str] = []

    # ── Character cards (max 3, prioritise active characters) ─────────────
    char_names = [c for c in active_characters if c in chars_db][:3]
    if char_names:
        cards = []
        for name in char_names:
            c = chars_db[name]
            parts = [f"[{name}]"]
            if c.get("appearance"): parts.append(f"appearance: {c['appearance']}")
            if c.get("location"):   parts.append(f"location: {c['location']}")
            if c.get("trait"):      parts.append(f"trait: {c['trait']}")
            if c.get("arc_state"):  parts.append(f"arc: {c['arc_state']}")
            cards.append(" | ".join(parts))
        sections.append("CHARACTERS:\n" + "\n".join(cards))

    # ── RAG: top-3 relevant past scenes (compressed to ≤30 tokens each) ──
    rag_hits = await query_relevant(
        project_id, query_text, n=3, exclude_scene_id=current_scene_id
    )
    if rag_hits:
        summaries = [h["summary"] for h in rag_hits]
        sections.append("RELEVANT PAST SCENES:\n" + "\n".join(f"• {s}" for s in summaries))

    # ── World rules (first 5) ─────────────────────────────────────────────
    if world_rules:
        rules_text = "\n".join(f"• {r['fact']}" for r in world_rules[:5])
        sections.append(f"WORLD FACTS:\n{rules_text}")

    return "\n\n".join(sections)


async def rebuild_index(project_id: str) -> int:
    """
    Rebuild the ChromaDB index from scene_meta.json.

    Called when the chroma/ directory is missing or stale.

    Args:
        project_id: Project ID.

    Returns:
        Number of scenes indexed.
    """
    meta_path = _project_dir(project_id) / "state" / "scene_meta.json"
    meta: dict = _load_json(meta_path) or {}

    count = 0
    for scene_id, data in meta.items():
        summary = data.get("compressed_summary") or data.get("scene_summary", "")
        if not summary:
            continue
        await upsert_scene(
            project_id=project_id,
            scene_id=scene_id,
            summary=summary,
            characters=data.get("characters_present", []),
        )
        count += 1

    return count
