/**
 * editor.js — Main ProseMirror editor initialisation.
 *
 * Sets up ProseMirror with:
 *   - basicSchema (paragraphs, text)
 *   - history plugin (undo/redo, completely separate from ghost text)
 *   - keymap (Enter, Shift-Enter, Ctrl-Z, Ctrl-Y, Tab handled here or in ghost)
 *   - Ghost Text plugin (sentence completion)
 *   - AI toolbar wiring (Continue, Self-Write, Rephrase)
 */

import { EditorState }                     from "prosemirror-state";
import { EditorView }                      from "prosemirror-view";
import { schema }                          from "prosemirror-schema-basic";
import { history, undo, redo }             from "prosemirror-history";
import { keymap }                          from "prosemirror-keymap";
import { baseKeymap, splitBlock }          from "prosemirror-commands";
import { createGhostPlugin, ghostKey }     from "./ghost.js";
import { streamIntoEditor }                from "./stream.js";
import { setProject as setBibleProject, refresh as refreshBible } from "./bible.js";
import { setProject as setConsistencyProject }                     from "./consistency.js";
import { toggle as toggleBrainstorm }                              from "./brainstorm.js";
import { triggerDescribe, dismiss as dismissDescribe }             from "./describe.js";

// ─── App State ───────────────────────────────────────────────────────────────

let activeProjectId = null;
let activeSceneId   = null;
let activeGenre     = "fiction";
let saveTimer       = null;
let aiAbort         = null;   // AbortController for toolbar AI actions

// ─── Initialise ProseMirror ──────────────────────────────────────────────────

const ghostPlugin = createGhostPlugin();

const view = new EditorView(document.querySelector("#editor"), {
  state: EditorState.create({
    schema,
    plugins: [
      history(),
      ghostPlugin,
      keymap({
        "Mod-z":       undo,
        "Mod-y":       redo,
        "Mod-Shift-z": redo,
        "Enter":       splitBlock,
      }),
      keymap(baseKeymap),
    ],
  }),

  dispatchTransaction(tr) {
    const newState = view.state.apply(tr);
    view.updateState(newState);

    // Auto-save 2s after last change
    if (tr.docChanged && activeSceneId && activeProjectId) {
      clearTimeout(saveTimer);
      saveTimer = setTimeout(saveScene, 2000);
      updateWordCount(newState);
    }
  },
});

view.focus();

// ─── Word count ──────────────────────────────────────────────────────────────

function updateWordCount(state) {
  const text  = state.doc.textContent;
  const words = text.trim() === "" ? 0 : text.trim().split(/\s+/).length;
  const el    = document.getElementById("word-count");
  if (el) el.textContent = `${words.toLocaleString()} words`;
  updateProgressBar(words);
}

// ─── Writing progress bar ──────────────────────────────────────────────────────────

let _wordGoal = 80_000;

export function setGoal(goal) {
  _wordGoal = goal;
  updateProgressBar();
}

function updateProgressBar(words) {
  // words may be called from word count or standalone
  const currentWords = words ?? 0;
  const pct   = Math.min(100, (currentWords / _wordGoal) * 100);
  const bar   = document.getElementById("progress-bar-fill");
  const label = document.getElementById("progress-label");
  if (bar)   bar.style.width = `${pct}%`;
  if (label) label.textContent = `${currentWords.toLocaleString()} / ${_wordGoal.toLocaleString()}`;
}

// ─── Auto-save + extraction trigger ─────────────────────────────────────────

async function saveScene() {
  if (!activeProjectId || !activeSceneId) return;
  const content = view.state.doc.textContent;
  await fetch(`/api/projects/${activeProjectId}/scenes/${activeSceneId}`, {
    method:  "PUT",
    headers: { "Content-Type": "application/json" },
    body:    JSON.stringify({ content }),
  });

  // Fire-and-forget background extraction (non-blocking)
  // This will update characters.json + world_rules.json + ChromaDB
  fetch(`/api/extract/scene_facts?project_id=${activeProjectId}`, {
    method:  "POST",
    headers: { "Content-Type": "application/json" },
    body:    JSON.stringify({ scene_id: activeSceneId, text: content }),
  }).then(() => {
    // Refresh Bible panel after extraction completes
    setTimeout(() => refreshBible(), 35_000);   // ~35s for BitNet 2B extraction
  }).catch(() => {});
}

// ─── Load scene into editor ──────────────────────────────────────────────────

export async function loadScene(projectId, sceneId, genre) {
  const resp = await fetch(`/api/projects/${projectId}/scenes/${sceneId}`);
  if (!resp.ok) return;
  const { content } = await resp.json();

  // Build new doc from text content
  const doc = schema.node("doc", null, [
    schema.node("paragraph", null,
      content.trim()
        ? [schema.text(content)]
        : []
    ),
  ]);

  view.dispatch(
    view.state.tr.replace(0, view.state.doc.content.size,
      doc.slice(0, doc.content.size)
    )
  );

  activeProjectId = projectId;
  activeSceneId   = sceneId;
  activeGenre     = genre || "fiction";
  updateWordCount(view.state);

  // Init Phase 2 panels for this project
  setBibleProject(projectId);
  setConsistencyProject(projectId);

  view.focus();
}

// ─── Cursor position helper ──────────────────────────────────────────────────

function cursorEnd() {
  return view.state.selection.from;
}

