/**
 * bookwriter.js — Full Book AI Write UI.
 *
 * 3-step flow:
 *   Step 1: Config modal (premise, genre, word count, …)
 *   Step 2: Outline preview (generated, can be inspected)
 *   Step 3: Writing dashboard (live progress, streaming scene, chapter tree)
 */

// ─── Constants ────────────────────────────────────────────────────────────────

const GENRES = [
  "literary fiction","mystery","thriller","fantasy","science fiction",
  "horror","romance","historical fiction","adventure","young adult",
  "crime","dystopian","magical realism",
];

const TONES = ["balanced","dark","hopeful","tense","lyrical","action-packed","introspective","humorous"];
const POVS  = ["third person limited","third person omniscient","first person","second person"];

// ─── Public entry point ───────────────────────────────────────────────────────

export function openBookWriter(projectId, projectTitle) {
  if (!projectId) {
    alert("Open a project first, then click Write Full Book.");
    return;
  }
  const overlay = _buildConfigModal(projectId, projectTitle);
  document.body.appendChild(overlay);
}

// ─── Step 1: Config modal ─────────────────────────────────────────────────────

function _buildConfigModal(projectId, projectTitle) {
  const overlay = document.createElement("div");
  overlay.id        = "bw-overlay";
  overlay.className = "modal-overlay";

  const genreOptions = GENRES.map(g =>
    `<option value="${g}">${_titleCase(g)}</option>`
  ).join("");
  const toneOptions = TONES.map(t =>
    `<option value="${t}">${_titleCase(t)}</option>`
  ).join("");
  const povOptions = POVS.map(p =>
    `<option value="${p}">${_titleCase(p)}</option>`
  ).join("");

  overlay.innerHTML = `
    <div class="modal-card bw-card" id="bw-card">
      <div class="settings-header">
        <h2>✍ AI Full Book Writer</h2>
        <button class="settings-close" id="bw-close">✕</button>
      </div>
      <p class="bw-subtitle">Project: <strong>${_esc(projectTitle)}</strong></p>

      <div class="bw-grid">
        <div class="form-field bw-full">
          <label for="bw-premise">Premise <span class="bw-req">*</span></label>
          <textarea id="bw-premise" rows="5"
            placeholder="Describe your story in 1–3 paragraphs. Include the central conflict, protagonist, and world. The more detail, the better the AI outline."></textarea>
        </div>

        <div class="form-field">
          <label for="bw-genre">Genre</label>
          <select id="bw-genre">${genreOptions}</select>
        </div>

        <div class="form-field">
          <label for="bw-tone">Tone</label>
          <select id="bw-tone">${toneOptions}</select>
        </div>

        <div class="form-field">
          <label for="bw-pov">Point of View</label>
          <select id="bw-pov">${povOptions}</select>
        </div>

        <div class="form-field">
          <label for="bw-protagonist">Protagonist</label>
          <input id="bw-protagonist" type="text"
                 placeholder="Name + brief description">
        </div>

        <div class="form-field">
          <label for="bw-antagonist">Antagonist / Conflict</label>
          <input id="bw-antagonist" type="text"
                 placeholder="Villain, force, or internal conflict">
        </div>

        <div class="form-field bw-full">
          <label for="bw-setting">Setting</label>
          <input id="bw-setting" type="text"
                 placeholder="Time period, location, world rules…">
        </div>

        <div class="form-field">
          <label for="bw-words">Target Word Count: <span id="bw-words-val">50,000</span></label>
          <input id="bw-words" type="range" min="5000" max="120000" step="5000" value="50000">
          <div class="bw-range-labels">
            <span>Novella (5K)</span><span>Novel (50K)</span><span>Epic (120K)</span>
          </div>
        </div>

        <div class="form-field">
          <label for="bw-chapters">Chapters: <span id="bw-chapters-val">20</span></label>
          <input id="bw-chapters" type="range" min="5" max="50" step="1" value="20">
        </div>

        <div class="form-field">
          <label for="bw-spc">Scenes per chapter: <span id="bw-spc-val">3</span></label>
          <input id="bw-spc" type="range" min="1" max="6" step="1" value="3">
        </div>

        <div class="bw-full bw-estimate" id="bw-estimate">
          📖 ~60 scenes · ~36,000 words · ~80 min writing time
        </div>
      </div>

      <div class="modal-actions">
        <button class="btn-cancel" id="bw-cancel-cfg">Cancel</button>
        <button class="btn-submit" id="bw-gen-outline">Generate Outline →</button>
      </div>
    </div>`;

  _wireConfig(overlay, projectId);
  return overlay;
}

