/* Several documents open: each in a tab across the top of the window, and a "+" after them for Home (recent files,
   Open, Tools).  Each tab keeps its own page, zoom, place, side panel (Ask, Translate, notes), undo and unsaved
   changes - the document itself stays open in Python (app.py: Tab, Bridge.tab_call), and a job started in one tab
   (Translate, Pull out data, Ask, making a scan's words selectable) carries on with that tab's document while
   another is on screen.
   Shortcuts: Ctrl+T Home, Ctrl+W close the tab, Ctrl+Tab / Ctrl+Shift+Tab next / previous (also ⇧⌘] / ⇧⌘[ on a
   Mac), Ctrl+1-8 that tab, Ctrl+9 the last one.
   Uses app.js globals (S, api, apiIn, el, icon, $, $$, toast, fail, setStatus, show, loadDoc, anchorPoint, closeFind,
   settleUnsaved, openPath, goHome, renderRecent, viewer, APP, keys, canOpen). */
"use strict";

const MAX_TABS = 10;

const Tabs = (() => {
  const list = [];        // {id, name, dirty, path, info (S.doc when it went to the back), view (how it looked)}
  const bar = el("nav", { class: "tabbar", id: "tabbar", "aria-label": "Open documents", hidden: true });
  const strip = el("div", { class: "tabs", role: "tablist", "aria-label": "Open documents" });
  const home = el("button", { class: "tab-home", id: "tab-home", "aria-label": "Home - open another document",
    onclick: () => goHome() }, icon("plus"));
  bar.append(strip, home);
  document.body.prepend(bar);

  const byId = id => list.find(t => t.id === id);
  const norm = p => String(p || "").replace(/\\/g, "/").toLowerCase();

  /** Is this file open in a tab (as opened, or as saved since)? */
  const openTab = path => list.find(t => [t.info?.source, t.info?.saved_to, t.path].some(q => q && norm(q) === norm(path)));

  /** Room for one more?  Says so when there isn't. */
  function room(path) {
    if (list.length < MAX_TABS || (path && openTab(path))) return true;
    toast(`Up to ${MAX_TABS} documents can be open at once - close one first (its × or ${keys("Ctrl+W")}).`, { error: true });
    return false;
  }

  /** A document just opened: its tab, on screen. */
  function add(r) {
    list.push({ id: r.tab, name: r.name, dirty: !!r.dirty, path: r.source || r.path, info: r, view: null });
    S.tab = r.tab;
    render();
  }

  /** The document on screen changed (edited, saved...): the tab's name and its unsaved dot. */
  function changed(d) {
    const t = byId(d.tab || S.tab);
    if (!t) return;
    if (t.name === d.name && t.dirty === !!d.dirty) return;
    t.name = d.name; t.dirty = !!d.dirty;
    render();
  }

  function render() {
    bar.hidden = !list.length;
    document.body.classList.toggle("has-tabs", !!list.length);
    strip.replaceChildren(...list.map((t, i) => {
      const on = t.id === S.tab && S.view === "editor";
      const tab = el("div", {
        class: "dtab" + (on ? " on" : "") + (t.dirty ? " dirty" : ""), role: "tab", tabindex: on ? "0" : "-1",
        "aria-selected": String(on), title: t.path ? `${t.name}\n${t.path}` : t.name, "data-tab": t.id,
        onclick: e => { if (!e.target.closest(".dtab-x")) switchTo(t.id); },
        onauxclick: e => { if (e.button === 1) { e.preventDefault(); closeTab(t.id); } },     // middle-click closes
        onkeydown: e => {
          if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
            e.preventDefault();
            const next = list[(i + (e.key === "ArrowRight" ? 1 : -1) + list.length) % list.length];
            strip.querySelector(`[data-tab="${next.id}"]`)?.focus();
          }
          if (e.key === "Enter" || e.key === " ") { e.preventDefault(); switchTo(t.id); }
        },
      },
        el("span", { class: "dtab-dot", "aria-hidden": "true" }),
        el("span", { class: "dtab-name", text: t.name }),
        t.dirty ? el("span", { class: "sr-only", text: " (not saved)" }) : null,
        el("button", { class: "dtab-x", tabindex: "-1", "aria-label": `Close ${t.name}`, title: `Close (${keys("Ctrl+W")})`,
          onclick: e => { e.stopPropagation(); closeTab(t.id); } }, icon("x")));
      return tab;
    }));
    home.classList.toggle("on", S.view === "home");
    home.setAttribute("aria-pressed", String(S.view === "home"));
    home.title = `Home - open another document (${keys("Ctrl+T")})`;
  }

  // ---- each tab's side panels and jobs (ai.js, translate.js, panels.js, scanwords.js keep one state per tab)
  const MODULES = () => [window.AI, window.Translate, window.Panels, window.ScanWords].filter(Boolean);
  function modulesLeave(id) { for (const m of MODULES()) m.tabLeave?.(id); }
  function modulesEnter(id) {
    for (const m of MODULES()) m.tabEnter?.(id);
    $(".stage").classList.toggle("with-panel", !!$(".stage > .aipanel:not([hidden]), .stage > .tpanel:not([hidden]), .stage > .cpanel:not([hidden])"));
  }
  function modulesClosed(id) { for (const m of MODULES()) m.tabClosed?.(id); }

  /** Open several documents, each in a tab (the last one on screen), as many as there's room for. */
  async function openAll(paths) {
    let skipped = 0;
    for (const p of paths) {
      if (list.length >= MAX_TABS && !openTab(p)) { skipped++; continue; }
      await openPath(p);
    }
    if (skipped) toast(`Up to ${MAX_TABS} documents can be open at once - ${skipped} more ${skipped === 1 ? "wasn't" : "weren't"} opened.`, { error: true });
  }

  /** Files arriving while a window is open on top (Pull out data working, a dialog...) wait until it closes, so
      what's behind it doesn't change. */
  async function noDialog() {
    while (document.querySelector("dialog[open]")) await new Promise(res => setTimeout(res, 300));
  }

  // ---- keyboard
  /** ⌘W on a Mac (Python asks - app.py _mac_close_key): true = done here; false = nothing is open, the window closes. */
  function closeKey() {
    if (document.querySelector("dialog[open]")) return true;          // not from under a window that's open
    if (!$("#menu").hidden) { closeMenu(); return true; }
    if (S.view === "editor" && S.tab) { closeTab(S.tab); return true; }
    return list.length > 0;                                          // Home with documents open: nothing closes
  }

  function key(e) {
    const ctrl = APP.mac ? e.metaKey : e.ctrlKey;
    const k = e.key.toLowerCase();
    if (ctrl && !e.shiftKey && !e.altKey && k === "t") { e.preventDefault(); goHome(); return true; }
    if (ctrl && !e.shiftKey && !e.altKey && k === "w") {
      if (S.view === "editor" && S.tab) { e.preventDefault(); closeTab(S.tab); return true; }
      if (list.length) { e.preventDefault(); return true; }      // Home with documents open: don't close the window
      return false;                                               // nothing open: the window closes, as before
    }
    let step = 0;
    if (e.ctrlKey && k === "tab") step = e.shiftKey ? -1 : 1;
    if (APP.mac && e.metaKey && e.shiftKey && (e.code === "BracketRight" || e.code === "BracketLeft")) step = e.code === "BracketRight" ? 1 : -1;
    if (step && list.length) {
      e.preventDefault();
      const at = list.findIndex(t => t.id === S.tab && S.view === "editor");
      const next = at < 0 ? (step > 0 ? list[0] : list[list.length - 1]) : list[(at + step + list.length) % list.length];
      if (next.id !== S.tab || S.view !== "editor") switchTo(next.id);
      return true;
    }
    if (ctrl && !e.shiftKey && !e.altKey && /^[1-9]$/.test(e.key) && list.length) {
      e.preventDefault();
      const t = e.key === "9" ? list[list.length - 1] : list[+e.key - 1];
      if (t) switchTo(t.id);
      return true;
    }
    return false;
  }

  return { list, byId, room, add, changed, render, openAll, noDialog, key, closeKey, modulesLeave, modulesEnter, modulesClosed,
    get count() { return list.length; } };
})();
window.Tabs = Tabs;

