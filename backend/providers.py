"""
Quill — LLM provider abstraction layer.

All providers implement BaseProvider and expose a single async generator:
    stream(messages, max_tokens, temperature) → AsyncIterator[str]

where each yielded string is a raw text token (not SSE-formatted).
The SSE wrapping is done in generate.py's _stream() helper.

Supported providers
───────────────────
  openai_compat   llama-server, LM Studio, Ollama (≥0.1.24), OpenAI,
                  Groq, OpenRouter, Together AI, Anyscale, Perplexity …
                  Anything that speaks POST /v1/chat/completions.

  ollama          Ollama native /api/chat (NDJSON streaming).
                  Use this for Ollama < 0.1.24 or when you want
                  model-list/pull features without an API key.

  anthropic       Anthropic Claude (Messages API, SSE).
                  Models: claude-3-5-sonnet-*, claude-3-haiku-*, …

  gemini          Google Gemini (generateContent streaming).
                  Models: gemini-1.5-flash, gemini-1.5-pro, …

Active provider is a hot-swappable singleton — reloaded when settings change.
"""

import json
import asyncio
from abc import ABC, abstractmethod
from typing import AsyncIterator, Optional
from pathlib import Path

import httpx

from . import config

# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------


class BaseProvider(ABC):
    """Abstract LLM provider."""

    name: str = "base"

    @abstractmethod
    async def stream(
        self,
        messages: list[dict],
        max_tokens: int,
        temperature: float,
        stop: list[str] | None = None,
    ) -> AsyncIterator[str]:
        """Yield raw text tokens (not SSE-framed)."""
        ...

    @abstractmethod
    async def test_connection(self) -> dict:
        """
        Probe the provider. Returns:
            {"ok": True/False, "message": str, "model": str}
        """
        ...

    async def list_models(self) -> list[str]:
        """Return available model names. Empty list if not supported."""
        return []


# ---------------------------------------------------------------------------
# OpenAI-compatible provider
# (llama-server, LM Studio, Ollama ≥0.1.24, OpenAI, Groq, OpenRouter, …)
# ---------------------------------------------------------------------------


class OpenAICompatProvider(BaseProvider):
    """
    Stream from any OpenAI-compatible /v1/chat/completions endpoint.

    Args:
        base_url:  e.g. "http://127.0.0.1:8081" or "https://api.openai.com/v1"
        api_key:   Bearer token (empty string for local servers)
        model:     Model name sent in the request body
        timeout:   HTTP timeout in seconds
    """

    name = "openai_compat"

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        model: str = "quill",
        timeout: float = 90.0,
    ) -> None:
        """Initialise an OpenAI-compatible provider."""
        self.base_url = base_url.rstrip("/")
        self.api_key  = api_key
        self.model    = model
        self.timeout  = timeout

    def _headers(self) -> dict:
        """Build request headers with optional Bearer token."""
        h = {"Accept": "text/event-stream", "Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    async def stream(
        self,
        messages: list[dict],
        max_tokens: int,
        temperature: float,
        stop: list[str] | None = None,
        frequency_penalty: float = 1.3,
        presence_penalty: float = 0.6,
    ) -> AsyncIterator[str]:
        """Stream tokens from an OpenAI-compatible endpoint."""
        payload = {
            "model":             self.model,
            "messages":         messages,
            "max_tokens":       max_tokens,
            "temperature":      temperature,
            "stream":           True,
            "frequency_penalty": frequency_penalty,   # penalise repeated tokens
            "presence_penalty":  presence_penalty,    # penalise already-seen tokens
            "repeat_penalty":    frequency_penalty,   # llama.cpp alias
        }
        if stop:
            payload["stop"] = stop

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                async with client.stream(
                    "POST",
                    f"{self.base_url}/v1/chat/completions",
                    json=payload,
                    headers=self._headers(),
                ) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        raw = line[6:].strip()
                        if raw == "[DONE]":
                            return
                        try:
                            chunk = json.loads(raw)
                            token = (
                                chunk["choices"][0]["delta"].get("content", "")
                            )
                            if token:
                                yield token
                        except (json.JSONDecodeError, KeyError, IndexError):
                            continue
        except httpx.RequestError as exc:
            yield f"[ERROR] Cannot reach LLM server: {exc}"

    async def test_connection(self) -> dict:
        """
        Test by hitting /v1/models or /v1/chat/completions with 1 token.
        """
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                r = await client.get(
                    f"{self.base_url}/v1/models",
                    headers=self._headers(),
                )
                if r.status_code == 200:
                    data = r.json()
                    models = [m["id"] for m in data.get("data", [])]
                    return {
                        "ok":      True,
                        "message": f"Connected — {len(models)} model(s) available",
                        "model":   self.model,
                        "models":  models,
                    }
                return {
                    "ok":      False,
                    "message": f"HTTP {r.status_code} from {self.base_url}/v1/models",
                }
        except Exception as exc:
            return {"ok": False, "message": str(exc)}

    async def list_models(self) -> list[str]:
        """Return model IDs from /v1/models."""
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                r = await client.get(
                    f"{self.base_url}/v1/models",
                    headers=self._headers(),
                )
                if r.status_code == 200:
                    return [m["id"] for m in r.json().get("data", [])]
        except Exception:
            pass
        return []


