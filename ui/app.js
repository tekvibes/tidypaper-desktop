/* The app's front-end (its name comes from brand.json via startup()).  Talks to Python through window.pywebview.api (app window)
   or POST /api/<method> (developer mode: py app.py --serve, open index.html?dev). */
"use strict";

// ------------------------------------------------------------------ bridge
const DEV = new URLSearchParams(location.search).has("dev");
// the app's name and what the editor can open - filled in by startup() from brand.json
const APP = window.APP = { name: "", ai: "", openable: [".pdf"], mac: false };

/** "this PC" -> "this Mac" (and "your PC's", "the PC's") on a Mac; text is written the Windows way. */
function here(s) {
  return APP.mac && s ? String(s).replace(/\b(this|your|the) PC\b/g, "$1 Mac") : s;
}

/** Shortcut text for this computer: written the Windows way ("Ctrl+S", "Ctrl Shift S"), shown as ⌘S / ⇧⌘S on a Mac. */
function keys(s) {
  if (!APP.mac || !s) return s;
  return String(s)
    .replace(/Ctrl\+Y\b/g, "⇧⌘Z").replace(/Ctrl\+Enter/g, "⌘↵")
    .replace(/Ctrl\+Shift\+Tab/g, "⇧⌘[").replace(/Ctrl\+Tab/g, "⇧⌘]")      // a Mac's next / previous tab
    .replace(/Ctrl[+ ]Shift[+ ]/g, "⇧⌘").replace(/Ctrl[+ ]?/g, "⌘").replace(/\bAlt[+ ]/g, "⌥").replace(/^Del$/, "⌫");
}

function bridgeReady() {
  if (DEV) return Promise.resolve();
  if (window.pywebview && window.pywebview.api && window.pywebview.api.startup) return Promise.resolve();
  return new Promise(res => window.addEventListener("pywebviewready", res, { once: true }));
}

/** Every call names the open document (tab) it's about - the one on screen when it's made (tabs.js; null on Home):
    a reply that comes back after a switch is still about the right document (Python: Bridge.tab_call). */
async function callIn(tab, name, args) {
  try {
    if (DEV) {
      const r = await fetch("/api/tab_call", { method: "POST", body: JSON.stringify([tab ?? null, name, args]) });
      return await r.json();
    }
    return (await window.pywebview.api.tab_call(tab ?? null, name, args)) ?? {};
  } catch (e) {
    console.error(name, e);
    return { error: `The app stopped responding. Close ${APP.name || "it"} and open it again.` };
  }
}
const call = (name, ...args) => callIn(S.tab, name, args);
const api = new Proxy({}, { get: (_, name) => (...args) => call(name, ...args) });
/** The same calls about one particular tab, whichever is on screen (a job's polling, closing a tab in the back). */
const apiIn = tab => new Proxy({}, { get: (_, name) => (...args) => callIn(tab, name, args) });

// ------------------------------------------------------------------ helpers
const $ = sel => document.querySelector(sel);
const $$ = sel => Array.from(document.querySelectorAll(sel));
const DPR = () => window.devicePixelRatio || 1;
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
function el(tag, attrs = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") n.className = v;
    else if (k === "text") n.textContent = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (v !== false && v != null) n.setAttribute(k, v === true ? "" : v);
  }
  for (const k of kids) if (k != null) n.append(k);
  return n;
}
function icon(name) {
  const s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  s.setAttribute("aria-hidden", "true");
  const u = document.createElementNS("http://www.w3.org/2000/svg", "use");
  u.setAttribute("href", "#i-" + name);
  s.append(u);
  return s;
}
const extOf = p => { const m = /\.[^.\\/]+$/.exec(p); return m ? m[0].toLowerCase() : ""; };
const canOpen = p => APP.openable.includes(extOf(p));
// documents open straight in the editor; pictures first offer their tasks (Photos → PDF, smaller, sign...)
const opensDirectly = p => canOpen(p) && !IMAGES.includes(extOf(p));
const fileIconFor = p => extOf(p) === ".pdf" ? "file-pdf" : IMAGES.includes(extOf(p)) ? "image" : "file";

// ------------------------------------------------------------------ tasks
const IMAGES = [".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff", ".heic", ".heif"];
const OFFICE = [".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt", ".odt", ".ods", ".odp", ".rtf", ".txt", ".csv"];
const TASKS = [
  { id: "edit", title: "Edit a document", desc: "Fix a typo, add text or a highlight, rearrange pages.", icon: "edittext", tint: "blue", exts: [".pdf"], editor: true },
  { id: "sign", title: "Sign a document", desc: "Add your signature, drawn or typed, and send it back.", icon: "sign", tint: "green", exts: [".pdf"], editor: true,
    tool: "sign" },
  { id: "fill", title: "Fill in a form", desc: "Type into the boxes of a PDF form and save it.", icon: "form", tint: "violet", exts: [".pdf"], editor: true,
    note: "Click a box on the form to type in it. Tab moves to the next box." },
  { id: "combine", title: "Combine PDFs", desc: "Join several PDFs into one file, in the order you choose.", icon: "combine", tint: "amber", exts: [".pdf"], multi: true },
  { id: "convert", title: "Convert to PDF", desc: "Turn Word, Excel or PowerPoint files into PDFs.", icon: "convert", tint: "rose", exts: OFFICE },
  { id: "smaller", title: "Make smaller for email", desc: "Shrink a PDF or photos so they attach easily.", icon: "compress", tint: "teal", exts: [".pdf", ...IMAGES] },
  { id: "photos", title: "Photos → PDF", desc: "Put pictures or scans into one tidy PDF.", icon: "image", tint: "amber", exts: IMAGES },
  { id: "ocr", title: "Pull text from a scan", desc: "Copy the words out of a photo or scanned page.", icon: "scan", tint: "violet", exts: [".pdf", ...IMAGES] },
];
const TOOL_NAMES = { markup: "Mark up", draw: "Draw", text: "Text", edittext: "Edit text", redact: "Redact", sign: "Sign" };

// ------------------------------------------------------------------ state
const S = {
  view: "home", tab: null, doc: null, rev: 0, zoom: 1, fit: "auto", current: 0,
  hits: [], hitIdx: -1, query: "", findOpts: { match_case: false, whole_words: false }, inflight: 0, visible: new Set(), dragFrom: null, sigs: null,
  pv: [],               // per-page version: bumped when one page is edited, so only it re-renders
  spread: (() => { try { return localStorage.getItem("tk.spread") === "1"; } catch { return false; } })(),
  warnedSigned: false,
};
const pageKey = i => `${S.rev}:${S.pv[i] || 0}`;

// ------------------------------------------------------------------ theme
function applyTheme(t) {
  document.documentElement.dataset.theme = t;
  $$("[data-theme-choice]").forEach(b => b.setAttribute("aria-checked", String(b.dataset.themeChoice === t)));
}
// Settings › Look: a trial look from looks.css on top of the usual colours ("classic" = none)
function applyLook(l) {
  if (l && l !== "classic") document.documentElement.dataset.look = l;
  else delete document.documentElement.dataset.look;
}
$$("[data-theme-choice]").forEach(b => b.addEventListener("click", () => {
  applyTheme(b.dataset.themeChoice);
  api.theme_set(b.dataset.themeChoice);
}));

// ------------------------------------------------------------------ toast & status
let toastTimer = 0;
function toast(msg, { error = false, actions = [], sticky = false, busy = false, ms = 0 } = {}) {
  const t = $("#toast");
  t.classList.toggle("error", error);
  t.classList.toggle("busy", busy);
  t.querySelector("use").setAttribute("href", error ? "#i-alert" : "#i-check");
  $("#toast-msg").textContent = msg;
  const box = $("#toast-actions");
  box.replaceChildren(...actions.map(a => el("button", { text: a.label, onclick: () => { hideToast(); a.run(); } })));
  t.hidden = false;
  clearTimeout(toastTimer);
  if (!sticky) toastTimer = setTimeout(hideToast, ms || (actions.length ? 9000 : error ? 7000 : 3500));
}
function hideToast() { $("#toast").hidden = true; }
$("#toast-close").addEventListener("click", hideToast);
function setStatus(msg) { if (msg) $("#status").textContent = msg; }
window.progress = setStatus;          // Python can call progress("...") during long jobs
function fail(r) { if (r && r.error) { toast(r.error, { error: true }); return true; } return false; }

