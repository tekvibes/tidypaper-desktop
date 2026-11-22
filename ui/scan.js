/* Scan a paper document -> PDF (the PC's scanner or the phone's camera).  window.ScanUI = { open() }
   Uses app.js globals: api, el, icon, $, $$, toast, fail, setStatus, openPath. */
"use strict";
(() => {
  // icons this sheet needs that the main sprite doesn't have
  document.querySelector("svg[style*='display:none']")?.insertAdjacentHTML("beforeend", `
    <symbol id="i-phone" viewBox="0 0 24 24"><rect x="6" y="2" width="12" height="20" rx="2.5"/><path d="M11 18h2"/></symbol>
    <symbol id="i-scanner" viewBox="0 0 24 24"><path d="M3 13h18v5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><path d="M5 13 18 5"/><path d="M7 17h2"/></symbol>
    <symbol id="i-left" viewBox="0 0 24 24"><path d="m15 18-6-6 6-6"/></symbol>`);

  const S2 = { dlg: null, poll: 0, pages: [], thumbs: new Map(), mode: null, paper: "a4", folder: "", busy: false,
    result: null, phone: null, dragFrom: null };

  function sheet() {
    if (S2.dlg) return S2.dlg;
    const d = el("dialog", { class: "dialog scan-sheet", "aria-labelledby": "scan-title" });
    d.innerHTML = `
      <div class="td-head">
        <span class="dlg-icon"><svg><use href="#i-scan"/></svg></span>
        <div class="sd-titles"><h3 id="scan-title">Scan a paper document</h3>
          <span class="muted scan-sub">Turn paper pages into one PDF</span></div>
        <button class="icon-btn sm td-x scan-x" aria-label="Close"><svg><use href="#i-x"/></svg></button>
      </div>

      <section class="scan-step" data-step="choose">
        <div class="scan-choices">
          <button class="scan-choice win-only" data-mode="scanner">
            <span class="scan-choice-icon"><svg><use href="#i-scanner"/></svg></span>
            <strong>Use my scanner</strong>
            <span class="muted">A scanner or printer-scanner connected to this PC</span>
          </button>
          <button class="scan-choice" data-mode="phone">
            <span class="scan-choice-icon"><svg><use href="#i-phone"/></svg></span>
            <strong>Use my phone's camera</strong>
            <span class="muted">Take photos of the pages - they arrive here over your Wi-Fi</span>
          </button>
        </div>
      </section>

      <section class="scan-step" data-step="pages" hidden>
        <div class="scan-source">
          <div class="scan-phone" hidden>
            <img class="scan-qr" alt="QR code to open on your phone">
            <div class="scan-phone-text">
              <strong>On your phone</strong>
              <ol>
                <li>Open the camera and point it at this code.</li>
                <li>Tap the link that appears, then <b>Take a photo</b>.</li>
                <li>Each photo shows up here. Take one per page.</li>
              </ol>
              <p class="muted small">Your phone must be on the same Wi-Fi as this ${APP.mac ? "Mac" : "PC"}. Or type this into its browser:
                <span class="scan-addr"></span></p>
              <p class="muted small scan-fw">${APP.mac ? `If your Mac asks whether ${APP.name} may accept incoming network connections, choose <b>Allow</b>.`
                : `The first time, Windows may ask whether to allow ${APP.name} on private networks - choose <b>Allow</b>.`}</p>
            </div>
          </div>
          <div class="scan-device" hidden>
            <button class="btn btn-primary scan-go"><svg><use href="#i-scanner"/></svg><span>Scan a page</span></button>
            <p class="muted small scan-device-hint">Put the page face down on the glass, then press Scan a page. Windows' scanner window opens - press Scan there.</p>
          </div>
        </div>
        <p class="scan-msg" role="status" aria-live="polite"></p>
        <div class="scan-tray-wrap">
          <ol class="scan-tray" aria-label="Pages"></ol>
          <p class="scan-empty muted">Pages appear here as you scan them.</p>
        </div>
        <div class="scan-opts">
          <label class="check"><input type="checkbox" class="scan-clean" checked><span>Clean up like a scan <span class="muted">- crop to the paper, black text on white</span></span></label>
          <details class="scan-more"><summary>More options</summary>
            <div class="scan-more-body">
              <label class="field"><span>Page size</span>
                <select class="scan-paper">
                  <option value="a4">A4</option><option value="letter">Letter</option><option value="image">Same as the photo</option>
                </select></label>
              <div class="field"><span>Save to</span>
                <div class="scan-folder-row"><span class="scan-folder muted"></span>
                  <button class="btn btn-ghost sm scan-folder-btn">Change…</button></div></div>
            </div>
          </details>
        </div>
        <div class="dlg-actions">
          <div class="scan-add-wrap">
            <button class="btn btn-ghost scan-add" aria-haspopup="menu" aria-expanded="false"><svg><use href="#i-plus"/></svg><span>Add another page</span></button>
            <div class="scan-add-menu" role="menu" hidden>
              <button role="menuitem" class="win-only" data-add="scanner"><svg><use href="#i-scanner"/></svg>Scan a page with my scanner</button>
              <button role="menuitem" data-add="phone"><svg><use href="#i-phone"/></svg><span>Take photos with my phone</span></button>
            </div>
          </div>
          <span class="spacer"></span>
          <button class="btn btn-ghost scan-cancel">Cancel</button>
          <button class="btn btn-primary scan-make" disabled>Make PDF</button>
        </div>
        <div class="scan-discard" hidden>
          <span>Discard the scanned pages?</span>
          <span class="spacer"></span>
          <button class="btn btn-ghost sm scan-keep">Keep scanning</button>
          <button class="btn btn-outline sm scan-discard-yes">Discard</button>
        </div>
      </section>

      <section class="scan-step" data-step="done" hidden>
        <div class="scan-done">
          <span class="dlg-icon ok"><svg><use href="#i-check"/></svg></span>
          <div><strong class="scan-done-name"></strong><p class="muted scan-done-where"></p></div>
        </div>
        <div class="scan-done-actions">
          <button class="btn btn-primary scan-open"><svg><use href="#i-edittext"/></svg>Open in the editor</button>
          <button class="btn btn-outline scan-ocr"><svg><use href="#i-scan"/></svg>Pull the text out</button>
          <button class="btn btn-ghost scan-show"><svg><use href="#i-folder-find"/></svg>Show in folder</button>
        </div>
        <div class="dlg-actions">
          <button class="btn btn-ghost scan-again">Scan another document</button>
          <span class="spacer"></span>
          <button class="btn btn-primary scan-close">Close</button>
        </div>
      </section>`;
    document.body.append(d);
    const q = s => d.querySelector(s);
    d.querySelectorAll(".scan-choice").forEach(b => b.addEventListener("click", () => choose(b.dataset.mode)));
    q(".scan-x").onclick = () => tryClose();
    q(".scan-cancel").onclick = () => tryClose();
    q(".scan-close").onclick = () => close();
    q(".scan-keep").onclick = () => { q(".scan-discard").hidden = true; q(".scan-make").focus(); };
    q(".scan-discard-yes").onclick = () => close();
    q(".scan-go").onclick = () => scanPage();
    q(".scan-add").onclick = e => addMenu(e.currentTarget);
    d.querySelectorAll("[data-add]").forEach(b => { b.onclick = () => addFrom(b.dataset.add); });
    q(".scan-add-menu").addEventListener("keydown", e => {
      const items = [...d.querySelectorAll("[data-add]")], k = items.indexOf(document.activeElement);
      if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); items[(k + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length].focus(); }
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); closeAddMenu(); q(".scan-add").focus(); }
    });
    d.addEventListener("pointerdown", e => { if (!e.target.closest(".scan-add-wrap")) closeAddMenu(); });
    q(".scan-make").onclick = () => makePdf();
    q(".scan-paper").onchange = e => { S2.paper = e.target.value; };
    q(".scan-folder-btn").onclick = async () => {
      const r = await api.pick_folder();
      if (fail(r) || !r.path) return;
      S2.folder = r.path; q(".scan-folder").textContent = r.path;
    };
    q(".scan-open").onclick = () => { const p = S2.result.path; close(); openPath(p); };
    q(".scan-ocr").onclick = () => { const p = S2.result.path; close(); window.TaskUI?.open("ocr", [p]); };
    q(".scan-show").onclick = () => api.show_in_folder(S2.result.path);
    q(".scan-again").onclick = () => start();
    d.addEventListener("cancel", e => {                                       // Esc
      e.preventDefault();
      if (!q(".scan-add-menu").hidden) { closeAddMenu(); q(".scan-add").focus(); return; }
      tryClose();
    });
    S2.dlg = d;
    return d;
  }
  const q = s => S2.dlg.querySelector(s);

  function step(name) {
    S2.dlg.querySelectorAll(".scan-step").forEach(s => { s.hidden = s.dataset.step !== name; });
    q(".scan-sub").textContent = name === "done" ? "Done" : S2.mode === "phone" ? "Using your phone's camera"
      : S2.mode === "scanner" ? "Using your scanner" : "Turn paper pages into one PDF";
  }

  async function start() {
    const r = await api.scan_start();
    if (fail(r)) return false;
    S2.pages = []; S2.thumbs.clear(); S2.mode = null; S2.result = null; S2.phone = null; S2.busy = false;
    S2.paper = r.paper; S2.folder = r.folder;
    q(".scan-paper").value = r.paper;
    q(".scan-folder").textContent = r.folder;
    q(".scan-clean").checked = true;
    q(".scan-discard").hidden = true;
    msg("");
    renderTray();
    step("choose");
    S2.dlg.querySelector(".scan-choice")?.focus();
    return true;
  }

  async function open() {
    const d = sheet();
    if (!d.open) d.showModal();
    if (!(await start())) { d.close(); return; }
    clearInterval(S2.poll);
    S2.poll = setInterval(poll, 800);
  }

  function msg(text, kind = "") {
    const m = q(".scan-msg");
    m.textContent = text || "";
    m.className = "scan-msg " + kind;
  }

  async function choose(mode) {
    S2.mode = mode;
    step("pages");
    q(".scan-phone").hidden = mode !== "phone";
    q(".scan-device").hidden = mode !== "scanner";
    if (mode === "phone") await startPhone();
    else { q(".scan-go").focus(); scanPage(); }
  }

  async function startPhone() {
    msg("Getting the phone link ready…");
    const r = await api.scan_phone_start();
    if (r.error) { msg(r.error, "bad"); return; }
    S2.phone = r;
    q(".scan-qr").src = r.qr;
    q(".scan-addr").textContent = r.url;
    msg("Waiting for photos from your phone…");
  }

  async function scanPage() {
    if (S2.busy) return;
    S2.busy = true;
    q(".scan-go").disabled = true;
    q(".scan-go span").textContent = "Scanning…";
    msg("Finish in the scanner window - it may be behind this one.");
    const r = await api.scan_device();
    if (r.error) { scanDone(); msg(r.error, "bad"); }
  }
  function scanDone() {
    S2.busy = false;
    q(".scan-go").disabled = false;
    q(".scan-go span").textContent = S2.pages.length ? "Scan the next page" : "Scan a page";
  }

  // the app's shared #menu sits under modal dialogs (top layer), so this sheet has its own little menu
  function addMenu(btn) {
    const m = q(".scan-add-menu");
    const show = m.hidden;
    m.hidden = !show;
    btn.setAttribute("aria-expanded", String(show));
    if (!show) return;
    m.querySelector("[data-add=phone] span").textContent = S2.phone ? "Show the phone code again" : "Take photos with my phone";
    m.querySelector("button").focus();
  }
  function closeAddMenu() { q(".scan-add-menu").hidden = true; q(".scan-add").setAttribute("aria-expanded", "false"); }
  async function addFrom(kind) {
    closeAddMenu();
    if (kind === "scanner") {
      S2.mode = "scanner"; q(".scan-device").hidden = false; step("pages");
      return scanPage();
    }
    S2.mode = "phone"; q(".scan-phone").hidden = false; step("pages");
    if (!S2.phone) await startPhone();
    q(".scan-qr").scrollIntoView({ block: "nearest" });
  }

  async function poll() {
    if (!S2.dlg?.open || S2.result) return;
    const r = await api.scan_status([...S2.thumbs.keys()]);
    if (r.error) return;
    for (const p of r.pages) if (p.thumb) S2.thumbs.set(p.key, p.thumb);
    const before = S2.pages.map(p => p.key).join();
    const grew = r.pages.length > S2.pages.length;
    S2.pages = r.pages;
    if (before !== S2.pages.map(p => p.key).join()) renderTray();
    const sc = r.scanner;
    if (sc.state === "done") { scanDone(); msg(`Page ${S2.pages.length} scanned. Put the next page on the glass and scan again, or make the PDF.`, "ok"); }
    else if (sc.state === "cancelled") { scanDone(); msg(S2.pages.length ? "" : "Scanning cancelled."); }
    else if (sc.state === "nodevice" || sc.state === "error") {
      scanDone(); msg(sc.error, "bad");
      if (sc.state === "nodevice") q(".scan-add").focus();
    }
    if (S2.mode === "phone" && grew) msg(`${S2.pages.length} page${S2.pages.length === 1 ? "" : "s"} received. Take the next one, or make the PDF.`, "ok");
    if (S2.phone && !r.phone.running && r.notice) { S2.phone = null; msg(r.notice, "bad"); }
  }

  // ------------------------------------------------------------------ the page tray
  function renderTray() {
    const tray = q(".scan-tray");
    const n = S2.pages.length;
    tray.replaceChildren(...S2.pages.map((p, k) => {
      const li = el("li", { class: "scan-page" + (p.landscape ? " wide" : ""), draggable: "true", "data-id": p.id, "data-k": k },
        el("div", { class: "scan-thumb" }, el("img", { src: S2.thumbs.get(p.key) || "", alt: `Page ${k + 1}` })),
        el("div", { class: "scan-page-bar" },
          el("span", { class: "scan-n", text: String(k + 1) }),
          el("button", { class: "icon-btn sm", "aria-label": `Move page ${k + 1} earlier`, title: "Move earlier", disabled: k === 0 || null,
            onclick: () => pageOp("move", p.id, k - 1) }, icon("left")),
          el("button", { class: "icon-btn sm", "aria-label": `Move page ${k + 1} later`, title: "Move later", disabled: k === n - 1 || null,
            onclick: () => pageOp("move", p.id, k + 1) }, icon("fwd")),
          el("button", { class: "icon-btn sm", "aria-label": `Turn page ${k + 1}`, title: "Turn right", onclick: () => pageOp("rotate", p.id, 90) }, icon("rotate-r")),
          el("button", { class: "icon-btn sm danger-hover", "aria-label": `Delete page ${k + 1}`, title: "Delete", onclick: () => pageOp("delete", p.id) }, icon("trash"))));
      li.addEventListener("dragstart", e => { S2.dragFrom = k; li.classList.add("dragging"); e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/x-scan-page", p.id); });
      li.addEventListener("dragend", () => { li.classList.remove("dragging"); S2.dragFrom = null; $$(".scan-page.drop").forEach(x => x.classList.remove("drop")); });
      li.addEventListener("dragover", e => { if (S2.dragFrom == null) return; e.preventDefault(); $$(".scan-page.drop").forEach(x => x.classList.remove("drop")); li.classList.add("drop"); });
      li.addEventListener("drop", e => { if (S2.dragFrom == null) return; e.preventDefault(); e.stopPropagation(); if (S2.dragFrom !== k) pageOp("move", S2.pages[S2.dragFrom].id, k); });
      return li;
    }));
    q(".scan-empty").hidden = n > 0;
    q(".scan-make").disabled = n === 0;
    q(".scan-make").textContent = n ? `Make PDF (${n} page${n === 1 ? "" : "s"})` : "Make PDF";
    if (S2.mode === "scanner" && !S2.busy) q(".scan-go span").textContent = n ? "Scan the next page" : "Scan a page";
  }

  async function pageOp(op, id, arg) {
    const focusId = op === "move" ? id : null;
    const r = await api.scan_page(op, id, arg);
    if (fail(r)) return;
    for (const p of r.pages) if (p.thumb) S2.thumbs.set(p.key, p.thumb);
    S2.pages = r.pages;
    if (op === "rotate") {   // the turned page needs its new thumbnail
      const st = await api.scan_status([...S2.thumbs.keys()]);
      if (!st.error) { for (const p of st.pages) if (p.thumb) S2.thumbs.set(p.key, p.thumb); S2.pages = st.pages; }
    }
    renderTray();
    if (focusId) {
      const k = S2.pages.findIndex(p => p.id === focusId);
      q(`.scan-page[data-id="${focusId}"] button[aria-label^="Move page ${k + 1} ${arg > k ? "later" : "earlier"}"]`)?.focus();
    }
    if (op === "delete") msg(S2.pages.length ? "Page deleted." : "");
  }

  // ------------------------------------------------------------------ finish
  async function makePdf() {
    const btn = q(".scan-make");
    btn.disabled = true;
    btn.textContent = "Making the PDF…";
    msg("Putting the pages together…");
    const r = await api.scan_make_pdf({ clean: q(".scan-clean").checked, paper: S2.paper, folder: S2.folder });
    if (r.error) { msg(r.error, "bad"); renderTray(); return; }
    S2.result = r;
    await api.scan_phone_stop();
    S2.phone = null;
    q(".scan-done-name").textContent = r.name;
    q(".scan-done-where").textContent = `${r.pages} page${r.pages === 1 ? "" : "s"} · saved in ${r.folder}`;
    step("done");
    q(".scan-open").focus();
    setStatus(r.status);
  }

  function tryClose() {
    if (S2.pages.length && !S2.result) { q(".scan-discard").hidden = false; q(".scan-keep").focus(); return; }
    close();
  }
  function close() {
    clearInterval(S2.poll);
    api.scan_stop();
    S2.phone = null;
    if (S2.dlg?.open) S2.dlg.close();
  }

  window.ScanUI = { open };
})();
