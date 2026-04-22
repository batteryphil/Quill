/**
 * settings.js — LLM provider configuration modal.
 *
 * Fetches provider registry from /api/settings/providers,
 * populates a UI for selecting + configuring the active backend,
 * and saves via PUT /api/settings with live test-connection feedback.
 */

// ─── State ────────────────────────────────────────────────────────────────────

let _registry  = {};   // PROVIDER_REGISTRY from server
let _current   = {};   // Current saved config (keys masked)
let _overlay   = null; // Active modal DOM node

// ─── Public ───────────────────────────────────────────────────────────────────

export async function openSettingsModal() {
  // Fetch registry + current config in parallel
  try {
    [_registry, _current] = await Promise.all([
      fetch("/api/settings/providers").then(r => r.json()),
      fetch("/api/settings").then(r => r.json()),
    ]);
  } catch (err) {
    alert("Could not load settings: " + err.message);
    return;
  }

  _overlay?.remove();
  _overlay = buildModal();
  document.body.appendChild(_overlay);
  setupModal(_overlay);
}

// ─── Build DOM ────────────────────────────────────────────────────────────────

function buildModal() {
  const currentId = _current?.provider?.provider_id || "llama_server";

  // Provider option list
  const providerOptions = Object.entries(_registry)
    .map(([id, info]) => {
      const icon = info.local ? "🖥" : "☁";
      return `<option value="${id}" ${id === currentId ? "selected" : ""}>${icon} ${info.label}</option>`;
    })
    .join("");

  const overlay = document.createElement("div");
  overlay.id        = "settings-overlay";
  overlay.className = "modal-overlay";
  overlay.innerHTML = `
    <div class="modal-card settings-card" id="settings-card">
      <div class="settings-header">
        <h2>⚙ Provider Settings</h2>
        <button class="settings-close" id="settings-close-btn">✕</button>
      </div>

      <div class="form-field">
        <label for="settings-provider-select">LLM Provider</label>
        <select id="settings-provider-select" class="settings-select">
          ${providerOptions}
        </select>
        <div id="settings-provider-desc" class="settings-desc"></div>
      </div>

      <div id="settings-fields">
        <!-- Dynamically rendered based on selected provider -->
      </div>

      <div id="settings-test-result" class="settings-test-result hidden"></div>

      <div class="modal-actions settings-actions">
        <button class="btn-secondary" id="settings-test-btn">⚡ Test Connection</button>
        <div style="flex:1"></div>
        <button class="btn-cancel"  id="settings-cancel-btn">Cancel</button>
        <button class="btn-submit"  id="settings-save-btn">Save</button>
      </div>
    </div>`;

  return overlay;
}

// ─── Setup interactions ───────────────────────────────────────────────────────

function setupModal(overlay) {
  const select     = overlay.querySelector("#settings-provider-select");
  const fieldsEl   = overlay.querySelector("#settings-fields");
  const descEl     = overlay.querySelector("#settings-provider-desc");
  const testResult = overlay.querySelector("#settings-test-result");

  // Initial render
  renderFields(fieldsEl, descEl, select.value);

  // Re-render on provider change
  select.addEventListener("change", () => {
    renderFields(fieldsEl, descEl, select.value);
    testResult.classList.add("hidden");
  });

  // Test connection
  overlay.querySelector("#settings-test-btn").addEventListener("click", async () => {
    const cfg = collectFields(overlay);
    testResult.className = "settings-test-result settings-test--loading";
    testResult.textContent = "⏳ Testing…";

    try {
      const resp = await fetch("/api/settings/test", {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify({ provider: cfg }),
      });
      const result = await resp.json();

      if (result.ok) {
        testResult.className = "settings-test-result settings-test--ok";
        let msg = `✓ ${result.message}`;
        if (result.models?.length) {
          // Populate model dropdown from discovered models
          populateModelDropdown(overlay, result.models, cfg.model);
          msg += ` — model list updated`;
        }
        testResult.textContent = msg;
      } else {
        testResult.className = "settings-test-result settings-test--err";
        testResult.textContent = `✗ ${result.message}`;
      }
    } catch (err) {
      testResult.className = "settings-test-result settings-test--err";
      testResult.textContent = `✗ ${err.message}`;
    }
  });

  // Save
  overlay.querySelector("#settings-save-btn").addEventListener("click", async () => {
    const cfg     = collectFields(overlay);
    const saveBtn = overlay.querySelector("#settings-save-btn");
    saveBtn.disabled   = true;
    saveBtn.textContent = "Saving…";

    try {
      const resp = await fetch("/api/settings", {
        method:  "PUT",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify({ provider: cfg }),
      });
      if (resp.ok) {
        testResult.className = "settings-test-result settings-test--ok";
        testResult.textContent = "✓ Settings saved. Provider reloaded.";
        setTimeout(() => overlay.remove(), 900);
      } else {
        const err = await resp.json();
        testResult.className = "settings-test-result settings-test--err";
        testResult.textContent = `✗ Save failed: ${err.detail || resp.statusText}`;
      }
    } catch (err) {
      testResult.className = "settings-test-result settings-test--err";
      testResult.textContent = `✗ ${err.message}`;
    } finally {
      saveBtn.disabled   = false;
      saveBtn.textContent = "Save";
    }
  });

  // Cancel / outside click
  overlay.querySelector("#settings-cancel-btn").addEventListener("click", () => overlay.remove());
  overlay.querySelector("#settings-close-btn").addEventListener("click", () => overlay.remove());
  overlay.addEventListener("click", e => { if (e.target === overlay) overlay.remove(); });
}