/** Before the document on screen goes to the back (or closes): what's typed or placed is put in, its place and the
    notes box are saved. */
async function flushDoc() {
  if (!S.doc) return;
  await window.Edit?.commitPending();
  await window.Reading?.flush();
  await window.Panels?.saveNotes?.();
}

/** The tab on screen goes to the back: keep how it looks (page, zoom, where it was scrolled, signatures checked) and
    its side panels; nothing is closed. */
function stashTab() {
  const t = Tabs.byId(S.tab);
  if (!t || !S.doc) return;
  t.info = S.doc;
  t.view = { anchor: anchorPoint(), current: S.current, zoom: S.zoom, fit: S.fit, turn: S.turn || 0, sigs: S.sigs,
             warnedSigned: S.warnedSigned };
  closeFind();
  window.Edit?.reset();
  Tabs.modulesLeave(t.id);
}

/** Bring a tab to the front with `info` (its document_info from Python) as it was left. */
function enterTab(id, info) {
  const t = Tabs.byId(id);
  if (!t) return;
  const v = t.view || {};
  S.tab = id;
  S.fit = v.fit === undefined ? "auto" : v.fit;
  S.zoom = v.zoom || 1; S.current = v.current || 0; S.turn = v.turn || 0;
  S.sigs = v.sigs ?? null; S.warnedSigned = !!v.warnedSigned;
  window.Edit?.reset();
  window.Reading?.entered(info);
  Tabs.modulesEnter(id);
  show("editor");
  loadDoc(info, false, v.anchor || null);
  setStatus(`${info.name} · ${info.pages.length} page${info.pages.length === 1 ? "" : "s"}`);
  if (info.signatures && !S.sigs) checkSignatures();
  Tabs.render();
}

