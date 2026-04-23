/**
 * Quill — Story Review & Auto-Fix Panel
 *
 * Opens a modal that:
 *  1. Runs /api/projects/{id}/review
 *  2. Displays categorised issues
 *  3. Lets user "Fix All" or fix individual issues
 */

export function openReviewPanel(projectId, projectTitle) {
  if (!projectId) { alert("Open a project first."); return; }

  const overlay = document.createElement("div");
  overlay.id = "review-overlay";
  overlay.style.cssText = [
    "position:fixed","inset:0","background:rgba(0,0,0,.65)",
    "backdrop-filter:blur(6px)","display:flex","align-items:center",
    "justify-content:center","z-index:400","font-family:'Inter',sans-serif"
  ].join(";");

  overlay.innerHTML = `
    <div id="review-card" style="
      background:#1a1a2e;border:1px solid #30363d;border-radius:14px;
      width:780px;max-width:96vw;max-height:88vh;display:flex;
      flex-direction:column;box-shadow:0 32px 80px rgba(0,0,0,.6);
      color:#e6edf3;overflow:hidden;">

      <!-- Header -->
      <div style="display:flex;align-items:center;justify-content:space-between;
                  padding:20px 24px;border-bottom:1px solid #30363d;flex-shrink:0">
        <div>
          <h2 style="margin:0;font-size:1.15rem;font-weight:700;color:#e6edf3">
            🔍 Story Review
          </h2>
          <p style="margin:4px 0 0;font-size:.8rem;color:#8b949e">${_esc(projectTitle)}</p>
        </div>
        <div style="display:flex;gap:10px;align-items:center">
          <button id="rv-fix-all" disabled style="
            padding:8px 18px;background:#7c3aed;border:none;border-radius:8px;
            color:#fff;cursor:pointer;font-weight:600;font-size:.85rem;
            opacity:.4;transition:opacity .2s">
            ✓ Fix All Issues
          </button>
          <button id="rv-close" style="
            background:transparent;border:1px solid #30363d;border-radius:8px;
            color:#8b949e;cursor:pointer;padding:7px 14px;font-size:.85rem">
            ✕ Close
          </button>
        </div>
      </div>

      <!-- Summary bar -->
      <div id="rv-summary" style="
        padding:14px 24px;border-bottom:1px solid #21262d;
        background:#161b22;flex-shrink:0;font-size:.82rem;color:#8b949e">
        Scanning your story…
      </div>

      <!-- Issue list -->
      <div id="rv-list" style="flex:1;overflow-y:auto;padding:16px 24px">
        <div style="display:flex;align-items:center;gap:12px;color:#8b949e;
                    padding:40px 0;justify-content:center">
          <span style="font-size:1.4rem">⏳</span>
          <span>Running review — this takes a few seconds…</span>
        </div>
      </div>
    </div>`;

  document.body.appendChild(overlay);
  overlay.querySelector("#rv-close").onclick = () => overlay.remove();
  overlay.addEventListener("click", e => { if (e.target === overlay) overlay.remove(); });

  _runReview(projectId, overlay);
}

// ── Issue type config ────────────────────────────────────────────────────

const ISSUE_META = {
  approach_label:  { icon: "🏷️",  label: "Approach Label",     color: "#f97316" },
  empty_scene:     { icon: "📭",  label: "Empty Scene",         color: "#ef4444" },
  repetition:      { icon: "🔁",  label: "Repeated Paragraph",  color: "#eab308" },
  character_drift: { icon: "👤",  label: "Character Name Drift", color: "#a78bfa" },
};

// ── Core logic ───────────────────────────────────────────────────────────

