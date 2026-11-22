/* "Ask this document" panel: What does it want? (the letter card), Summarise / What do I need to do? / Dates & money,
   free questions, Explain.
   Answers stream in; [p.3: "quote"] markers become chips that jump to the page and highlight the quote; maths written
   as \( \) / \[ \] (or $ $, $$ $$) is drawn with KaTeX (ui/katex).  Each document's conversation is kept (bridge_ai.py)
   and shown again when it's reopened; New conversation clears it.
   Uses app.js globals (api, S, el, icon, $, $$, toast, fail, setStatus, goTo, openFind, runSearch, findInput). */
"use strict";
(() => {
  // each open document (tab) has its own panel and conversation; A is the one on screen (tabs.js swaps them)
  const fresh = () => ({ open: false, el: null, job: null, poll: 0, history: false, tab: null });
  let A = fresh();
  const BACK = new Map();             // tab id -> its A while another tab is on screen
  let settings = null;                // the AI settings: the same for every tab

  async function refresh() {
    const r = await api.ai_settings_get();
    if (!r.error) settings = r;
    if (A.open && A.el) renderTop();
    return settings;
  }
  const ready = () => !!settings?.ready;

  // ------------------------------------------------------------------ panel
  function build() {
    A.el = el("aside", { class: "aipanel", "aria-label": "Ask about this document", hidden: true },
      el("div", { class: "ai-head" },
        el("span", { class: "ai-mark", "aria-hidden": "true" }, icon("ask")),
        el("strong", { text: "Ask about this document" }),
        el("button", { class: "icon-btn sm", "aria-label": "AI settings", title: "AI settings", onclick: () => window.Settings?.open("ai") }, icon("more")),
        el("button", { class: "icon-btn sm", "aria-label": "Close", title: "Close", onclick: () => toggle(false) }, icon("x"))),
      el("div", { class: "ai-top" }),
      el("div", { class: "ai-chat", role: "log", "aria-live": "polite", oncontextmenu: chatMenu }),
      el("form", { class: "ai-ask", onsubmit: e => { e.preventDefault(); const q = A.el.querySelector(".ai-q"); const v = q.value.trim(); if (v) { q.value = ""; ask("ask", v); } } },
        el("textarea", { class: "ai-q", rows: 2, placeholder: "Ask anything, e.g. “Can I cancel early?”", "aria-label": "Your question",
          onkeydown: e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); e.currentTarget.form.requestSubmit(); } if (e.key === "Escape") { e.stopPropagation(); toggle(false); } } }),
        el("button", { class: "btn btn-primary sm ai-send", type: "submit", "aria-label": "Ask" }, icon("fwd"))),
      el("p", { class: "ai-foot muted", text: "AI can get things wrong - tap a page chip to check the words yourself." }));
    const grip = el("div", { class: "grip side-grip", role: "separator", "aria-orientation": "vertical", tabindex: "0",
      "aria-label": "Ask panel width", title: "Drag to make this panel wider or narrower (double-click to reset)" });
    A.el.prepend(grip);
    window.sideGrip?.(grip);
    $(".stage").append(A.el);
  }

  function renderTop() {
    const top = A.el.querySelector(".ai-top");
    const s = settings;
    if (!s?.cfg?.enabled || !s.ready) {
      const inv = el("input", { class: "st-in", placeholder: "Paste an invitation", spellcheck: "false", autocomplete: "off", "aria-label": "Invitation" });
      const msg = el("p", { class: "small ai-inv-msg", role: "status" });
      const join = async () => {
        if (!inv.value.trim()) { msg.textContent = keys("Paste the invitation first (Ctrl+V)."); return; }
        msg.textContent = "Checking…";
        const r = await api.ai_join(inv.value);
        if (r.error) { msg.className = "small ai-inv-msg ai-err"; msg.textContent = r.error; return; }
        toast(r.status);
        await refresh(); renderTop();
      };
      inv.addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); join(); } });
      top.replaceChildren(el("div", { class: "ai-setup" },
        el("p", {}, el("strong", { text: "Get plain-English answers about this document." }),
          " Summaries, what you need to do, deadlines and fees - with the page each answer comes from."),
        el("p", { class: "muted small", text: "It's off until you choose an AI service. The document's text is only sent when you ask, and you see which pages first." }),
        el("div", { class: "ai-inv" }, el("span", { class: "small", text: "Got an invitation from your family?" }),
          el("div", { class: "st-row" }, inv, el("button", { class: "btn btn-primary sm", text: "Use", onclick: join })), msg),
        el("button", { class: "btn btn-ghost sm", onclick: () => window.Settings?.open("ai") }, icon("ask"), s?.cfg?.enabled ? "Finish setting up" : "Choose an AI service yourself")));
      A.el.querySelector(".ai-ask").hidden = true;
      return;
    }
    A.el.querySelector(".ai-ask").hidden = false;
    const p = s.presets[s.cfg.provider];
    top.replaceChildren(
      el("div", { class: "ai-quick" },
        el("button", { class: "chip", onclick: () => ask("card") }, icon("mail"), "What does it want?"),
        el("button", { class: "chip", onclick: () => ask("summary") }, icon("note"), "Summarise"),
        el("button", { class: "chip", onclick: () => ask("todo") }, icon("tick"), "What do I need to do?"),
        el("button", { class: "chip", onclick: () => ask("dates") }, icon("calendar"), "Dates & money")),
      el("div", { class: "ai-who muted small" }, el("span", { class: "ai-who-text",
        text: p.kind === "relay" ? APP.ai : `${p.short || "Custom"} · ${s.cfg.models[s.cfg.provider] || p.default_model}` }),
        el("button", { class: "link-btn small", text: "New conversation", onclick: newChat })));
    if (p.kind === "relay") {         // the relay chooses the model (not shown to people); show this month's allowance
      api.ai_relay_info().then(r => {
        if (r.error || r.old_relay) return;
        const m = r.month || {}, cur = r.currency === "GBP" ? "£" : "$";
        const who = top.querySelector(".ai-who-text");
        if (who) who.textContent = APP.ai +
          (m.limit != null ? ` · ${cur}${(m.spent || 0).toFixed(2)} of ${cur}${Number(m.limit).toFixed(2)} this month` : "");
      });
    }
  }

  async function toggle(force) {
    if (!A.el) build();
    A.open = force ?? !A.open;
    if (A.open && window.Panels && $(".cpanel:not([hidden])")) window.Panels.toggleComments(false);
    if (A.open) window.Translate?.close();
    A.el.hidden = !A.open;
    $(".stage").classList.toggle("with-panel", A.open || !!$(".cpanel:not([hidden])"));
    $("#btn-ask")?.setAttribute("aria-pressed", String(A.open));
    if (A.open) { await refresh(); renderTop(); await showHistory(); A.el.querySelector(".ai-q")?.focus(); }
  }

  async function newChat() {
    stop();
    await api.ai_reset_chat();
    A.el.querySelector(".ai-chat").replaceChildren();
    A.history = true;
    setStatus("New conversation - the earlier questions about this document are forgotten.");
  }

  /** The questions asked about this document before (kept on this computer): shown once when the panel opens. */
  async function showHistory() {
    if (A.history || !S.doc) return;
    A.history = true;
    const r = await api.ai_history();
    const chat = A.el.querySelector(".ai-chat");
    if (r.error || !r.turns?.length || chat.childElementCount) return;
    const when = new Date(r.turns[r.turns.length - 1].at * 1000);
    chat.append(el("div", { class: "ai-earlier muted small", role: "note" },
      `Earlier questions about this document · ${when.toLocaleDateString(undefined, { day: "numeric", month: "short" })}`,
      el("span", { text: " · kept on this " + (APP.mac ? "Mac" : "PC") + " - New conversation clears them." })));
    for (const t of r.turns) {
      const me = el("div", { class: "ai-msg me" + (t.think ? " think" : "") }, t.think ? el("span", { class: "ai-think-tag", text: "Think it through" }) : "", t.label);
      me._copy = t.label;
      const box = el("div", { class: "ai-text" });
      const plain = t.card ? cardText(t.card) : stripMarkers(t.answer);
      if (t.card) renderCard(box, t.card); else render(box, t.answer);
      const ans = el("div", { class: "ai-msg bot" }, box, el("div", { class: "ai-meta muted small" },
        `Pages ${t.pages}${t.partial ? " (best matches)" : ""} · ${t.provider}${t.think ? " · thought it through" : ""}`,
        el("button", { class: "link-btn small", text: "Copy", onclick: () => copyText(plain, "Answer copied.") }),
        el("button", { class: "link-btn small ai-report", text: "Report this answer", onclick: () => report("answer") })));
      ans._copy = plain;
      chat.append(me, ans);
    }
    chat.scrollTop = chat.scrollHeight;
  }

  // ------------------------------------------------------------------ consent / what's sent
  function confirmSend(pv) {
    const d = el("dialog", { class: "dialog ai-consent" });
    d.innerHTML = `<form method="dialog">
      <div class="dlg-head"><span class="dlg-icon"><svg><use href="#i-ask"/></svg></span><div><h3></h3><p class="muted ai-c-msg"></p></div></div>
      <details class="ai-sent"><summary>Show exactly what's sent</summary><pre></pre></details>
      <label class="check ai-c-remember"><input type="checkbox" checked><span></span></label>
      <div class="dlg-actions"><button class="btn btn-ghost" value="cancel">Cancel</button><button class="btn btn-primary" value="ok">Send and ask</button></div></form>`;
    d.querySelector("h3").textContent = pv.local ? here("Ask the AI on this PC?") : `Send this document's text to ${pv.provider}?`;
    const many = s => s.includes("-") || s.includes(",") ? "s" : "";
    const what = pv.pictures && !pv.chars
      ? `pictures of page${many(pv.pictures)} ${pv.pictures} (about ${pv.picture_mb} MB - this document is a scan)`
      : pv.pictures ? `the text of the document plus pictures of the scanned page${many(pv.pictures)} ${pv.pictures}`
        : pv.ocr ? `the text of page${many(pv.pages)} ${pv.pages}, read from the scanned pages on this ${APP.mac ? "Mac" : "PC"} first`
          : `the text of page${many(pv.pages)} ${pv.pages} (${pv.chars.toLocaleString()} characters${pv.partial ? `, the parts that match your question out of ${pv.page_count} pages` : ""})`;
    d.querySelector(".ai-c-msg").textContent = pv.local
      ? `Ollama runs on this computer, so nothing leaves it. It will read pages ${pv.pages}.`
      : `To answer, ${APP.name} sends ${what} and your question to ${pv.provider}. ` +
        "Nothing is sent anywhere else, and nothing is sent without you asking.";
    d.querySelector("pre").textContent = `Question:\n${pv.prompt}\n\nDocument text:\n${pv.text}`;
    d.querySelector(".ai-c-remember span").textContent = `Don't ask again for ${pv.provider}`;
    d.querySelector(".ai-c-remember").hidden = pv.consent || pv.local;
    document.body.append(d);
    d.showModal();
    d.querySelector(".btn-primary").focus();
    return new Promise(res => d.addEventListener("close", () => {
      const out = d.returnValue === "ok" ? { remember: d.querySelector(".ai-c-remember input").checked } : null;
      d.remove(); res(out);
    }, { once: true }));
  }

  /** Scanned pages are about to be read as text on this computer, because the AI can't see pictures: say what that
      means for the answer before sending.  Resolves true = send anyway. */
  function warnScan(pv) {
    const d = el("dialog", { class: "dialog ai-consent ai-scan-warn" });
    const many = /[-,]/.test(pv.ocr);
    const fix = pv.relay
      ? `Ask the person who runs ${APP.ai} to set a model for scans - it reads the page pictures themselves.`
      : "Claude (Settings → AI assistant) reads the page pictures itself.";
    d.innerHTML = `<form method="dialog">
      <div class="dlg-head"><span class="dlg-icon warn"><svg><use href="#i-alert"/></svg></span><div><h3></h3><p class="muted ai-c-msg"></p></div></div>
      <p class="note-amber small ai-w-fix"></p>
      <label class="check"><input type="checkbox" class="ai-w-stop"><span>Don't warn me about this again</span></label>
      <div class="dlg-actions"><button class="btn btn-ghost" value="cancel">Cancel</button><button class="btn btn-primary" value="ok">Send anyway</button></div></form>`;
    d.querySelector("h3").textContent = `${pv.provider} can't see the scanned page${many ? "s" : ""}`;
    d.querySelector(".ai-c-msg").textContent =
      `Page${many ? "s" : ""} ${pv.ocr} ${many ? "are pictures" : "is a picture"} (a scan or photo), and ${pv.provider} reads text only. ` +
      `So ${APP.name} will read the words on this ${APP.mac ? "Mac" : "PC"} first and send those. Printed text usually comes out fine, ` +
      "but handwriting, ticks and stamps are often misread - and then the answer can be wrong.";
    d.querySelector(".ai-w-fix").textContent = fix;
    document.body.append(d);
    d.showModal();
    d.querySelector(".btn-ghost").focus();                 // the safe choice is the default
    return new Promise(res => d.addEventListener("close", async () => {
      const go = d.returnValue === "ok";
      if (d.querySelector(".ai-w-stop").checked) await api.ai_settings_set({ warn_scans: false });
      d.remove(); res(go);
    }, { once: true }));
  }

  // ------------------------------------------------------------------ report (also Translate's and Pull out data's)
  /** Report this answer: an email to the developer in the person's own email app, with nothing of the document or
      the answer put in it (bridge_ai.ai_report). */
  async function report(what) {
    const r = await api.ai_report(what);
    if (fail(r)) return;
    const d = el("dialog", { class: "dialog ai-consent ai-report-dlg" });
    d.innerHTML = `<form method="dialog">
      <div class="dlg-head"><span class="dlg-icon"><svg><use href="#i-alert"/></svg></span><div><h3></h3><p class="muted ai-r-msg"></p></div></div>
      <p class="small">Write to <strong class="ai-r-to"></strong></p>
      <p class="note-amber small ai-r-note"></p>
      <div class="dlg-actions"><button type="button" class="btn btn-ghost ai-r-copy">Copy the address</button><span class="spacer"></span>
        <button class="btn btn-ghost" value="cancel">Cancel</button><button class="btn btn-primary" value="ok">Open an email</button></div></form>`;
    d.querySelector("h3").textContent = { translation: "Report this translation", data: "Report this pulled-out data" }[what] || "Report this answer";
    d.querySelector(".ai-r-msg").textContent = `Tell the makers of ${APP.name} if what the AI wrote is inappropriate, harmful or badly wrong.`;
    d.querySelector(".ai-r-to").textContent = r.address;
    d.querySelector(".ai-r-note").textContent = "Nothing from your document or the answer is put in the email. Copy in only what " +
      "you're happy to share. Nothing is sent until you press Send in your email app.";
    d.querySelector(".ai-r-copy").onclick = () => copyText(r.address, "Address copied.");
    document.body.append(d);
    d.addEventListener("close", async () => {
      const go = d.returnValue === "ok";
      d.remove();
      if (!go) return;
      const x = await api.ai_report(what, true);
      if (fail(x)) return;
      toast(x.opened ? `Your email app should open. If it doesn't, write to ${r.address}.`
        : `No email app could be opened - write to ${r.address}.`, { ms: 9000 });
    }, { once: true });
    d.showModal();
  }

  // ------------------------------------------------------------------ asking
  const LABELS = { summary: "Summarise this document", todo: "What do I need to do?", dates: "Dates, deadlines & money",
    card: "What does it want?" };

  async function ask(mode, question = "", selection = null, page = null, think = false) {
    if (!A.open) await toggle(true);
    const a = A, tabApi = apiIn(S.tab);           // this tab's panel and document, even if another comes to the front
    const s = await refresh();
    if (!s?.cfg?.enabled || !s.ready) { renderTop(); return window.Settings?.open("ai"); }
    stop(a);
    const pv = await tabApi.ai_preview(mode, question, selection, page);
    if (fail(pv)) return;
    if (pv.ocr && pv.warn_scans && !think && !(await warnScan(pv))) return;
    let remember = true;
    if (!pv.consent || s.cfg.show_sent) {
      if (pv.local && !s.cfg.show_sent) remember = true;
      else {
        const c = await confirmSend(pv);
        if (!c) return;
        remember = c.remember;
      }
    }
    const chat = a.el.querySelector(".ai-chat");
    const qText = mode === "ask" ? question : mode === "explain" ? `Explain: “${selection.length > 140 ? selection.slice(0, 140) + "…" : selection}”` : LABELS[mode];
    const me = el("div", { class: "ai-msg me" + (think ? " think" : "") }, think ? el("span", { class: "ai-think-tag", text: "Think it through" }) : "", qText);
    me._copy = qText;
    chat.append(me);
    const ans = el("div", { class: "ai-msg bot" }, el("div", { class: "ai-text" },
      el("span", { class: "ai-typing", text: think ? "Thinking it through - this can take a minute…" : mode === "card" ? "Reading the letter…" : "Reading the document…" })));
    const meta = el("div", { class: "ai-meta muted small" });
    const stopBtn = el("button", { class: "link-btn small", text: "Stop", onclick: () => stop(a) });
    meta.append(stopBtn);
    ans.append(meta);
    chat.append(ans);
    chat.scrollTop = chat.scrollHeight;
    const r = await tabApi.ai_ask(mode, question, selection, page, remember, think);
    if (r.error) { ans.querySelector(".ai-text").replaceChildren(el("p", { class: "ai-err", text: r.error })); meta.remove(); return; }
    a.job = r.job;
    const tick = async () => {
      if (a.job !== r.job) return;
      const st = await api.ai_status(r.job);
      if (st.error) return;
      if (st.text && mode !== "card") render(ans.querySelector(".ai-text"), st.text);   // the card's JSON isn't shown as it comes
      else if (st.progress) ans.querySelector(".ai-typing") && (ans.querySelector(".ai-typing").textContent = st.progress);
      chat.scrollTop = chat.scrollHeight;
      if (!st.done) { a.poll = setTimeout(tick, 120); return; }
      a.job = null;
      const plain = st.card ? cardText(st.card) : stripMarkers(st.text || "");
      if (st.card) renderCard(ans.querySelector(".ai-text"), st.card);
      else if (mode === "card" && st.text && !st.problem) ans.querySelector(".ai-text").replaceChildren();
      if (st.text) ans._copy = plain;
      if (st.problem) { ans.querySelector(".ai-text").append(el("p", { class: "ai-err", text: st.problem })); }
      if (!st.text && !st.problem) ans.querySelector(".ai-text").replaceChildren(el("p", { class: "muted", text: st.cancelled ? "Stopped." : "No answer came back. Try again." }));
      if (!st.text && st.problem) ans.querySelector(".ai-typing")?.remove();
      const how = st.think ? (st.retried ? " · too much thinking, answered without it" : " · thought it through") : "";
      meta.replaceChildren(
        `Pages ${st.pages}${st.partial ? " (best matches)" : ""} · ${st.provider}${st.elapsed_s != null ? ` · ${st.elapsed_s} s` : ""}${st.truncated ? " · cut short" : ""}${how}`,
        st.text ? el("button", { class: "link-btn small", text: "Copy", onclick: () => copyText(plain, "Answer copied.") }) : "",
        st.text ? el("button", { class: "link-btn small ai-report", text: "Report this answer", onclick: () => report("answer") }) : "",
        // a harder question: ask again, letting the AI think first (slower; the relay's admin decides how much)
        st.can_think && !st.think && !st.cancelled ? el("button", { class: "link-btn small ai-think-btn", title: "Ask again, letting the AI think it through first. Slower, for harder questions.",
          onclick: e => { e.currentTarget.remove(); ask(mode, question, selection, page, true); } }, icon("ask"), "Think it through") : "");
      if (a === A) setStatus(st.problem ? "The AI couldn't answer." : "Answer ready.");
    };
    tick();
  }

  function stop(a = A) {
    clearTimeout(a.poll);
    if (a.job) { api.ai_cancel(a.job); a.job = null; }
  }

  // ------------------------------------------------------------------ tabs (tabs.js)
  // the panel of the tab going to the back is taken off the page (an answer still coming in keeps filling it)
  function tabLeave(id) {
    A.el?.remove();
    A.tab = id;
    BACK.set(id, A);
    A = fresh();
    $("#btn-ask")?.setAttribute("aria-pressed", "false");
  }
  function tabEnter(id) {
    A = BACK.get(id) || fresh();
    BACK.delete(id);
    if (A.el) { $(".stage").append(A.el); A.el.hidden = !A.open; if (A.open) renderTop(); }
    $("#btn-ask")?.setAttribute("aria-pressed", String(A.open));
  }
  function tabClosed(id) {
    const a = BACK.get(id);
    if (!a) return;
    stop(a);
    a.el?.remove();
    BACK.delete(id);
  }

  async function explain(text, page) {
    if (!text?.trim()) return;
    await toggle(true);
    return ask("explain", "", text, page);
  }

  // ------------------------------------------------------------------ rendering answers
  const CITE = /\[p\.?\s?(\d+)(?:\s*[:,-]\s*[“"]([^”"\]]{2,200})[”"])?\]/g;
  // maths: \( \) or $ $ in a sentence; \[ \] or $$ $$ on their own (may run over several lines).  A $ $ pair only counts
  // when it looks like maths, so "$5 and $10" stays money.
  const MATH_INLINE = String.raw`\\\((.+?)\\\)|\$([^\s$](?:[^$\n]*?[^\s$\\])?)\$(?!\d)`;
  const MATH_BLOCK = /\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\]/g;
  const looksLikeMaths = t => /[\\^_{}=]/.test(t);
  const stripMarkers = t => t.replace(CITE, (m, p, q) => q ? `“${q}” (page ${p})` : `(page ${p})`);

  function inline(node, text) {
    // **bold**, citation chips and maths, everything else as plain text (never innerHTML)
    let last = 0;
    const re = new RegExp(`${CITE.source}|\\*\\*([^*]+)\\*\\*|${MATH_INLINE}`, "g");
    for (const m of text.matchAll(re)) {
      if (m[5] != null && !looksLikeMaths(m[5])) continue;          // "$5 or $6" - money, not maths
      if (m.index > last) node.append(text.slice(last, m.index));
      if (m[3] != null) node.append(el("strong", { text: m[3] }));
      else if (m[4] != null || m[5] != null) node.append(maths(m[4] ?? m[5], false, m[0]));
      else node.append(cite(+m[1], m[2]));
      last = m.index + m[0].length;
    }
    if (last < text.length) node.append(text.slice(last));
  }
  /** One formula, drawn by KaTeX (as the LaTeX itself if KaTeX can't). */
  function maths(tex, display, source) {
    const node = el(display ? "div" : "span", { class: display ? "ai-math-block" : "ai-math", "data-copy": source });
    try {
      if (!window.katex) throw new Error("no KaTeX");
      window.katex.render(tex, node, { displayMode: display, throwOnError: false, strict: "ignore", output: "htmlAndMathml" });
    } catch { node.textContent = source; }
    return node;
  }
  function cite(page, quote) {
    return el("button", { class: "ai-cite", title: quote ? `“${quote}” - page ${page}` : `Go to page ${page}`,
      "data-copy": quote ? `“${quote}” (page ${page})` : `(page ${page})`,
      onclick: () => jump(page, quote) }, icon("file"), `p.${page}`);
  }
  function render(box, text) {
    box.replaceChildren();
    let list = null;
    const blocks = [];             // formulas set on their own line: kept whole, even across line breaks
    text = text.replace(MATH_BLOCK, (m, a, b) => `\n\u0001${blocks.push([a ?? b, m]) - 1}\u0001\n`);
    for (const raw of text.split(/\n/)) {
      const line = raw.trim();
      if (!line) { list = null; continue; }
      const mb = /^\u0001(\d+)\u0001$/.exec(line);
      if (mb) { list = null; const [tex, src] = blocks[+mb[1]]; box.append(maths(tex, true, src)); continue; }
      const b = /^([-*•]|\d+[.)])\s+(.*)$/.exec(line);
      if (b) {
        if (!list) { list = el(/^\d/.test(b[1]) ? "ol" : "ul"); box.append(list); }
        const li = el("li"); inline(li, b[2]); list.append(li);
      } else {
        list = null;
        const h = /^#{1,4}\s+(.*)$/.exec(line);
        const p = el(h ? "h4" : "p"); inline(p, h ? h[1] : line); box.append(p);
      }
    }
  }

  // ------------------------------------------------------------------ the letter card (bridge_card.py)
  /** What does it want? - who it's from, what it asks, the deadline, the amount, reference numbers, how to reach them,
      the next step; each with its page.  Amounts, the deadline and references the app couldn't find in the letter's own
      text say so ("found": false), and scanned pages it couldn't look in say that ("found": null). */
  function renderCard(box, c) {
    box.replaceChildren();
    const card = el("div", { class: "ai-card" });
    if (c.empty) {
      card.append(el("p", { class: "muted", text: "This doesn't look like a letter that asks you for anything. Try Summarise instead." }));
      return box.append(card);
    }
    const pageChip = p => p ? cite(p) : "";
    const check = f => f === false ? el("span", { class: "ai-card-warn", text: "Not found in the letter - check it." })
      : f === null ? el("span", { class: "ai-card-warn", text: "Couldn't check this - part of the letter is a scan." }) : "";
    const row = (label, ...value) => card.append(el("div", { class: "ai-card-row" },
      el("span", { class: "ai-card-k", text: label }), el("div", { class: "ai-card-v" }, ...value)));
    if (c.from?.text) row("From", c.from.text, " ", pageChip(c.from.page));
    if (c.about?.text) row("About", c.about.text, " ", pageChip(c.about.page));
    if (c.asks?.length) row("They want", el("ul", {}, ...c.asks.map(x => el("li", {}, x.text, " ", pageChip(x.page)))));
    if (c.deadline?.text) {
      const add = c.deadline.date && c.deadline.found !== false
        ? el("button", { class: "link-btn small", onclick: async () => { const r = await api.ai_card_calendar(c); if (!fail(r)) toast("Opened in your calendar - check it, then save it there."); } },
            icon("calendar"), "Add to calendar") : "";
      row("By", el("strong", { text: c.deadline.text }), " ", c.deadline.found === false ? "" : pageChip(c.deadline.page), add, check(c.deadline.found));
    }
    if (c.amount?.text) row("Amount", el("strong", { text: c.amount.text }), " ", c.amount.found === false ? "" : pageChip(c.amount.page), check(c.amount.found));
    for (const r of c.references || [])          // a number not in the letter: no page to send them to
      row(r.label, el("span", { class: "ai-card-ref", text: r.value }), " ", r.found === false ? "" : pageChip(r.page),
        el("button", { class: "link-btn small", onclick: () => copyText(r.value, `${r.label} copied.`) }, icon("copy"), "Copy"), check(r.found));
    if (c.contact?.text) row("Contact", c.contact.text, " ", pageChip(c.contact.page));
    if (c.next_step?.text) card.append(el("p", { class: "ai-card-next" }, el("strong", { text: "Next: " }), c.next_step.text, " ", pageChip(c.next_step.page)));
    box.append(card);
  }
  /** The card as plain text, for Copy. */
  function cardText(c) {
    if (c.empty) return "This doesn't look like a letter that asks you for anything.";
    const pg = p => p ? ` (page ${p})` : "";
    const out = [];
    if (c.from?.text) out.push(`From: ${c.from.text}${pg(c.from.page)}`);
    if (c.about?.text) out.push(`About: ${c.about.text}${pg(c.about.page)}`);
    if (c.asks?.length) out.push("They want:", ...c.asks.map(x => `- ${x.text}${pg(x.page)}`));
    if (c.deadline?.text) out.push(`By: ${c.deadline.text}${pg(c.deadline.page)}`);
    if (c.amount?.text) out.push(`Amount: ${c.amount.text}${pg(c.amount.page)}`);
    for (const r of c.references || []) out.push(`${r.label}: ${r.value}${pg(r.page)}`);
    if (c.contact?.text) out.push(`Contact: ${c.contact.text}${pg(c.contact.page)}`);
    if (c.next_step?.text) out.push(`Next: ${c.next_step.text}${pg(c.next_step.page)}`);
    return out.join("\n");
  }

  function jump(page, quote) {
    const i = Math.max(0, Math.min(page - 1, S.doc.pages.length - 1));
    goTo(i);
    if (quote) {
      openFind();
      findInput.value = quote;
      runSearch(quote).then(() => {
        if (!S.hits.length) setStatus(`Page ${page} - the exact words weren't found (the AI may have paraphrased).`);
      });
    }
  }

  // ------------------------------------------------------------------ selecting and copying
  // The app turns text selection off and its window has no right-click menu, so the chat brings its own:
  // drag to select, then Ctrl+C or right-click → Copy; Ctrl+A selects just the conversation.
  // Page chips copy as "(page 3)", the same as the Copy button.
  async function copyText(text, msg = "Copied.") {
    if (!text) return;
    try { await navigator.clipboard.writeText(text); }
    catch {
      const ta = el("textarea", { style: "position:fixed;left:-9999px;opacity:0" });
      ta.value = text; document.body.append(ta); ta.select();
      const ok = document.execCommand("copy"); ta.remove();
      if (!ok) return toast("Couldn't copy - try again.", { error: true });
    }
    toast(msg);
  }
  /** The selection, if it's inside the conversation. */
  function chatRange() {
    const s = getSelection(), chat = A.el?.querySelector(".ai-chat");
    if (!chat || !s?.rangeCount || s.isCollapsed) return null;
    const r = s.getRangeAt(0);
    return chat.contains(r.commonAncestorContainer) ? r : null;
  }
  /** Selected text as plain text: chips become "(page 3)", the Copy / Think it through line is left out. */
  function rangeText(r) {
    const box = el("div", { style: "position:fixed;left:-9999px;top:0;width:600px;white-space:normal" });
    box.append(r.cloneContents());
    box.querySelectorAll(".ai-cite, .ai-math, .ai-math-block").forEach(b => b.replaceWith(b.dataset.copy || ""));
    box.querySelectorAll(".ai-meta, .ai-think-tag, .ai-typing, .ai-earlier").forEach(n => n.remove());
    document.body.append(box);
    const t = box.innerText.replace(/[ \t]+\n/g, "\n").replace(/\n{3,}/g, "\n\n").trim();
    box.remove();
    return t || r.toString().trim();
  }
  function selectChat() { const chat = A.el?.querySelector(".ai-chat"); if (chat?.childElementCount) getSelection().selectAllChildren(chat); }

  function chatMenu(e) {
    const msg = e.target.closest(".ai-msg"), r = chatRange();
    const sel = r && rangeText(r);                   // read it now: clicking the menu may clear the selection
    const items = [];
    if (sel) items.push({ label: "Copy", icon: "copy", kbd: "Ctrl C", run: () => copyText(sel) });
    if (msg?._copy) items.push({ label: msg.classList.contains("me") ? "Copy this question" : "Copy this answer", icon: "copy",
      run: () => copyText(msg._copy, msg.classList.contains("me") ? "Question copied." : "Answer copied.") });
    if (msg?._copy && msg.classList.contains("bot")) items.push({ label: "Report this answer", icon: "alert", run: () => report("answer") });
    if (A.el.querySelector(".ai-chat").childElementCount) items.push({ label: "Select all", kbd: "Ctrl A", run: selectChat });
    if (!items.length) return;
    e.preventDefault();
    showMenu(items, e.clientX, e.clientY);
  }

  /** Ctrl+C / Ctrl+A when the selection is in the conversation (called from app.js before the page tools see the key). */
  function key(e) {
    if (!A.open || !(e.ctrlKey || e.metaKey) || e.altKey) return false;
    const k = e.key.toLowerCase(), r = chatRange();
    if (k === "c" && r) { e.preventDefault(); copyText(rangeText(r)); return true; }
    if (k === "a" && (r || e.target.closest?.(".ai-chat"))) { e.preventDefault(); selectChat(); return true; }
    return false;
  }
  // copying by any other route (e.g. the keyboard's own Copy key) still gets the tidy text
  document.addEventListener("copy", e => {
    const r = chatRange();
    if (!r || !e.clipboardData) return;
    e.clipboardData.setData("text/plain", rangeText(r));
    e.preventDefault();
  });

  // the comments panel and this one share the right edge: opening one closes the other
  document.addEventListener("click", e => { if (e.target.closest("#btn-comments") && A.open) toggle(false); }, true);

  refresh();
  window.AI = { toggle, report, ask, explain, refresh, key, isReady: ready, render, renderCard, cardText, tabLeave, tabEnter, tabClosed,
    docChanged() {
      A.history = false;                            // the next document's earlier questions are shown when the panel opens
      if (A.el) { stop(); A.el.querySelector(".ai-chat").replaceChildren(); if (A.open) showHistory(); }
    } };
})();