// ------------------------------------------------------------------ menus
let menuCleanup = null;
function showMenu(items, x, y, anchor) {
  closeMenu();
  const m = $("#menu");
  // an open modal dialog sits in the top layer: the menu must live inside it to appear in front and be clickable
  const host = document.querySelector("dialog[open]") || document.body;
  if (m.parentElement !== host) host.append(m);
  m.replaceChildren(...items.map(it => it === "-" ? el("hr") : el("button", {
    role: it.checked != null ? "menuitemradio" : "menuitem", class: it.danger ? "danger" : "",
    "aria-checked": it.checked != null ? String(!!it.checked) : null, disabled: it.disabled || null,
    onclick: () => { closeMenu(); it.run(); },
  }, it.icon ? icon(it.icon) : null, it.label, it.kbd ? el("span", { class: "kbd", text: keys(it.kbd) }) : null)));
  m.hidden = false;
  if (anchor) { const r = anchor.getBoundingClientRect(); x = r.left; y = r.bottom + 6; }
  const w = m.offsetWidth, h = m.offsetHeight;
  m.style.left = clamp(x, 8, innerWidth - w - 8) + "px";
  m.style.top = (y + h > innerHeight - 8 ? Math.max(8, y - h) : y) + "px";
  m.querySelector("button:not([disabled])")?.focus();
  const onDown = e => { if (!m.contains(e.target)) closeMenu(); };
  const onKey = e => {
    if (e.key === "Escape") { closeMenu(); anchor?.focus(); }
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      const bs = $$("#menu button:not([disabled])"); const i = bs.indexOf(document.activeElement);
      bs[(i + (e.key === "ArrowDown" ? 1 : -1) + bs.length) % bs.length]?.focus(); e.preventDefault();
    }
  };
  setTimeout(() => document.addEventListener("pointerdown", onDown, true));
  document.addEventListener("keydown", onKey, true);
  menuCleanup = () => { document.removeEventListener("pointerdown", onDown, true); document.removeEventListener("keydown", onKey, true); };
}
function closeMenu() { $("#menu").hidden = true; menuCleanup?.(); menuCleanup = null; }

// ------------------------------------------------------------------ dialogs
function askPassword(name, wrong) {
  const d = $("#pw-dialog");
  $("#pw-name").textContent = `Enter the password for ${name} to open it.`;
  $("#pw-error").hidden = !wrong;
  $("#pw-input").value = "";
  d.showModal();
  $("#pw-input").focus();
  return new Promise(res => d.addEventListener("close", () => res(d.returnValue === "ok" ? $("#pw-input").value : null), { once: true }));
}

/** Changes to this file that weren't saved last time (bridge_kept): put them back, or throw them away. */
function offerKept(k) {
  toast(`You have unsaved changes to this document from ${k.when}.`, {
    sticky: true,
    actions: [
      { label: "Put them back", run: async () => {
        const r = await api.kept_restore();
        if (fail(r)) return;
        loadDoc(r);
        setStatus(r.status);
        toast(r.status);
      } },
      { label: "Throw away", run: async () => { await api.kept_discard(); setStatus("The unsaved changes were thrown away."); } },
    ],
  });
}

function confirmUnsaved(what = "leave") {
  const d = $("#confirm-dialog");
  $("#confirm-title").textContent = "Save your changes?";
  $("#confirm-msg").textContent = `You've edited ${S.doc?.name ?? "this document"}. If you ${what} without saving, the changes are lost.`;
  $("#confirm-actions").replaceChildren(
    el("button", { class: "btn btn-ghost", value: "cancel", text: "Cancel" }),
    el("button", { class: "btn btn-outline", value: "discard", text: "Don't save" }),
    el("button", { class: "btn btn-primary", value: "save", text: "Save" }));
  d.showModal();
  d.querySelector('[value="save"]').focus();
  return new Promise(res => d.addEventListener("close", () => res(d.returnValue || "cancel"), { once: true }));
}

/** true when it's fine to drop the current document */
async function settleUnsaved(what) {
  if (!S.doc || !S.doc.dirty) return true;
  const c = await confirmUnsaved(what);
  if (c === "cancel") return false;
  if (c === "save") return await save();
  await api.kept_discard(true);                  // "Don't save": thrown away, not kept to offer back later
  return true;
}

// ------------------------------------------------------------------ views
function show(view) {
  S.view = view;
  $("#home").hidden = view !== "home";
  $("#editor").hidden = view !== "editor";
  document.body.classList.toggle("on-home", view === "home");
  if (view === "home") document.title = APP.name;
  closeMenu();
  window.Tabs?.render();
}

// ================================================================== HOME
function buildTasks() {
  $(".tasks").replaceChildren(...TASKS.map(t => el("button", {
    class: "task", "data-task": t.id, onclick: () => runTask(t),
  },
    el("span", { class: `task-icon tint-${t.tint}` }, icon(t.icon)),
    el("span", { class: "task-title", text: t.title }),
    el("span", { class: "task-desc", text: t.desc }))));
}

async function runTask(t, paths) {
  if (!t.editor) {                  // everything else runs a tools.py utility (ui/tasks.js)
    if (window.TaskUI) return window.TaskUI.open(t.id, paths || []);
    return toast(`“${t.title}” isn't available in this build.`, { error: true });
  }
  if (t.editor) {
    let path = paths && paths.find(canOpen);
    if (!path) {
      const r = await api.pick_files("doc");
      if (fail(r) || !r.paths?.length) return;
      path = r.paths[0];
    }
    if (await openPath(path)) {
      if (t.id === "fill") return window.Fill?.open();
      if (t.tool) window.Edit?.setTool(t.tool);
      if (t.note) setStatus(t.note);
    }
  }
}

async function renderRecent(list, carry) {
  if (!list) { const r = await api.recent(); list = r.recent || []; carry = r.carry_on; }
  if (carry !== undefined) window.Reading?.showCarryOn(carry);
  const ul = $("#recent-list");
  ul.replaceChildren(...list.map(f => el("li", {},
    el("button", { class: "recent-open", title: f.path, onclick: () => openPath(f.path) },
      icon(fileIconFor(f.path)),
      el("span", { class: "recent-text" },
        el("span", { class: "recent-name", text: f.name }),
        el("span", { class: "recent-folder", text: "‎" + f.folder })),
      el("span", { class: "recent-date", text: f.modified })),
    el("span", { class: "recent-tools" },
      el("button", { class: "icon-btn sm", "aria-label": `Show ${f.name} in folder`, title: "Show in folder",
        onclick: () => api.show_in_folder(f.path) }, icon("folder-find")),
      el("button", { class: "icon-btn sm", "aria-label": `Remove ${f.name} from this list`, title: "Remove from list",
        onclick: async () => renderRecent((await api.forget_recent(f.path)).recent) }, icon("x"))))));
  ul.hidden = !list.length;
  $("#recent-empty").hidden = !!list.length;
}

$("#scan-btn").addEventListener("click", () => window.ScanUI?.open
  ? window.ScanUI.open() : toast("Scanning isn't available in this build.", { error: true }));
$("#choose-file").addEventListener("click", async () => {
  const r = await api.pick_files("any", true);
  if (fail(r) || !r.paths?.length) return;
  if (r.paths.length === 1 && opensDirectly(r.paths[0])) return openPath(r.paths[0]);
  suggest(r.paths);
});

// ---- Batch 4: open a link (a PDF, or a webpage made into a PDF) and the sample tour (bridge_open.py)
$("#link-btn").addEventListener("click", () => openLink());
$("#sample-btn").addEventListener("click", async () => {
  const r = await api.sample_path();
  if (fail(r)) return;
  if (await openPath(r.path)) setStatus("This is the sample tour - try things out. Save puts a copy in your Documents folder.");
});

