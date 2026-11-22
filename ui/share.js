/* Getting the document out: print, email, final copy, password.  Uses app.js globals
   (api, S, el, icon, $, toast, hideToast, fail, setStatus, save, fileActions) and window.Edit. */
"use strict";
(() => {
  // ------------------------------------------------------------------ small dialogs
  function ask({ title, message, actions, iconName = "alert", tone = "warn" }) {
    const d = el("dialog", { class: "dialog" });
    d.innerHTML = `<form method="dialog"><div class="dlg-head"><span class="dlg-icon ${tone}"><svg><use href="#i-${iconName}"/></svg></span>
      <div><h3></h3><p class="muted"></p></div></div><div class="dlg-actions"></div></form>`;
    d.querySelector("h3").textContent = title;
    d.querySelector("p").textContent = message;
    d.querySelector(".dlg-actions").replaceChildren(...actions.map(a =>
      el("button", { class: "btn " + (a.primary ? "btn-primary" : a.outline ? "btn-outline" : "btn-ghost"), value: a.value, text: a.label })));
    document.body.append(d);
    d.showModal();
    d.querySelector(".btn-primary")?.focus();
    return new Promise(res => d.addEventListener("close", () => { res(d.returnValue || "cancel"); d.remove(); }, { once: true }));
  }

  function askPasswordTwice() {
    const d = el("dialog", { class: "dialog" });
    d.innerHTML = `<form method="dialog">
      <div class="dlg-head"><span class="dlg-icon"><svg><use href="#i-lock"/></svg></span>
        <div><h3>Protect with a password</h3><p class="muted">Saves a copy that can only be opened with this password. Your original stays as it is.</p></div></div>
      <div class="field-row">
        <label class="field"><span>Password</span><input class="pw1" type="password" autocomplete="new-password"></label>
        <label class="field"><span>Type it again</span><input class="pw2" type="password" autocomplete="new-password"></label>
      </div>
      <p class="field-error" hidden></p>
      <p class="note-amber small"><svg><use href="#i-alert"/></svg><span>If the password is forgotten, the copy can't be opened by anyone - ${APP.name} can't recover it.</span></p>
      <div class="dlg-actions"><button class="btn btn-ghost" value="cancel" formnovalidate>Cancel</button>
        <button class="btn btn-primary" value="ok">Save protected copy</button></div></form>`;
    document.body.append(d);
    d.showModal();
    d.querySelector(".pw1").focus();
    return new Promise(res => {
      const onClose = () => {
        if (d.returnValue !== "ok") { d.remove(); return res(null); }
        const a = d.querySelector(".pw1").value, b = d.querySelector(".pw2").value, err = d.querySelector(".field-error");
        const again = msg => { err.textContent = msg; err.hidden = false; d.returnValue = ""; d.showModal(); d.addEventListener("close", onClose, { once: true }); };
        if (a.length < 4) return again("Choose a password of at least 4 characters.");
        if (a !== b) return again("The two passwords don't match.");
        d.remove(); res(a);
      };
      d.addEventListener("close", onClose, { once: true });
    });
  }

  /** The file to share: the saved copy if there is one; offers to save edits first. */
  async function fileToShare(verb) {
    await window.Edit?.commitPending();
    if (S.doc.dirty) {
      const c = await ask({ title: "Save your changes first?", tone: "warn",
        message: `You've edited ${S.doc.name}. Save the changes so they're included before you ${verb} it?`,
        actions: [{ label: "Cancel", value: "cancel" }, { label: `${verb[0].toUpperCase() + verb.slice(1)} the last saved version`, value: "old", outline: true },
          { label: `Save and ${verb}`, value: "save", primary: true }] });
      if (c === "cancel") return null;
      if (c === "save" && !(await save())) return null;
    }
    return S.doc.saved_to || S.doc.source;
  }

  // ------------------------------------------------------------------ print
  async function print() {
    if (!S.doc) return;
    await window.Edit?.commitPending();
    const n = S.doc.pages.length, dpi = n > 60 ? 150 : 200, tabApi = apiIn(S.tab);   // this tab's pages, even after a switch
    const root = $("#print-root") || document.body.appendChild(el("div", { id: "print-root", "aria-hidden": "true" }));
    root.replaceChildren();
    toast(`Getting ${n} page${n > 1 ? "s" : ""} ready to print…`, { sticky: true });
    for (let i = 0; i < n; i++) {
      setStatus(`Preparing page ${i + 1} of ${n} for printing…`);
      const r = await tabApi.print_page(i, dpi);
      if (fail(r)) { root.replaceChildren(); return; }
      root.append(el("div", { class: "print-page" + (r.w > r.h ? " landscape" : "") }, el("img", { src: r.src, alt: "" })));
    }
    await Promise.all([...root.querySelectorAll("img")].map(img => img.decode ? img.decode().catch(() => {}) : null));
    hideToast();
    setStatus("Choose a printer in the print window.");
    const done = () => { root.replaceChildren(); setStatus("Printing done - or cancelled."); window.removeEventListener("afterprint", done); };
    window.addEventListener("afterprint", done);
    window.print();
  }

  // ------------------------------------------------------------------ email
  async function email(path) {
    if (!path) {
      if (!S.doc) return;
      path = await fileToShare("email");
      if (!path) return;
    }
    const send = await checkBeforeSending(path);
    if (!send) return;
    const r = await api.email_file(send.path);
    if (fail(r)) return;
    const say = send.note ? `${send.note} ${r.status}` : r.status;
    setStatus(say);
    toast(say, { actions: ["mapi", "app"].includes(r.how) ? [] : [{ label: "Show in folder", run: () => api.show_in_folder(r.path || send.path) }] });
  }

  // ------------------------------------------------------------------ before sending: numbers people keep private
  /** Card, bank, National Insurance and NHS numbers in the file about to go: send it as it is, or a copy with the ticked
   *  ones blacked out.  {path, note} to send, or null (closed).  A check that can't run never stops the email. */
  async function checkBeforeSending(path) {
    setStatus("Checking for card, bank and ID numbers…");
    const r = await api.share_check(path);
    if (!r || r.error || !r.items?.length) { setStatus("No card, bank or ID numbers found."); return { path }; }
    const n = r.items.length;
    setStatus(`${n === 1 ? "A number" : `${n} numbers`} to keep private found.`);
    const d = el("dialog", { class: "dialog pii-dlg sc-dlg" });
    d.innerHTML = `<form method="dialog">
      <div class="td-head"><span class="dlg-icon warn"><svg><use href="#i-redact"/></svg></span>
        <div class="sd-titles"><h3>Before you email it</h3><span class="muted"></span></div>
        <button class="icon-btn sm td-x" value="cancel" aria-label="Close"><svg><use href="#i-x"/></svg></button></div>
      <div class="pi-list"></div>
      <p class="muted small sc-note"></p>
      <div class="dlg-actions"><button class="btn btn-ghost" value="cancel">Cancel</button><span class="spacer"></span>
        <button class="btn btn-outline sc-asis" value="asis">Email it as it is</button>
        <button class="btn btn-primary sc-ok" value="ok">Email a copy without them</button></div></form>`;
    d.querySelector(".sd-titles .muted").textContent =
      `This document shows ${n === 1 ? "a number" : `${n} numbers`} people usually keep private. Tick what to black out.`;
    const stem = (path.split(/[\\/]/).pop() || "").replace(/\.pdf$/i, "").replace(/_(edited|final|protected|unlocked|redacted)$/, "");
    d.querySelector(".sc-note").textContent =
      `The copy is saved beside it as ${stem}_redacted.pdf, with the numbers taken out for good. Your document keeps them.`;
    const groups = {}, boxes = [];
    for (const it of r.items) {
      const g = groups[it.label] || (groups[it.label] = el("div", { class: "pi-group" }, el("div", { class: "pi-ghead", text: it.label })));
      const cb = el("input", { type: "checkbox", "aria-label": `Black out ${it.text}` });
      cb.checked = true;
      cb.addEventListener("change", count);
      boxes.push([cb, it]);
      g.append(el("label", { class: "pi-row" }, cb,
        el("span", { class: "pi-text" }, el("strong", { text: it.text }), el("small", { class: "muted", text: it.context })),
        it.shown && window.Fill?.flash
          ? el("button", { type: "button", class: "ai-cite", title: "Show it on the page", onclick: e => { e.preventDefault(); window.Fill.flash(it.page, it.shown); } }, icon("file"), `p.${it.page + 1}`)
          : el("small", { class: "muted", text: `p.${it.page + 1}` })));
    }
    d.querySelector(".pi-list").replaceChildren(...Object.values(groups));
    function count() {
      const k = boxes.filter(([cb]) => cb.checked).length, ok = d.querySelector(".sc-ok");
      ok.disabled = !k;
      ok.textContent = k === n ? "Email a copy without them" : `Email a copy without ${k} of them`;
    }
    count();
    document.body.append(d);
    d.showModal();
    d.querySelector(".sc-ok").focus();
    const how = await new Promise(res => d.addEventListener("close", () => { res(d.returnValue || "cancel"); d.remove(); }, { once: true }));
    if (how === "asis") return { path };
    if (how !== "ok") return null;
    const chosen = boxes.filter(([cb]) => cb.checked).map(([, it]) => it);
    setStatus("Making a copy without them…");
    const c = await api.share_redacted_copy(path, chosen);
    if (fail(c)) return null;
    return { path: c.saved, note: c.status };
  }

  // ------------------------------------------------------------------ final copy
  async function finalCopy() {
    await window.Edit?.commitPending();
    let r = await api.final_copy(false);
    if (r.needs_confirm) {
      const c = await ask({ title: "Make a final copy?", message: r.message,
        actions: [{ label: "Cancel", value: "cancel" }, { label: "Make final copy", value: "ok", primary: true }] });
      if (c !== "ok") return;
      r = await api.final_copy(true);
    }
    if (fail(r)) return;
    setStatus(r.status);
    toast(r.status, { actions: fileActions(r.saved) });
  }

  // ------------------------------------------------------------------ passwords
  async function protect() {
    await window.Edit?.commitPending();
    const pw = await askPasswordTwice();
    if (!pw) return;
    const r = await api.protect_copy(pw);
    if (fail(r)) return;
    setStatus(r.status);
    toast(r.status, { actions: fileActions(r.saved) });
  }
  async function unprotect() {
    const r = await api.unprotect_copy();
    if (fail(r)) return;
    setStatus(r.status);
    toast(r.status, { actions: fileActions(r.saved) });
  }

  window.Share = { print, email, finalCopy, protect, unprotect };
})();