# ---------------------------------------------------------------------------
# Ollama native provider
# ---------------------------------------------------------------------------


class OllamaProvider(BaseProvider):
    """
    Ollama native streaming API (/api/chat).

    Streams NDJSON lines from Ollama's own format.
    Supports model listing via /api/tags and pulling via /api/pull.

    Args:
        base_url:  e.g. "http://127.0.0.1:11434"
        model:     Ollama model name, e.g. "llama3", "mistral", "phi3"
    """

    name = "ollama"

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:11434",
        model: str = "llama3",
        timeout: float = 90.0,
    ) -> None:
        """Initialise an Ollama native provider."""
        self.base_url = base_url.rstrip("/")
        self.model    = model
        self.timeout  = timeout

    async def stream(
        self,
        messages: list[dict],
        max_tokens: int,
        temperature: float,
        stop: list[str] | None = None,
    ) -> AsyncIterator[str]:
        """Stream tokens from Ollama /api/chat (NDJSON)."""
        payload: dict = {
            "model":    self.model,
            "messages": messages,
            "stream":   True,
            "options": {
                "num_predict": max_tokens,
                "temperature": temperature,
            },
        }
        if stop:
            payload["options"]["stop"] = stop

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                async with client.stream(
                    "POST",
                    f"{self.base_url}/api/chat",
                    json=payload,
                ) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line.strip():
                            continue
                        try:
                            chunk = json.loads(line)
                            token = chunk.get("message", {}).get("content", "")
                            if token:
                                yield token
                            if chunk.get("done"):
                                return
                        except json.JSONDecodeError:
                            continue
        except httpx.RequestError as exc:
            yield f"[ERROR] Cannot reach Ollama: {exc}"

    async def test_connection(self) -> dict:
        """Probe Ollama via GET /api/tags."""
        try:
            async with httpx.AsyncClient(timeout=6.0) as client:
                r = await client.get(f"{self.base_url}/api/tags")
                if r.status_code == 200:
                    models = [m["name"] for m in r.json().get("models", [])]
                    return {
                        "ok":      True,
                        "message": f"Ollama connected — {len(models)} model(s) installed",
                        "model":   self.model,
                        "models":  models,
                    }
                return {"ok": False, "message": f"HTTP {r.status_code}"}
        except Exception as exc:
            return {"ok": False, "message": str(exc)}

    async def list_models(self) -> list[str]:
        """List installed Ollama models from /api/tags."""
        try:
            async with httpx.AsyncClient(timeout=6.0) as client:
                r = await client.get(f"{self.base_url}/api/tags")
                if r.status_code == 200:
                    return [m["name"] for m in r.json().get("models", [])]
        except Exception:
            pass
        return []


# ---------------------------------------------------------------------------
# Anthropic provider
# ---------------------------------------------------------------------------


