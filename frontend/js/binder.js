/**
 * binder.js — Left sidebar: project tree and scene management.
 */

import { loadScene, setGoal } from "./editor.js";

// ─── State ────────────────────────────────────────────────────────────────────

let currentProject = null;
let sceneMeta      = {};
let metaPollTimer  = null;

// ─── Render binder from structure ────────────────────────────────────────────

function buildLabelBadges(meta) {
  const labels = [];
  if (meta.pov) labels.push(`<span class="badge">${meta.pov}</span>`);
  if (meta.pacing) labels.push(`<span class="badge">${meta.pacing}</span>`);
  if (meta.tension) labels.push(`<span class="badge">${meta.tension}</span>`);
  return labels.join("");
}

function renderBinder(project) {
  currentProject = project;
  const { structure } = project;
  const binder = document.getElementById("binder-tree");
  binder.innerHTML = "";

  for (const act of structure.acts) {
    const actEl = document.createElement("div");
    actEl.className = "binder-act";

    const actHeader = document.createElement("div");
    actHeader.className = "binder-act-header";
    actHeader.innerHTML = `<span class="binder-toggle">▾</span> <span>${act.title}</span>`;
    actHeader.addEventListener("click", () => {
      actEl.classList.toggle("collapsed");
    });
    actEl.appendChild(actHeader);

    for (const chapter of act.chapters) {
      const chapEl = document.createElement("div");
      chapEl.className = "binder-chapter";

      const chapHeader = document.createElement("div");
      chapHeader.className = "binder-chapter-header";
      chapHeader.innerHTML = `<span class="binder-toggle">▾</span> ${chapter.title}`;
      chapHeader.addEventListener("click", () => chapEl.classList.toggle("collapsed"));
      chapEl.appendChild(chapHeader);

      for (const scene of chapter.scenes) {
        const sceneEl = document.createElement("div");
        sceneEl.className = "binder-scene";
        sceneEl.dataset.sceneId = scene.id;

        const statusDot = `<span class="status-dot status-${scene.status}"></span>`;
        const wc        = scene.word_count > 0
          ? `<span class="scene-wc">${scene.word_count.toLocaleString()}w</span>`
          : "";

        // Scene meta labels (pov, pacing, tension) if extracted
        const meta   = sceneMeta[scene.id];
        const labels = meta ? buildLabelBadges(meta) : "";

        sceneEl.innerHTML = `
          <span class="scene-icon">📄</span>
          <span class="scene-title">${scene.title}</span>
          ${wc}${labels}${statusDot}
        `;

        sceneEl.addEventListener("click", async () => {
          // Deactivate previous
          document.querySelectorAll(".binder-scene.active")
            .forEach(el => el.classList.remove("active"));
          sceneEl.classList.add("active");

          // Update header
          document.getElementById("scene-title-display").textContent = scene.title;

          await loadScene(project.id, scene.id, project.genre);
        });

        chapEl.appendChild(sceneEl);
      }

      // + New scene button
      const addSceneBtn = document.createElement("button");
      addSceneBtn.className = "btn-add-scene";
      addSceneBtn.textContent = "+ Scene";
      addSceneBtn.addEventListener("click", async (e) => {
        e.stopPropagation();
        const overlay = document.createElement("div");
        overlay.style.cssText = "position:fixed;inset:0;background:rgba(0,0,0,.4);display:flex;align-items:center;justify-content:center;z-index:300";
        overlay.innerHTML = `<div style="background:#fff;padding:24px;border-radius:10px;width:300px;font-family:'Inter',sans-serif">
          <p style="margin-bottom:10px;font-weight:500">Scene title</p>
          <input id="ns-title" type="text" placeholder="New Scene" autofocus
            style="width:100%;padding:8px 10px;border:1px solid #ddd;border-radius:6px;font-size:.9rem;box-sizing:border-box">
          <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:14px">
            <button id="ns-cancel" style="padding:6px 14px;border:1px solid #ddd;border-radius:5px;background:transparent;cursor:pointer">Cancel</button>
            <button id="ns-create" style="padding:6px 14px;background:#7c3aed;color:#fff;border:none;border-radius:5px;cursor:pointer;font-weight:500">Create</button>
          </div></div>`;
        document.body.appendChild(overlay);
        const inp = overlay.querySelector("#ns-title"); inp.focus();
        const doAdd = async () => {
          const title = inp.value.trim() || "New Scene";
          overlay.remove();
          await fetch(`/api/projects/${project.id}/scenes`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ act: act.id, chapter: chapter.id, title }),
          });
          await refreshProject(project.id);
        };
        overlay.querySelector("#ns-cancel").addEventListener("click", () => overlay.remove());
        overlay.querySelector("#ns-create").addEventListener("click", doAdd);
        inp.addEventListener("keydown", (e) => { if (e.key === "Enter") doAdd(); });
      });
      chapEl.appendChild(addSceneBtn);

      actEl.appendChild(chapEl);
    }

    binder.appendChild(actEl);
  }
}

