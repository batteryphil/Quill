/**
 * describe.js — "Describe / Expand" inline popover.
 *
 * Activated when the writer has text selected and clicks
 * "🔍 Describe" in the toolbar (or presses Ctrl+D).
 *
 * Shows a floating popover near the selection with streaming expanded prose.
 * Four mode buttons: Sensory | Action | Emotional | Setting.
 * Accept → replaces selection. Dismiss → closes.
 */

import { streamTokens } from "./stream.js";

// ─── Active state ─────────────────────────────────────────────────────────────

let _popover      = null;
let _abortCtrl    = null;
let _view         = null;
let _selectionFrom = 0;
let _selectionTo   = 0;
let _selectionText = "";

// ─── Public: trigger ─────────────────────────────────────────────────────────

export function triggerDescribe(view, mode = "sensory") {
  const { from, to, empty } = view.state.selection;
  if (empty) return;

  _view          = view;
  _selectionFrom = from;
  _selectionTo   = to;
  _selectionText = view.state.doc.textBetween(from, to, " ");

  // Dismiss any existing popover
  dismiss();

  // Get surrounding context (up to 200 words around selection)
  const fullText = view.state.doc.textContent;
  const before   = fullText.slice(0, from);
  const after    = fullText.slice(to);
  const context  = before.slice(-400) + after.slice(0, 200);

  // Position near cursor end
  const coords = view.coordsAtPos(to);
  createPopover(coords, _selectionText, context, mode);
}

// ─── Popover DOM ─────────────────────────────────────────────────────────────

function createPopover(coords, selText, context, initialMode) {
  _popover = document.createElement("div");
  _popover.className = "describe-popover";
  _popover.innerHTML = `
    <div class="describe-header">
      <span class="describe-title">✨ Expand</span>
      <div class="describe-modes">
        <button class="describe-mode-btn ${initialMode === 'sensory'   ? 'active' : ''}" data-mode="sensory">Sensory</button>
        <button class="describe-mode-btn ${initialMode === 'action'    ? 'active' : ''}" data-mode="action">Action</button>
        <button class="describe-mode-btn ${initialMode === 'emotional' ? 'active' : ''}" data-mode="emotional">Emotional</button>
        <button class="describe-mode-btn ${initialMode === 'setting'   ? 'active' : ''}" data-mode="setting">Setting</button>
      </div>
      <button class="describe-close-btn" title="Dismiss">✕</button>
    </div>
    <div class="describe-original">"${selText.slice(0, 80)}${selText.length > 80 ? '…' : ''}"</div>
    <div class="describe-output"><span class="describe-spinner"></span> Generating…</div>
    <div class="describe-actions hidden">
      <button class="describe-accept-btn">✓ Replace selection</button>
      <button class="describe-copy-btn">⎘ Copy</button>
      <button class="describe-retry-btn">↺ Try again</button>
    </div>`;

  document.body.appendChild(_popover);

  // Position it just below the selection end
  const scrollTop  = window.scrollY;
  const editorRect = document.getElementById("editor-area")?.getBoundingClientRect() || {left: 0};
  let   left       = Math.max(editorRect.left + 8, coords.left - 20);
  let   top        = coords.bottom + scrollTop + 8;

  // Keep within viewport
  const pw = 420;
  if (left + pw > window.innerWidth - 16) left = window.innerWidth - pw - 16;
  _popover.style.left  = `${left}px`;
  _popover.style.top   = `${top}px`;
  _popover.style.width = `${pw}px`;

  // ── Mode switching ─────────────────────────────────────────────────────
  _popover.querySelectorAll(".describe-mode-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      _popover?.querySelectorAll(".describe-mode-btn").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      resetOutput();
      streamExpansion(selText, context, btn.dataset.mode);
    });
  });

  // ── Accept ────────────────────────────────────────────────────────────
  _popover.querySelector(".describe-accept-btn").addEventListener("click", () => {
    const expanded = _popover.querySelector(".describe-output")?.textContent?.trim();
    if (expanded && _view) {
      _view.dispatch(
        _view.state.tr.replaceWith(
          _selectionFrom, _selectionTo,
          _view.state.schema.text(expanded)
        )
      );
    }
    dismiss();
  });

  // ── Copy ──────────────────────────────────────────────────────────────
  _popover.querySelector(".describe-copy-btn").addEventListener("click", () => {
    const expanded = _popover.querySelector(".describe-output")?.textContent?.trim();
    if (expanded) navigator.clipboard.writeText(expanded);
  });

  // ── Retry ─────────────────────────────────────────────────────────────
  _popover.querySelector(".describe-retry-btn").addEventListener("click", () => {
    const activeMode = _popover.querySelector(".describe-mode-btn.active")?.dataset.mode || "sensory";
    resetOutput();
    streamExpansion(selText, context, activeMode);
  });

  // ── Close ─────────────────────────────────────────────────────────────
  _popover.querySelector(".describe-close-btn").addEventListener("click", dismiss);

  // ── Click outside ─────────────────────────────────────────────────────
  setTimeout(() => {
    document.addEventListener("click", _outsideClick, { once: false });
  }, 100);

  // Start streaming
  streamExpansion(selText, context, initialMode);
}

function _outsideClick(e) {
  if (_popover && !_popover.contains(e.target)) {
    dismiss();
  }
}

function resetOutput() {
  if (!_popover) return;
  _abortCtrl?.abort();
  _abortCtrl = null;
  const out     = _popover.querySelector(".describe-output");
  const actions = _popover.querySelector(".describe-actions");
  out.innerHTML    = `<span class="describe-spinner"></span> Generating…`;
  actions?.classList.add("hidden");
}

// ─── SSE expansion ───────────────────────────────────────────────────────────

async function streamExpansion(text, context, mode) {
  _abortCtrl = new AbortController();
  const outputEl  = _popover?.querySelector(".describe-output");
  const actionsEl = _popover?.querySelector(".describe-actions");
  if (!outputEl) return;

  outputEl.textContent = "";
  let full = "";

  await streamTokens({
    url:    "/api/generate/describe",
    body:   { text, context, mode },
    signal: _abortCtrl.signal,
    onToken(token) {
      full += token;
      outputEl.textContent = full;
    },
    onDone() {
      actionsEl?.classList.remove("hidden");
    },
    onError(err) {
      outputEl.textContent = `⚠ ${err.message}`;
    },
  });
}

// ─── Dismiss ─────────────────────────────────────────────────────────────────

export function dismiss() {
  _abortCtrl?.abort();
  _abortCtrl = null;
  _popover?.remove();
  _popover = null;
  document.removeEventListener("click", _outsideClick);
}
