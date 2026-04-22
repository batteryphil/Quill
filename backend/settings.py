"""
Quill — Settings API.

Manages LLM provider configuration stored in ~/.quill/config.json.
The full config is never sent to the client with raw API keys —
keys are always masked as "sk-***…***" in GET responses.

Endpoints
─────────
  GET  /api/settings              Current config (keys masked)
  PUT  /api/settings              Save new config → reload provider
  GET  /api/settings/providers    Registry of all supported provider types
  POST /api/settings/test         Test connection to current or given config
  GET  /api/settings/models       List models from active provider
"""

import json
import re
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel, Field

from .providers import (
    PROVIDER_REGISTRY,
    build_provider,
    get_active_provider,
    reload_provider,
)

router = APIRouter(prefix="/api/settings", tags=["settings"])

# ---------------------------------------------------------------------------
# Config path
# ---------------------------------------------------------------------------

_CONFIG_PATH = Path.home() / ".quill" / "config.json"


def _load_config() -> dict:
    """Load config from disk. Returns defaults if not found."""
    if _CONFIG_PATH.exists():
        try:
            with open(_CONFIG_PATH) as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "provider": {
            "provider_id": "llama_server",
            "base_url":    "http://127.0.0.1:8081",
            "api_key":     "",
            "model":       "quill",
        }
    }


def _save_config(cfg: dict) -> None:
    """Persist config to disk (creates ~/.quill/ if needed)."""
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


def _mask_key(key: str) -> str:
    """Return a masked API key for safe client display."""
    if not key or len(key) < 8:
        return ""
    return key[:4] + "***" + key[-4:]


def _mask_config(cfg: dict) -> dict:
    """Return a copy of cfg with API keys masked."""
    import copy
    masked = copy.deepcopy(cfg)
    provider = masked.get("provider", {})
    if provider.get("api_key"):
        provider["api_key"] = _mask_key(provider["api_key"])
    return masked


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class ProviderConfig(BaseModel):
    """LLM provider configuration."""

    provider_id: str = Field("llama_server", description="Key into PROVIDER_REGISTRY")
    base_url:    str = Field("http://127.0.0.1:8081", description="Base URL for local or API endpoint")
    api_key:     str = Field("", description="API key (empty for local providers)")
    model:       str = Field("quill", description="Model name / ID")


class SettingsPayload(BaseModel):
    """Full settings payload."""

    provider: ProviderConfig


class TestRequest(BaseModel):
    """Test a specific provider config (may differ from saved config)."""

    provider: ProviderConfig


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("")
async def get_settings() -> dict:
    """
    Return current settings with API keys masked.

    Use PUT /api/settings to update.
    """
    cfg = _load_config()
    return _mask_config(cfg)


@router.put("")
async def save_settings(payload: SettingsPayload) -> dict:
    """
    Save new provider settings and hot-reload the active provider.

    API key masking: if the client sends back a masked key (contains ***),
    the existing stored key is preserved.
    """
    existing = _load_config()
    existing_key = existing.get("provider", {}).get("api_key", "")

    new_key = payload.provider.api_key
    # If client echoed back a masked key, preserve existing
    if "***" in new_key:
        new_key = existing_key

    cfg = {
        "provider": {
            "provider_id": payload.provider.provider_id,
            "base_url":    payload.provider.base_url,
            "api_key":     new_key,
            "model":       payload.provider.model,
        }
    }
    _save_config(cfg)
    reload_provider()

    return {"saved": True, **_mask_config(cfg)}


@router.get("/providers")
async def list_providers() -> dict:
    """
    Return the full PROVIDER_REGISTRY for populating the settings UI.
    Includes label, description, whether an API key / URL is needed.
    """
    return PROVIDER_REGISTRY


@router.post("/test")
async def test_provider(req: TestRequest) -> dict:
    """
    Test a provider config (may be unsaved / in-progress).

    Returns {"ok": bool, "message": str, "model": str, "models": list}.
    """
    existing_key = _load_config().get("provider", {}).get("api_key", "")
    key = req.provider.api_key
    if "***" in key:
        key = existing_key

    cfg = {
        "provider_id": req.provider.provider_id,
        "base_url":    req.provider.base_url,
        "api_key":     key,
        "model":       req.provider.model,
    }
    try:
        provider = build_provider(cfg)
        result   = await provider.test_connection()
        return result
    except Exception as exc:
        return {"ok": False, "message": str(exc)}


@router.get("/models")
async def list_models() -> dict:
    """Return model names from the currently active provider."""
    try:
        models = await get_active_provider().list_models()
        return {"models": models}
    except Exception as exc:
        return {"models": [], "error": str(exc)}