/** The Open a link dialog: paste an address -> a PDF is downloaded and opened, or a webpage is offered as a PDF. */
function openLink(prefill = "") {
  const d = $("#link-dialog"), input = $("#link-input"), msg = $("#link-msg"), acts = $("#link-actions");
  const st = { busy: false };
  const say = (text, error = false) => { msg.textContent = text || ""; msg.classList.toggle("error", error); };
  const buttons = (...bs) => acts.replaceChildren(...bs);
  const cancel = el("button", { type: "button", class: "btn btn-ghost", text: "Cancel", onclick: () => d.close() });
  const step = (sub, field = true, note = true) => {
    $("#link-sub").textContent = sub;
    $("#link-field").hidden = !field;
    $("#link-note").hidden = !note;
  };
  const busy = (on, text) => {
    st.busy = on;
    input.disabled = on;
    acts.querySelectorAll("button").forEach(b => { b.disabled = on && b !== cancel; });
    if (on) say(text);
  };
  const ask = () => {
    step("Paste the address of a PDF or a webpage.");
    say("");
    buttons(cancel, el("button", { type: "submit", class: "btn btn-primary", id: "link-go", text: "Open" }));
  };
  const go = async (allowPrivate = false) => {
    if (st.busy) return;
    busy(true, "Looking at the link…");
    const r = await api.fetch_link(input.value, allowPrivate);
    busy(false);
    if (r.error) { ask(); return say(r.error, true); }
    if (r.ask_private) {
      step(`${r.host} is on this computer's own network`, false, false);
      say("Addresses like this are things such as your router or a printer, not the internet. Only open it if you trust it.");
      return buttons(cancel, el("button", { type: "button", class: "btn btn-primary", text: "Open anyway", onclick: () => go(true) }));
    }
    if (r.webpage) {
      step("That's a webpage, not a PDF", false, false);
      say(`“${r.title}”. ${APP.name} can make a PDF of it: A4 pages, with words you can select. Nothing from the website is kept.`);
      return buttons(cancel, el("button", { type: "button", class: "btn btn-primary", id: "link-webpage", text: "Make a PDF of it",
        onclick: async () => {
          busy(true, "Making a PDF of the webpage… (this can take a few seconds)");
          const w = await api.webpage_to_pdf(r.url, r.title);
          busy(false);
          if (w.error) return say(w.error, true);
          d.close();
          if (await openPath(w.path)) { setStatus(w.status); toast(w.status, { actions: [{ label: "Show in folder", run: () => api.show_in_folder(w.path) }] }); }
        } }));
    }
    d.close();
    if (await openPath(r.path)) { setStatus(r.status); toast(r.status, { actions: [{ label: "Show in folder", run: () => api.show_in_folder(r.path) }] }); }
  };
  $("#link-form").onsubmit = e => { e.preventDefault(); go(); };
  input.value = prefill;
  input.disabled = false;
  ask();
  d.showModal();
  input.focus();
  input.select();
}
window.openLink = openLink;

async function suggest(paths) {
  const r = await api.file_info(paths);
  if (fail(r)) return;
  const files = r.files.filter(f => f.exists && !f.is_dir);
  if (!files.length) return toast("Drop files rather than folders.", { error: true });
  const exts = [...new Set(files.map(f => f.ext))];
  const fits = TASKS.filter(t => exts.every(e => t.exts.includes(e))
    && (files.length === 1 ? !t.multi : t.multi || !t.editor));   // combining needs 2+, editing takes one
  // combining is the obvious first choice for several PDFs; editing for one
  fits.sort((a, b) => (files.length > 1 ? (b.multi ? 1 : 0) - (a.multi ? 1 : 0) : 0));
  $(".dz-idle").hidden = true;
  $(".dz-suggest").hidden = false;
  $("#dz-files").replaceChildren(...files.slice(0, 6).map(f => el("span", { class: "file-chip", title: f.path },
    icon(fileIconFor(f.name)), el("span", { text: f.name }))),
    files.length > 6 ? el("span", { class: "file-chip", text: `+${files.length - 6} more` }) : "");
  const several = files.length > 1 && files.every(f => opensDirectly(f.path));
  $("#dz-q").textContent = fits.length || several ? `What would you like to do with ${files.length > 1 ? "them" : "it"}?`
    : `${APP.name} doesn't have a task for this kind of file yet.`;
  $("#dz-actions").replaceChildren(...fits.map((t, i) => el("button", {
    class: "btn" + (i === 0 ? " first" : ""), onclick: () => { resetDropzone(); runTask(t, files.map(f => f.path)); },
  }, icon(t.icon), t.title)),
    // several documents: each can open in a tab of its own (tabs.js)
    several ? el("button", { class: "btn" + (fits.length ? "" : " first"),
      onclick: () => { resetDropzone(); Tabs.openAll(files.map(f => f.path)); } }, icon("open"), "Open each in a tab") : "");
  $$(".task").forEach(c => c.classList.toggle("suggested", fits.some(t => t.id === c.dataset.task)));
  $("#dz-actions button")?.focus();
}
function resetDropzone() {
  $(".dz-idle").hidden = false;
  $(".dz-suggest").hidden = true;
  $$(".task.suggested").forEach(c => c.classList.remove("suggested"));
}
$("#dz-cancel").addEventListener("click", resetDropzone);
$("#more-tools").addEventListener("click", () => window.TaskUI ? window.TaskUI.openMoreTools()
  : toast("More tools aren't available in this build.", { error: true }));

/** Home (the "+" after the tabs, Ctrl+T): the open documents stay open in their tabs. */
async function goHome() {
  if (S.view === "home") return;
  await flushDoc();
  stashTab();
  S.tab = null; S.doc = null;
  resetDropzone();
  show("home");
  renderRecent();
  Tabs.render();
}

// ------------------------------------------------------------------ drag & drop (both views)
let dragDepth = 0;
const hasFiles = e => e.dataTransfer && Array.from(e.dataTransfer.types || []).includes("Files");
document.addEventListener("dragenter", e => { if (!hasFiles(e)) return; dragDepth++; $("#drag-overlay").hidden = false; });
document.addEventListener("dragleave", e => { if (!hasFiles(e)) return; if (--dragDepth <= 0) { dragDepth = 0; $("#drag-overlay").hidden = true; } });
document.addEventListener("dragover", e => { if (hasFiles(e)) e.preventDefault(); });
document.addEventListener("drop", e => {
  if (!hasFiles(e)) return;
  e.preventDefault();
  dragDepth = 0; $("#drag-overlay").hidden = true;
  if (DEV) toast("Drag-and-drop works in the app window (not in developer mode).", { error: true });
  // in the app window Python receives the real paths and calls onFilesDropped
});
/** Files dropped on the window.  On a document: documents open in tabs of their own; anything else goes to Home
    to choose a task (the documents stay open).  On Home: one document opens, several are offered the tasks. */
window.onFilesDropped = async paths => {
  dragDepth = 0; $("#drag-overlay").hidden = true;
  await Tabs.noDialog();                          // not under a window that's open (Pull out data working...)
  if (S.view === "editor" && paths.every(canOpen)) return Tabs.openAll(paths);
  if (S.view === "editor") await goHome();
  if (paths.length === 1 && opensDirectly(paths[0])) return openPath(paths[0]);
  suggest(paths);
};
/** Files the computer asked us to open (Finder's Open, "Open in…", a second copy, at start): each document in a tab. */
window.onFilesOpened = async paths => {
  await Tabs.noDialog();
  if (paths.length && paths.every(opensDirectly)) return Tabs.openAll(paths);
  return window.onFilesDropped(paths);
};

// ================================================================== EDITOR
const viewer = $("#viewer"), pagesEl = $("#pages"), strip = $("#strip");

/** Open a document in a tab of its own (one already open: its tab comes to the front). */
async function openPath(path) {
  if (!Tabs.room(path)) return false;
  await flushDoc();
  const slow = extOf(path) !== ".pdf";           // Word files etc. become a PDF copy first: a few seconds
  if (slow) toast(`Opening ${path.split(/[\\/]/).pop()}…`, { sticky: true, busy: true });
  let r = await api.open_file(path);
  if (slow && !r.error) hideToast();
  while (r.needs_password) {
    const pw = await askPassword(r.name, r.wrong);
    if (pw == null) return false;
    r = await api.unlock(pw);
  }
  if (r.cancelled || fail(r)) return false;
  if (r.already) { if (r.tab !== S.tab) { stashTab(); enterTab(r.tab, r); } setStatus(r.status); return true; }
  stashTab();                                    // the document that was on screen stays open in its tab
  Tabs.add(r);
  S.fit = "auto"; S.current = 0; S.sigs = null; S.warnedSigned = false;
  window.Reading?.opened(r);                     // where you were last time (page, zoom) and your bookmarks
  window.Edit?.reset();
  Tabs.modulesEnter(r.tab);                      // a new tab: its own Ask, Translate and notes panels (closed)
  show("editor");
  loadDoc(r, true);
  setStatus(`Opened ${r.name} · ${r.pages.length} page${r.pages.length === 1 ? "" : "s"}`);
  if (r.converted) explainCopy(r);
  if (r.kept) offerKept(r.kept);
  window.Fill?.docOpened(r);
  if (r.signatures) checkSignatures();
  window.ScanWords?.docOpened();                 // a scan: offer to make its words selectable
  return true;
}

/** A Word file, picture... opens as a PDF copy: say so once, plainly. */
function explainCopy(r) {
  const c = r.converted, stem = c.from.replace(/\.[^.]+$/, "");
  toast(c.text_only
    ? (APP.mac ? `Showing just the words of ${c.from} - install the free LibreOffice to see its layout and pictures. Your file isn't changed.`
      : `Showing just the words of ${c.from} - this PC has no Microsoft Office or LibreOffice to show its layout and pictures. Your file isn't changed.`)
    : `This is a PDF copy of your ${c.kind} - ${c.from} isn't changed. Saving makes ${stem}_edited.pdf.`, { ms: 9000 });
}

