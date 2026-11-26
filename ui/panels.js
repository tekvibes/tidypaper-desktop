/* Comments panel (with the document's own notes box and Export notes), Open recent, extra page actions and the update check.
   Uses app.js globals: api, S, el, icon, $, $$, toast, fail, setStatus, showMenu, openDialog, openPath,
   refreshDoc, goTo, fileActions, pagesEl, viewer. */
"use strict";
(() => {
  const P = { open: false, data: null, q: "", t: 0, el: null, notesT: 0, notesFor: null };
  const KIND = {
    note: ["note", "Note"], "text box": ["text", "Text box"], highlight: ["markup", "Highlight"],
    underline: ["underline", "Underline"], "strike-through": ["strike", "Strike-through"], squiggle: ["underline", "Squiggle"],
    drawing: ["draw", "Drawing"], box: ["box", "Box"], ellipse: ["ellipse", "Ellipse"], line: ["line", "Line"],
    signature: ["sign", "Signature"], picture: ["image", "Picture"], stamp: ["image", "Stamp"], link: ["link", "Link"],
  };
  const kindOf = k => KIND[k] || ["note", k ? k[0].toUpperCase() + k.slice(1) : "Mark"];

  // ------------------------------------------------------------------ Open (folder button)
  async function openMenu(btn) {
    const r = await api.recent();
    const skip = new Set([S.doc?.source, S.doc?.saved_to, S.doc?.path].filter(Boolean));
    const rec = (r.recent || []).filter(f => !skip.has(f.path)).slice(0, 8);
    showMenu([
      { label: "Open a file…", icon: "open", kbd: "Ctrl O", run: () => openDialog() },
      ...(rec.length ? ["-", ...rec.map(f => ({ label: f.name, icon: fileIconFor(f.path), run: () => openPath(f.path) }))] : []),
    ], 0, 0, btn);
    $$("#menu button").forEach((b, k) => { const f = rec[k - 1]; if (f) b.title = f.path; });
  }

  // ------------------------------------------------------------------ page menu extras
  function pageItems(i) {
    return [
      { label: "Insert pages from a file after this…", icon: "plus", run: () => insertAfter(i) },
      { label: "Save this page as a picture", icon: "image", run: () => pagePicture(i) },
    ];
  }
  async function insertAfter(i) {
    const f = await api.pick_files("any", true);
    if (fail(f) || !f.paths?.length) return;
    setStatus("Adding pages…");
    const nothingAdded = () => { $("#status").textContent = ""; };   // (setStatus ignores "": "Adding pages…" would stay)
    const passwords = {};             // a password-protected PDF: asked here, used once, not kept
    let r = await api.insert_from_files(i, f.paths, passwords);
    while (r.needs_password) {
      const pw = await askPassword(r.name, r.wrong);
      if (pw == null) return nothingAdded();
      passwords[r.path] = pw;
      r = await api.insert_from_files(i, f.paths, passwords);
    }
    if (fail(r)) return nothingAdded();
    S.current = r.first;
    refreshDoc(r);                    // page count changed: full rebuild, lands on the first new page
    setStatus(r.status);
  }
  async function pagePicture(i) {
    const r = await api.page_picture(i);
    if (fail(r)) return;
    setStatus(r.status);
    toast(r.status, { actions: fileActions(r.saved) });
  }

  // ------------------------------------------------------------------ comments panel
  function panel() {
    if (P.el) return P.el;
    P.el = el("aside", { class: "cpanel", "aria-label": "Comments and marks", hidden: true },
      el("div", { class: "cp-head" },
        el("strong", { text: "Notes and marks" }),
        el("span", { class: "cp-count" }),
        el("button", { class: "btn btn-ghost sm cp-export", title: "Save your notes, highlights and bookmarks as a file", onclick: e => exportMenu(e.currentTarget) },
          icon("export"), "Export"),
        el("button", { class: "icon-btn sm cp-close", "aria-label": "Close comments", title: "Close", onclick: () => toggleComments(false) }, icon("x"))),
      // the document's own notes: kept on this computer with the document's memory, not in the PDF
      el("details", { class: "cp-notes", open: true },
        el("summary", {}, el("span", { text: "My notes on this document" }), el("span", { class: "cp-notes-state muted small" })),
        el("textarea", { class: "cp-notes-in", rows: 4, spellcheck: "true", "aria-label": "My notes on this document",
          placeholder: "Anything you want to remember about this document. Kept on this computer - not added to the PDF.",
          oninput: notesTyped, onkeydown: e => { if (e.key === "Escape") { e.stopPropagation(); e.target.blur(); } } })),
      el("label", { class: "cp-search" }, icon("search"),
        el("input", { type: "search", placeholder: "Filter by text or kind", "aria-label": "Filter comments",
          oninput: e => { P.q = e.target.value.trim().toLowerCase(); render(); },
          onkeydown: e => { if (e.key === "Escape") { e.stopPropagation(); if (e.target.value) { e.target.value = ""; P.q = ""; render(); } else toggleComments(false); } } })),
      el("div", { class: "cp-list", role: "list" }));
    const grip = el("div", { class: "grip side-grip", role: "separator", "aria-orientation": "vertical", tabindex: "0",
      "aria-label": "Comments panel width", title: "Drag to make this panel wider or narrower (double-click to reset)" });
    P.el.prepend(grip);
    window.sideGrip?.(grip);
    $(".stage").append(P.el);
    return P.el;
  }

  function toggleComments(force, focus = true) {
    P.open = typeof force === "boolean" ? force : !P.open;
    const p = panel();
    if (P.open) window.Translate?.close();
    p.hidden = !P.open;
    $(".stage").classList.toggle("with-panel", P.open);
    $("#btn-comments").setAttribute("aria-pressed", String(P.open));
    if (P.open) { load(); if (focus) p.querySelector("input").focus({ preventScroll: true }); }
  }

  // ---- tabs (tabs.js): one panel, shown again for the tabs it was open in (the notes were saved before leaving)
  const OPEN = new Set();
  function tabLeave(id) {
    if (P.open) { OPEN.add(id); toggleComments(false); }
    P.notesFor = null; P.data = null;
  }
  function tabEnter(id) {
    const was = OPEN.delete(id);
    if (was) toggleComments(true, false);
  }
  function tabClosed(id) { OPEN.delete(id); }

  async function load() {
    if (!S.doc) return;
    loadNotes();
    const r = await api.comments();
    if (r.error) return;
    P.data = r;
    render();
  }

  // ------------------------------------------------------------------ the document's own notes
  async function loadNotes() {
    const id = S.doc?.source || S.doc?.path;
    if (P.notesFor === id) return;
    const box = P.el.querySelector(".cp-notes-in"), state = P.el.querySelector(".cp-notes-state");
    const r = await api.doc_notes(null);
    if (r.error) return;
    P.notesFor = id;
    box.value = r.notes || "";
    box.disabled = !r.remembers;
    if (!r.remembers) box.placeholder = "Notes aren't kept for password-protected documents.";
  }
  function notesTyped() {
    const state = P.el.querySelector(".cp-notes-state");
    state.textContent = "Saving…";
    clearTimeout(P.notesT);
    P.notesT = setTimeout(saveNotes, 600);
  }
  async function saveNotes() {
    clearTimeout(P.notesT);
    if (!P.el || P.notesFor == null) return;
    const box = P.el.querySelector(".cp-notes-in"), state = P.el.querySelector(".cp-notes-state");
    if (box.disabled || state.textContent !== "Saving…") return;
    const r = await api.doc_notes(box.value);
    state.textContent = r.error ? "Not saved" : "Saved";
  }

  // ------------------------------------------------------------------ export
  function exportMenu(btn) {
    showMenu([
      { label: "As text (Markdown, .md)", icon: "file", run: () => doExport("md") },
      { label: "As data (JSON, .json)", icon: "file", run: () => doExport("json") },
    ], 0, 0, btn);
  }
  async function doExport(fmt) {
    await saveNotes();
    const r = await api.export_notes(fmt);
    if (fail(r)) return;
    setStatus(r.status);
    toast(r.status, { actions: fileActions(r.saved) });
  }

  function render() {
    if (!P.el || !P.data) return;
    const list = P.el.querySelector(".cp-list");
    const q = P.q;
    const match = it => !q || it.text.toLowerCase().includes(q) || (it.note || "").toLowerCase().includes(q) || kindOf(it.kind)[1].toLowerCase().includes(q);
    const groups = P.data.pages.map(g => ({ page: g.page, items: g.items.filter(match) })).filter(g => g.items.length);
    const shown = groups.reduce((n, g) => n + g.items.length, 0);
    P.el.querySelector(".cp-count").textContent = P.data.count ? (q ? `${shown} of ${P.data.count}` : String(P.data.count)) : "";
    if (!P.data.count) {
      list.replaceChildren(el("p", { class: "cp-empty", text: "No comments or marks yet. Notes, highlights, drawings, signatures and links you add will be listed here." }));
      return;
    }
    if (!shown) { list.replaceChildren(el("p", { class: "cp-empty", text: `Nothing matches “${P.q}”.` })); return; }
    list.replaceChildren(...groups.map(g => el("section", { class: "cp-group" },
      el("h4", { class: "cp-page", text: `Page ${g.page + 1}` }),
      ...g.items.map(it => {
        const [ic, label] = kindOf(it.kind);
        return el("div", { class: "cp-row", role: "listitem" },
          el("button", { class: "cp-item", title: "Show on the page", onclick: e => focusItem(g.page, it, e.currentTarget) },
            el("span", { class: `cp-icon k-${it.kind.replace(/\W+/g, "-")}` }, icon(ic)),
            el("span", { class: "cp-body" },
              el("span", { class: "cp-kind", text: label + (it.author ? ` · ${it.author}` : "") }),
              it.text ? el("span", { class: "cp-text" + (/highlight|underline|strike|squiggle/.test(it.kind) ? " quoted" : ""), text: it.text }) : null,
              it.note ? el("span", { class: "cp-note" }, icon("note"), it.note) : null)),
          el("button", { class: "icon-btn sm cp-del", "aria-label": `Delete ${label.toLowerCase()} on page ${g.page + 1}`, title: "Delete",
            onclick: () => remove(g.page, it) }, icon("trash")));
      }))));
  }

  function focusItem(i, it, btn) {
    $$(".cp-item.active").forEach(b => b.classList.remove("active"));
    btn?.classList.add("active");
    const div = pagesEl.children[i], p = S.doc.pages[i];
    if (!div) return;
    goTo(i);
    const y = div.offsetTop + it.rect[1] * S.zoom - viewer.clientHeight / 3;
    viewer.scrollTo({ top: Math.max(0, y), behavior: "smooth" });
    div.querySelectorAll(".cp-flash").forEach(n => n.remove());
    const [x0, y0, x1, y1] = it.rect;
    const f = el("div", { class: "cp-flash",
      style: `left:${x0 / p.w * 100}%;top:${y0 / p.h * 100}%;width:${(x1 - x0) / p.w * 100}%;height:${(y1 - y0) / p.h * 100}%` });
    div.append(f);
    setTimeout(() => f.remove(), 1800);
  }

  async function remove(i, it) {
    const r = await api.comment_delete(i, it.xref, it.kind);
    if (fail(r)) return;
    refreshDoc(r);
    setStatus(r.status);
  }

  function docChanged() {
    if (P.notesFor != null && P.notesFor !== (S.doc?.source || S.doc?.path)) { saveNotes(); P.notesFor = null; }
    if (!P.open) return;
    clearTimeout(P.t);
    P.t = setTimeout(load, 350);
  }

  // ------------------------------------------------------------------ updates
  // A copy that can install updates itself downloads the new version quietly (bridge_more / updater.py), then says
  // it's ready: Restart now, or it goes in when you close the app.  Otherwise: "... is available - Get it".
  let following = null;
  async function checkUpdates(force = false) {
    const r = await api.update_check(force);
    if (!r || r.error) return r;
    if (r.updated) toast(`${APP.name} was updated to ${r.updated}${r.updated_notes ? " - " + r.updated_notes.slice(0, 90) : "."}`, { ms: 7000 });
    else if (r.update_failed) toast(`The update to ${APP.name} ${r.update_failed} didn't go in - you still have ${r.current || "the old version"}.`,
      { error: true, sticky: true, actions: r.url ? [{ label: "Get it", run: () => api.open_external(r.url) }] : [] });
    if (!r.newer) return r;
    if (r.auto) follow(r);
    else offerLink(r);
    return r;
  }

  function offerLink(r) {
    if (!r.url) return;
    toast(`${APP.name} ${r.version} is available${r.notes ? " - " + r.notes.slice(0, 90) : "."}`, { sticky: true, actions: [
      { label: "Get it", run: () => api.open_external(r.url) },
      { label: "Not now", run: () => api.update_snooze(r.version) },
    ] });
  }

  // follow the download until it's ready (or failed: then the plain link instead)
  function follow(r) {
    clearTimeout(following);
    const tick = async () => {
      const st = await api.update_status();
      if (!st || st.error) return;
      if (st.state === "ready") return readyToast(st.version, r.notes);
      if (st.state === "failed") return offerLink(r);
      if (st.state === "downloading" || st.state === "checking") following = setTimeout(tick, 3000);
    };
    tick();
  }

  function readyToast(version, notes) {
    toast(`${APP.name} ${version} is ready${notes ? " - " + notes.slice(0, 80) : ""}. It goes in when you close ${APP.name}.`, { sticky: true, actions: [
      { label: "Restart now", run: restartNow },
      { label: "When I close it", run: () => {} },
    ] });
  }

  async function restartNow() {
    const r = await api.update_restart();
    if (r && r.cancelled) return;
    if (!fail(r)) toast(`Closing ${APP.name} to put the new version in - it opens again by itself.`, { busy: true, sticky: true });
  }

  window.Panels = { openMenu, pageItems, toggleComments, docChanged, checkUpdates, restartNow, saveNotes, tabLeave, tabEnter, tabClosed };
})();
