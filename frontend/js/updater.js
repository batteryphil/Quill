/**
 * updater.js — Quill self-update UI.
 *
 * On load: checks /api/update/check (throttled to once/hour via localStorage).
 * If an update is available: shows an animated badge on the ↑ update button.
 * Clicking: opens a modal with the changelog and an "Update & Restart" button.
 * Applying: streams update progress (SSE), then polls until server is back.
 */

// ─── Constants ────────────────────────────────────────────────────────────────

const CHECK_INTERVAL_MS  = 60 * 60 * 1000; // 1 hour
const CACHE_KEY          = "quill_update_cache";
const RECONNECT_DELAY_MS = 2_500;
const MAX_RECONNECT_MS   = 30_000;

// ─── State ────────────────────────────────────────────────────────────────────

let _updateInfo = null;   // Last /check response
let _overlay    = null;

// ─── Initialise ───────────────────────────────────────────────────────────────

export async function initUpdater() {
  // Check cache first; only hit GitHub if stale
  const cached = _loadCache();
  if (cached) {
    _updateInfo = cached;
    if (cached.has_update) _showBadge();
    return;
  }

  await _checkForUpdate(true);
}

// ─── Badge ────────────────────────────────────────────────────────────────────

function _showBadge() {
  const btn = document.getElementById("btn-update");
  if (!btn) return;
  btn.classList.add("update-available");
  btn.title = `Update available`;
}

function _hideBadge() {
  const btn = document.getElementById("btn-update");
  if (!btn) return;
  btn.classList.remove("update-available");
  btn.title = "Check for updates";
}

// ─── Check ────────────────────────────────────────────────────────────────────

async function _checkForUpdate(saveCache = true) {
  try {
    const resp = await fetch("/api/update/check");
    if (!resp.ok) return null;
    _updateInfo = await resp.json();
    if (saveCache) _saveCache(_updateInfo);
    if (_updateInfo.has_update) _showBadge();
    else _hideBadge();
    return _updateInfo;
  } catch {
    return null;
  }
}

// ─── Modal ────────────────────────────────────────────────────────────────────

export async function openUpdateModal() {
  // Always re-check when user clicks the button (bypass 1-hour cache)
  _clearCache();
  const info = await _checkForUpdate(false);
  if (!info) {
    _showSimpleModal("⚠ Could not reach update server.", false);
    return;
  }
  _overlay?.remove();
  _overlay = _buildModal(info);
  document.body.appendChild(_overlay);
  _wireModal(_overlay, info);
}

function _buildModal(info) {
  const overlay = document.createElement("div");
  overlay.id        = "update-overlay";
  overlay.className = "modal-overlay";

  const statusLine = info.error
    ? `<div class="update-error-banner">${info.error}</div>`
    : info.has_update
      ? `<div class="update-available-banner">
           🟢 Update available — <strong>${info.commits.length}</strong> new commit${info.commits.length !== 1 ? "s" : ""}
           <span class="update-sha">${info.local_sha} → ${info.remote_sha}</span>
         </div>`
      : `<div class="update-ok-banner">✓ Quill is up to date <span class="update-sha">${info.local_sha}</span></div>`;

  const commitList = info.commits?.length
    ? `<div class="update-commits">
         <div class="update-commits-label">What's new:</div>
         ${info.commits.map(c => `
           <div class="update-commit-row">
             <span class="update-commit-sha">${c.sha}</span>
             <span class="update-commit-msg">${_escape(c.message)}</span>
             <span class="update-commit-date">${_fmtDate(c.date)}</span>
           </div>`).join("")}
       </div>`
    : "";

  overlay.innerHTML = `
    <div class="modal-card update-card" id="update-card">
      <div class="settings-header">
        <h2>⬆ Quill Updates</h2>
        <button class="settings-close" id="update-close-btn">✕</button>
      </div>

      ${statusLine}
      ${commitList}

      <div id="update-progress" class="update-progress hidden"></div>

      <div class="modal-actions">
        <button class="btn-secondary" id="update-check-btn">⟳ Check Again</button>
        <div style="flex:1"></div>
        <button class="btn-cancel"  id="update-dismiss-btn">Close</button>
        ${info.has_update
          ? `<button class="btn-submit update-apply-btn" id="update-apply-btn">⬆ Update &amp; Restart</button>`
          : ``}
      </div>
    </div>`;

  return overlay;
}

function _wireModal(overlay, info) {
  overlay.querySelector("#update-close-btn").addEventListener("click",   () => overlay.remove());
  overlay.querySelector("#update-dismiss-btn").addEventListener("click", () => overlay.remove());
  overlay.addEventListener("click", e => { if (e.target === overlay) overlay.remove(); });

  overlay.querySelector("#update-check-btn").addEventListener("click", async () => {
    _clearCache();
    const fresh = await _checkForUpdate(false);
    if (fresh) {
      overlay.remove();
      _overlay = _buildModal(fresh);
      document.body.appendChild(_overlay);
      _wireModal(_overlay, fresh);
    }
  });

  overlay.querySelector("#update-apply-btn")?.addEventListener("click", () => {
    _runUpdate(overlay);
  });
}