function _wireConfig(overlay, projectId) {
  overlay.querySelector("#bw-close").onclick    = () => overlay.remove();
  overlay.querySelector("#bw-cancel-cfg").onclick = () => overlay.remove();
  overlay.addEventListener("click", e => { if (e.target === overlay) overlay.remove(); });

  // Live sliders
  const words = overlay.querySelector("#bw-words");
  const chaps = overlay.querySelector("#bw-chapters");
  const spc   = overlay.querySelector("#bw-spc");

  function updateEstimate() {
    const w  = parseInt(words.value);
    const c  = parseInt(chaps.value);
    const s  = parseInt(spc.value);
    const scenes    = c * s;
    const wps       = Math.round(w / Math.max(scenes, 1));
    const etaMin    = Math.round(scenes * 50 / 60);  // ~50s per scene @ BitNet speed
    overlay.querySelector("#bw-words-val").textContent    = w.toLocaleString();
    overlay.querySelector("#bw-chapters-val").textContent = c;
    overlay.querySelector("#bw-spc-val").textContent      = s;
    overlay.querySelector("#bw-estimate").innerHTML =
      `📖 ~${scenes} scenes · ~${w.toLocaleString()} words · ~${etaMin} min writing time`;
  }
  words.oninput = chaps.oninput = spc.oninput = updateEstimate;
  updateEstimate();

  // Generate Outline
  overlay.querySelector("#bw-gen-outline").addEventListener("click", async () => {
    const premise = overlay.querySelector("#bw-premise").value.trim();
    if (premise.length < 50) {
      overlay.querySelector("#bw-premise").style.border = "2px solid #ef4444";
      overlay.querySelector("#bw-premise").placeholder =
        "Please write at least 50 characters describing your story.";
      return;
    }

    const config = {
      project_id:         projectId,
      premise,
      genre:              overlay.querySelector("#bw-genre").value,
      tone:               overlay.querySelector("#bw-tone").value,
      pov:                overlay.querySelector("#bw-pov").value,
      protagonist:        overlay.querySelector("#bw-protagonist").value.trim(),
      antagonist:         overlay.querySelector("#bw-antagonist").value.trim(),
      setting:            overlay.querySelector("#bw-setting").value.trim(),
      target_words:       parseInt(words.value),
      num_chapters:       parseInt(chaps.value),
      scenes_per_chapter: parseInt(spc.value),
    };

    // Transition to outline step
    _showOutlineStep(overlay, config);
  });
}

// ─── Step 2: Outline generation + preview ─────────────────────────────────────

function _showOutlineStep(overlay, config) {
  const card = overlay.querySelector("#bw-card");
  card.innerHTML = `
    <div class="settings-header">
      <h2>✍ Generating Outline…</h2>
      <button class="settings-close" id="bw-close2">✕</button>
    </div>
    <div class="bw-outline-generating">
      <div class="bw-spinner"></div>
      <p>The AI is building your book structure…</p>
      <p class="bw-muted">This usually takes 10–30 seconds.</p>
    </div>`;
  card.querySelector("#bw-close2").onclick = () => overlay.remove();

  // Start the job (will generate outline in background)
  fetch("/api/book/start", {
    method:  "POST",
    headers: { "Content-Type": "application/json" },
    body:    JSON.stringify(config),
  })
  .then(r => r.json())
  .then(data => {
    if (data.job_id) {
      _listenForOutline(overlay, card, data.job_id, config);
    } else {
      card.innerHTML = `<p class="bw-error">Failed to start: ${JSON.stringify(data)}</p>`;
    }
  })
  .catch(err => {
    card.innerHTML = `<p class="bw-error">Error: ${err.message}</p>`;
  });
}

