/**
 * ghost.js — ProseMirror Ghost Text Plugin
 *
 * Manages inline sentence completion using ProseMirror's Decoration API.
 * The ghost text is a VIEW-LAYER decoration only — it never touches the
 * document model, so the undo/redo stack (history plugin) is unaffected.
 *
 * Flow:
 *   1. Writer pauses for DEBOUNCE_MS (1000ms of absolute keyboard silence)
 *   2. Last CONTEXT_WORDS words sent to /api/generate/complete
 *   3. Tokens stream via SSE → appended to a Decoration.widget at cursor pos
 *   4. Tab → accept (insert as real text) | Escape → dismiss | Alt+] → retry
 */

import { Plugin, PluginKey } from "prosemirror-state";
import { Decoration, DecorationSet } from "prosemirror-view";

// ─── Constants ───────────────────────────────────────────────────────────────

const DEBOUNCE_MS    = 1000;   // hard keyboard-silence threshold
const CONTEXT_WORDS  = 150;    // words of prefix sent to LLM
const MAX_TOKENS     = 25;     // max tokens to generate

// ─── Plugin key (used to get plugin state from outside) ──────────────────────

export const ghostKey = new PluginKey("ghost");

// ─── Internal state shape ────────────────────────────────────────────────────
// {
//   suggestion : string   — accumulated tokens so far
//   ghostPos   : number   — cursor position when ghost was triggered (-1 = off)
//   active     : boolean  — decoration visible?
//   streaming  : boolean  — SSE stream still open?
// }

const IDLE_STATE = { suggestion: "", ghostPos: -1, active: false, streaming: false };

// ─── Plugin factory ──────────────────────────────────────────────────────────

export function createGhostPlugin() {
  let debounceTimer   = null;
  let abortController = null;   // cancels the in-flight SSE fetch

  // ── helpers (defined as functions so they are hoisted) ──────────────────

  function cancel() {
    clearTimeout(debounceTimer);
    debounceTimer = null;
    if (abortController) {
      abortController.abort();
      abortController = null;
    }
  }

  function clearGhost(view) {
    view.dispatch(
      view.state.tr.setMeta(ghostKey, { type: "CLEAR" })
    );
  }

  function acceptGhost(view) {
    const { suggestion, ghostPos } = ghostKey.getState(view.state);
    if (!suggestion || ghostPos < 0) return;
    // Insert the suggestion as real document text, then clear ghost metadata
    view.dispatch(
      view.state.tr
        .insertText(suggestion, ghostPos)
        .setMeta(ghostKey, { type: "CLEAR" })
    );
  }

  async function triggerCompletion(view) {
    const state = view.state;
    const { from } = state.selection;

    // Extract prefix: last CONTEXT_WORDS words before cursor
    const fullText  = state.doc.textContent;
    const before    = fullText.slice(0, from).trimStart();
    const words     = before.split(/\s+/);
    const prefix    = words.slice(-CONTEXT_WORDS).join(" ");

    if (prefix.trim().length < 12) return;   // too short to be useful

    // Mark ghost as active at the current cursor pos
    view.dispatch(
      view.state.tr.setMeta(ghostKey, {
        type: "SET",
        payload: { suggestion: "", ghostPos: from, active: true, streaming: true },
      })
    );

    abortController = new AbortController();
    const needsSpace = before.length > 0 && !/\\s$/.test(before);

    try {
      const resp = await fetch("/api/generate/complete", {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify({ prefix, max_tokens: MAX_TOKENS }),
        signal:  abortController.signal,
      });

      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);

      const reader  = resp.body.getReader();
      const decoder = new TextDecoder();

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;

        const chunk = decoder.decode(value, { stream: true });
        for (const line of chunk.split("\n")) {
          if (!line.startsWith("data: ")) continue;
          const payload = line.slice(6).trim();
          if (payload === "[DONE]") break;
          try {
            let token = JSON.parse(payload);
            if (typeof token === "string" && token.length > 0) {
              const currentGhostObj = ghostKey.getState(view.state);
              if (needsSpace && currentGhostObj.suggestion.length === 0 && !token.startsWith(' ') && !token.startsWith('\\n')) {
                  token = ' ' + token;
              }
              // Append token to ghost suggestion via plugin meta
              view.dispatch(
                view.state.tr.setMeta(ghostKey, { type: "ADD_TOKEN", token })
              );
            }
          } catch { /* ignore parse errors */ }
        }
      }

      // Stream finished — mark done
      view.dispatch(
        view.state.tr.setMeta(ghostKey, { type: "STREAM_DONE" })
      );
    } catch (err) {
      if (err.name !== "AbortError") {
        console.warn("[Quill] Ghost text error:", err.message);
      }
      clearGhost(view);
    } finally {
      abortController = null;
    }
  }

  // ── Plugin definition ────────────────────────────────────────────────────

  return new Plugin({
    key: ghostKey,

    // ── State machine ──────────────────────────────────────────────────────
    state: {
      init() { return IDLE_STATE; },

      apply(tr, prev) {
        const meta = tr.getMeta(ghostKey);

        if (meta) {
          switch (meta.type) {
            case "SET":       return { ...prev, ...meta.payload };
            case "ADD_TOKEN": return { ...prev, suggestion: prev.suggestion + meta.token };
            case "STREAM_DONE": return { ...prev, streaming: false };
            case "CLEAR":     return IDLE_STATE;
          }
        }

        // If the writer typed while ghost is active → clear it
        if (tr.docChanged && prev.active) {
          return IDLE_STATE;
        }

        return prev;
      },
    },

    // ── Decorations (the visible ghost text) ──────────────────────────────
    props: {
      decorations(state) {
        const ghost = ghostKey.getState(state);
        if (!ghost.active || ghost.ghostPos < 0) return DecorationSet.empty;

        // Build the ghost text content shown inline
        const displayText = ghost.suggestion || (ghost.streaming ? "▍" : "");
        if (!displayText) return DecorationSet.empty;

        const widget = Decoration.widget(
          ghost.ghostPos,
          () => {
            const span       = document.createElement("span");
            span.className   = ghost.streaming && !ghost.suggestion
              ? "ghost-cursor"
              : "ghost-text";
            span.textContent = displayText;
            return span;
          },
          {
            side: 1,
            key:  "ghost-" + displayText.length,   // forces update on each token
          }
        );

        return DecorationSet.create(state.doc, [widget]);
      },

      // ── Keyboard handling ────────────────────────────────────────────────
      handleKeyDown(view, event) {
        const ghost = ghostKey.getState(view.state);

        // Tab — accept ghost
        if (event.key === "Tab" && ghost.active && ghost.suggestion) {
          event.preventDefault();
          cancel();
          acceptGhost(view);
          return true;
        }

        // Escape — dismiss ghost
        if (event.key === "Escape" && ghost.active) {
          event.preventDefault();
          cancel();
          clearGhost(view);
          return true;
        }

        // Alt+] — new suggestion
        if (event.key === "]" && event.altKey) {
          event.preventDefault();
          cancel();
          clearGhost(view);
          triggerCompletion(view);
          return true;
        }

        return false;
      },
    },

    // ── View lifecycle: reset debounce on every doc change ────────────────
    view(editorView) {
      return {
        update(view, prevState) {
          if (view.state.doc !== prevState.doc) {
            // Document changed → cancel anything in flight
            cancel();

            // Restart debounce timer
            debounceTimer = setTimeout(() => {
              debounceTimer = null;
              triggerCompletion(view);
            }, DEBOUNCE_MS);
          }
        },
        destroy() {
          cancel();
        },
      };
    },
  });
}