// ─── Apply update (SSE stream) ────────────────────────────────────────────────

async function _runUpdate(overlay) {
  const applyBtn   = overlay.querySelector("#update-apply-btn");
  const progressEl = overlay.querySelector("#update-progress");
  const card       = overlay.querySelector("#update-card");

  if (applyBtn) applyBtn.disabled = true;
  overlay.querySelector("#update-check-btn").disabled = true;
  overlay.querySelector("#update-dismiss-btn").disabled = true;

  progressEl.classList.remove("hidden");
  progressEl.innerHTML = "";

  function appendLine(step, text) {
    if (!text?.trim()) return;
    const div       = document.createElement("div");
    div.className   = `update-line update-line--${step}`;
    div.textContent = text;
    progressEl.appendChild(div);
    progressEl.scrollTop = progressEl.scrollHeight;
  }

  try {
    const resp = await fetch("/api/update/apply", { method: "POST" });
    if (!resp.ok) {
      appendLine("error", `✗ Server returned ${resp.status}`);
      _reenableModal(overlay);
      return;
    }

    const reader  = resp.body.getReader();
    const decoder = new TextDecoder();
    let   buf     = "";

    // eslint-disable-next-line no-constant-condition
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buf += decoder.decode(value, { stream: true });
      const parts = buf.split("\n\n");
      buf = parts.pop();   // keep incomplete last part

      for (const part of parts) {
        const line = part.trim();
        if (!line.startsWith("data:")) continue;
        const raw = line.slice(5).trim();

        if (raw === "RESTART") {
          appendLine("done", "🔄 Server restarting — reconnecting…");
          _clearCache();
          _waitForReconnect(overlay, progressEl);
          return;
        }
        if (raw === "DONE") break;

        try {
          const { step, line: msg } = JSON.parse(raw);
          appendLine(step, msg);
        } catch { /* non-JSON SSE line */ }
      }
    }
  } catch (err) {
    appendLine("error", `✗ ${err.message}`);
    _reenableModal(overlay);
  }
}

function _reenableModal(overlay) {
  overlay.querySelector("#update-apply-btn") && (overlay.querySelector("#update-apply-btn").disabled = false);
  overlay.querySelector("#update-check-btn").disabled = false;
  overlay.querySelector("#update-dismiss-btn").disabled = false;
}

// ─── Reconnect polling ────────────────────────────────────────────────────────

async function _waitForReconnect(overlay, progressEl) {
  const start = Date.now();

  function appendLine(text) {
    const div = document.createElement("div");
    div.className   = "update-line update-line--done";
    div.textContent = text;
    progressEl.appendChild(div);
    progressEl.scrollTop = progressEl.scrollHeight;
  }

  // Poll /api/update/status until the server responds
  let attempt = 0;
  while (Date.now() - start < MAX_RECONNECT_MS) {
    await _sleep(RECONNECT_DELAY_MS + attempt * 500);
    attempt++;
    try {
      const r = await fetch("/api/update/status");
      if (r.ok) {
        const info = await r.json();
        appendLine(`✓ Connected — now at ${info.short_sha}`);
        _hideBadge();

        // Close modal after brief pause
        setTimeout(() => overlay.remove(), 1500);
        return;
      }
    } catch { /* server still coming back */ }
  }

  appendLine("⚠ Server took too long to restart — please refresh the page.");
}

// ─── Simple (no update) modal ─────────────────────────────────────────────────

function _showSimpleModal(msg, isError) {
  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";
  overlay.innerHTML = `
    <div class="modal-card" style="width:360px;text-align:center">
      <p style="font-size:0.9rem;color:${isError ? '#991b1b' : '#374151'};margin:8px 0 20px">${msg}</p>
      <button class="btn-submit" id="simple-ok">OK</button>
    </div>`;
  overlay.querySelector("#simple-ok").addEventListener("click", () => overlay.remove());
  overlay.addEventListener("click", e => { if (e.target === overlay) overlay.remove(); });
  document.body.appendChild(overlay);
}

// ─── Cache helpers ────────────────────────────────────────────────────────────

function _saveCache(data) {
  try {
    localStorage.setItem(CACHE_KEY, JSON.stringify({ data, ts: Date.now() }));
  } catch { /* storage full */ }
}

function _loadCache() {
  try {
    const raw = localStorage.getItem(CACHE_KEY);
    if (!raw) return null;
    const { data, ts } = JSON.parse(raw);
    if (Date.now() - ts > CHECK_INTERVAL_MS) return null;
    return data;
  } catch { return null; }
}

function _clearCache() {
  try { localStorage.removeItem(CACHE_KEY); } catch { /* */ }
}

// ─── Utils ────────────────────────────────────────────────────────────────────

function _escape(str) {
  return str
    .replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function _fmtDate(iso) {
  try {
    const d = new Date(iso);
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  } catch { return ""; }
}

function _sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}