function _listenForOutline(overlay, card, jobId, config) {
  const es = new EventSource(`/api/book/${jobId}/stream`);

  es.onmessage = e => {
    const ev = JSON.parse(e.data);
    if (ev.type === "outline_ready") {
      es.close();
      _showOutlinePreview(overlay, card, jobId, ev.outline, config);
    } else if (ev.type === "error") {
      es.close();
      card.innerHTML = `<p class="bw-error">Outline generation error: ${_esc(ev.message)}</p>`;
    }
  };

  es.onerror = () => {
    es.close();
    // Fallback: poll status
    setTimeout(async () => {
      const resp = await fetch(`/api/book/${jobId}`);
      const data = await resp.json();
      if (data.outline?.acts?.length) {
        _showOutlinePreview(overlay, card, jobId, data.outline, config);
      }
    }, 2000);
  };
}

function _showOutlinePreview(overlay, card, jobId, outline, config) {
  const title    = outline.title || "Untitled Novel";
  let outlineHtml = "";
  let totalScenes = 0;

  for (const act of (outline.acts || [])) {
    outlineHtml += `<div class="bw-act-header">${_esc(act.name)}</div>`;
    for (const chap of (act.chapters || [])) {
      outlineHtml += `
        <div class="bw-chap-header">${_esc(chap.title)}</div>
        <ul class="bw-scene-list">`;
      for (const scene of (chap.scenes || [])) {
        totalScenes++;
        outlineHtml += `<li>${_esc(typeof scene === "string" ? scene : JSON.stringify(scene))}</li>`;
      }
      outlineHtml += `</ul>`;
    }
  }

  card.innerHTML = `
    <div class="settings-header">
      <h2>📖 "${_esc(title)}"</h2>
      <button class="settings-close" id="bw-close3">✕</button>
    </div>
    <p class="bw-subtitle">
      ${totalScenes} scenes across ${(outline.acts||[]).reduce((n,a)=>n+(a.chapters||[]).length,0)} chapters.
      Review the outline — once you start, Quill will write every scene automatically.
    </p>
    <div class="bw-outline-scroll">${outlineHtml}</div>
    <div class="modal-actions">
      <button class="btn-cancel" id="bw-back">← Reconfigure</button>
      <button class="btn-submit bw-start-btn" id="bw-start-write">✍ Start Writing</button>
    </div>`;

  card.querySelector("#bw-close3").onclick         = () => overlay.remove();
  card.querySelector("#bw-back").onclick           = () => overlay.remove(); // simplest: re-open
  card.querySelector("#bw-start-write").addEventListener("click", () => {
    _showDashboard(overlay, card, jobId, outline, config, totalScenes);
  });
}

// ─── Step 3: Writing dashboard ────────────────────────────────────────────────

