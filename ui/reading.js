/* Reading comfort: each document reopens where you left it (page and zoom), Home's "Carry on reading" card,
   Pages | Contents in the page panel, your own bookmarks, focus mode and the document's details.
   What's kept lives in Python (bridge_read.py: one small file per document, recognised by its contents).
   Uses app.js globals: api, S, el, icon, $, $$, toast, fail, setStatus, openPath, strip, viewer, pagesEl, clamp, pref. */
"use strict";
(() => {
  const R = {
    remembers: false,     // false for password-protected documents: nothing is kept for them
    marks: new Set(),     // bookmarked pages (0-based)
    onlyMarked: false,
    ready: false,         // the scroll that restores the page isn't the reader moving
    saveT: 0,
    tab: pref.get("ppTab", "pages") === "contents" ? "contents" : "pages",
    outline: null,
    focus: false,
  };

  // ------------------------------------------------------------------ where you were
  function opened(r) {
    const m = r.memory || {};
    R.remembers = !!r.remembers;
    R.ready = false;
    R.outline = null;
    R.marks = new Set(m.bookmarks || []);
    R.onlyMarked = false;
    if (Number.isInteger(m.page) && m.page > 0 && m.page < r.pages.length) S.current = m.page;
    if ("fit" in m) {
      if (m.fit) S.fit = m.fit;
      else if (typeof m.zoom === "number") { S.fit = null; S.zoom = clamp(m.zoom, 0.2, 6); }
    }
    clearTimeout(R.saveT); R.saveT = 0;
    setTimeout(() => { R.ready = true; }, 700);
    if (S.current > 0 && !r.converted) {
      setTimeout(() => toast(`Back where you left off: page ${S.current + 1}.`,
        { actions: [{ label: "Go to page 1", run: () => goToSpot(0, null) }] }), 0);
    }
  }
  /** A tab came back to the front: its bookmarks and whether it's remembered (its place is the tab's own). */
  function entered(r) {
    const m = r.memory || {};
    R.remembers = !!r.remembers;
    R.ready = false;
    R.outline = null;
    R.marks = new Set(m.bookmarks || []);
    R.onlyMarked = false;
    clearTimeout(R.saveT); R.saveT = 0;
    setTimeout(() => { R.ready = true; }, 700);
  }
  const place = () => ({ page: S.current, zoom: Math.round(S.zoom * 1000) / 1000, fit: S.fit || null });
  function placeChanged() {
    if (R.tab === "contents" && R.outline) markHere();
    if (!R.remembers || !R.ready || !S.doc) return;
    clearTimeout(R.saveT);
    R.saveT = setTimeout(save, 800);
  }
  async function save() {
    clearTimeout(R.saveT); R.saveT = 0;
    if (R.remembers && S.doc) await api.doc_remember(place());
  }
  /** Before the document closes or another opens: keep the place if it moved since the last save. */
  async function flush() { if (R.saveT) await save(); }

  // ------------------------------------------------------------------ Home: Carry on reading
  function showCarryOn(c) {
    const box = $("#carry-on");
    box.hidden = !c;
    if (!c) return;
    $("#carry-name").textContent = c.name;
    $("#carry-page").textContent = `Page ${c.page + 1}${c.pages ? " of " + c.pages : ""}`;
    $("#carry-open").title = c.path;
    $("#carry-open").onclick = () => openPath(c.path);
  }
  $("#carry-forget").addEventListener("click", async () => { await api.forget_carry_on(); showCarryOn(null); });

  // ------------------------------------------------------------------ bookmarks
  function decorateThumb(t, i) {
    t.classList.toggle("marked", R.marks.has(i));
    t.append(el("span", {
      class: "thumb-mark", role: "button", "aria-label": `Bookmark page ${i + 1}`, title: "Bookmark this page (B)",
      onclick: e => { e.stopPropagation(); e.preventDefault(); toggleMark(i); },
      onpointerdown: e => e.stopPropagation(),
    }, icon("bookmark")));
  }
  function stripBuilt() {
    updateMarks();
    if (R.tab === "contents") loadOutline(); else R.outline = null;
  }
  function updateMarks() {
    for (const t of strip.children) {
      const i = +t.dataset.i, on = R.marks.has(i);
      t.classList.toggle("marked", on);
      t.querySelector(".thumb-mark")?.setAttribute("aria-label", `${on ? "Remove the bookmark on" : "Bookmark"} page ${i + 1}`);
    }
    if (!R.marks.size) R.onlyMarked = false;
    const f = $("#pp-marked");
    f.hidden = !R.marks.size;
    f.setAttribute("aria-pressed", String(R.onlyMarked));
    f.querySelector("b").textContent = String(R.marks.size);
    f.setAttribute("aria-label", `Show only the ${R.marks.size} bookmarked page${R.marks.size === 1 ? "" : "s"}`);
    strip.classList.toggle("only-marked", R.onlyMarked);
    const n = S.doc?.pages.length || 0;
    $("#pp-count").textContent = `${n} page${n === 1 ? "" : "s"}`;
    $("#pp-count").hidden = !!R.marks.size;            // room for the filter (the count is under the name too)
  }
  async function toggleMark(i) {
    if (!S.doc) return;
    const on = !R.marks.has(i);
    if (on) R.marks.add(i); else R.marks.delete(i);
    updateMarks();
    setStatus(on ? `Page ${i + 1} bookmarked.` : `Bookmark on page ${i + 1} removed.`);
    if (R.remembers) await api.doc_remember({ bookmarks: [...R.marks] });
    else if (on && !toggleMark.told) {
      toggleMark.told = true;
      toast("This document has a password, so its bookmarks aren't kept after you close it.");
    }
  }
  $("#pp-marked").addEventListener("click", () => {
    R.onlyMarked = !R.onlyMarked;
    updateMarks();
    if (R.onlyMarked) strip.querySelector(".thumb.marked")?.scrollIntoView({ block: "nearest" });
  });
  const pageItems = i => [{ label: R.marks.has(i) ? "Remove bookmark" : "Bookmark this page", icon: "bookmark", kbd: "B",
    run: () => toggleMark(i) }, "-"];
  /** Pages were deleted, copied or moved: Python moved the bookmarks with them. */
  async function pagesChanged() {
    if (!R.remembers) return;
    const r = await api.doc_memory();
    if (!r.error) R.marks = new Set(r.memory?.bookmarks || []);
  }

  // ------------------------------------------------------------------ Pages | Contents
  function setTab(t) {
    R.tab = t;
    pref.set("ppTab", t);
    for (const [id, name] of [["#pp-tab-pages", "pages"], ["#pp-tab-contents", "contents"]]) {
      $(id).setAttribute("aria-selected", String(t === name));
      $(id).setAttribute("aria-pressed", String(t === name));
    }
    strip.hidden = t !== "pages";
    $("#pp-sub").hidden = t !== "pages";
    $("#outline").hidden = t !== "contents";
    if (t === "contents" && S.doc && !R.outline) loadOutline();
    if (t === "pages") strip.children[S.current]?.scrollIntoView({ block: "center" });
  }
  $("#pp-tab-pages").addEventListener("click", () => setTab("pages"));
  $("#pp-tab-contents").addEventListener("click", () => setTab("contents"));

  async function loadOutline() {
    const box = $("#outline");
    const r = await api.outline();
    if (fail(r)) return;
    R.outline = r.outline;
    if (!r.outline.length) {
      box.replaceChildren(el("p", { class: "ol-empty", text: "This PDF has no contents list." }),
        el("p", { class: "ol-empty", text: "Tip: bookmark the pages you want to come back to - the ribbon on each page in Pages, or press B." }));
      return;
    }
    // the flat list (each with a level) as a tree
    const root = { kids: [] }, stack = [{ level: 0, node: root }];
    r.outline.forEach((e, k) => {
      while (stack.length > 1 && stack[stack.length - 1].level >= e.level) stack.pop();
      const node = { ...e, k, kids: [] };
      stack[stack.length - 1].node.kids.push(node);
      stack.push({ level: e.level, node });
    });
    const openAll = r.outline.length <= 60;
    const build = (n, depth) => {
      const row = el("div", { class: "ol-row" + (n.kids.length && !(openAll || depth === 0) ? " closed" : ""), "data-level": depth + 1,
        style: `padding-left:${depth * 14}px`, role: "treeitem", "aria-expanded": n.kids.length ? String(openAll || depth === 0) : null },
        n.kids.length ? el("button", { class: "ol-twist", "aria-label": `Show or hide what's under ${n.title}`,
          onclick: () => {
            const closed = row.classList.toggle("closed");
            row.setAttribute("aria-expanded", String(!closed));
            kids.hidden = closed;
          } }, icon("fwd")) : el("span", { class: "ol-pad" }),
        el("button", { class: "ol-go", "data-k": n.k, disabled: n.page == null || null,
          title: n.page == null ? "This entry doesn't point to a page in this document" : `Go to page ${n.page + 1}`,
          onclick: () => goToSpot(n.page, n.y) },
          el("span", { class: "ol-title", text: n.title }),
          n.page != null ? el("span", { class: "ol-page", text: String(n.page + 1) }) : null));
      const kids = el("div", { role: "group", hidden: n.kids.length && !(openAll || depth === 0) ? true : null },
        ...n.kids.map(c => build(c, depth + 1)));
      return el("div", { class: "ol-node" }, row, n.kids.length ? kids : null);
    };
    box.replaceChildren(...root.kids.map(n => build(n, 0)));
    markHere();
  }
  /** The entry for the page you're on (the last one starting at or before it). */
  function markHere() {
    const o = R.outline || [];
    let here = -1;
    o.forEach((e, k) => { if (e.page != null && e.page <= S.current) here = k; });
    $$("#outline .ol-go.here").forEach(b => b.classList.remove("here"));
    if (here >= 0) $(`#outline .ol-go[data-k="${here}"]`)?.classList.add("here");
  }
  function goToSpot(page, y) {
    const pg = pagesEl.children[page];
    if (!pg) return;
    const top = pg.offsetTop + (y != null ? Math.max(0, y * S.zoom - 24) : -20);
    viewer.scrollTo({ top, behavior: "auto" });
    viewer.focus({ preventScroll: true });
  }

  // ------------------------------------------------------------------ focus mode
  const exitBtn = el("button", { class: "focus-exit", hidden: true, onclick: () => focus(false) }, icon("x"), "Leave focus mode (Esc)");
  document.body.append(exitBtn);
  // (WKWebView reports outerWidth / outerHeight as 0; the page fills the whole screen only in full screen)
  const isFull = () => innerWidth >= screen.width - 2 && innerHeight >= screen.height - 2;
  function focus(on) {
    if (on && S.view !== "editor") return;
    if (on === R.focus) return;
    R.focus = on;
    window.Edit?.setTool?.("select");
    document.body.classList.toggle("focus", on);
    api.fullscreen(on, isFull());
    exitBtn.hidden = !on;
    if (on) {
      exitBtn.classList.add("show");
      clearTimeout(focus.t);
      focus.t = setTimeout(() => exitBtn.classList.remove("show"), 2500);
      viewer.focus({ preventScroll: true });
    }
  }
  document.addEventListener("mousemove", e => {
    if (!R.focus) return;
    exitBtn.classList.toggle("show", e.clientY < 70 && e.clientX > innerWidth - 320);
  });

  // ------------------------------------------------------------------ document details
  async function details() {
    const r = await api.details();
    if (fail(r)) return;
    $("#details-sub").textContent = S.doc?.name || "";
    $("#details-list").replaceChildren(...r.rows.flatMap(x => [el("dt", { text: x.label }), el("dd", { text: x.value })]));
    $("#details-copy").onclick = async () => {
      const text = r.rows.map(x => `${x.label}: ${x.value}`).join("\n");
      try { await navigator.clipboard.writeText(text); } catch {
        const ta = el("textarea", { style: "position:fixed;opacity:0" }); ta.value = text; document.body.append(ta);
        ta.select(); document.execCommand("copy"); ta.remove();
      }
      setStatus("Details copied.");
    };
    $("#details-dialog").showModal();
    $("#details-dialog .btn-primary").focus();
  }

  // ------------------------------------------------------------------ keys (editor, not typing, no Ctrl/⌘)
  function key(e) {
    if (e.ctrlKey || e.metaKey || e.altKey) return false;
    const k = e.key.toLowerCase();
    if (k === "f") { e.preventDefault(); focus(!R.focus); return true; }
    if (k === "escape" && R.focus) { e.preventDefault(); focus(false); return true; }
    if (k === "b") {
      e.preventDefault();
      const t = e.target.closest?.(".thumb");
      toggleMark(t ? +t.dataset.i : S.current);
      return true;
    }
    return false;
  }

  setTab(R.tab);
  window.Reading = { opened, entered, placeChanged, flush, showCarryOn, decorateThumb, stripBuilt, pageItems, pagesChanged,
    focus, details, key, get focused() { return R.focus; } };
})();
