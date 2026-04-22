/**
 * brainstorm.js — "What happens next?" idea panel.
 *
 * Triggered by the 💡 toolbar button.
 * Streams 5 ideas, accumulates the full SSE response, then parses
 * the numbered list into idea cards on [DONE].
 *
 * Each card has a "Use this →" button that inserts it as a block
 * comment at the cursor position (so the writer can develop from it).
 */

import { streamTokens } from "./stream.js";

// ─── State ───────────────────────────────────────────────────────────────────

let abortController = null;
let isOpen          = false;

// ─── DOM refs (resolved lazily) ──────────────────────────────────────────────

const panel    = () => document.getElementById("brainstorm-panel");
const listEl   = () => document.getElementById("brainstorm-list");
const questionEl = () => document.getElementById("brainstorm-question");

// ─── Toggle ───────────────────────────────────────────────────────────────────

export function toggle(view, projectId, sceneId, genre) {
  if (isOpen) {
    close();
  } else {
    open(view, projectId, sceneId, genre);
  }
}

function close() {
  panel()?.classList.add("hidden");
  isOpen = false;
  abortController?.abort();
  abortController = null;
}

async function open(view, projectId, sceneId, genre) {
  panel()?.classList.remove("hidden");
  isOpen = true;
  await run(view, projectId, sceneId, genre);
}

// ─── Run generation ───────────────────────────────────────────────────────────

export async function run(view, projectId, sceneId, genre = "fiction") {
  const list  = listEl();
  const q     = questionEl()?.value?.trim() || "What could happen next?";
  if (!list) return;

  // Show loading state
  list.innerHTML = `<div class="brainstorm-loading">
    <span class="bs-spinner"></span>
    <span>Thinking of ideas…</span>
  </div>`;

  // Get last 300 words as context
  const text    = view.state.doc.textContent;
  const words   = text.trim().split(/\s+/);
  const context = words.slice(-300).join(" ");

  if (abortController) { abortController.abort(); }
  abortController = new AbortController();

  let accumulated = "";

  await streamTokens({
    url:    "/api/generate/brainstorm",
    body:   {
      context,
      question:   q,
      n:          5,
      genre,
      project_id: projectId || "",
      scene_id:   sceneId   || "",
    },
    signal: abortController.signal,
    onToken(token) {
      accumulated += token;
      // Live preview — show raw text while streaming
      list.innerHTML = `<pre class="brainstorm-raw">${accumulated}</pre>`;
    },
    onDone() {
      renderIdeas(list, accumulated, view);
    },
    onError(err) {
      list.innerHTML = `<div class="brainstorm-error">⚠ ${err.message}</div>`;
    },
  });
}

// ─── Parse + render idea cards ────────────────────────────────────────────────

function renderIdeas(container, rawText, view) {
  // Parse lines matching: "1. [Title]: Description"
  const lines = rawText.split("\n").filter(l => /^\d+\./.test(l.trim()));

  if (!lines.length) {
    container.innerHTML = `<div class="brainstorm-error">Model produced no parseable ideas. Try again.</div>`;
    return;
  }

  container.innerHTML = "";
  lines.forEach((line, i) => {
    // Parse "1. [Title]: Description" or "1. Title: Description"
    const match = line.match(/^\d+\.\s*(?:\[?([^\]:]+)\]?):\s*(.+)/);
    const title = match ? match[1].trim() : `Idea ${i + 1}`;
    const desc  = match ? match[2].trim() : line.replace(/^\d+\.\s*/, "").trim();

    const card = document.createElement("div");
    card.className = "brainstorm-card";
    card.innerHTML = `
      <div class="bs-card-title">${title}</div>
      <div class="bs-card-desc">${desc}</div>
      <button class="bs-use-btn" data-title="${title}" data-desc="${desc}">
        → Use this
      </button>`;

    card.querySelector(".bs-use-btn").addEventListener("click", () => {
      insertIdea(view, title, desc);
      highlightCard(card);
    });

    container.appendChild(card);
  });
}

function insertIdea(view, title, desc) {
  // Insert as an italic [Note: ...] block at current cursor
  const pos  = view.state.selection.from;
  const note = `\n\n[Idea: ${title} — ${desc}]\n\n`;
  view.dispatch(view.state.tr.insertText(note, pos));
  view.focus();
}

function highlightCard(card) {
  document.querySelectorAll(".brainstorm-card").forEach(c => c.classList.remove("bs-used"));
  card.classList.add("bs-used");
}

// ─── Keyboard shortcut: Escape closes ────────────────────────────────────────

document.addEventListener("keydown", e => {
  if (e.key === "Escape" && isOpen) close();
});

// ─── Export close for external use ───────────────────────────────────────────
export { close as closeBrainstorm };
