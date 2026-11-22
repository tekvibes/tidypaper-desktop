/* Digital signing (stage 3): "Set up my signing identity", the B · Sign digitally dialog, and the
   verify panel behind the green shield.  Uses app.js globals (api, S, el, icon, $, toast, fail, setStatus,
   openPath, refreshDoc, showMenu) and window.Edit. */
"use strict";
(() => {
  let identity = null;

  // ------------------------------------------------------------------ identity
  async function loadIdentity() {
    const r = await api.identity_get();
    if (!fail(r)) identity = r;
    return identity;
  }

  let idDlg = null;
  function identityDialog() {
    if (idDlg) return idDlg;
    idDlg = el("dialog", { class: "dialog id-dlg" });
    idDlg.innerHTML = `
      <form method="dialog">
        <div class="dlg-head"><span class="dlg-icon ok"><svg><use href="#i-shield"/></svg></span>
          <div><h3>Set up my signing identity</h3>
          <p class="muted">A private digital ID kept on this ${APP.mac ? "Mac" : "PC"} and locked with your password. You'll type the password each time you sign.</p></div></div>
        <label class="field"><span>Your name, as it should appear on signatures</span><input class="id-name" autocomplete="name" required></label>
        <label class="field"><span>Email <span class="muted">(optional)</span></span><input class="id-email" type="email" autocomplete="email"></label>
        <div class="field-row">
          <label class="field"><span>Password</span><input class="id-pw" type="password" autocomplete="new-password"></label>
          <label class="field"><span>Type it again</span><input class="id-pw2" type="password" autocomplete="new-password"></label>
        </div>
        <p class="field-error id-err" hidden></p>
        <p class="note-amber small id-replace" hidden><svg><use href="#i-alert"/></svg><span>This replaces your current identity. Documents you already signed stay valid, but people who trusted your old one will need your new certificate.</span></p>
        <div class="dlg-actions">
          <button class="btn btn-ghost" value="cancel" formnovalidate>Cancel</button>
          <button class="btn btn-primary id-ok" value="ok">Set up</button>
        </div>
      </form>`;
    document.body.append(idDlg);
    return idDlg;
  }

  async function setupIdentity(replace = false) {
    const d = identityDialog(), q = s => d.querySelector(s);
    const id = identity || await loadIdentity() || {};
    q(".id-name").value = id.name || id.profile_name || "";
    q(".id-email").value = id.email || id.profile_email || "";
    q(".id-pw").value = q(".id-pw2").value = "";
    q(".id-err").hidden = true;
    q(".id-replace").hidden = !replace;
    d.returnValue = "";
    d.showModal();
    (q(".id-name").value ? q(".id-pw") : q(".id-name")).focus();
    return new Promise(res => {
      const onClose = async () => {
        if (d.returnValue !== "ok") return res(false);
        const err = msg => { q(".id-err").textContent = msg; q(".id-err").hidden = false; d.returnValue = ""; d.showModal(); d.addEventListener("close", onClose, { once: true }); };
        if (q(".id-pw").value !== q(".id-pw2").value) return err("The two passwords don't match.");
        q(".id-ok").disabled = true;
        setStatus("Setting up your signing identity…");
        const r = await api.identity_create(q(".id-name").value, q(".id-pw").value, q(".id-email").value);
        q(".id-ok").disabled = false;
        if (r.error) return err(r.error);
        identity = r;
        setStatus(r.status);
        toast(r.status);
        res(true);
      };
      d.addEventListener("close", onClose, { once: true });
    });
  }

  async function ensureIdentity() {
    const id = await loadIdentity();
    if (id?.exists && !id.expired) {
      setStatus("Drag where the signature box should go - or sign without a visible box.");
      return true;
    }
    return setupIdentity();
  }

  async function exportCertificate() {
    const r = await api.identity_export();
    if (r.cancelled || fail(r)) return;
    toast(r.status || "Certificate saved.", { actions: [{ label: "Show in folder", run: () => api.show_in_folder(r.saved) }] });
  }

  // ------------------------------------------------------------------ sign dialog (Concept B)
  let signDlg = null;
  function signDialog() {
    if (signDlg) return signDlg;
    signDlg = el("dialog", { class: "dialog sign-dlg" });
    signDlg.innerHTML = `
      <form method="dialog">
        <div class="td-head"><span class="dlg-icon ok"><svg><use href="#i-shield"/></svg></span>
          <div class="sd-titles"><h3>Sign digitally</h3><span class="muted">Seals the document so any later change can be detected</span></div>
          <button class="icon-btn sm td-x" value="cancel" aria-label="Close"><svg><use href="#i-x"/></svg></button></div>
        <div class="sd-body">
          <div class="sd-left">
            <div class="sd-id">
              <span class="sd-avatar"></span>
              <div class="sd-who"><strong class="sd-name"></strong><span class="muted small sd-meta"></span></div>
              <button type="button" class="btn btn-ghost sm sd-change" aria-haspopup="menu">Change</button>
            </div>
            <label class="field"><span>Your signing password</span><input class="sd-pw" type="password" autocomplete="current-password"></label>
            <p class="field-error sd-err" hidden></p>
            <label class="field"><span>Reason</span><input class="sd-reason"></label>
            <label class="check"><input type="checkbox" class="sd-ts" checked><span>Add a trusted timestamp <span class="muted">- proves when you signed, even after your ID expires (needs internet)</span></span></label>
            <label class="check"><input type="checkbox" class="sd-lock"><span>Lock the document after signing <span class="muted">- no further changes allowed</span></span></label>
          </div>
          <div class="sd-right">
            <span class="sd-cap muted small"></span>
            <div class="sd-stamp">
              <img class="sd-sigimg" alt="">
              <div class="sd-stamp-text"></div>
            </div>
            <div class="seg sd-vis" role="radiogroup" aria-label="Appearance">
              <button type="button" role="radio" data-vis="1">Visible box</button>
              <button type="button" role="radio" data-vis="0">Invisible</button>
            </div>
            <label class="check"><input type="checkbox" class="sd-img"><span>Include my signature picture</span></label>
          </div>
        </div>
        <p class="note-amber sd-note"><svg><use href="#i-alert"/></svg><span class="sd-note-text"></span></p>
        <div class="dlg-actions">
          <button type="button" class="link-btn sd-cer">Send people my certificate (.cer)</button>
          <span class="spacer"></span>
          <button class="btn btn-ghost" value="cancel" formnovalidate>Cancel</button>
          <button class="btn btn-primary sd-ok" value="ok"><svg><use href="#i-shield"/></svg>Sign and save copy</button>
        </div>
      </form>`;
    document.body.append(signDlg);
    return signDlg;
  }

  async function begin({ page = S.current, rect = null } = {}) {
    if (!(await ensureIdentity())) return;
    window.Edit?.hidePill();
    const d = signDialog(), q = s => d.querySelector(s);
    const id = identity;
    const adhoc = window.Edit?.adhoc;
    const sig = adhoc ? { signature: { type: adhoc.spec.type }, preview: adhoc.preview } : await api.signature_get();
    const hasImg = !!sig.signature;
    let visible = !!rect;
    q(".sd-avatar").textContent = id.initials;
    q(".sd-name").textContent = id.name;
    q(".sd-meta").textContent = [id.email, id.org, `self-made ID, valid to ${id.expires}`].filter(Boolean).join(" · ");
    q(".sd-pw").value = "";
    q(".sd-err").hidden = true;
    q(".sd-reason").value = id.reason || "I approve this document";
    q(".sd-img").checked = hasImg;
    q(".sd-img").disabled = !hasImg;
    q(".sd-img").parentElement.title = hasImg ? "" : "Set up your signature with Sign → My signature first";
    if (hasImg) q(".sd-sigimg").src = sig.preview;
    const stem = (S.doc.source || S.doc.name).split(/[\\/]/).pop().replace(/\.pdf$/i, "").replace(/_edited$/, "");
    q(".sd-note-text").textContent = `${S.doc.dirty ? "Your edits are included. " : ""}The signed copy is saved as ${stem}_signed.pdf next to the original. ` +
      "Editing that copy later breaks the seal - keep the original for changes.";
    const refresh = () => {
      d.querySelectorAll("[data-vis]").forEach(b => b.setAttribute("aria-checked", String((b.dataset.vis === "1") === visible)));
      q("[data-vis='1']").disabled = !rect;
      q("[data-vis='1']").title = rect ? "" : "Drag a box on the page with Sign → Digital signature to place a visible signature";
      q(".sd-stamp").classList.toggle("hidden-stamp", !visible);
      q(".sd-img").parentElement.hidden = !visible;
      q(".sd-sigimg").hidden = !(visible && q(".sd-img").checked && hasImg);
      q(".sd-cap").textContent = visible ? `Appearance on page ${page + 1}` : "No box on the page - the signature is still checked by every PDF viewer";
      const now = new Date().toLocaleString(undefined, { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });
      q(".sd-stamp-text").replaceChildren("Digitally signed by ", el("strong", { text: id.name }), el("br"), now,
        ...(q(".sd-reason").value.trim() ? [el("br"), "Reason: " + q(".sd-reason").value.trim()] : []));
    };
    d.querySelectorAll("[data-vis]").forEach(b => { b.onclick = () => { visible = b.dataset.vis === "1" && !!rect; refresh(); }; });
    q(".sd-img").onchange = refresh;
    q(".sd-reason").oninput = refresh;
    q(".sd-cer").onclick = exportCertificate;
    q(".sd-change").onclick = e => showMenu([
      { label: "Set up a new signing identity…", icon: "shield", run: async () => { d.close("cancel"); if (await setupIdentity(true)) begin({ page, rect }); } },
      { label: "Save my certificate to share (.cer)…", icon: "export", run: exportCertificate },
    ], 0, 0, e.currentTarget);
    refresh();
    d.returnValue = "";
    d.showModal();
    q(".sd-pw").focus();

    const onClose = async () => {
      if (d.returnValue !== "ok") { window.Edit?.cancel(); return; }
      const err = msg => { q(".sd-err").textContent = msg; q(".sd-err").hidden = false; d.returnValue = ""; d.showModal(); q(".sd-pw").select(); d.addEventListener("close", onClose, { once: true }); };
      if (!q(".sd-pw").value) return err("Type your signing password.");
      q(".sd-ok").disabled = true;
      setStatus(q(".sd-ts").checked ? "Signing… (getting a timestamp from the internet)" : "Signing…");
      const r = await api.sign_document({
        password: q(".sd-pw").value, reason: q(".sd-reason").value, timestamp: q(".sd-ts").checked, lock: q(".sd-lock").checked,
        page, rect: visible ? rect : null, with_image: visible && q(".sd-img").checked, sig: adhoc?.spec || null,
      });
      q(".sd-ok").disabled = false;
      if (r.error) return err(r.error);
      window.Edit?.cancel();
      setStatus(r.status);
      toast(r.status + " Your original is unchanged.", { sticky: true, actions: [
        { label: "Open signed copy", run: () => openPath(r.signed) },
        { label: "Show in folder", run: () => api.show_in_folder(r.signed) },
      ] });
    };
    d.addEventListener("close", onClose, { once: true });
  }

  // ------------------------------------------------------------------ verify panel
  let vDlg = null;
  function verifyDialog() {
    if (vDlg) return vDlg;
    vDlg = el("dialog", { class: "dialog verify-dlg" });
    vDlg.innerHTML = `
      <form method="dialog">
        <div class="td-head"><span class="dlg-icon ok v-icon"><svg><use href="#i-shield"/></svg></span>
          <div class="sd-titles"><h3>Signatures</h3><span class="muted v-file"></span></div>
          <button class="icon-btn sm td-x" value="close" aria-label="Close"><svg><use href="#i-x"/></svg></button></div>
        <p class="note-amber small v-unsaved" hidden><svg><use href="#i-alert"/></svg><span>You have unsaved edits. This checks the file as it is on disk.</span></p>
        <ul class="v-list"></ul>
        <div class="dlg-actions">
          <button type="button" class="link-btn v-folder">${here("Trusted people on this PC")}</button>
          <span class="spacer"></span>
          <button class="btn btn-primary" value="close">Close</button>
        </div>
      </form>`;
    document.body.append(vDlg);
    vDlg.querySelector(".v-folder").onclick = () => api.trusted_folder();
    return vDlg;
  }

  function renderVerify(r) {
    const d = verifyDialog(), q = s => d.querySelector(s);
    q(".v-file").textContent = r.file;
    q(".v-unsaved").hidden = !r.unsaved_edits;
    const worst = r.signatures.some(s => s.level === "bad") ? "bad" : r.signatures.some(s => s.level === "warn") ? "warn" : "ok";
    q(".v-icon").className = "dlg-icon v-icon " + worst;
    q(".v-list").replaceChildren(...(r.signatures.length ? r.signatures.map(s => el("li", { class: "v-item " + s.level },
      el("span", { class: "v-badge" }, icon(s.level === "ok" ? "shield" : "shield-alert")),
      el("div", { class: "v-body" },
        el("div", { class: "v-who" }, el("strong", { text: s.signer }), el("span", { class: "muted small", text: s.when ? " · " + s.when : "" })),
        el("p", { text: s.sentence }),
        el("div", { class: "v-tags" },
          s.trusted ? el("span", { class: "tag ok", text: here("Trusted on this PC") }) : null,
          s.timestamped ? el("span", { class: "tag", text: "Timestamped" }) : null,
          s.locked ? el("span", { class: "tag", text: "Locked by signer" }) : null)),
      s.can_trust ? el("button", { type: "button", class: "btn btn-outline sm", text: `Trust ${s.signer}`, onclick: async () => {
        const t = await api.trust_signer(s.index);
        if (fail(t)) return;
        renderVerify(t); setStatus(t.status); toast(t.status);
        if (window.checkSignatures) window.checkSignatures();
      } }) : null))
      : [el("li", { class: "v-empty muted", text: "This document has no digital signatures." })]));
  }

  async function verify() {
    setStatus("Checking signatures…");
    const r = await api.verify_report();
    if (fail(r)) return;
    renderVerify(r);
    setStatus(r.signatures.length ? `${r.signatures.length} signature${r.signatures.length > 1 ? "s" : ""} checked.` : "No digital signatures in this document.");
    const d = verifyDialog();
    if (!d.open) d.showModal();
  }

  window.Sign = { begin, verify, ensureIdentity, setupIdentity, exportCertificate, cancelPlacement() {} };
})();
