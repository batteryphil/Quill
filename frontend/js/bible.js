/**
 * bible.js — Story Bible right-sidebar panel.
 *
 * Displays auto-extracted characters and world rules.
 * Polls every 30s and after scene extractions complete.
 * Characters are editable inline.
 */

// ─── State ───────────────────────────────────────────────────────────────────

let currentProjectId = null;
let pollTimer        = null;

// ─── Public API ──────────────────────────────────────────────────────────────

/** Set the active project and start polling. */
export function setProject(projectId) {
  currentProjectId = projectId;
  clearInterval(pollTimer);
  refresh();
  pollTimer = setInterval(refresh, 30_000);
}

/** Force a refresh of both panels. */
export async function refresh() {
  if (!currentProjectId) return;
  await Promise.all([refreshCharacters(), refreshWorldRules()]);
}

// ─── Characters ───────────────────────────────────────────────────────────────

async function refreshCharacters() {
  const resp = await fetch(`/api/projects/${currentProjectId}/characters`);
  if (!resp.ok) return;
  const db = await resp.json();
  renderCharacters(db);
}

function renderCharacters(db) {
  const container = document.getElementById("bible-characters");
  if (!container) return;

  const names = Object.keys(db);
  if (names.length === 0) {
    container.innerHTML = `<div class="bible-empty">
      No characters yet.<br>
      <small>They appear automatically as you write.</small>
    </div>`;
    return;
  }

  container.innerHTML = "";
  for (const name of names) {
    const c   = db[name];
    const el  = document.createElement("div");
    el.className = "char-card";

    const fields = [
      { key: "appearance",   label: "Appearance" },
      { key: "location",     label: "Location"   },
      { key: "trait",        label: "Trait"       },
      { key: "arc_state",    label: "Arc"         },
      { key: "relationship", label: "Relationship"},
    ];

    const rows = fields
      .map(({ key, label }) => `
        <div class="char-field">
          <span class="char-field-label">${label}</span>
          <span class="char-field-value"
                contenteditable="true"
                data-name="${name}"
                data-field="${key}">${c[key] || ""}</span>
        </div>`)
      .join("");

    el.innerHTML = `
      <div class="char-name">${name}</div>
      ${rows}
    `;

    el.querySelectorAll("[contenteditable]").forEach(span => {
      span.addEventListener("blur", async () => {
        const val = span.textContent.trim();
        await fetch(
          `/api/projects/${currentProjectId}/characters/${encodeURIComponent(span.dataset.name)}`,
          {
            method:  "PATCH",
            headers: { "Content-Type": "application/json" },
            body:    JSON.stringify({ field: span.dataset.field, value: val }),
          }
        );
      });
    });

    container.appendChild(el);
  }
}

// ─── World Rules ──────────────────────────────────────────────────────────────

async function refreshWorldRules() {
  const resp = await fetch(`/api/projects/${currentProjectId}/world_rules`);
  if (!resp.ok) return;
  const rules = await resp.json();
  renderWorldRules(rules);
}

function renderWorldRules(rules) {
  const container = document.getElementById("bible-world-rules");
  if (!container) return;

  if (rules.length === 0) {
    container.innerHTML = `<div class="bible-empty">
      No world facts yet.<br>
      <small>Extracted as you write.</small>
    </div>`;
    return;
  }

  const categories = [...new Set(rules.map(r => r.category))].sort();

  container.innerHTML = categories.map(cat => {
    const catRules = rules.filter(r => r.category === cat);
    return `
      <div class="world-category">
        <div class="world-cat-label">${cat}</div>
        ${catRules.map(r => `
          <div class="world-rule">
            <span class="world-rule-dot">•</span>
            <span class="world-rule-text">${r.fact}</span>
          </div>`).join("")}
      </div>`;
  }).join("");

  // Add rule button
  const addBtn = document.createElement("button");
  addBtn.className   = "btn-add-rule";
  addBtn.textContent = "+ Add rule";
  addBtn.addEventListener("click", () => showAddRuleModal());
  container.appendChild(addBtn);
}

function showAddRuleModal() {
  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";
  overlay.innerHTML = `
    <div class="modal-card" style="width:380px">
      <h2>Add World Rule</h2>
      <div class="form-field">
        <label>Rule / Fact</label>
        <textarea id="wr-fact" style="min-height:60px" placeholder="e.g. Magic requires physical contact with water"></textarea>
      </div>
      <div class="form-field">
        <label>Category</label>
        <input id="wr-category" type="text" placeholder="rule" value="rule">
      </div>
      <div class="modal-actions">
        <button class="btn-cancel" id="wr-cancel">Cancel</button>
        <button class="btn-submit" id="wr-add">Add</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);

  overlay.querySelector("#wr-cancel").addEventListener("click", () => overlay.remove());
  overlay.querySelector("#wr-add").addEventListener("click", async () => {
    const fact = overlay.querySelector("#wr-fact").value.trim();
    const cat  = overlay.querySelector("#wr-category").value.trim() || "rule";
    if (!fact) return;
    await fetch(`/api/projects/${currentProjectId}/world_rules`, {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify({ fact, category: cat }),
    });
    overlay.remove();
    refreshWorldRules();
  });
}
