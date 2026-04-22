/**
 * export.js — Project export modal.
 *
 * Checks pandoc availability on open, then lets the writer configure
 * and download the manuscript as Markdown, EPUB, PDF, or DOCX.
 *
 * The export pipeline:
 *   POST /api/export/{project_id}  →  binary blob  →  download via <a> click
 */

// ─── State ───────────────────────────────────────────────────────────────────

let _pandocAvailable = false;
let _projectId       = null;
let _projectTitle    = "";

// ─── Boot: check pandoc availability ─────────────────────────────────────────

(async function checkPandoc() {
  try {
    const resp = await fetch("/api/export/check");
    if (resp.ok) {
      const info = await resp.json();
      _pandocAvailable = info.available;
    }
  } catch { /* offline */ }
})();

// ─── Public API ──────────────────────────────────────────────────────────────

export function openExportModal(projectId, projectTitle) {
  _projectId    = projectId;
  _projectTitle = projectTitle || "Manuscript";
  renderModal();
}

// ─── Modal rendering ─────────────────────────────────────────────────────────

function renderModal() {
  // Dismiss existing
  document.getElementById("export-modal-overlay")?.remove();

  const overlay = document.createElement("div");
  overlay.id        = "export-modal-overlay";
  overlay.className = "modal-overlay";

  const pandocNote = _pandocAvailable
    ? `<div class="export-pandoc-ok">✓ pandoc available — all formats enabled</div>`
    : `<div class="export-pandoc-warn">
        ⚠ pandoc not found — only Markdown available<br>
        <small>Install: <code>sudo apt install pandoc</code></small>
       </div>`;

  overlay.innerHTML = `
    <div class="modal-card" id="export-modal-card" style="width:440px">
      <h2>📤 Export Manuscript</h2>

      ${pandocNote}

      <div class="form-field">
        <label for="export-author">Author name</label>
        <input id="export-author" type="text"
               placeholder="e.g. Jane Doe" autocomplete="name">
      </div>

      <div class="export-options">
        <label class="export-toggle">
          <input id="export-scene-headers" type="checkbox" checked>
          <span>Include scene title headers</span>
        </label>
        <label class="export-toggle">
          <input id="export-strip-notes" type="checkbox" checked>
          <span>Strip [Idea: …] brainstorm notes</span>
        </label>
        <label class="export-toggle">
          <input id="export-toc" type="checkbox" checked>
          <span>Include table of contents (EPUB/DOCX)</span>
        </label>
      </div>

      <div class="export-formats" role="group" aria-label="Export format">
        <button class="export-fmt-btn active" data-format="markdown">
          <span class="export-fmt-icon">📄</span>
          <span class="export-fmt-label">Markdown</span>
          <span class="export-fmt-sub">.md</span>
        </button>
        <button class="export-fmt-btn ${_pandocAvailable ? '' : 'disabled'}"
                data-format="epub" title="${_pandocAvailable ? '' : 'Requires pandoc'}">
          <span class="export-fmt-icon">📚</span>
          <span class="export-fmt-label">EPUB</span>
          <span class="export-fmt-sub">.epub</span>
        </button>
        <button class="export-fmt-btn ${_pandocAvailable ? '' : 'disabled'}"
                data-format="pdf" title="${_pandocAvailable ? '' : 'Requires pandoc'}">
          <span class="export-fmt-icon">🖨</span>
          <span class="export-fmt-label">PDF</span>
          <span class="export-fmt-sub">.pdf</span>
        </button>
        <button class="export-fmt-btn ${_pandocAvailable ? '' : 'disabled'}"
                data-format="docx" title="${_pandocAvailable ? '' : 'Requires pandoc'}">
          <span class="export-fmt-icon">📝</span>
          <span class="export-fmt-label">Word</span>
          <span class="export-fmt-sub">.docx</span>
        </button>
      </div>

      <div id="export-status" class="export-status"></div>

      <div class="modal-actions">
        <button class="btn-cancel" id="export-cancel">Cancel</button>
        <button class="btn-submit" id="export-go">⬇ Export</button>
      </div>
    </div>`;

  document.body.appendChild(overlay);

  // ── Format selection ────────────────────────────────────────────────────
  overlay.querySelectorAll(".export-fmt-btn").forEach(btn => {
    if (btn.classList.contains("disabled")) return;
    btn.addEventListener("click", () => {
      overlay.querySelectorAll(".export-fmt-btn").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
    });
  });

  // ── Dismiss ─────────────────────────────────────────────────────────────
  overlay.querySelector("#export-cancel").addEventListener("click", () => overlay.remove());
  overlay.addEventListener("click", e => { if (e.target === overlay) overlay.remove(); });

  // ── Export button ───────────────────────────────────────────────────────
  overlay.querySelector("#export-go").addEventListener("click", () => runExport(overlay));
}

// ─── Export execution ─────────────────────────────────────────────────────────

async function runExport(overlay) {
  if (!_projectId) return;

  const format  = overlay.querySelector(".export-fmt-btn.active")?.dataset.format || "markdown";
  const author  = overlay.querySelector("#export-author").value.trim();
  const sceneH  = overlay.querySelector("#export-scene-headers").checked;
  const stripN  = overlay.querySelector("#export-strip-notes").checked;
  const toc     = overlay.querySelector("#export-toc").checked;

  const statusEl = overlay.querySelector("#export-status");
  const goBtn    = overlay.querySelector("#export-go");

  setStatus(statusEl, "⏳ Compiling manuscript…", "loading");
  goBtn.disabled = true;

  try {
    const resp = await fetch(`/api/export/${_projectId}`, {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify({
        format,
        author,
        include_scene_headers: sceneH,
        strip_notes:           stripN,
        toc,
      }),
    });

    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ detail: resp.statusText }));
      setStatus(statusEl, `⚠ ${err.detail || "Export failed"}`, "error");
      goBtn.disabled = false;
      return;
    }

    setStatus(statusEl, "✓ Downloading…", "ok");

    // Extract filename from Content-Disposition header
    const cd   = resp.headers.get("Content-Disposition") || "";
    const match = /filename="([^"]+)"/.exec(cd);
    const filename = match ? match[1] : `${_projectTitle}.${format}`;

    // Trigger browser download
    const blob = await resp.blob();
    const url  = URL.createObjectURL(blob);
    const a    = document.createElement("a");
    a.href     = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    URL.revokeObjectURL(url);
    a.remove();

    setTimeout(() => overlay.remove(), 800);

  } catch (err) {
    setStatus(statusEl, `⚠ ${err.message}`, "error");
    goBtn.disabled = false;
  }
}

function setStatus(el, msg, type) {
  if (!el) return;
  el.textContent  = msg;
  el.className    = `export-status export-status--${type}`;
}