$("#btn-open").addEventListener("click", e => window.Panels?.openMenu ? window.Panels.openMenu(e.currentTarget) : openDialog());
async function openDialog() {
  const r = await api.pick_files("doc");
  if (!fail(r) && r.paths?.length) openPath(r.paths[0]);
}
$("#btn-home").addEventListener("click", goHome);

/** Apply a fresh document_info.  Rebuilds pages when the document changed.  `anchor`: where the view was
    (anchorPoint) - a tab coming back to the front. */
function loadDoc(info, fresh = false, anchor = null) {
  if (info.tab && info.tab !== S.tab) return;     // about a tab that's in the back now (it reloads when it's shown)
  if (fresh) S.turn = 0;                          // a newly opened document starts the right way up
  S.doc = info;
  S.rev++;
  S.pv = [];
  S.current = clamp(S.current, 0, info.pages.length - 1);
  updateChrome();
  if (S.fit) S.zoom = fitZoom();                  // a fresh document with no fit kept its remembered zoom
  buildPages();
  buildStrip();
  if (S.query) runSearch(S.query, true);
  if (anchor) { restoreAnchor(anchor); onScroll(); }        // a tab back in front: where it was, straight away
  requestAnimationFrame(() => { if (anchor) restoreAnchor(anchor); else scrollToPage(S.current, !fresh); onScroll(); });
}

function updateChrome() {
  const d = S.doc;
  $("#doc-name").textContent = d.name;
  $("#doc-name").title = d.path || "";
  const sub = $("#doc-sub");
  sub.classList.toggle("dirty", d.dirty);
  sub.textContent = d.dirty ? "Edited · not saved" : d.saved_to ? "Saved · original unchanged"
    : `${d.converted ? "PDF copy · " : ""}${d.pages.length} page${d.pages.length === 1 ? "" : "s"}`;
  $("#btn-undo").disabled = !d.can_undo;
  $("#btn-redo").disabled = !d.can_redo;
  document.title = (d.dirty ? "• " : "") + d.name + " — " + APP.name;
  Tabs.changed(d);
  renderShield();
  fitToolbar();
  window.Panels?.docChanged?.();
}
/** Tool labels collapse to icons (with tooltips) when the toolbar would overflow. */
function fitToolbar() {
  const tb = $(".toolbar");
  tb.classList.remove("compact");
  if (tb.scrollWidth > tb.clientWidth + 1) tb.classList.add("compact");
}
window.addEventListener("resize", () => { if (S.view === "editor") fitToolbar(); });

// ---- signatures (the green shield)
async function checkSignatures() {
  $("#signed").hidden = false;
  $("#signed-text").textContent = "Checking signature…";
  const r = await api.signature_status();
  S.sigs = r.signatures || [];
  renderShield();
}
function renderShield() {
  const b = $("#signed");
  if (!S.doc || !S.doc.signatures) { b.hidden = true; return; }
  b.hidden = false;
  if (!S.sigs) return;
  const bad = S.sigs.some(s => !s.ok) || S.doc.dirty;
  b.classList.toggle("warn", bad);
  b.querySelector("use").setAttribute("href", bad ? "#i-shield-alert" : "#i-shield");
  const who = [...new Set(S.sigs.map(s => s.signer))];
  $("#signed-text").textContent = S.doc.dirty ? "Edits will break the signature"
    : bad ? "Changed after signing" : `Signed by ${who.slice(0, 2).join(", ")}${who.length > 2 ? " +" + (who.length - 2) : ""}`;
  fitToolbar();
}
$("#signed").addEventListener("click", () => {
  if (window.Sign) return window.Sign.verify();
  if (!S.sigs?.length) return;
  const s = S.sigs[0];
  toast(S.doc.dirty ? "This document is signed. Saving your edits makes a copy whose signature no longer checks out."
    : s.ok ? `Signed by ${s.signer}${s.when ? " on " + s.when : ""}. Nothing has changed since.`
      : `The signature by ${s.signer} doesn't match - the file was changed after it was signed.`);
});

// ---- zoom
const ZOOMS = [0.5, 0.75, 1, 1.25, 1.5, 2, 3];
/** "auto" = fit width but never bigger than a comfortable reading size; "width"; "page" = whole page visible */
function fitZoom(kind = S.fit || "auto") {
  const pages = S.doc.pages;
  const side = (S.turn || 0) % 180 !== 0;          // the view is turned: a page's height runs across
  // the typical page's width (the middle one), not the widest: one wide page (a cover spread) would make every
  // other page small - it can be scrolled sideways instead
  const widths = pages.map(p => side ? p.h : p.w).sort((a, b) => a - b);
  const maxW = widths[Math.floor(widths.length / 2)];
  const availW = viewer.clientWidth - 96;
  let z = S.spread && pages.length > 1 ? (availW - 22) / (2 * maxW) : availW / maxW;
  if (kind === "page") {
    const p = pages[S.current] || pages[0];
    z = Math.min(z, (viewer.clientHeight - 56) / (side ? p.w : p.h));
  }
  if (kind === "auto") z = Math.min(z, 1.35);
  return clamp(z, 0.2, 6);
}
function setSpread(on) {
  S.spread = on;
  try { localStorage.setItem("tk.spread", on ? "1" : "0"); } catch { /* private mode */ }
  pagesEl.classList.toggle("spread", on);
  setZoom(S.zoom, S.fit || (on ? "auto" : null));
  requestAnimationFrame(() => scrollToPage(S.current));
  setStatus(on ? "Showing two pages side by side." : "Showing one page at a time.");
}
function setZoom(z, fit = null) {
  if (!S.doc) return;
  const anchor = anchorPoint();
  S.fit = fit;
  S.zoom = fit ? fitZoom(fit) : clamp(z, 0.2, 6);
  sizePages();
  restoreAnchor(anchor);
  $("#zoom-val").textContent = Math.round(S.zoom * 100) + "%";
  clearTimeout(setZoom.t);
  setZoom.t = setTimeout(scheduleRender, 120);
  window.Reading?.placeChanged();
}
const zoomStep = dir => {
  const cur = S.zoom;
  const next = dir > 0 ? ZOOMS.find(z => z > cur + 0.01) ?? cur * 1.25 : [...ZOOMS].reverse().find(z => z < cur - 0.01) ?? cur / 1.25;
  setZoom(next);
};
$("#zoom-in").addEventListener("click", () => zoomStep(1));
$("#zoom-out").addEventListener("click", () => zoomStep(-1));
$("#zoom-val").addEventListener("click", e => showMenu([
  { label: "Automatic", checked: S.fit === "auto", run: () => setZoom(0, "auto"), kbd: "Ctrl 0" },
  { label: "Fit width", checked: S.fit === "width", run: () => setZoom(0, "width") },
  { label: "Whole page", checked: S.fit === "page", run: () => setZoom(0, "page") },
  "-",
  { label: "Two pages side by side", icon: "spread", checked: S.spread, run: () => setSpread(!S.spread) },
  "-",
  ...ZOOMS.map(z => ({ label: Math.round(z * 100) + "%", checked: !S.fit && Math.abs(S.zoom - z) < 0.005, run: () => setZoom(z) })),
], 0, 0, e.currentTarget));
viewer.addEventListener("wheel", e => {
  if (!e.ctrlKey) return;
  e.preventDefault();
  setZoom(S.zoom * Math.exp(-e.deltaY * 0.0015));
}, { passive: false });
new ResizeObserver(() => {
  if (S.doc && S.view === "editor" && S.fit) return setZoom(0, S.fit);
  if (S.doc) showAcross(pagesEl.children[S.current]);      // a panel opened beside the pages: keep this one in view
  onScroll();
}).observe(viewer);

function anchorPoint() {
  const pg = pagesEl.children[S.current];
  if (!pg) return null;
  const mid = viewer.scrollTop + viewer.clientHeight / 2, cx = viewer.scrollLeft + viewer.clientWidth / 2;
  return { i: S.current, f: (mid - pg.offsetTop) / pg.offsetHeight, fx: (cx - pg.offsetLeft) / pg.offsetWidth };
}
function restoreAnchor(a) {
  if (!a) return;
  const pg = pagesEl.children[a.i];
  if (!pg) return;
  viewer.scrollTop = pg.offsetTop + a.f * pg.offsetHeight - viewer.clientHeight / 2;
  viewer.scrollLeft = xFor(pg, a.fx);
}
// sideways, a page is shown where it can be seen: every page is centred in the widest one's width, so a narrower
// page can sit off to one side of a narrow view.  It fits: centred in the view; it doesn't: the part that was in the
// middle (fx, 0-1 across the page) stays there, without desk beside the page
function xFor(pg, fx = 0.5) {
  const w = viewer.clientWidth, left = pg.offsetLeft - 20, right = pg.offsetLeft + pg.offsetWidth + 20;
  if (right - left <= w) return pg.offsetLeft + pg.offsetWidth / 2 - w / 2;
  return clamp(pg.offsetLeft + fx * pg.offsetWidth - w / 2, left, right - w);
}
// after scrolling down from a wider page, or a panel opening beside the pages (the view narrows from the right, so
// the page ends up against that edge, under the scrubber): a page that fits is centred; a wider one with desk beside
// it is moved over (as far as is needed, so a page read sideways stays put)
function showAcross(pg) {
  if (!pg) return;
  const x = viewer.scrollLeft, w = viewer.clientWidth;
  if (pg.offsetWidth + 40 <= w) {
    viewer.scrollLeft = xFor(pg);
  } else {
    viewer.scrollLeft = clamp(x, pg.offsetLeft - 20, pg.offsetLeft + pg.offsetWidth + 20 - w);
  }
}