// ─── Load a project ──────────────────────────────────────────────────────────

async function loadProject(projectId) {
  const resp = await fetch(`/api/projects/${projectId}`);
  if (!resp.ok) return;
  const project = await resp.json();

  document.getElementById("project-title").textContent = project.title;

  // Make project ID accessible to other modules (export button)
  document.body.dataset.projectId = project.id;

  // Init progress bar goal from project
  setGoal(project.word_count_goal || 80_000);

  // Load scene meta and start polling
  clearInterval(metaPollTimer);
  await refreshSceneMeta(projectId);
  metaPollTimer = setInterval(() => refreshSceneMeta(projectId), 30_000);

  renderBinder(project);
}

async function refreshSceneMeta(projectId) {
  try {
    const resp = await fetch(`/api/projects/${projectId}/scene_meta`);
    if (resp.ok) {
      sceneMeta = await resp.json();
      // Re-render if binder is visible (lightweight — just update badge spans)
      document.querySelectorAll(".binder-scene").forEach(el => {
        const sid  = el.dataset.sceneId;
        const meta = sceneMeta[sid];
        if (!meta) return;
        const existing = el.querySelector(".badge");
        if (!existing && (meta.pov || meta.pacing)) {
          // Inject badges before the status dot
          const dot  = el.querySelector(".status-dot");
          const frag = document.createDocumentFragment();
          [meta.pov, meta.pacing].filter(Boolean).forEach(label => {
            const b = document.createElement("span");
            b.className   = "badge";
            b.textContent = label;
            frag.appendChild(b);
          });
          if (dot) el.insertBefore(frag, dot);
        }
      });
    }
  } catch { /* silent */ }
}

async function refreshProject(projectId) {
  await loadProject(projectId);
}

// ─── Project sidebar list ─────────────────────────────────────────────────────

async function renderProjectList() {
  const resp     = await fetch("/api/projects");
  const projects = await resp.json();
  const list     = document.getElementById("project-list");
  list.innerHTML = "";

  for (const p of projects) {
    const item = document.createElement("div");
    item.className = "project-list-item";
    item.style.display = "flex";
    item.style.alignItems = "center";

    const titleSpan = document.createElement("span");
    titleSpan.textContent = p.title;
    titleSpan.style.flex = "1";

    const delBtn = document.createElement("button");
    delBtn.innerHTML = "🗑️";
    delBtn.style.cssText = "background:transparent;border:none;cursor:pointer;opacity:0.5;";
    delBtn.title = "Delete Project";
    
    delBtn.onmouseover = () => delBtn.style.opacity = "1";
    delBtn.onmouseout = () => delBtn.style.opacity = "0.5";
    
    delBtn.addEventListener("click", async (e) => {
      e.stopPropagation();
      if (!confirm(`Are you sure you want to permanently delete "${p.title}"?`)) return;
      await fetch(`/api/projects/${p.id}`, { method: "DELETE" });
      renderProjectList();
    });

    item.appendChild(titleSpan);
    item.appendChild(delBtn);

    item.addEventListener("click", () => {
      document.getElementById("project-picker").classList.add("hidden");
      document.getElementById("main-layout").classList.remove("hidden");
      loadProject(p.id);
    });
    list.appendChild(item);
  }
}

// ─── New project modal ──────────────────────────────────────────────────────

