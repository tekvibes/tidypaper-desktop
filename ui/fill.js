/* Fill with my details, My details, and Find personal details (to redact).
   Uses app.js globals (api, S, el, icon, $, $$, toast, fail, setStatus, goTo, refreshDoc, pagesEl). */
"use strict";
(() => {
  const SOURCE = { rule: "matched by its label", ai: "matched by AI - check it", paper: "printed label" };

  function dialog(cls, html) {
    const d = el("dialog", { class: "dialog " + cls });
    d.innerHTML = html;
    document.body.append(d);
    d.addEventListener("close", () => setTimeout(() => d.remove(), 0), { once: true });
    return d;
  }

  function flash(page, rect) {
    goTo(page);
    const p = S.doc.pages[page], div = pagesEl.children[page];
    if (!div || !rect) return;
    const f = el("div", { class: "fill-flash",
      style: `left:${rect[0] / p.w * 100}%;top:${rect[1] / p.h * 100}%;width:${(rect[2] - rect[0]) / p.w * 100}%;height:${(rect[3] - rect[1]) / p.h * 100}%` });
    div.append(f);
    setTimeout(() => f.remove(), 1600);
  }

  // ------------------------------------------------------------------ My details
  async function details() {
    const r = await api.details_get();
    if (fail(r)) return false;
    const d = dialog("fill-details", `<form method="dialog">
      <div class="td-head"><h3>My details</h3><span class="muted">used to fill in forms - kept on this ${APP.mac ? "Mac" : "PC"} only</span>
        <button class="icon-btn sm td-x" value="cancel" aria-label="Close"><svg><use href="#i-x"/></svg></button></div>
      <div class="fd-body"></div>
      <p class="note-amber small"><svg><use href="#i-lock"/></svg><span>${APP.mac ? "Saved in your Mac's Keychain" : "Saved encrypted for your Windows account"}. They're never sent to an AI service - only a form's box labels are, when AI matching is on.</span></p>
      <p class="field-error fd-err" hidden></p>
      <div class="dlg-actions"><button type="button" class="link-btn fd-clear">Delete my saved details</button><span class="spacer"></span>
        <button class="btn btn-ghost" value="cancel" formnovalidate>Cancel</button><button class="btn btn-primary" value="ok">Save</button></div></form>`);
    const body = d.querySelector(".fd-body");
    const inputs = {};
    const groups = {};
    for (const f of r.fields) {
      const box = groups[f.group] || (groups[f.group] = el("fieldset", { class: "fd-group" }, el("legend", { text: f.group })));
      const inp = el("input", { class: "st-in", type: f.key === "date_of_birth" ? "date" : f.key === "email" ? "email" : f.sensitive ? "password" : "text",
        autocomplete: "off", spellcheck: "false" });
      inp.value = r.details[f.key] || "";
      inputs[f.key] = inp;
      const row = el("label", { class: "field" }, el("span", { text: f.label }), inp);
      if (f.sensitive && f.key !== "date_of_birth")
        row.append(el("button", { type: "button", class: "link-btn small fd-show", text: "Show", onclick: e => {
          inp.type = inp.type === "password" ? "text" : "password"; e.currentTarget.textContent = inp.type === "password" ? "Show" : "Hide"; } }));
      box.append(row);
    }
    const custom = el("div", { class: "fd-custom" });
    const addCustom = (c = { label: "", value: "" }) => custom.append(el("div", { class: "st-row fd-crow" },
      el("input", { class: "st-in fd-cl", placeholder: "Label, e.g. Membership number", value: c.label }),
      el("input", { class: "st-in fd-cv", placeholder: "Value", value: c.value }),
      el("button", { type: "button", class: "icon-btn sm", "aria-label": "Remove", onclick: e => e.currentTarget.parentElement.remove() }, icon("x"))));
    (r.custom || []).forEach(addCustom);
    body.append(...Object.values(groups),
      el("fieldset", { class: "fd-group" }, el("legend", { text: "Anything else" }), custom,
        el("button", { type: "button", class: "btn btn-ghost sm", onclick: () => addCustom() }, icon("plus"), "Add another")));
    d.querySelector(".fd-clear").onclick = async () => {
      const c = await api.details_clear(); if (fail(c)) return;
      Object.values(inputs).forEach(i => { i.value = ""; }); custom.replaceChildren(); setStatus(c.status); toast(c.status);
    };
    d.showModal();
    inputs.first_name.focus();
    return new Promise(res => {
      const onClose = async () => {
        if (d.returnValue !== "ok") return res(false);
        const vals = Object.fromEntries(Object.entries(inputs).map(([k, i]) => [k, i.value]));
        const cus = [...custom.querySelectorAll(".fd-crow")].map(row => ({ label: row.querySelector(".fd-cl").value, value: row.querySelector(".fd-cv").value }));
        const s = await api.details_set(vals, cus);
        if (s.error) {
          const e = d.querySelector(".fd-err"); e.textContent = s.error; e.hidden = false;
          d.returnValue = ""; d.showModal(); return d.addEventListener("close", onClose, { once: true });
        }
        setStatus(s.status); toast(s.status);
        res(true);
      };
      d.addEventListener("close", onClose, { once: true });
    });
  }

  // ------------------------------------------------------------------ Fill with my details
  async function open() {
    if (!S.doc) return;
    await window.Edit?.commitPending();
    setStatus("Looking at the form…");
    let plan = await api.fill_plan(true);
    if (fail(plan)) return;
    if (!plan.details_saved) {
      if (!(await details())) return;
      plan = await api.fill_plan(true);
      if (fail(plan)) return;
    }
    const items = plan.items;
    if (!items.length) return toast("No form boxes or labels like “Name:” were found in this document. Use Text → Text box to type anywhere.", { error: true });
    const matched = items.filter(it => it.value), rest = items.filter(it => !it.value);
    const d = dialog("fill-dlg", `<form method="dialog">
      <div class="td-head"><h3>Fill with my details</h3><span class="muted fl-sub"></span>
        <button class="icon-btn sm td-x" value="cancel" aria-label="Close"><svg><use href="#i-x"/></svg></button></div>
      <div class="fl-list"></div>
      <details class="fl-rest"><summary></summary><div class="fl-rest-list"></div></details>
      <p class="muted small fl-note"></p>
      <div class="dlg-actions"><button type="button" class="btn btn-ghost fl-edit"><svg><use href="#i-edittext"/></svg>Edit my details</button>
        <span class="spacer"></span><button class="btn btn-ghost" value="cancel">Cancel</button>
        <button class="btn btn-primary fl-ok" value="ok">Fill</button></div></form>`);
    d.querySelector(".fl-sub").textContent = `${matched.length} of ${items.length} ${plan.paper ? "places" : "boxes"} matched`;
    const opts = Object.entries(plan.describe);
    const row = it => {
      const cb = el("input", { type: "checkbox", "aria-label": `Fill ${it.label}` });
      cb.checked = !!it.value && !(it.current && it.current !== it.value);
      const val = el("input", { class: "st-in fl-val", value: it.value || "", "aria-label": `Value for ${it.label}`,
        oninput: () => { cb.checked = !!val.value.trim(); count(); } });
      const pick = el("select", { class: "st-in fl-pick", "aria-label": "Which detail",
        onchange: async () => {
          if (!pick.value) return;
          const r = await api.details_get(); if (fail(r)) return;
          it.match = pick.value;
          val.value = pick.value === "today" ? new Date().toLocaleDateString() : pick.value === "full_name"
            ? [r.details.first_name, r.details.middle_name, r.details.last_name].filter(Boolean).join(" ")
            : pick.value === "address" ? [r.details.address_line1, r.details.address_line2, r.details.town, r.details.county, r.details.postcode].filter(Boolean).join(", ")
              : r.details[pick.value] || "";
          cb.checked = !!val.value; count();
        } }, el("option", { value: "", text: it.match ? plan.describe[it.match] : "Choose a detail…" }),
        ...opts.map(([k, label]) => el("option", { value: k, text: label })));
      if (it.match) pick.value = "";
      cb.addEventListener("change", count);
      const r = el("div", { class: "fl-row" + (it.source === "ai" ? " ai" : "") }, cb,
        el("button", { type: "button", class: "fl-label", title: "Show it on the page", onclick: () => flash(it.page, it.rect || (it.at && [it.at[0], it.at[1], it.at[0] + 120, it.at[1] + 14])) },
          el("span", { text: it.label || "(no label)" }),
          el("small", { class: "muted", text: `page ${it.page + 1}${it.source ? " · " + SOURCE[it.source] : it.kind === "paper" ? " · " + SOURCE.paper : ""}${it.current ? ` · now: “${it.current}”` : ""}` })),
        el("div", { class: "fl-vals" }, val, pick));
      r._get = () => cb.checked && val.value.trim() ? { ...it, value: val.value } : null;
      return r;
    };
    const rows = [...matched.map(row)];
    d.querySelector(".fl-list").replaceChildren(...rows);
    const restRows = rest.map(row);
    d.querySelector(".fl-rest-list").replaceChildren(...restRows);
    d.querySelector(".fl-rest summary").textContent = `Not matched (${rest.length}) - choose a detail for any of these`;
    d.querySelector(".fl-rest").hidden = !rest.length;
    const aiOn = window.AI?.isReady();
    d.querySelector(".fl-note").textContent = plan.ai_used ? "AI looked at the labels of the boxes the rules couldn't place - your details weren't sent."
      : plan.ai_note ? plan.ai_note
        : rest.length && !aiOn ? "Tip: turn on the AI assistant in Settings to match more boxes automatically." : "";
    function count() {
      const n = [...rows, ...restRows].filter(r => r._get()).length;
      const ok = d.querySelector(".fl-ok");
      ok.textContent = n ? `Fill ${n} ${n === 1 ? (plan.paper ? "place" : "box") : (plan.paper ? "places" : "boxes")}` : "Fill";
      ok.disabled = !n;
    }
    count();
    d.querySelector(".fl-edit").onclick = async () => { d.close("cancel"); if (await details()) open(); };
    d.showModal();
    d.querySelector(".fl-ok").focus();
    d.addEventListener("close", async () => {
      if (d.returnValue !== "ok") return setStatus("");
      const chosen = [...rows, ...restRows].map(r => r._get()).filter(Boolean);
      const r = await api.fill_apply(chosen);
      if (fail(r)) return;
      refreshDoc(r, null);
      setStatus(r.status);
      toast(r.status);
    }, { once: true });
  }

  // ------------------------------------------------------------------ Find personal details
  async function findPersonal() {
    if (!S.doc) return;
    await window.Edit?.commitPending();
    // pictures of pages have no words to look through: they are read first, as "Make words selectable" reads them
    // (its bar shows the reading, with Stop)
    let stopped = false, readNow = false;
    const sp = await api.scan_pages();
    if (fail(sp)) return;
    if (sp.pages.length && window.ScanWords) {
      setStatus("Reading the scan's words first, to look through them…");
      const rd = await window.ScanWords.read({ quiet: true });
      if (rd.error && window.ScanWords.running) return toast(rd.error, { error: true });
      if (rd.front === false) return;                    // another document came to the front meanwhile
      stopped = !!rd.stopped;
      readNow = !!rd.changed?.length;
    }
    setStatus("Looking for personal details…");
    const r = await api.pii_scan(true, stopped);
    if (fail(r)) return;
    const hint = r.details_saved ? "" : " Add your details (More → My details) to also look for your name and address.";
    // nothing could be read at all: there was nothing to look through - said in a box that waits, never skipped
    if (r.left && r.scans.length >= r.pages) { setStatus(""); return waitBox(r.left); }
    if (!r.items.length) {
      setStatus("No personal details found.");
      if (r.left) return waitBox(`${r.nothing_found} ${r.left}`);
      return toast(r.nothing_found + (readNow ? " " + r.scans_read : "") + hint, readNow ? { ms: 9000 } : {});
    }
    // groups of a kind under headings that open and close (iOS's and Android's list); a long list starts closed up,
    // so it can be taken in without scrolling
    const long = r.items.length > 10;
    const d = dialog("pii-dlg", `<form method="dialog">
      <div class="td-head"><span class="dlg-icon bad"><svg><use href="#i-redact"/></svg></span>
        <div class="sd-titles"><h3>Find personal details</h3><span class="muted">Tick what to remove. It's taken out of the document for good - Undo works until you save.${long ? " Click a heading to open or close it." : ""}</span></div>
        <button class="icon-btn sm td-x" value="cancel" aria-label="Close"><svg><use href="#i-x"/></svg></button></div>
      <p class="muted small pi-read" hidden></p>
      <div class="pi-tools"><button type="button" class="link-btn small pi-all">Tick all</button><button type="button" class="link-btn small pi-none">Untick all</button></div>
      <div class="pi-list"></div>
      <p class="muted small pi-note"></p>
      <div class="dlg-actions"><span class="spacer"></span><button class="btn btn-ghost" value="cancel">Cancel</button>
        <button class="btn btn-primary pi-ok danger-btn" value="ok">Remove</button></div></form>`);
    const groups = {};
    const boxes = [];
    for (const it of r.items) {
      let g = groups[it.label];
      if (!g) {
        const head = el("button", { type: "button", class: "pi-ghead", "aria-expanded": String(!long) });
        const all = el("button", { type: "button", class: "link-btn small pi-gall", hidden: true });
        const rows = el("div", { class: "pi-rows", hidden: long });
        g = groups[it.label] = { label: it.label, head, all, rows, boxes: [] };
        head.onclick = () => { rows.hidden = !rows.hidden; head.setAttribute("aria-expanded", String(!rows.hidden)); count(); };
        all.onclick = () => { const on = !g.boxes.every(cb => cb.checked); g.boxes.forEach(cb => { cb.checked = on; }); count(); };
        g.el = el("div", { class: "pi-group" }, el("div", { class: "pi-gtop" }, head, all), rows);
      }
      const cb = el("input", { type: "checkbox", "aria-label": `Remove ${it.text}` });
      cb.checked = it.conf >= 0.85;            // an address (0.75) never is
      cb.addEventListener("change", count);
      boxes.push([cb, it]);
      g.boxes.push(cb);
      g.rows.append(el("label", { class: "pi-row" + (it.conf < 0.85 ? " unsure" : "") }, cb,
        el("span", { class: "pi-text" }, el("strong", { text: it.text }), el("small", { class: "muted", text: it.context })),
        el("button", { type: "button", class: "ai-cite", title: "Show it on the page", onclick: e => { e.preventDefault(); flashAll(it); } }, icon("file"), `p.${it.page + 1}`)));
    }
    if (readNow) {
      d.querySelector(".pi-read").textContent = r.scans_read;
      d.querySelector(".pi-read").hidden = false;
    }
    d.querySelector(".pi-list").replaceChildren(...Object.values(groups).map(g => g.el));
    d.querySelector(".pi-note").textContent = [
      r.ai_used ? "AI double-checked the doubtful ones." : r.ai_note || "",
      r.details_saved ? "" : "Add your details (More → My details) to also find your name and address.",
      r.items.some(it => it.kind === "address") ? "Addresses are never ticked for you, and other unticked items looked doubtful - tick what should go."
        : "Unticked items looked doubtful - tick them if they should go.",
    ].filter(Boolean).join(" ");
    function count() {
      const n = boxes.filter(([cb]) => cb.checked).length;
      const ok = d.querySelector(".pi-ok");
      ok.textContent = n ? `Remove ${n} for good` : "Remove";
      ok.disabled = !n;
      // each heading says how many it has and how many are ticked, open or closed; Tick all on a group of two or more
      for (const g of Object.values(groups)) {
        const t = g.boxes.filter(cb => cb.checked).length;
        g.head.textContent = `${g.rows.hidden ? "▸" : "▾"} ${g.label} (${g.boxes.length})${t ? ` · ${t} ticked` : ""}`;
        g.head.title = g.rows.hidden ? "Show them" : "Hide them";
        g.all.hidden = g.boxes.length < 2;
        const every = t === g.boxes.length;
        g.all.textContent = every ? "Untick all" : "Tick all";
        g.all.setAttribute("aria-label", `${every ? "Untick all" : "Tick all"}: ${g.label}`);
      }
    }
    d.querySelector(".pi-all").onclick = () => { boxes.forEach(([cb]) => { cb.checked = true; }); count(); };
    d.querySelector(".pi-none").onclick = () => { boxes.forEach(([cb]) => { cb.checked = false; }); count(); };
    count();
    d.showModal();
    setStatus(`${r.items.length} possible personal detail${r.items.length > 1 ? "s" : ""} found.`);
    if (r.left) waitBox(r.left);                         // pages still pictures: over the list, until OK
    d.addEventListener("close", async () => {
      if (d.returnValue !== "ok") return;
      const chosen = boxes.filter(([cb]) => cb.checked).map(([, it]) => it);
      const res = await api.pii_redact(chosen);
      if (fail(res)) return;
      refreshDoc(res, null);
      setStatus(res.status);
      if (!res.stuck) return toast(res.status);
      // some stayed: said in a box that waits, not a line that goes (iOS's "nothing happened")
      waitBox(res.status, "Not everything was removed");
    }, { once: true });
  }

  /** Something the person must see: a box that stays until OK, not a line that goes. */
  function waitBox(msg, title = "") {
    const box = dialog("pii-stuck", `<form method="dialog">
      <div class="td-head"><span class="dlg-icon bad"><svg><use href="#i-alert"/></svg></span>
        <div class="sd-titles">${title ? "<h3></h3>" : ""}<p class="pi-stuck-msg"></p></div></div>
      <div class="dlg-actions"><span class="spacer"></span><button class="btn btn-primary" value="ok">OK</button></div></form>`);
    if (title) box.querySelector("h3").textContent = title;
    box.querySelector(".pi-stuck-msg").textContent = msg;
    box.showModal();
    box.querySelector(".btn-primary").focus();
  }

  /** Every line of a find (an address has a box a line). */
  function flashAll(it) {
    flash(it.page, it.rect);
    const p = S.doc.pages[it.page], div = pagesEl.children[it.page];
    if (!p || !div) return;
    for (const rect of it.more || []) {
      const f = el("div", { class: "fill-flash",
        style: `left:${rect[0] / p.w * 100}%;top:${rect[1] / p.h * 100}%;width:${(rect[2] - rect[0]) / p.w * 100}%;height:${(rect[3] - rect[1]) / p.h * 100}%` });
      div.append(f);
      setTimeout(() => f.remove(), 1600);
    }
  }

  function docOpened(doc) {
    if (doc?.has_forms) toast("This document has boxes you can type into.", { actions: [{ label: "Fill with my details", run: open }] });
  }

  window.Fill = { open, details, findPersonal, docOpened, flash };
})();
