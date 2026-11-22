/* Editor tools (stage 2): mark up, draw, text, edit text, redact, your signature, forms, crop.
   Uses app.js globals: api, S, el, icon, $, $$, clamp, toast, fail, setStatus, pagesEl, viewer, showMenu,
   refreshDoc, goTo.  Page coordinates are display points (1 = 1 PDF point, rotation applied). */
"use strict";
(() => {
  const SVGNS = "http://www.w3.org/2000/svg";
  const store = {
    get(k, d) { try { const v = localStorage.getItem("tk." + k); return v == null ? d : JSON.parse(v); } catch { return d; } },
    set(k, v) { try { localStorage.setItem("tk." + k, JSON.stringify(v)); } catch { /* private mode */ } },
  };

  const PALETTE = {
    highlight: ["#ffd60a", "#86efac", "#f9a8d4", "#93c5fd"],
    underline: ["#e11d48", "#2563eb", "#111827", "#16a34a"],
    draw: ["#1a237e", "#111827", "#e11d48", "#2563eb", "#16a34a", "#f97316"],
    text: ["#111827", "#1a237e", "#e11d48", "#2563eb"],
  };
  PALETTE.strikeout = PALETTE.underline;
  const COLOUR_NAMES = { "#ffd60a": "Yellow", "#86efac": "Green", "#f9a8d4": "Pink", "#93c5fd": "Blue", "#e11d48": "Red",
    "#2563eb": "Blue", "#111827": "Black", "#16a34a": "Green", "#1a237e": "Navy", "#f97316": "Orange" };
  const SUB = {
    markup: [["highlight", "Highlight", "markup"], ["underline", "Underline", "underline"], ["strikeout", "Strike", "strike"]],
    draw: [["pen", "Pen", "draw"], ["rect", "Box", "box"], ["ellipse", "Ellipse", "ellipse"], ["line", "Line", "line"], ["arrow", "Arrow", "arrow"]],
    text: [["textbox", "Text box", "text"], ["tick", "Tick", "tick"], ["cross", "Cross", "cross"], ["date", "Date", "calendar"],
      ["note", "Note", "note"], ["image", "Picture", "image"], ["link", "Link", "link"]],
    sign: [["mine", "My signature", "sign"], ["initials", "Initials", "initials"], ["digital", "Digital signature", "shield"]],
  };
  const HINTS = {
    select: "Click a form box to type in it · drag across text to copy or mark it · click a note, shape or picture to change it",
    highlight: "Drag across the words to highlight", underline: "Drag across the words to underline",
    strikeout: "Drag across the words to strike through",
    pen: "Draw freely on the page", rect: "Drag to draw a box", ellipse: "Drag to draw an ellipse",
    line: "Drag to draw a line", arrow: "Drag to draw an arrow - it points where you let go",
    textbox: "Click where the text should start, then type", note: "Click where the sticky note should go",
    tick: "Click each box to tick it - drag to make a bigger tick", cross: "Click where the cross goes - drag to make it bigger",
    date: "Click where today's date goes",
    initials: "Click where your initials go - then you can add them to every page",
    image: "Click to place a picture, or drag the area it should fill", link: "Drag over the area people should click",
    edittext: "Click a line or paragraph to change its words - double-click to edit straight away",
    redact: "Drag across text or over an area. Nothing is removed until you confirm.",
    mine: "Click where your signature goes, or drag a box for it",
    digital: "Drag where the signature box should go - or sign without a visible box",
    crop: "Drag over the part of the page to keep",
  };
  const FONT_CSS = { Helvetica: "Arial, Helvetica, sans-serif", "Helvetica Bold": "Arial, Helvetica, sans-serif",
    Times: "'Times New Roman', Times, serif", "Times Bold": "'Times New Roman', Times, serif",
    Courier: "'Courier New', Courier, monospace", "Courier Bold": "'Courier New', Courier, monospace" };
  const ALIGNS = ["Left", "Centre", "Right", "Justified"];

  const E = {
    tool: "select",
    sub: store.get("sub", { markup: "highlight", draw: "pen", text: "textbox", sign: "mine" }),
    color: store.get("color", { highlight: "#ffd60a", underline: "#e11d48", strikeout: "#e11d48", draw: "#1a237e", text: "#111827" }),
    width: store.get("width", 2), size: store.get("size", 12), font: store.get("font", "Helvetica"),
    dateFmt: store.get("dateFmt", "long"), sigs: { signature: undefined, initials: undefined },
    layouts: new Map(), lay: new Map(),
    drag: null, sel: null, tsel: null, target: null, pending: null, cropPage: null, sig: undefined, anchor: null,
    adhoc: null,          // a one-off signature {spec, preview, aspect} - used instead of the saved one, never stored
  };
  const save = () => { store.set("sub", E.sub); store.set("color", E.color); store.set("width", E.width); store.set("size", E.size); store.set("font", E.font); store.set("dateFmt", E.dateFmt); };
  const DATE_FMTS = {
    long: d => d.toLocaleDateString(undefined, { day: "numeric", month: "long", year: "numeric" }),
    short: d => d.toLocaleDateString(undefined, { day: "2-digit", month: "2-digit", year: "numeric" }),
    medium: d => d.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" }),
    iso: d => d.toISOString().slice(0, 10),
  };
  const today = () => DATE_FMTS[E.dateFmt] ? DATE_FMTS[E.dateFmt](new Date()) : DATE_FMTS.long(new Date());
  const mode = () => (E.tool === "markup" || E.tool === "draw" || E.tool === "text" || E.tool === "sign") ? E.sub[E.tool] : E.tool;
  const colourFor = m => E.color[m] || (m === "textbox" || m === "note" || m === "link" ? E.color.text : E.color.draw);

  // ------------------------------------------------------------------ page data
  function layout(i) {
    if (!E.layouts.has(i)) {
      const tab = S.tab;
      E.layouts.set(i, api.page_layout(i).then(r => {
        if (tab !== S.tab) return r;          // another tab came to the front meanwhile: not its page
        if (r.error) { E.layouts.delete(i); return null; }
        E.lay.set(i, r); return r;
      }));
    }
    return E.layouts.get(i);
  }
  const pageDiv = i => pagesEl.children[i];
  const pageSize = i => S.doc.pages[i];
  function ov(i) { return pageDiv(i)?.querySelector(".ov"); }
  function ink(i) { return pageDiv(i)?.querySelector("svg.ink"); }
  function svg(tag, attrs) {
    const n = document.createElementNS(SVGNS, tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
    return n;
  }
  const pct = (i, r) => {   // display rect -> CSS percentages within the page (zoom independent)
    const p = pageSize(i);
    return `left:${r[0] / p.w * 100}%;top:${r[1] / p.h * 100}%;width:${(r[2] - r[0]) / p.w * 100}%;height:${(r[3] - r[1]) / p.h * 100}%`;
  };
  // The view can be turned on screen (app.js setTurn: each page drawn turned round its middle), so a point on
  // the screen is turned back before it becomes a point on the page, and the other way round.
  const turn = () => window.ViewTurn?.turn || 0;
  function screenPoint(b, p, x, y) {           // page point (display units) -> screen point
    const t = turn(), w = p.w * S.zoom, h = p.h * S.zoom, cx = b.left + b.width / 2, cy = b.top + b.height / 2;
    const dx = x * S.zoom - w / 2, dy = y * S.zoom - h / 2;
    const [c, s] = [[1, 0], [0, 1], [-1, 0], [0, -1]][t / 90];
    return [cx + dx * c - dy * s, cy + dx * s + dy * c];
  }
  function clientRect(i, r) {
    const b = pageDiv(i).getBoundingClientRect();
    if (!turn()) return { left: b.left + r[0] * S.zoom, top: b.top + r[1] * S.zoom, right: b.left + r[2] * S.zoom, bottom: b.top + r[3] * S.zoom };
    const p = pageSize(i);
    const pts = [[r[0], r[1]], [r[2], r[1]], [r[0], r[3]], [r[2], r[3]]].map(([x, y]) => screenPoint(b, p, x, y));
    const xs = pts.map(q => q[0]), ys = pts.map(q => q[1]);
    return { left: Math.min(...xs), top: Math.min(...ys), right: Math.max(...xs), bottom: Math.max(...ys) };
  }
  function pagePoint(e, div) {
    const b = div.getBoundingClientRect();
    const p = pageSize(+div.dataset.i);
    const t = turn();
    if (!t) return [clamp((e.clientX - b.left) / S.zoom, 0, p.w), clamp((e.clientY - b.top) / S.zoom, 0, p.h)];
    const dx = e.clientX - (b.left + b.width / 2), dy = e.clientY - (b.top + b.height / 2);
    const [c, s] = [[1, 0], [0, 1], [-1, 0], [0, -1]][t / 90];
    const lx = dx * c + dy * s, ly = -dx * s + dy * c;      // turned back
    return [clamp((lx + p.w * S.zoom / 2) / S.zoom, 0, p.w), clamp((ly + p.h * S.zoom / 2) / S.zoom, 0, p.h)];
  }
  const inside = (r, x, y, pad = 0) => x >= r[0] - pad && x <= r[2] + pad && y >= r[1] - pad && y <= r[3] + pad;
  const area = r => (r[2] - r[0]) * (r[3] - r[1]);
  const norm = (a, b) => [Math.min(a[0], b[0]), Math.min(a[1], b[1]), Math.max(a[0], b[0]), Math.max(a[1], b[1])];

  async function apply(promise) {
    const r = await promise;
    if (fail(r)) return null;
    refreshDoc(r);
    setStatus(r.status);
    return r;
  }

  // ------------------------------------------------------------------ page decoration
  function decorate(div, i) {
    const p = pageSize(i);
    const o = el("div", { class: "ov" });
    o.append(svg("svg", { class: "ink", viewBox: `0 0 ${p.w} ${p.h}`, preserveAspectRatio: "none" }));
    o.append(el("div", { class: "flds" }));
    div.append(o);
  }
  function pageVisible(i) {      // load words/fields early so the first click or drag already knows the page
    layout(i).then(L => { if (!L) return; noteBadges(i, L); if (S.doc?.has_forms) buildFields(i, L); });
  }
  function pageChanged(i) {      // reload at once: the next click must see what was just added
    E.layouts.delete(i); E.lay.delete(i);
    E.spotsFor = null;
    if (E.tool === "sign" && E.sub.sign !== "digital") showSpots();
    layout(i).then(L => { if (!L) return; noteBadges(i, L); if (S.doc?.has_forms || pageDiv(i)?.querySelector(".fld")) buildFields(i, L); });
  }
  function docChanged() { E.layouts.clear(); E.lay.clear(); clearAll(); E.spotsFor = null; E.spots = null;
    if (E.tool === "sign" && E.sub.sign !== "digital") showSpots(); }

  // ------------------------------------------------------------------ forms
  function buildFields(i, L) {
    const box = ov(i)?.querySelector(".flds");
    if (!box) return;
    const fields = L.fields.filter(f => !f.readonly);
    const k = fields.map(f => f.xref).join(",");
    if (box.dataset.k === k) {           // same fields: refresh values without disturbing focus
      for (const f of fields) {
        const n = box.querySelector(`[data-xref="${f.xref}"]`);
        if (!n || n.contains(document.activeElement)) continue;
        if (f.kind === "check" || f.kind === "radio") n.setAttribute("aria-checked", String(f.checked));
        else if ("value" in f) { const inp = n.querySelector("input,textarea,select"); if (inp) { inp.value = f.value; inp.dataset.orig = f.value; } }
      }
      return;
    }
    box.dataset.k = k;
    box.replaceChildren(...fields.map(f => fieldEl(i, f)));
  }

  function fieldEl(i, f) {
    const wrap = el("div", { class: `fld fld-${f.kind}`, style: pct(i, f.rect), "data-xref": f.xref, title: f.name });
    const h = f.rect[3] - f.rect[1];
    const fs = f.size || clamp(h * 0.62, 7, 14);
    if (f.kind === "text") {
      const inp = el(f.multiline ? "textarea" : "input", { class: "fld-in", "aria-label": f.name || "Form box",
        maxlength: f.maxlen || null, spellcheck: "true" });
      inp.value = f.value; inp.dataset.orig = f.value;
      inp.style.fontSize = `calc(var(--z) * ${fs}px)`;
      const commit = async () => {
        if (inp.value === inp.dataset.orig) return;
        const v = inp.value;
        inp.dataset.orig = v;
        await apply(api.set_field(i, f.xref, v));
      };
      inp.addEventListener("blur", commit);
      inp.addEventListener("keydown", e => {
        if (e.key === "Enter" && !f.multiline) { e.preventDefault(); focusNextField(inp, e.shiftKey ? -1 : 1); }
        if (e.key === "Escape") { inp.value = inp.dataset.orig; inp.blur(); e.stopPropagation(); }
      });
      wrap.append(inp);
    } else if (f.kind === "check" || f.kind === "radio") {
      wrap.setAttribute("role", f.kind === "radio" ? "radio" : "checkbox");
      wrap.setAttribute("aria-checked", String(f.checked));
      wrap.setAttribute("aria-label", f.name || "Tick box");
      wrap.tabIndex = 0;
      const toggle = async () => {
        const now = wrap.getAttribute("aria-checked") !== "true";
        wrap.setAttribute("aria-checked", String(now));
        await apply(api.set_field(i, f.xref, now));
      };
      wrap.addEventListener("click", toggle);
      wrap.addEventListener("keydown", e => { if (e.key === " " || e.key === "Enter") { e.preventDefault(); toggle(); } });
    } else if (f.kind === "combo" || f.kind === "list") {
      const sel = el("select", { class: "fld-in", "aria-label": f.name || "Choice" },
        ...[...(f.choices.includes(f.value) || !f.value ? [] : [f.value]), ...f.choices].map(c => el("option", { value: c, text: c })));
      sel.value = f.value;
      sel.style.fontSize = `calc(var(--z) * ${fs}px)`;
      sel.addEventListener("change", () => apply(api.set_field(i, f.xref, sel.value)));
      wrap.append(sel);
    } else if (f.kind === "signature") {
      if (f.signed) { wrap.classList.add("fld-signed"); return wrap; }
      wrap.append(el("button", { class: "fld-sign", text: "Sign here", onclick: e => signField(i, f, e.currentTarget) }));
    }
    return wrap;
  }
  function focusNextField(from, dir) {
    const all = $$(".fld-in, .fld[role=checkbox], .fld[role=radio], .fld-sign");
    const k = all.indexOf(from);
    const next = all[k + dir];
    if (next) next.focus(); else from.blur();
  }
  function signField(i, f, btn) {
    showPill([
      { label: "Add my signature", icon: "sign", primary: true, run: () => placeSignatureIn(i, f.rect) },
      { label: "Sign digitally", icon: "shield", run: () => window.Sign?.begin({ page: i, rect: f.rect }) },
    ], i, f.rect);
    btn.blur();
  }

  // ------------------------------------------------------------------ tool bar (sub-options)
  function setTool(t) {
    if (t === "sign" && E.tool === "sign" && E.sub.sign === "digital") window.Sign?.cancelPlacement?.();
    commitPending();
    clearAll();
    if (t !== "crop") E.cropPage = null;
    E.tool = t;
    $$("#tools button[data-tool]").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.tool === t)));
    [...pagesEl.classList].filter(c => c.startsWith("tool-") || c.startsWith("sub-")).forEach(c => pagesEl.classList.remove(c));
    pagesEl.classList.add("tool-" + t);
    if (SUB[t]) pagesEl.classList.add("sub-" + E.sub[t]);
    buildSubbar();
    setStatus(HINTS[mode()] || "");
    if (t === "sign") onSignSub();
    if (t === "sign" && E.sub.sign !== "digital") showSpots(); else hideSpots();
  }

  function buildSubbar() {
    const bar = $("#subbar");
    const t = E.tool, m = mode();
    if (t === "select") { bar.hidden = true; return; }
    const kids = [];
    if (SUB[t]) {
      kids.push(el("div", { class: "seg sub-seg", role: "radiogroup", "aria-label": "Kind" }, ...SUB[t].map(([id, label, ic]) =>
        el("button", { role: "radio", "aria-checked": String(E.sub[t] === id), onclick: () => {
          E.sub[t] = id; save(); setTool(t);
        } }, icon(ic), label))));
    }
    const pal = PALETTE[m] || (t === "draw" ? PALETTE.draw : (t === "text" && m !== "image" && m !== "link" ? PALETTE.text : null));
    if (pal && m !== "note") {
      kids.push(el("span", { class: "vsep" }));
      kids.push(el("div", { class: "swatches", role: "radiogroup", "aria-label": "Colour" }, ...pal.map(c =>
        el("button", { class: "swatch", role: "radio", style: `--c:${c}`, "aria-checked": String(colourFor(m) === c),
          "aria-label": COLOUR_NAMES[c] || c, title: COLOUR_NAMES[c] || c,
          onclick: () => { E.color[t === "draw" ? "draw" : t === "text" ? "text" : m] = c; save(); buildSubbar(); } }))));
    }
    if (t === "draw") {
      kids.push(el("span", { class: "vsep" }));
      kids.push(el("div", { class: "seg sub-seg widths", role: "radiogroup", "aria-label": "Line thickness" },
        ...[[1, "Thin"], [2, "Medium"], [4, "Thick"]].map(([w, label]) => el("button", {
          role: "radio", "aria-checked": String(E.width === w), title: label, "aria-label": label,
          onclick: () => { E.width = w; save(); buildSubbar(); } },
          el("span", { class: "wline", style: `height:${w + 1}px` })))));
    }
    if (t === "text" && m === "date") {
      kids.push(el("span", { class: "vsep" }));
      const fmt = el("select", { class: "sub-select", "aria-label": "Date style", onchange: e => { E.dateFmt = e.target.value; save(); } },
        ...Object.keys(DATE_FMTS).map(k => el("option", { value: k, text: DATE_FMTS[k](new Date()) })));
      fmt.value = E.dateFmt;
      kids.push(fmt);
    }
    if (t === "text" && (m === "textbox" || m === "date")) {
      kids.push(el("span", { class: "vsep" }));
      const font = el("select", { class: "sub-select", "aria-label": "Font", onchange: e => { E.font = e.target.value; save(); } },
        ...Object.keys(FONT_CSS).map(f => el("option", { value: f, text: f })));
      font.value = E.font;
      const size = el("select", { class: "sub-select", "aria-label": "Size", onchange: e => { E.size = +e.target.value; save(); } },
        ...[8, 9, 10, 11, 12, 14, 16, 18, 24, 32].map(s => el("option", { value: s, text: s + " pt" })));
      size.value = E.size;
      kids.push(font, size);
    }
    if (t === "sign") {
      kids.push(el("span", { class: "vsep" }));
      if (m === "initials") {
        kids.push(el("button", { class: "btn btn-ghost sm", onclick: () => openSignaturePad({ slot: "initials" }) }, icon("edittext"), "Change my initials"));
      } else if (m === "mine" && E.adhoc) {
        kids.push(el("span", { class: "sub-chip", text: "One-off signature - not saved" }));
        kids.push(el("button", { class: "btn btn-ghost sm", onclick: () => { E.adhoc = null; buildSubbar(); ensureSignature(); } },
          icon("sign"), "Use my saved signature"));
      } else if (m === "mine") {
        kids.push(el("button", { class: "btn btn-ghost sm", onclick: () => openSignaturePad({ remember: true }) }, icon("edittext"), "Change my signature"));
        kids.push(el("button", { class: "btn btn-ghost sm", onclick: () => openSignaturePad({ remember: false }) }, icon("plus"), "One-off signature"));
      }
      if (m !== "digital") kids.push(el("button", { class: "btn btn-ghost sm spots-btn", id: "spots-next", hidden: !E.spots?.length,
        onclick: nextSpot }, icon("search"), spotsLabel()));
      else kids.push(el("button", { class: "btn btn-ghost sm", onclick: () => window.Sign?.begin({ page: S.current, rect: null }) }, icon("shield"), "Sign without a visible box"));
    }
    if (t === "crop") {
      kids.push(el("span", { class: "sub-hint", text: `Drag over the part of page ${E.cropPage + 1} to keep` }));
      kids.push(el("button", { class: "btn btn-ghost sm", text: "Cancel", onclick: () => setTool("select") }));
    } else if (t === "edittext" || t === "redact") {
      kids.push(el("span", { class: "sub-hint", text: HINTS[t] }));
      if (t === "redact" && window.Fill)
        kids.push(el("button", { class: "btn btn-ghost sm", onclick: () => window.Fill.findPersonal() }, icon("search"), "Find personal details"));
    }
    kids.push(el("button", { class: "icon-btn sm sub-close", "aria-label": "Back to Select", title: "Done (Esc)", onclick: () => setTool("select") }, icon("x")));
    bar.replaceChildren(...kids);
    bar.hidden = false;
  }

  async function onSignSub() {
    if (E.sub.sign === "mine" || E.sub.sign === "initials") {
      // cancelling set-up keeps the Sign tool open, so Initials / Digital signature are still one click away
      const r = await ensureSignature(E.sub.sign === "initials" ? "initials" : "signature");
      if (!r) setStatus("Choose My signature, Initials or Digital signature above - you'll be asked to set it up when you place it.");
    } else {
      window.Sign?.ensureIdentity().then(ok => { if (!ok) setStatus("A digital signature needs a signing identity - choose Digital signature again to set one up."); });
    }
  }

  // ------------------------------------------------------------------ pill (floating action bar)
  function showPill(items, i, rect) {
    const p = $("#pill");
    p.replaceChildren(...items.map(it => {
      if (it === "-") return el("span", { class: "pill-sep" });
      if (it.chip) return el("span", { class: "pill-chip " + (it.kind || "") }, icon(it.kind === "warn" ? "alert" : "check"), it.chip);
      if (it.note) return el("span", { class: "pill-note", title: it.note }, icon("note"), it.note.length > 60 ? it.note.slice(0, 60) + "…" : it.note);
      if (it.swatches) return el("span", { class: "pill-swatches", role: "radiogroup", "aria-label": it.label || "Colour" },
        ...it.swatches.map(c => el("button", { class: "swatch", role: "radio", style: `--c:${c}`, "aria-checked": String(sameColour(c, it.current)),
          "aria-label": COLOUR_NAMES[c] || c, title: COLOUR_NAMES[c] || c,
          onclick: () => { hidePill(); if (!sameColour(c, it.current)) it.run(c); } })));
      return el("button", { class: (it.primary ? "primary " : "") + (it.danger ? "danger" : ""), onclick: () => { hidePill(); it.run(); } },
        it.icon ? icon(it.icon) : null, it.label);
    }));
    p.hidden = false;
    E.anchor = { i, rect };
    placePill();
    p.querySelector("button")?.focus({ preventScroll: true });
  }
  function placePill() {
    const p = $("#pill");
    if (p.hidden || !E.anchor || !pageDiv(E.anchor.i)) return;
    const r = clientRect(E.anchor.i, E.anchor.rect);
    const h = p.offsetHeight, w = p.offsetWidth;
    let top = r.top - h - 10;
    if (top < 70) top = r.bottom + 10;
    p.style.top = clamp(top, 64, innerHeight - h - 8) + "px";
    p.style.left = clamp(r.left, 8, innerWidth - w - 8) + "px";
  }
  function hidePill() { $("#pill").hidden = true; E.anchor = null; }
  /** '#FFD60A' and '#ffd60a' (and a colour a byte off after the PDF round trip) are the same colour. */
  function sameColour(a, b) {
    if (!a || !b) return false;
    const n = h => [1, 3, 5].map(k => parseInt(h.slice(k, k + 2), 16));
    const x = n(a.toLowerCase()), y = n(b.toLowerCase());
    return x.every((v, k) => Math.abs(v - y[k]) <= 3);
  }
  viewer.addEventListener("scroll", () => { placePill(); placePop(); });

  // ------------------------------------------------------------------ popover (small text entry near the page)
  function popover({ title, value = "", placeholder = "", multiline = true, ok = "Add", i, at, onOk, onCancel }) {
    const pop = $("#pop");
    const input = el(multiline ? "textarea" : "input", { class: "pop-in", placeholder, "aria-label": title, rows: multiline ? 4 : null });
    input.value = value;
    const done = async v => { closePop(); await onOk(v); };
    const cancel = () => { closePop(); onCancel?.(); };
    input.addEventListener("keydown", e => {
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); cancel(); }
      if (e.key === "Enter" && (!multiline || e.ctrlKey || e.metaKey)) { e.preventDefault(); done(input.value); }
    });
    pop.replaceChildren(el("div", { class: "pop-title", text: title }), input,
      el("div", { class: "pop-actions" },
        multiline ? el("span", { class: "muted small", text: keys("Ctrl ↵ to add") }) : null,
        el("button", { class: "btn btn-ghost sm", text: "Cancel", onclick: cancel }),
        el("button", { class: "btn btn-primary sm", text: ok, onclick: () => done(input.value) })));
    pop.hidden = false;
    E.popAt = { i, at };
    placePop();
    input.focus();
    if (!multiline) input.select();
  }
  function placePop() {
    const pop = $("#pop");
    if (pop.hidden || !E.popAt || !pageDiv(E.popAt.i)) return;
    const r = clientRect(E.popAt.i, [E.popAt.at[0], E.popAt.at[1], E.popAt.at[0], E.popAt.at[1]]);
    pop.style.left = clamp(r.left, 8, innerWidth - pop.offsetWidth - 8) + "px";
    pop.style.top = clamp(r.top + 12, 64, innerHeight - pop.offsetHeight - 8) + "px";
  }
  function closePop() { $("#pop").hidden = true; E.popAt = null; }

  // ------------------------------------------------------------------ temporary marks on a page
  function tmp(i, node) { node.classList.add("tmp"); ink(i)?.append(node); return node; }
  function clearTmp() { $$("svg.ink .tmp").forEach(n => n.remove()); $$(".ov .tmp-el").forEach(n => n.remove()); }
  function clearAll() {
    clearTmp(); hidePill(); closePop();
    E.sel = null; E.tsel = null; E.target = null; E.drag = null;
    $$(".ov .sel-box").forEach(n => n.remove());
  }
  function cancel() { E.pending?.cancel(); E.pending = null; clearAll(); }
  async function commitPending() { const p = E.pending; E.pending = null; if (p) await p.commit(); }

  // ------------------------------------------------------------------ text selection (reading order)
  function nearestWord(L, x, y) {
    let best = -1, bd = Infinity;
    L.words.forEach((w, k) => {
      const dx = x < w[0] ? w[0] - x : x > w[2] ? x - w[2] : 0;
      const dy = y < w[1] ? w[1] - y : y > w[3] ? y - w[3] : 0;
      const d = dx + dy * 3;
      if (d < bd) { bd = d; best = k; }
    });
    return { k: best, d: bd };
  }
  function selection(L, a, b) {
    if (!L || !L.words.length) return null;
    const s = nearestWord(L, ...a), e = nearestWord(L, ...b);
    if (s.k < 0 || (s.d > 30 && e.d > 30)) return null;
    const [lo, hi] = s.k <= e.k ? [s.k, e.k] : [e.k, s.k];
    const words = L.words.slice(lo, hi + 1);
    const rects = [];
    let text = "", last = null;
    for (const w of words) {
      const line = w[5] + ":" + w[6];
      if (last === line) {
        const r = rects[rects.length - 1];
        r[0] = Math.min(r[0], w[0]); r[1] = Math.min(r[1], w[1]); r[2] = Math.max(r[2], w[2]); r[3] = Math.max(r[3], w[3]);
        text += " " + w[4];
      } else {
        rects.push([w[0], w[1], w[2], w[3]]);
        text += (text ? "\n" : "") + w[4];
      }
      last = line;
    }
    return { rects, text, bbox: rects.reduce((u, r) => [Math.min(u[0], r[0]), Math.min(u[1], r[1]), Math.max(u[2], r[2]), Math.max(u[3], r[3])]) };
  }
  function drawSelection(i, sel, kind) {
    clearTmp();
    if (!sel) return;
    const c = kind === "select" ? "var(--accent)" : kind === "redact" ? "#000" : colourFor(kind);
    for (const r of sel.rects) {
      const h = r[3] - r[1];
      if (kind === "underline") tmp(i, svg("rect", { x: r[0], y: r[3] - h * 0.12, width: r[2] - r[0], height: Math.max(0.8, h * 0.08), fill: c }));
      else if (kind === "strikeout") tmp(i, svg("rect", { x: r[0], y: r[1] + h * 0.48, width: r[2] - r[0], height: Math.max(0.8, h * 0.08), fill: c }));
      else tmp(i, svg("rect", { x: r[0], y: r[1], width: r[2] - r[0], height: h, fill: c,
        class: kind === "redact" ? "redact-prev" : kind === "select" ? "select-prev" : "hl-prev" }));
    }
  }

  // ------------------------------------------------------------------ pointer input
  pagesEl.addEventListener("pointerdown", onDown);
  pagesEl.addEventListener("pointermove", onHover);
  pagesEl.addEventListener("pointerleave", () => { $$(".ov .ghost, svg.ink .hover").forEach(n => n.remove()); });
  pagesEl.addEventListener("dblclick", onDouble);

  function onDown(e) {
    if (e.button !== 0 || !S.doc) return;
    if (e.target.closest(".fld, .tb-edit, .placer")) return;
    const div = e.target.closest(".page");
    if (!div) return;
    const i = +div.dataset.i, pt = pagePoint(e, div), m = mode();
    if (E.tool === "crop" && i !== E.cropPage) return;
    if (E.pending) { e.preventDefault(); commitPending(); return; }     // a click outside finishes what's being placed
    hidePill(); closePop();
    layout(i);
    e.preventDefault();
    viewer.focus({ preventScroll: true });

    const L = E.lay.get(i);
    if (E.tool === "select") {
      const hit = L && L.annots.filter(a => inside(a.rect, ...pt, 2)).sort((a, b) => area(a.rect) - area(b.rect))[0];
      if (hit) { selectAnnot(i, hit); if (hit.movable) startDrag(e, div, i, pt, "move", { annot: hit }); return; }
      clearAll();
      return startDrag(e, div, i, pt, "tsel", { kind: "select" });
    }
    clearAll();
    if (E.tool === "markup") return startDrag(e, div, i, pt, "tsel", { kind: m });
    if (E.tool === "redact") return startDrag(e, div, i, pt, "redact", { kind: "redact" });
    if (E.tool === "draw") return startDrag(e, div, i, pt, m === "pen" ? "pen" : "shape", { kind: m });
    if (E.tool === "text") {
      if (m === "textbox") return inlineText(i, pt);
      if (m === "tick" || m === "cross") return startDrag(e, div, i, pt, "rect", { kind: m });
      if (m === "date") return apply(api.annotate(i, "textbox", { pt: [pt[0], pt[1] - E.size * 0.65], text: today() },
        { color: colourFor("textbox"), size: E.size, font: E.font }));
      if (m === "note") return popover({ title: "Sticky note", i, at: pt, ok: "Add note",
        onOk: v => v.trim() && apply(api.annotate(i, "note", { pt, text: v }, { color: "#f5c400" })) });
      return startDrag(e, div, i, pt, "rect", { kind: m });
    }
    if (E.tool === "sign" || E.tool === "crop") return startDrag(e, div, i, pt, "rect", { kind: E.tool === "crop" ? "crop" : m });
    if (E.tool === "edittext") return startDrag(e, div, i, pt, "click", {});
  }

  function startDrag(e, div, i, pt, type, extra) {
    E.drag = { i, div, type, start: pt, last: pt, pts: [pt], moved: false, ...extra };
    const move = ev => onMove(ev);
    const up = ev => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up); onUp(ev); };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    if (type === "pen") E.drag.node = tmp(i, svg("polyline", { points: `${pt[0]},${pt[1]}`, fill: "none", stroke: colourFor("draw"),
      "stroke-width": E.width, "stroke-linecap": "round", "stroke-linejoin": "round" }));
  }

  function onMove(e) {
    const d = E.drag;
    if (!d) return;
    const pt = pagePoint(e, d.div);
    d.last = pt;
    if (!d.moved && Math.hypot(pt[0] - d.start[0], pt[1] - d.start[1]) * S.zoom > 3) d.moved = true;
    if (!d.moved) return;
    const r = norm(d.start, pt);
    if (d.type === "redact") {          // starting on a word = select text; elsewhere = an area
      const L = E.lay.get(d.i);
      if (!L) return;
      d.type = L.words.some(w => inside(w, ...d.start, 1)) ? "tsel" : "rect";
    }
    if (d.type === "pen") {
      d.pts.push(pt);
      d.node.setAttribute("points", d.pts.map(p => p.join(",")).join(" "));
    } else if (d.type === "tsel") {
      d.sel = selection(E.lay.get(d.i), d.start, pt);
      drawSelection(d.i, d.sel, d.kind);
    } else if (d.type === "shape") {
      clearTmp();
      const c = colourFor("draw"), w = E.width;
      if (d.kind === "rect") tmp(d.i, svg("rect", { x: r[0], y: r[1], width: r[2] - r[0], height: r[3] - r[1], fill: "none", stroke: c, "stroke-width": w }));
      else if (d.kind === "ellipse") tmp(d.i, svg("ellipse", { cx: (r[0] + r[2]) / 2, cy: (r[1] + r[3]) / 2, rx: (r[2] - r[0]) / 2, ry: (r[3] - r[1]) / 2, fill: "none", stroke: c, "stroke-width": w }));
      else {
        tmp(d.i, svg("line", { x1: d.start[0], y1: d.start[1], x2: pt[0], y2: pt[1], stroke: c, "stroke-width": w, "stroke-linecap": "round" }));
        if (d.kind === "arrow") tmp(d.i, arrowHead(d.start, pt, c, w));
      }
    } else if (d.type === "rect") {
      clearTmp();
      tmp(d.i, svg("rect", { x: r[0], y: r[1], width: r[2] - r[0], height: r[3] - r[1], class: "drag-box " + d.kind }));
    } else if (d.type === "move") {
      const box = ov(d.i).querySelector(".sel-box");
      if (box) box.style.transform = `translate(${(pt[0] - d.start[0]) * S.zoom}px, ${(pt[1] - d.start[1]) * S.zoom}px)`;
    }
  }
  function arrowHead(a, b, c, w) {
    const ang = Math.atan2(b[1] - a[1], b[0] - a[0]), L = 6 + w * 2.5;
    const p1 = [b[0] - L * Math.cos(ang - 0.45), b[1] - L * Math.sin(ang - 0.45)];
    const p2 = [b[0] - L * Math.cos(ang + 0.45), b[1] - L * Math.sin(ang + 0.45)];
    return svg("polyline", { points: `${p1} ${b} ${p2}`, fill: "none", stroke: c, "stroke-width": w, "stroke-linecap": "round", "stroke-linejoin": "round" });
  }

  async function onUp() {
    const d = E.drag;
    if (!d) return;
    E.drag = null;
    const { i, start, last } = d;
    const r = norm(start, last);
    const big = (r[2] - r[0]) > 4 && (r[3] - r[1]) > 4;
    if (d.type === "pen") {
      if (d.pts.length < 2) { clearTmp(); return; }
      await apply(api.annotate(i, "ink", { strokes: [d.pts] }, { color: colourFor("draw"), width: E.width }));
      clearTmp();
    } else if (d.type === "shape") {
      clearTmp();
      if (!d.moved) return setStatus(HINTS[d.kind]);
      const geom = d.kind === "line" || d.kind === "arrow" ? { p1: start, p2: last } : { rect: r };
      if (d.kind !== "line" && d.kind !== "arrow" && !big) return;
      await apply(api.annotate(i, d.kind, geom, { color: colourFor("draw"), width: E.width }));
    } else if (d.type === "tsel") {
      const sel = d.moved ? d.sel : null;
      if (d.kind === "select") {
        if (!sel && !d.moved) {             // a plain click: the page data may have arrived since pointer-down
          const L = await layout(i);
          const hit = L && L.annots.filter(a => inside(a.rect, ...start, 2)).sort((a, b) => area(a.rect) - area(b.rect))[0];
          if (hit) return selectAnnot(i, hit);
          const pic = L && pictureAt(L, start);
          if (pic) return selectPicture(i, pic);
        }
        if (!sel) return clearTmp();
        E.tsel = { i, ...sel };
        return showPill([
          { label: "Highlight", icon: "markup", run: () => markSel("highlight") },
          { label: "Underline", icon: "underline", run: () => markSel("underline") },
          { label: "Strike", icon: "strike", run: () => markSel("strikeout") },
          { label: "Copy", icon: "copy", run: copySel },
          { label: "Redact", icon: "redact", run: () => confirmRedact(i, sel.rects, sel.bbox, true) },
          ...(window.AI?.isReady() ? [{ label: "Explain", icon: "ask", run: () => { const t = E.tsel?.text; clearAll(); window.AI.explain(t, i); } }] : []),
        ], i, sel.bbox);
      }
      if (!sel) { clearTmp(); return setStatus(d.kind === "redact" ? HINTS.redact : HINTS[d.kind]); }
      if (d.kind === "redact") return confirmRedact(i, sel.rects, sel.bbox, true);
      const r = await apply(api.annotate(i, d.kind, { rects: sel.rects }, { color: colourFor(d.kind) }));
      clearTmp();
      if (r?.xref) reselect(i, r.xref);
    } else if (d.type === "move") {
      const dx = last[0] - start[0], dy = last[1] - start[1];
      if (d.moved && Math.hypot(dx, dy) > 1) {
        const res = await apply(api.annot_move(i, d.annot.xref, dx, dy));
        if (!res) { const box = ov(i).querySelector(".sel-box"); if (box) box.style.transform = ""; return; }
        E.layouts.delete(i); E.lay.delete(i);
        const L = await layout(i);
        const a = L?.annots.find(x => x.xref === d.annot.xref);
        if (a) selectAnnot(i, a);
      }
    } else if (d.type === "rect") {
      const kind = d.kind;
      if (kind === "redact") {
        clearTmp();
        if (!big) return setStatus(HINTS.redact);
        return confirmRedact(i, [r], r);
      }
      if (kind === "crop") {
        if (!big) { clearTmp(); return; }
        return showPill([
          { label: "Crop page", icon: "fit", primary: true, run: async () => { clearTmp(); await apply(api.annotate(i, "crop", { rect: r })); setTool("select"); } },
          { label: "Cancel", run: () => clearTmp() },
        ], i, r);
      }
      clearTmp();
      if (kind === "image") {
        const f = await api.pick_files("image");
        if (fail(f) || !f.paths?.length) return;
        const info = await api.image_info(f.paths[0]);
        if (fail(info)) return;
        const path = f.paths[0];
        return placer({ i, rect: big ? fitShape(r, info.aspect) : around(i, start, Math.min(200, pageSize(i).w * 0.5), info.aspect),
          src: info.preview, label: "Place picture", onPlace: rect => apply(api.annotate(i, "image", { path, rect })) });
      }
      if (kind === "link") {
        if (!big) return setStatus(HINTS.link);
        return popover({ title: "Link to (web address or email)", multiline: false, value: "https://", i, at: [r[0], r[3]], ok: "Add link",
          onOk: v => v.trim() && v.trim() !== "https://" && apply(api.annotate(i, "link", { rect: r, uri: v })) });
      }
      if (kind === "mine") return placeSignatureIn(i, big ? r : null, start);
      if (kind === "initials") return placeSignatureIn(i, big ? r : null, start, "initials");
      if (kind === "tick" || kind === "cross")
        return apply(api.annotate(i, "mark", { mark: kind, pt: start, rect: big ? r : null }, { color: colourFor("textbox"), size: 14 }));
      if (kind === "digital") {
        if (!big) return setStatus("Drag a box where the signature should appear - or use “Sign without a visible box”.");
        return window.Sign?.begin({ page: i, rect: r });
      }
    } else if (d.type === "click") {
      if (!d.moved) editTextAt(i, start);
    }
  }

  // ---- hover feedback
  function onHover(e) {
    if (E.drag || e.buttons) return;
    const div = e.target.closest(".page");
    if (!div) return;
    const i = +div.dataset.i, pt = pagePoint(e, div), L = E.lay.get(i);
    if (!L) { layout(i); return; }
    if (E.tool === "edittext") {
      const b = L.blocks.find(r => inside(r, ...pt));
      const old = ink(i).querySelector(".hover");
      if (!b) { old?.remove(); return; }
      if (old && old.dataset.k === b.join()) return;
      old?.remove();
      const n = svg("rect", { x: b[0] - 3, y: b[1] - 3, width: b[2] - b[0] + 6, height: b[3] - b[1] + 6, rx: 2, class: "hover" });
      n.dataset.k = b.join();
      ink(i).append(n);
    } else if (E.tool === "select") {
      const overWord = L.words.some(w => inside(w, ...pt, 1));
      const overAnnot = L.annots.some(a => inside(a.rect, ...pt, 2));
      div.style.cursor = overAnnot ? "pointer" : overWord ? "text" : pictureAt(L, pt) ? "zoom-in" : "";
    } else if (E.tool === "sign" && (E.sub.sign === "mine" || E.sub.sign === "initials") && !E.pending &&
               (E.sub.sign === "initials" ? E.sigs.initials : E.adhoc || E.sig)?.preview) {
      const s = E.sub.sign === "initials" ? E.sigs.initials : E.adhoc || E.sig;
      let g = ov(i).querySelector(".ghost");
      $$(".ov .ghost").forEach(n => { if (n !== g) n.remove(); });
      if (!g || g.dataset.src !== s.preview.slice(-40)) {
        g?.remove();
        g = el("img", { class: "ghost tmp-el", src: s.preview, alt: "" }); g.dataset.src = s.preview.slice(-40); ov(i).append(g);
      }
      const r = around(i, pt, E.sub.sign === "initials" ? INI_W : SIG_W, s.aspect || 3);
      Object.assign(g.style, { left: `calc(var(--z) * ${r[0]}px)`, top: `calc(var(--z) * ${r[1]}px)`,
        width: `calc(var(--z) * ${r[2] - r[0]}px)`, height: `calc(var(--z) * ${r[3] - r[1]}px)` });
    }
  }

  function onDouble(e) {
    const div = e.target.closest(".page");
    if (!div || e.target.closest(".fld")) return;
    const i = +div.dataset.i, pt = pagePoint(e, div), L = E.lay.get(i);
    if (E.tool === "edittext") { hidePill(); return openTextDialog(i, pt, "paragraph"); }
    if (E.tool !== "select" || !L) return;
    const w = L.words.find(w => inside(w, ...pt, 1));
    if (!w) return;
    const sel = { rects: [w.slice(0, 4)], text: w[4], bbox: w.slice(0, 4) };
    drawSelection(i, sel, "select");
    E.tsel = { i, ...sel };
    showPill([
      { label: "Highlight", icon: "markup", run: () => markSel("highlight") },
      { label: "Copy", icon: "copy", run: copySel },
      { label: "Fix this word", icon: "edittext", run: () => { clearAll(); openTextDialog(i, [(w[0] + w[2]) / 2, (w[1] + w[3]) / 2], "line"); } },
    ], i, sel.bbox);
  }

  // ------------------------------------------------------------------ selection actions
  async function markSel(kind) {
    const t = E.tsel;
    if (!t) return;
    clearAll();
    const r = await apply(api.annotate(t.i, kind, { rects: t.rects }, { color: E.color[kind] }));
    if (r?.xref) reselect(t.i, r.xref);            // its pill offers Add note and the colours straight away
  }
  async function copySel() {
    const t = E.tsel;
    if (!t) return;
    try { await navigator.clipboard.writeText(t.text); }
    catch {
      const ta = el("textarea", { style: "position:fixed;opacity:0" }); ta.value = t.text; document.body.append(ta);
      ta.select(); document.execCommand("copy"); ta.remove();
    }
    clearAll();
    setStatus("Copied.");
    toast("Text copied.");
  }
  /** text: the rects are over selected words (Python shrinks them to the letters, so the lines around them stay) */
  function confirmRedact(i, rects, bbox, text = false) {
    drawSelection(i, { rects }, "redact");
    showPill([
      { label: "Black out", icon: "redact", danger: true, run: async () => { clearTmp(); await apply(api.annotate(i, "redact", { rects, text })); } },
      { label: "Erase (leave blank)", icon: "redact", danger: true, run: async () => { clearTmp(); await apply(api.annotate(i, "redact", { rects, text, white: true })); } },
      { label: "Cancel", run: () => clearAll() },
    ], i, bbox);
  }

  // ---- annotations
  function selectAnnot(i, a) {
    $$(".ov .sel-box").forEach(n => n.remove());
    E.sel = { i, a };
    ov(i).append(el("div", { class: "sel-box" + (a.movable ? " movable" : ""), style: pct(i, a.rect) }));
    const items = [];
    if (a.markup) {                // a highlight / underline / strike-through: its note and its colour
      if (a.note) items.push({ note: a.note }, "-");
      items.push({ label: a.note ? "Edit note" : "Add note", icon: "note", primary: true, run: () => editAnnotNote(i, a) });
      items.push({ swatches: a.kind === "highlight" ? PALETTE.highlight : PALETTE.underline, current: a.color, label: "Colour",
        run: c => apply(api.annot_colour(i, a.xref, c)).then(r => r && reselect(i, a.xref)) });
      if (a.note) items.push({ label: "Delete note", icon: "x", run: () => apply(api.annot_note(i, a.xref, "")) });
    }
    if (a.resizable) items.push({ label: "Move or resize", icon: "fit", primary: true, run: () => adjustAnnot(i, a) });
    if (a.editable) items.push({ label: "Edit text", icon: "edittext", run: () => editAnnotText(i, a) });
    if ((a.kind === "initials" || a.kind === "mark" || a.kind === "signature") && S.doc.pages.length > 1)
      items.push({ label: "Copy to every page", icon: "copy", run: () => { clearAll(); apply(api.place_on_every_page(i, a.xref)); } });
    items.push({ label: `Delete ${a.kind}`, icon: "trash", danger: true, run: () => deleteSel() });
    if (a.movable && !a.resizable) items.push("-", { chip: "Drag to move" });
    showPill(items, i, a.rect);
    setStatus(a.markup ? `${a.kind[0].toUpperCase() + a.kind.slice(1)} selected - add a note to it, change its colour, or press Delete to remove it.`
      : `${a.kind[0].toUpperCase() + a.kind.slice(1)} selected - Delete removes it${a.movable ? ", drag to move it" : ""}.`);
  }
  /** After a change: pick the item up again (from the page's fresh layout), so its pill shows what it is now. */
  async function reselect(i, xref) {
    const L = await layout(i);
    const a = L?.annots.find(x => x.xref === xref);
    if (a) selectAnnot(i, a);
  }
  function editAnnotNote(i, a) {
    popover({ title: a.note ? "Edit note" : `Note on this ${a.kind}`, value: a.note, i, at: [a.rect[0], a.rect[3]], ok: "Save",
      placeholder: "What you want to remember about these words",
      onOk: v => { clearAll(); return apply(api.annot_note(i, a.xref, v)); } });
  }
  /** A small note sign at the corner of each highlight that has a note; point at it to read the note, click to change it. */
  function noteBadges(i, L) {
    const o = ov(i);
    if (!o) return;
    o.querySelectorAll(".hl-note").forEach(n => n.remove());
    for (const a of L.annots.filter(x => x.markup && x.note)) {
      const p = pageSize(i);
      o.append(el("button", { class: "hl-note", type: "button", title: a.note, "aria-label": `Note: ${a.note}`,
        style: `left:${a.rect[2] / p.w * 100}%;top:${a.rect[1] / p.h * 100}%`,
        onpointerdown: e => { e.stopPropagation(); e.preventDefault(); clearAll(); selectAnnot(i, a); } }, icon("note")));
    }
  }
  async function deleteSel() {
    const s = E.sel;
    if (!s) return;
    clearAll();
    await apply(api.annot_delete(s.i, s.a.xref));
  }
  function editAnnotText(i, a) {
    popover({ title: a.kind === "note" ? "Edit note" : "Edit text", value: a.text, i, at: [a.rect[0], a.rect[3]], ok: "Save",
      onOk: v => { clearAll(); return apply(api.annot_text(i, a.xref, v)); } });
  }

  // ------------------------------------------------------------------ text box typed straight onto the page
  function inlineText(i, pt) {
    const ta = el("textarea", { class: "tb-edit tmp-el", "aria-label": "New text", rows: 1, spellcheck: "true" });
    Object.assign(ta.style, { left: `calc(var(--z) * ${pt[0]}px)`, top: `calc(var(--z) * ${pt[1]}px)`,
      fontSize: `calc(var(--z) * ${E.size}px)`, fontFamily: FONT_CSS[E.font], color: colourFor("textbox"),
      fontWeight: E.font.includes("Bold") ? "700" : "400" });
    const grow = () => { ta.style.height = "auto"; ta.style.height = ta.scrollHeight + "px"; ta.style.width = "auto";
      ta.style.width = Math.max(ta.scrollWidth + 4, 40) + "px"; };
    ta.addEventListener("input", grow);
    let done = false;
    const commit = async () => {
      if (done) return; done = true;
      E.pending = null;
      const text = ta.value;
      ta.remove();
      if (text.trim()) await apply(api.annotate(i, "textbox", { pt, text }, { color: colourFor("textbox"), size: E.size, font: E.font }));
    };
    const discard = () => { done = true; E.pending = null; ta.remove(); };
    ta.addEventListener("keydown", e => {
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); discard(); }
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); commit(); }
    });
    ta.addEventListener("blur", () => setTimeout(commit, 0));
    ov(i).append(ta);
    E.pending = { commit, cancel: discard };
    setTimeout(() => { ta.focus(); grow(); });
    setStatus(keys("Type your text - click outside the box (or Ctrl+Enter) when you're done, Esc to cancel."));
  }

  // ------------------------------------------------------------------ edit existing words
  async function editTextAt(i, pt) {
    const info = await api.text_info(i, ...pt);
    if (fail(info)) return;
    if (info.none && info.scan) return scanTextAt(i, pt);          // words in a picture: a scan, a photo of a letter
    if (info.none) return setStatus("There's no text there. To add new words, use Text → Text box.");
    E.target = { i, pt, info };
    clearTmp();
    const b = (info.block || info.span).bbox;
    tmp(i, svg("rect", { x: b[0] - 3, y: b[1] - 3, width: b[2] - b[0] + 6, height: b[3] - b[1] + 6, rx: 2, class: "target" }));
    const f = info.font, blk = info.block;
    const chip = `${f.family} · ${(blk || info.span).size} pt${blk ? " · " + blk.align_name : ""}`;
    showPill([
      ...(blk ? [{ label: "Edit paragraph", icon: "edittext", primary: true, run: () => openTextDialog(i, pt, "paragraph", info) }] : []),
      ...(info.span ? [{ label: "Fix this line", icon: "text", run: () => openTextDialog(i, pt, "line", info) }] : []),
      { label: "Comment", icon: "note", run: () => popover({ title: "Comment", i, at: [b[2], b[1]], ok: "Add comment",
        onOk: v => v.trim() && apply(api.annotate(i, "note", { pt: [b[2] + 4, b[1]], text: v }, { color: "#f5c400" })) }) },
      { label: "Redact", icon: "redact", run: () => confirmRedact(i, [b], b, true) },
      ...(window.AI?.isReady() ? [{ label: "Explain", icon: "ask", run: () => { clearAll(); window.AI.explain((blk || info.span).text, i); } }] : []),
      "-",
      { chip, kind: f.kind === "builtin" ? "warn" : "ok" },
    ], i, b);
  }

  // dialog: edit paragraph / fix line  (Concept A's font chip, text shown in the real font)
  let textDlg = null;
  function textDialog() {
    if (textDlg) return textDlg;
    textDlg = el("dialog", { class: "dialog text-dlg" });
    textDlg.innerHTML = `
      <form method="dialog" novalidate>
        <div class="td-head"><h3 class="td-title">Edit paragraph</h3><span class="muted td-where"></span>
          <button class="icon-btn sm td-x" value="cancel" aria-label="Close"><svg><use href="#i-x"/></svg></button></div>
        <div class="chips">
          <button type="button" class="chip td-font" aria-haspopup="menu"></button>
          <label class="chip">Size <input class="td-size" type="number" min="4" max="96" step="any"></label>
          <label class="chip td-align-wrap">Align <select class="td-align">${ALIGNS.map((a, k) => `<option value="${k}">${a}</option>`).join("")}</select></label>
          <span class="chip static td-lh"></span>
          <span class="muted small td-note">detected from the page - change any of them</span>
        </div>
        <div class="td-edit"><textarea class="td-text" spellcheck="true" aria-label="Text"></textarea>
          <input class="td-line" spellcheck="true" aria-label="Text">
          <div class="td-fit"><svg><use href="#i-check"/></svg><span class="td-fit-msg"></span><span class="td-count muted"></span></div>
        </div>
        <div class="dlg-actions">
          <label class="td-keep"><input type="checkbox" class="td-keep-cb"> Keep the original wording as a comment</label>
          <span class="spacer"></span><span class="muted small td-kbd">${keys("Ctrl ↵ to apply")}</span>
          <button class="btn btn-ghost" value="cancel">Cancel</button>
          <button class="btn btn-primary td-ok" value="ok">Replace paragraph</button>
        </div>
      </form>`;
    document.body.append(textDlg);
    return textDlg;
  }

  let fontSeq = 0;
  async function previewFont(f) {
    if (!f.url) return f.fallback;
    try {
      const name = "tkdoc" + (++fontSeq);
      const face = new FontFace(name, `url(${f.url})`);
      await face.load();
      document.fonts.add(face);
      return `"${name}", ${f.fallback}`;
    } catch { return f.fallback; }
  }

  async function openTextDialog(i, pt, kind, info) {
    info = info || await api.text_info(i, ...pt);
    if (fail(info)) return;
    if (info.none || (kind === "paragraph" && !info.block) || (kind === "line" && !info.span))
      return setStatus("There's no text there. To add new words, use Text → Text box.");
    clearTmp(); hidePill();
    const d = textDialog(), q = s => d.querySelector(s);
    const para = kind === "paragraph", src = para ? info.block : info.span, f = info.font;
    let font = "original", standIn = null;   // standIn: the page's font hasn't got a letter the new words need
    q(".td-title").textContent = para ? "Edit paragraph" : "Fix this line";
    q(".td-where").textContent = `page ${i + 1}`;
    q(".td-ok").textContent = para ? "Replace paragraph" : "Replace line";
    q(".td-text").hidden = !para; q(".td-line").hidden = para;
    q(".td-fit").hidden = !para; q(".td-keep").hidden = !para; q(".td-kbd").hidden = !para;
    q(".td-align-wrap").hidden = !para; q(".td-lh").hidden = !para;
    const input = para ? q(".td-text") : q(".td-line");
    input.value = src.text;
    q(".td-size").value = src.size;
    if (para) { q(".td-align").value = String(src.align); q(".td-lh").textContent = `Line height ${src.lineheight}`; }
    q(".td-keep-cb").checked = false;
    const chip = q(".td-font");
    const setChip = () => {
      const orig = font === "original";
      if (orig && standIn) {
        chip.className = "chip td-font warn";
        chip.replaceChildren(icon("alert"), `${standIn.name} · ${f.family} here hasn't got ${standIn.missing || "every letter"}`, icon("down"));
        chip.title = `This PDF holds only the letters it uses. The new words will be written in ${standIn.name}.`;
        return;
      }
      chip.title = "";
      const cls = !orig ? "chip td-font" : f.kind === "builtin" ? "chip td-font warn" : "chip td-font ok";
      chip.className = cls;
      chip.replaceChildren(icon(orig && f.kind === "builtin" ? "alert" : "check"),
        orig ? (f.kind === "embedded" ? `Original font · ${f.family}, embedded`
          : f.kind === "standard" ? `Original font · ${f.family}, standard PDF font`
          : f.kind === "installed" ? here(`Original font · ${f.family}, installed on this PC`)
            : `Closest match · original ${f.family} isn't available`) : font,
        icon("down"));
    };
    setChip();
    const css = await previewFont(f);
    const applyLook = () => {
      input.style.fontFamily = font === "original" ? (standIn ? FONT_CSS[standIn.name] || f.fallback : css) : FONT_CSS[font];
      input.style.fontWeight = (font === "original" ? f.bold : font.includes("Bold")) ? "700" : "400";
      input.style.fontStyle = font === "original" && f.italic ? "italic" : "normal";
      input.style.color = src.color;
      input.style.textAlign = para ? ["left", "center", "right", "justify"][+q(".td-align").value] : "left";
      input.style.letterSpacing = para && src.tracking ? `${src.tracking}em` : "normal";   // a heading set tight stays tight
      input.style.fontSize = clamp(+q(".td-size").value * 1.3, 12, 26) + "px";
    };
    applyLook();
    chip.onclick = () => showMenu([
      { label: "Original font", checked: font === "original", run: () => { font = "original"; standIn = null; setChip(); applyLook(); check(); } },
      "-", ...Object.keys(FONT_CSS).map(n => ({ label: n, checked: font === n, run: () => { font = n; setChip(); applyLook(); check(); } })),
    ], 0, 0, chip);

    let t = 0, fits = true;
    const fitMsg = q(".td-fit-msg"), fitBox = q(".td-fit");
    const check = () => {
      applyLook();
      if (!para) return;
      q(".td-count").textContent = `${input.value.length} characters`;
      clearTimeout(t);
      t = setTimeout(async () => {
        const r = await api.paragraph_fit(i, ...pt, input.value, +q(".td-size").value, +q(".td-align").value, font);
        if (r.error) return;
        if (font === "original" && JSON.stringify(r.font || null) !== JSON.stringify(standIn)) {
          standIn = r.font || null; setChip(); applyLook();
        }
        fits = r.used != null;
        fitBox.className = "td-fit " + (r.fits ? "ok" : fits ? "warn" : "bad");
        fitBox.querySelector("use").setAttribute("href", r.fits ? "#i-check" : "#i-alert");
        fitMsg.textContent = r.fits ? (r.spare_lines > 0 ? `Fits the original box with ${r.spare_lines} line${r.spare_lines > 1 ? "s" : ""} to spare.` : "Fits the original box.")
          : fits ? `A little long - it will shrink to ${r.used} pt to fit the box.`
            : "Too long to fit in this paragraph's box. Shorten it or choose a smaller size.";
        q(".td-ok").disabled = !fits;
      }, 300);
    };
    input.oninput = check; q(".td-size").oninput = check; q(".td-align").onchange = check;
    input.onkeydown = e => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey || !para)) { e.preventDefault(); d.close("ok"); } };
    q(".td-ok").disabled = false;
    fitBox.className = "td-fit ok"; fitMsg.textContent = "Checking…";
    check();
    d.returnValue = "";
    d.showModal();
    input.focus();
    if (!para) input.select();
    d.addEventListener("close", async () => {
      clearTimeout(t);
      if (d.returnValue !== "ok") return;
      const size = clamp(+q(".td-size").value || src.size, 4, Math.max(96, src.size));   // the page's own size (21.64, 120) is fine
      if (para) await apply(api.edit_paragraph(i, ...pt, input.value, size, +q(".td-align").value, font, q(".td-keep-cb").checked));
      else await apply(api.fix_text(i, ...pt, input.value, size, font));
    }, { once: true });
  }

  // ------------------------------------------------------------------ change a word in a scan
  // A scanned page is a picture of the paper: its words are read here on this computer, and the one picked is
  // painted out of the picture and drawn again to match (the iPad's Fix this word / Fix this line).
  // Python side: bridge_scanedit.py.
  let scanSeq = 0;
  async function scanTextAt(i, pt) {
    const seq = ++scanSeq;
    clearTmp(); hidePill();
    setStatus("Reading the words…");
    const r = await api.scan_spot(i, ...pt);
    if (seq !== scanSeq) return;                 // another click since
    setStatus(HINTS.edittext || "");
    if (fail(r)) return;
    if (r.none) return setStatus("There's no text there. To add new words, use Text → Text box.");
    if (r.cant) return setStatus(r.cant);
    E.target = { i, pt, scan: r };
    const b = r.word_bbox;
    tmp(i, svg("rect", { x: b[0] - 3, y: b[1] - 3, width: b[2] - b[0] + 6, height: b[3] - b[1] + 6, rx: 2, class: "target" }));
    showPill([
      { label: "Fix this word", icon: "edittext", primary: true, run: () => openScanDialog(i, "word", r) },
      ...(r.line ? [{ label: "Fix this line", icon: "text", run: () => openScanDialog(i, "line", r) }] : []),
      "-",
      { chip: `Scan · closest match ${r.face} · ${r.size} pt`, kind: "warn" },
    ], i, b);
    setStatus("This page is a scan: the new words are drawn into its picture to match the old ones.");
  }

  let scanDlg = null;
  function scanDialog() {
    if (scanDlg) return scanDlg;
    scanDlg = el("dialog", { class: "dialog text-dlg scan-dlg" });
    scanDlg.innerHTML = `
      <form method="dialog" novalidate>
        <div class="td-head"><h3 class="sd-title">Fix this word</h3><span class="muted sd-where"></span>
          <button class="icon-btn sm td-x" value="cancel" aria-label="Close"><svg><use href="#i-x"/></svg></button></div>
        <div class="sd-look-h">How it will look</div>
        <div class="sd-look"><img class="sd-img" alt="How it will look"><span class="sd-wait muted small">Drawing it…</span></div>
        <div class="td-edit"><input class="td-line sd-text" spellcheck="false" autocomplete="off" aria-label="Text">
          <div class="td-fit sd-msg" hidden><svg><use href="#i-alert"/></svg><span class="sd-msg-text"></span></div>
        </div>
        <div class="chips sd-chips">
          <button type="button" class="chip sd-font" aria-haspopup="menu"></button>
          <label class="chip">Size <input class="sd-size" type="number" min="4" max="96" step="any"></label>
        </div>
        <p class="muted small sd-note">This page is a scan - a picture of the paper - so the new words are drawn into the picture to
          match the old ones as nearly as can be. Check how it looks above. The old words are taken out of the file, not covered.</p>
        <div class="dlg-actions">
          <span class="spacer"></span>
          <button class="btn btn-ghost" value="cancel">Cancel</button>
          <button class="btn btn-primary sd-ok" value="ok">Replace word</button>
        </div>
      </form>`;
    document.body.append(scanDlg);
    return scanDlg;
  }

  function openScanDialog(i, kind, spot) {
    clearTmp(); hidePill();
    const d = scanDialog(), q = s => d.querySelector(s);
    const noun = kind === "word" ? "word" : "line";
    let face = null;                             // null: the font found nearest to the scan's
    q(".sd-title").textContent = `Fix this ${noun}`;
    q(".sd-where").textContent = `page ${i + 1} · scan`;
    q(".sd-ok").textContent = `Replace ${noun}`;
    const input = q(".sd-text"), size = q(".sd-size"), img = q(".sd-img"), msg = q(".sd-msg"), chip = q(".sd-font");
    input.value = kind === "word" ? spot.word : (spot.line || spot.word);
    size.value = spot.size;
    img.removeAttribute("src"); img.hidden = true; q(".sd-wait").hidden = false;
    msg.hidden = true;
    const setChip = () => chip.replaceChildren(icon("text"), face || `Closest match · ${spot.face}`, icon("down"));
    setChip();
    chip.onclick = () => showMenu([
      { label: `Closest match · ${spot.face}`, checked: !face, run: () => { face = null; setChip(); show(0); } },
      "-", ...spot.faces.map(n => ({ label: n, checked: face === n, run: () => { face = n; setChip(); show(0); } })),
    ], 0, 0, chip);
    const asked = () => [kind, input.value, face, +size.value || null];

    let t = 0, seq = 0, open = true;
    const show = (wait = 300) => {               // redrawn at a pause in the typing
      clearTimeout(t);
      t = setTimeout(async () => {
        const mine = ++seq;
        const r = await api.scan_show(...asked());
        if (!open || mine !== seq) return;
        if (r.error) { msg.hidden = false; msg.className = "td-fit sd-msg bad"; q(".sd-msg-text").textContent = r.error; q(".sd-ok").disabled = true; return; }
        if (r.image) { img.src = r.image; img.hidden = false; q(".sd-wait").hidden = true; }
        q(".sd-ok").disabled = !!r.problem;
        msg.hidden = !r.problem && !r.squeezed;
        msg.className = "td-fit sd-msg " + (r.problem ? "bad" : "warn");
        q(".sd-msg-text").textContent = r.problem || (!r.squeezed ? "" : kind === "word" && spot.line
          ? "A little long - the letters are set closer to fit before the next word. Fix this line gives more room."
          : "A little long - the letters are set closer to fit.");
      }, wait);
    };
    input.oninput = () => show(); size.oninput = () => show();
    input.onkeydown = e => { if (e.key === "Enter") { e.preventDefault(); if (!q(".sd-ok").disabled) d.close("ok"); } };
    q(".sd-ok").disabled = false;
    show(0);
    d.returnValue = "";
    d.showModal();
    input.focus(); input.select();
    d.addEventListener("close", async () => {
      open = false; clearTimeout(t);
      if (d.returnValue !== "ok") return;
      await apply(api.scan_replace(...asked()));
    }, { once: true });
  }

  // ------------------------------------------------------------------ your own signature
  async function ensureSignature(slot = "signature") {
    if (E.sigs[slot] === undefined) {
      const r = await api.signature_get(slot);
      E.sigs[slot] = r.signature ? r : null;
    }
    E.sig = E.sigs.signature;
    if (E.sigs[slot]) { setStatus(slot === "initials" ? HINTS.initials : HINTS.mine); return true; }
    return openSignaturePad({ slot });
  }

  async function currentSignature(slot = "signature") {
    if (slot === "signature" && E.adhoc) return E.adhoc;
    return (await ensureSignature(slot)) ? E.sigs[slot] : null;
  }

  /** Drop the signature as an adjustable box; it becomes part of the page when placed. */
  async function placeSignatureIn(i, rect, pt, slot = "signature", onLine = false) {
    const s = await currentSignature(slot);
    if (!s) return;
    $$(".ov .ghost").forEach(n => n.remove());
    const aspect = s.aspect || 3, ini = slot === "initials";
    const big = rect && rect[2] - rect[0] > 8 && rect[3] - rect[1] > 8;
    const r = big && onLine ? onLineShape(rect, aspect) : big ? fitShape(rect, aspect) : around(i, pt || [(rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2], ini ? INI_W : SIG_W, aspect);
    const spec = ini ? null : E.adhoc?.spec;
    placer({ i, rect: r, src: s.preview, label: ini ? "Place initials" : "Place signature",
      onPlace: async rect => {
        const res = await apply(api.annotate(i, "signature", { rect, sig: spec, slot }));
        if (res && ini && S.doc.pages.length > 1)
          toast("Initials added.", { actions: [{ label: `Add to all ${S.doc.pages.length} pages`, run: () => apply(api.place_on_every_page(i, res.xref)) }] });
      } });
  }

  // ------------------------------------------------------------------ placer: move / resize before it's fixed
  const SIG_W = 160, INI_W = 60;
  function fitShape(r, aspect) {          // largest box of this shape inside r, centred
    let w = r[2] - r[0], h = r[3] - r[1];
    if (w / h > aspect) w = h * aspect; else h = w / aspect;
    const cx = (r[0] + r[2]) / 2, cy = (r[1] + r[3]) / 2;
    return [cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2];
  }
  function onLineShape(r, aspect) {      // largest box of this shape inside r, sitting on its bottom-left (a signing line)
    let w = r[2] - r[0], h = r[3] - r[1];
    if (w / h > aspect) w = h * aspect; else h = w / aspect;
    return [r[0], r[3] - h, r[0] + w, r[3]];
  }
  function around(i, pt, w, aspect) {     // a box of width w centred on pt, kept on the page
    const p = pageSize(i);
    w = Math.min(w, p.w * 0.9);
    const h = w / aspect;
    const x = clamp(pt[0] - w / 2, 0, p.w - w), y = clamp(pt[1] - h / 2, 0, p.h - h);
    return [x, y, x + w, y + h];
  }

  function placer({ i, rect, src, keepShape = true, label = "Place", onPlace, onCancel }) {
    E.pending?.cancel();
    const p = pageSize(i);
    const st = { rect: rect.slice() };
    const aspect = (rect[2] - rect[0]) / (rect[3] - rect[1]);
    const box = el("div", { class: "placer tmp-el", tabindex: "0", role: "group",
      "aria-label": "Drag to move. Drag a corner to resize. Arrow keys nudge, + and - resize, Enter places it." },
      el("img", { src, alt: "", draggable: "false" }),
      ...["nw", "ne", "sw", "se"].map(c => el("span", { class: "handle h-" + c, "data-c": c })));
    const show = () => {
      const r = st.rect;
      Object.assign(box.style, { left: `calc(var(--z) * ${r[0]}px)`, top: `calc(var(--z) * ${r[1]}px)`,
        width: `calc(var(--z) * ${r[2] - r[0]}px)`, height: `calc(var(--z) * ${r[3] - r[1]}px)` });
      E.anchor = { i, rect: st.rect };
      placePill();
    };
    const resizeBy = f => {
      const r = st.rect, cx = (r[0] + r[2]) / 2, cy = (r[1] + r[3]) / 2;
      const w = clamp((r[2] - r[0]) * f, 12, p.w), h = keepShape ? w / aspect : clamp((r[3] - r[1]) * f, 8, p.h);
      st.rect = [cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2];
      show();
    };
    box.addEventListener("pointerdown", e => {
      e.preventDefault(); e.stopPropagation();
      box.focus({ preventScroll: true });
      const c = e.target.dataset.c, start = pagePoint(e, pageDiv(i)), r0 = st.rect.slice();
      const move = ev => {
        const pt = pagePoint(ev, pageDiv(i)), dx = pt[0] - start[0], dy = pt[1] - start[1];
        if (!c) {
          const w = r0[2] - r0[0], h = r0[3] - r0[1];
          const x = clamp(r0[0] + dx, 0, p.w - w), y = clamp(r0[1] + dy, 0, p.h - h);
          st.rect = [x, y, x + w, y + h];
        } else {                                    // the opposite corner stays put
          const ax = c.includes("w") ? r0[2] : r0[0], ay = c.includes("n") ? r0[3] : r0[1];
          let w = Math.max(12, Math.abs(pt[0] - ax)), h = Math.max(8, Math.abs(pt[1] - ay));
          if (keepShape) { if (w / h > aspect) w = h * aspect; else h = w / aspect; }
          const x0 = c.includes("w") ? ax - w : ax, y0 = c.includes("n") ? ay - h : ay;
          st.rect = [x0, y0, x0 + w, y0 + h];
        }
        show();
      };
      const up = () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up); };
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", up);
    });
    box.addEventListener("keydown", e => {
      const step = e.shiftKey ? 10 : 1;
      const d = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] }[e.key];
      if (d) {
        const r = st.rect, w = r[2] - r[0], h = r[3] - r[1];
        const x = clamp(r[0] + d[0], 0, p.w - w), y = clamp(r[1] + d[1], 0, p.h - h);
        st.rect = [x, y, x + w, y + h]; show();
      } else if (e.key === "+" || e.key === "=") resizeBy(1.08);
      else if (e.key === "-") resizeBy(1 / 1.08);
      else if (e.key === "Enter") place();
      else if (e.key === "Escape") cancel();
      else return;
      e.preventDefault(); e.stopPropagation();
    });
    let done = false;
    const finish = () => { done = true; E.pending = null; box.remove(); hidePill(); };
    const place = async () => { if (done) return; finish(); await onPlace(st.rect); };
    const cancel = () => { if (done) return; finish(); onCancel?.(); };
    E.pending = { commit: place, cancel };
    ov(i).append(box);
    showPill([
      { label, icon: "check", primary: true, run: place },
      { label: "Cancel", run: cancel },
      "-", { chip: "Drag to move · pull a corner to resize" },
    ], i, st.rect);
    show();
    box.focus({ preventScroll: true });
    setStatus(`Drag to move it, pull a corner to resize, then press ${label} (or Enter).`);
  }

  /** Pick up a placed signature / picture / box and adjust it; Cancel puts it back. */
  async function adjustAnnot(i, a) {
    clearAll();
    const r = await api.annot_pickup(i, a.xref);
    if (fail(r)) return;
    refreshDoc(r);
    placer({ i, rect: r.rect, src: r.image, keepShape: a.keep_shape, label: "Done",
      onPlace: rect => apply(api.annot_place(i, a.xref, rect)),
      onCancel: async () => { const u = await api.undo(); if (!fail(u)) { refreshDoc(u, null); setStatus("Left as it was."); } } });
  }

  let padDlg = null;
  function signaturePad() {
    if (padDlg) return padDlg;
    padDlg = el("dialog", { class: "dialog sig-dlg" });
    padDlg.innerHTML = `
      <form method="dialog">
        <div class="td-head"><h3 class="sig-title">Your signature</h3><span class="muted sig-sub"></span>
          <button class="icon-btn sm td-x" value="cancel" aria-label="Close"><svg><use href="#i-x"/></svg></button></div>
        <div class="seg sig-tabs" role="tablist">
          <button type="button" role="tab" data-tab="draw"><svg><use href="#i-draw"/></svg>Draw</button>
          <button type="button" role="tab" data-tab="type"><svg><use href="#i-text"/></svg>Type</button>
          <button type="button" role="tab" data-tab="upload"><svg><use href="#i-image"/></svg>Picture</button>
        </div>
        <div class="sig-panel" data-panel="draw">
          <canvas class="sig-pad" width="1040" height="400" aria-label="Draw your signature here"></canvas>
          <div class="sig-under"><span class="muted small">Sign with your mouse, touchpad or pen</span><span class="spacer"></span>
            <button type="button" class="btn btn-ghost sm sig-clear">Clear</button></div>
        </div>
        <div class="sig-panel" data-panel="type">
          <label class="field"><span>Your name</span><input class="sig-name" autocomplete="name"></label>
          <div class="sig-styles" role="radiogroup" aria-label="Style"></div>
          <p class="muted small sig-styles-note"></p>
        </div>
        <div class="sig-panel" data-panel="upload">
          <div class="sig-upload"><img class="sig-up-prev" alt="" hidden>
            <p class="muted small">Sign a piece of white paper, take a photo, then choose it here. The paper is made see-through automatically.</p>
            <button type="button" class="btn btn-outline sig-choose"><svg><use href="#i-open"/></svg>Choose a picture…</button></div>
        </div>
        <div class="dlg-actions">
          <div class="ink-colours" role="radiogroup" aria-label="Ink colour"></div>
          <span class="spacer"></span>
          <label class="check sig-remember-row"><input type="checkbox" class="sig-remember"><span>${here("Remember on this PC")}</span></label>
          <button class="btn btn-ghost" value="cancel">Cancel</button>
          <button class="btn btn-primary sig-ok" value="ok">Use signature</button>
        </div>
      </form>`;
    document.body.append(padDlg);
    return padDlg;
  }

  async function openSignaturePad({ remember = true, slot = "signature" } = {}) {
    const d = signaturePad(), q = s => d.querySelector(s);
    const ini = slot === "initials";
    if (ini) remember = true;
    q(".sig-remember").checked = remember;
    q(".sig-remember-row").hidden = ini;
    q(".sig-title").textContent = ini ? "Your initials" : remember ? "Your signature" : "One-off signature";
    q(".sig-sub").textContent = ini ? here("saved on this PC, like your signature") : remember ? "draw, type or use a photo" : "used for this document only - not saved";
    q(".sig-remember").onchange = () => {
      const r = q(".sig-remember").checked;
      q(".sig-title").textContent = r ? "Your signature" : "One-off signature";
      q(".sig-sub").textContent = r ? "draw, type or use a photo" : "used for this document only - not saved";
    };
    const st = { tab: "draw", colour: "#1a237e", strokes: [], font: null, upload: null };
    const inks = [["#1a237e", "Navy"], ["#111827", "Black"], ["#1d4ed8", "Blue"]];
    const setTab = tab => {
      st.tab = tab;
      d.querySelectorAll("[data-tab]").forEach(b => b.setAttribute("aria-selected", String(b.dataset.tab === tab)));
      d.querySelectorAll("[data-panel]").forEach(p => { p.hidden = p.dataset.panel !== tab; });
      q(".ink-colours").hidden = tab === "upload";
      if (tab === "type") q(".sig-name").focus();
    };
    d.querySelectorAll("[data-tab]").forEach(b => { b.onclick = () => setTab(b.dataset.tab); });
    // ink colours
    const drawColours = () => q(".ink-colours").replaceChildren(el("span", { class: "muted small", text: "Ink" }), ...inks.map(([c, n]) =>
      el("button", { type: "button", class: "swatch", style: `--c:${c}`, role: "radio", "aria-checked": String(st.colour === c), "aria-label": n, title: n,
        onclick: () => { st.colour = c; drawColours(); redraw(); styleButtons(); } })));
    // draw pad
    const cv = q(".sig-pad"), ctx = cv.getContext("2d");
    const redraw = () => {
      ctx.clearRect(0, 0, cv.width, cv.height);
      ctx.strokeStyle = "#cbd5e1"; ctx.setLineDash([8, 8]); ctx.lineWidth = 2;
      ctx.beginPath(); ctx.moveTo(60, 320); ctx.lineTo(980, 320); ctx.stroke(); ctx.setLineDash([]);
      ctx.strokeStyle = st.colour; ctx.lineWidth = 5; ctx.lineCap = "round"; ctx.lineJoin = "round";
      for (const s of st.strokes) { ctx.beginPath(); s.forEach(([x, y], k) => k ? ctx.lineTo(x * cv.width, y * cv.height) : ctx.moveTo(x * cv.width, y * cv.height)); ctx.stroke(); }
    };
    let cur = null;
    const at = e => { const b = cv.getBoundingClientRect(); return [(e.clientX - b.left) / b.width, (e.clientY - b.top) / b.height]; };
    cv.onpointerdown = e => { cv.setPointerCapture(e.pointerId); cur = [at(e)]; st.strokes.push(cur); };
    cv.onpointermove = e => { if (cur) { cur.push(at(e)); redraw(); } };
    cv.onpointerup = () => { if (cur && cur.length < 2) st.strokes.pop(); cur = null; redraw(); };
    q(".sig-clear").onclick = () => { st.strokes = []; redraw(); };
    // typed styles - each button previews itself in its font
    const fonts = (await api.signature_fonts()).fonts || [];
    const faces = [];
    for (const f of fonts) {
      try { const face = new FontFace("tksig-" + faces.length, `url(${f.url})`); await face.load(); document.fonts.add(face); faces.push({ ...f, css: `"tksig-${faces.length}"` }); }
      catch { /* unusable font */ }
    }
    st.font = faces[0]?.id || null;
    const styleButtons = () => {
      const name = q(".sig-name").value.trim() || "Your name";
      q(".sig-styles").replaceChildren(...(faces.length ? faces : [{ id: null, label: "Italic", css: "'Times New Roman', serif" }]).map(f =>
        el("button", { type: "button", class: "sig-style", role: "radio", "aria-checked": String(st.font === f.id), title: f.label,
          style: `font-family:${f.css};color:${st.colour}${f.id ? "" : ";font-style:italic"}`, onclick: () => { st.font = f.id; styleButtons(); } }, name)));
      q(".sig-styles-note").textContent = faces.length ? here(`Styles come from handwriting fonts on this PC: `) + `${faces.slice(0, 3).map(f => f.label).join(", ")}${faces.length > 3 ? "…" : ""}`
        : here("No handwriting fonts were found on this PC, so a plain italic is used.");
    };
    q(".sig-name").oninput = styleButtons;
    // picture
    q(".sig-choose").onclick = async () => {
      const r = await api.pick_files("image");
      if (fail(r) || !r.paths?.length) return;
      const p = await api.signature_preview({ type: "image", path: r.paths[0] });
      if (fail(p)) return;
      st.upload = r.paths[0];
      const img = q(".sig-up-prev"); img.src = p.preview; img.hidden = false;
    };
    const ident = await api.identity_get();
    const fullName = ident.name || ident.profile_name || E.sigs.signature?.signature?.name || "";
    const initialsOf = n => n.split(/\s+/).filter(Boolean).map(w => w[0].toUpperCase()).join(".") + (n.trim() ? "." : "");
    q(".sig-name").value = ini ? (E.sigs.initials?.signature?.name || initialsOf(fullName)) : (E.sig?.signature?.name || fullName);
    d.querySelector(".sig-panel[data-panel=type] .field span").textContent = ini ? "Your initials" : "Your name";
    drawColours(); redraw(); styleButtons();
    const curType = (ini ? E.sigs.initials : E.sig)?.signature?.type;
    setTab(curType === "typed" ? "type" : curType === "image" ? "upload" : "draw");
    d.returnValue = "";
    d.showModal();
    return new Promise(res => {
      const onClose = async () => {
        if (d.returnValue !== "ok") return res(false);
        const sig = st.tab === "draw" ? { type: "ink", colour: st.colour, strokes: st.strokes.map(s => s.map(([x, y]) => [x, y])) }
          : st.tab === "type" ? { type: "typed", name: q(".sig-name").value, font: st.font, colour: st.colour }
            : { type: "image", path: st.upload };
        if (st.tab === "upload" && !st.upload) { toast("Choose a picture of your signature first.", { error: true }); d.showModal(); return d.addEventListener("close", onClose, { once: true }); }
        if (q(".sig-remember").checked) {
          const r = await api.signature_set(sig, slot);
          if (fail(r)) { d.showModal(); return d.addEventListener("close", onClose, { once: true }); }
          E.sigs[slot] = r;
          if (!ini) { E.sig = r; E.adhoc = null; }
          setStatus(`${ini ? "Initials" : "Signature"} saved. Now click where ${ini ? "they go" : "it goes"} on the page, or drag a box.`);
        } else {
          const r = await api.signature_preview(sig);
          if (fail(r)) { d.showModal(); return d.addEventListener("close", onClose, { once: true }); }
          E.adhoc = { spec: sig, preview: r.preview, aspect: r.aspect };
          setStatus("One-off signature ready (not saved). Click where it goes, or drag a box for it.");
        }
        if (E.tool === "sign") buildSubbar();
        res(true);
      };
      d.addEventListener("close", onClose, { once: true });
    });
  }

  // ------------------------------------------------------------------ pictures in the page (bridge_pictures.py)
  function pictureAt(L, pt) {             // the smallest picture under the point; a page-sized one (a scan) only by its menu
    return (L.images || []).filter(p => !p.page_sized && inside(p.rect, ...pt)).sort((a, b) => area(a.rect) - area(b.rect))[0];
  }
  function selectPicture(i, p) {
    $$(".ov .sel-box").forEach(n => n.remove());
    E.sel = null;
    ov(i).append(el("div", { class: "sel-box", style: pct(i, p.rect) }));
    showPill([
      { label: "View larger", icon: "search", primary: true, run: () => { clearAll(); viewPicture(i, p); } },
      { label: "Copy picture", icon: "copy", run: () => { clearAll(); copyPicture(i, p); } },
      { label: "Save picture…", icon: "download", run: () => { clearAll(); savePicture(i, p); } },
      { label: "Replace picture…", icon: "image", run: () => { clearAll(); replacePicture(i, p); } },
    ], i, p.rect);
    setStatus(`Picture selected (${p.w} × ${p.h} pixels) - view it larger, copy it, save it or put another picture in its place.`);
  }
  async function copyPicture(i, p) {
    const r = await api.picture_copy(i, p.k, p.xref);
    if (fail(r)) return;
    setStatus(r.status); toast("Picture copied.");
  }
  async function savePicture(i, p) {
    const r = await api.picture_save(i, p.k, p.xref);
    if (fail(r) || r.cancelled) return;
    setStatus(r.status);
    toast(r.status, { actions: [{ label: "Show in folder", run: () => api.show_in_folder(r.saved) }] });
  }
  async function replacePicture(i, p) {
    const f = await api.pick_files("image");
    if (fail(f) || !f.paths?.length) return false;
    const r = await apply(api.picture_replace(i, p.k, f.paths[0], null));
    if (!r) return false;
    toast(r.status, { actions: [{ label: "Undo", run: () => window.undo?.() }] });
    const L = await layout(i);
    const q = L?.images.find(x => x.k === p.k);
    if (q) selectPicture(i, q);
    return true;
  }
  let picDlg = null;
  function pictureDialog() {
    if (picDlg) return picDlg;
    picDlg = el("dialog", { class: "dialog pic-dlg", "aria-labelledby": "pic-title" });
    picDlg.innerHTML = `
      <form method="dialog">
        <div class="td-head"><h3 id="pic-title">Picture</h3><span class="muted pic-sub"></span>
          <div class="seg pic-fit" role="radiogroup" aria-label="Size">
            <button type="button" role="radio" data-fit="fit">Fit</button>
            <button type="button" role="radio" data-fit="actual">Actual size</button></div>
          <button class="icon-btn sm td-x" value="cancel" aria-label="Close"><svg><use href="#i-x"/></svg></button></div>
        <div class="pic-view" tabindex="0" aria-label="The picture"><img alt=""></div>
        <p class="muted small pic-note" hidden></p>
        <div class="dlg-actions">
          <button type="button" class="btn btn-ghost pic-replace"><svg><use href="#i-image"/></svg>Replace…</button>
          <span class="spacer"></span>
          <button type="button" class="btn btn-ghost pic-save"><svg><use href="#i-download"/></svg>Save…</button>
          <button type="button" class="btn btn-outline pic-copy"><svg><use href="#i-copy"/></svg>Copy</button>
          <button class="btn btn-primary" value="ok">Done</button>
        </div>
      </form>`;
    document.body.append(picDlg);
    return picDlg;
  }
  async function viewPicture(i, p) {
    const r = await api.picture_get(i, p.k, p.xref);
    if (fail(r)) return;
    const d = pictureDialog(), q = s => d.querySelector(s);
    const kind = r.mime === "image/jpeg" ? "JPEG" : "PNG";
    q("#pic-title").textContent = `Picture on page ${i + 1}`;
    q(".pic-sub").textContent = `${r.w} × ${r.h} pixels · ${kind}${r.dpi ? ` · about ${r.dpi} dpi on the page` : ""}`;
    q(".pic-note").hidden = !r.smaller;
    q(".pic-note").textContent = "It's a very big picture, so it's shown smaller here. Copy and Save use the full picture.";
    const img = q(".pic-view img");
    img.src = r.src;
    const setFit = f => {
      q(".pic-view").classList.toggle("actual", f === "actual");
      d.querySelectorAll("[data-fit]").forEach(b => b.setAttribute("aria-checked", String(b.dataset.fit === f)));
      if (f === "actual") { img.style.width = `${r.smaller ? r.w : img.naturalWidth || r.w}px`; } else img.style.width = "";
    };
    d.querySelectorAll("[data-fit]").forEach(b => { b.onclick = () => setFit(b.dataset.fit); });
    img.ondblclick = () => setFit(q(".pic-view").classList.contains("actual") ? "fit" : "actual");
    setFit("fit");
    q(".pic-copy").onclick = () => copyPicture(i, p);
    q(".pic-save").onclick = () => savePicture(i, p);
    q(".pic-replace").onclick = async () => { d.close(); await replacePicture(i, p); };
    d.showModal();
    q(".btn-primary").focus();
  }

  // ------------------------------------------------------------------ where to sign (bridge_pictures.sign_places)
  const spotsLabel = () => {
    const n = E.spots?.length || 0;
    return n === 1 ? "1 place to sign" : `${n} places to sign`;
  };
  async function showSpots() {
    if (!S.doc) return;
    const key = S.doc.path + "|" + (S.doc.pages.length);
    if (!E.spots || E.spotsFor !== key) {
      const r = await api.sign_places();
      if (r.error) return;
      E.spots = r.places; E.spotsFor = key;
    }
    if (E.tool !== "sign" || E.sub.sign === "digital") return;
    $$(".ov .sign-spot").forEach(n => n.remove());
    E.spots.forEach((s, n) => {
      if (s.source === "field" && S.doc.has_forms) return;      // the form box has its own Sign here button
      const o = ov(s.page);
      if (!o) return;
      const ini = s.kind === "initials";
      o.append(el("button", { class: "sign-spot" + (ini ? " ini" : ""), type: "button", style: pct(s.page, s.rect), "data-n": n,
        title: `${s.label} - click to add your ${ini ? "initials" : "signature"} here`,
        onpointerdown: e => { e.stopPropagation(); e.preventDefault(); },
        onclick: e => { e.stopPropagation(); signSpot(s); } },
      el("span", { class: "sign-spot-tag" }, icon(ini ? "initials" : "sign"), ini ? "Initials here" : "Sign here")));
    });
    const b = $("#spots-next");
    if (b) { b.hidden = !E.spots.length; b.lastChild.textContent = spotsLabel(); }
    if (E.spots.length && !E.spotsTold) {
      E.spotsTold = true;
      setStatus(`Found ${spotsLabel()} - click a "Sign here" box to put your signature on its line.`);
    }
  }
  function hideSpots() { $$(".ov .sign-spot").forEach(n => n.remove()); E.spotsTold = false; }
  function middleOn(s) {                  // scroll so the place sits a little above the middle, clear of the bars
    const vb = viewer.getBoundingClientRect(), top = clientRect(s.page, s.rect).top;
    if (top < vb.top + 150 || top > vb.bottom - 100) viewer.scrollTop += top - (vb.top + vb.height * 0.42);
  }
  function signSpot(s) {
    middleOn(s);
    hideSpots();
    E.sub.sign = s.kind === "initials" ? "initials" : "mine"; save();
    if (s.source === "field" && E.sub.sign === "mine") return signField(s.page, { rect: s.rect }, document.activeElement);
    placeSignatureIn(s.page, s.rect, null, s.kind === "initials" ? "initials" : "signature", true);
  }
  function nextSpot() {
    if (!E.spots?.length) return;
    const vb = viewer.getBoundingClientRect(), mid = vb.top + vb.height * 0.42;
    const after = E.spots.find(s => clientRect(s.page, s.rect).top > mid + 4) || E.spots[0];
    middleOn(after);
    const n = $(`.sign-spot[data-n="${E.spots.indexOf(after)}"]`);
    if (n) { n.classList.remove("flash"); void n.offsetWidth; n.classList.add("flash"); n.focus({ preventScroll: true }); }
  }

  // ------------------------------------------------------------------ crop
  function startCrop(i) {
    goTo(i);
    E.cropPage = i;
    setTool("crop");
  }

  // ------------------------------------------------------------------ keyboard
  function key(e) {
    const k = e.key, plain = !e.ctrlKey && !e.metaKey && !e.altKey;
    if (k === "Escape") {
      if (E.pending) { E.pending.cancel(); E.pending = null; return true; }
      if (!$("#pill").hidden || E.sel || E.tsel || E.target) { clearAll(); return true; }
      if (E.tool !== "select") { setTool("select"); return true; }
      return false;
    }
    if ((k === "Delete" || k === "Backspace") && E.sel) { e.preventDefault(); deleteSel(); return true; }
    if ((e.ctrlKey || e.metaKey) && k.toLowerCase() === "c" && E.tsel) { e.preventDefault(); copySel(); return true; }
    if (plain && !e.shiftKey) {
      const t = { v: "select", h: "markup", d: "draw", t: "text", e: "edittext", r: "redact", s: "sign" }[k.toLowerCase()];
      if (t && document.activeElement?.closest?.(".toolbar, .viewer, body") && !document.activeElement.closest(".strip")) { setTool(t); return true; }
    }
    return false;
  }

  function reset() { E.layouts.clear(); E.lay.clear(); E.pending = null; E.adhoc = null; E.spots = null; E.spotsFor = null; setTool("select"); }

  window.Edit = { setTool, decorate, pageVisible, pageChanged, docChanged, reset, cancel, commitPending, key, startCrop,
    ensureSignature, openSignaturePad, showPill, hidePill, apply, showSpots, get tool() { return E.tool; }, get adhoc() { return E.adhoc; } };
})();