function showNewProjectModal() {
  // Reuse the self-write modal pattern — build a lightweight inline dialog
  const overlay = document.createElement("div");
  overlay.style.cssText = [
    "position:fixed","inset:0","background:rgba(0,0,0,.6)",
    "backdrop-filter:blur(4px)","display:flex",
    "align-items:center","justify-content:center","z-index:300"
  ].join(";");

  overlay.innerHTML = `
    <div style="background:#fff;border-radius:12px;padding:32px;width:360px;
                box-shadow:0 24px 64px rgba(0,0,0,.25);font-family:'Inter',sans-serif">
      <h2 style="font-size:1.1rem;font-weight:600;margin-bottom:20px;color:#1c1917">
        New Project
      </h2>
      <div style="margin-bottom:14px">
        <label style="display:block;font-size:.78rem;font-weight:500;color:#6b7280;margin-bottom:5px">
          Title
        </label>
        <input id="np-title" type="text"
          placeholder="e.g. The Lighthouse Chronicles"
          autofocus
          style="width:100%;padding:9px 12px;border:1px solid #e5e7eb;border-radius:6px;
                 font-size:.9rem;outline:none;font-family:inherit;box-sizing:border-box">
      </div>
      <div style="margin-bottom:20px">
        <label style="display:block;font-size:.78rem;font-weight:500;color:#6b7280;margin-bottom:5px">
          Genre
        </label>
        <input id="np-genre" type="text" placeholder="fiction" value="fiction"
          style="width:100%;padding:9px 12px;border:1px solid #e5e7eb;border-radius:6px;
                 font-size:.9rem;outline:none;font-family:inherit;box-sizing:border-box">
      </div>
      <div style="display:flex;gap:10px;justify-content:flex-end">
        <button id="np-cancel" style="padding:8px 16px;background:transparent;border:1px solid #e5e7eb;
                border-radius:6px;color:#6b7280;cursor:pointer;font-family:inherit;font-size:.85rem">
          Cancel
        </button>
        <button id="np-create" style="padding:8px 18px;background:#7c3aed;border:none;
                border-radius:6px;color:#fff;cursor:pointer;font-family:inherit;
                font-size:.85rem;font-weight:500">
          Create
        </button>
      </div>
    </div>
  `;

  document.body.appendChild(overlay);
  const input = overlay.querySelector("#np-title");
  input.focus();

  async function doCreate() {
    const title = input.value.trim();
    const genre = overlay.querySelector("#np-genre").value.trim() || "fiction";
    if (!title) { input.style.borderColor = "#ef4444"; return; }
    overlay.remove();
    const resp = await fetch("/api/projects", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title, genre }),
    });
    const project = await resp.json();
    document.getElementById("project-picker").classList.add("hidden");
    document.getElementById("main-layout").classList.remove("hidden");
    loadProject(project.id);
  }

  overlay.querySelector("#np-cancel").addEventListener("click", () => overlay.remove());
  overlay.querySelector("#np-create").addEventListener("click", doCreate);
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") doCreate(); });
}

document.getElementById("btn-new-project")?.addEventListener("click", showNewProjectModal);
// Also wire the in-editor sidebar + button
document.getElementById("btn-new-project-sidebar")?.addEventListener("click", showNewProjectModal);

// Wire the folder/open-project button to show the project picker
document.getElementById("btn-open-project")?.addEventListener("click", () => {
  renderProjectList();
  document.getElementById("main-layout").classList.add("hidden");
  document.getElementById("project-picker").classList.remove("hidden");
});

// Expose binder refresh so bookwriter can trigger it after completion
window.__binderRefresh = () => {
  renderProjectList();
  if (document.body.dataset.projectId) {
    refreshProject(document.body.dataset.projectId);
  }
};

// ─── Boot ────────────────────────────────────────────────────────────────────

(async function boot() {
  const resp     = await fetch("/api/projects");
  const projects = await resp.json();

  if (projects.length === 0) {
    // No projects yet — show picker with just "new" option
    document.getElementById("project-picker").classList.remove("hidden");
    renderProjectList();
    return;
  }

  // Auto-load the most recently updated project
  const latest = projects.sort((a, b) =>
    new Date(b.updated) - new Date(a.updated)
  )[0];

  document.getElementById("project-picker").classList.add("hidden");
  document.getElementById("main-layout").classList.remove("hidden");
  await loadProject(latest.id);
})();
