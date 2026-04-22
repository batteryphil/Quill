"""
Quill — generation routes.

All endpoints stream via Server-Sent Events (SSE).
Connects to llama-server's OpenAI-compatible /v1/chat/completions
and re-emits tokens in our simple format: ``data: <token>\\n\\n``
"""

import json
import asyncio
from typing import AsyncIterator

import httpx
from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from . import config

router = APIRouter(prefix="/api/generate", tags=["generate"])

# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class CompleteRequest(BaseModel):
    """Ghost text sentence completion request."""

    prefix: str = Field(..., description="Last ~150 words before cursor")
    max_tokens: int = Field(config.GHOST_MAX_TOKENS, ge=1, le=50)


class ContinueRequest(BaseModel):
    """'Continue from here' request."""

    prefix: str = Field(..., description="Last ~500 words for style continuity")
    instruction: str = Field("Continue the story naturally.", max_length=200)
    max_tokens: int = Field(config.CONTINUE_MAX_TOKENS, ge=50, le=500)
    # RAG enrichment (optional — gracefully skipped if not provided)
    project_id: str = Field("", description="Project ID for RAG context")
    scene_id: str   = Field("", description="Current scene ID (excluded from RAG)")
    characters: list[str] = Field(default_factory=list, description="Active characters")


class SelfWriteRequest(BaseModel):
    """Write a full scene from a brief."""

    who: str = Field(..., description="Characters present, comma-separated")
    where: str = Field(..., description="Location of the scene")
    what: str = Field(..., description="What must happen in this scene")
    tone: str = Field("neutral", description="Tone descriptor, e.g. tense, lyrical")
    target_words: int = Field(400, ge=100, le=800)
    prior_context: str = Field("", description="Last scene excerpt for voice continuity")
    max_tokens: int = Field(config.SELF_WRITE_MAX_TOKENS, ge=100, le=700)
    # RAG enrichment (optional)
    project_id: str = Field("", description="Project ID for RAG context")
    scene_id: str   = Field("", description="Scene about to be written")


class RephraseRequest(BaseModel):
    """Rephrase a highlighted selection."""

    text: str = Field(..., description="Selected text to rephrase")
    style: str = Field("same", description="'same' | 'simpler' | 'elevated' | 'shorter'")
    context: str = Field("", description="Surrounding paragraph for tone reference")


# ---------------------------------------------------------------------------
# Core SSE emitter
# ---------------------------------------------------------------------------


async def _stream_llama(
    messages: list[dict],
    max_tokens: int,
    temperature: float = config.DEFAULT_TEMPERATURE,
    top_p: float = config.DEFAULT_TOP_P,
    stop: list[str] | None = None,
) -> AsyncIterator[str]:
    """
    Stream tokens from llama-server and yield SSE-formatted strings.

    Emitted format (consumed by frontend stream.js):
        data: <token>\\n\\n
        data: [DONE]\\n\\n
    """
    payload = {
        "model": "quill",
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "top_p": top_p,
        "stream": True,
        "stop": stop or config.STOP_SEQUENCES,
    }

    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            async with client.stream(
                "POST",
                f"{config.LLAMA_SERVER_URL}/v1/chat/completions",
                json=payload,
                headers={"Accept": "text/event-stream"},
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    raw = line[6:].strip()
                    if raw == "[DONE]":
                        break
                    try:
                        chunk = json.loads(raw)
                        token = chunk["choices"][0]["delta"].get("content", "")
                        if token:
                            yield f"data: {json.dumps(token)}\n\n"
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue
    except httpx.RequestError as exc:
        yield f"data: {json.dumps('[ERROR] Cannot reach LLM server: ' + str(exc))}\n\n"
    finally:
        yield "data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/complete")
