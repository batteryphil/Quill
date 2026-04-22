"""
Quill — FastAPI application entry point.

Mounts the frontend static files and registers all API routers.
Run with:  python -m uvicorn backend.main:app --reload --port 8000
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .generate    import router as gen_router
from .projects    import router as proj_router
from .extract     import router as extract_router
from .audit       import router as audit_router
from .export      import router as export_router
from .settings    import router as settings_router
from .updater     import router as updater_router
from .bookwriter  import router as bookwriter_router

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Quill",
    description="AI-first local book writing backend",
    version="0.1.0",
)

# Allow browser requests from any origin during local dev
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

app.include_router(gen_router)
app.include_router(proj_router)
app.include_router(extract_router)
app.include_router(audit_router)
app.include_router(export_router)
app.include_router(settings_router)
app.include_router(updater_router)
app.include_router(bookwriter_router)

# RAG endpoints (query + rebuild)
@app.post("/api/rag/rebuild/{project_id}")
async def rag_rebuild(project_id: str) -> dict:
    """Rebuild the ChromaDB index from stored scene_meta.json."""
    from .rag import rebuild_index
    count = await rebuild_index(project_id)
    return {"indexed": count}

# ---------------------------------------------------------------------------
# Static frontend
# ---------------------------------------------------------------------------

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"

app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
