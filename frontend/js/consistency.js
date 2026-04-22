/**
 * consistency.js — Contradiction auditor panel.
 *
 * Displays contradiction warning cards surfaced by the auditor.
 * Writer can: Fix noted, Update character card, or Dismiss.
 * Polls every 60s and refreshes after manual audit runs.
 */

// ─── State ───────────────────────────────────────────────────────────────────

let currentProjectId = null;
let pollTimer        = null;

// ─── Public API ──────────────────────────────────────────────────────────────

export function setProject(projectId) {
  currentProjectId = projectId;
  clearInterval(pollTimer);
  refresh();
  pollTimer = setInterval(refresh, 60_000);
}

export async function refresh() {
  if (!currentProjectId) return;
  await loadContradictions();
  updateBadge();
}

// ─── Run audit button ─────────────────────────────────────────────────────────

document.getElementById("btn-run-audit")?.addEventListener("click", async () => {
  if (!currentProjectId) return;
  const btn = document.getElementById("btn-run-audit");
  btn.textContent = "Auditing…";
  btn.disabled    = true;
  try {
    await fetch(`/api/audit/run/${currentProjectId}`, { method: "POST" });
    await refresh();
  } finally {
    btn.textContent = "▶ Run audit";
    btn.disabled    = false;
  }
});

// ─── Load contradictions ──────────────────────────────────────────────────────

async function loadContradictions() {
  if (!currentProjectId) return;
  const resp = await fetch(`/api/audit/contradictions/${currentProjectId}`);
  if (!resp.ok) return;
  const items = await resp.json();
  renderContradictions(items);
}

function renderContradictions(items) {
  const container = document.getElementById("consistency-list");
  if (!container) return;

  if (items.length === 0) {
    container.innerHTML = `<div class="bible-empty">
      <span style="font-size:1.3rem">✓</span><br>
      No contradictions found.<br>
      <small>Auditor runs every 5 scenes.</small>
    </div>`;
    return;
  }

  container.innerHTML = "";
  for (const item of items) {
    const el = document.createElement("div");
    el.className = `contradiction-card severity-${item.severity || "medium"}`;

    el.innerHTML = `
      <div class="contradiction-header">
        <span class="contradiction-severity-icon">${severityIcon(item.severity)}</span>
        <span class="contradiction-field">${item.field || "Inconsistency"}</span>
      </div>
      <div class="contradiction-body">
        <div class="contradiction-row">
          <span class="contradiction-label">Established:</span>
          <span class="contradiction-text">${item.established || ""}</span>
        </div>
        <div class="contradiction-row">
          <span class="contradiction-label">Contradicts:</span>
          <span class="contradiction-text contradiction-text--error">${item.contradicting || ""}</span>
        </div>
      </div>
      <div class="contradiction-actions">
        <button class="contradiction-btn" data-id="${item.id}" data-action="fix_noted">Fix scene</button>
        <button class="contradiction-btn" data-id="${item.id}" data-action="update_card">Update card</button>
        <button class="contradiction-btn contradiction-btn--dismiss" data-id="${item.id}" data-action="dismiss">Dismiss</button>
      </div>`;

    el.querySelectorAll(".contradiction-btn").forEach(btn => {
      btn.addEventListener("click", async () => {
        await resolveContradiction(btn.dataset.id, btn.dataset.action);
        el.classList.add("resolving");
        setTimeout(() => el.remove(), 300);
        updateBadge();
      });
    });

    container.appendChild(el);
  }
}

function severityIcon(severity) {
  return severity === "high" ? "🔴" : severity === "low" ? "🟡" : "🟠";
}

async function resolveContradiction(id, action) {
  await fetch(`/api/audit/contradictions/${currentProjectId}/${id}`, {
    method:  "PATCH",
    headers: { "Content-Type": "application/json" },
    body:    JSON.stringify({ action }),
  });
}

// ─── Badge on tab ─────────────────────────────────────────────────────────────

async function updateBadge() {
  if (!currentProjectId) return;
  const resp = await fetch(`/api/audit/contradictions/${currentProjectId}`);
  if (!resp.ok) return;
  const items = await resp.json();
  const badge = document.getElementById("consistency-badge");
  if (!badge) return;
  if (items.length > 0) {
    badge.textContent = items.length;
    badge.classList.remove("hidden");
  } else {
    badge.classList.add("hidden");
  }
}
