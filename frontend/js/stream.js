/**
 * stream.js — Reusable SSE streaming utility.
 *
 * Handles reading from a fetch() response body and yielding tokens
 * via callbacks. Abstracts the ReadableStream / TextDecoder boilerplate.
 */

/**
 * Stream tokens from an SSE endpoint into the ProseMirror editor.
 *
 * @param {Object} opts
 * @param {string}   opts.url          — endpoint URL
 * @param {Object}   opts.body         — JSON request body
 * @param {Function} opts.onToken      — called with each string token
 * @param {Function} [opts.onDone]     — called when stream ends
 * @param {Function} [opts.onError]    — called on error with Error object
 * @param {AbortSignal} [opts.signal]  — abort signal for cancellation
 */
export async function streamTokens({ url, body, onToken, onDone, onError, signal }) {
  try {
    const resp = await fetch(url, {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify(body),
      signal,
    });

    if (!resp.ok) {
      throw new Error(`Server returned ${resp.status} ${resp.statusText}`);
    }

    const reader  = resp.body.getReader();
    const decoder = new TextDecoder();
    let   buffer  = "";

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });

      // Process complete SSE lines from buffer
      const lines = buffer.split("\n");
      buffer = lines.pop();   // keep incomplete last line in buffer

      for (const line of lines) {
        if (!line.startsWith("data: ")) continue;
        const payload = line.slice(6).trim();
        if (payload === "[DONE]") {
          onDone?.();
          return;
        }
        try {
          const token = JSON.parse(payload);
          if (typeof token === "string" && token.length > 0) {
            onToken(token);
          }
        } catch { /* ignore malformed SSE lines */ }
      }
    }

    onDone?.();
  } catch (err) {
    if (err.name !== "AbortError") {
      onError?.(err);
    }
  }
}

/**
 * Insert streamed tokens into ProseMirror at the given position.
 * Returns the total text inserted.
 *
 * @param {EditorView} view
 * @param {number}     insertAt   — document position to start inserting
 * @param {string}     url
 * @param {Object}     body
 * @param {AbortSignal} [signal]
 * @param {Function}   [onStatus] — called with status string updates
 * @returns {Promise<string>} full text that was inserted
 */
export async function streamIntoEditor(view, insertAt, url, body, signal, onStatus) {
  let offset = 0;   // tracks how much text we've inserted so insertAt stays correct

  onStatus?.("generating");

  await streamTokens({
    url,
    body,
    signal,
    onToken(token) {
      const pos = insertAt + offset;
      view.dispatch(view.state.tr.insertText(token, pos));
      offset += token.length;
    },
    onDone() {
      onStatus?.("done");
    },
    onError(err) {
      console.error("[Quill] Stream error:", err);
      onStatus?.("error");
    },
  });

  return offset;   // number of chars inserted
}