let switching = false;
/** Show another open document. */
async function switchTo(id) {
  if (switching || (id === S.tab && S.view === "editor")) return;
  switching = true;
  try {
    await flushDoc();
    const r = await api.tab_switch(id);
    if (r.closed) { Tabs.list.splice(Tabs.list.findIndex(t => t.id === id), 1); Tabs.modulesClosed(id); Tabs.render(); return; }
    if (fail(r)) return;
    stashTab();
    enterTab(id, r);
  } finally {
    switching = false;
  }
}

/** Close a tab.  Unsaved changes: asked about with the document on screen ("Save your changes?"), as before. */
async function closeTab(id) {
  const t = Tabs.byId(id);
  if (!t) return;
  if (id !== S.tab || S.view !== "editor") {
    if (!t.dirty) {                                // in the back and nothing to ask: just close it
      await apiIn(id).close_document();
      Tabs.list.splice(Tabs.list.indexOf(t), 1);
      Tabs.modulesClosed(id);
      Tabs.render();
      return;
    }
    await switchTo(id);
    if (S.tab !== id) return;
  }
  if (!(await settleUnsaved("close it"))) return;
  await flushDoc();
  const at = Tabs.list.indexOf(t);
  stashTab();
  await api.close_document();
  Tabs.list.splice(at, 1);
  Tabs.modulesClosed(id);
  S.tab = null; S.doc = null;
  const next = Tabs.list[at] || Tabs.list[at - 1];
  if (next) {
    const r = await api.tab_switch(next.id);
    if (!fail(r)) { enterTab(next.id, r); return; }
  }
  show("home");
  renderRecent();
  Tabs.render();
}