// ---- pages
const pageObserver = new IntersectionObserver(entries => {
  for (const en of entries) {
    const i = +en.target.dataset.i;
    if (en.isIntersecting) { S.visible.add(i); window.Edit?.pageVisible(i); }
    else {
      S.visible.delete(i);
      const img = en.target.querySelector("img");   // free memory for pages far away
      if (img) { img.remove(); en.target.dataset.key = ""; }
    }
  }
  scheduleRender();
}, { root: viewer, rootMargin: "900px 0px" });

function buildPages() {
  pageObserver.disconnect();
  pagesEl.classList.toggle("spread", S.spread);
  S.visible.clear();
  pagesEl.replaceChildren(...S.doc.pages.map((p, i) => el("div", {
    class: "page page-loading", "data-i": i, role: "img", "aria-label": `Page ${i + 1}`,
  })));
  sizePages();
  $("#zoom-val").textContent = Math.round(S.zoom * 100) + "%";
  for (const pg of pagesEl.children) { window.Edit?.decorate(pg, +pg.dataset.i); pageObserver.observe(pg); }
}
function sizePages() {
  pagesEl.style.setProperty("--z", S.zoom);          // overlays size themselves with calc(var(--z) * Npx)
  const t = S.turn || 0, side = t % 180 !== 0;
  S.doc.pages.forEach((p, i) => {
    const d = pagesEl.children[i];
    const w = Math.round(p.w * S.zoom), h = Math.round(p.h * S.zoom);
    d.style.width = w + "px";
    d.style.height = h + "px";
    // the view turned (on screen only): the page is drawn turned round its middle; margins make its room the
    // turned size, so pages don't overlap and scrolling covers them (pagePoint in edit.js turns clicks back)
    d.style.transform = t ? `rotate(${t}deg)` : "";
    d.style.margin = side ? `${(w - h) / 2}px ${(h - w) / 2}px` : "";
  });
  pagesEl.classList.toggle("turned", t !== 0);
}

/** Turn the view (on screen only - the file, printing and the pages panel never change): 0, 90, 180, 270. */
function setTurn(t) {
  if (!S.doc) return;
  S.turn = ((t % 360) + 360) % 360;
  const page = S.current;
  setZoom(S.zoom, S.fit || "auto");
  requestAnimationFrame(() => scrollToPage(page));
  setStatus(S.turn ? `The view is turned ${({ 90: "right", 180: "upside down", 270: "left" })[S.turn]} - on screen only, the file isn't changed.`
    : "The view is back the right way up.");
}
function turnMenuItem() {
  const now = { 0: "Normal", 90: "Right", 180: "Upside down", 270: "Left" }[S.turn || 0];
  return {
    label: `Turn the view: ${now}`, icon: "rotate-r", run: () => showMenu([
      { label: "Turn right", icon: "rotate-r", run: () => setTurn((S.turn || 0) + 90) },
      { label: "Turn left", icon: "rotate-l", run: () => setTurn((S.turn || 0) - 90) },
      { label: "Back to normal", checked: !S.turn, disabled: !S.turn, run: () => setTurn(0) },
      "-",
      { label: "On screen only. To turn the pages in the file, use the page's menu in the pages panel.", disabled: true, run() {} },
    ], innerWidth - 360, 64),
  };
}
window.ViewTurn = { set: setTurn, get turn() { return S.turn || 0; } };

function scheduleRender() {
  if (!S.doc) return;
  const scale = Math.round(S.zoom * DPR() * 100) / 100;
  const keyOf = i => `${pageKey(i)}:${scale}`;
  const todo = [...S.visible].filter(i => pagesEl.children[i]?.dataset.key !== keyOf(i) && pagesEl.children[i]?.dataset.want !== keyOf(i))
    .sort((a, b) => Math.abs(a - S.current) - Math.abs(b - S.current));
  while (S.inflight < 2 && todo.length) {
    const i = todo.shift();
    const key = keyOf(i);
    const div = pagesEl.children[i];
    div.dataset.want = key;
    S.inflight++;
    const rev = S.rev;
    api.render_page(i, scale).then(r => {
      S.inflight--;
      if (rev === S.rev && key === keyOf(i) && !r.error && pagesEl.children[i] === div && S.visible.has(i)) {
        let img = div.querySelector("img");
        if (!img) { img = el("img", { alt: "", draggable: "false", decoding: "async" }); div.prepend(img); }
        img.src = r.src;
        window.DarkPages?.rendered(div, i, img);      // dark pages: the photos again in their own colours
        div.dataset.key = key;
        div.classList.remove("page-loading");
      }
      if (div.dataset.want === key) div.dataset.want = "";
      scheduleRender();
    });
  }
}

function scrollToPage(i, smooth = false) {
  const pg = pagesEl.children[i];
  if (!pg) return;
  viewer.scrollTo({ top: pg.offsetTop - 20, left: xFor(pg, 0), behavior: smooth ? "smooth" : "auto" });
}
function goTo(i) {
  if (!S.doc) return;
  i = clamp(i, 0, S.doc.pages.length - 1);
  scrollToPage(i);
  setCurrent(i);
}

let scrollRaf = 0;
viewer.addEventListener("scroll", () => { if (!scrollRaf) scrollRaf = requestAnimationFrame(() => { scrollRaf = 0; onScroll(); }); });
function onScroll() {
  if (!S.doc) return;
  const line = viewer.scrollTop + viewer.clientHeight * 0.35;
  const kids = pagesEl.children;
  let lo = 0, hi = kids.length - 1;
  while (lo < hi) {                     // last page whose top is above the reading line
    const mid = (lo + hi + 1) >> 1;
    if (kids[mid].offsetTop <= line) lo = mid; else hi = mid - 1;
  }
  if (viewer.scrollTop + viewer.clientHeight >= viewer.scrollHeight - 2) lo = kids.length - 1;
  setCurrent(lo);
  updateScrubber();
}
function setCurrent(i) {
  const changed = i !== S.current;
  S.current = i;
  if (changed) { window.ScanWords?.pageShown(); window.Translate?.pageShown(i); showAcross(pagesEl.children[i]); }
  $("#hud-page").textContent = `Page ${i + 1} of ${S.doc.pages.length}`;
  const prev = strip.querySelector(".thumb.current");
  const now = strip.children[i];
  if (prev !== now) {
    prev?.classList.remove("current"); prev?.setAttribute("aria-selected", "false");
    now?.classList.add("current"); now?.setAttribute("aria-selected", "true");
    if (now && strip.offsetParent) {
      const sr = strip.getBoundingClientRect(), nr = now.getBoundingClientRect();
      if (nr.top < sr.top || nr.bottom > sr.bottom) strip.scrollBy({ top: nr.top - sr.top - sr.height / 2 + nr.height / 2, behavior: "smooth" });
    }
  }
  if (changed) { scheduleRender(); window.Reading?.placeChanged(); }
}

// ---- right-edge scrubber
const scrub = $("#scrubber"), scrubThumb = $("#scrub-thumb"), scrubTip = $("#scrub-tip");
function updateScrubber() {
  const H = scrub.clientHeight, sh = viewer.scrollHeight, ch = viewer.clientHeight;
  scrub.hidden = sh <= ch + 1;
  const h = Math.max(24, H * ch / sh);
  scrubThumb.style.height = h + "px";
  scrubThumb.style.top = (sh > ch ? (H - h) * viewer.scrollTop / (sh - ch) : 0) + "px";
}
function scrubTo(clientY) {
  const r = scrub.getBoundingClientRect();
  const f = clamp((clientY - r.top) / r.height, 0, 1);
  viewer.scrollTop = f * (viewer.scrollHeight - viewer.clientHeight);
  onScroll();
  scrubTip.hidden = false;
  scrubTip.style.top = (clientY - r.top) + "px";
  scrubTip.textContent = `Page ${S.current + 1}`;
}
scrub.addEventListener("pointerdown", e => {
  scrub.setPointerCapture(e.pointerId); scrub.classList.add("dragging"); scrubTo(e.clientY);
  const move = ev => scrubTo(ev.clientY);
  const up = () => { scrub.classList.remove("dragging"); scrubTip.hidden = true; scrub.removeEventListener("pointermove", move); };
  scrub.addEventListener("pointermove", move);
  scrub.addEventListener("pointerup", up, { once: true });
  scrub.addEventListener("pointercancel", up, { once: true });
});