class AnthropicProvider(BaseProvider):
    """
    Anthropic Claude via the Messages API with streaming.

    Args:
        api_key:  Anthropic API key (sk-ant-…)
        model:    e.g. "claude-3-5-sonnet-20241022", "claude-3-haiku-20240307"
    """

    name = "anthropic"
    _API_URL = "https://api.anthropic.com/v1/messages"
    _VERSION = "2023-06-01"

    def __init__(
        self,
        api_key: str,
        model: str = "claude-3-haiku-20240307",
        timeout: float = 90.0,
    ) -> None:
        """Initialise an Anthropic provider."""
        self.api_key = api_key
        self.model   = model
        self.timeout = timeout

    def _headers(self) -> dict:
        """Build Anthropic-specific headers."""
        return {
            "x-api-key":         self.api_key,
            "anthropic-version": self._VERSION,
            "content-type":      "application/json",
        }

    def _adapt_messages(self, messages: list[dict]) -> tuple[str, list[dict]]:
        """
        Convert OpenAI-style messages to Anthropic format.

        Extracts the system message (first system role) and returns
        (system_prompt, user/assistant messages).
        """
        system      = ""
        user_msgs   = []
        for m in messages:
            role = m.get("role", "user")
            if role == "system":
                system = m.get("content", "")
            else:
                user_msgs.append({"role": role, "content": m.get("content", "")})
        # Anthropic requires alternating user/assistant, starting with user
        if not user_msgs or user_msgs[0]["role"] != "user":
            user_msgs.insert(0, {"role": "user", "content": "Continue."})
        return system, user_msgs

    async def stream(
        self,
        messages: list[dict],
        max_tokens: int,
        temperature: float,
        stop: list[str] | None = None,
    ) -> AsyncIterator[str]:
        """Stream tokens from Anthropic Messages API (SSE)."""
        system, user_msgs = self._adapt_messages(messages)
        payload: dict = {
            "model":      self.model,
            "messages":   user_msgs,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream":     True,
        }
        if system:
            payload["system"] = system
        if stop:
            payload["stop_sequences"] = stop

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                async with client.stream(
                    "POST",
                    self._API_URL,
                    json=payload,
                    headers=self._headers(),
                ) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        raw = line[6:].strip()
                        try:
                            ev = json.loads(raw)
                            if ev.get("type") == "content_block_delta":
                                delta = ev.get("delta", {})
                                if delta.get("type") == "text_delta":
                                    token = delta.get("text", "")
                                    if token:
                                        yield token
                            elif ev.get("type") == "message_stop":
                                return
                        except json.JSONDecodeError:
                            continue
        except httpx.HTTPStatusError as exc:
            yield f"[ERROR] Anthropic API: {exc.response.text[:200]}"
        except httpx.RequestError as exc:
            yield f"[ERROR] Cannot reach Anthropic: {exc}"

    async def test_connection(self) -> dict:
        """Send a minimal 1-token request to verify the API key."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.post(
                    self._API_URL,
                    json={
                        "model":      self.model,
                        "messages":   [{"role": "user", "content": "Hi"}],
                        "max_tokens": 1,
                    },
                    headers=self._headers(),
                )
                if r.status_code in (200, 201):
                    return {
                        "ok":      True,
                        "message": f"Anthropic API key valid — model: {self.model}",
                        "model":   self.model,
                    }
                return {
                    "ok":      False,
                    "message": f"HTTP {r.status_code}: {r.text[:200]}",
                }
        except Exception as exc:
            return {"ok": False, "message": str(exc)}

    async def list_models(self) -> list[str]:
        """Return known Claude model IDs (Anthropic has no /models endpoint)."""
        return [
            "claude-3-5-sonnet-20241022",
            "claude-3-5-haiku-20241022",
            "claude-3-opus-20240229",
            "claude-3-haiku-20240307",
        ]


# ---------------------------------------------------------------------------
# Google Gemini provider
# ---------------------------------------------------------------------------


class GeminiProvider(BaseProvider):
    """
    Google Gemini via generateContent streaming (REST SSE).

    Args:
        api_key:  Google AI Studio API key
        model:    e.g. "gemini-1.5-flash", "gemini-1.5-pro", "gemini-2.0-flash"
    """

    name = "gemini"
    _BASE = "https://generativelanguage.googleapis.com/v1beta/models"

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-2.5-flash",
        timeout: float = 180.0,
    ) -> None:
        """Initialise a Gemini provider."""
        self.api_key = api_key
        self.model   = model
        self.timeout = timeout

    def _adapt_messages(self, messages: list[dict]) -> tuple[str, list[dict]]:
        """Convert OpenAI-style messages → Gemini contents + system_instruction."""
        system_text = ""
        contents    = []
        for m in messages:
            role = m.get("role", "user")
            if role == "system":
                system_text = m.get("content", "")
                continue
            gemini_role = "model" if role == "assistant" else "user"
            contents.append({
                "role":  gemini_role,
                "parts": [{"text": m.get("content", "")}],
            })
        # Gemini requires the first content to be from "user"
        if not contents or contents[0]["role"] != "user":
            contents.insert(0, {"role": "user", "parts": [{"text": "Continue."}]})
        return system_text, contents

    async def stream(
        self,
        messages: list[dict],
        max_tokens: int,
        temperature: float,
        stop: list[str] | None = None,
    ) -> AsyncIterator[str]:
        """Stream tokens from Gemini streamGenerateContent."""
        system_text, contents = self._adapt_messages(messages)

        gen_config: dict = {
            "maxOutputTokens": max_tokens,
            "temperature":     temperature,
        }
        # Disable extended thinking on Gemini 2.5 models — it burns the token
        # budget silently and leaves almost nothing for actual output.
        if "2.5" in self.model or "2-5" in self.model:
            gen_config["thinkingConfig"] = {"thinkingBudget": 0}

        payload: dict = {
            "contents":         contents,
            "generationConfig": gen_config,
        }
        if system_text:
            payload["system_instruction"] = {
                "parts": [{"text": system_text}]
            }
        if stop:
            payload["generationConfig"]["stopSequences"] = stop

        url = (
            f"{self._BASE}/{self.model}:streamGenerateContent"
            f"?alt=sse&key={self.api_key}"
        )

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                async with client.stream("POST", url, json=payload) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        raw = line[6:].strip()
                        try:
                            ev = json.loads(raw)
                            for cand in ev.get("candidates", []):
                                for part in cand.get("content", {}).get("parts", []):
                                    # Skip thought parts (Gemini 2.5 thinking blocks)
                                    if part.get("thought"):
                                        continue
                                    token = part.get("text", "")
                                    if token:
                                        yield token
                        except json.JSONDecodeError:
                            continue
        except httpx.HTTPStatusError as exc:
            yield f"[ERROR] Gemini API: {exc.response.text[:200]}"
        except httpx.RequestError as exc:
            yield f"[ERROR] Cannot reach Gemini: {exc}"

    async def test_connection(self) -> dict:
        """Send a minimal request to verify key + model."""
        system_text, contents = self._adapt_messages(
            [{"role": "user", "content": "Hi"}]
        )
        url = (
            f"{self._BASE}/{self.model}:generateContent?key={self.api_key}"
        )
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.post(
                    url,
                    json={
                        "contents":         contents,
                        "generationConfig": {"maxOutputTokens": 1},
                    },
                )
                if r.status_code == 200:
                    return {
                        "ok":      True,
                        "message": f"Gemini API key valid — model: {self.model}",
                        "model":   self.model,
                    }
                return {
                    "ok":      False,
                    "message": f"HTTP {r.status_code}: {r.text[:200]}",
                }
        except Exception as exc:
            return {"ok": False, "message": str(exc)}

    async def list_models(self) -> list[str]:
        """Return known Gemini model IDs."""
        return [
            "gemini-2.5-flash",
            "gemini-2.0-flash",
            "gemini-2.0-flash-lite",
            "gemini-1.5-flash",
            "gemini-1.5-flash-8b",
            "gemini-1.5-pro",
        ]


# ---------------------------------------------------------------------------
# Provider registry — maps type string → constructor
# ---------------------------------------------------------------------------

#: Human-readable metadata for the settings UI.
PROVIDER_REGISTRY: dict[str, dict] = {
    "llama_server": {
        "label":       "llama-server (BitNet / llama.cpp)",
        "type":        "openai_compat",
        "default_url": "http://127.0.0.1:8081",
        "needs_key":   False,
        "needs_model": True,
        "local":       True,
        "description": "Local llama.cpp server. Recommended: BitNet 2B.",
    },
    "lm_studio": {
        "label":       "LM Studio",
        "type":        "openai_compat",
        "default_url": "http://127.0.0.1:1234",
        "needs_key":   False,
        "needs_model": True,
        "local":       True,
        "description": "LM Studio local server (OpenAI-compatible).",
    },
    "ollama": {
        "label":       "Ollama",
        "type":        "ollama",
        "default_url": "http://127.0.0.1:11434",
        "needs_key":   False,
        "needs_model": True,
        "local":       True,
        "description": "Ollama local model runner. Auto-detects installed models.",
    },
    "openai": {
        "label":       "OpenAI",
        "type":        "openai_compat",
        "default_url": "https://api.openai.com/v1",
        "needs_key":   True,
        "needs_model": True,
        "local":       False,
        "description": "OpenAI GPT-4o, GPT-4o-mini, etc. Requires API key.",
    },
    "anthropic": {
        "label":       "Anthropic Claude",
        "type":        "anthropic",
        "default_url": "",
        "needs_key":   True,
        "needs_model": True,
        "local":       False,
        "description": "Claude 3.5 Sonnet, Haiku, Opus. Requires API key.",
    },
    "gemini": {
        "label":       "Google Gemini",
        "type":        "gemini",
        "default_url": "",
        "needs_key":   True,
        "needs_model": True,
        "local":       False,
        "description": "Gemini 1.5 Flash / Pro / 2.0. Requires Google AI Studio key.",
    },
    "groq": {
        "label":       "Groq",
        "type":        "openai_compat",
        "default_url": "https://api.groq.com/openai/v1",
        "needs_key":   True,
        "needs_model": True,
        "local":       False,
        "description": "Groq — extremely fast LLaMA / Mixtral inference. Requires API key.",
    },
    "openrouter": {
        "label":       "OpenRouter",
        "type":        "openai_compat",
        "default_url": "https://openrouter.ai/api/v1",
        "needs_key":   True,
        "needs_model": True,
        "local":       False,
        "description": "OpenRouter — routes to 200+ models. Requires API key.",
    },
    "custom": {
        "label":       "Custom (OpenAI-compatible)",
        "type":        "openai_compat",
        "default_url": "http://127.0.0.1:8080",
        "needs_key":   False,
        "needs_model": True,
        "local":       True,
        "description": "Any OpenAI-compatible endpoint (vLLM, text-generation-webui, etc.).",
    },
}


# ---------------------------------------------------------------------------
# Provider factory
# ---------------------------------------------------------------------------


def build_provider(cfg: dict) -> BaseProvider:
    """
    Instantiate the correct provider from a config dict.

    Expected cfg keys:
        provider_id   str   Key into PROVIDER_REGISTRY
        base_url      str   Override URL (optional for cloud providers)
        api_key       str   API key (empty for local)
        model         str   Model name / ID

    Returns:
        Concrete BaseProvider instance.
    """
    provider_id = cfg.get("provider_id", "llama_server")
    registry    = PROVIDER_REGISTRY.get(provider_id, PROVIDER_REGISTRY["llama_server"])
    ptype       = registry["type"]

    base_url  = cfg.get("base_url", registry.get("default_url", ""))
    api_key   = cfg.get("api_key", "")
    model     = cfg.get("model", "")

    if ptype == "openai_compat":
        return OpenAICompatProvider(
            base_url=base_url,
            api_key=api_key,
            model=model or "quill",
        )
    elif ptype == "ollama":
        return OllamaProvider(
            base_url=base_url or "http://127.0.0.1:11434",
            model=model or "llama3",
        )
    elif ptype == "anthropic":
        return AnthropicProvider(api_key=api_key, model=model or "claude-3-haiku-20240307")
    elif ptype == "gemini":
        return GeminiProvider(api_key=api_key, model=model or "gemini-1.5-flash")
    else:
        # Fallback: treat as OpenAI-compat
        return OpenAICompatProvider(base_url=base_url, api_key=api_key, model=model)


# ---------------------------------------------------------------------------
# Active provider singleton
# ---------------------------------------------------------------------------

_active_provider: Optional[BaseProvider] = None


def get_active_provider() -> BaseProvider:
    """
    Return the currently active provider, building it from config if needed.

    Thread-safe for async use (FastAPI single-process).
    """
    global _active_provider
    if _active_provider is None:
        _active_provider = _build_from_config()
    return _active_provider


def reload_provider() -> None:
    """
    Invalidate the cached provider. Next call to get_active_provider()
    will rebuild from the current config file.
    Called after settings are saved.
    """
    global _active_provider
    _active_provider = None


def _build_from_config() -> BaseProvider:
    """
    Read ~/.quill/config.json and build the active provider.
    Falls back to llama-server defaults if no config exists.
    """
    cfg_path = Path.home() / ".quill" / "config.json"
    if cfg_path.exists():
        try:
            with open(cfg_path) as f:
                full_cfg = json.load(f)
            provider_cfg = full_cfg.get("provider", {})
            if provider_cfg:
                return build_provider(provider_cfg)
        except Exception as exc:
            print(f"[Quill] Config load error: {exc} — using defaults")

    # Default: llama-server on 8081
    return OpenAICompatProvider(
        base_url=config.LLAMA_SERVER_URL,
        model="quill",
    )