function lastWords(n = 300) {
  const text  = view.state.doc.textContent;
  const words = text.trim().split(/\s+/);
  return words.slice(-n).join(" ");
}

// ─── AI Status indicator ──────────────────────────────────────────────────────

function setStatus(msg, type = "info") {
  const el = document.getElementById("ai-status");
  if (!el) return;
  el.textContent = msg;
  el.className   = `ai-status ai-status--${type}`;
}

// ─── Cancel in-flight AI action ──────────────────────────────────────────────

function cancelAI() {
  if (aiAbort) {
    aiAbort.abort();
    aiAbort = null;
  }
}

// ─── AI Toolbar actions ───────────────────────────────────────────────────────

// ▶ Continue ----------------------------------------------------------------
document.getElementById("btn-continue")?.addEventListener("click", async () => {
  cancelAI();
  aiAbort = new AbortController();

  const from = cursorEnd();
  setStatus("Writing…", "generating");

  // Insert a paragraph break before AI text
  view.dispatch(view.state.tr.insertText("\n\n", from));
  const insertAt = from + 2;

  await streamIntoEditor(
    view, insertAt,
    "/api/generate/continue",
    {
      prefix:      lastWords(300),
      instruction: "Continue the story naturally.",
      // RAG enrichment
      project_id:  activeProjectId || "",
      scene_id:    activeSceneId   || "",
      characters:  [],   // Phase 3: extract from current text
    },
    aiAbort.signal,
    (s) => {
      if (s === "done")  setStatus("Done", "done");
      if (s === "error") setStatus("Connection error", "error");
    }
  );

  aiAbort = null;
});

// ✍ Self-Write dialog -------------------------------------------------------
document.getElementById("btn-self-write")?.addEventListener("click", () => {
  document.getElementById("self-write-modal")?.classList.remove("hidden");
});

document.getElementById("self-write-cancel")?.addEventListener("click", () => {
  document.getElementById("self-write-modal")?.classList.add("hidden");
});

document.getElementById("self-write-submit")?.addEventListener("click", async () => {
  const modal = document.getElementById("self-write-modal");
  const who   = document.getElementById("sw-who")?.value?.trim();
  const where = document.getElementById("sw-where")?.value?.trim();
  const what  = document.getElementById("sw-what")?.value?.trim();
  const tone  = document.getElementById("sw-tone")?.value?.trim() || "neutral";

  if (!who || !where || !what) {
    alert("Please fill in Who, Where, and What.");
    return;
  }

  modal?.classList.add("hidden");
  cancelAI();
  aiAbort = new AbortController();

  const from     = cursorEnd();
  const insertAt = from;
  setStatus("Self-writing scene…", "generating");

  await streamIntoEditor(
    view, insertAt,
    "/api/generate/self_write",
    {
      who, where, what, tone,
      target_words:  400,
      prior_context: lastWords(200),
      // RAG enrichment
      project_id:  activeProjectId || "",
      scene_id:    activeSceneId   || "",
    },
    aiAbort.signal,
    (s) => {
      if (s === "done")  setStatus("Scene written", "done");
      if (s === "error") setStatus("Connection error", "error");
    }
  );

  aiAbort = null;
});

// ↩ Rephrase ----------------------------------------------------------------
document.getElementById("btn-rephrase")?.addEventListener("click", async () => {
  const { from, to, empty } = view.state.selection;
  if (empty) {
    setStatus("Select text to rephrase", "info");
    return;
  }

  const selected = view.state.doc.textBetween(from, to, " ");
  cancelAI();
  aiAbort = new AbortController();
  setStatus("Rephrasing…", "generating");

  // Delete selected text, then stream rephrased version in its place
  view.dispatch(view.state.tr.deleteSelection());
  const insertAt = view.state.selection.from;

  await streamIntoEditor(
    view, insertAt,
    "/api/generate/rephrase",
    { text: selected, style: "same" },
    aiAbort.signal,
    (s) => {
      if (s === "done")  setStatus("Rephrased", "done");
      if (s === "error") setStatus("Error", "error");
    }
  );

  aiAbort = null;
});

// ─── Phase 3: Brainstorm ─────────────────────────────────────────────────────

document.getElementById("btn-brainstorm")?.addEventListener("click", () => {
  dismissDescribe();
  toggleBrainstorm(view, activeProjectId, activeSceneId, activeGenre);
});

// ─── Phase 3: Describe ───────────────────────────────────────────────────────

document.getElementById("btn-describe")?.addEventListener("click", () => {
  const { empty } = view.state.selection;
  if (empty) {
    setStatus("Select text first", "info");
    return;
  }
  triggerDescribe(view, "sensory");
});

// Ctrl+D = Describe
document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === "d") {
    const { empty } = view.state.selection;
    if (!empty) {
      e.preventDefault();
      triggerDescribe(view, "sensory");
    }
  }
});

// ─── Stop button ─────────────────────────────────────────────────────────────
document.getElementById("btn-stop")?.addEventListener("click", () => {
  cancelAI();
  setStatus("Stopped", "info");
});

// ─── Focus mode (F11) ────────────────────────────────────────────────────────
document.addEventListener("keydown", (e) => {
  if (e.key === "F11") {
    e.preventDefault();
    document.body.classList.toggle("focus-mode");
  }
});

// ─── Export loadScene for use by binder.js ───────────────────────────────────
export { view };