// ---- page list (left)
const thumbObserver = new IntersectionObserver(entries => {
  for (const en of entries) if (en.isIntersecting) loadThumb(en.target);
}, { root: strip, rootMargin: "400px 0px" });
function buildStrip() {
  thumbObserver.disconnect();
  strip.replaceChildren(...S.doc.pages.map((p, i) => {
    const t = el("button", {
      class: "thumb", role: "option", "aria-selected": "false", "aria-label": `Page ${i + 1}`, draggable: "true", "data-i": i,
      onclick: () => goTo(i),
      oncontextmenu: e => { e.preventDefault(); pageMenu(i, e.clientX, e.clientY); },
      onkeydown: e => {
        if (e.key === "ContextMenu" || (e.shiftKey && e.key === "F10")) { e.preventDefault(); const r = t.getBoundingClientRect(); pageMenu(i, r.left, r.top - 8); }
        if (e.key === "Delete" || (APP.mac && e.key === "Backspace")) pageOp("delete", i);   // a Mac's delete key
        if (e.key === "ArrowDown" || e.key === "ArrowRight") { e.preventDefault(); strip.children[i + 1]?.focus(); }
        if (e.key === "ArrowUp" || e.key === "ArrowLeft") { e.preventDefault(); strip.children[i - 1]?.focus(); }
      },
    },
      el("span", { class: "thumb-img", style: `--r:${(p.w / p.h).toFixed(4)}` }),
      el("span", { class: "thumb-n", text: String(i + 1) }));
    window.Reading?.decorateThumb(t, i);
    thumbObserver.observe(t);
    return t;
  }));
  window.Reading?.stripBuilt();
  strip.children[S.current]?.classList.add("current");
  const n = S.doc.pages.length;
  $("#pp-count").textContent = `${n} page${n === 1 ? "" : "s"}`;
}
async function loadThumb(t) {
  const i = +t.dataset.i, key = pageKey(i);
  if (t.dataset.rev === key) return;
  t.dataset.rev = key;
  const box = t.querySelector(".thumb-img");
  const r = await api.thumbnail(i, Math.round(box.offsetWidth * DPR()));
  if (key === pageKey(i) && !r.error) {
    const img = el("img", { src: r.src, alt: "", draggable: "false" });
    box.replaceChildren(img);
    window.DarkPages?.rendered(box, i, img);
  }
}
function reloadThumb(i) {
  const t = strip.children[i];
  if (!t) return;
  t.dataset.rev = "";
  thumbObserver.unobserve(t); thumbObserver.observe(t);      // reloads now if it's in view, later if not
}

/** Apply the result of an edit.  Only the changed pages re-render when the page layout is unchanged. */
function refreshDoc(r, changed = r.changed) {
  if (r.tab && r.tab !== S.tab) return;           // about a tab that's in the back now
  const old = S.doc;
  const same = old && old.pages.length === r.pages.length &&
    r.pages.every((p, k) => p.w === old.pages[k].w && p.h === old.pages[k].h && p.rot === old.pages[k].rot);
  if (!same) { loadDoc(r); window.Edit?.docChanged(); return; }
  S.doc = r;
  updateChrome();
  for (const i of changed || r.pages.map((_, k) => k)) {
    S.pv[i] = (S.pv[i] || 0) + 1;
    reloadThumb(i);
    window.Edit?.pageChanged(i);
  }
  scheduleRender();
  if (S.query) runSearch(S.query, true);
  window.ScanWords?.recheck();
  if (r.signatures && r.dirty && !S.warnedSigned) {
    S.warnedSigned = true;
    toast("This document is digitally signed. Saving your edits makes a copy whose signature no longer checks out.", { error: true });
  }
}
window.refreshDoc = refreshDoc;

// drag to reorder
strip.addEventListener("dragstart", e => {
  const t = e.target.closest(".thumb"); if (!t) return;
  S.dragFrom = +t.dataset.i;
  t.classList.add("dragging");
  e.dataTransfer.effectAllowed = "move";
  e.dataTransfer.setData("text/x-toolkit-page", String(S.dragFrom));
});
function clearDropMarks() { $$(".thumb.drop-before, .thumb.drop-after").forEach(t => t.classList.remove("drop-before", "drop-after")); }
strip.addEventListener("dragover", e => {
  if (S.dragFrom == null) return;
  e.preventDefault();
  const t = e.target.closest(".thumb"); clearDropMarks(); if (!t) return;
  const r = t.getBoundingClientRect();
  t.classList.add(e.clientY < r.top + r.height / 2 ? "drop-before" : "drop-after");
});
strip.addEventListener("drop", e => {
  if (S.dragFrom == null) return;
  e.preventDefault(); e.stopPropagation();
  const t = $(".thumb.drop-before, .thumb.drop-after");
  if (t) {
    let to = +t.dataset.i + (t.classList.contains("drop-after") ? 1 : 0);
    if (to > S.dragFrom) to -= 1;
    if (to !== S.dragFrom) pageOp("move", S.dragFrom, to);
  }
  clearDropMarks();
});
strip.addEventListener("dragend", () => { $$(".thumb.dragging").forEach(t => t.classList.remove("dragging")); clearDropMarks(); S.dragFrom = null; });

function pageMenu(i, x, y) {
  const n = S.doc.pages.length;
  showMenu([
    { label: "Turn right", icon: "rotate-r", run: () => pageOp("rotate", i, 90) },
    { label: "Turn left", icon: "rotate-l", run: () => pageOp("rotate", i, -90) },
    "-",
    { label: "Move earlier", icon: "back", disabled: i === 0, run: () => pageOp("move", i, i - 1) },
    { label: "Move later", icon: "fwd", disabled: i === n - 1, run: () => pageOp("move", i, i + 1) },
    { label: "Copy this page", icon: "copy", run: () => pageOp("duplicate", i) },
    { label: "Add a blank page after", icon: "blank", run: () => pageOp("blank", i) },
    ...(window.Reading?.pageItems(i) || []),
    ...(window.Panels?.pageItems?.(i) || []),
    { label: "Crop page…", icon: "fit", run: () => window.Edit?.startCrop(i) },
    { label: "Save this page as a PDF", icon: "export", run: () => pageOp("extract", i) },
    "-",
    { label: "Delete page", icon: "trash", danger: true, disabled: n === 1, run: () => pageOp("delete", i), kbd: "Del" },
  ], x, y);
}

async function pageOp(op, i, arg = null) {
  const r = await api.page_op(op, i, arg);
  if (fail(r)) return;
  if (op === "move") S.current = arg;
  else if (op === "duplicate" || op === "blank") S.current = i + 1;
  else if (op === "delete") S.current = Math.min(i, r.pages.length - 1);
  if (op !== "extract") { await window.Reading?.pagesChanged(); loadDoc(r); }
  setStatus(r.status);
  if (r.saved) toast(r.status, { actions: fileActions(r.saved) });
  if (op === "move") strip.children[S.current]?.focus();
}

$("#btn-comments").addEventListener("click", () => window.Panels?.toggleComments?.());
$("#btn-print").addEventListener("click", () => window.Share?.print());
$("#btn-ask").addEventListener("click", () => window.AI?.toggle());
$("#btn-settings-home").addEventListener("click", () => window.Settings?.open("ai"));
// ---- page list show / hide, and resizable panel edges (remembered on this PC)
const pref = { get(k, d) { try { return localStorage.getItem("tk." + k) ?? d; } catch { return d; } },
               set(k, v) { try { localStorage.setItem("tk." + k, String(v)); } catch { /* private mode */ } } };
function showPages(on) {
  $("#filmstrip").classList.toggle("hidden", !on);
  $("#btn-pages").setAttribute("aria-pressed", String(on));
  pref.set("pages", on ? "1" : "0");
  if (on) { strip.children[S.current]?.scrollIntoView({ block: "center" }); reloadAllThumbs(); }
}
function reloadAllThumbs() { for (let i = 0; i < strip.children.length; i++) reloadThumb(i); }
$("#btn-pages").addEventListener("click", () => showPages($("#filmstrip").classList.contains("hidden")));
$("#pp-close").addEventListener("click", () => { showPages(false); $("#btn-pages").focus(); });