// ─── Field rendering ─────────────────────────────────────────────────────────

function renderFields(container, descEl, providerId) {
  const info    = _registry[providerId] || {};
  const current = _current?.provider || {};
  const isCurrent = current.provider_id === providerId;

  descEl.textContent = info.description || "";

  const savedUrl   = isCurrent ? (current.base_url || "")  : "";
  const savedKey   = isCurrent ? (current.api_key  || "")  : "";
  const savedModel = isCurrent ? (current.model    || "")  : "";
  const defaultUrl = info.default_url || "";

  let html = "";

  // Base URL (not shown for pure cloud APIs that have a fixed URL)
  if (info.needs_model || info.local || (!info.needs_key)) {
    html += `
      <div class="form-field">
        <label for="settings-url">Base URL</label>
        <input id="settings-url" type="text"
               value="${savedUrl || defaultUrl}"
               placeholder="${defaultUrl || "http://127.0.0.1:8080"}">
      </div>`;
  }

  // API key (cloud providers)
  if (info.needs_key) {
    html += `
      <div class="form-field">
        <label for="settings-key">API Key</label>
        <div class="settings-key-row">
          <input id="settings-key" type="password"
                 value="${savedKey}"
                 placeholder="Paste your API key here"
                 autocomplete="off">
          <button class="settings-eye-btn" id="settings-eye" title="Show/hide">👁</button>
        </div>
      </div>`;
  }

  // Model
  html += `
    <div class="form-field">
      <label for="settings-model">Model</label>
      <div class="settings-model-row">
        <input id="settings-model" type="text"
               value="${savedModel}"
               placeholder="e.g. llama3, gpt-4o-mini, claude-3-haiku-20240307"
               list="settings-model-list">
        <datalist id="settings-model-list"></datalist>
        <button class="settings-refresh-btn" id="settings-refresh-models"
                title="Fetch model list from provider">⟳</button>
      </div>
    </div>`;

  container.innerHTML = html;

  // Eye button for API key
  container.querySelector("#settings-eye")?.addEventListener("click", () => {
    const inp = container.querySelector("#settings-key");
    inp.type  = inp.type === "password" ? "text" : "password";
  });

  // Refresh models button
  container.querySelector("#settings-refresh-models")?.addEventListener("click", async () => {
    const btn = container.querySelector("#settings-refresh-models");
    btn.textContent = "…";
    try {
      const resp   = await fetch("/api/settings/models");
      const { models } = await resp.json();
      if (models?.length) {
        populateModelDropdown(container.closest(".modal-card").parentElement, models, savedModel);
      }
    } catch { /* silent */ }
    btn.textContent = "⟳";
  });
}

function populateModelDropdown(overlay, models, currentModel) {
  const dl = overlay.querySelector("#settings-model-list");
  if (!dl) return;
  dl.innerHTML = models.map(m => `<option value="${m}">`).join("");
  // If only one model, auto-select
  if (models.length === 1) {
    const inp = overlay.querySelector("#settings-model");
    if (inp && !inp.value) inp.value = models[0];
  }
}

function collectFields(overlay) {
  const providerId = overlay.querySelector("#settings-provider-select")?.value || "llama_server";
  const base_url   = overlay.querySelector("#settings-url")?.value?.trim()   || "";
  const api_key    = overlay.querySelector("#settings-key")?.value?.trim()   || "";
  const model      = overlay.querySelector("#settings-model")?.value?.trim() || "";
  return { provider_id: providerId, base_url, api_key, model };
}