async function _runReview(projectId, overlay) {
  const listEl    = overlay.querySelector("#rv-list");
  const summaryEl = overlay.querySelector("#rv-summary");
  const fixAllBtn = overlay.querySelector("#rv-fix-all");

  try {
    const resp = await fetch(`/api/projects/${projectId}/review`, {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify({ protagonist: "" }),
    });
    const data = await resp.json();

    if (!data.issues || data.issues.length === 0) {
      summaryEl.innerHTML = `<span style="color:#3fb950">✓ No issues found across ${data.scene_count} scenes.</span>`;
      listEl.innerHTML = `<div style="text-align:center;padding:60px 0;color:#3fb950;font-size:1.1rem">
        🎉 Story looks clean!
      </div>`;
      return;
    }

    // Summary bar
    const parts = Object.entries(data.summary)
      .map(([type, count]) => {
        const m = ISSUE_META[type] || { icon: "⚠️", label: type, color: "#8b949e" };
        return `<span style="margin-right:16px">${m.icon} <b style="color:${m.color}">${count}</b> ${m.label}${count > 1 ? "s" : ""}</span>`;
      }).join("");
    summaryEl.innerHTML = `Found <b style="color:#e6edf3">${data.total}</b> issue${data.total > 1 ? "s" : ""} across 
      <b>${data.scene_count}</b> scenes: ${parts}`;

    // Build issue cards
    listEl.innerHTML = "";
    for (const issue of data.issues) {
      listEl.appendChild(_buildIssueCard(issue));
    }

    // Enable Fix All
    fixAllBtn.disabled = false;
    fixAllBtn.style.opacity = "1";
    fixAllBtn.onclick = () => _fixAll(projectId, data.issues, overlay);

  } catch (err) {
    summaryEl.innerHTML = `<span style="color:#ef4444">Review error: ${_esc(err.message)}</span>`;
    listEl.innerHTML = "";
  }
}

function _buildIssueCard(issue) {
  const m = ISSUE_META[issue.type] || { icon: "⚠️", label: issue.type, color: "#8b949e" };

  const card = document.createElement("div");
  card.dataset.issueId = issue.scene_id + "_" + issue.type;
  card.style.cssText = `
    background:#0d1117;border:1px solid #21262d;border-radius:10px;
    padding:14px 16px;margin-bottom:10px;display:flex;
    align-items:flex-start;gap:14px;
    border-left:3px solid ${m.color};
  `;

  card.innerHTML = `
    <div style="font-size:1.3rem;flex-shrink:0;margin-top:2px">${m.icon}</div>
    <div style="flex:1;min-width:0">
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">
        <span style="font-size:.72rem;font-weight:600;color:${m.color};
                     text-transform:uppercase;letter-spacing:.05em">${m.label}</span>
        <span style="font-size:.72rem;color:#8b949e">
          ${issue.scene_id === "all" ? "All Scenes" : "Scene: " + _esc(issue.scene_id)}
        </span>
      </div>
      <div style="font-size:.85rem;color:#c9d1d9;margin-bottom:6px">
        ${_esc(issue.description)}
      </div>
      ${issue.detail && issue.fix_type !== "normalize_name" ? `
        <div style="font-size:.75rem;color:#8b949e;font-style:italic;
                    background:#161b22;border-radius:5px;padding:6px 10px;
                    margin-top:4px;word-break:break-word">
          ${_esc(issue.detail.slice(0, 150))}${issue.detail.length > 150 ? "…" : ""}
        </div>` : ""}
    </div>
    <button data-fix='${JSON.stringify(issue)}' style="
      padding:6px 12px;background:#21262d;border:1px solid #30363d;
      border-radius:6px;color:#c9d1d9;cursor:pointer;font-size:.78rem;
      white-space:nowrap;flex-shrink:0;transition:background .15s">
      Fix
    </button>`;

  card.querySelector("button").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    const issue = JSON.parse(btn.dataset.fix);
    btn.textContent = "Fixing…";
    btn.disabled = true;
    const projectId = document.body.dataset.projectId;
    await _applyFixes(projectId, [issue], "");
    card.style.opacity = ".4";
    btn.textContent = "✓ Fixed";
    btn.style.color = "#3fb950";
  });

  return card;
}

