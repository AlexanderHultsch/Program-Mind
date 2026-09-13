/* Program Mind - the shell (spec section 11). Vanilla JS, no build step.
   Owns the top bar (mark, project chip, status icons, burger menu), the
   options, statistics, status and proposal dialogs, the project picker, the
   routes and the home page with the recent work; every agent registers
   itself with PM and owns its own screens (10 September 2026). */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  let config = null;

  const store = {
    get(key) { try { const v = localStorage.getItem(`programmind:${key}`); return v ? JSON.parse(v) : null; } catch (e) { return null; } },
    set(key, value) { try { localStorage.setItem(`programmind:${key}`, JSON.stringify(value)); } catch (e) { /* no storage */ } },
    del(key) { try { localStorage.removeItem(`programmind:${key}`); } catch (e) { /* no storage */ } },
  };

  function esc(text) {
    return String(text == null ? "" : text)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  async function api(method, path, body) {
    const response = await fetch(path, {
      method, headers: { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    let data = null;
    try { data = await response.json(); } catch (e) { data = null; }
    if (!response.ok) throw new Error((data && data.error) || `${response.status} ${response.statusText}`);
    return data;
  }

  function show(name) {
    // Scroll to the top only when the screen actually changes: a poll that
    // redraws the same screen must not pull the page back up (9 September 2026).
    const target = $(`screen-${name}`);
    const changed = !target || target.hidden;
    document.querySelectorAll(".screen").forEach((el) => { el.hidden = el.id !== `screen-${name}`; });
    if (changed) window.scrollTo({ top: 0 });
  }

  function setError(id, message) {
    const el = $(id);
    el.textContent = message || "";
    el.hidden = !message;
  }

  function applyTheme(theme) {
    const root = document.documentElement;
    if (theme === "light" || theme === "dark") root.setAttribute("data-theme", theme);
    else root.removeAttribute("data-theme");
  }

  function greeting() {
    const h = new Date().getHours();
    return h < 5 ? "Good evening." : h < 12 ? "Good morning." : h < 18 ? "Good afternoon." : "Good evening.";
  }

  function lines(text) { return String(text || "").split("\n").map((s) => s.trim()).filter(Boolean); }
  // Text that the model wrote as bullets ("- " lines) becomes a list; anything
  // else is shown as it is. Arrays are lists too.

  function fmt(value) {
    if (Array.isArray(value)) return value.length ? `<ul class="bullets">${value.map((i) => `<li>${esc(i)}</li>`).join("")}</ul>` : "<span class='muted'>none stated</span>";
    const text = String(value == null ? "" : value).trim();
    if (!text) return "<span class='muted'>none stated</span>";
    const rows = text.split("\n").map((r) => r.trim()).filter(Boolean);
    if (rows.length && rows.every((r) => /^[-*•]\s+/.test(r))) {
      return `<ul class="bullets">${rows.map((r) => `<li>${esc(r.replace(/^[-*•]\s+/, ""))}</li>`).join("")}</ul>`;
    }
    return esc(text);
  }
  // A pick-list of members: a labelled checkbox per member, all ticked at first.

  function renderHints() {
    // The one line under the picker and under each agent's question box (the "N notes" chip went with the shell, spec 11.1, decision 13).
    const status = config.knowledge_status;
    const problem = !config.model ? '<span class="error">No model configured. Open Options.</span>'
      : !status.configured ? "No knowledge source set. The agents answer from your question alone."
      : !status.ok ? `<span class="error">${esc(status.error)}</span>` : "";
    $("home-hint").innerHTML = problem || `${status.notes} notes in the vault.`;
    $("board-hint").innerHTML = problem || `Reads up to ${fmtNum(config.token_budget)} tokens of notes from your vault per member.`;
    $("ask-hint").innerHTML = problem || "";     // spec 5.7: the estimate under the box already says what is read
    $("site-address").innerHTML = `This site: <strong>http://${esc(config.site_host || "program-mind.localhost")}:${esc(location.port || "80")}/</strong> · also reachable at http://localhost:${esc(location.port || "80")}/`;
    $("link-bug").href = `${config.repository || ""}/issues`;
    const obsidian = $("link-obsidian");
    obsidian.hidden = !config.vault_name;
    obsidian.href = config.vault_name ? `obsidian://open?vault=${encodeURIComponent(config.vault_name)}` : "#";
  }

  async function loadConfig() {
    config = await api("GET", "/api/config");
    applyTheme(config.theme);
    renderHints();
    renderProjectPicker();
  }

  // The project picker on the home page: a dropdown with checkboxes, like a
  // spreadsheet filter. The default comes from the configuration; the
  // choice travels with the question.

  let chosenProjects = null;   // null until the config is known

  function defaultProjects() {
    return String(config.project || "").split(/[,;]/).map((s) => s.trim()).filter(Boolean);
  }

  function projectLabel() {
    return !chosenProjects || chosenProjects.length === 0 ? "All projects" : chosenProjects.length === 1 ? chosenProjects[0] : `${chosenProjects.length} projects`;
  }

  function renderProjectChips() {
    ["board-project", "ask-project", "thread-project", "project-chip-label"].forEach((id) => { if ($(id)) $(id).textContent = projectLabel(); });
  }

  function renderProjectPicker() {
    const names = (config.projects || []).slice();
    // Chosen once on the start page, remembered in the browser, carried into every use case (spec 9.5).
    if (chosenProjects === null) chosenProjects = Array.isArray(store.get("projects")) ? store.get("projects") : defaultProjects();
    renderProjectChips();
    chosenProjects.forEach((p) => { if (!names.some((n) => n.toLowerCase() === p.toLowerCase())) names.push(p); });
    const all = chosenProjects.length === 0;
    $("projects-list").innerHTML = `<label class="all"><input type="checkbox" value="" ${all ? "checked" : ""}> All projects</label>` +
      names.map((n) => `<label><input type="checkbox" value="${esc(n)}" ${chosenProjects.some((p) => p.toLowerCase() === n.toLowerCase()) ? "checked" : ""}> ${esc(n)}</label>`).join("");
    $("projects-label").textContent = all ? "All projects" : chosenProjects.length === 1 ? chosenProjects[0] : `${chosenProjects.length} projects`;
    $("projects-list").querySelectorAll("input").forEach((box) => box.addEventListener("change", () => {
      if (box.value === "") chosenProjects = [];
      else {
        chosenProjects = Array.from($("projects-list").querySelectorAll("input")).filter((b) => b.value && b.checked).map((b) => b.value);
      }
      store.set("projects", chosenProjects);
      renderProjectPicker();
      loadStatus();                      // the project check follows the choice (a user action, not the timer)
    }));
    $("btn-projects").disabled = names.length === 0;
    if (names.length === 0) $("projects-label").textContent = "No project pages in the vault";
  }

  function toggleProjectMenu(open) {
    const menu = $("projects-menu");
    menu.hidden = open === undefined ? !menu.hidden : !open;
    $("btn-projects").setAttribute("aria-expanded", String(!menu.hidden));
  }

  function openOptions() {
    $("opt-vault").value = config.vault_path || "";
    $("opt-roles").value = config.roles_folder || "";
    $("opt-budget").value = config.token_budget || 6000;
    $("opt-model").value = config.model || "";
    $("opt-occonfig").value = config.opencode_config || "";
    $("opt-limit").value = config.token_limit == null ? "" : config.token_limit;
    $("opt-auto").checked = !!config.auto_approve;
    $("opt-audit").value = config.audit_folder || "";
    $("opt-theme").value = config.theme || "system";
    $("opt-path").textContent = config.config_path ? `Saved to ${config.config_path}` : "";
    const s = config.knowledge_status;
    $("opt-vault-status").textContent = !s.configured ? "Not set." : s.ok ? `${s.notes} notes found.` : s.error;
    renderRolesStatus(config.roles_status);
    setError("options-error", "");
    $("options-nav").querySelector("button").click();     // always opens on the first section
    $("options-dialog").hidden = false;
  }

  async function saveOptions(event) {
    event.preventDefault();
    try {
      config = await api("POST", "/api/config", {
        vault_path: $("opt-vault").value,
        roles_folder: $("opt-roles").value,
        token_budget: Number($("opt-budget").value) || 6000,
        model: $("opt-model").value,
        opencode_config: $("opt-occonfig").value,
        token_limit: $("opt-limit").value === "" ? null : Number($("opt-limit").value),
        auto_approve: $("opt-auto").checked,
        audit_folder: $("opt-audit").value,
        theme: $("opt-theme").value,
      });
      applyTheme(config.theme);
      renderHints();
      renderProjectPicker();
      $("options-dialog").hidden = true;
      loadStatus();                      // the settings changed: a fresh look, not a timer tick
    } catch (err) { setError("options-error", err.message); }
  }

  // One folder or file dialog on the server, its result written into an input.

  async function browseInto(buttonId, inputId, endpoint, extra) {
    const btn = $(buttonId);
    btn.disabled = true; btn.textContent = "Choose in the dialog…";
    try {
      const data = await api("POST", endpoint, Object.assign({ initial: $(inputId).value }, extra || {}));
      if (data.path) $(inputId).value = data.path;
    } catch (err) { setError("options-error", err.message); }
    btn.disabled = false; btn.textContent = "Browse…";
  }

  const browse = () => browseInto("btn-browse", "opt-vault", "/api/pick-folder", { title: "Choose the knowledge source (Obsidian vault)" });

  const browseRoles = () => browseInto("btn-browse-roles", "opt-roles", "/api/pick-folder", { title: "Choose the roles folder" });

  const browseConfigFile = () => browseInto("btn-browse-occonfig", "opt-occonfig", "/api/pick-file");

  function renderRolesStatus(r) {
    const el = $("opt-roles-status");
    if (!r) { el.textContent = ""; return; }
    if (r.error) {
      el.innerHTML = `<span class="error">${esc(r.error)}</span>`;
      $("btn-install-roles").hidden = false;
      return;
    }
    let text = `Board of ${r.count}: ${r.members.join(", ")} - from ${r.folder}.`;
    if (r.skipped && r.skipped.length) text += ` Not on the board: ${r.skipped.map((x) => `${x.member} (${x.reason})`).join("; ")}.`;
    const conduct = r.conduct || [];
    text += conduct.length ? ` Common note(s): ${conduct.join(", ")}.` : " No common note (kind: conduct) in the folder.";
    el.textContent = text;
    $("btn-install-roles").hidden = conduct.length > 0;
  }

  async function installRoles() {
    const btn = $("btn-install-roles");
    btn.disabled = true;
    try {
      // Save the folder typed above first, so the examples land where the user said.
      if ($("opt-roles").value !== (config.roles_folder || "") || $("opt-vault").value !== (config.vault_path || "")) {
        config = await api("POST", "/api/config", { roles_folder: $("opt-roles").value, vault_path: $("opt-vault").value });
        renderProjectPicker();
      }
      const r = await api("POST", "/api/roles/install");
      config.roles_status = r;
      renderRolesStatus(r);
      if (r.written) $("opt-roles-status").textContent += ` Written ${r.written.length} file(s) to ${r.target}.`;
    } catch (err) { setError("options-error", err.message); }
    btn.disabled = false;
  }

  function fmtNum(n) { return n == null ? "–" : Number(n).toLocaleString("en-GB"); }

  function fmtSec(s) { return s == null ? "–" : s < 60 ? `${Number(s).toFixed(1)} s` : `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`; }

  function md(text) {
    const blocks = String(text || "").replace(/\r/g, "").split(/\n\s*\n/).map((b) => b.trim()).filter(Boolean);
    const inline = (s) => esc(s).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>").replace(/`([^`]+)`/g, "<code>$1</code>");
    return blocks.map((b) => {
      const rows = b.split("\n").map((r) => r.trim()).filter(Boolean);
      if (rows.every((r) => /^[-*•]\s+/.test(r))) return `<ul>${rows.map((r) => `<li>${inline(r.replace(/^[-*•]\s+/, ""))}</li>`).join("")}</ul>`;
      if (rows.every((r) => /^\d+[.)]\s+/.test(r))) return `<ol>${rows.map((r) => `<li>${inline(r.replace(/^\d+[.)]\s+/, ""))}</li>`).join("")}</ol>`;
      if (rows.length === 1 && /^#{1,6}\s+/.test(rows[0])) return `<p><strong>${inline(rows[0].replace(/^#{1,6}\s+/, ""))}</strong></p>`;
      return `<p>${rows.map(inline).join("<br>")}</p>`;
    }).join("");
  }

  function fmtDate(seconds) {
    try { return new Date(seconds * 1000).toLocaleString("en-GB", { dateStyle: "medium", timeStyle: "short" }); } catch (e) { return ""; }
  }


  // -- the status icons (spec 11.1, decision 7): three checks, on load and every five minutes --

  const STATUS_MINUTES = 5;
  let status = null;
  let statusTimer = null;
  const STATE_WORDS = { green: "OK", amber: "Check", red: "Problem" };

  function stateClass(item) { return item ? `state-${item.state}` : "state-unknown"; }

  async function loadStatus() {
    try {
      const q = (chosenProjects || []).length ? `?projects=${encodeURIComponent(chosenProjects.join(","))}` : "";
      status = await api("GET", `/api/status${q}`);
    } catch (err) {
      status = { error: err.message, vault: { state: "red", detail: err.message }, ai: { state: "red", detail: err.message }, project: { state: "red", detail: err.message } };
    }
    renderStatus();
    if (!$("status-dialog").hidden) renderStatusDialog();
  }

  function startStatusTimer() {
    if (statusTimer) clearInterval(statusTimer);
    statusTimer = setInterval(loadStatus, STATUS_MINUTES * 60 * 1000);   // never more often than this
  }

  function vaultCard(v) {
    const rows = [];
    if (v.path) rows.push(`<dt>Folder</dt><dd>${esc(v.path)}</dd>`);
    if (v.state !== "red") rows.push(`<dt>Notes</dt><dd>${fmtNum(v.notes)} · ${v.projects} project page(s)</dd>`);
    if (v.roles_folder) rows.push(`<dt>Roles</dt><dd>${esc(v.roles_folder)}</dd>`);
    if (v.last_read) rows.push(`<dt>Last read</dt><dd>${esc(fmtDate(v.last_read))}</dd>`);
    const link = v.vault_name && v.state !== "red" ? `<a href="obsidian://open?vault=${encodeURIComponent(v.vault_name)}">Open in Obsidian</a>` : `<a href="#" data-open="options">Open Options</a>`;
    return { title: "Vault", rows, link };
  }

  function aiCard(a) {
    const rows = [`<dt>Model</dt><dd>${esc(a.model || "not set")}</dd>`,
      `<dt>OpenCode</dt><dd>${esc(a.opencode || "not found on PATH")}</dd>`,
      `<dt>Gateway file</dt><dd>${esc(a.config_file || (a.profile === "private" ? "none (private setup)" : "not set"))}</dd>`,
      `<dt>Answers</dt><dd>${a.streams ? "shown as the model writes them (OpenCode's server mode)" : "shown when the call is done (one run per call)"}</dd>`];
    const c = a.last_call;
    if (c) rows.push(`<dt>Test call</dt><dd>${c.ok ? `answered "${esc(c.answer)}" in ${esc(fmtSec(c.seconds))}` : `failed: ${esc(c.error)}`} · ${esc(fmtDate(c.at))}</dd>`);
    else rows.push(`<dt>Test call</dt><dd>none yet · click the icon to run one</dd>`);
    return { title: "Model", rows, link: `<a href="#" data-open="options">Open Options</a>` };
  }

  function projectCard(pr) {
    const rows = [];
    (pr.pages || []).forEach((page) => {
      rows.push(`<dt>${esc(page.title)}</dt><dd>${esc(page.path)}${page.summary ? `<br><span class="muted">${esc(page.summary)}</span>` : ""}</dd>`);
      if (page.gates && page.gates.length) rows.push(`<dt>Gates</dt><dd>${page.gates.map(esc).join("<br>")}</dd>`);
    });
    if (pr.state !== "red" && !(pr.projects || []).length) rows.push(`<dt>Known</dt><dd>${(pr.known || []).map(esc).join(", ") || "none"}</dd>`);
    return { title: "Project", rows, link: `<a href="/" data-open="home">Choose on the home page</a>` };
  }

  function cardHtml(name, item, card) {
    const heading = `<div class="status-head"><strong>${esc(card.title)}</strong>
      <span class="state-chip state-${esc(item.state)}"><span class="dot state-${esc(item.state)}"></span>${esc(STATE_WORDS[item.state] || "unknown")}</span></div>`;
    return `${heading}<p class="small status-detail">${esc(item.detail || "")}</p>${card.rows.length ? `<dl class="small">${card.rows.join("")}</dl>` : ""}<p class="small status-link">${card.link}</p>`;
  }

  function renderStatus() {
    const cards = { vault: vaultCard, ai: aiCard, project: projectCard };
    ["vault", "ai", "project"].forEach((name) => {
      const item = (status && status[name]) || { state: "unknown", detail: "Not checked yet." };
      $(`dot-${name}`).className = `dot ${stateClass(item)}`;
      $(`btn-status-${name}`).title = `${cards[name](item).title}: ${item.detail || ""}`;
      $(`btn-status-${name}`).classList.toggle("is-bad", item.state === "red");
      $(`card-${name}`).innerHTML = cardHtml(name, item, cards[name](item));
    });
    document.querySelectorAll(".status-card a[data-open], #status-body a[data-open]").forEach((a) => a.addEventListener("click", (e) => {
      e.preventDefault();
      if (a.dataset.open === "options") openOptions(); else navigate("/");
      $("status-dialog").hidden = true;
    }));
  }

  // -- the status page in the menu: the three checks written out, refresh, the test call --

  function renderStatusDialog() {
    const cards = { vault: vaultCard, ai: aiCard, project: projectCard };
    $("status-body").innerHTML = ["vault", "ai", "project"].map((name) => {
      const item = (status && status[name]) || { state: "unknown", detail: "Not checked yet." };
      return `<div class="status-row">${cardHtml(name, item, cards[name](item))}</div>`;
    }).join("");
    $("status-checked").textContent = status && status.checked ? `Checked ${fmtDate(status.checked)} · again in ${STATUS_MINUTES} minutes` : "";
    renderStatus();
  }

  function openStatus(askTest) {
    setError("status-error", "");
    $("status-test").hidden = !askTest;
    renderStatusDialog();
    $("status-dialog").hidden = false;
  }

  async function runAiTest() {
    const btn = $("btn-status-test-yes");
    btn.disabled = true; btn.textContent = "Calling…";
    try {
      const ai = await api("POST", "/api/status/ai");
      if (status) status.ai = ai;
      $("status-test").hidden = true;
      renderStatusDialog();
    } catch (err) { setError("status-error", err.message); }
    btn.disabled = false; btn.textContent = "Yes, run one test call";
  }

  // -- the burger menu (decision 8): the tools, not the agents --

  function toggleMenu(open) {
    const list = $("menu-list");
    list.hidden = open === undefined ? !list.hidden : !open;
    $("btn-menu").setAttribute("aria-expanded", String(!list.hidden));
  }

  // -- About (decision 12): the version, the address, the specification --

  function renderAbout() {
    const port = location.port || "80";
    $("about-version").textContent = config.version || "unknown";
    $("about-address").innerHTML = `http://${esc(config.site_host || "program-mind.localhost")}:${esc(port)}/<br><span class="muted">also http://localhost:${esc(port)}/ · the server listens on this machine only</span>`;
    $("about-spec").innerHTML = config.spec
      ? `<a href="/spec" target="_blank" rel="noopener">Read it here</a> <span class="muted">· docs/spec.md, as it is on this machine</span>`
      : `<a href="${esc(config.repository || "")}/blob/main/docs/spec.md" target="_blank" rel="noopener">Read it on the repository</a> <span class="muted">· it is not next to this copy</span>`;
    $("about-repo").innerHTML = `<a href="${esc(config.repository || "")}" target="_blank" rel="noopener">${esc(config.repository || "")}</a>`;
  }

  // -- the archive (decision 10): everything closed, of every agent --

  let archive = [];

  let archiveKind = "all";      // the tab in force: all, board or ask

  const AGENT_NAME = { board: "Board", ask: "Ask the vault" };

  function workRow(r, extra) {
    // One row, three columns of their own width, so a longer agent name or a
    // longer date never shifts the titles out of line.
    return `<div class="thread-row work-row ${extra}" data-kind="${esc(r.kind)}" data-id="${esc(r.id)}" title="Open">
      <span class="agent-tag">${esc(AGENT_NAME[r.kind] || r.kind)}</span>
      <button type="button" class="thread-open">${esc(r.title)}</button>
      <span class="thread-meta">${r.count} ${esc(r.unit)}(s) · ${esc(fmtDate(r.updated))}${r.projects && r.projects.length ? ` · ${esc(r.projects.join(", "))}` : ""}</span>`;
  }

  function pathOf(row) { return row.dataset.kind === "board" ? `/board/${row.dataset.id}` : `/ask/${row.dataset.id}`; }

  function openOnRowClick(box, path) {
    // The whole row opens it, not just the title. A button inside the row
    // that does something else (Delete) keeps its own click.
    box.querySelectorAll(".thread-row").forEach((row) => row.addEventListener("click", (event) => {
      const button = event.target.closest("button");
      if (button && !button.classList.contains("thread-open")) return;
      path(row);
    }));
  }

  function renderArchive() {
    const filter = ($("archive-filter").value || "").trim().toLowerCase();
    const rows = archive.filter((r) => (archiveKind === "all" || r.kind === archiveKind)
      && (!filter || r.title.toLowerCase().includes(filter)));
    $("archive-count").textContent = archive.length
      ? `${rows.length} of ${archive.length} closed · newest first`
      : "Nothing closed yet. A topic or a thread moves here when you close it.";
    $("archive-list").innerHTML = rows.length
      ? rows.map((r) => `${workRow(r, "archive-row")}
        <button type="button" class="ghost small-btn archive-delete" title="Delete this for good">Delete</button></div>`).join("")
      : `<p class="empty">${archive.length ? "Nothing here matches." : "Nothing closed yet. A topic or a thread moves here when you close it."}</p>`;
    openOnRowClick($("archive-list"), (row) => navigate(pathOf(row)));
    $("archive-list").querySelectorAll(".archive-delete").forEach((b) => b.addEventListener("click", async () => {
      const row = b.closest(".thread-row");
      if (!window.confirm("Delete this for good? Its questions and answers are removed; a note written to the vault stays.")) return;
      try { await api("DELETE", `/api/history/${row.dataset.id}`); loadArchive(); } catch (err) { setError("archive-error", err.message); }
    }));
  }

  function openArchive() {
    $("archive-filter").value = "";     // a fresh look each time the archive is opened
    archiveKind = "all";
    $("archive-tabs").querySelectorAll(".tab").forEach((t) => t.setAttribute("aria-selected", String(t.dataset.kind === "all")));
    loadArchive();
  }

  async function loadArchive() {
    setError("archive-error", "");
    try {
      const data = await api("GET", "/api/history?state=closed");
      archive = data.items || [];
      renderArchive();
    } catch (err) { setError("archive-error", err.message); }
  }

  // -- the home page (decision 5): the recent open work of every agent --

  async function loadRecent() {
    const box = $("recent-list");
    try {
      const data = await api("GET", "/api/history?state=open");
      const items = data.items || [];
      box.innerHTML = items.length ? items.map((r) => `${workRow(r, "recent-row")}</div>`).join("")
        : "<p class='muted small'>Nothing open. Start with an agent above; closed work is in the archive.</p>";
      openOnRowClick(box, (row) => navigate(pathOf(row)));
    } catch (err) { setError("start-error", err.message); }
  }

  $("greeting").textContent = greeting();

  $("btn-stats").addEventListener("click", openStats);

  $("btn-stats-close").addEventListener("click", () => { $("stats-dialog").hidden = true; });

  $("btn-write").addEventListener("click", writeMemory);

  $("btn-discard").addEventListener("click", discardMemory);

  $("btn-brand").addEventListener("click", () => navigate("/"));

  $("project-chip").addEventListener("click", (e) => { e.stopPropagation(); navigate("/"); toggleProjectMenu(true); });

  $("btn-menu").addEventListener("click", (e) => { e.stopPropagation(); toggleMenu(); });

  $("menu-list").addEventListener("click", () => toggleMenu(false));

  document.addEventListener("click", (e) => { if (!$("menu").contains(e.target)) toggleMenu(false); });

  document.addEventListener("keydown", (e) => { if (e.key === "Escape") { toggleMenu(false); toggleProjectMenu(false); } });

  $("btn-status-vault").addEventListener("click", () => openStatus(false));

  $("btn-status-ai").addEventListener("click", () => openStatus(true));

  $("btn-status-project").addEventListener("click", () => navigate("/"));

  $("btn-status-close").addEventListener("click", () => { $("status-dialog").hidden = true; });

  $("btn-status-refresh").addEventListener("click", loadStatus);

  $("btn-status-test").addEventListener("click", () => { $("status-test").hidden = false; });

  $("btn-status-test-no").addEventListener("click", () => { $("status-test").hidden = true; });

  $("btn-status-test-yes").addEventListener("click", runAiTest);

  $("btn-archive").addEventListener("click", () => navigate("/archive"));

  $("btn-privacy").addEventListener("click", () => navigate("/privacy"));

  $("btn-about").addEventListener("click", () => navigate("/about"));

  $("archive-filter").addEventListener("input", renderArchive);

  $("archive-tabs").addEventListener("click", (event) => {
    const tab = event.target.closest(".tab");
    if (!tab) return;
    archiveKind = tab.dataset.kind;
    $("archive-tabs").querySelectorAll(".tab").forEach((t) => t.setAttribute("aria-selected", String(t === tab)));
    renderArchive();
  });

  // Options: one section at a time, the list beside the fields (spec 12.2)
  $("options-nav").addEventListener("click", (event) => {
    const tab = event.target.closest("button[data-pane]");
    if (!tab) return;
    $("options-nav").querySelectorAll("button").forEach((b) => b.setAttribute("aria-selected", String(b === tab)));
    $("options-panes").querySelectorAll("fieldset").forEach((f) => { f.hidden = f.id !== tab.dataset.pane; });
  });

  document.querySelectorAll("a.tile, a.change-project, #link-threads, #btn-thread-new, #btn-board-new").forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); navigate(a.getAttribute("href")); }));

  $("btn-projects").addEventListener("click", () => toggleProjectMenu());

  document.addEventListener("click", (e) => { if (!$("project-picker").contains(e.target)) toggleProjectMenu(false); });

  $("btn-projects-default").addEventListener("click", async () => {
    try { config = await api("POST", "/api/config", { project: (chosenProjects || []).join(", ") }); renderProjectPicker(); toggleProjectMenu(false); }
    catch (err) { setError("home-error", err.message); }
  });

  $("btn-error-options").addEventListener("click", openOptions);

  $("btn-options").addEventListener("click", openOptions);

  $("btn-options-cancel").addEventListener("click", () => { $("options-dialog").hidden = true; });

  $("options-form").addEventListener("submit", saveOptions);

  $("btn-browse").addEventListener("click", browse);

  $("btn-browse-occonfig").addEventListener("click", browseConfigFile);

  $("btn-install-roles").addEventListener("click", installRoles);

  $("btn-browse-roles").addEventListener("click", browseRoles);

  document.querySelectorAll(".modal").forEach((m) => m.addEventListener("click", (e) => { if (e.target === m) m.hidden = true; }));

  function wallTimes(marks) {
    // Wall time per phase, from the phase marks: each mark lasts until the next.
    const rows = [];
    const names = { running: "board members", synthesising: "consolidation", "follow-up": "follow-up", proposing: "memory proposal" };
    let followUps = 0;
    for (let i = 0; i < marks.length - 1; i++) {
      const name = marks[i].phase;
      if (!names[name]) continue;   // the rest is time waiting for Alex, not for the model
      const seconds = marks[i + 1].at - marks[i].at;
      if (seconds < 0.05) continue;  // the combined mode has no separate consolidation
      rows.push({ phase: name === "follow-up" ? `follow-up ${++followUps}` : names[name], seconds });
    }
    // The clarifier's own time: from the start (or an answer) to the questions.
    const clar = [];
    for (let i = 0; i < marks.length - 1; i++) {
      if (["started", "questions"].includes(marks[i].phase) && ["questions", "confirm"].includes(marks[i + 1].phase) && marks[i + 1].at - marks[i].at < 3600) {
        clar.push(marks[i + 1].at - marks[i].at);
      }
    }
    return { rows, clarifier: clar.reduce((a, b) => a + b, 0) };
  }

  // -- the shared proposal screen (the memory step of any agent) --------------

  let memoryHandlers = null;
  function showProposal(p, vaultPath, handlers) {
    if (!$("screen-proposal").hidden) return;
    memoryHandlers = handlers;
    $("prop-vault").value = vaultPath || "";
    $("prop-path").value = p.path;
    $("prop-title").value = p.title;
    $("prop-tags").value = p.tags.join(", ");
    $("prop-body").value = p.body;
    $("prop-mode").textContent = p.mode === "append" ? "appends to existing note" : "new note";
    $("prop-preview").textContent = p.preview;
    setError("proposal-error", p.parse_error || "");
    show("proposal");
  }
  async function writeMemory() {
    const body = {
      path: $("prop-path").value, title: $("prop-title").value,
      tags: $("prop-tags").value.split(",").map((t) => t.trim()).filter(Boolean),
      body: $("prop-body").value,
    };
    try { await memoryHandlers.write(body); } catch (err) { setError("proposal-error", err.message); }
  }
  async function discardMemory() {
    try { await memoryHandlers.discard(); } catch (err) { setError("proposal-error", err.message); }
  }

  // -- statistics: tokens and time per step, for whichever agent is open ------

  function statTile(value, label) {
    return `<div class="stat"><div class="stat-value">${esc(value)}</div><div class="stat-label">${esc(label)}</div></div>`;
  }

  function openStats() {
    const dialog = $("stats-dialog");
    const agent = agentFor(currentRoute);
    const source = agent && agent.stats ? agent.stats() : null;
    const stats = source && source.stats;
    if (!stats || !stats.calls.length) {
      $("stats-summary").textContent = source ? "No model call yet." : "Start a topic or a thread; the statistics fill in as the model works.";
      $("stats-body").innerHTML = "";
      dialog.hidden = false;
      return;
    }
    const calls = stats.calls;
    const sum = (key) => calls.reduce((a, c) => a + (c[key] || 0), 0);
    const groups = new Map();
    calls.forEach((c) => {
      const key = c.step;
      const g = groups.get(key) || { step: key, calls: 0, input: 0, output: 0, seconds: 0, members: [] };
      g.calls += 1; g.input += c.input_tokens || 0; g.output += c.output_tokens || 0; g.seconds += c.seconds || 0;
      if (c.member) g.members.push(c);
      groups.set(key, g);
    });
    const wall = wallTimes(stats.marks || []);
    const last = stats.marks && stats.marks.length ? stats.marks[stats.marks.length - 1].at : Date.now() / 1000;
    const total = last - stats.started;
    $("stats-summary").innerHTML = `<div class="stat-grid">
      ${statTile(calls.length, "model calls")}
      ${statTile(fmtNum(sum("input_tokens")), "tokens in")}
      ${statTile(fmtNum(sum("output_tokens")), "tokens out")}
      ${statTile(fmtSec(sum("seconds")), "model time")}
      ${statTile(fmtSec(total), "from the question")}
    </div>`;
    const rows = [];
    groups.forEach((g) => {
      rows.push(`<tr><td>${esc(g.step)}</td><td class="num">${g.calls}</td><td class="num">${fmtNum(g.input)}</td><td class="num">${fmtNum(g.output)}</td><td class="num">${fmtSec(g.seconds)}</td></tr>`);
      g.members.forEach((c) => {
        rows.push(`<tr class="group"><td>&nbsp;&nbsp;${esc(c.member)}${c.error ? ` <span class="err">failed: ${esc(c.error)}</span>` : ""}</td><td class="num">1</td><td class="num">${fmtNum(c.input_tokens)}</td><td class="num">${fmtNum(c.output_tokens)}</td><td class="num">${fmtSec(c.seconds)}</td></tr>`);
      });
    });
    rows.push(`<tr class="total"><td>Total</td><td class="num">${calls.length}</td><td class="num">${fmtNum(sum("input_tokens"))}</td><td class="num">${fmtNum(sum("output_tokens"))}</td><td class="num">${fmtSec(sum("seconds"))}</td></tr>`);
    const wallRows = wall.rows.map((r) => `<tr><td>${esc(r.phase)}</td><td class="num">${fmtSec(r.seconds)}</td></tr>`).join("");
    $("stats-body").innerHTML = `
      <table><thead><tr><th>Step</th><th class="num">Calls</th><th class="num">Tokens in</th><th class="num">Tokens out</th><th class="num">Model time</th></tr></thead><tbody>${rows.join("")}</tbody></table>
      <p class="eyebrow">Wall time <span class="muted small">members run in parallel, so a step is shorter than its calls added up</span></p>
      <table><thead><tr><th>Phase</th><th class="num">Duration</th></tr></thead><tbody>
        <tr><td>clarifier (all rounds)</td><td class="num">${fmtSec(wall.clarifier)}</td></tr>${wallRows}
        <tr class="total"><td>From the question to now</td><td class="num">${fmtSec(total)}</td></tr></tbody></table>
      ${agent && agent.statsExtra ? agent.statsExtra() : ""}
      <p class="muted small">A retried empty run counts once, with the retry's tokens. Time you spent answering questions is not counted as model time.</p>`;
    dialog.hidden = false;
  }

  // -- routes (spec 9.5): one page, the path decides the screen; agents own theirs --

  const agents = [];
  let currentRoute = "start";
  function register(agent) { agents.push(agent); }
  function routeOf(pathname) {
    if (["/archive", "/privacy", "/about"].includes(pathname)) return pathname.slice(1);
    for (const a of agents) { const r = a.match(pathname); if (r) return r; }
    return "start";
  }
  function agentFor(route) { return agents.find((a) => a.match(location.pathname) === route && route !== "start") || null; }
  function setPath(path, replace) {
    // ``replace`` rewrites the entry the browser is on instead of adding one:
    // an address that only opens something (/board/<id>) must not stay in the
    // history, or Back lands on it again and reopens what you just left.
    currentRoute = routeOf(path);
    if (location.pathname === path) return;
    try { history[replace ? "replaceState" : "pushState"]({ route: currentRoute }, "", path); } catch (e) { /* not available */ }
  }
  function navigate(path) { setPath(path); renderRoute(); }
  function renderRoute() {
    currentRoute = routeOf(location.pathname);
    const agent = agentFor(currentRoute);
    agents.forEach((a) => { if (a !== agent && a.onLeave) a.onLeave(); });
    renderProjectChips();
    if (!agent) {
      $("step-line").hidden = true;
      if (currentRoute === "archive") { show("archive"); openArchive(); return; }
      if (currentRoute === "privacy") { show("privacy"); return; }
      if (currentRoute === "about") { renderAbout(); show("about"); return; }
      show("start"); loadRecent(); return;
    }
    if (agent.onEnter) agent.onEnter();
    agent.render(currentRoute);
  }
  window.addEventListener("popstate", () => {
    // The browser's own Back: between the use cases by path; inside an
    // agent, whatever the agent makes of it (the board steps back).
    if (routeOf(location.pathname) !== currentRoute) { renderRoute(); return; }
    const agent = agentFor(currentRoute);
    if (agent && agent.onPopState) agent.onPopState();
  });

  window.PM = {
    $, esc, api, store, show, setError, fmt, fmtNum, fmtSec, lines, md, fmtDate,
    register, navigate, setPath, showProposal, openOptions, renderProjectPicker, loadStatus, loadRecent,
    openOnRowClick, steps: () => $("step-line"),
    route: () => currentRoute,
    projects: () => (chosenProjects || []),
    get config() { return config; },
    set config(value) { config = value; },
  };

  // Boot once every agent script has registered (they load after this one).
  document.addEventListener("DOMContentLoaded", () => {
    loadConfig()
      .then(() => Promise.all(agents.map((a) => (a.boot ? a.boot() : null))))
      .then(() => { renderRoute(); loadStatus(); startStatusTimer(); })
      .catch((err) => { $("home-hint").textContent = err.message; show("start"); });
  });
})();
