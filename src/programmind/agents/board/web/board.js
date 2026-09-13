/* Program Mind - the Board agent (spec sections 3, 5.1 and 9). Loaded after
   shell.js; registers itself with the shell and owns every board screen. */
(function () {
  "use strict";
  const PM = window.PM;
  const { $, esc, api, store, show, setError, fmt, fmtNum, fmtSec, lines, md } = PM;


  const ICONS = {
    dollar: '<path d="M12 3v18"/><path d="M16.5 7.5A3.5 3.5 0 0 0 13 5h-2.5a3 3 0 0 0 0 6h3a3 3 0 0 1 0 6H11a3.5 3.5 0 0 1-3.5-2.5"/>',
    chip: '<rect x="7" y="7" width="10" height="10" rx="1.5"/><path d="M9 3v4M15 3v4M9 17v4M15 17v4M3 9h4M3 15h4M17 9h4M17 15h4"/>',
    gear: '<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M4.9 19.1 7 17M17 7l2.1-2.1"/>',
    factory: '<path d="M3 21V9l5 3V9l5 3V9l5 3v9H3z"/><path d="M17 12V4h3v8"/><path d="M7 17h2M11 17h2M15 17h2"/>',
    code: '<path d="M8 7l-5 5 5 5"/><path d="M16 7l5 5-5 5"/><path d="M14 4l-4 16"/>',
    target: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1.2"/><path d="M12 3v2M21 12h-2"/>',
    scale: '<path d="M12 3v18M5 21h14"/><path d="M3 7h18"/><path d="M6 7l-3 7a3 3 0 0 0 6 0L6 7zM18 7l-3 7a3 3 0 0 0 6 0l-3-7z"/>',
    people: '<circle cx="9" cy="8" r="3"/><circle cx="17" cy="9" r="2.5"/><path d="M3 20a6 6 0 0 1 12 0"/><path d="M15 20a4.5 4.5 0 0 1 7 -3"/>',
    shield: '<path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6l8-3z"/><path d="M9 12l2 2 4-4"/>',
    truck: '<rect x="2" y="7" width="12" height="9" rx="1"/><path d="M14 10h4l3 3v3h-7z"/><circle cx="6" cy="18" r="1.8"/><circle cx="17" cy="18" r="1.8"/>',
    flask: '<path d="M9 3h6M10 3v6l-5 9a2 2 0 0 0 1.8 3h10.4a2 2 0 0 0 1.8-3l-5-9V3"/><path d="M7 15h10"/>',
    chart: '<path d="M3 21h18"/><path d="M6 17v-5M11 17V7M16 17v-8M21 17V4"/>',
    layers: '<path d="M12 3l9 5-9 5-9-5 9-5z"/><path d="M3 13l9 5 9-5"/><path d="M3 17l9 5 9-5"/>',
    person: '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
  };

  const SYNTHESIS_ICON = '<path d="M20 6 9 17l-5-5"/>';

  let memberMeta = {};   // name -> {title, short, icon, color, perspective}, from the session

  function meta(name) {
    return memberMeta[name] || { title: name, short: name.slice(0, 14), icon: "person", color: "#6b7280", perspective: "" };
  }

  function setMemberMeta(list) {
    memberMeta = {};
    (list || []).forEach((m) => { memberMeta[m.name] = m; });
  }

  let session = null;

  let justClosed = false;    // closed on this screen: the confirmation, not the read-only result


  let pollTimer = null;

  const openMembers = new Set();

  // What the browser keeps between reloads (9 September 2026: nothing typed
  // is lost to an error or a refresh): the current session's id and a draft
  // of everything typed, per session. localStorage may be unavailable; every
  // access is guarded and the page works without it.

  function draft() { return (session && store.get(`draft:${session.id}`)) || {}; }

  function saveDraft(patch) { if (session) store.set(`draft:${session.id}`, Object.assign(draft(), patch)); }

  function forgetSession() { if (session) store.del(`draft:${session.id}`); store.del("session"); }

  function avatar(name, cls) {
    const m = meta(name);
    const icon = Object.prototype.hasOwnProperty.call(ICONS, m.icon) ? ICONS[m.icon] : ICONS.person;
    return `<span class="avatar ${cls || ""}" style="background:${esc(m.color)}" aria-hidden="true"><svg viewBox="0 0 24 24">${icon}</svg></span>`;
  }

  function renderPicks(container, names, ticked) {
    container.innerHTML = names.map((name) => `
      <label class="${ticked.has(name) ? "" : "off"}" style="color:${esc(meta(name).color)}">
        <input type="checkbox" value="${esc(name)}" ${ticked.has(name) ? "checked" : ""}>
        ${avatar(name)}<span style="color:var(--text)">${esc(name)}</span></label>`).join("");
    container.querySelectorAll("input").forEach((box) => box.addEventListener("change", () => {
      box.closest("label").classList.toggle("off", !box.checked);
    }));
  }
  // Where a member's material came from: verified facts from the knowledge
  // net (with the note), flagged citations, and the member's own judgement.

  function sourcesRows(a) {
    const facts = a.sources || [];
    const net = facts.length
      ? `<ul class="bullets sources">${facts.map((s) => `<li class="${s.verified ? "ok" : "warn"}"><span class="mark">${s.verified ? "✓" : "?"}</span> ${esc(s.fact)} <span class="note">${esc(s.source || "no note named")}${s.verified ? "" : ` · ${esc(s.note)}`}</span></li>`).join("")}</ul>`
      : "<span class='muted'>nothing taken from the knowledge net</span>";
    const own = a.judgement ? fmt(a.judgement) : "<span class='muted'>none listed</span>";
    const flags = (a.flags || []).length ? `<dt class="warn">Check</dt><dd class="flag">${a.flags.map(esc).join("; ")}</dd>` : "";
    return `<dt>From the knowledge net</dt><dd>${net}</dd><dt>Own judgement</dt><dd>${own}</dd>${flags}`;
  }

  function modeOf(id) { return $(id).value || "individual"; }

  function selectionOf() { return $("selection-mode").value === "python" ? "python" : "ai"; }

  function setSelection(value) { $("selection-mode").value = value === "python" ? "python" : "ai"; }

  let lastPickState = null;
  // The one line under the dropdown, and the banner when the model's pick failed.

  function updatePickStatus() {
    if (!session) return;
    const state = session.pick_state;
    const python = selectionOf() === "python";
    const text = python ? "Python ranks the sections by the words of the question and of each member, plus the page properties."
      : state === "running" ? "The model is choosing the knowledge for each member…"
      : state === "done" ? `The model chose the knowledge for each member; its reasons are listed under the estimate.${session.pick_dropped ? ` ${session.pick_dropped} id(s) it named were not candidates and were dropped.` : ""}`
      : state === "failed" ? "The model's pick did not come back usable; the Python ranking is shown instead."
      : "The model will choose the knowledge for each member on this screen.";
    $("pick-status").textContent = text;
    const banner = $("pick-banner");
    banner.hidden = !(state === "failed" && !python);
    if (!banner.hidden) banner.textContent = `AI pick failed: ${session.pick_error || "no usable answer"}. Python's ranking is in force. Change the dropdown and back to try again.`;
    if (lastPickState !== null && lastPickState !== state && ["done", "failed"].includes(state)) requestEstimate();
    lastPickState = state;
  }

  function setMode(id, value) { $(id).value = value; if ($(id).value !== value) $(id).value = "individual"; }
  // One member's answer as rows: the full assessment, or the reasons why the
  // topic does not touch it. Used on the member cards and inside follow-ups.

  function memberBody(a) {
    if (a.applies === false) {
      return `<p class="na-note">This member says the topic does not touch its responsibilities. Its reasons:</p><dl><dt>Why not</dt><dd>${fmt(a.view)}</dd></dl>`;
    }
    return `<dl><dt>View</dt><dd>${fmt(a.view)}</dd>${a.impact ? `<dt>Impact on my area</dt><dd>${fmt(a.impact)}</dd>` : ""}<dt>Risks</dt><dd>${fmt(a.risks)}</dd><dt>Recommendation</dt><dd>${fmt(a.recommendation)}</dd>${sourcesRows(a)}</dl>`;
  }

  function picked(container) {
    return Array.from(container.querySelectorAll("input:checked")).map((box) => box.value);
  }

  function stopPolling() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } }

  function startPolling() {
    stopPolling();
    pollTimer = setInterval(async () => {
      if (!session) return stopPolling();
      try { session = await api("GET", `/api/sessions/${session.id}`); render(); }
      catch (err) { stopPolling(); showError(err.message); }
    }, 1000);
  }

  function needsPolling(s) {
    return ["clarifying", "running", "synthesising", "proposing"].includes(s.phase) || s.busy
      || (s.phase === "confirm" && s.pick_state === "running");
  }

  async function ask(event) {
    event.preventDefault();
    const question = $("question").value.trim();
    if (!question) return;
    setError("home-error", "");
    try {
      session = await api("POST", "/api/sessions", { question, projects: PM.projects() });
      store.del("question");
      justClosed = false;
      openMembers.clear();
      $("screen-result").dataset.phase = "";
      $("turns").dataset.key = "";
      render();
    } catch (err) { setError("home-error", err.message); }
  }

  // Question · Clarify · Confirm · Result. Which step the topic stands on,
  // and which of the others can be reached from it: an earlier step goes
  // back exactly as the arrows did, the next one forward again.

  const STEP_OF = { clarifying: 1, questions: 1, confirm: 2, running: 3, synthesising: 3, result: 3,
                    proposing: 3, proposal: 3, closed: 3, written: 3 };

  function stepIndex() {
    if (!session || !$("screen-home").hidden) return 0;
    if (session.phase === "error") return session.result ? 3 : (session.inputs && session.inputs.topic) ? 2 : 1;
    return STEP_OF[session.phase] || 0;
  }

  function renderSteps() {
    const line = PM.steps();
    if (!["board", "board-topic"].includes(PM.route())) { line.hidden = true; return; }
    line.hidden = false;
    const current = stepIndex();
    const nav = (session && session.nav) || { back: false, forward: false };
    const onHome = !session || !$("screen-home").hidden;
    const canBack = !!session && !onHome && (nav.back || session.phase === "questions");
    const canForward = !!session && (onHome ? !["closed", "written"].includes(session.phase) : nav.forward);
    line.querySelectorAll(".step").forEach((button) => {
      const step = Number(button.dataset.step);
      const clickable = step < current ? canBack : step === current ? false : step === current + 1 ? canForward : false;
      button.className = `step${step === current ? " current" : step < current ? " done" : ""}${clickable ? " clickable" : ""}`;
      button.querySelector(".step-mark").textContent = step < current ? "✓" : String(step + 1);
      button.setAttribute("aria-current", step === current ? "step" : "false");
      button.disabled = !clickable;
      button.title = step === current ? "Where the topic stands"
        : clickable ? (step < current ? "Go back to this step; everything typed is kept" : "Forward again")
        : "Not reachable from here";
    });
  }

  async function goToStep(target) {
    if (!session) return;
    if (!$("screen-home").hidden) { render(); return; }   // a topic is open behind the question screen
    for (let guard = 0; guard < 8; guard++) {
      const current = stepIndex();
      if (current === target) break;
      if (current < target) return goForward();
      try {
        session = await api("POST", `/api/sessions/${session.id}/back`);
      } catch (err) {
        // Nothing behind the first round: the question screen, with the
        // question kept and the topic still reachable with the next step.
        $("question").value = session.question;
        PM.setPath("/board"); show("home"); renderSteps(); pushHistory();
        return;
      }
    }
    render();
  }

  let lastPhase = null;

  let steppingBack = false;   // true while the browser's own Back is being served

  function pushHistory() {
    // A step of the flow adds an entry, so the browser's Back steps through
    // it. The step the browser's Back itself caused must not add one: it
    // would put back the entry just left and the topic could never be left
    // at all (seen 10 September 2026: Back bounced between two screens).
    const phase = session ? `${session.id}:${session.phase}` : "home";
    if (phase === lastPhase) return;
    lastPhase = phase;
    if (steppingBack) return;
    try { history.pushState({ phase }, ""); } catch (e) { /* not available */ }
  }

  function render() {
    if (PM.route() !== "board") return;
    if (!session) { show("home"); renderSteps(); return; }
    store.set("session", session.id);
    if (session.member_meta && session.member_meta.length) setMemberMeta(session.member_meta);
    if (needsPolling(session)) { if (!pollTimer) startPolling(); } else stopPolling();
    switch (session.phase) {
      case "clarifying": return renderClarifying();
      case "questions": return renderQuestions();
      case "confirm": renderConfirm(); return updatePickStatus();
      case "running":
      case "synthesising": return renderRunning();
      case "result": return renderResult();
      case "proposing": return show("proposing");
      case "proposal": return PM.showProposal(session.proposal, session.knowledge.vault_path || "", memoryHandlers);
      case "written":
      case "closed": return justClosed || !session.result ? renderDone() : renderResult();
      case "error": return showError(session.error);
      default: return showError(`Unknown state: ${session.phase}`);
    }
  }
  // Every render also refreshes the arrows and the browser history.

  const _render = render;

  render = function () { _render(); renderSteps(); pushHistory(); };

  function showError(message) {
    $("error-detail").textContent = message || "Unknown error.";
    show("error");
  }

  function renderClarifying() {
    const k = session.knowledge;
    $("clarifying-detail").textContent = k && k.vault_path
      ? `Selected ${k.selected} of ${k.total} notes from your vault (about ${k.tokens} tokens). Working out what is missing…`
      : "Working out what the board needs to know…";
    show("clarifying");
  }

  function renderQuestions() {
    if (!$("screen-questions").hidden) return;   // already drawn; keep the typed answers
    const done = session.rounds || [];
    const round = done.length + 1;
    $("questions-intro").textContent = round === 1
      ? "A few things would change the recommendation. Answer what you can; leave the rest blank."
      : `Round ${round}: your answers raised a few more points. Answer what you can; leave the rest blank.`;
    $("rounds-done").innerHTML = done.map((r, n) => `
      <details><summary>Round ${n + 1}: ${r.questions.length} question(s) answered</summary>
        <dl>${r.questions.map((q, i) => `<dt>${esc(q)}</dt><dd>${esc(r.answers[i] || "(not answered)")}</dd>`).join("")}</dl>
      </details>`).join("");
    const form = $("questions-form");
    const kept = draft().answers || {};
    const key = session.clarification.questions.join("|");
    form.innerHTML = session.clarification.questions.map((q, i) => `
      <label><span class="q-text">${i + 1}. ${esc(q)}</span>
        <textarea rows="2" data-index="${i}" placeholder="Your answer, or leave blank">${esc(session.answers[i] || (kept.key === key ? kept.values[i] : "") || "")}</textarea></label>`).join("");
    form.oninput = () => saveDraft({ answers: { key, values: Array.from(form.querySelectorAll("textarea")).map((t) => t.value) } });
    const last = round >= (session.max_rounds || 5);
    $("btn-answers").textContent = last ? "Continue to the board" : "Continue";
    $("btn-answers-final").hidden = last;
    $("questions-hint").textContent = last
      ? `This is the last round (${session.max_rounds}). The board is asked next.`
      : "Continue: the clarifier checks whether anything is still missing and asks again if so, or hands over to the board. Ask the board now: skip further questions.";
    show("questions");
    const first = form.querySelector("textarea");
    if (first) first.focus();
  }

  async function submitAnswers(final) {
    const answers = Array.from($("questions-form").querySelectorAll("textarea")).map((t) => t.value.trim());
    try { session = await api("POST", `/api/sessions/${session.id}/answers`, { answers, final: !!final }); render(); }
    catch (err) { showError(err.message); }
  }

  function renderConfirm() {
    if (!$("screen-confirm").hidden) return;
    const inp = session.inputs;
    const kept = draft().confirm;
    $("in-topic").value = (kept && kept.topic) || inp.topic || "";
    $("in-context").value = kept ? kept.context : (inp.context || "");
    ["in-topic", "in-context", "in-options", "in-constraints"].forEach((id) => { $(id).addEventListener("input", requestEstimate); });
    $("in-options").value = kept ? kept.options : (inp.options || []).join("\n");
    $("in-constraints").value = kept ? kept.constraints : (inp.constraints || []).join("\n");
    $("confirm-form").oninput = saveConfirmDraft;
    const k = session.knowledge;
    const r = session.roles || { members: [], count: 0, source: "", folder: null };
    let rolesLine = `Board of ${r.count}: ${r.members.join(", ")} (profiles from ${r.folder}).`;
    if (r.skipped && r.skipped.length) rolesLine += ` Not on the board: ${r.skipped.map((x) => `${x.member}, ${x.reason}`).join("; ")}.`;
    const kpi = r.kpi_members || [];
    rolesLine += kpi.length ? ` KPI notes from the vault attached for: ${kpi.join(", ")}.` : " No KPI notes (kind: kpi) in the vault yet.";
    const names = r.members || [];
    const ticked = new Set(kept && kept.members ? kept.members : (session.selected_members && session.selected_members.length ? session.selected_members : names));
    renderPicks($("confirm-members"), names, ticked);
    $("confirm-members").querySelectorAll("input").forEach((box) => box.addEventListener("change", saveConfirmDraft));
    setMode("run-mode", (kept && kept.mode) || session.mode || "individual");
    // Knowledge selection (spec 5.1): the browser remembers the last choice;
    // a choice that differs from the session's starts (or skips) the pick.
    const remembered = store.get("selection") || session.selection || PM.config.selection || "ai";
    setSelection(remembered);
    if (remembered !== session.selection) api("POST", `/api/sessions/${session.id}/pick`, { selection: remembered }).then((s2) => {
      session = s2; updatePickStatus();
      if (needsPolling(session) && !pollTimer) startPolling();      // the pick just started: watch it land
    }).catch(() => {});
    $("selection-mode").onchange = async () => {
      const value = selectionOf();
      store.set("selection", value);
      try { session = await api("POST", `/api/sessions/${session.id}/pick`, { selection: value }); } catch (err) { setError("home-error", err.message); }
      updatePickStatus(); countLine(); saveConfirmDraft();
      if (needsPolling(session) && !pollTimer) startPolling();
    };
    lastPickState = null;
    updatePickStatus();
    $("budget").value = (kept && kept.budget != null) ? kept.budget : (session.budget != null ? session.budget : (PM.config.token_budget || 6000));
    picks = { extra: (kept && kept.extra) || session.extra || [], exclude: (kept && kept.exclude) || session.exclude || [] };
    outlineCache = null;
    $("budget").disabled = !(k && k.vault_path);
    const countLine = () => {
      $("budget-value").textContent = `${fmtNum(Number($("budget").value))} tokens`;
      $("confirm-knowledge").textContent = (k && k.vault_path ? "" : "No knowledge source configured. ") + rolesLine;
      requestEstimate();
    };
    $("confirm-members").querySelectorAll("input").forEach((box) => box.addEventListener("change", countLine));
    $("run-mode").onchange = () => { countLine(); saveConfirmDraft(); };
    $("budget").oninput = () => { countLine(); saveConfirmDraft(); };
    countLine();
    show("confirm");
  }

  function saveConfirmDraft() {
    saveDraft({ confirm: {
      topic: $("in-topic").value, context: $("in-context").value, options: $("in-options").value,
      constraints: $("in-constraints").value, members: picked($("confirm-members")), mode: modeOf("run-mode"),
      budget: Number($("budget").value), extra: picks.extra, exclude: picks.exclude,
    } });
  }

  // The live estimate: exact calls, "about" tokens, from the server's own
  // prompt builders. Debounced, and a stale answer never overwrites a newer one.

  let estimateTimer = null;

  let estimateSeq = 0;

  let picks = { extra: [], exclude: [] };     // manual picks for the open topic: section ids

  let outlineCache = null;

  let lastEstimate = null;     // what the last estimate said, for the filter to redraw from

  function renderOutline() {
    const filter = ($("outline-filter").value || "").toLowerCase();
    const rows = (outlineCache || []).map((note) => {
      const secs = note.sections.filter((s) => !filter || note.path.toLowerCase().includes(filter) || (s.heading || "").toLowerCase().includes(filter));
      if (!secs.length) return "";
      return `<div class="note"><div class="note-title">${esc(note.path)}</div>${secs.map((s) => `
        <label><input type="checkbox" data-id="${esc(s.id)}" ${picks.extra.includes(s.id) ? "checked" : ""}> ${esc(s.heading || "(whole note)")}<span class="tok">${fmtNum(s.tokens)}</span></label>`).join("")}</div>`;
    }).join("");
    $("outline").innerHTML = rows || "<span class='muted small'>Nothing matches.</span>";
    $("outline").querySelectorAll("input").forEach((box) => box.addEventListener("change", () => {
      const id = box.dataset.id;
      picks.extra = picks.extra.filter((x) => x !== id);
      picks.exclude = picks.exclude.filter((x) => x !== id);
      if (box.checked) picks.extra.push(id);
      saveConfirmDraft(); requestEstimate();
    }));
  }

  // Spec 5.8: every member receives the whole vault, so the picker is one
  // list of pages, as on the Ask the vault screen. The per-member ranking of
  // 5.1 and its controls come back only when the vault does not fit.
  function renderWholeVault(e) {
    const filter = ($("outline-filter").value || "").toLowerCase();
    const hit = (path) => !filter || path.toLowerCase().includes(filter);
    const row = (s) => `
      <li ${hit(s.path) ? "" : "hidden"}><input type="checkbox" data-id="${esc(s.path)}" ${picks.exclude.includes(s.path) ? "" : "checked"} title="Untick to leave this page out for every member"> ${esc(s.path)}${s.core ? ' <span class="core-tag" title="Every member receives this">core</span>' : ""}<span class="tok">${fmtNum(s.tokens)}</span></li>`;
    const pages = e.pages || [];
    $("estimate-notes").innerHTML = `<ul class="sec-list">${pages.map(row).join("")}</ul>`;
    $("estimate-notes").querySelectorAll("input").forEach((box) => box.addEventListener("change", () => {
      const id = box.dataset.id;
      picks.exclude = picks.exclude.filter((x) => x !== id);
      if (!box.checked) picks.exclude.push(id);
      saveConfirmDraft(); requestEstimate();
    }));
  }

  // Spec 5.8: every member receives the whole vault, so the picker is one
  // list of pages, as on the Ask the vault screen. The per-member ranking of
  // 5.1 and its controls come back only when the vault does not fit.
  function renderWholeVault(e) {
    const filter = ($("outline-filter").value || "").toLowerCase();
    const row = (s) => `
      <li ${!filter || s.path.toLowerCase().includes(filter) ? "" : "hidden"}><input type="checkbox" data-id="${esc(s.path)}" ${picks.exclude.includes(s.path) ? "" : "checked"} title="Untick to leave this page out for every member"> ${esc(s.path)}${s.core ? ' <span class="core-tag" title="The shared core">core</span>' : ""}<span class="tok">${fmtNum(s.tokens)}</span></li>`;
    $("estimate-notes").innerHTML = `<ul class="sec-list">${(e.pages || []).map(row).join("")}</ul>`;
    $("estimate-notes").querySelectorAll("input").forEach((box) => box.addEventListener("change", () => {
      const id = box.dataset.id;
      picks.exclude = picks.exclude.filter((x) => x !== id);
      if (!box.checked) picks.exclude.push(id);
      saveConfirmDraft(); requestEstimate();
    }));
  }

  function renderMemberSections(e) {
    const briefs = e.briefs || {};
    const split = e.split || {};
    const row = (s) => `
      <li><input type="checkbox" data-id="${esc(s.id)}" ${picks.exclude.includes(s.id) ? "" : "checked"} title="Untick to leave this out for every member"> ${esc(s.path)}${s.heading ? ` · ${esc(s.heading)}` : ""}${s.core ? ' <span class="core-tag" title="Every member receives this">core</span>' : ""}${s.forced ? ' <span class="forced">your pick</span>' : ""}<span class="tok">${fmtNum(s.tokens)}</span>${s.reason ? `<span class="reason">${esc(s.reason)}</span>` : ""}</li>`;
    const briefRow = (b) => `
      <li><input type="checkbox" data-id="${esc(b.path)}" ${picks.exclude.includes(b.path) ? "" : "checked"} title="Untick to leave this page out for every member"> ${esc(b.path)} <span class="brief-tag" title="One line, not the page">summary</span>${b.reason ? `<span class="reason">${esc(b.reason)}</span>` : (b.summary ? `<span class="reason">${esc(b.summary)}</span>` : "")}</li>`;
    $("estimate-notes").innerHTML = `<dl>${Object.entries(e.sections || {}).map(([m, secs]) => {
      const sp = split[m];
      const sizes = sp ? `<span class="muted small"> core ${fmtNum(sp.core)} · own ${fmtNum(sp.own)} · summaries ${fmtNum(sp.brief)} tokens</span>` : "";
      const full = secs.length ? `<p class="tier">In full${sizes}</p><ul class="sec-list">${secs.map(row).join("")}</ul>` : "<span class='muted'>nothing from the vault</span>";
      const brief = (briefs[m] || []).length ? `<p class="tier">As one line each</p><ul class="sec-list">${briefs[m].map(briefRow).join("")}</ul>` : "";
      return `<dt>${esc(m)}${e.picked_by && e.picked_by[m] === "model" ? ' <span class="mode-mark" title="The model chose these">AI pick</span>' : ""}</dt><dd>${full}${brief}</dd>`;
    }).join("")}</dl>`;
    $("estimate-notes").querySelectorAll("input").forEach((box) => box.addEventListener("change", () => {
      const id = box.dataset.id;
      picks.exclude = picks.exclude.filter((x) => x !== id);
      if (!box.checked) { picks.exclude.push(id); picks.extra = picks.extra.filter((x) => x !== id); }
      saveConfirmDraft(); requestEstimate();
    }));
  }

  function requestEstimate() {
    if (estimateTimer) clearTimeout(estimateTimer);
    estimateTimer = setTimeout(async () => {
      if (!session || $("screen-confirm").hidden) return;
      const seq = ++estimateSeq;
      const members = picked($("confirm-members"));
      if (!members.length) { $("estimate").textContent = "Tick at least one member."; return; }
      try {
        const e = await api("POST", `/api/sessions/${session.id}/estimate`, {
          members, mode: modeOf("run-mode"), budget: Number($("budget").value), selection: selectionOf(),
          topic: $("in-topic").value, context: $("in-context").value,
          options: lines($("in-options").value), constraints: lines($("in-constraints").value),
          extra: picks.extra, exclude: picks.exclude, outline: !outlineCache,
        });
        if (seq !== estimateSeq) return;
        const learned = e.overhead_learned_from ? `overhead of ${fmtNum(e.overhead_per_call)} per call learned from ${e.overhead_learned_from} real call(s) of this topic` : `overhead of ${fmtNum(e.overhead_per_call)} per call assumed until the first real call`;
        const forced = e.forced_tokens ? ` · <strong>${fmtNum(e.forced_tokens)} tokens</strong> from your picks on top of the slider` : "";
        const pickCall = (e.per_call || []).some((c) => c.label === "knowledge pick") ? " (one of them the knowledge pick)" : "";
        const whole = !!e.whole_vault;
        $("estimate").innerHTML = `<strong>${e.calls} model call(s)</strong>${pickCall}, about <strong>${fmtNum(e.tokens_in)} tokens in</strong>`
          + `${whole ? ` · the whole vault, ${(e.pages || []).length} page(s), to every member` : forced} · ${esc(learned)} · tokens are an estimate, calls are exact`;
        // Spec 5.8, decision 3: the slider, the selection and the per-member
        // lists mean something only when the vault does not fit one read.
        ["budget-eyebrow", "budget-row", "selection-eyebrow", "selection-row", "picks-actions", "outline-block"]
          .forEach((id) => { if ($(id)) $(id).hidden = whole; });
        if (whole) { $("budget-info").hidden = true; $("selection-info").hidden = true; $("pick-status").textContent = ""; }
        $("estimate-summary").textContent = whole
          ? `Pages read for this question · ${(e.pages || []).length} page(s)`
          : "What each member would receive, with the reasons, and what to add or leave out";
        lastEstimate = e;
        if (whole) renderWholeVault(e); else renderMemberSections(e);
        if (e.outline) { outlineCache = e.outline; renderOutline(); }
      } catch (err) {
        if (seq === estimateSeq) $("estimate").textContent = `No estimate: ${err.message}`;
      }
    }, 250);
  }

  async function goBack() {
    if (!session) return newTopic();
    if (!$("screen-home").hidden) return;                 // nothing behind the home screen
    try {
      session = await api("POST", `/api/sessions/${session.id}/back`);
      render();
    } catch (err) {
      // Nothing to return to on the server: the home screen with the question kept,
      // the topic still reachable with Forward.
      $("question").value = session.question;
      show("home"); renderSteps(); pushHistory();
    }
  }

  async function goForward() {
    if (!session) return;
    if (!$("screen-home").hidden) { render(); return; }    // back into the open topic
    try { session = await api("POST", `/api/sessions/${session.id}/forward`); render(); }
    catch (err) { setError("home-error", err.message); }
  }

  async function runBoard() {
    const members = picked($("confirm-members"));
    if (!members.length) { showError("Tick at least one member to ask."); return; }
    const body = {
      topic: $("in-topic").value, context: $("in-context").value,
      options: lines($("in-options").value), constraints: lines($("in-constraints").value),
      members, mode: modeOf("run-mode"), budget: Number($("budget").value), selection: selectionOf(),
      extra: picks.extra, exclude: picks.exclude,
    };
    try { session = await api("POST", `/api/sessions/${session.id}/run`, body); render(); }
    catch (err) { showError(err.message); }
  }

  // One screen for "in session", "consolidating" and the result: the member
  // tiles stay in place and become clickable once their answer is in; the
  // "Board direction" box below turns into the recommendation.

  function memberStateLabel(name, state, answered, failed) {
    if (failed.has(name) || state === "failed") return "failed";
    const a = answered.get(name);
    if (a) {
      if (a.applies === false) return "not affected";
      return openMembers.has(name) ? "hide answer" : "read answer";
    }
    return state === "pending" ? "waiting" : state === "running" ? "thinking…" : state === "done" ? "answered" : "failed";
  }

  // A tile is clickable as soon as that member's answer is in (9 September
  // 2026: read the early answers while the others still think).

  function renderTiles(answered, failed) {
    $("member-grid").innerHTML = Object.entries(session.members).map(([name, state]) => {
      const a = answered.get(name);
      const clickable = !!a && !failed.has(name);
      const classes = ["member-tile", state,
        clickable ? "clickable" : "",
        openMembers.has(name) ? "active" : "",
        a && a.applies === false ? "na" : ""].filter(Boolean).join(" ");
      return `<button type="button" class="${classes}" data-member="${esc(name)}" style="color:${esc(meta(name).color)}" ${clickable ? "" : "disabled"}>
        ${avatar(name)}<span class="name">${esc(name)}</span><span class="state">${esc(memberStateLabel(name, state, answered, failed))}</span></button>`;
    }).join("");
    $("member-grid").querySelectorAll("button.clickable").forEach((tile) => tile.addEventListener("click", () => {
      const name = tile.dataset.member;
      if (openMembers.has(name)) openMembers.delete(name); else openMembers.add(name);
      renderTiles(answered, failed);
      renderMemberCards(answered);
    }));
  }

  // Spec 5.8, decision 5: what each member is writing, while it writes it.
  // The finished, checked entry replaces it when the call lands.
  function renderLive() {
    const live = (session && session.live) || {};
    const members = Object.keys(live).filter((m) => m && !(session.partial || {})[m]);
    $("live-members").innerHTML = members.map((m) => `
      <div class="live-answer"><p class="live-who" style="color:${esc(meta(m).color)}">${esc(m)} is writing…</p>
      <div class="a">${md(live[m])}</div></div>`).join("");
    $("live-members").hidden = !members.length;
    const direction = live[""] || "";
    if (direction) $("synthesis-body").innerHTML = `<div class="live-answer"><div class="a">${md(direction)}</div></div>`;
  }

  function renderRunning() {
    $("screen-result").dataset.phase = session.phase;
    const synthesising = session.phase === "synthesising";
    const n = Object.keys(session.members).length;
    const count = Object.values(session.members).filter((s) => s === "done").length;
    $("result-topic").textContent = (session.inputs && session.inputs.topic) || session.question || "";
    const early = new Map(Object.entries(session.partial || {}));
    const combined = session.mode === "combined";
    $("board-state").textContent = combined
      ? `The board is in session, combined: one call writes the entries of ${n} member(s) and the direction together.`
      : synthesising
        ? "Every member has answered. One more call reads every answer and writes the board direction."
        : `The board is in session: ${n} member(s), each answering without seeing the others.${early.size ? " Answers already in can be read now." : ""}`;
    const failedNow = new Map(Object.entries(session.members).filter(([, s]) => s === "failed").map(([m]) => [m, m]));
    const key = `${session.phase}:${Object.values(session.members).join(",")}:${early.size}:${Array.from(openMembers).join(",")}`;
    if ($("member-grid").dataset.key !== key) {     // redraw only on change: keeps the open cards steady
      $("member-grid").dataset.key = key;
      renderTiles(early, failedNow);
      renderMemberCards(early);
    }
    renderLive();
    setError("failed-members", "");
    const card = $("synthesis-card");
    card.className = `card synthesis direction-tile ${synthesising ? "running" : "pending"}`;
    card.querySelector(".avatar").innerHTML = `<svg viewBox="0 0 24 24">${SYNTHESIS_ICON}</svg>`;
    $("direction-state").textContent = combined
      ? "Thinking: one call writes every member's entry and the direction…"
      : synthesising
        ? "Thinking: reading all answers, weighing the disagreements, writing the recommendation…"
        : `Waits for every member to answer (${count} of ${n} so far)`;
    $("direction-spinner").hidden = !(synthesising || combined);
    if (combined) card.className = "card synthesis direction-tile running";
    if (!((session.live || {})[""])) $("synthesis-body").innerHTML = "";
    $("ask-back").hidden = true;
    show("result");
  }

  function renderSources(result) {
    const src = result.sources || {};
    const d = result.synthesis_data || {};
    const net = (src.network || []).length
      ? `<ul class="bullets">${src.network.map((n) => `<li>${esc(n)}</li>`).join("")}</ul>`
      : "<span class='muted'>no note was cited by any member</span>";
    const rests = (d.rests_on_judgement || []).length
      ? fmt(d.rests_on_judgement)
      : `<span class='muted'>${src.judgement_count ? `${src.judgement_count} statement(s) of the members' own judgement, none named as decisive` : "nothing"}</span>`;
    // Not confirmed, not wrong (10 September 2026): the model named a page
    // that was not among the ones sent to that member. The statement may be
    // right; nothing the board read backs it.
    const bad = (src.unverified || []).length
      ? `<dt class="warn">To check</dt><dd><ul class="bullets sources">${src.unverified.map((u) => `<li class="warn"><span class="mark">?</span> ${esc(u)}</li>`).join("")}</ul>
         <p class="muted small">These name a page that was not among the ones sent. They may still be right; nothing the board read confirms them.</p></dd>`
      : "";
    return `<div class="sources-block"><dl>
      <dt>From the knowledge net</dt><dd>${net}</dd>
      <dt>Rests on judgement</dt><dd>${rests}</dd>
      ${bad}
    </dl></div>`;
  }

  function renderSynthesis(result) {
    const d = result.synthesis_data;
    if (!d) return `<p class="error">${esc(result.synthesis)}</p>${renderSources(result)}`;
    const list = (items) => (items && items.length) ? `<ul>${items.map((i) => `<li>${esc(i)}</li>`).join("")}</ul>` : "<span class='muted'>none stated</span>";
    const notAffected = d.not_affected && d.not_affected.length ? `<dt>Not affected</dt><dd>${list(d.not_affected)}</dd>` : "";
    return `
      <div class="rec">${esc(d.overall_recommendation)}</div>
      <dl>
        <dt>Decisive criterion</dt><dd>${fmt(d.decisive_criterion)}</dd>
        <dt>Counter-arguments</dt><dd>${list(d.counter_arguments)}</dd>
        <dt>What would change it</dt><dd>${fmt(d.what_would_change_it)}</dd>
        ${notAffected}
      </dl>
      <div class="disagree"><dl><dt>Disagreements</dt><dd>${list(d.disagreements)}</dd></dl></div>
      ${renderSources(result)}`;
  }

  function renderTurn(t) {
    const who = t.members && t.members.length ? `<div class="members">Asked again${t.mode === "combined" ? " (combined, one call)" : ""}: ${t.members.map(esc).join(", ")}</div>` : "";
    if (t.pending) return `<div class="turn pending"><div class="q">${esc(t.question)}</div>${who}<div class="a">The board is thinking…</div></div>`;
    if (t.error) return `<div class="turn error"><div class="q">${esc(t.question)}</div>${who}<div class="a">${esc(t.answer)}</div></div>`;
    const d = t.data;
    let body;
    if (d) {
      body = `<div class="a">${fmt(d.answer)}</div><dl>
        <dt>Reasons</dt><dd>${fmt(d.reasons)}</dd>
        <dt>Recommendation now</dt><dd>${esc(d.recommendation_now || "")}</dd>
        <dt>Disagreements</dt><dd>${fmt(d.disagreements)}</dd></dl>`;
    } else {
      body = `<div class="a">${fmt(t.answer)}</div>`;
    }
    const answers = (t.assessments || []).map((a) => `
      <div class="turn-member" style="border-left-color:${esc(meta(a.member).color)}">
        <div class="who">${esc(a.member)}${a.applies === false ? ' <span class="na-note">· not affected</span>' : ""}</div>
        ${memberBody(a)}
      </div>`).join("");
    const fresh = (t.new_pages || []).length
      ? `<p class="fresh-pages small">Read from the vault for this question: ${t.new_pages.map(esc).join(" · ")}</p>` : "";
    const failed = (t.failed_members || []).length ? `<p class="error small">Failed: ${t.failed_members.map(esc).join(" · ")}</p>` : "";
    const details = answers ? `<details class="turn-members"><summary class="muted small">What each member said</summary>${answers}</details>` : "";
    return `<div class="turn"><div class="q">${esc(t.question)}</div>${who}${body}${fresh}${details}${failed}</div>`;
  }

  function renderResult() {
    const r = session.result;
    const answered = new Map(r.assessments.map((a) => [a.member, a]));
    const failed = new Map(r.failed_members.map((f) => [f.split(":")[0], f]));
    const alreadyShown = !$("screen-result").hidden && $("screen-result").dataset.phase === "result";
    if (!alreadyShown) {
      $("screen-result").dataset.phase = "result";
      $("member-grid").dataset.key = "";
      $("result-topic").textContent = r.topic;
      $("board-state").textContent = `${r.assessments.length} member(s) answered. Click a member to read its answer.`;
      renderTiles(answered, failed);
      renderMemberCards(answered);
      setError("failed-members", r.failed_members.length ? `Failed member(s): ${r.failed_members.join(" · ")}` : "");
      const card = $("synthesis-card");
      card.className = "card synthesis direction-tile done";
      card.querySelector(".avatar").innerHTML = `<svg viewBox="0 0 24 24">${SYNTHESIS_ICON}</svg>`;
      $("direction-state").textContent = r.mode === "combined"
        ? `Combined answer: ${r.assessments.length} member entries and the direction from one call`
        : `Synthesis of ${r.assessments.length} independent assessment(s)`;
      $("board-state").innerHTML = `${r.assessments.length} member(s) answered. Click a member to read its answer.` +
        (r.mode === "combined" ? ` <span class="mode-mark" title="One call wrote every entry; the entries can lean towards each other.">combined</span>` : "");
      $("direction-spinner").hidden = true;
      $("synthesis-body").innerHTML = renderSynthesis(r);
      const board = (session.roles && session.roles.members && session.roles.members.length) ? session.roles.members : Object.keys(session.members);
      renderPicks($("followup-members"), board.filter((n) => !failed.has(n)), new Set());
      $("followup-members").querySelectorAll("label").forEach((label) => {
        const name = label.querySelector("input").value;
        if (!(name in session.members)) label.insertAdjacentHTML("beforeend", ' <span class="muted small">not asked yet</span>');
      });
      $("ask-back").hidden = false;
    }
    const last = session.turns[session.turns.length - 1];
    const turnsKey = `${session.turns.length}:${last ? `${last.pending}:${(last.answer || "").length}:${(last.assessments || []).length}` : ""}`;
    if ($("turns").dataset.key !== turnsKey) {     // redraw only on change: keeps <details> open while polling
      $("turns").innerHTML = session.turns.map(renderTurn).join("");
      $("turns").dataset.key = turnsKey;
    }
    const closed = ["closed", "written"].includes(session.phase);
    $("followup-form").hidden = closed;
    $("board-closed").hidden = !closed;
    if (closed) {
      $("board-closed-text").textContent = session.written_path
        ? `This topic is closed. A note was written to your vault: ${session.written_path}`
        : "This topic is closed. It stays here to read; it takes no further questions.";
    }
    $("btn-followup").disabled = session.busy;
    $("btn-close").disabled = session.busy;
    const again = picked($("followup-members")).length;
    $("followup-mode-row").hidden = again === 0;
    const combinedFollow = modeOf("followup-mode") === "combined";
    $("result-hint").textContent = `${session.llm_calls} model call(s) so far · this follow-up costs ${again ? (combinedFollow ? "one (combined)" : `${again + 1} (${again} member(s) asked again, plus one)`) : "one"}`;
    setError("result-error", session.error || "");
    show("result");
  }

  function renderMemberCards(answered) {
    $("member-cards").innerHTML = Object.keys(session.members).filter((n) => openMembers.has(n) && answered.has(n)).map((name) => {
      const a = answered.get(name);
      return `<div class="card member-card" style="border-left-color:${esc(meta(name).color)}">
        <div class="card-head">${avatar(name)}<div><div class="card-title">${esc(name)}</div><div class="muted small">${esc(meta(name).title)}${meta(name).level ? ` · level ${esc(meta(name).level)}` : ""}${(meta(name).roles || []).length > 1 ? ` · ${esc(meta(name).roles.length)} roles` : ""}</div></div></div>
        ${memberBody(a)}
      </div>`;
    }).join("");
  }

  async function followUp(event) {
    event.preventDefault();
    const question = $("followup").value.trim();
    if (!question) return;
    const members = picked($("followup-members"));
    try {
      session = await api("POST", `/api/sessions/${session.id}/follow-up`, { question, members, mode: modeOf("followup-mode") });
      $("followup").value = "";
      saveDraft({ followup: "" });
      $("followup-members").querySelectorAll("input").forEach((box) => { box.checked = false; box.closest("label").classList.add("off"); });
      render();
    } catch (err) { setError("result-error", err.message); }
  }

  async function closeTopic(remember) {
    setError("close-error", "");
    justClosed = true;                    // this screen closed it: the confirmation follows
    try {
      session = await api("POST", `/api/sessions/${session.id}/close`, { remember });
      $("close-dialog").hidden = true;
      render();
    } catch (err) { setError("close-error", err.message); }
  }

  function knowledgeSplitTable() {
    const split = (PM.route() === "board" && session && session.knowledge_split) || {};
    const names = Object.keys(split);
    if (!names.length) return "";
    const rows = names.map((m) => `<tr><td>${esc(m)}</td><td class="num">${fmtNum(split[m].core)}</td><td class="num">${fmtNum(split[m].own)}</td><td class="num">${fmtNum(split[m].brief)}</td></tr>`).join("");
    return `<p class="eyebrow">Knowledge per member <span class="muted small">tokens by tier, last run · selection: ${esc(session.selection === "python" ? "Python only" : session.pick_state === "done" ? "AI assisted" : "AI assisted, Python fallback")}</span></p>
      <table class="split"><thead><tr><th>Member</th><th class="num">Shared core</th><th class="num">Own sections</th><th class="num">Summaries</th></tr></thead><tbody>${rows}</tbody></table>`;
  }

  function renderDone() {
    forgetSession();
    if (session.phase === "written") {
      $("done-title").textContent = "Written to your vault";
      $("done-detail").textContent = session.written_path;
    } else {
      $("done-title").textContent = "Topic closed";
      $("done-detail").textContent = `Nothing was written. ${session.llm_calls} model call(s) in total.`;
    }
    show("done");
  }

  function newTopic(keepQuestion) {
    stopPolling();
    const question = session ? session.question : (store.get("question") || "");
    if (session && (["clarifying", "running", "synthesising", "proposing"].includes(session.phase) || session.busy)) {
      api("POST", `/api/sessions/${session.id}/abandon`).catch(() => {});   // stop the running calls
    }
    forgetSession();
    session = null;
    openMembers.clear();
    $("question").value = keepQuestion ? question : "";
    store.set("question", $("question").value);
    $("followup").value = "";
    PM.setPath("/board");
    show("home"); renderSteps();
    $("question").focus();
  }

  async function resumeSession() {
    // A reload or a closed tab must not lose the topic: the server still
    // holds the session, the browser remembers which one.
    const id = store.get("session");
    if (!id) return false;
    try {
      const data = await api("GET", `/api/sessions/${id}`);
      if (["closed", "written"].includes(data.phase)) { forgetSession(); return false; }
      session = data;
      if (session.phase === "result") $("followup").value = draft().followup || "";
      return true;
    } catch (e) {
      store.del("session");
      return false;
    }
  }

  $("ask-form").addEventListener("submit", ask);
  $("question").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) ask(e); });

  $("btn-answers").addEventListener("click", () => submitAnswers(false));

  $("btn-answers-final").addEventListener("click", () => submitAnswers(true));

  $("followup-members").addEventListener("change", () => { if (session && session.phase === "result") renderResult(); });

  $("followup-mode").addEventListener("change", () => { if (session && session.phase === "result") renderResult(); });

  $("btn-mode-info").addEventListener("click", () => { $("mode-info").hidden = !$("mode-info").hidden; });

  $("btn-budget-info").addEventListener("click", () => { $("budget-info").hidden = !$("budget-info").hidden; });

  $("btn-selection-info").addEventListener("click", () => { $("selection-info").hidden = !$("selection-info").hidden; });

  $("btn-picks-accept").addEventListener("click", () => { picks.exclude = []; saveConfirmDraft(); requestEstimate(); });

  $("btn-picks-python").addEventListener("click", () => { setSelection("python"); $("selection-mode").onchange(); });

  $("btn-followup-mode-info").addEventListener("click", () => { $("mode-info").hidden = false; $("mode-info").scrollIntoView({ block: "center" }); });

  $("btn-back-home").addEventListener("click", () => newTopic(false));

  $("btn-run").addEventListener("click", runBoard);

  $("btn-back-questions").addEventListener("click", goBack);

  $("followup-form").addEventListener("submit", followUp);

  $("followup").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) followUp(e); });

  $("btn-close").addEventListener("click", () => { setError("close-error", ""); $("close-dialog").hidden = false; });

  $("btn-close-cancel").addEventListener("click", () => { $("close-dialog").hidden = true; });

  $("btn-close-no").addEventListener("click", () => closeTopic(false));

  $("btn-close-yes").addEventListener("click", () => closeTopic(true));

  $("btn-new").addEventListener("click", () => newTopic(false));

  $("btn-error-home").addEventListener("click", () => newTopic(true));

  $("outline-filter").addEventListener("input", () => {
    if (lastEstimate && lastEstimate.whole_vault) renderWholeVault(lastEstimate); else renderOutline();
  });

  PM.steps().querySelectorAll(".step").forEach((button) => button.addEventListener("click", () => goToStep(Number(button.dataset.step))));

  $("btn-error-back").addEventListener("click", goBack);

  $("question").addEventListener("input", () => store.set("question", $("question").value));

  $("followup").addEventListener("input", () => saveDraft({ followup: $("followup").value }));

  $("question").value = store.get("question") || "";

  // The board's memory step goes through the shell's proposal screen.
  const memoryHandlers = {
    write: async (body) => { session = await api("POST", `/api/sessions/${session.id}/memory`, body); render(); },
    discard: async () => { session = await api("POST", `/api/sessions/${session.id}/discard-memory`); render(); },
  };

  async function openTopic(id) {
    // A topic from the home page's recent work (spec 11.1, decision 5): the
    // server still holds it; the address becomes /board once it is open.
    stopPolling();
    try {
      const data = await api("GET", `/api/sessions/${id}`);
      session = data;
      justClosed = false;                 // a closed topic opened from the archive is read, not closed again
      openMembers.clear();
      $("screen-result").dataset.phase = "";
      $("turns").dataset.key = "";
      if (session.phase === "result") $("followup").value = draft().followup || "";
      PM.setPath("/board", true);      // the /board/<id> entry is replaced, never kept
      lastPhase = null;
      render();
    } catch (err) { setError("home-error", err.message); PM.setPath("/board", true); show("home"); renderSteps(); }
  }

  PM.register({
    id: "board",
    match: (pathname) => (pathname === "/board" ? "board" : pathname.startsWith("/board/") ? "board-topic" : null),
    render: (route) => {
      if (route === "board-topic") return openTopic(location.pathname.split("/")[2]);
      if (session) render(); else { show("home"); renderSteps(); }
    },
    onEnter: () => { lastPhase = null; },
    onLeave: () => { stopPolling(); PM.steps().hidden = true; },
    onPopState: async () => {
      if (!session) return;
      steppingBack = true;
      try { await ($("screen-home").hidden ? goBack() : goForward()); } finally { steppingBack = false; }
    },
    boot: () => { $("question").value = store.get("question") || ""; return resumeSession(); },
    stats: () => session,
    statsExtra: knowledgeSplitTable,
  });
})();