/** Drag (or arrow-key) a panel edge to resize it.  side "left": the panel is left of the grip; "right": it's right of it. */
function makeGrip(grip, { side, get, set, min, max, def, key, done }) {
  const clamp = w => Math.round(Math.max(min(), Math.min(max(), w)));
  const apply = w => { set(clamp(w)); grip.setAttribute("aria-valuenow", String(clamp(w))); };
  apply(+pref.get(key, def) || def);
  grip.addEventListener("pointerdown", e => {
    if (e.button !== 0) return;
    e.preventDefault();
    const x0 = e.clientX, w0 = get();
    grip.setPointerCapture(e.pointerId);
    grip.classList.add("dragging"); document.body.classList.add("resizing");
    const move = ev => apply(w0 + (side === "left" ? ev.clientX - x0 : x0 - ev.clientX));
    const up = () => {
      grip.removeEventListener("pointermove", move);
      grip.classList.remove("dragging"); document.body.classList.remove("resizing");
      pref.set(key, get()); done?.();
    };
    grip.addEventListener("pointermove", move);
    grip.addEventListener("pointerup", up, { once: true });
    grip.addEventListener("pointercancel", up, { once: true });
  });
  grip.addEventListener("dblclick", () => { apply(def); pref.set(key, get()); done?.(); });
  grip.addEventListener("keydown", e => {
    const step = e.shiftKey ? 64 : 16, grow = side === "left" ? "ArrowRight" : "ArrowLeft", shrink = side === "left" ? "ArrowLeft" : "ArrowRight";
    if (e.key !== grow && e.key !== shrink && e.key !== "Home") return;
    e.preventDefault();
    apply(e.key === "Home" ? def : get() + (e.key === grow ? step : -step));
    pref.set(key, get()); done?.();
  });
  grip.setAttribute("aria-valuemin", String(min())); grip.setAttribute("aria-valuemax", String(max()));
}
const editorEl = $("#editor"), pagePanel = $("#filmstrip");
makeGrip($("#pp-grip"), {
  side: "left", key: "pagesW", def: 184, min: () => 120, max: () => 380,
  get: () => parseInt(getComputedStyle(pagePanel).getPropertyValue("--pp-w")) || 184,
  set: w => pagePanel.style.setProperty("--pp-w", w + "px"),
  done: reloadAllThumbs,                         // sharper thumbnails at the new size
});
/** The Ask and comments panels share the right edge and one width; each gets its own grip (ui/ai.js, ui/panels.js). */
window.sideGrip = grip => makeGrip(grip, {
  side: "right", key: "sideW", def: 316, min: () => 260,
  max: () => { const sw = $(".stage").clientWidth; return sw ? Math.max(260, Math.min(760, sw - 320)) : 760; },   // 0 while hidden
  get: () => parseInt(getComputedStyle(editorEl).getPropertyValue("--side-w")) || 316,
  set: w => editorEl.style.setProperty("--side-w", w + "px"),
});
showPages(pref.get("pages", "1") === "1");

// ---- undo / redo
async function undo() { window.Edit?.cancel(); const r = await api.undo(); if (!fail(r)) { refreshDoc(r, null); setStatus(r.status); } }
async function redo() { window.Edit?.cancel(); const r = await api.redo(); if (!fail(r)) { refreshDoc(r, null); setStatus(r.status); } }
$("#btn-undo").addEventListener("click", undo);
$("#btn-redo").addEventListener("click", redo);

// ---- tools (ui/edit.js)
$("#tools").addEventListener("click", e => {
  const b = e.target.closest("button[data-tool]");
  if (b) window.Edit?.setTool(b.dataset.tool);
});

// ---- save
const fileActions = path => [
  { label: "Open", run: () => api.open_external(path) },
  { label: "Show in folder", run: () => api.show_in_folder(path) },
  ...(window.Share && /\.pdf$/i.test(path) ? [{ label: "Email", run: () => window.Share.email(path) }] : []),
];
async function save(as = false) {
  if (!S.doc) return false;
  await window.Edit?.commitPending();
  const r = as ? await api.save_as() : await api.save();
  if (r.cancelled || fail(r)) return false;
  if (r.tab !== S.tab) { window.Tabs?.changed(r); return true; }   // another tab came to the front meanwhile
  S.doc = r; updateChrome();
  setStatus(r.status);
  toast(`${r.status} Your original is unchanged.`, { actions: fileActions(r.saved) });
  return true;
}
$("#btn-save").addEventListener("click", () => save());
$("#btn-more").addEventListener("click", e => showMenu([
  { label: "Save a copy as…", icon: "export", kbd: "Ctrl Shift S", run: () => save(true) },
  { label: "Print…", icon: "print", kbd: "Ctrl P", run: () => window.Share?.print() },
  { label: "Email this document…", icon: "mail", run: () => window.Share?.email() },
  { label: "Save a final copy (can't be changed)…", icon: "final", run: () => window.Share?.finalCopy() },
  S.doc?.locked ? { label: "Save a copy without the password", icon: "lock", run: () => window.Share?.unprotect() }
    : { label: "Protect with a password…", icon: "lock", run: () => window.Share?.protect() },
  "-",
  { label: "Show original in folder", icon: "folder-find", disabled: !S.doc?.source, run: () => api.show_in_folder(S.doc.source) },
  { label: S.doc?.saved_to ? "Show saved copy in folder" : "Nothing saved yet", icon: "folder-find", disabled: !S.doc?.saved_to,
    run: () => api.show_in_folder(S.doc.saved_to) },
  "-",
  { label: "Document details…", icon: "info", run: () => window.Reading?.details() },
  { label: "Focus mode", icon: "focus", kbd: "F", run: () => window.Reading?.focus(true) },
  window.DarkPages?.menuItem(),
  turnMenuItem(),
  { label: "Keyboard shortcuts", icon: "keyboard", kbd: "?", run: () => window.Shortcuts?.open() },
  "-",
  { label: "Fill with my details…", icon: "form", run: () => window.Fill?.open() },
  { label: "Find personal details to remove…", icon: "redact", run: () => window.Fill?.findPersonal() },
  { label: "My details…", icon: "edittext", run: () => window.Fill?.details() },
  { label: "Pull out data (tables, statements, invoices)…", icon: "list", run: () => window.Extract?.open() },
  { label: "Ask about this document (AI)…", icon: "ask", run: () => window.AI?.toggle(true) },
  { label: "Translate this document (AI)…", icon: "translate", run: () => window.Translate?.toggle(true) },
  { label: "Settings…", icon: "gear", run: () => window.Settings?.open("ai") },
  "-",
  { label: "Check signatures…", icon: "shield", run: () => window.Sign?.verify() },
  { label: "My signing identity…", icon: "sign", run: async () => {
    const id = await api.identity_get();
    if (fail(id)) return;
    if (!id.exists) return window.Sign?.setupIdentity();
    showMenu([
      { label: `${id.name} · valid to ${id.expires}`, disabled: true, run() {} },
      { label: "Save my certificate to share (.cer)…", icon: "export", run: () => window.Sign?.exportCertificate() },
      { label: "Set up a new signing identity…", icon: "shield", run: () => window.Sign?.setupIdentity(true) },
      { label: "Change my signature (drawn / typed)…", icon: "edittext", run: () => window.Edit?.openSignaturePad() },
    ], innerWidth - 300, 64);
  } },
  "-",
  { label: "Close document", icon: "x", kbd: "Ctrl W", run: () => closeTab(S.tab) },
], 0, 0, e.currentTarget));

// ---- find (options and the results list by page: Match case, Whole words, List)
const findInput = $("#find-input"), findResults = $("#find-results");
function openFind() {
  $("#findbar").hidden = false;
  findInput.focus(); findInput.select();
}
function clearHits() { for (const h of $$(".page .hit")) h.remove(); }
function closeFind() {
  $("#findbar").hidden = true;
  S.query = ""; S.hits = []; S.hitIdx = -1;
  clearHits();
  $("#find-count").textContent = "";
  showResults(false);
}
$("#btn-find").addEventListener("click", () => ($("#findbar").hidden ? openFind() : closeFind()));
$("#find-close").addEventListener("click", () => { closeFind(); viewer.focus(); });
findInput.addEventListener("input", () => {
  clearTimeout(findInput.t);
  findInput.t = setTimeout(() => runSearch(findInput.value.trim()), 250);
});
findInput.addEventListener("keydown", e => {
  if (e.key === "Enter") { e.preventDefault(); if (findInput.value.trim() !== S.query) runSearch(findInput.value.trim()); else stepHit(e.shiftKey ? -1 : 1); }
  if (e.key === "Escape") { closeFind(); viewer.focus(); }
  if (e.key === "ArrowDown" && !findResults.hidden) { e.preventDefault(); findResults.querySelector(".fr-hit.current, .fr-hit")?.focus(); }
});
$("#find-next").addEventListener("click", () => stepHit(1));
$("#find-prev").addEventListener("click", () => stepHit(-1));
for (const [id, key] of [["#find-case", "match_case"], ["#find-word", "whole_words"]]) {
  $(id).addEventListener("click", e => {
    S.findOpts[key] = !S.findOpts[key];
    e.currentTarget.setAttribute("aria-pressed", String(S.findOpts[key]));
    if (findInput.value.trim()) runSearch(findInput.value.trim(), true);
    findInput.focus();
  });
}
$("#find-list").addEventListener("click", () => { showResults(findResults.hidden); if (findResults.hidden) findInput.focus(); });
function showResults(on) {
  findResults.hidden = !on;
  $("#find-list").setAttribute("aria-pressed", String(on));
  if (on) renderResults();
}

