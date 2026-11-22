/* Words in a scan (Batch 5) - the iPad's bar: on a scanned page, "This page is a scan · Make its words selectable".
   The words are read here (Apple's text reader on a Mac, the Tesseract add-on on Windows) and written into the page
   invisibly, so they can be selected, copied, highlighted and found.  One undo step; Save keeps them.
   Python side: bridge_scanwords.py / scanwords.py. */
(() => {
  // each open document (tab) has its own: W is the one on screen (tabs.js swaps them); reading carries on in the back
  const fresh = () => ({ pages: new Set(), dismissed: false, running: false, done: 0, total: 0, token: 0, recheckT: 0, tab: null });
  let W = fresh();
  const BACK = new Map();
  const bar = el("div", { class: "scan-bar", id: "scan-bar", role: "status", hidden: true });
  $(".stage").append(bar);

  /** A document was opened: which of its pages are scans? */
  async function docOpened() {
    W.pages = new Set(); W.dismissed = false; W.running = false; W.token++;
    update();
    await check();
  }

  async function check() {
    if (!S.doc || W.running) return;
    const w = W, token = W.token;
    const r = await api.scan_pages();
    if (w !== W || token !== W.token || r.error) return;
    W.pages = new Set(r.pages);
    update();
  }

  /** After any change (undo can make a page a scan again). */
  function recheck() {
    clearTimeout(W.recheckT);
    W.recheckT = setTimeout(check, 300);
  }

  function update() {
    if (W.running) {
      bar.replaceChildren(
        el("span", { class: "scan-spin", "aria-hidden": "true" }),
        el("span", { text: W.total > 1 ? `Reading the words… page ${Math.min(W.done + 1, W.total)} of ${W.total}` : "Reading the words…" }),
        el("button", { class: "scan-btn", text: "Stop", onclick: stop }));
      bar.hidden = false;
      return;
    }
    if (!S.doc || W.dismissed || !W.pages.has(S.current)) { bar.hidden = true; return; }
    bar.replaceChildren(
      el("span", { text: "This page is a scan" }),
      el("button", {
        class: "scan-btn primary", onclick: start,
        title: "Read the words in the scan here on this computer, so they can be selected, copied, highlighted and found",
      }, icon("scan"), "Make its words selectable"),
      el("button", { class: "icon-btn sm", "aria-label": "Not now", title: "Not now", onclick: () => { W.dismissed = true; update(); } }, icon("x")));
    bar.hidden = false;
  }

  /** Read the scanned pages' words -> {changed, stopped} (or {error}) once done.  [quiet]: no toast of its own (Find
      personal details reads a scan first, and says so in its list). */
  async function start({ quiet = false } = {}) {
    if (W.running) return { error: "The scan's words are being read already - wait for that to finish." };
    const w = W, tabApi = apiIn(S.tab);           // this tab's job, even when another tab is on screen
    w.tab = S.tab;
    const r = await tabApi.scan_words_start(null);
    if (r.error) { if (!quiet) fail(r); return r; }
    let out = { changed: [], stopped: false };
    w.running = true; w.done = 0; w.total = r.total || w.pages.size;
    update();
    const token = w.token;
    while (token === w.token) {
      await new Promise(res => setTimeout(res, 400));
      const st = await tabApi.scan_words_status();
      const front = w === W;
      if (st.error) { w.running = false; out = st; if (front && !st.closed) toast(st.error, { error: true }); break; }
      w.done = st.done || 0; w.total = st.total || w.total;
      if (st.running) { if (front) update(); continue; }
      w.running = false;
      const res = st.result;
      if (!res) break;
      if (res.error) { out = res; if (front) toast(res.error, { error: true }); break; }
      out = { changed: res.changed || [], stopped: !!res.stopped, front };
      w.pages = new Set([...w.pages].filter(i => !res.changed.includes(i)));
      if (!front) { if (res.changed?.length) window.Tabs?.changed(res); break; }   // shown when its tab is back in front
      refreshDoc(res, res.changed);
      setStatus(res.status);
      if (!quiet) toast(res.status, res.changed?.length ? { actions: [{ label: "Undo", run: () => window.undo?.() }] } : {});
      break;
    }
    if (w !== W) return out;
    update();
    check();
    return out;
  }

  function stop() { api.scan_words_stop(); }

  /** Going Home: stop reading, forget the pages. */
  function closed() {
    if (W.running) api.scan_words_stop();
    W.token++; W.running = false; W.pages = new Set();
    bar.hidden = true;
  }

  // ---- tabs (tabs.js)
  function tabLeave(id) { BACK.set(id, W); W = fresh(); bar.hidden = true; }
  function tabEnter(id) { W = BACK.get(id) || fresh(); BACK.delete(id); W.tab = id; update(); if (!W.running && S.doc) check(); }
  function tabClosed(id) { const w = BACK.get(id); if (w) { w.token++; BACK.delete(id); } }

  window.ScanWords = { docOpened, recheck, closed, pageShown: update, tabLeave, tabEnter, tabClosed, read: start, get running() { return W.running; } };
})();
