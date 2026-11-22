/* Settings window: appearance + the AI assistant (provider, key, model, privacy) + quick decisions + My details.
   Uses app.js globals (api, el, icon, $, $$, toast, fail, applyTheme, applyLook). */
"use strict";
(() => {
  let dlg = null, data = null;

  function field(label, control, hint) {
    return el("label", { class: "field" }, el("span", { text: label }), control, hint ? el("small", { class: "muted st-hint", text: hint }) : null);
  }

  function build() {
    dlg = el("dialog", { class: "dialog settings-dlg", "aria-label": "Settings" });
    dlg.innerHTML = `
      <form method="dialog">
        <div class="td-head"><h3>Settings</h3><button class="icon-btn sm td-x" value="close" aria-label="Close"><svg><use href="#i-x"/></svg></button></div>
        <div class="st-tabs seg" role="tablist">
          <button type="button" role="tab" data-tab="look"><svg><use href="#i-sun"/></svg>Appearance</button>
          <button type="button" role="tab" data-tab="ai"><svg><use href="#i-ask"/></svg>AI assistant</button>
          <button type="button" role="tab" data-tab="details"><svg><use href="#i-form"/></svg>My details</button>
          <button type="button" role="tab" data-tab="updates"><svg><use href="#i-download"/></svg>Updates</button>
        </div>
        <section class="st-panel" data-panel="look"></section>
        <section class="st-panel" data-panel="ai"></section>
        <section class="st-panel" data-panel="details"></section>
        <section class="st-panel" data-panel="updates"></section>
        <p class="st-status muted small" role="status" aria-live="polite"></p>
        <div class="dlg-actions"><button class="btn btn-primary" value="close">Done</button></div>
      </form>`;
    document.body.append(dlg);
    dlg.querySelectorAll("[data-tab]").forEach(b => { b.onclick = () => tab(b.dataset.tab); });
    dlg.addEventListener("close", () => window.AI?.refresh());
  }

  function tab(t) {
    dlg.querySelectorAll("[data-tab]").forEach(b => b.setAttribute("aria-selected", String(b.dataset.tab === t)));
    dlg.querySelectorAll("[data-panel]").forEach(p => { p.hidden = p.dataset.panel !== t; });
  }
  const say = (msg, bad = false) => { const s = dlg.querySelector(".st-status"); s.textContent = msg || ""; s.classList.toggle("bad", bad); };

  async function save(patch) {
    const r = await api.ai_settings_set(patch);
    if (r.error) { say(r.error, true); return false; }
    data = r;
    return true;
  }

  // ------------------------------------------------------------------ appearance
  function renderLook() {
    const cur = document.documentElement.dataset.theme || "system";
    const seg = el("div", { class: "seg", role: "radiogroup", "aria-label": "Appearance" },
      ...[["system", APP.mac ? "Match macOS" : "Match Windows", "monitor"], ["light", "Light", "sun"], ["dark", "Dark", "moon"]].map(([v, label, ic]) =>
        el("button", { type: "button", role: "radio", "aria-checked": String(cur === v),
          onclick: async () => { applyTheme(v); await api.theme_set(v); renderLook(); } }, icon(ic), label)));
    const curLook = document.documentElement.dataset.look || "classic";
    const looks = el("div", { class: "st-looks", role: "radiogroup", "aria-label": "Look" },
      ...LOOKS.map(L => el("button", { type: "button", role: "radio", class: "st-look", "aria-checked": String(curLook === L.id),
          onclick: async () => {
            applyLook(L.id);
            const r = await api.look_set(L.id);
            if (r.error) say(r.error, true);
            renderLook();
          } },
        L.id === "classic" ? el("span", { class: "st-look-mark brand-mark", style: "background:#111827;color:#fff" }, icon("file-pdf"))
          : el("img", { class: "st-look-mark", src: `looks/${L.id}-mark.png`, alt: "" }),
        el("span", { class: "st-look-text" }, el("strong", { text: L.name }), el("small", { class: "muted", text: L.about })),
        el("span", { class: "st-look-sw", "aria-hidden": "true" }, ...L.sw.map(c => el("i", { style: `background:${c}` }))))));
    dlg.querySelector("[data-panel=look]").replaceChildren(
      field("Light or dark", seg, APP.mac ? "Match macOS follows your Mac's own setting." : "Match Windows follows your PC's own setting."),
      el("div", { class: "field" }, el("span", { text: "Look (preview)" }), looks,
        el("small", { class: "muted st-hint", text: "Trying out new colours, fonts and icons - the buttons and screens stay where they are."
          + (APP.mac ? " The Dock shows the look's icon while the app is open." : "") })));
  }

  const LOOKS = [
    { id: "classic", name: "Classic", about: "The look you have now", sw: ["#111827", "#2563eb", "#eceae6"] },
    { id: "highlighter", name: "Highlighter", about: "Ink navy with one bright yellow", sw: ["#14213d", "#ffd447", "#2f5bea"] },
    { id: "teal", name: "Calm Teal", about: "Soft and friendly, rounder corners", sw: ["#0e5c57", "#e8664a", "#eef5f2"] },
    { id: "midnight", name: "Midnight Folio", about: "Midnight blue and violet, best in Dark", sw: ["#0f1024", "#5b3fe0", "#5ee6b5"] },
  ];

  // ------------------------------------------------------------------ updates (bridge_more / updater.py)
  let updTimer = null;
  async function renderUpdates(note) {
    clearTimeout(updTimer);
    const box = dlg.querySelector("[data-panel=updates]");
    const r = await api.update_settings_get();
    if (r.error) return box.replaceChildren(el("p", { class: "field-error", text: r.error }));
    const st = r.update || {};
    const auto = el("input", { type: "checkbox", class: "st-switch-in", disabled: !r.possible || null });
    auto.checked = r.auto && r.possible;
    auto.onchange = async () => {
      const x = await api.update_settings_set(auto.checked);
      if (!fail(x)) renderUpdates(auto.checked ? "New versions will be put in by themselves." : "You'll be told about new versions instead.");
    };
    const where = APP.mac ? "Mac" : "PC";
    const state = st.state === "downloading" ? `Getting ${APP.name} ${st.version}… ${st.total ? Math.round(100 * st.done / st.total) + "%" : ""}`
      : st.state === "checking" ? `Checking ${APP.name} ${st.version}…`
      : st.state === "ready" ? `${APP.name} ${st.version} is ready - it goes in when you close ${APP.name}.`
      : st.state === "failed" ? `${APP.name} ${st.version} couldn't be got: ${st.error || "unknown problem"}`
      : note || "";
    const check = el("button", { type: "button", class: "btn btn-outline sm", text: "Check now", disabled: !r.enabled || null, onclick: async () => {
      check.disabled = true; say("Checking…");
      const x = await window.Panels.checkUpdates(true);
      check.disabled = false;
      if (!x || x.error) return say(x?.error || "Couldn't check.", true);
      say(x.offline ? "Couldn't reach the update service - are you online?" : x.newer ? (x.auto ? `${APP.name} ${x.version} is on its way.` : `${APP.name} ${x.version} is available.`)
        : `You have the latest version (${r.current}).`);
      if (x.auto) { clearTimeout(updTimer); updTimer = setTimeout(() => dlg.open && renderUpdates(), 1500); }
    } });
    box.replaceChildren(...[                         // (replaceChildren writes a null as the word "null": filtered out)
      el("p", { class: "small", text: `This is ${APP.name} ${r.current}.` }),
      el("label", { class: "st-switch" }, auto, el("span", {},
        el("strong", { text: "Put new versions in by themselves" }),
        el("small", { class: "muted", text: r.possible
          ? `A new version is downloaded quietly, checked, and put in when you close ${APP.name} (or straight away with Restart now). Your documents and settings stay as they are.`
          : `This copy can't update itself (${r.why}) - you'll be told about new versions and given a link instead.` }))),
      el("div", { class: "st-row" }, check,
        st.state === "ready" ? el("button", { type: "button", class: "btn btn-primary sm", text: "Restart now", onclick: () => { dlg.close(); window.Panels.restartNow(); } }) : null),
      state ? el("p", { class: "muted small", role: "status", text: state }) : null].filter(Boolean));
    // keep following a download (the first draw happens before the dialog opens, so don't ask whether it's open yet)
    if (st.state === "downloading" || st.state === "checking") updTimer = setTimeout(() => dlg.open && renderUpdates(), 2000);
  }

  // ------------------------------------------------------------------ My details (also in the editor's … menu)
  async function renderDetails() {
    const box = dlg.querySelector("[data-panel=details]");
    const r = await api.details_get();
    const n = r.error ? 0 : r.fields.filter(f => r.details[f.key]).length + (r.custom || []).length;
    const where = APP.mac ? "in your Mac's Keychain" : "on this PC, encrypted for your Windows account";
    box.replaceChildren(
      el("p", { class: "muted small", text: "Your name, address, phone number and so on. Fill with my details (in the editor's … menu) " +
        "uses them to fill in forms for you. They're never sent to an AI service." }),
      el("p", { class: r.error ? "field-error" : "small", text: r.error || (n ? `${n} detail${n === 1 ? "" : "s"} saved ${where}.` : "Nothing saved yet.") }),
      el("button", { type: "button", class: "btn btn-ghost", onclick: async () => {
        dlg.close();
        await window.Fill?.details();
        open("details");                                // back to Settings, showing what's saved now
      } }, icon("edittext"), n ? "Edit my details…" : "Add my details…"));
  }

  // ------------------------------------------------------------------ AI
  function renderAi() {
    const { cfg, presets, decide_presets: dp, has_key: hasKey } = data;
    const p = cfg.provider, pre = presets[p];
    const box = dlg.querySelector("[data-panel=ai]");

    const onSwitch = el("input", { type: "checkbox", class: "st-switch-in" });
    onSwitch.checked = cfg.enabled;
    onSwitch.onchange = async () => { if (await save({ enabled: onSwitch.checked })) { renderAi(); say(onSwitch.checked ? "AI assistant on." : "AI assistant off - nothing is sent."); } };

    const provider = el("select", { class: "st-in" }, ...Object.entries(presets).map(([id, x]) => el("option", { value: id, text: x.label })));
    provider.value = p;
    provider.onchange = async () => { if (await save({ provider: provider.value })) { renderAi(); say(""); } };

    const key = el("input", { class: "st-in", type: "password", autocomplete: "off", spellcheck: "false",
      placeholder: pre.kind === "relay" ? (hasKey[p] ? here("Saved on this PC - paste a new code to replace it") : "Paste your code here")
        : hasKey[p] ? here("Saved on this PC - paste a new key to replace it") : "Paste your key here" });
    const keyRow = el("div", { class: "st-row" }, key,
      el("button", { type: "button", class: "btn btn-outline sm", text: pre.kind === "relay" ? "Save code" : "Save key", onclick: async () => {
        const r = await api.ai_key_set(p, key.value);
        if (r.error) return say(r.error, true);
        data = r; key.value = ""; renderAi(); say(r.status);
      } }),
      hasKey[p] ? el("button", { type: "button", class: "btn btn-ghost sm", text: "Remove", onclick: async () => {
        const r = await api.ai_key_clear(p); if (r.error) return say(r.error, true); data = r; renderAi(); say(r.status);
      } }) : null);

    const addr = el("input", { class: "st-in", type: "url", placeholder: "https://…/v1", value: cfg.base_urls[p] || pre.base_url || "" });
    addr.onchange = () => save({ base_urls: { [p]: addr.value.trim() === pre.base_url ? "" : addr.value.trim() } }).then(ok => ok && say("Address saved."));

    const listId = "st-models-" + p;
    const models = el("datalist", { id: listId }, ...(pre.models || []).map(m => el("option", { value: m })));
    const model = el("input", { class: "st-in", list: listId, spellcheck: "false", placeholder: pre.default_model || "Choose or type a model name",
      value: cfg.models[p] || pre.default_model || "" });
    model.onchange = () => save({ models: { [p]: model.value.trim() } }).then(ok => ok && say("Model saved."));
    const modelRow = el("div", { class: "st-row" }, model, models,
      el("button", { type: "button", class: "btn btn-ghost sm", text: "Load list", onclick: async () => {
        say("Getting the list of models…");
        const r = await api.ai_models(p);
        if (r.error) return say(r.error, true);
        models.replaceChildren(...r.models.map(m => el("option", { value: m })));
        say(`${r.models.length} models available - click the box to pick one.`);
        model.focus();
      } }));

    const test = el("button", { type: "button", class: "btn btn-primary sm", onclick: async () => {
      test.disabled = true; say("Testing…");
      const r = await api.ai_test(p);
      test.disabled = false;
      say(r.error || r.status, !!r.error);
    } }, icon("check"), "Test");

    const showSent = el("input", { type: "checkbox" });
    showSent.checked = cfg.show_sent;
    showSent.onchange = () => save({ show_sent: showSent.checked });
    const warnScans = el("input", { type: "checkbox" });
    warnScans.checked = cfg.warn_scans !== false;
    warnScans.onchange = () => save({ warn_scans: warnScans.checked });

    // quick decisions slot
    const dec = cfg.decide || { provider: "none", model: "" };
    const decSel = el("select", { class: "st-in" }, ...Object.entries(dp).map(([id, x]) => el("option", { value: id, text: x.label })));
    decSel.value = dec.provider;
    const decModel = el("input", { class: "st-in", spellcheck: "false", value: dec.model || dp[dec.provider]?.default_model || "",
      placeholder: "typesafe/jev-1.13", hidden: dec.provider === "none" });
    const saveDec = () => save({ decide: { provider: decSel.value, model: decModel.value } }).then(ok => ok && say("Saved."));
    decSel.onchange = () => { decModel.hidden = decSel.value === "none"; if (!decModel.value) decModel.value = dp[decSel.value]?.default_model || ""; saveDec(); };
    decModel.onchange = saveDec;

    // a family relay chooses the model itself (kept to the admin page): show this month's allowance instead of a picker
    const relayInfo = el("div", { class: "st-relay muted small", text: `Press Check to see what ${APP.ai} has set up for you.` });
    const showRelay = r => {
      if (r.error) return relayInfo.replaceChildren(el("span", { class: "st-bad", text: r.error }));
      if (r.old_relay) return relayInfo.replaceChildren(`${APP.ai} lets you choose the model.`);
      const m = r.month || {}, cur = r.currency === "GBP" ? "£" : "$";
      relayInfo.replaceChildren(
        el("div", {}, el("strong", { text: `${APP.ai} is set up for you` })),
        el("div", { text: `${r.person ? r.person + " · " : ""}${m.limit != null ? `${cur}${(m.spent || 0).toFixed(2)} of ${cur}${Number(m.limit).toFixed(2)} used this month` : `${cur}${(m.spent || 0).toFixed(2)} used this month`}` +
          `${r.pictures === false ? " · page pictures off" : ""}${r.paused ? " · paused" : ""}` }));
    };
    const relayRow = el("div", { class: "st-row" }, relayInfo,
      el("button", { type: "button", class: "btn btn-ghost sm", text: "Check", onclick: async () => showRelay(await api.ai_relay_info()) }));
    if (pre.kind === "relay" && hasKey[p] && (cfg.base_urls[p] || "")) api.ai_relay_info().then(showRelay);

    const needsAddr = (pre.kind === "openai" && (p === "custom" || p === "ollama")) || pre.kind === "relay";
    // an invitation from a family relay sets everything up in one step
    const inviteIn = el("input", { class: "st-in", placeholder: "Paste your invitation here", spellcheck: "false", autocomplete: "off",
      "aria-label": "Invitation" });
    const useInvite = async () => {
      if (!inviteIn.value.trim()) return say(keys("Paste the invitation first (Ctrl+V)."), true);
      say("Checking the invitation…");
      const r = await api.ai_join(inviteIn.value);
      if (r.error) return say(r.error, true);
      data = r; inviteIn.value = ""; renderAi(); say(r.status);
      window.AI?.refresh();
    };
    inviteIn.addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); useInvite(); } });
    const inviteBox = el("div", { class: "st-invite" },
      el("strong", { text: "Got an invitation?" }),
      el("small", { class: "muted", text: `If someone invited you to ${APP.ai}, paste the invitation they sent you - it connects everything in one go.` }),
      el("div", { class: "st-row" }, inviteIn, el("button", { type: "button", class: "btn btn-primary sm", text: "Use invitation", onclick: useInvite })));

    box.replaceChildren(
      inviteBox,
      el("label", { class: "st-switch" }, onSwitch, el("span", {},
        el("strong", { text: "Use the AI assistant" }),
        el("small", { class: "muted", text: "Off until you turn it on. When on, a document's text is sent to the service below only when you ask about it - you'll see which pages first." }))),
      el("fieldset", { class: "st-group", disabled: !cfg.enabled || null },
        field("Service", provider, pre.note || null),
        pre.needs_key || p === "custom" ? el("div", { class: "field" },
          el("span", {}, (pre.key_label || "Key") + " ", pre.key_url ? el("button", { type: "button", class: "link-btn small", text: "Get a key", onclick: () => api.open_external(pre.key_url) }) : null),
          keyRow, el("small", { class: "muted st-hint", text: APP.mac ? "Kept in your Mac's Keychain - never in a file, never shown again."
            : "Kept in Windows Credential Manager on this PC - never in a file, never shown again." })) : null,
        p === "ollama" ? el("p", { class: "muted small" }, "Install Ollama, then download a model (for example ", el("code", { text: "ollama pull llama3.2" }), "). ",
          el("button", { type: "button", class: "link-btn small", text: "ollama.com", onclick: () => api.open_external(pre.key_url) })) : null,
        needsAddr ? field("Address", addr) : null,
        pre.kind === "relay" ? field("Your allowance", relayRow)
          : field("Model", modelRow, p === "anthropic" ? "Claude Opus 5.5 is the most capable; Sonnet 5.5 and Haiku 5.5 are faster and cheaper." : null),
        el("div", { class: "st-row st-test" }, test, el("span", { class: "muted small", text: "Sends one tiny message to check the key and model." })),
        el("label", { class: "check" }, showSent, el("span", { text: "Show me exactly what's sent before every question" })),
        el("label", { class: "check" }, warnScans, el("span", { text: "Warn me when an AI that reads text only gets a scan" })),
        Object.keys(cfg.consent).some(k => cfg.consent[k]) ? el("button", { type: "button", class: "link-btn small", text: "Ask me again before sending documents",
          onclick: async () => { const all = {}; Object.keys(cfg.consent).forEach(k => { all[k] = false; }); if (await save({ consent: all })) { renderAi(); say("You'll be asked again before anything is sent."); } } }) : null),
      el("details", { class: "st-more" }, el("summary", { text: "Quick decisions (optional)" }),
        el("p", { class: "muted small", text: "A second, very fast kind of AI that answers set questions (yes/no, pick one) instead of writing text. It will power Fill with my details and the personal-details finder, coming next. OpenRouter Decisions uses your OpenRouter key." }),
        field("Service", decSel), decModel));
  }

  async function open(section = "ai") {
    if (!dlg) build();
    const r = await api.ai_settings_get();
    if (fail(r)) return;
    data = r;
    renderLook(); renderAi(); say("");
    await renderDetails();
    await renderUpdates();
    tab(section);
    if (!dlg.open) dlg.showModal();
  }

  window.Settings = { open };
})();