async function _fixAll(projectId, issues, overlay) {
  const btn       = overlay.querySelector("#rv-fix-all");
  const summaryEl = overlay.querySelector("#rv-summary");
  const listEl    = overlay.querySelector("#rv-list");

  btn.textContent = "Applying fixes…";
  btn.disabled = true;

  const regenCount = issues.filter(i => i.fix_type === "regenerate_scene").length;
  const fixCount   = issues.filter(i => i.fix_type !== "regenerate_scene").length;

  summaryEl.innerHTML = `
    <span style="color:#a78bfa">⚙️ Stripping labels &amp; normalizing names…</span>`;

  await _applyFixes(projectId, issues, "");

  if (regenCount > 0) {
    summaryEl.innerHTML = `
      <span style="color:#60a5fa">✍️ Regenerating ${regenCount} empty scene${regenCount > 1 ? "s" : ""} via AI… this may take a few minutes.</span>`;

    // Show live regen cards
    listEl.innerHTML = `
      <div style="color:#8b949e;padding:16px 0;font-size:.85rem">
        <div style="margin-bottom:12px">Generating new prose for ${regenCount} blank scene${regenCount > 1 ? "s" : ""}…</div>
        <div id="rv-regen-list" style="display:flex;flex-direction:column;gap:8px"></div>
      </div>`;

    const regenList = listEl.querySelector("#rv-regen-list");
    const regenIssues = issues.filter(i => i.fix_type === "regenerate_scene");
    for (const issue of regenIssues) {
      const el = document.createElement("div");
      el.id = `rv-regen-${issue.scene_id}`;
      el.style.cssText = "background:#161b22;border:1px solid #21262d;border-radius:8px;padding:10px 14px;font-size:.82rem";
      el.innerHTML = `<span style="color:#60a5fa">⏳</span> <b>${_esc(issue.scene_title.slice(0,60))}${issue.scene_title.length > 60 ? "…" : ""}</b>`;
      regenList.appendChild(el);
    }
  }

  // ── Auto re-evaluate after fixes ──────────────────────────────────────────
  summaryEl.innerHTML = `<span style="color:#f0c27f">🔍 Re-evaluating story…</span>`;

  try {
    const resp = await fetch(`/api/projects/${projectId}/review`, {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify({ protagonist: "" }),
    });
    const data = await resp.json();

    if (!data.issues || data.issues.length === 0) {
      summaryEl.innerHTML = `<span style="color:#3fb950">✅ All issues resolved! Story is clean across ${data.scene_count} scenes.</span>`;
      listEl.innerHTML = `<div style="text-align:center;padding:60px 0;color:#3fb950;font-size:1.1rem">
        🎉 Story is clean — no issues remaining!
      </div>`;
    } else {
      const parts = Object.entries(data.summary)
        .map(([type, count]) => `<b style="color:#e6edf3">${count}</b> ${type.replace(/_/g," ")}${count > 1 ? "s" : ""}`)
        .join(", ");
      summaryEl.innerHTML = `<span style="color:#f0c27f">⚠️ ${data.total} issue${data.total > 1 ? "s" : ""} remaining: ${parts}</span>`;

      listEl.innerHTML = "";
      for (const issue of data.issues) {
        listEl.appendChild(_buildIssueCard(issue));
      }

      // Re-wire Fix All for remaining issues
      btn.textContent = "✓ Fix Remaining";
      btn.disabled = false;
      btn.style.opacity = "1";
      btn.onclick = () => _fixAll(projectId, data.issues, overlay);
    }
  } catch (err) {
    summaryEl.innerHTML = `<span style="color:#ef4444">Re-evaluation error: ${_esc(err.message)}</span>`;
  }

  if (window.__binderRefresh) window.__binderRefresh();
}

async function _applyFixes(projectId, fixes, protagonist) {
  try {
    await fetch(`/api/projects/${projectId}/fix`, {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify({ fixes, protagonist }),
    });
  } catch (err) {
    console.error("[Review] Fix failed:", err);
  }
}

function _esc(str = "") {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
