/* Translate: the document's pages in another language, in a side panel beside the document, page by page
   (bridge_translate.py).  This page / Every page; each page's translation streams in under its own heading with a page
   link; reading on scrolls the panel to the page shown.  Copy all, Save PDF (translate_pdf.py), Save text.
   Uses app.js globals (api, S, el, icon, $, $$, toast, fail, setStatus, goTo, APP). */
"use strict";
(() => {
  // each open document (tab) has its own panel and translation; T is the one on screen (tabs.js swaps them)
  const fresh = () => ({ open: false, el: null, opts: null, job: null, poll: 0, pages: new Map(), lang: "", follow: true, bad: null });
  let T = fresh();
  const BACK = new Map();             // tab id -> its T while another tab is on screen
  const LS = "tp.translate.lang";
  const plural = (n, one, many = one + "s") => `${n} ${n === 1 ? one : many}`;
  const money = g => g < 0.01 ? "under 1p" : g < 1 ? Math.round(g * 100) + "p" : "£" + g.toFixed(2);
  const remembered = () => { try { return localStorage.getItem(LS) || ""; } catch { return ""; } };

  function build() {
    T.el = el("aside", { class: "tpanel", "aria-label": "Translate this document", hidden: true },
      el("div", { class: "ai-head" },
        el("span", { class: "ai-mark", "aria-hidden": "true" }, icon("translate")),
        el("strong", { text: "Translate" }),
        el("button", { class: "icon-btn sm", "aria-label": "Close", title: "Close", onclick: () => toggle(false) }, icon("x"))),
      el("div", { class: "tr-top" },
        el("label", { class: "tr-lang" }, el("span", { class: "small", text: "Into" }),
          el("input", { class: "st-in tr-in", list: "tr-langs", spellcheck: "false", autocomplete: "off", "aria-label": "Translate into",
            placeholder: "Choose a language", onchange: langChanged,
            onkeydown: e => { if (e.key === "Escape") { e.stopPropagation(); toggle(false); } if (e.key === "Enter") { e.preventDefault(); langChanged(); } } }),
          el("datalist", { id: "tr-langs" })),
        el("p", { class: "tr-mean small", role: "alert", hidden: true }),
        el("div", { class: "tr-go" },
          el("button", { class: "btn btn-primary sm tr-page", onclick: () => start("page") }, "This page"),
          el("button", { class: "btn btn-ghost sm tr-all", onclick: () => start("all") }, "Every page"),
          el("button", { class: "btn btn-ghost sm tr-stop", hidden: true, onclick: stop }, "Stop")),
        el("p", { class: "tr-est muted small", role: "status" })),
      el("div", { class: "tr-body", "aria-live": "polite" }),
      el("div", { class: "tr-actions" },
        el("button", { class: "btn btn-ghost sm tr-copy", onclick: copyAll }, icon("copy"), "Copy all"),
        el("button", { class: "btn btn-ghost sm tr-pdf", onclick: savePdf, title: "Save the translation as a new A4 PDF, page by page" }, "Save PDF…"),
        el("button", { class: "btn btn-ghost sm tr-save", onclick: save, title: "Save the translation as a text file" }, "Save text…")),
      el("p", { class: "ai-foot muted", text: "AI can get things wrong - for anything important, check with someone who reads the language." }));
    const grip = el("div", { class: "grip side-grip", role: "separator", "aria-orientation": "vertical", tabindex: "0",
      "aria-label": "Translate panel width", title: "Drag to make this panel wider or narrower (double-click to reset)" });
    T.el.prepend(grip);
    window.sideGrip?.(grip);
    $(".stage").append(T.el);
  }

  async function toggle(force) {
    if (!S.doc) return;
    if (!T.el) build();
    T.open = force ?? !T.open;
    if (T.open) {
      if ($(".aipanel:not([hidden])")) window.AI?.toggle(false);
      if ($(".cpanel:not([hidden])")) window.Panels?.toggleComments(false);
    }
    T.el.hidden = !T.open;
    $(".stage").classList.toggle("with-panel", T.open || !!$(".cpanel:not([hidden]), .aipanel:not([hidden])"));
    if (T.open) await load();
  }
  const close = () => { if (T.open) toggle(false); };

  async function load() {
    const t = T;
    const o = await api.translate_options();
    if (fail(o) || t !== T) return;
    T.opts = o;
    T.el.querySelector("#tr-langs").replaceChildren(...[...o.languages, ...(o.others || [])].map(n => el("option", { value: n })));
    const inp = T.el.querySelector(".tr-in");
    if (!inp.value) inp.value = remembered() || "English";
    await langChanged();
    if (t === T) inp.focus({ preventScroll: true });
  }

  // the Into box: only a real language goes on ("Frnch" -> "Did you mean French?", with a button for each guess)
  async function langChanged() {
    const inp = T.el.querySelector(".tr-in");
    const lang = inp.value.trim();
    const mean = T.el.querySelector(".tr-mean");
    const t = T;
    const which = lang ? await api.translate_language(lang) : { suggest: [], message: "Choose a language to translate into." };
    if (t !== T) return;                          // another tab came to the front meanwhile
    T.bad = which.language ? null : which;
    mean.hidden = !T.bad;
    if (T.bad) {
      // "Did you mean [French]?" - the guesses are the buttons; no guess: the sentence alone
      const pick = n => el("button", { type: "button", class: "chip-btn", onclick: () => { inp.value = n; langChanged(); } }, n);
      const g = T.bad.suggest;
      mean.replaceChildren(...(!g.length ? [T.bad.message] : ["Did you mean ", ...g.flatMap((n, k) =>
        [k ? (k === g.length - 1 ? " or " : ", ") : "", pick(n)]), "?"]));
      return update();
    }
    const r = await api.translate_cached(which.language);
    if (t !== T) return;
    if (r.error) return update();
    inp.value = T.lang = r.language;
    try { localStorage.setItem(LS, T.lang); } catch { /* not kept */ }
    if (!T.job) {
      T.pages = new Map(Object.entries(r.pages).map(([k, v]) => [+k, { text: v, done: true }]));
      render();
    }
    update();
  }

  // the estimate line, and which buttons work
  function update() {
    const o = T.opts, est = T.el.querySelector(".tr-est");
    const busy = !!T.job;
    T.el.querySelector(".tr-stop").hidden = !busy;
    T.el.querySelector(".tr-page").hidden = T.el.querySelector(".tr-all").hidden = busy;
    const any = [...T.pages.values()].some(p => p.text);
    for (const c of [".tr-copy", ".tr-save", ".tr-pdf"]) T.el.querySelector(c).disabled = !any;
    if (busy) return;
    if (!o?.ai) {
      est.textContent = o?.ai_note || "";
      T.el.querySelector(".tr-page").disabled = T.el.querySelector(".tr-all").disabled = true;
      return;
    }
    const lang = T.el.querySelector(".tr-in").value.trim();
    T.el.querySelector(".tr-page").disabled = T.el.querySelector(".tr-all").disabled = !lang || !!T.bad;
    if (T.bad) { est.textContent = ""; return; }
    const todo = o.chars.map((_, i) => i).filter(i => !T.pages.get(i)?.text && (o.chars[i] || o.pictures.includes(i)));
    const bits = [];
    if (T.pages.get(S.current)?.text) bits.push(`Page ${S.current + 1} is translated below`);
    else if (!o.chars[S.current] && !o.pictures.includes(S.current)) bits.push(`Page ${S.current + 1} has no words to translate`);
    else bits.push(`This page sends page ${S.current + 1}${o.pictures.includes(S.current) && !o.see ? " (read on this " + (APP.mac ? "Mac" : "PC") + " first)" : ""} to ${o.provider}`);
    if (todo.length && !(todo.length === 1 && todo[0] === S.current)) {
      const g = o.page_gbp ? todo.reduce((n, i) => n + (o.page_gbp[i] || 0), 0) : null;
      bits.push(`every page: ${plural(todo.length, "page")}${T.pages.size ? " still to do" : ""}, a few at a time${g != null ? `, about ${money(g)} of your allowance` : ""}`);
    }
    if (o.month?.limit != null) bits.push(`£${o.month.spent.toFixed(2)} of £${o.month.limit.toFixed(2)} used this month`);
    est.textContent = bits.join(" · ") + ".";
  }

  // ------------------------------------------------------------------ translating
  // which: "page", "all", or a page number to translate again (its translation is replaced)
  async function start(which) {
    if (T.el.querySelector(".tr-in").value.trim() !== T.lang) await langChanged();
    const lang = T.el.querySelector(".tr-in").value.trim();
    if (!lang || T.job || T.bad) return;
    const n = S.doc.pages.length, again = typeof which === "number";
    let pages = again ? [which] : which === "page" ? [S.current] : [...Array(n).keys()].filter(i => !T.pages.get(i)?.text);
    if (!pages.length) pages = [S.current];
    if (which === "all" && pages.length > 20) {
      const o = T.opts, g = o.page_gbp ? pages.reduce((s, i) => s + (o.page_gbp[i] || 0), 0) : null;
      const ok = await confirmBig(pages.length, g);
      if (!ok) return;
    }
    const t = T;
    const r = await api.translate_start(pages, T.el.querySelector(".tr-in").value.trim(), again);
    if (fail(r)) return;
    t.job = r.job;
    t.follow = true;
    if (t !== T) return poll(t);
    render();
    update();
    setStatus(`Translating into ${r.language} with ${r.provider}…`);
    poll(t);
  }

  function confirmBig(n, g) {
    return new Promise(res => {
      const d = el("dialog", { class: "dialog" });
      d.innerHTML = `<form method="dialog"><h3>Translate ${n} pages?</h3>
        <p class="muted">Each page goes to <span class="tr-big-who"></span> on its own${g != null ? `, about ${money(g)} of your allowance in all` : ""}.
        It takes a while - you can read on, and stop at any time.</p>
        <div class="dlg-actions"><span class="spacer"></span><button class="btn btn-ghost" value="cancel">Cancel</button>
        <button class="btn btn-primary" value="ok">Translate</button></div></form>`;
      d.querySelector(".tr-big-who").textContent = T.opts.provider;       // a name from settings: never as HTML
      document.body.append(d);
      d.addEventListener("close", () => { res(d.returnValue === "ok"); setTimeout(() => d.remove(), 0); }, { once: true });
      d.showModal();
    });
  }

  // a translation carries on while its tab is in the back: the pages are kept in its T, shown when it's back in front
  async function poll(t = T) {
    clearTimeout(t.poll);
    if (!t.job) return;
    const st = await api.translate_status(t.job);
    if (st.error) { t.job = null; if (t === T) update(); return; }
    for (const [k, v] of Object.entries(st.pages || {})) t.pages.set(+k, v);
    if (t === T) render(st.current);
    if (st.progress && t === T) setStatus(st.progress);
    if (!st.done) { t.poll = setTimeout(() => poll(t), 250); return; }
    t.job = null;
    if (t !== T) return;
    render();                       // the Translate again buttons work again
    update();
    if (st.cancelled) setStatus("Stopped.");
    else if (st.problem) { setStatus(""); toast(st.problem, { error: true }); }
    else setStatus(`Translated into ${st.language} - check anything important with someone who reads it.`);
  }

  async function stop() {
    if (T.job) await api.translate_cancel(T.job);
  }

  // ------------------------------------------------------------------ the translation, page by page
  function render(current) {
    const body = T.el.querySelector(".tr-body");
    const done = [...T.pages.entries()].sort((a, b) => a[0] - b[0]);
    if (!done.length) {
      body.replaceChildren(el("div", { class: "tr-empty" },
        el("p", {}, el("strong", { text: "Read this document in your language." })),
        el("p", { class: "muted small", text: "Choose a language, then translate the page you're on, or every page. The translation shows here, page by page, next to the original." })));
      return;
    }
    const keep = new Map([...body.querySelectorAll(".tr-pg")].map(n => [+n.dataset.page, n]));
    const nodes = done.map(([i, p]) => {
      let sec = keep.get(i);
      if (!sec) {
        sec = el("section", { class: "tr-pg", "data-page": i },
          el("div", { class: "tr-pg-head" },
            el("button", { type: "button", class: "ai-cite", title: "Show this page", onclick: () => { T.follow = false; goTo(i); } }, icon("file"), `Page ${i + 1}`),
            el("span", { class: "spacer" }),
            el("button", { type: "button", class: "link-btn small tr-report", text: "Report", title: "Report this translation",
              onclick: () => window.AI?.report("translation") }),
            el("button", { type: "button", class: "icon-btn sm tr-pg-again", "aria-label": `Translate page ${i + 1} again`,
              title: "Translate this page again (replaces this translation)", onclick: () => start(i) }, icon("rotate-r")),
            el("button", { type: "button", class: "icon-btn sm tr-pg-copy", "aria-label": `Copy page ${i + 1}`, title: "Copy this page",
              onclick: () => copy(plain(T.pages.get(i)?.text || ""), `Copied page ${i + 1}.`) }, icon("copy"))),
          el("div", { class: "tr-text" }));
      }
      const t = sec.querySelector(".tr-text");
      const text = p.empty ? "" : p.text;
      if (t.dataset.raw !== text) { t.dataset.raw = text; t.replaceChildren(...shown(text)); }
      sec.classList.toggle("working", !p.done);
      sec.querySelector(".tr-pg-again").disabled = !!T.job || !p.done;
      sec.classList.toggle("blank", !!p.empty);
      if (p.empty) t.replaceChildren(el("span", { class: "muted small", text: "No words on this page to translate." }));
      else if (!p.done && !text) t.replaceChildren(el("span", { class: "ai-typing", text: "Translating…" }));
      sec.classList.toggle("current", i === S.current);
      return sec;
    });
    body.replaceChildren(...nodes);
    if (current != null && T.follow) reveal(current, false);
    update();
  }

  function reveal(i, smooth = true) {
    const sec = T.el?.querySelector(`.tr-pg[data-page="${i}"]`);
    if (!sec) return;
    const body = T.el.querySelector(".tr-body");
    body.scrollTo({ top: sec.offsetTop - body.offsetTop - 6, behavior: smooth ? "smooth" : "auto" });
  }

  // ------------------------------------------------------------------ the AI's marks (translate_pdf.py's rules)
  // "# " / "## " headings, "- " bullet points, "a | b" table rows; Markdown's bold taken off
  const HEADING = /^(#{1,6})(?:\s+(.*))?$/, BULLET = /^[-*•–]\s+(.*)$/;
  const tidy = raw => raw.replaceAll("**", "").replaceAll("__", "").split(/\s+/).filter(Boolean).join(" ");
  function cells(line) {
    if (!line.includes(" | ") && !(line.startsWith("|") && line.endsWith("|") && line.split("|").length > 3)) return null;
    const c = line.replace(/^\|+|\|+$/g, "").split("|").map(x => x.trim());
    return c.length > 1 ? c : null;
  }
  function blocks(text) {
    const out = [];
    let cur = null;
    for (const raw of (text || "").split(/\r\n|\r|\n/)) {
      const line = tidy(raw);
      if (!line) { cur = null; continue; }
      const h = line.match(HEADING), b = h ? null : line.match(BULLET), c = h || b ? null : cells(line);
      if (h) { if (h[2]) out.push({ kind: "heading", level: h[1].length === 1 ? 1 : 2, text: h[2] }); cur = null; }
      else if (b) { if (cur?.kind !== "list") out.push(cur = { kind: "list", items: [] }); cur.items.push(b[1]); }
      else if (c) {
        if (c.some(Boolean) && c.filter(Boolean).every(x => /^:?-{2,}:?$/.test(x))) continue;
        if (cur?.kind !== "table") out.push(cur = { kind: "table", rows: [] });
        cur.rows.push(c);
      } else { if (cur?.kind !== "lines") out.push(cur = { kind: "lines", lines: [] }); cur.lines.push(line); }
    }
    // a real table has 2 rows or more, most (2 in 3) with the same number of cells; else (a letterhead's "a | b | c"
    // small print) its lines stay text
    for (let k = 0; k < out.length; k++) {
      const b = out[k];
      if (b.kind !== "table") continue;
      const counts = b.rows.map(r => r.length), common = Math.max(...counts.map(c => counts.filter(x => x === c).length));
      if (counts.length < 2 || common * 3 < counts.length * 2) out[k] = { kind: "lines", lines: b.rows.map(r => r.join(" | ")) };
    }
    // a long line (a paragraph on one line) is a paragraph of its own; short lines together stay together
    for (let k = out.length - 1; k >= 0; k--) {
      if (out[k].kind !== "lines") continue;
      const parts = [];
      let group = [];
      for (const l of out[k].lines) {
        if ([...l].length >= 80) { if (group.length) parts.push(group); group = []; parts.push([l]); } else group.push(l);
      }
      if (group.length) parts.push(group);
      out.splice(k, 1, ...parts.map(g => ({ kind: "lines", lines: g })));
    }
    const last = out[out.length - 1];      // the page's own page number at the end
    if (last?.kind === "lines" && last.lines.length === 1 && /^\d{1,4}$/.test(last.lines[0])) out.pop();
    return out;
  }
  // a page's translation as the panel shows it (each block finds its own direction: dir="auto")
  const shown = text => blocks(text).map(b =>
    b.kind === "heading" ? el("div", { class: b.level === 1 ? "tr-h1" : "tr-h2", dir: "auto", text: b.text })
    : b.kind === "list" ? el("ul", { class: "tr-ul", dir: "auto" }, ...b.items.map(x => el("li", { text: x })))
    : b.kind === "table" ? el("table", { class: "tr-tab", dir: "auto" }, ...b.rows.map(r => el("tr", {}, ...r.map(x => el("td", { text: x })))))
    : el("div", { class: "tr-p", dir: "auto", text: b.lines.join("\n") }));
  // plain text for Copy and Save text: headings without "#", bullet points with "• "
  const plain = text => (text || "").split(/\r\n|\r|\n/).map(raw => {
    const line = tidy(raw), h = line.match(HEADING), b = h ? null : line.match(BULLET);
    return h ? (h[2] || "") : b ? "• " + b[1] : line;
  }).join("\n").trim();

  // the document scrolled to another page: show its translation (if there is one) and say what This page sends
  function pageShown(i) {
    if (!T.open) return;
    T.el.querySelectorAll(".tr-pg.current").forEach(n => n.classList.remove("current"));
    T.el.querySelector(`.tr-pg[data-page="${i}"]`)?.classList.add("current");
    if (!T.job) reveal(i);
    update();
  }

  // ------------------------------------------------------------------ tabs (tabs.js)
  function tabLeave(id) {
    T.el?.remove();
    BACK.set(id, T);
    T = fresh();
  }
  function tabEnter(id) {
    T = BACK.get(id) || fresh();
    BACK.delete(id);
    if (!T.el) return;
    $(".stage").append(T.el);
    T.el.hidden = !T.open;
    if (T.open) { render(); update(); }
  }
  function tabClosed(id) {
    const t = BACK.get(id);
    if (!t) return;
    clearTimeout(t.poll);
    if (t.job) api.translate_cancel(t.job);
    t.el?.remove();
    BACK.delete(id);
  }

  function docChanged() {
    stop();
    T.job = null;
    T.pages = new Map();
    if (T.open && S.doc) load();
    else if (T.open) toggle(false);
  }

  const allText = () => [...T.pages.entries()].filter(([, p]) => p.text && !p.empty).sort((a, b) => a[0] - b[0])
    .map(([i, p]) => `Page ${i + 1}\n\n${plain(p.text)}`).join("\n\n");
  async function copy(text, msg) {
    try { await navigator.clipboard.writeText(text); toast(msg); }
    catch { toast("Couldn't copy - try Save text instead.", { error: true }); }
  }
  const copyAll = () => copy(allText(), "Copied the translation.");
  async function save() {
    const r = await api.translate_save(allText(), T.lang || T.el.querySelector(".tr-in").value);
    if (r.cancelled || fail(r)) return;
    setStatus(r.status);
    toast(r.status, { actions: [{ label: "Show in folder", run: () => api.show_in_folder(r.saved) }] });
  }

  // a new tidy A4 PDF of the pages translated so far, then offer to open it here
  async function savePdf() {
    const pages = Object.fromEntries([...T.pages.entries()].filter(([, p]) => p.text && !p.empty && p.done).map(([i, p]) => [i, p.text]));
    if (!Object.keys(pages).length) return toast(T.job ? "Wait for a page to finish translating." : "Nothing translated yet.");
    setStatus("Making the PDF…");
    const r = await api.translate_save_pdf(pages, T.lang || T.el.querySelector(".tr-in").value);
    if (r.cancelled) return setStatus("");
    if (fail(r)) return;
    setStatus(r.status);
    toast(r.status, { actions: [{ label: "Open", run: () => openPath(r.saved) }, { label: "Show in folder", run: () => api.show_in_folder(r.saved) }] });
  }

  window.Translate = { toggle, close, pageShown, docChanged, tabLeave, tabEnter, tabClosed };
})();
