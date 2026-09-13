/* Program Mind - the Ask the vault agent (spec section 10). Loaded after
   shell.js; registers itself with the shell and owns the thread screens. */
(function () {
  "use strict";
  const PM = window.PM;
  const { $, esc, api, store, show, setError, fmtNum, md, fmtDate } = PM;

  let thread = null;                 // the open thread's snapshot

  let threadPollTimer = null;

  let askPicks = { extra: [], exclude: [] };

  let askEstimateTimer = null;

  let askEstimateSeq = 0;

  // Spec 5.7: one picker, under whichever question box is on screen.
  function movePicker(where) {
    const picker = $("ask-picker");
    const home = where === "ask" ? $("ask-picker-home") : null;
    if (home) { if (picker.parentElement !== home) home.appendChild(picker); return; }
    const form = $("thread-form");
    if (picker.parentElement !== form) form.insertBefore(picker, form.querySelector(".row.between"));
  }

  function stopThreadPolling() { if (threadPollTimer) { clearInterval(threadPollTimer); threadPollTimer = null; } }

  function startThreadPolling() {
    stopThreadPolling();
    threadPollTimer = setInterval(async () => {
      if (!thread || PM.route() !== "thread") return stopThreadPolling();
      try { thread = await api("GET", `/api/ask/${thread.id}`); renderThread(); }
      catch (err) { stopThreadPolling(); setError("thread-error", err.message); }
    }, 500);       // spec 4.1, decision 7: often enough for the answer to grow on the page
  }
  // A small Markdown: paragraphs, bullet and numbered lists, bold, code. Escaped first.

  function obsidianLink(path) {
    const file = path.replace(/\.md$/i, "");
    return `obsidian://open?vault=${encodeURIComponent(PM.config.vault_name || "")}&file=${encodeURIComponent(file)}`;
  }

  async function loadThreads() {
    const box = $("thread-list");
    try {
      const data = await api("GET", "/api/ask");
      // Only open threads (spec 11.1, decision 10): a closed one moves to
      // the archive by itself and stays readable there.
      const rows = (data.threads || []).filter((r) => r.status !== "closed");
      box.innerHTML = rows.length ? rows.map((r) => `
        <div class="thread-row ask-row" data-id="${esc(r.id)}" title="Open this thread">
          <button type="button" class="thread-open">${esc(r.title)}</button>
          <span class="thread-meta">${r.questions} question(s) · ${esc(fmtDate(r.updated))}${r.projects && r.projects.length ? ` · ${esc(r.projects.join(", "))}` : ""}</span>
          <button type="button" class="ghost small-btn thread-delete" title="Delete this thread">Delete</button>
        </div>`).join("") : "<p class='muted small'>No open thread. Ask the first question above; closed threads are in the archive.</p>";
      PM.openOnRowClick(box, (row) => PM.navigate(`/ask/${row.dataset.id}`));
      box.querySelectorAll(".thread-delete").forEach((b) => b.addEventListener("click", async () => {
        const id = b.closest(".thread-row").dataset.id;
        if (!window.confirm("Delete this thread? Its questions and answers are removed; a note written to the vault stays.")) return;
        try { await api("DELETE", `/api/ask/${id}`); loadThreads(); } catch (err) { setError("ask-error", err.message); }
      }));
    } catch (err) { setError("ask-error", err.message); }
  }

  async function newThread(event) {
    event.preventDefault();
    const question = $("ask-question").value.trim();
    if (!question) return;
    setError("ask-error", "");
    $("btn-ask-new").disabled = true;
    try {
      const created = await api("POST", "/api/ask", { projects: PM.projects(), budget: PM.config.ask_budget });
      thread = await api("POST", `/api/ask/${created.id}/question`,
        { question, extra: askPicks.extra, exclude: askPicks.exclude });
      store.del("ask-question");
      $("ask-question").value = "";
      PM.setPath(`/ask/${thread.id}`);
      renderThread();
    } catch (err) { setError("ask-error", err.message); }
    $("btn-ask-new").disabled = false;
  }

  async function openThread(id) {
    try {
      thread = await api("GET", `/api/ask/${id}`);
      askPicks = { extra: thread.extra || [], exclude: thread.exclude || [] };
      $("thread-question").value = store.get(`thread-draft:${id}`) || "";
      renderThread();
    } catch (err) { setError("ask-error", err.message); PM.navigate("/ask"); }
  }

  function renderTurnCard(t) {
    const sources = (t.sources || []).length
      ? `<ul>${t.sources.map((s) => `<li><a href="${obsidianLink(s.path)}" title="Open in Obsidian">${esc(s.path)}</a>${s.heading ? ` · ${esc(s.heading)}` : ""}${s.brief ? ' <span class="brief-tag" title="Only its one-line summary was sent">summary only</span>' : ""}${s.why ? ` <span class="why">${esc(s.why)}</span>` : ""}</li>`).join("")}</ul>`
      : "<span class='muted'>no page named</span>";
    const dropped = t.dropped ? `<p class="muted small">${t.dropped} source(s) the model named were not among the pages sent and were dropped.</p>` : "";
    // Spec 5.4, decision 6: a gap says the pages sent did not hold it, not that the vault does not.
    const gaps = (t.gaps || []).length ? `<div class="gaps"><span class="gaps-title">${t.read_all ? "Not in the vault" : "Not in the pages read"}</span><ul>${t.gaps.map((g) => `<li>${esc(g)}</li>`).join("")}</ul></div>` : "";
    const hint = t.decision_question ? `<p class="hint-board muted">This reads like a decision. <a href="/board" class="to-board">Put it to the Board</a> for an assessment by every swim lane.</p>` : "";
    const parse = t.parse_error ? `<p class="error small">${esc(t.parse_error)}; the text is shown as it came.</p>` : "";
    // Spec 5.4, decision 5: the sources fold up, and the pages read for the
    // question sit in the same fold, the kept ones marked.
    const kept = new Set(t.kept || []);
    const rounds = t.rounds || [];
    const addedIn = {};
    rounds.forEach((r) => (r.read || []).forEach((p) => { if (r.n > 1) addedIn[p] = r.n; }));
    const read = (t.paths || []).length
      ? `<p class="read-title">Pages read for this question · ${t.paths.length}${kept.size ? ` · ${kept.size} kept from earlier turns` : ""}${rounds.length > 1 ? ` · ${rounds.length} rounds` : ""}</p>
         <ul class="read-list">${t.paths.map((p) => `<li>${esc(p)}${kept.has(p) ? ' <span class="kept-tag">kept</span>' : ""}${addedIn[p] ? ` <span class="kept-tag">round ${addedIn[p]}</span>` : ""}</li>`).join("")}</ul>` : "";
    // Spec 5.5: the rounds of the loop, each with what it added and what the check said; the earlier answers folded.
    const roundRows = rounds.length > 1 || (rounds[0] && rounds[0].check && rounds[0].check.note) ? rounds.map((r) => {
      const c = r.check || {};
      const wanted = (c.wanted || []).length ? `<br>The check asked for ${c.wanted.length} more page${c.wanted.length === 1 ? "" : "s"}: ${c.wanted.map(esc).join(" · ")}` : (c.error ? `<br>The check failed: ${esc(c.error)}` : (r.check ? "<br>The check named nothing more." : ""));
      const earlier = r.n < rounds.length ? `<details class="first-answer"><summary class="muted small">The answer of round ${r.n}</summary><div class="a">${md(r.answer)}</div></details>` : "";
      return `<li><strong>Round ${r.n}</strong>${r.n > 1 ? `: read ${(r.read || []).length} more page${(r.read || []).length === 1 ? "" : "s"}` : ""}.${c.note ? ` ${esc(c.note)}` : ""}${wanted}${earlier}</li>`;
    }).join("") : "";
    const second = roundRows ? `<p class="read-title">The loop · ${rounds.length} round${rounds.length === 1 ? "" : "s"}</p><ul class="read-list rounds">${roundRows}</ul>` : "";
    const stopped = t.cap_hit ? `<p class="warn small">Stopped at the round cap; the check still wanted: ${(t.still_wanted || []).map(esc).join(" · ")}.</p>` : "";
    const beyond = (t.left || []).length ? `<p class="warn small">${t.left.length} page${t.left.length === 1 ? "" : "s"} did not fit the ceiling and ${t.left.length === 1 ? "was" : "were"} not read: ${t.left.map(esc).join(" · ")}.</p>` : "";
    const count = (t.sources || []).length;
    return `<div class="bubble"><div class="q">${esc(t.question)}</div></div>
      <div class="turn answer"><div class="a">${md(t.answer)}</div>${parse}${gaps}
      <details class="sources-fold"><summary>Sources · ${count} page${count === 1 ? "" : "s"}${(t.paths || []).length ? ` · ${t.paths.length} read` : ""}</summary>
      <div class="sources">${sources}${dropped}${read}${second}</div></details>${stopped}${beyond}${hint}</div>`;
  }

  // Spec 5.4, decision 9: the steps of the running question, with real numbers.
  function renderSteps() {
    const steps = (thread && thread.steps) || [];
    $("thread-steps").hidden = !steps.length;
    $("thread-pending-text").hidden = !!steps.length;
    $("thread-steps").innerHTML = steps.map((s, i) => `<span class="step ${s.state}"><span class="step-mark">${s.state === "done" ? "✓" : i + 1}</span>${esc(s.label)}${s.state === "current" ? "…" : ""}</span>`).join("");
  }

  function renderThread() {
    if (!thread) return;
    movePicker("thread");        // a new thread opens without passing through render()
    const closed = thread.status === "closed";
    if (thread.phase === "proposing") { show("proposing"); startThreadPolling(); return; }
    if (thread.phase === "proposal" && thread.proposal) { stopThreadPolling(); PM.showProposal(thread.proposal, PM.config.vault_path || "", memoryHandlers); return; }
    $("thread-title").textContent = thread.title || "New thread";
    const calls = (thread.stats && thread.stats.calls || []).length;
    $("thread-state").textContent = `${(thread.turns || []).length} question(s) · ${calls} model call(s)` + (closed ? " · closed" : " · open until you close it");
    const key = `${thread.id}:${(thread.turns || []).length}:${thread.busy}:${thread.status}:${thread.phase}`;
    if ($("thread-turns").dataset.key !== key) {
      $("thread-turns").dataset.key = key;
      $("thread-turns").innerHTML = (thread.turns || []).map(renderTurnCard).join("");
      $("thread-turns").querySelectorAll("a.to-board").forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); PM.navigate("/board"); }));
    }
    $("thread-pending").hidden = !thread.busy;
    if (thread.busy) {
      $("thread-pending-question").textContent = thread.pending_question || "";
      $("thread-pending-text").textContent = thread.phase === "choosing"
        ? "Reading the table of contents and choosing what to read…"
        : "Reading the chosen pages and answering…";
      renderSteps();
      // Spec 4.1: what the model has written so far, in the card the answer will stand in.
      const live = thread.live || "";
      $("thread-live").hidden = !live;
      $("thread-pending").classList.toggle("writing", !!live);
      if (live) $("thread-live-text").innerHTML = md(live);
    } else { $("thread-live").hidden = true; $("thread-pending").classList.remove("writing"); }
    $("thread-form").hidden = closed || thread.busy;
    renderPicksMode();
    $("thread-closed").hidden = !closed || thread.busy;
    if (closed) {
      $("thread-closed-text").textContent = thread.written_path
        ? `This thread is closed. A note was written to your vault: ${thread.written_path}`
        : "This thread is closed. It stays here to read; it takes no further questions.";
    }
    setError("thread-error", thread.error || "");
    show("thread");
    if (thread.busy) startThreadPolling(); else { stopThreadPolling(); if (!closed) requestAskEstimate(); }
  }

  // The picks screen (spec 5.3): the question is asked, the model has
  // chosen, and the form turns into "read these pages?" until Alex reads
  // or cancels. The same slider, list and outline serve both modes.
  let picksFor = null;    // the pending question the current adjustments belong to

  function inPicks() { return !!thread && thread.phase === "picks" && !!thread.pending_question; }

  function renderPicksMode() {
    const picks = inPicks();
    if (picks && picksFor !== thread.pending_question) {
      picksFor = thread.pending_question;
      askPicks = { extra: [], exclude: [] };      // a fresh choice: no adjustments yet
      $("ask-estimate-detail").open = true;
    }
    if (!picks && picksFor) { picksFor = null; $("ask-estimate-detail").open = false; }   // back to the folded estimate
    $("thread-picks-head").hidden = !picks;
    $("thread-question").hidden = picks;
    $("btn-thread-close").hidden = picks;
    $("btn-thread-cancel").hidden = !picks;
    $("btn-thread-ask").textContent = picks ? "Read and answer" : "Ask";
    if (picks) {
      $("thread-picks-question").textContent = thread.pending_question;
      const parts = [];
      if (thread.picks) parts.push(`The vault is larger than one read (the ceiling). The model ranked ${thread.picks.full.length} item(s) first; the rest follows in the vault's order and the ceiling cuts at the end.`);
      else parts.push(`The vault is larger than one read, and the model's ranking did not come back usable${thread.pick_error ? ` (${thread.pick_error})` : ""}: the word ranking leads instead.`);
      if (thread.pick_dropped) parts.push(`${thread.pick_dropped} pick(s) named nothing in the vault and were dropped.`);
      parts.push("After the answer a check looks for pages left unread and swaps them in, once.");
      const keptCount = (thread.kept || []).length, droppedKept = Object.keys(thread.dropped_kept || {}).length;
      if (keptCount) parts.push(`${keptCount} page(s) read earlier in this thread are kept.`);
      if (droppedKept) parts.push(`The model let ${droppedKept} kept page(s) go.`);
      if (thread.contents_trimmed) parts.push("The table of contents was trimmed to fit; a page may be missing.");
      parts.push(`Table of contents: ${fmtNum(thread.contents_tokens)} tokens.`);
      $("thread-picks-status").textContent = parts.join(" ");
    }
  }

  async function readPicks() {
    if (!inPicks()) return;
    setError("thread-error", "");
    try {
      thread = await api("POST", `/api/ask/${thread.id}/read`, { extra: askPicks.extra, exclude: askPicks.exclude });
      renderThread();
    } catch (err) { setError("thread-error", err.message); }
  }

  async function askInThread(event) {
    event.preventDefault();
    if (inPicks()) return readPicks();
    const question = $("thread-question").value.trim();
    if (!question || !thread) return;
    setError("thread-error", "");
    try {
      thread = await api("POST", `/api/ask/${thread.id}/question`, { question, extra: askPicks.extra, exclude: askPicks.exclude });
      $("thread-question").value = "";
      store.del(`thread-draft:${thread.id}`);
      renderThread();
    } catch (err) { setError("thread-error", err.message); }
  }

  async function stopThread() {
    if (!thread) return;
    const typed = thread.pending_question || "";
    try {
      thread = await api("POST", `/api/ask/${thread.id}/stop`);
      if (typed && !$("thread-question").value.trim()) $("thread-question").value = typed;   // nothing typed is lost
      renderThread();
    } catch (err) { setError("thread-error", err.message); }
  }

  async function closeThread(remember) {
    if (!thread) return;
    try {
      thread = await api("POST", `/api/ask/${thread.id}/close`, { remember });
      $("thread-close-dialog").hidden = true;
      renderThread();
    } catch (err) { setError("thread-close-error", err.message); }
  }

  let lastEstimate = null;    // what the last estimate said, for the count of the "not chosen" group

  // Spec 5.6: every page of the vault, ticked, whole; a page beyond the
  // ceiling only when the vault does not fit, where a tick moves it to the front.
  function renderAskSections(e) {
    lastEstimate = e;
    const filter = ($("ask-outline-filter").value || "").toLowerCase();
    const hit = (path, reason) => !filter || path.toLowerCase().includes(filter) || (reason || "").toLowerCase().includes(filter);
    const row = (s) => `
      <li ${hit(s.path, s.reason) ? "" : "hidden"}><input type="checkbox" data-id="${esc(s.path)}" ${askPicks.exclude.includes(s.path) ? "" : "checked"} title="Untick to leave this page out"> ${esc(s.path)}${s.core ? ' <span class="core-tag" title="The shared core">core</span>' : ""}${s.kept ? ' <span class="kept-tag" title="Read for an earlier question of this thread">kept</span>' : ""}${s.forced ? ' <span class="forced">your pick</span>' : ""}<span class="tok">${fmtNum(s.tokens)}</span>${s.reason ? `<span class="reason">${esc(s.reason)}</span>` : ""}</li>`;
    const beyondRow = (o) => `
      <li class="over" ${hit(o.path, o.reason) ? "" : "hidden"}><input type="checkbox" data-id="${esc(o.path)}" data-beyond="1" ${askPicks.extra.includes(o.path) ? "checked" : ""} title="Tick to read this page first; another falls out at the ceiling"> ${esc(o.path)}${o.kept ? ' <span class="kept-tag">kept</span>' : ""} <span class="over-tag">beyond the ceiling</span><span class="tok">${fmtNum(o.tokens || 0)}</span>${o.reason ? `<span class="reason">${esc(o.reason)}</span>` : ""}</li>`;
    const group = (key, title, count, extra, body) => `<details class="group" data-group="${key}" ${openGroups.has(key) ? "open" : ""}><summary><span class="group-title">${title}</span> <span class="count">${count}</span>${extra ? ` <span class="muted small">${extra}</span>` : ""}</summary>${body}</details>`;
    const pages = e.pages || [];
    const n = (k) => `${k} page${k === 1 ? "" : "s"}`;
    const who = e.whole_vault ? "Read for this question" : e.picked_by === "model" ? "Ranked by the model, within the ceiling" : "By the word ranking, within the ceiling";
    let html = group("chosen", who, n(pages.length), `${e.whole_vault ? "the whole vault · " : ""}${fmtNum(e.knowledge_tokens)} tokens, whole pages · KPI notes ${fmtNum(e.kpi_tokens)} tokens on top`,
      pages.length ? `<ul class="sec-list">${pages.map(row).join("")}</ul>` : "<p class='muted small'>Nothing from the vault.</p>");
    const droppedKept = (thread && !thread.whole_vault && thread.dropped_kept) || {};
    if (Object.keys(droppedKept).length) {
      html += group("kept", "Let go by the model", n(Object.keys(droppedKept).length), "read for an earlier question; ranked last for this one",
        `<ul class="sec-list">${Object.keys(droppedKept).map((p) => `<li class="muted">${esc(p)}${droppedKept[p] ? `<span class="reason">${esc(droppedKept[p])}</span>` : ""}</li>`).join("")}</ul>`);
    }
    if ((e.beyond || []).length) {
      html += group("beyond", "Beyond the ceiling", n(e.beyond.length), `one read may hold ${fmtNum(e.ceiling)} tokens; these did not fit. Tick one to read it first`,
        `<ul class="sec-list">${e.beyond.map(beyondRow).join("")}</ul>`);
    }
    $("ask-pages-summary").textContent = `Pages read for this question · ${n(pages.length)}`
      + ((e.beyond || []).length ? `, ${e.beyond.length} beyond the ceiling` : "");
    $("ask-estimate-notes").innerHTML = html;
    $("ask-estimate-notes").querySelectorAll("details.group").forEach((d) => d.addEventListener("toggle", () => {
      if (d.open) openGroups.add(d.dataset.group); else openGroups.delete(d.dataset.group);
    }));
    $("ask-estimate-notes").querySelectorAll("input").forEach((box) => box.addEventListener("change", () => {
      const id = box.dataset.id;
      if (box.dataset.beyond) {
        askPicks.extra = askPicks.extra.filter((x) => x !== id);
        if (box.checked) askPicks.extra.push(id);
      } else {
        askPicks.exclude = askPicks.exclude.filter((x) => x !== id);
        if (!box.checked) { askPicks.exclude.push(id); askPicks.extra = askPicks.extra.filter((x) => x !== id); }
      }
      requestAskEstimate();
    }));
  }

  const openGroups = new Set();     // which of the folded groups Alex has opened, kept across estimates

  function requestAskEstimate() {
    if (askEstimateTimer) clearTimeout(askEstimateTimer);
    askEstimateTimer = setTimeout(async () => {
      const onAsk = PM.route() === "ask";
      if (!onAsk && (!thread || PM.route() !== "thread" || $("thread-form").hidden)) return;
      const seq = ++askEstimateSeq;
      try {
        const e = await api("POST", onAsk ? "/api/ask/estimate" : `/api/ask/${thread.id}/estimate`, {
          question: onAsk ? $("ask-question").value : (inPicks() ? thread.pending_question : $("thread-question").value),
          projects: onAsk ? PM.projects() : undefined,
          extra: askPicks.extra, exclude: askPicks.exclude,
        });
        if (seq !== askEstimateSeq) return;
        const learned = e.overhead_learned_from ? `overhead of ${fmtNum(e.overhead_per_call)} learned from ${e.overhead_learned_from} real call(s) of this thread` : `overhead of ${fmtNum(e.overhead_per_call)} assumed until the first real call`;
        const who = e.whole_vault ? "the whole vault" : e.picked_by === "model" ? "the model's ranking" : "the word ranking";
        const calls = e.whole_vault ? "1 model call" : (inPicks() ? "2 more model calls per round" : "1 ranking call, then 2 calls per round") + `, up to ${e.max_reads} rounds`;
        $("ask-estimate").innerHTML = `<strong>${calls}</strong> · reads ${(e.pages || []).length} page(s) by ${who}, about <strong>${fmtNum(e.tokens_in)} tokens in</strong> · <span class="muted">${esc(learned)} · tokens are an estimate</span>`
          + ` <button type="button" class="info-btn" id="btn-ask-info" title="How the pages are chosen and read" aria-label="How the pages are chosen and read">i</button>`;
        $("btn-ask-info").addEventListener("click", () => { $("ask-info").hidden = !$("ask-info").hidden; });
        renderAskSections(e);
      } catch (err) {
        if (seq === askEstimateSeq) $("ask-estimate").textContent = `No estimate: ${err.message}`;
      }
    }, 250);
  }


  $("ask-new-form").addEventListener("submit", newThread);

  $("ask-question").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) newThread(e); });

  $("ask-question").addEventListener("input", () => { store.set("ask-question", $("ask-question").value); requestAskEstimate(); });

  $("thread-form").addEventListener("submit", askInThread);

  $("thread-question").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) askInThread(e); });

  $("thread-question").addEventListener("input", () => { if (thread) store.set(`thread-draft:${thread.id}`, $("thread-question").value); requestAskEstimate(); });

  $("btn-ask-info").addEventListener("click", () => { $("ask-info").hidden = !$("ask-info").hidden; });

  $("ask-outline-filter").addEventListener("input", () => { if (lastEstimate) renderAskSections(lastEstimate); });

  $("btn-thread-stop").addEventListener("click", stopThread);

  $("btn-thread-cancel").addEventListener("click", stopThread);   // leaves the picks screen; the question stays typed

  $("btn-thread-close").addEventListener("click", () => { setError("thread-close-error", ""); $("thread-close-dialog").hidden = false; });

  $("btn-thread-close-cancel").addEventListener("click", () => { $("thread-close-dialog").hidden = true; });

  $("btn-thread-close-no").addEventListener("click", () => closeThread(false));

  $("btn-thread-close-yes").addEventListener("click", () => closeThread(true));

  $("ask-question").value = store.get("ask-question") || "";

  const memoryHandlers = {
    write: async (body) => { thread = await api("POST", `/api/ask/${thread.id}/memory`, body); renderThread(); },
    discard: async () => { thread = await api("POST", `/api/ask/${thread.id}/discard-memory`); renderThread(); },
  };

  PM.register({
    id: "ask",
    match: (pathname) => (pathname === "/ask" ? "ask" : pathname.startsWith("/ask/") ? "thread" : null),
    render: (route) => {
      movePicker(route);
      if (route === "ask") { show("ask"); askPicks = { extra: [], exclude: [] }; loadThreads(); requestAskEstimate(); }
      else openThread(location.pathname.split("/")[2]);
    },
    onLeave: stopThreadPolling,
    boot: () => { $("ask-question").value = store.get("ask-question") || ""; },
    stats: () => thread,
  });
})();
