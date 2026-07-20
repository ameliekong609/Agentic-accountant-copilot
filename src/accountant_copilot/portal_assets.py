"""Static HTML/JavaScript asset for the local portal."""
from __future__ import annotations



_INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Workpaper Portal</title>
  <style>
    :root {
      color-scheme: light;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #f4f7f8;
      color: #222b35;
      font-synthesis: none;
      text-rendering: optimizeLegibility;
      -webkit-font-smoothing: antialiased;
    }
    * { box-sizing: border-box; }
    body { margin: 0; background: #f4f7f8; }
    button, input, select, textarea { font: inherit; }
    .shell { padding: 24px; display: grid; gap: 16px; }
    header { display: flex; justify-content: space-between; align-items: flex-end; gap: 20px; }
    h1 { margin: 0; font-size: 28px; line-height: 1.1; letter-spacing: 0; }
    .eyebrow { margin: 0 0 5px; color: #647383; font-weight: 800; font-size: 12px; text-transform: uppercase; letter-spacing: 0; }
    .muted { color: #677586; }
    .grid { display: grid; grid-template-columns: 420px minmax(0, 1fr); gap: 16px; align-items: start; }
    .panel { border: 1px solid #d7e1e8; border-radius: 8px; background: white; overflow: hidden; }
    .full-width { grid-column: 1 / -1; }
    .panel-body { padding: 16px; display: grid; gap: 14px; }
    .panel-title { padding: 14px 16px; border-bottom: 1px solid #e4eaef; display: flex; align-items: center; justify-content: space-between; gap: 12px; }
    .panel-title h2 { margin: 0; font-size: 16px; }
    .stack { display: grid; gap: 10px; }
    .row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
    .check-row { display: flex; gap: 8px; align-items: flex-start; color: #536273; font-size: 13px; line-height: 1.35; }
    .check-row input { margin-top: 2px; }
    .button, button, label.upload {
      display: inline-flex; align-items: center; justify-content: center; gap: 8px;
      min-height: 40px; border-radius: 8px; border: 1px solid #cbd7df; background: white;
      padding: 0 14px; font-weight: 850; color: #24313d; cursor: pointer; text-decoration: none;
    }
    button.primary, .button.primary, label.upload { background: #107c68; border-color: #107c68; color: white; }
    button:disabled { opacity: 0.55; cursor: not-allowed; }
    input[type="text"], select {
      width: 100%; min-height: 40px; border: 1px solid #cbd7df; border-radius: 8px; padding: 0 12px; background: white;
    }
    .summary { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; }
    .metric { border: 1px solid #d7e1e8; border-radius: 8px; background: white; padding: 12px 14px; }
    .metric strong { display: block; font-size: 24px; }
    .metric span { color: #6a7785; font-size: 13px; font-weight: 700; }
    .steps { display: grid; grid-template-columns: repeat(6, minmax(130px, 1fr)); gap: 10px; }
    .step { border: 1px solid #d7e1e8; border-radius: 8px; background: #fff; padding: 12px; display: grid; gap: 8px; min-height: 92px; }
    .step strong { font-size: 14px; }
    .step span.description { color: #667585; font-size: 12px; line-height: 1.35; }
    .status { width: fit-content; border-radius: 999px; padding: 4px 9px; font-size: 12px; font-weight: 900; background: #edf2f6; color: #4c5b6a; }
    .status.complete, .status.completed { background: #e1f3ec; color: #0b6c58; }
    .status.running { background: #fff1ce; color: #835900; }
    .status.failed { background: #fde7e7; color: #a0333d; }
    .table-wrap { max-height: 360px; overflow: auto; border: 1px solid #e3e9ee; border-radius: 8px; }
    table { width: 100%; border-collapse: collapse; }
    th, td { border-bottom: 1px solid #e6edf1; padding: 9px 10px; text-align: left; vertical-align: top; font-size: 13px; }
    th { position: sticky; top: 0; background: #f0f4f6; color: #5f6c7a; text-transform: uppercase; font-size: 11px; letter-spacing: 0; }
    .output { display: grid; gap: 12px; }
    .callout { border: 1px solid #cfe2dc; background: #f4fbf8; border-radius: 8px; padding: 14px; }
    .callout.warn { border-color: #edd28d; background: #fff9e9; }
    .callout.fail { border-color: #efb5b9; background: #fff3f3; }
    .milestones { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; }
    .milestone { border: 1px solid #e0e8ee; border-radius: 8px; padding: 10px 12px; background: #fff; display: grid; gap: 4px; }
    .milestone.complete { border-color: #b8ddcf; background: #f4fbf8; }
    .milestone strong { font-size: 13px; }
    .milestone span { color: #687688; font-size: 12px; line-height: 1.35; }
    .findings { display: grid; gap: 8px; }
    .finding { border: 1px solid #e0e8ee; border-radius: 8px; padding: 10px 12px; background: #fff; }
    .finding strong { display: block; margin-bottom: 3px; }
    .notes-grid { display: grid; grid-template-columns: minmax(260px, 340px) minmax(640px, 1fr); gap: 16px; align-items: start; }
    .note-list { max-height: 620px; overflow: auto; display: grid; gap: 8px; }
    .note-item {
      width: 100%; min-height: auto; display: grid; gap: 6px; justify-content: stretch; text-align: left;
      border-color: #dde6ec; background: #fff; padding: 10px 12px;
    }
    .note-item.selected { border-color: #107c68; box-shadow: 0 0 0 2px rgba(16, 124, 104, 0.12); }
    .note-item .note-top { display: flex; justify-content: space-between; gap: 8px; align-items: center; }
    .note-item strong { overflow-wrap: anywhere; }
    .note-detail { border: 1px solid #d7e1e8; border-radius: 8px; background: #fbfdfe; padding: 18px; display: grid; gap: 16px; min-height: 520px; }
    .note-detail h3 { margin: 0; font-size: 20px; line-height: 1.2; }
    .note-meta { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; }
    .note-metric { border: 1px solid #dfe8ee; background: white; border-radius: 8px; padding: 9px 10px; }
    .note-metric span { display: block; color: #687688; font-size: 11px; font-weight: 850; text-transform: uppercase; }
    .note-metric strong { display: block; margin-top: 4px; overflow-wrap: anywhere; }
    .story-block { display: grid; gap: 5px; }
    .story-block h4 { margin: 0; font-size: 13px; color: #667585; text-transform: uppercase; letter-spacing: 0; }
    .story-block p { margin: 0; line-height: 1.45; }
    .evidence-links { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
    .evidence-link {
      border: 1px solid #dce5eb; border-radius: 8px; background: white; padding: 10px 12px; text-decoration: none; color: #25313d;
      display: grid; gap: 4px;
    }
    .table-link { color: #0f6e5d; font-weight: 850; text-decoration: none; }
    .table-link:hover { text-decoration: underline; }
    .evidence-link strong { overflow-wrap: anywhere; }
    .evidence-link span { color: #687688; font-size: 12px; }
    .pill { display: inline-flex; width: fit-content; border-radius: 999px; padding: 3px 8px; background: #edf2f6; color: #4c5b6a; font-size: 11px; font-weight: 900; }
    .pill.ready { background: #e1f3ec; color: #0b6c58; }
    .pill.needs_attention { background: #fff1ce; color: #835900; }
    .pill.not_posted, .pill.excluded { background: #eef2f5; color: #556373; }
    pre { margin: 0; overflow: auto; white-space: pre-wrap; font-size: 12px; color: #566472; }
    .small { font-size: 12px; }
    @media (max-width: 980px) {
      .shell { padding: 14px; }
      header { align-items: flex-start; flex-direction: column; }
      .grid { grid-template-columns: 1fr; }
      .summary, .steps, .milestones { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .notes-grid { grid-template-columns: 1fr; }
      .note-meta { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .evidence-links { grid-template-columns: 1fr; }
    }
    @media (max-width: 560px) {
      .summary, .steps, .milestones { grid-template-columns: 1fr; }
      .note-meta { grid-template-columns: 1fr; }
      .row { align-items: stretch; }
      .button, button, label.upload { width: 100%; }
    }
  </style>
</head>
<body>
  <div class="shell">
    <header>
      <div>
        <p class="eyebrow">Tenet Legacy</p>
        <h1>Hi, I’m Tessa, your AI workpaper assistant.</h1>
        <p class="muted">I’ll help prepare the financial-statement workpaper. Upload the client file pack, and I’ll prepare the Excel workbook with row stories and review notes.</p>
      </div>
      <div class="row">
        <button id="demoBtn">Load demo</button>
        <button id="refreshBtn">Refresh status</button>
        <button id="resetBtn">Start over</button>
      </div>
    </header>

    <section class="summary" id="summary"></section>

    <section class="grid">
      <aside class="panel">
        <div class="panel-title"><h2>Client files</h2><span class="status" id="fileCount">0 files</span></div>
        <div class="panel-body">
          <label class="upload" id="folderUploadLabel">
            Upload folder
            <input id="fileInput" hidden type="file" multiple webkitdirectory directory mozdirectory />
          </label>
          <p class="muted small" id="uploadStatus">No folder uploaded yet.</p>
          <div class="stack">
            <label class="small muted" for="priorFs">Prior-year financial statement</label>
            <select id="priorFs"><option value="">Auto detect</option></select>
          </div>
          <div class="stack">
            <label class="small muted">Target financial year</label>
            <div class="row" style="flex-wrap: nowrap;">
              <input id="fyStart" type="text" placeholder="Start, e.g. 2024-07-01" />
              <input id="fyEnd" type="text" placeholder="End, e.g. 2025-06-30" />
            </div>
          </div>
          <label class="check-row">
            <input id="allowCache" type="checkbox" />
            <span>Reuse previous AI reading when the same files were already read. Leave off for a fresh run.</span>
          </label>
          <button class="primary" id="startBtn" title="Upload a folder first">Prepare Excel workpaper</button>
          <p class="muted small" id="clientFolder"></p>
        </div>
      </aside>

      <main class="stack">
        <section class="steps" id="steps"></section>
        <section class="panel output">
          <div class="panel-title"><h2>Workpaper status</h2><span class="status" id="jobStatus">Idle</span></div>
          <div class="panel-body">
            <div id="outputMessage" class="callout">Upload files, then prepare the workpaper.</div>
            <div class="milestones" id="milestones"></div>
            <div class="row" id="downloadRow" style="display:none;">
              <a class="button primary" href="/download/workbook">Download Excel workbook</a>
              <a class="button" href="/download/summary">Download summary</a>
            </div>
            <div class="findings" id="findings"></div>
          </div>
        </section>
        <section class="panel" id="evidencePreviewPanel">
          <div class="panel-title"><h2 id="evidencePanelTitle">Uploaded files</h2><span class="muted small" id="uploadLabel"></span></div>
          <div class="table-wrap">
            <table>
              <thead id="filesTableHead"><tr><th>File</th><th>Size</th><th>Modified</th></tr></thead>
              <tbody id="filesTable"></tbody>
            </table>
          </div>
        </section>
      </main>

      <section class="panel full-width" id="movementNotesPanel" style="display:none;">
        <div class="panel-title">
          <h2>Movement stories</h2>
          <span class="muted small" id="movementNotesCount"></span>
        </div>
        <div class="panel-body">
          <p class="muted small">Use this beside Excel. Search the Note ID from the TB Bridge, then read the row story and open the supporting evidence.</p>
          <input id="noteSearch" type="text" placeholder="Search note ID, account, amount, column, or evidence..." />
          <div class="notes-grid">
            <div class="note-list" id="movementNotesList"></div>
            <div class="note-detail" id="movementNoteDetail"></div>
          </div>
        </div>
      </section>
    </section>
  </div>

  <script>
    const state = { polling: null, latestData: null, selectedNoteId: "" };
    const $ = (id) => document.getElementById(id);
    const money = (n) => Number(n || 0).toLocaleString("en-AU");
    function statusClass(value) { return `status ${value || ""}`; }
    async function api(path, options = {}) {
      const response = await fetch(path, options);
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || `Request failed: ${response.status}`);
      return data;
    }
    async function uploadFiles(files, input) {
      const uploadStatus = $("uploadStatus");
      if (!files || !files.length) {
        uploadStatus.textContent = "No files were selected. Choose the client folder that contains the source documents.";
        return;
      }
      uploadStatus.textContent = `Uploading ${files.length.toLocaleString("en-AU")} file(s)...`;
      $("startBtn").disabled = true;
      const form = new FormData();
      Array.from(files).forEach((file) => form.append("files", file, file.webkitRelativePath || file.name));
      try {
        const result = await api("/api/upload", { method: "POST", body: form });
        uploadStatus.textContent = `Uploaded ${(result.extracted_files || files.length).toLocaleString("en-AU")} file(s). Ready to prepare the workpaper.`;
        await refresh();
      } finally {
        if (input) input.value = "";
      }
    }
    async function refresh() {
      const data = await api("/api/state");
      render(data);
      if (data.job?.status === "running" && !state.polling) {
        state.polling = setInterval(refresh, 5000);
      }
      if (data.job?.status !== "running" && state.polling) {
        clearInterval(state.polling);
        state.polling = null;
      }
    }
    function render(data) {
      state.latestData = data;
      $("summary").innerHTML = [
        ["Files", data.file_count || 0],
        ["Evidence read", data.counts?.documents || 0],
        ["Workpaper rows", data.counts?.matrix_rows || 0],
        ["Row stories", data.counts?.movement_notes || 0],
      ].map(([label, value]) => `<div class="metric"><strong>${money(value)}</strong><span>${label}</span></div>`).join("");
      $("fileCount").textContent = `${data.file_count || 0} files`;
      $("clientFolder").textContent = data.client_folder || "";
      $("uploadLabel").textContent = data.upload_label || "";
      if (data.file_count > 0 && !data.job) {
        $("uploadStatus").textContent = `${data.file_count.toLocaleString("en-AU")} file(s) uploaded. Ready to prepare the workpaper.`;
      } else if (!data.client_folder && !data.job) {
        $("uploadStatus").textContent = "No folder uploaded yet.";
      }
      $("jobStatus").textContent = data.job?.status || "Idle";
      $("jobStatus").className = statusClass(data.job?.status || "");
      $("steps").innerHTML = (data.stages || []).map((step) => `
        <div class="step">
          <span class="${statusClass(step.status)}">${step.status}</span>
          <strong>${step.label}</strong>
          <span class="description">${escapeHtml(step.description || "")}</span>
        </div>`).join("");
      $("milestones").innerHTML = (data.milestones || []).map((item) => `
        <div class="milestone ${escapeAttr(item.status || "")}">
          <strong>${escapeHtml(item.label || "")}</strong>
          <span>${escapeHtml(item.description || "")}</span>
        </div>`).join("");
      const evidenceRows = data.evidence_index || [];
      if (evidenceRows.length) {
        $("evidencePanelTitle").textContent = "Evidence index";
        $("uploadLabel").textContent = `${evidenceRows.length.toLocaleString("en-AU")} document${evidenceRows.length === 1 ? "" : "s"} read`;
        $("filesTableHead").innerHTML = `<tr><th>Original file</th><th>Tessa name</th><th>Type</th><th>Status</th><th>PDF</th></tr>`;
        $("filesTable").innerHTML = evidenceRows.map((row) => `
          <tr>
            <td>${escapeHtml(row.original_file_name || "")}</td>
            <td><strong>${escapeHtml(row.display_name || "")}</strong></td>
            <td>${escapeHtml(formatLabel(row.document_type || ""))}</td>
            <td><span class="pill ${escapeAttr(row.entity_relevance || "")}">${escapeHtml(formatLabel(row.entity_relevance || "read"))}</span></td>
            <td>${row.open_url ? `<a class="table-link" target="_blank" rel="noreferrer" href="${escapeAttr(row.open_url)}">Open</a>` : ""}</td>
          </tr>`).join("");
      } else {
        $("evidencePanelTitle").textContent = "Uploaded files";
        $("uploadLabel").textContent = data.upload_label || "";
        $("filesTableHead").innerHTML = `<tr><th>File</th><th>Size</th><th>Modified</th></tr>`;
        $("filesTable").innerHTML = (data.files || []).slice(0, 80).map((file) => `
          <tr>
            <td>${escapeHtml(file.relative_path || file.name)}</td>
            <td>${((file.size || 0) / 1024).toLocaleString("en-AU", { maximumFractionDigits: 1 })} KB</td>
            <td>${file.modified_at ? new Date(file.modified_at).toLocaleString() : ""}</td>
          </tr>`).join("") || `<tr><td colspan="3" class="muted">No files selected.</td></tr>`;
      }
      const prior = $("priorFs");
      const old = prior.value;
      prior.innerHTML = `<option value="">Auto detect</option>` + (data.prior_fs_candidates || []).map((item) => `
        <option value="${escapeAttr(item.path)}">${escapeHtml(item.relative_path || item.name)}</option>`).join("");
      if (old) prior.value = old;
      const running = data.job?.status === "running";
      $("startBtn").disabled = running || !(data.client_folder);
      $("startBtn").title = running ? "Tessa is already preparing a workpaper" : (data.client_folder ? "Prepare the Excel workpaper" : "Upload a folder first");
      $("demoBtn").disabled = running || !data.demo_available;
      $("demoBtn").title = data.demo_available ? "Replay the latest completed demo workpaper" : "No demo snapshot is available yet";
      $("resetBtn").disabled = running;
      const message = $("outputMessage");
      const download = $("downloadRow");
      const turingStatus = data.turing?.status || "";
      if (data.job?.status === "completed") {
        message.className = "callout";
        const attention = data.progress?.status === "needs_attention" || (data.turing?.findings || []).length > 0;
        message.innerHTML = attention
          ? `<strong>Tessa prepared the workbook and found review notes.</strong><br/>The workbook is available. Use Movement stories and Review notes beside Excel.`
          : `<strong>Final workbook ready.</strong><br/>Review status: ${escapeHtml(turingStatus || "not available")}.`;
        download.style.display = data.artifacts?.workbook_exists ? "flex" : "none";
      } else if (data.job?.status === "failed") {
        message.className = "callout fail";
        const restored = data.progress?.last_good_restored;
        message.innerHTML = restored
          ? `<strong>Tessa could not refresh the workbook.</strong><br/>The engineering checker is reviewing it. Previous workbook kept.`
          : `<strong>Tessa could not refresh the workbook.</strong><br/>The engineering checker is reviewing it.`;
        download.style.display = data.artifacts?.workbook_exists ? "flex" : "none";
      } else if (running) {
        message.className = "callout warn";
        const draftReady = !!data.artifacts?.workbook_exists;
        const elapsed = data.elapsed_label ? ` Running for ${escapeHtml(data.elapsed_label)}.` : "";
        message.innerHTML = draftReady
          ? `<strong>Draft workbook available.</strong><br/>Senior review is still checking it.${elapsed} Full client packs often take ${escapeHtml(data.expected_duration_label || "60-90 minutes")}. You can close this page and come back later.`
          : `<strong>Tessa is preparing the workpaper.</strong><br/>${escapeHtml(data.progress_message || "This usually takes 60-90 minutes for a full client pack. You can close this page and come back later.")}`;
        download.style.display = draftReady ? "flex" : "none";
      } else {
        message.className = "callout";
        message.textContent = "Upload files, then prepare the workpaper.";
        download.style.display = data.artifacts?.workbook_exists ? "flex" : "none";
      }
      const findings = data.turing?.findings || [];
      const internalNotes = data.turing?.internal_note_count || 0;
      $("findings").innerHTML = findings.length
        ? findings.slice(0, 8).map((finding) => `
          <div class="finding">
            <strong>${escapeHtml(finding.title || finding.category || "Review note")}</strong>
            <span>${escapeHtml(finding.body || finding.message || "")}</span>
            ${finding.check ? `<span class="muted small">${escapeHtml(finding.check)}</span>` : ""}
          </div>`).join("")
        : internalNotes
          ? `<div class="finding"><strong>Review notes handled internally</strong><span>Tessa kept ${internalNotes.toLocaleString("en-AU")} low-risk review note${internalNotes === 1 ? "" : "s"} in the audit trail.</span></div>`
          : "";
      renderMovementNotes(data.movement_notes || []);
    }
    function noteSearchBlob(note) {
      return [
        note.note_id, note.tb_row, note.account_name, note.statement_section, note.statement_group, note.status,
        note.tb_column, note.opening_balance, note.closing_balance, note.main_amount, note.other_amounts,
        note.explanation, note.calculation, note.evidence_summary, note.check_hint,
        ...(note.context_stories || []),
        ...(note.evidence_docs || []).flatMap((doc) => [doc.display_name, doc.document_type, doc.period, doc.document_id]),
      ].join(" ").toLowerCase();
    }
    function renderMovementNotes(notes) {
      const panel = $("movementNotesPanel");
      const count = $("movementNotesCount");
      const list = $("movementNotesList");
      const detail = $("movementNoteDetail");
      if (!notes.length) {
        panel.style.display = "none";
        $("evidencePreviewPanel").style.display = "block";
        return;
      }
      panel.style.display = "block";
      $("evidencePreviewPanel").style.display = "block";
      count.textContent = `${notes.length.toLocaleString("en-AU")} row notes`;
      const query = ($("noteSearch").value || "").trim().toLowerCase();
      const filtered = query ? notes.filter((note) => noteSearchBlob(note).includes(query)) : notes;
      if (!filtered.some((note) => note.note_id === state.selectedNoteId)) {
        state.selectedNoteId = filtered[0]?.note_id || "";
      }
      const selected = filtered.find((note) => note.note_id === state.selectedNoteId) || filtered[0];
      list.innerHTML = filtered.slice(0, 120).map((note) => `
        <button class="note-item ${note.note_id === selected?.note_id ? "selected" : ""}" data-note-id="${escapeAttr(note.note_id)}">
          <span class="note-top">
            <span class="pill ${escapeAttr(note.status || "")}">${escapeHtml(note.status || "review")}</span>
            <span class="muted small">${escapeHtml(note.note_id || "")}${note.tb_row ? ` · row ${escapeHtml(note.tb_row)}` : ""}</span>
          </span>
          <strong>${escapeHtml(note.account_name || "Unnamed row")}</strong>
          <span class="muted small">${escapeHtml(note.tb_column || "No movement")}</span>
        </button>`).join("") || `<p class="muted">No movement notes match this search.</p>`;
      list.querySelectorAll("[data-note-id]").forEach((button) => {
        button.addEventListener("click", () => {
          state.selectedNoteId = button.getAttribute("data-note-id") || "";
          renderMovementNotes(state.latestData?.movement_notes || []);
        });
      });
      if (!selected) {
        detail.innerHTML = `<p class="muted">Search for a Note ID from Excel, e.g. R006.</p>`;
        return;
      }
      const evidenceLinks = (selected.evidence_docs || []).map((doc) => `
        <a class="evidence-link" target="_blank" rel="noreferrer" href="${escapeAttr(doc.open_url || "#")}">
          <strong>${escapeHtml(doc.display_name || doc.document_id || "Open source")}</strong>
          <span>${escapeHtml([doc.document_type, doc.period].filter(Boolean).join(" · "))}</span>
        </a>`).join("");
      const stories = (selected.context_stories || []).map((story) => `<li>${escapeHtml(story)}</li>`).join("");
      const rowTutorial = selected.row_tutorial || null;
      const movementRows = rowTutorial?.movements?.length
        ? `<div class="table-wrap" style="max-height: 300px;"><table><thead><tr><th>Movement column</th><th>Why</th><th>Amount</th></tr></thead><tbody>${rowTutorial.movements.map((movement) => `
            <tr>
              <td>${escapeHtml(movement.column || "")}</td>
              <td>${escapeHtml(movement.explanation || "")}</td>
              <td>${escapeHtml(movement.amount || "")}</td>
            </tr>`).join("")}</tbody></table></div>`
        : `<p class="muted">No FY movement was identified for this row. Tessa is carrying the opening balance forward unless the accountant adds an adjustment.</p>`;
      const bridge = selected.profit_bridge || null;
      const bridgeRows = bridge?.rows?.length
        ? `<div class="table-wrap" style="max-height: 280px;"><table><thead><tr><th>P&L row</th><th>Effect</th><th>Amount</th></tr></thead><tbody>${bridge.rows.map((row) => `
            <tr>
              <td>${escapeHtml(row.account_name || "")}</td>
              <td>${escapeHtml(row.effect || "")}</td>
              <td>${escapeHtml(row.amount || "")}</td>
            </tr>`).join("")}</tbody></table></div>`
        : "";
      detail.innerHTML = `
        <div>
          <span class="pill ${escapeAttr(selected.status || "")}">${escapeHtml(selected.status || "review")}</span>
          <h3>${escapeHtml(selected.account_name || "Movement note")}</h3>
          <p class="muted small">${escapeHtml(selected.note_id || "")}${selected.tb_row ? ` · Excel row ${escapeHtml(selected.tb_row)}` : ""}${selected.statement_group ? ` · ${escapeHtml(selected.statement_group)}` : ""}</p>
        </div>
        <div class="note-meta">
          <div class="note-metric"><span>Opening</span><strong>${escapeHtml(selected.opening_balance || "-")}</strong></div>
          <div class="note-metric"><span>Movement</span><strong>${escapeHtml(selected.tb_column || "-")}</strong></div>
          <div class="note-metric"><span>Main amount</span><strong>${escapeHtml(selected.main_amount || "-")}</strong></div>
          <div class="note-metric"><span>Closing</span><strong>${escapeHtml(selected.closing_balance || "-")}</strong></div>
        </div>
        <div class="story-block">
          <h4>What happened</h4>
          <p>${escapeHtml(selected.explanation || "No explanation available.")}</p>
        </div>
        ${rowTutorial ? `<div class="story-block">
          <h4>${escapeHtml(rowTutorial.title || "How to read this row")}</h4>
          <p>${escapeHtml(rowTutorial.tutorial || "")}</p>
          ${rowTutorial.formula ? `<p><strong>${escapeHtml(rowTutorial.formula)}</strong></p>` : ""}
          ${movementRows}
        </div>` : ""}
        <div class="story-block">
          <h4>Calculation</h4>
          <p>${escapeHtml(selected.calculation || "No calculation note available.")}</p>
        </div>
        ${bridge ? `<div class="story-block">
          <h4>${escapeHtml(bridge.title || "Book-profit bridge")}</h4>
          <p>${escapeHtml(bridge.summary || "")}</p>
          ${bridge.calculation ? `<p><strong>${escapeHtml(bridge.calculation)}</strong></p>` : ""}
          ${bridgeRows}
        </div>` : ""}
        <div class="story-block">
          <h4>What to check</h4>
          <p>${escapeHtml(selected.check_hint || "Review linked evidence if this row is selected.")}</p>
        </div>
        ${selected.evidence_summary ? `<div class="story-block"><h4>Evidence note</h4><p>${escapeHtml(selected.evidence_summary)}</p></div>` : ""}
        ${stories ? `<div class="story-block"><h4>Supporting context</h4><ul>${stories}</ul></div>` : ""}
        <div class="story-block">
          <h4>Open evidence</h4>
          <div class="evidence-links">${evidenceLinks || `<p class="muted">No direct source link mapped to this note yet.</p>`}</div>
        </div>
        ${selected.other_amounts ? `<div class="story-block"><h4>Searchable amounts</h4><p>${escapeHtml(selected.other_amounts)}</p></div>` : ""}
      `;
    }
    function escapeHtml(value) {
      return String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
    }
    function escapeAttr(value) { return escapeHtml(value).replace(/`/g, "&#96;"); }
    function formatLabel(value) {
      return String(value || "").replace(/_/g, " ").replace(/\b\w/g, (char) => char.toUpperCase());
    }
    $("fileInput").addEventListener("change", (event) => uploadFiles(event.target.files, event.target).catch((error) => {
      $("uploadStatus").textContent = error.message;
      alert(error);
    }));
    $("startBtn").addEventListener("click", () => api("/api/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        prior_fs_file: $("priorFs").value,
        fy_start: $("fyStart").value,
        fy_end: $("fyEnd").value,
        allow_cache: $("allowCache").checked
      })
    }).then(refresh).catch(alert));
    $("refreshBtn").addEventListener("click", () => refresh().catch(alert));
    $("demoBtn").addEventListener("click", () => api("/api/demo", { method: "POST" }).then(refresh).catch(alert));
    $("resetBtn").addEventListener("click", () => {
      if (!confirm("Clear uploaded files and generated workpaper results?")) return;
      api("/api/reset", { method: "POST" }).then(refresh).catch(alert);
    });
    $("noteSearch").addEventListener("input", () => renderMovementNotes(state.latestData?.movement_notes || []));
    refresh().catch((error) => { $("outputMessage").textContent = error.message; });
  </script>
</body>
</html>
"""
