/* Home-screen tasks: the task sheet (pick files -> options -> one button -> result) and the "More tools" list.
   Exposes window.TaskUI = { open(taskId, paths), openMoreTools() }.
   Uses app.js globals (shared classic-script scope): api, el, icon, $, $$, toast, fail, extOf, IMAGES. */
"use strict";

(function () {
  // ---------------------------------------------------------------- extra icons for the sprite
  const EXTRA_ICONS = {
    scissors: '<circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M20 4 8.1 15.9M14.5 14.5 20 20M8.1 8.1 12 12"/>',
    stamp: '<path d="M9 3h6l-1 7h-4z"/><path d="M5 14a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2v3H5z"/><path d="M5 21h14"/>',
    hash: '<path d="M4 9h16M4 15h16M10 3 8 21M16 3l-2 18"/>',
    qr: '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><path d="M14 14h3v3h-3zM20 14v.01M14 20h.01M17 20h4v-3"/>',
    table: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 10h18M3 15h18M9 4v16"/>',
    rename: '<path d="M4 7h11M4 12h7M4 17h5"/><path d="m14 19 6-6-2-2-6 6v2z"/>',
    folder: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    copies: '<rect x="8" y="8" width="13" height="13" rx="2"/><path d="M4 16V5a1 1 0 0 1 1-1h11"/>',
    zip: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5M10 5h1M10 8h1M10 11h1M10 14h1v3h-1z"/>',
    unzip: '<path d="M4 7a2 2 0 0 1 2-2h3l2 2h7a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2z"/><path d="M12 10v6M9.5 13.5 12 16l2.5-2.5"/>',
    film: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 4v16M17 4v16M3 9h4M3 15h4M17 9h4M17 15h4"/>',
    clip: '<path d="M4 6h16M4 18h16"/><path d="M8 6v12M16 6v12"/><path d="m11 10 3 2-3 2z"/>',
    type: '<path d="M4 6h16M4 12h10M4 18h13"/>',
  };
  const sprite = document.querySelector("body > svg");
  for (const [k, d] of Object.entries(EXTRA_ICONS)) {
    if (!document.getElementById("i-" + k)) sprite.insertAdjacentHTML("beforeend", `<symbol id="i-${k}" viewBox="0 0 24 24">${d}</symbol>`);
  }

  // ---------------------------------------------------------------- helpers
  const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
  const verbText = (verb, n) => n ? verb.replace("{n}", n).replace("{s}", n === 1 ? "" : "s")
    : verb.replace("{n} ", "").replace("{s}", "s");
  const fileIcon = f => f.is_dir ? "folder" : fileIconFor(f.name);
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const fill = (node, ...kids) => node.replaceChildren(...kids.filter(k => k != null && k !== false && k !== ""));

  let LIST = null;
  async function taskList() {
    if (!LIST) { const r = await api.task_list(); if (fail(r)) return null; LIST = r; }
    return LIST;
  }

  // ---------------------------------------------------------------- the sheet
  const sheet = el("dialog", { class: "dialog task-sheet", "aria-labelledby": "ts-title" });
  sheet.innerHTML = `
    <div class="ts-head">
      <span class="task-icon" id="ts-icon"></span>
      <div class="ts-titles"><h3 id="ts-title"></h3><p id="ts-desc" class="muted"></p></div>
      <button class="icon-btn sm ts-close" id="ts-close" aria-label="Close"><svg><use href="#i-x"/></svg></button>
    </div>
    <div class="ts-body" id="ts-body"></div>
    <div class="ts-foot" id="ts-foot"></div>`;
  document.body.append(sheet);
  const body = sheet.querySelector("#ts-body"), foot = sheet.querySelector("#ts-foot");

  /** cur = { spec, files, options, values, touched, addon, rejected, phase, job, result, preview } */
  let cur = null;
  let pollToken = 0;

  sheet.querySelector("#ts-close").addEventListener("click", () => close());
  sheet.addEventListener("cancel", e => {
    if (cur && (cur.phase === "running" || cur.installing)) { e.preventDefault(); toast("Still working - this will finish in a moment."); }
  });
  sheet.addEventListener("close", () => { pollToken++; if (cur && cur.phase !== "running") cur = null; });
  function close() {
    if (cur && (cur.phase === "running" || cur.installing)) return toast("Still working - this will finish in a moment.");
    sheet.close();
  }

  // dropping files on the window while the sheet is open adds them to the sheet
  let dragDepth = 0;
  const hasFiles = e => Array.from(e.dataTransfer?.types || []).includes("Files");
  sheet.addEventListener("dragenter", e => { if (hasFiles(e) && acceptsFiles()) { dragDepth++; sheet.classList.add("drop-over"); } });
  sheet.addEventListener("dragleave", e => { if (hasFiles(e) && --dragDepth <= 0) { dragDepth = 0; sheet.classList.remove("drop-over"); } });
  sheet.addEventListener("drop", () => { dragDepth = 0; sheet.classList.remove("drop-over"); });
  const acceptsFiles = () => cur && cur.phase === "setup" && ["files", "folders"].includes(cur.spec.pick);
  const prevDrop = window.onFilesDropped;
  window.onFilesDropped = async paths => {
    if (sheet.open && acceptsFiles()) { $("#drag-overlay").hidden = true; return addPaths(paths); }
    if (sheet.open) return;
    if (more.open) more.close();
    return prevDrop ? prevDrop(paths) : undefined;
  };

  async function open(taskId, paths = []) {
    if (cur && cur.phase === "running" && sheet.open) return toast("Still working - this will finish in a moment.");
    const r = await api.task_prepare(taskId, paths);
    if (fail(r)) return;
    cur = { spec: r.task, phase: "setup", values: {}, touched: new Set(), files: [], options: [] };
    adopt(r);
    header();
    render();
    if (!sheet.open) sheet.showModal();
    focusFirst();
  }

  function adopt(r) {
    cur.files = r.files;
    cur.rejected = r.rejected;
    cur.addon = r.addon;
    cur.options = r.options;
    for (const o of r.options) if (!cur.touched.has(o.key)) cur.values[o.key] = o.default;
  }

  async function addPaths(paths) {
    const r = await api.task_prepare(cur.spec.id, [...cur.files.map(f => f.path), ...paths]);
    if (fail(r)) return;
    adopt(r);
    render();
  }

  function header() {
    const s = cur.spec;
    const ic = sheet.querySelector("#ts-icon");
    ic.className = `task-icon tint-${s.tint}`;
    ic.replaceChildren(icon(s.icon));
    sheet.querySelector("#ts-title").textContent = s.title;
    sheet.querySelector("#ts-desc").textContent = s.desc;
  }

  function focusFirst() {
    requestAnimationFrame(() => (body.querySelector("input:not([type=checkbox]), select, textarea")
      || foot.querySelector(".btn-primary:not([disabled])") || body.querySelector("button"))?.focus());
  }

  function render() {
    const phase = cur.phase;
    if (cur.spec.pick === "text") return renderText();
    if (phase === "setup") renderSetup();
    else if (phase === "running") renderRunning();
    else if (phase === "done") renderDone();
    else if (phase === "error") renderError();
  }

  // ---------------------------------------------------------------- setup
  function renderSetup() {
    const s = cur.spec, kids = [];
    if (cur.addon && !cur.addon.installed) kids.push(addonBox());
    if (s.pick === "files" || s.pick === "folders") kids.push(filesBlock());
    if (cur.rejected) kids.push(el("p", { class: "ts-note", role: "status" }, icon("alert"), el("span", { text: cur.rejected })));
    const shown = cur.options.filter(visible);
    const main = shown.filter(o => !o.advanced), adv = shown.filter(o => o.advanced);
    if (main.length) kids.push(el("div", { class: "ts-fields" }, ...main.map(field)));
    if (adv.length) {
      const det = el("details", { class: "ts-more" }, el("summary", {}, icon("down"), "More options"),
        el("div", { class: "ts-fields" }, ...adv.map(field)));
      if (adv.some(o => cur.touched.has(o.key) && cur.values[o.key] !== o.default)) det.open = true;
      kids.push(det);
    }
    if (s.id === "rename") kids.push(el("div", { class: "ts-preview", id: "ts-preview" }));
    fill(body, ...kids);
    if (s.id === "rename") refreshPreview();
    renderSetupFoot();
  }

  function filesBlock() {
    const s = cur.spec, n = cur.files.length, folders = s.pick === "folders";
    const addBtn = el("button", { class: "btn btn-ghost sm-btn", onclick: () => pick() }, icon("plus"), folders ? "Add a folder…" : "Add files…");
    // a task that takes folders as well as files (Remove personal details from many PDFs)
    const addFolder = s.folders_too ? el("button", { class: "btn btn-ghost sm-btn", onclick: () => pick(true) }, icon("folder"), "Add a folder…") : null;
    if (!n) {
      return el("div", { class: "ts-empty" },
        el("span", { class: "ts-empty-icon" }, icon(folders ? "folder" : "drop")),
        el("strong", { text: folders ? "Choose the folders to look in" : `Choose the ${s.accepts} to use` }),
        el("span", { class: "muted", text: "or drop them anywhere on this window" }),
        el("div", { class: "ts-empty-actions" },
          el("button", { class: cur.addon && !cur.addon.installed ? "btn btn-outline" : "btn btn-primary", onclick: () => pick() },
            icon("open"), folders ? "Choose a folder…" : "Choose files…"),
          s.folders_too ? el("button", { class: "btn btn-outline", onclick: () => pick(true) }, icon("folder"), "Choose a folder…") : null));
    }
    const list = el("ul", { class: "ts-files", "aria-label": "Files" });
    cur.files.forEach((f, i) => list.append(fileRow(f, i)));
    const dest = s.folders_too ? (cur.values.save_to ? `The copies go in a new folder in ${cur.values.save_to} - the originals aren't changed.`
        : "The copies go in a new folder beside the originals - the originals aren't changed.")
      : cur.values.save_to ? `Results are saved in ${cur.values.save_to}.`
      : s.id === "rename" || s.id === "duplicates" ? "" : "Results are saved next to the original files - nothing is overwritten.";
    return el("div", { class: "ts-files-block" },
      el("div", { class: "ts-section-h" },
        el("span", { text: folders ? plural(n, "folder") : s.reorder && n > 1 ? `${plural(n, "file")}, in this order`
          : s.folders_too ? [cur.files.filter(f => !f.is_dir).length ? plural(cur.files.filter(f => !f.is_dir).length, "file") : "",
            cur.files.filter(f => f.is_dir).length ? plural(cur.files.filter(f => f.is_dir).length, "folder") : ""].filter(Boolean).join(" and ")
          : plural(n, "file") }),
        el("span", { class: "ts-add-btns" }, addBtn, addFolder)),
      list,
      dest ? el("p", { class: "ts-dest", text: dest }) : null);
  }

  function fileRow(f, i) {
    const s = cur.spec, n = cur.files.length;
    const li = el("li", { class: "ts-file", draggable: s.reorder ? "true" : null, "data-i": i },
      icon(fileIcon(f)),
      el("span", { class: "ts-fname", title: f.path, text: f.name }),
      el("span", { class: "ts-fsize", text: f.size }),
      s.reorder ? el("button", { class: "icon-btn sm", "aria-label": `Move ${f.name} up`, disabled: i === 0 || null, onclick: () => move(i, i - 1) }, icon("up")) : null,
      s.reorder ? el("button", { class: "icon-btn sm", "aria-label": `Move ${f.name} down`, disabled: i === n - 1 || null, onclick: () => move(i, i + 1) }, icon("down")) : null,
      el("button", { class: "icon-btn sm", "aria-label": `Remove ${f.name}`, title: "Remove", onclick: () => removeFile(i) }, icon("x")));
    if (s.reorder) {
      li.addEventListener("dragstart", e => { e.dataTransfer.setData("text/x-toolkit-file", String(i)); e.dataTransfer.effectAllowed = "move"; li.classList.add("dragging"); });
      li.addEventListener("dragend", () => li.classList.remove("dragging"));
      li.addEventListener("dragover", e => {
        if (!Array.from(e.dataTransfer.types).includes("text/x-toolkit-file")) return;
        e.preventDefault();
        const r = li.getBoundingClientRect(), after = e.clientY > r.top + r.height / 2;
        li.classList.toggle("drop-before", !after); li.classList.toggle("drop-after", after);
      });
      li.addEventListener("dragleave", () => li.classList.remove("drop-before", "drop-after"));
      li.addEventListener("drop", e => {
        const from = +e.dataTransfer.getData("text/x-toolkit-file");
        if (Number.isNaN(from) || !Array.from(e.dataTransfer.types).includes("text/x-toolkit-file")) return;
        e.preventDefault(); e.stopPropagation();
        let to = i + (li.classList.contains("drop-after") ? 1 : 0);
        li.classList.remove("drop-before", "drop-after");
        if (to > from) to -= 1;
        move(from, to);
      });
    }
    return li;
  }

  function move(from, to) {
    if (to < 0 || to >= cur.files.length || from === to) return;
    const [f] = cur.files.splice(from, 1);
    cur.files.splice(to, 0, f);
    render();
    body.querySelectorAll(".ts-file")[to]?.querySelector(`[aria-label^="Move"]:not([disabled])`)?.focus();
  }
  function removeFile(i) {
    cur.files.splice(i, 1);
    addPaths([]);   // re-check (names, defaults) with what's left
  }
  async function pick(folder = false) {
    const r = await api.task_pick(cur.spec.id, folder);
    if (fail(r) || !r.paths?.length) return;
    addPaths(r.paths);
  }

  /** options with when = {key: [values]} only show while that other option has one of those values */
  const visible = o => !o.when || Object.entries(o.when).every(([k, vals]) => vals.includes(cur.values[k]));

  function field(o) {
    const id = "ts-opt-" + o.key, v = cur.values[o.key];
    const set = val => { cur.values[o.key] = val; cur.touched.add(o.key); onChange(o.key); };
    let control;
    if (o.kind === "check") {
      const box = el("label", { class: "ts-check" + (o.hint ? " has-hint" : "") },
        el("input", { type: "checkbox", id, checked: !!v || null, onchange: e => set(e.target.checked) }),
        el("span", { class: "ts-check-text" }, el("span", { text: o.label }), o.hint ? el("small", { text: o.hint }) : null));
      return o.section ? el("div", { class: "ts-check-group" }, el("div", { class: "ts-section-h" }, el("span", { text: o.section })), box) : box;
    }
    if (o.kind === "choice") {
      control = el("select", { id, onchange: e => set(e.target.value) },
        ...o.choices.map(c => el("option", { value: c, text: c, selected: c === v || null })));
    } else if (o.kind === "lines") {                 // several lines of text (Black out words)
      control = el("textarea", { id, class: "ts-lines", rows: 4, placeholder: o.placeholder || null, spellcheck: "false",
        oninput: e => set(e.target.value) });
      control.value = v ?? "";
    } else if (o.kind === "folder") {
      control = el("div", { class: "ts-path-row" },
        el("span", { class: "ts-path" + (v ? "" : " muted"), title: v || "", text: v || o.placeholder || "Not chosen" }),
        el("button", { class: "btn btn-outline sm-btn", onclick: async () => {
          const r = await api.pick_folder();
          if (!fail(r) && r.path) { set(r.path); render(); }
        } }, "Choose…"),
        v && o.key === "save_to" ? el("button", { class: "btn btn-ghost sm-btn", onclick: () => { set(""); render(); } }, "Reset") : null);
      return el("div", { class: "field" }, el("span", { text: o.label }), control, o.hint ? el("small", { text: o.hint }) : null);
    } else {
      control = el("input", {
        id, type: o.kind === "int" ? "number" : o.kind === "password" ? "password" : "text",
        value: v ?? "", placeholder: o.placeholder || null, autocomplete: "off", min: o.kind === "int" ? "0" : null,
        oninput: e => set(o.kind === "int" ? (e.target.value === "" ? "" : Number(e.target.value)) : e.target.value),
        onkeydown: e => { if (e.key === "Enter") { e.preventDefault(); foot.querySelector(".btn-primary:not([disabled])")?.click(); } },
      });
    }
    return el("label", { class: "field", for: id }, el("span", { text: o.label }), control, o.hint ? el("small", { text: o.hint }) : null);
  }

  let previewTimer = 0;
  function onChange(key) {
    if (cur.spec.id === "rename") { clearTimeout(previewTimer); previewTimer = setTimeout(refreshPreview, 150); }
    if (cur.options.some(o => o.when && key in o.when)) {
      const open = body.querySelector("details.ts-more")?.open;
      render();
      if (open) body.querySelector("details.ts-more").open = true;
      return body.querySelector("#ts-opt-" + key)?.focus();
    }
    renderSetupFoot();
  }

  async function refreshPreview() {
    const box = body.querySelector("#ts-preview");
    if (!box) return;
    if (!cur.files.length) { cur.preview = null; fill(box, ); return renderSetupFoot(); }
    const r = await api.rename_preview(cur.files.map(f => f.path), cur.values);
    if (r.error) { fill(box, el("p", { class: "ts-note", text: r.error })); return; }
    cur.preview = r;
    const rows = r.rows.slice(0, 300);
    fill(box, 
      el("div", { class: "ts-section-h" }, el("span", { text: "Preview" }),
        el("span", { class: "muted", text: r.changes ? `${plural(r.changes, "file")} will be renamed` : "Nothing would change yet" })),
      el("div", { class: "ts-table-wrap" }, el("table", { class: "ts-table" },
        el("thead", {}, el("tr", {}, el("th", { text: "Now" }), el("th", { text: "Will be" }))),
        el("tbody", {}, ...rows.map(row => el("tr", { class: row.clash ? "clash" : row.changed ? "changed" : "" },
          el("td", { text: row.old }),
          el("td", {}, row.clash ? el("span", { text: `${row.new} - name taken, skipped` }) : row.new)))))));
    renderSetupFoot();
  }

  function readiness() {
    const s = cur.spec, n = cur.files.length, v = cur.values;
    if (cur.addon && !cur.addon.installed) return { ok: false, note: `Install ${cur.addon.name} first.` };
    if ((s.pick === "files" || s.pick === "folders") && n < s.min_files) {
      return { ok: false, note: n ? `Add ${s.min_files - n === 1 ? "one more" : s.min_files - n + " more"} to continue.` : "" };
    }
    if (s.id === "qr" && !String(v.text || "").trim()) return { ok: false, note: "Type a web address or some text." };
    if (s.id === "split" && String(v.mode).startsWith("Just") && !String(v.ranges || "").trim()) return { ok: false, note: "Type which pages to save." };
    if (s.id === "media_trim" && !String(v.start || "").trim() && !String(v.end || "").trim()) return { ok: false, note: "Type a start or end time." };
    if (s.id === "rename" && cur.preview && !cur.preview.changes) return { ok: false, note: "" };
    if (s.id === "tidy" && !String(v.folder || "").trim()) return { ok: false, note: "Choose a folder." };
    if (s.id === "redact_words" && !String(v.words || "").trim()) return { ok: false, note: "Type the words to black out." };
    return { ok: true, note: "" };
  }

  function primaryLabel() {
    const s = cur.spec, v = cur.values;
    if (s.id === "tidy") return v.dry_run ? "Show what would move" : "Tidy the folder";
    if (s.id === "rename") return verbText(s.verb, cur.preview ? cur.preview.changes : cur.files.length);
    return verbText(s.verb, s.pick === "none" ? 1 : cur.files.length);
  }

  function renderSetupFoot() {
    if (cur.phase !== "setup") return;
    const r = readiness();
    fill(foot, 
      el("span", { class: "ts-foot-note", text: r.note }),
      el("button", { class: "btn btn-ghost", onclick: close }, "Cancel"),
      el("button", { class: "btn btn-primary", disabled: !r.ok || null, onclick: run }, primaryLabel()));
  }

  // ---------------------------------------------------------------- add-on
  function addonBox() {
    const a = cur.addon;
    const prog = el("p", { class: "ts-addon-progress", hidden: true });
    const installBtn = el("button", { class: "btn btn-primary sm-btn" }, icon("export"), `Install ${a.name}`);
    const checkBtn = el("button", { class: "btn btn-ghost sm-btn" }, "Check again");
    const box = el("div", { class: "ts-addon", role: "group", "aria-label": "Add-on needed" },
      el("span", { class: "ts-addon-icon" }, icon("export")),
      el("div", { class: "ts-addon-text" },
        el("strong", { text: `Needs a free add-on: ${a.name}` }),
        el("span", { text: `${a.why} One-time download, ${a.size}.` }),
        el("div", { class: "ts-addon-actions" }, a.can_install ? installBtn : el("span", { class: "muted", text: `Download it from ${a.url}` }), checkBtn),
        prog));
    checkBtn.addEventListener("click", async () => { await addPaths([]); if (cur.addon?.installed) toast(`${a.name} is ready.`); });
    installBtn.addEventListener("click", async () => {
      installBtn.disabled = true; checkBtn.disabled = true;
      prog.hidden = false; prog.className = "ts-addon-progress"; prog.textContent = `Starting the ${a.name} download…`;
      const r = await api.addon_install(a.key);
      if (r.installed) return addPaths([]);
      if (r.error) { prog.textContent = r.error; prog.classList.add("error"); installBtn.disabled = checkBtn.disabled = false; return; }
      cur.installing = true;
      const token = ++pollToken;
      while (token === pollToken) {
        await sleep(800);
        const st = await api.task_status(r.job);
        if (!st.state) { prog.textContent = st.error; prog.classList.add("error"); break; }
        prog.textContent = st.progress;
        if (st.state === "done") { cur.installing = false; toast(`${a.name} is installed.`); return addPaths([]); }
        if (st.state === "error") { prog.textContent = st.error; prog.classList.add("error"); break; }
      }
      cur.installing = false;
      installBtn.disabled = checkBtn.disabled = false;
    });
    return box;
  }

  // ---------------------------------------------------------------- run
  async function run(extra = {}) {
    const values = { ...cur.values, ...extra };
    cur.lastValues = values;
    const r = await api.task_run(cur.spec.id, cur.files.map(f => f.path), values);
    if (fail(r)) return;
    cur.phase = "running"; cur.job = r.job; cur.progress = "Starting…"; cur.stopping = false;
    render();
    const token = ++pollToken;
    while (token === pollToken) {
      await sleep(300);
      const st = await api.task_status(r.job);
      if (token !== pollToken || !cur) return;
      if (!st.state) { cur.phase = "error"; cur.result = { error: st.error, log: [] }; break; }
      if (st.state === "running") { setProgress(st.progress); continue; }
      cur.result = st;
      cur.phase = st.state === "done" ? "done" : "error";
      break;
    }
    render();
    foot.querySelector(".btn-primary")?.focus();
  }

  function setProgress(text) {
    cur.progress = text;
    const p = body.querySelector(".ts-progress");
    if (p) p.textContent = text;
  }

  function renderRunning() {
    fill(body, el("div", { class: "ts-running" },
      el("div", { class: "ts-bar", role: "progressbar", "aria-label": "Working" }, el("span")),
      el("p", { class: "ts-progress", role: "status", "aria-live": "polite", text: cur.progress || "Working…" })));
    const stop = cur.spec.stoppable ? el("button", { class: "btn btn-ghost", disabled: cur.stopping || null, onclick: async e => {
      cur.stopping = true; e.currentTarget.disabled = true; e.currentTarget.textContent = "Stopping…";
      await api.task_stop(cur.job);
    } }, cur.stopping ? "Stopping…" : "Stop") : null;
    fill(foot, el("span", { class: "ts-foot-note", text: cur.spec.stoppable ? "Stop keeps what is done so far." : "You can keep this window open while it works." }),
      stop, el("button", { class: "btn btn-primary", disabled: true }, "Working…"));
  }

  // ---------------------------------------------------------------- result
  function outputRow(o) {
    return el("li", { class: "ts-out" },
      icon(o.is_dir ? "folder" : fileIcon(o)),
      el("span", { class: "ts-out-text" },
        el("span", { class: "ts-fname", title: o.path, text: o.name }),
        el("span", { class: "ts-out-meta", text: [o.note || o.size, o.folder].filter(Boolean).join(" · ") })),
      el("button", { class: "btn btn-outline sm-btn", onclick: () => api.open_external(o.path) }, o.is_dir ? "Open folder" : "Open"),
      o.is_dir ? null : el("button", { class: "btn btn-ghost sm-btn", onclick: () => api.show_in_folder(o.path) }, "Show in folder"));
  }

  function logBlock(lines, open = false) {
    if (!lines?.length) return null;
    const pre = el("pre", { class: "ts-log", text: lines.join("\n") });
    return open ? pre : el("details", { class: "ts-more ts-details" }, el("summary", {}, icon("down"), "Details"), pre);
  }

  function renderDone() {
    const s = cur.spec, r = cur.result, outs = r.outputs || [];
    const preview = s.id === "tidy" && cur.lastValues?.dry_run;
    let title;
    if (preview) title = "Here's what would move - nothing has moved yet.";
    else if (s.id === "rename") title = outs.length ? `Renamed ${plural(outs.length, "file")}.` : "Nothing needed renaming.";
    else if (s.id === "tidy") title = outs.length ? `Moved ${plural(outs.length, "file")} into folders.` : "Nothing needed moving.";
    else if (s.id === "duplicates") title = (r.log || []).slice(-2).join(" ").split(". Report:")[0] + ".";
    else title = outs.length === 1 ? "Your file is ready." : outs.length ? `${outs.length} files are ready.` : "Finished, but nothing was made.";
    if (r.extra?.summary) return renderReport();
    const showList = outs.length && !(s.id === "tidy" || s.id === "rename");
    fill(body, 
      el("div", { class: "ts-done-head" }, el("span", { class: "ts-done-icon" }, icon("check")), el("strong", { text: title })),
      showList ? el("ul", { class: "ts-outs" }, ...outs.slice(0, 50).map(outputRow)) : null,
      showList && s.id !== "duplicates" ? el("p", { class: "ts-dest", text: "Your original files haven't been changed." }) : null,
      (preview || !showList) ? logBlock(r.log, true) : logBlock(r.log));
    const actions = [];
    if (preview) {
      actions.push(el("button", { class: "btn btn-ghost", onclick: close }, "Close"),
        el("button", { class: "btn btn-primary", onclick: () => run({ dry_run: false }) }, "Tidy it for real"));
    } else {
      actions.push(el("button", { class: "btn btn-ghost", onclick: again }, s.pick === "none" ? "Do another" : "Start again"),
        el("button", { class: "btn btn-primary", onclick: close }, "Done"));
    }
    fill(foot, el("span", { class: "ts-foot-note" }), ...actions);
  }

  /** Remove personal details from many PDFs: what was done (counts, never the details), then each file. */
  function renderReport() {
    const r = cur.result, x = r.extra, outs = r.outputs || [];
    fill(body,
      el("div", { class: "ts-done-head" }, el("span", { class: "ts-done-icon" }, icon("check")), el("strong", { text: x.summary })),
      x.names ? el("p", { class: "ts-note" }, icon("alert"), el("span", { text: x.names })) : null,
      el("ul", { class: "ts-report", "aria-label": "Each file" }, ...x.rows.slice(0, 500).map(row => el("li", { text: row.line }))),
      el("ul", { class: "ts-outs" }, ...outs.map(outputRow)),
      el("p", { class: "ts-dest", text: "Your original files haven't been changed." }));
    fill(foot, el("span", { class: "ts-foot-note" }),
      el("button", { class: "btn btn-ghost", onclick: again }, "Start again"),
      el("button", { class: "btn btn-primary", onclick: close }, "Done"));
  }

  function renderError() {
    const r = cur.result || {};
    fill(body, 
      el("div", { class: "ts-error", role: "alert" }, icon("alert"), el("span", { text: r.error || "That didn't work." })),
      logBlock(r.log));
    fill(foot, el("span", { class: "ts-foot-note" }),
      el("button", { class: "btn btn-ghost", onclick: close }, "Close"),
      el("button", { class: "btn btn-primary", onclick: () => { cur.phase = "setup"; render(); focusFirst(); } }, "Back"));
  }

  function again() {
    cur.phase = "setup"; cur.result = null;
    if (cur.spec.pick !== "none") { cur.files = []; cur.rejected = null; }
    addPaths([]).then(focusFirst);
  }

  // ---------------------------------------------------------------- text cleaner
  function renderText() {
    const o = cur.options[0];
    const out = el("textarea", { class: "ts-text", id: "ts-text-out", readonly: true, rows: 6, "aria-label": "Result" });
    const input = el("textarea", { class: "ts-text", id: "ts-text-in", rows: 6, placeholder: "Paste or type text here", "aria-label": "Your text" });
    const mode = el("select", { id: "ts-text-mode" }, ...o.choices.map(c => el("option", { value: c, text: c, selected: c === cur.values.mode || null })));
    let t = 0;
    const update = () => {
      clearTimeout(t);
      t = setTimeout(async () => {
        const r = await api.text_transform(input.value, mode.value);
        out.value = r.error ? "" : r.text;
      }, 120);
    };
    input.addEventListener("input", update);
    mode.addEventListener("change", () => { cur.values.mode = mode.value; update(); });
    fill(body, 
      el("label", { class: "field" }, el("span", { text: "Your text" }), input),
      el("label", { class: "field" }, el("span", { text: o.label }), mode),
      el("label", { class: "field" }, el("span", { text: "Result" }), out));
    fill(foot, el("span", { class: "ts-foot-note" }),
      el("button", { class: "btn btn-ghost", onclick: close }, "Close"),
      el("button", { class: "btn btn-primary", onclick: () => copy(out.value) }, icon("copy"), "Copy result"));
  }

  async function copy(text) {
    if (!text) return toast("There's nothing to copy yet.", { error: true });
    try { await navigator.clipboard.writeText(text); }
    catch {
      const ta = el("textarea", { style: "position:fixed;opacity:0" }); ta.value = text; document.body.append(ta);
      ta.select(); document.execCommand("copy"); ta.remove();
    }
    toast("Copied - paste it wherever you need it.");
  }

  // ---------------------------------------------------------------- more tools
  const more = el("dialog", { class: "dialog more-sheet", "aria-labelledby": "mt-title" });
  more.innerHTML = `
    <div class="ts-head">
      <div class="ts-titles"><h3 id="mt-title">More tools</h3><p class="muted">Everything else <span class="app-name"></span> can do. Each one saves a new file - your originals stay as they are.</p></div>
      <button class="icon-btn sm ts-close" aria-label="Close"><svg><use href="#i-x"/></svg></button>
    </div>
    <div class="ts-body mt-body" id="mt-body"></div>`;
  document.body.append(more);
  more.querySelector(".ts-close").addEventListener("click", () => more.close());

  async function openMoreTools() {
    const l = await taskList();
    if (!l) return;
    const groups = l.groups.map(g => [g, l.tasks.filter(t => t.group === g && !t.home)]).filter(([, ts]) => ts.length);
    fill(more.querySelector("#mt-body"), ...groups.map(([g, ts]) => el("section", { class: "mt-group" },
      el("h4", { text: g }),
      el("div", { class: "mt-grid" }, ...ts.map(t => el("button", {
        class: "mt-card", onclick: () => { more.close(); open(t.id, []); },
      },
        el("span", { class: `task-icon tint-${t.tint}` }, icon(t.icon)),
        el("span", { class: "mt-text" }, el("span", { class: "mt-title", text: t.title }), el("span", { class: "mt-desc", text: t.desc }))))))));
    more.showModal();
    more.querySelector(".mt-card")?.focus();
  }

  window.TaskUI = { open, openMoreTools };
})();