async def generate_complete(req: CompleteRequest) -> StreamingResponse:
    """
    Sentence completion — ghost text endpoint.

    Receives the last ~150 words (prefix) and streams a short completion
    of max 25 tokens. Frontend injects these as ProseMirror Decorations.
    """
    messages = [
        {
            "role": "system",
            "content": (
                "You are a prose completion assistant. "
                "Complete the given text naturally and concisely. "
                "Output ONLY the completion text. "
                "No explanation, no leading space, no quotation marks. "
                "Stop after one sentence or clause."
            ),
        },
        {
            "role": "user",
            "content": f"Complete this: {req.prefix}",
        },
    ]
    return StreamingResponse(
        _stream_llama(messages, req.max_tokens, temperature=0.75, stop=["\n", "."]),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/continue")
async def generate_continue(req: ContinueRequest) -> StreamingResponse:
    """
    'Continue from here' — streams ~350 words matching the existing voice.

    If project_id is provided, injects RAG context (character cards,
    relevant past scenes, world facts) within the ≤930 token budget.
    """
    # Build RAG context block if project info provided
    rag_block = ""
    if req.project_id:
        try:
            from .rag import build_rag_context
            rag_block = await build_rag_context(
                project_id=req.project_id,
                query_text=req.prefix[-500:],
                active_characters=req.characters,
                current_scene_id=req.scene_id or None,
            )
        except Exception as exc:
            print(f"[Quill RAG] Context build error: {exc}")

    rag_section = f"{rag_block}\n\n" if rag_block else ""

    messages = [
        {
            "role": "system",
            "content": (
                "You are a fiction writing assistant. "
                "Continue the story from exactly where the excerpt ends. "
                "Match the voice, tense, and pacing of the provided text precisely. "
                "Do not summarise. Do not add chapter headings. "
                "Just continue the prose as if you wrote everything before it."
            ),
        },
        {
            "role": "user",
            "content": (
                f"{rag_section}"
                f"{req.instruction}\n\n"
                f"--- STORY SO FAR (continue from here) ---\n{req.prefix}"
            ),
        },
    ]
    return StreamingResponse(
        _stream_llama(messages, req.max_tokens, temperature=0.72),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/self_write")
async def generate_self_write(req: SelfWriteRequest) -> StreamingResponse:
    """
    Self-write — generates a full scene from a brief.

    Injects RAG context if project_id is supplied.
    Prompt budget strictly enforced: prior_context truncated to 400 tokens max.
    """
    # Truncate prior_context to stay within budget (~400 tokens ≈ 300 words)
    prior_words = req.prior_context.split()
    if len(prior_words) > 300:
        prior_context = " ".join(prior_words[-300:])
    else:
        prior_context = req.prior_context

    prior_block = (
        f"\n\nPREVIOUS SCENE EXCERPT (for voice/style reference):\n{prior_context}"
        if prior_context
        else ""
    )

    # Build RAG context block
    rag_block = ""
    who_list  = [n.strip() for n in req.who.split(",") if n.strip()]
    if req.project_id:
        try:
            from .rag import build_rag_context
            rag_block = await build_rag_context(
                project_id=req.project_id,
                query_text=f"{req.where} {req.what}",
                active_characters=who_list,
                current_scene_id=req.scene_id or None,
            )
        except Exception as exc:
            print(f"[Quill RAG] Self-write context error: {exc}")

    rag_section = f"{rag_block}\n\n" if rag_block else ""

    messages = [
        {
            "role": "system",
            "content": (
                "You are a skilled fiction author. "
                "Write scenes that are vivid, grounded, and emotionally resonant. "
                "Show don't tell. Use specific concrete details. "
                "Match the tone of any provided reference text exactly."
            ),
        },
        {
            "role": "user",
            "content": (
                f"{rag_section}"
                f"Write a scene with these parameters:\n"
                f"Characters: {req.who}\n"
                f"Location: {req.where}\n"
                f"What must happen: {req.what}\n"
                f"Tone: {req.tone}\n"
                f"Target length: ~{req.target_words} words\n"
                f"{prior_block}\n\n"
                f"Write the scene now. Full prose only, no outline, no headers."
            ),
        },
    ]
    return StreamingResponse(
        _stream_llama(messages, req.max_tokens, temperature=0.78),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/rephrase")
async def generate_rephrase(req: RephraseRequest) -> StreamingResponse:
    """
    Rephrase a highlighted selection.
    """
    style_instructions = {
        "same":     "Rephrase keeping exactly the same tone, register, and length.",
        "simpler":  "Rephrase using simpler, clearer language. Shorter sentences.",
        "elevated": "Rephrase with more literary, elevated prose. Richer vocabulary.",
        "shorter":  "Rephrase more concisely. Cut at least 30% of the word count.",
    }
    style_note = style_instructions.get(req.style, style_instructions["same"])

    context_block = (
        f"\nContext (for tone reference only, do not rewrite):\n{req.context}\n\n"
        if req.context
        else ""
    )

    messages = [
        {
            "role": "system",
            "content": (
                f"You are a prose editor. {style_note} "
                "Output ONLY the rephrased text. No explanation, no preamble."
            ),
        },
        {
            "role": "user",
            "content": f"{context_block}Rephrase this:\n{req.text}",
        },
    ]
    return StreamingResponse(
        _stream_llama(messages, min(len(req.text.split()) * 2, 300), temperature=0.65),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Phase 3: Brainstorm
# ---------------------------------------------------------------------------


class BrainstormRequest(BaseModel):
    """Generate N plot ideas for what happens next."""

    context: str = Field(..., description="Last ~300 words of current scene")
    question: str = Field(
        "What could happen next?",
        description="Specific question for the brainstorm",
    )
    n: int = Field(5, ge=2, le=8)
    genre: str = Field("fiction", description="Genre hint for tone calibration")
    project_id: str = Field("", description="Project ID for RAG enrichment")
    scene_id: str = Field("", description="Current scene ID")


@router.post("/brainstorm")
async def generate_brainstorm(req: BrainstormRequest) -> StreamingResponse:
    """
    Stream 5 plot ideas formatted as a numbered list.

    Client accumulates the full SSE stream, then parses the numbered list
    into individual idea cards when [DONE] is received.

    Format the model is primed to output:
        1. [Short Title]: One sentence description of what happens.
        2. [Short Title]: ...
    """
    rag_block = ""
    if req.project_id:
        try:
            from .rag import build_rag_context
            rag_block = await build_rag_context(
                project_id=req.project_id,
                query_text=req.context[-300:],
                active_characters=[],
                current_scene_id=req.scene_id or None,
            )
        except Exception as exc:
            print(f"[Quill RAG] Brainstorm context error: {exc}")

    rag_section = f"STORY CONTEXT:\n{rag_block}\n\n" if rag_block else ""

    messages = [
        {
            "role": "system",
            "content": (
                f"You are a creative story consultant specialising in {req.genre}. "
                "Generate plot ideas that are distinct, specific, and dramatically interesting. "
                "Format EXACTLY as a numbered list — each idea on its own line:\n"
                "1. [Two-word title]: One vivid sentence describing what happens.\n"
                "No preamble, no explanation after the list. Just the numbered list."
            ),
        },
        {
            "role": "user",
            "content": (
                f"{rag_section}"
                f"RECENT SCENE:\n{req.context[-600:]}\n\n"
                f"Question: {req.question}\n"
                f"Generate {req.n} distinct ideas:"
            ),
        },
    ]
    return StreamingResponse(
        _stream_llama(messages, req.n * 40, temperature=0.88),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Phase 3: Describe (sensory expansion)
# ---------------------------------------------------------------------------


class DescribeRequest(BaseModel):
    """Expand a highlighted word/phrase into rich sensory detail."""

    text: str = Field(..., description="Selected text to expand (5-80 words ideal)")
    context: str = Field("", description="Surrounding paragraph for tone/voice reference")
    mode: str = Field(
        "sensory",
        description="'sensory' | 'action' | 'emotional' | 'setting'",
    )


@router.post("/describe")
async def generate_describe(req: DescribeRequest) -> StreamingResponse:
    """
    Expand selected text into a richer version using the requested mode.

    Modes:
      sensory   — sight, sound, smell, touch, taste
      action    — slow down and show the physical action beat-by-beat
      emotional — show the internal emotional state through physical tells
      setting   — expand into a vivid place description
    """
    mode_instructions = {
        "sensory": (
            "Expand the text into a multi-sensory paragraph. "
            "Include at least three different senses (sight, sound, smell, touch, taste). "
            "Keep one focused paragraph — no scene summary."
        ),
        "action": (
            "Slow down and rewrite the text as a beat-by-beat action sequence. "
            "Every physical movement described precisely. Short, punchy sentences."
        ),
        "emotional": (
            "Rewrite the text to show the character's internal emotional state "
            "entirely through physical tells, micro-expressions, and body language. "
            "No stating emotions directly — show them."
        ),
        "setting": (
            "Expand the text into a vivid setting description. "
            "Establish atmosphere, light, sound, and physical space. "
            "Ground the reader in this specific place."
        ),
    }
    instruction = mode_instructions.get(req.mode, mode_instructions["sensory"])

    context_block = (
        f"SURROUNDING PARAGRAPH (match this voice/tense exactly):\n{req.context}\n\n"
        if req.context.strip()
        else ""
    )

    target_len = max(60, min(len(req.text.split()) * 4, 200))

    messages = [
        {
            "role": "system",
            "content": (
                f"You are a prose stylist. {instruction} "
                "Output ONLY the expanded prose. No preamble, no explanation. "
                "Match the tense and POV of any provided context exactly."
            ),
        },
        {
            "role": "user",
            "content": (
                f"{context_block}"
                f"Expand this into ~{target_len} words:\n\"{req.text}\""
            ),
        },
    ]
    return StreamingResponse(
        _stream_llama(messages, target_len + 30, temperature=0.72),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