async function runSearch(q, keepPlace = false) {
  S.query = q;
  clearHits();
  if (!q) { S.hits = []; $("#find-count").textContent = ""; renderResults(); return; }
  const opts = { ...S.findOpts };
  const r = await api.search(q, opts);
  if (fail(r) || q !== S.query || opts.match_case !== S.findOpts.match_case || opts.whole_words !== S.findOpts.whole_words) return;
  clearHits();                                       // an earlier search may have finished in between
  S.hits = r.hits;
  for (const [k, h] of S.hits.entries()) {
    const p = S.doc.pages[h.page];
    for (const [x0, y0, x1, y1] of h.rects || [h.rect]) {      // a match over a line break has a box on each line
      pagesEl.children[h.page]?.append(el("div", {
        class: "hit", "data-k": k,
        style: `left:${x0 / p.w * 100}%;top:${y0 / p.h * 100}%;width:${(x1 - x0) / p.w * 100}%;height:${(y1 - y0) / p.h * 100}%`,
      }));
    }
  }
  if (!S.hits.length) { S.hitIdx = -1; $("#find-count").textContent = "No matches"; renderResults(); return; }
  const first = keepPlace ? Math.max(0, S.hits.findIndex(h => h.page >= S.current)) : S.hits.findIndex(h => h.page >= S.current);
  S.hitIdx = (first < 0 ? 0 : first) - 1;
  renderResults();
  stepHit(1, keepPlace);
}
function stepHit(d, noScroll = false) {
  if (!S.hits.length) return;
  S.hitIdx = (S.hitIdx + d + S.hits.length) % S.hits.length;
  $("#find-count").textContent = `${S.hitIdx + 1} of ${S.hits.length}`;
  $$(".page .hit.current").forEach(h => h.classList.remove("current"));
  const h = S.hits[S.hitIdx];
  const div = pagesEl.children[h.page];
  div?.querySelectorAll(`.hit[data-k="${S.hitIdx}"]`).forEach(n => n.classList.add("current"));
  if (!noScroll && div) {
    const y = div.offsetTop + h.rect[1] * S.zoom - viewer.clientHeight / 3;
    const x = div.offsetLeft + h.rect[0] * S.zoom - viewer.clientWidth / 3;
    viewer.scrollTo({ top: y, left: Math.max(0, x), behavior: "smooth" });
  }
  findResults.querySelector(".fr-hit.current")?.classList.remove("current");
  const row = findResults.querySelector(`.fr-hit[data-k="${S.hitIdx}"]`);
  if (row) { row.classList.add("current"); row.scrollIntoView({ block: "nearest" }); }
}
/** The matches listed by page: "Page 3 · 2 matches", then each with the words around it. */
function renderResults() {
  if (findResults.hidden) return;
  if (!S.query) { findResults.replaceChildren(el("p", { class: "fr-more", text: "Type what to find - the matches are listed here by page." })); return; }
  if (!S.hits.length) { findResults.replaceChildren(el("p", { class: "fr-more", text: "No matches." })); return; }
  const SHOW = 500, rows = [], perPage = new Map();
  for (const h of S.hits) perPage.set(h.page, (perPage.get(h.page) || 0) + 1);
  let page = -1;
  for (const [k, h] of S.hits.slice(0, SHOW).entries()) {
    if (h.page !== page) {
      page = h.page;
      const n = perPage.get(page);
      rows.push(el("div", { class: "fr-page", role: "presentation" }, `Page ${page + 1}`, el("span", { text: `${n} match${n === 1 ? "" : "es"}` })));
    }
    const sn = h.snippet;
    rows.push(el("button", { class: "fr-hit" + (k === S.hitIdx ? " current" : ""), role: "option", "data-k": k,
      onclick: () => { S.hitIdx = k - 1; stepHit(1); },
      onkeydown: e => {
        if (e.key === "ArrowDown" || e.key === "ArrowUp") {
          e.preventDefault();
          const all = $$(".fr-hit"), at = all.indexOf(e.currentTarget);
          all[at + (e.key === "ArrowDown" ? 1 : -1)]?.focus();
        }
        if (e.key === "Escape") { showResults(false); findInput.focus(); }
      } },
      ...(sn ? [sn.before, el("mark", { text: sn.match }), sn.after] : [`Match ${k + 1}`])));
  }
  if (S.hits.length > SHOW) rows.push(el("p", { class: "fr-more", text: `…and ${S.hits.length - SHOW} more. Add a word to narrow it down.` }));
  findResults.replaceChildren(...rows);
}

// ---- keyboard
document.addEventListener("keydown", e => {
  if (document.querySelector("dialog[open]") || !$("#menu").hidden) return;
  const ctrl = e.ctrlKey || e.metaKey, k = e.key.toLowerCase();
  const typing = /^(input|textarea|select)$/i.test(document.activeElement?.tagName || "");
  if (Tabs.key(e)) return;                        // Ctrl+T Home, Ctrl+W close, Ctrl+Tab, Ctrl+1-9 (tabs.js)
  if (ctrl && e.shiftKey && k === "o") { e.preventDefault(); return openLink(); }
  if (ctrl && k === "o") { e.preventDefault(); return S.view === "editor" ? openDialog() : $("#choose-file").click(); }
  if (S.view !== "editor") return;
  if (ctrl && k === "s") { e.preventDefault(); return save(e.shiftKey); }
  if (ctrl && k === "f") { e.preventDefault(); return openFind(); }
  if (ctrl && k === "p") { e.preventDefault(); return window.Share?.print(); }
  if (typing) return;
  if (window.AI?.key(e)) return;                  // copying from the Ask panel wins over a selection on the page
  if (window.Edit?.key(e)) return;
  if (ctrl && k === "z" && !e.shiftKey) { e.preventDefault(); return undo(); }
  if (ctrl && (k === "y" || (k === "z" && e.shiftKey))) { e.preventDefault(); return redo(); }
  if (ctrl && (k === "=" || k === "+")) { e.preventDefault(); return zoomStep(1); }
  if (ctrl && k === "-") { e.preventDefault(); return zoomStep(-1); }
  if (ctrl && k === "0") { e.preventDefault(); return setZoom(0, "auto"); }
  if (window.Reading?.key(e)) return;             // F focus mode, B bookmark this page, Esc leaves focus mode
  if (e.target.closest?.(".strip, .toolbar, .zoom")) return;   // let focused controls handle their own keys
  if (k === "pagedown" || (k === " " && !e.shiftKey)) { e.preventDefault(); return goTo(S.current + 1); }
  if (k === "pageup" || (k === " " && e.shiftKey)) { e.preventDefault(); return goTo(S.current - 1); }
  if (k === "home") { e.preventDefault(); return goTo(0); }
  if (k === "end") { e.preventDefault(); return goTo(S.doc.pages.length - 1); }
  if (k === "escape") closeFind();
});

// ================================================================== start
(async function start() {
  buildTasks();
  applyTheme("system");
  await bridgeReady();
  const r = await api.startup();
  if (fail(r)) return;
  Object.assign(APP, { name: r.name, ai: r.ai_name, openable: r.openable || [".pdf"], mac: !!r.mac });
  document.documentElement.classList.toggle("mac", APP.mac);
  if (APP.mac) {
    $$("[title*='Ctrl']").forEach(n => { n.title = keys(n.title); });   // toolbar tooltips
    $$("[data-theme-choice=system]").forEach(n => { n.title = "Match macOS"; });
  }
  $$(".app-name").forEach(n => { n.textContent = APP.name; });
  for (const t of TASKS) if (t.editor) t.exts = APP.openable;
  applyTheme(r.theme);
  applyLook(r.look);
  show("home");
  renderRecent(r.recent, r.carry_on ?? null);
  setTimeout(() => window.Panels?.checkUpdates?.(), 1500);
  if (r.start_files?.length) window.onFilesOpened(r.start_files);   // right-click "Open in <app>", Finder
})();