function _showDashboard(overlay, card, jobId, outline, config, totalScenes) {
  // Expand the modal to full-ish size
  card.className = "modal-card bw-card bw-dashboard-card";
  card.innerHTML = `
    <div class="bw-dash-layout">
      <!-- Left: chapter tree -->
      <div class="bw-tree" id="bw-tree">
        <div class="bw-tree-title">📖 Structure</div>
        <div id="bw-tree-body"></div>
      </div>

      <!-- Right: main panel -->
      <div class="bw-main">
        <div class="bw-dash-header">
          <div>
            <h2 id="bw-book-title">${_esc(outline.title || "Untitled Novel")}</h2>
            <span class="bw-status-badge" id="bw-status-badge">Writing…</span>
          </div>
          <button class="settings-close" id="bw-close-dash">✕</button>
        </div>

        <!-- Stats bar -->
        <div class="bw-stats">
          <div class="bw-stat"><div class="bw-stat-val" id="bw-words-written">0</div><div class="bw-stat-label">words</div></div>
          <div class="bw-stat"><div class="bw-stat-val" id="bw-scenes-done">0/${totalScenes}</div><div class="bw-stat-label">scenes</div></div>
          <div class="bw-stat"><div class="bw-stat-val" id="bw-pct">0%</div><div class="bw-stat-label">complete</div></div>
          <div class="bw-stat"><div class="bw-stat-val" id="bw-eta">—</div><div class="bw-stat-label">est. remaining</div></div>
        </div>

        <!-- Progress bar -->
        <div class="bw-progress-track">
          <div class="bw-progress-fill" id="bw-progress-fill" style="width:0%"></div>
        </div>

        <!-- Current scene label -->
        <div class="bw-current-label" id="bw-current-label">Preparing…</div>

        <!-- Live streaming scene text -->
        <div class="bw-live-scene" id="bw-live-scene"></div>

        <!-- Controls -->
        <div class="bw-controls">
          <button class="btn-secondary" id="bw-pause-btn">⏸ Pause</button>
          <button class="btn-cancel"    id="bw-cancel-btn">✕ Cancel</button>
          <span class="bw-muted" id="bw-done-msg" style="display:none">
            ✓ Book complete! Reload the binder to see all scenes.
          </span>
        </div>
      </div>
    </div>`;

  // ── Build chapter tree ────────────────────────────────────────────────────
  const treeBody = card.querySelector("#bw-tree-body");
  const treeNodes = {};  // scene_key → element

  for (const act of (outline.acts || [])) {
    const actEl = document.createElement("div");
    actEl.className = "bw-tree-act";
    actEl.textContent = act.name;
    treeBody.appendChild(actEl);
    for (const [ci, chap] of (act.chapters || []).entries()) {
      const chapEl = document.createElement("div");
      chapEl.className = "bw-tree-chap";
      chapEl.textContent = chap.title;
      treeBody.appendChild(chapEl);
      for (const [si] of (chap.scenes || []).entries()) {
        const nodeEl = document.createElement("div");
        nodeEl.className = "bw-tree-scene";
        nodeEl.textContent = `· Scene ${si + 1}`;
        nodeEl.dataset.key = `${ci+1}_${si+1}`;
        treeBody.appendChild(nodeEl);
        treeNodes[`${ci+1}_${si+1}`] = nodeEl;
      }
    }
  }

  // ── Wire controls ─────────────────────────────────────────────────────────
  card.querySelector("#bw-close-dash").onclick = () => overlay.remove();

  const pauseBtn  = card.querySelector("#bw-pause-btn");
  const cancelBtn = card.querySelector("#bw-cancel-btn");
  let paused = false;

  pauseBtn.addEventListener("click", async () => {
    if (!paused) {
      await fetch(`/api/book/${jobId}/pause`, { method: "POST" });
      pauseBtn.textContent = "▶ Resume";
      paused = true;
    } else {
      await fetch(`/api/book/${jobId}/resume`, { method: "POST" });
      pauseBtn.textContent = "⏸ Pause";
      paused = false;
    }
  });

  cancelBtn.addEventListener("click", async () => {
    if (!confirm("Cancel book generation? Scenes written so far will be kept.")) return;
    await fetch(`/api/book/${jobId}/cancel`, { method: "POST" });
    cancelBtn.disabled = true;
    cancelBtn.textContent = "Cancelled";
  });

  // ── SSE connection ────────────────────────────────────────────────────────
  const liveEl     = card.querySelector("#bw-live-scene");
  const labelEl    = card.querySelector("#bw-current-label");
  const fillEl     = card.querySelector("#bw-progress-fill");
  const wordsEl    = card.querySelector("#bw-words-written");
  const scenesDoneEl = card.querySelector("#bw-scenes-done");
  const pctEl      = card.querySelector("#bw-pct");
  const etaEl      = card.querySelector("#bw-eta");
  const statusBadge = card.querySelector("#bw-status-badge");
  const doneMsg    = card.querySelector("#bw-done-msg");

  let currentKey    = null;
  let scenesDone    = 0;
  let totalScenesCt = totalScenes;

  function updateProgress(done, total, totalWords) {
    const pct = total > 0 ? Math.round((done / total) * 100) : 0;
    fillEl.style.width         = pct + "%";
    wordsEl.textContent        = totalWords.toLocaleString();
    scenesDoneEl.textContent   = `${done}/${total}`;
    pctEl.textContent          = pct + "%";
  }

  const es = new EventSource(`/api/book/${jobId}/stream`);

  es.onmessage = e => {
    const ev = JSON.parse(e.data);

    switch (ev.type) {
      case "snapshot":
        scenesDone    = ev.job.done_scenes;
        totalScenesCt = ev.job.total_scenes || totalScenes;
        updateProgress(scenesDone, totalScenesCt, ev.job.total_words);
        break;

      case "total_scenes":
        totalScenesCt = ev.total;
        break;

      case "scene_start":
        currentKey = `${ev.chapter}_${ev.scene}`;
        labelEl.textContent = `Writing: Ch.${ev.chapter} Scene ${ev.scene} — ${ev.beat.slice(0,80)}…`;
        liveEl.textContent  = "";
        // Highlight tree node
        Object.values(treeNodes).forEach(n => n.classList.remove("bw-active"));
        if (treeNodes[currentKey]) treeNodes[currentKey].classList.add("bw-active");
        break;

      case "token":
        liveEl.textContent += ev.text;
        // auto-scroll
        liveEl.scrollTop = liveEl.scrollHeight;
        break;

      case "scene_done":
        scenesDone = ev.done;
        updateProgress(ev.done, ev.total, ev.total_words);
        // Mark tree done
        if (treeNodes[currentKey]) {
          treeNodes[currentKey].classList.remove("bw-active");
          treeNodes[currentKey].classList.add("bw-done");
          treeNodes[currentKey].textContent = "✓ " + treeNodes[currentKey].textContent.replace(/^[·✓]\s/, "");
        }
        // ETA estimate
        if (ev.done > 0 && ev.total > 0) {
          const remaining = ev.total - ev.done;
          const avgPerScene = 50; // seconds estimate
          const etaMin = Math.round((remaining * avgPerScene) / 60);
          etaEl.textContent = etaMin > 0 ? `~${etaMin}m` : "<1m";
        }
        break;

      case "paused":
        statusBadge.textContent = "Paused";
        statusBadge.className   = "bw-status-badge bw-badge--paused";
        break;

      case "resumed":
        statusBadge.textContent = "Writing…";
        statusBadge.className   = "bw-status-badge";
        break;

      case "book_done":
        es.close();
        statusBadge.textContent = "Complete!";
        statusBadge.className   = "bw-status-badge bw-badge--done";
        labelEl.textContent     = `✓ Finished! "${ev.title}" — ${ev.total_words.toLocaleString()} words across ${ev.total_scenes} scenes.`;
        fillEl.style.width      = "100%";
        liveEl.textContent      = "";
        pauseBtn.disabled       = true;
        cancelBtn.style.display = "none";
        doneMsg.style.display   = "";
        card.querySelector("#bw-close-dash").style.display = "block";
        // Reload binder if project is open
        if (window.__binderRefresh) window.__binderRefresh();
        break;

      case "cancelled":
        es.close();
        statusBadge.textContent = "Cancelled";
        statusBadge.className   = "bw-status-badge bw-badge--error";
        cancelBtn.style.display = "none";
        pauseBtn.disabled       = true;
        card.querySelector("#bw-close-dash").style.display = "block";
        if (window.__binderRefresh) window.__binderRefresh();
        break;

      case "error":
        es.close();
        statusBadge.textContent = "Error";
        statusBadge.className   = "bw-status-badge bw-badge--error";
        labelEl.textContent     = `Error: ${ev.message}`;
        break;
    }
  };

  es.onerror = () => {
    // Reconnect silently — server might have restarted
    console.warn("[Quill] Book writer SSE disconnected — will retry");
  };
}

// ─── Utilities ────────────────────────────────────────────────────────────────

function _esc(str) {
  return String(str)
    .replace(/&/g,"&amp;").replace(/</g,"&lt;")
    .replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}

function _titleCase(s) {
  return s.split(" ").map(w => w[0].toUpperCase() + w.slice(1)).join(" ");
}
