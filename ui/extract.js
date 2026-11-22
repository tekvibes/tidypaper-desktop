/* Pull out data: everything in it (no template), tables, bank statements, invoices, payslips or your own fields -> a
   table to check, edit, copy or save as CSV / Excel / JSON (bridge_extract.py).  Uses app.js globals (api, S, el, icon, $, toast, fail, setStatus, goTo). */
"use strict";
(() => {
  const LAST = new Map();             // the open documents' last results (this session), so Done doesn't lose them
  const docKey = () => S.doc?.source || S.doc?.name || "";
  const plural = (n, one, many = one + "s") => `${n} ${n === 1 ? one : many}`;
  const fmtMoney = n => "£" + n.toFixed(2);

  function dialog(cls, html) {
    const d = el("dialog", { class: "dialog " + cls });
    d.innerHTML = html;
    document.body.append(d);
    d.addEventListener("close", () => setTimeout(() => d.remove(), 0), { once: true });
    return d;
  }

  // ------------------------------------------------------------------ 1. what to pull out
  async function open() {
    if (!S.doc) return;
    await window.Edit?.commitPending();
    setStatus("Looking at the document…");
    const o = await api.extract_options();
    if (fail(o)) return;
    setStatus("Choose what to pull out.");
    const last = LAST.get(docKey());
    const d = dialog("xd-pick", `<form method="dialog">
      <div class="td-head"><span class="dlg-icon"><svg><use href="#i-list"/></svg></span>
        <div class="sd-titles"><h3>Pull out data</h3><span class="muted">Into a table you can check, change, copy or save for a spreadsheet.</span></div>
        <button class="icon-btn sm td-x" value="cancel" aria-label="Close"><svg><use href="#i-x"/></svg></button></div>
      <div class="xd-choices" role="radiogroup" aria-label="What to pull out"></div>
      <div class="xd-own" hidden>
        <label class="field"><span>What to pull out - one on each line</span>
          <textarea class="st-in xd-labels" rows="4" placeholder="Policy number&#10;Start date&#10;Premium"></textarea></label>
        <label class="c xd-list"><input type="checkbox" class="xd-repeat"> These repeat - one row for each
          <input class="st-in xd-one" value="item" aria-label="What each row is" size="10"></label>
      </div>
      <p class="xd-est muted small" role="status"></p>
      <div class="dlg-actions"><button type="button" class="link-btn xd-last" hidden>Show the last results</button>
        <span class="spacer"></span><button class="btn btn-ghost" value="cancel" formnovalidate>Cancel</button>
        <button class="btn btn-primary xd-go" value="ok">Pull out data</button></div></form>`);
    const box = d.querySelector(".xd-choices");
    const choices = [];
    if (o.all) choices.push({ ...o.all, ai: true });
    if (o.tables) choices.push({ id: "tables", label: "Tables as they are",
      about: `${plural(o.tables, "table")}, ${plural(o.table_rows, "row")} - read on this ${APP.mac ? "Mac" : "PC"}, nothing sent anywhere` });
    for (const t of o.templates) choices.push({ ...t, ai: true });
    choices.push({ id: "own", label: "Choose your own fields", about: "Name what you want - from a letter, a policy, a report…", ai: true });
    let chosen = choices.find(c => !c.ai || o.ai)?.id || "own";      // everything in it, when AI is set up
    for (const c of choices) {
      const off = c.ai && !o.ai;
      const r = el("label", { class: "xd-choice" + (off ? " off" : "") },
        el("input", { type: "radio", name: "xd", value: c.id, disabled: off, checked: c.id === chosen,
          onchange: () => { chosen = c.id; update(); } }),
        el("span", {}, el("strong", { text: c.label }), el("small", { class: "muted", text: c.about })));
      box.append(r);
    }
    const own = d.querySelector(".xd-own"), labels = d.querySelector(".xd-labels"), repeat = d.querySelector(".xd-repeat");
    labels.addEventListener("input", update); repeat.addEventListener("change", update);
    const est = d.querySelector(".xd-est"), go = d.querySelector(".xd-go");
    function update() {
      own.hidden = chosen !== "own";
      if (chosen === "tables") { est.textContent = "Nothing is sent - the tables are read from the document's own text."; go.disabled = false; return; }
      if (!o.ai) { est.textContent = o.ai_note || ""; go.disabled = true; return; }
      const e = o.estimates[chosen === "own" ? (repeat.checked ? "own_list" : "own") : chosen];
      const parts = [`Sends ${plural(e.pages, "page")} to ${o.provider}${e.parts > 1 ? ` in ${e.parts} parts, a few pages at a time` : ""}`];
      if (e.chars) parts[0] += ` (${e.chars.toLocaleString()} characters of text${e.pictures ? `, and pictures of ${plural(e.pictures, "scanned page")}` : ""})`;
      else if (e.pictures) parts[0] += ` (pictures of ${plural(e.pictures, "scanned page")})`;
      if (e.read_here) parts.push(`${plural(e.read_here, "scanned page")} read on this ${APP.mac ? "Mac" : "PC"} first`);
      if (e.gbp != null) parts.push(`about ${e.gbp < 0.01 ? "under 1p" : e.gbp < 1 ? Math.round(e.gbp * 100) + "p" : fmtMoney(e.gbp)} of your allowance`);
      if (o.month?.limit != null) parts.push(`${fmtMoney(o.month.spent)} of ${fmtMoney(o.month.limit)} used this month`);
      est.textContent = parts.join(" · ") + ".";
      go.disabled = chosen === "own" && !labels.value.trim();
    }
    update();
    if (last) {
      const b = d.querySelector(".xd-last");
      b.hidden = false;
      b.onclick = () => { d.close("cancel"); results(last); };
    }
    d.showModal();
    d.addEventListener("close", async () => {
      if (d.returnValue !== "ok") return;
      if (chosen === "tables") {
        setStatus("Reading the tables…");
        const r = await api.extract_tables();
        if (fail(r)) return;
        setStatus(r.status);
        return show(r.results);
      }
      const ownSpec = chosen === "own" ? { labels: labels.value.split("\n"), list: repeat.checked, one: d.querySelector(".xd-one").value } : null;
      run(chosen, ownSpec);
    }, { once: true });
  }

  // ------------------------------------------------------------------ 2. working
  async function run(tid, ownSpec) {
    const r = await api.extract_start(tid, ownSpec);
    if (fail(r)) return;
    const d = dialog("xd-run", `<form method="dialog">
      <div class="td-head"><span class="dlg-icon"><svg><use href="#i-list"/></svg></span>
        <div class="sd-titles"><h3>Pulling out the data…</h3><span class="muted xd-prog">Starting…</span></div></div>
      <div class="xd-bar"><i></i></div>
      <div class="dlg-actions"><button class="btn btn-ghost" value="stop">Stop</button></div></form>`);
    d.addEventListener("cancel", e => e.preventDefault());
    d.showModal();
    d.addEventListener("close", () => { if (d.returnValue === "stop") api.extract_cancel(r.job); }, { once: true });
    const bar = d.querySelector(".xd-bar i"), prog = d.querySelector(".xd-prog");
    for (;;) {
      await new Promise(res => setTimeout(res, 300));
      const st = await api.extract_status(r.job);
      if (fail(st)) { d.close(); return; }
      prog.textContent = st.progress || "";
      bar.style.width = `${Math.round(100 * Math.max(0, (st.part || 1) - 0.5) / Math.max(1, st.parts || 1))}%`;
      if (!st.done) continue;
      if (d.open) d.close();
      if (st.cancelled) return setStatus("Stopped.");
      if (st.problem) return toast(st.problem, { error: true });
      const list = st.results || [st.result];
      const tables = list.filter(x => x.template.rows).length, rows = list.reduce((n, x) => n + x.rows.length, 0);
      const gaps = list[0].missed?.length ? " Some figures may have been left out - see the top of the results." : "";
      setStatus((tid === "all" ? `${plural(tables, "table")}, ${plural(rows, "row")} pulled out - check them against the pages.`
        : `${plural(rows, "row")} pulled out - check them against the pages.`) + gaps);
      return show(list);
    }
  }

  // ------------------------------------------------------------------ 3. check, change, copy, save
  function show(list) {
    const state = { list, at: 0 };
    LAST.set(docKey(), state);
    results(state);
  }

  function results(state) {
    const d = dialog("xd-res", `<form method="dialog">
      <div class="td-head"><span class="dlg-icon"><svg><use href="#i-list"/></svg></span>
        <div class="sd-titles"><h3 class="xd-title"></h3><span class="muted xd-sub"></span></div>
        <select class="st-in xd-which" aria-label="Which table" hidden></select>
        <button class="icon-btn sm td-x" value="done" aria-label="Close"><svg><use href="#i-x"/></svg></button></div>
      <ul class="xd-checks"></ul>
      <div class="xd-scroll"><div class="xd-fields"></div><table class="xd-table"></table><ol class="xd-foot"></ol></div>
      <div class="dlg-actions"><button type="button" class="btn btn-ghost xd-add"><svg><use href="#i-plus"/></svg>Add a row</button>
        <button type="button" class="link-btn small xd-report" hidden>Report this answer</button>
        <span class="spacer"></span>
        <button type="button" class="btn btn-ghost xd-copy"><svg><use href="#i-copy"/></svg>Copy</button>
        <button type="button" class="btn btn-ghost xd-csv"><svg><use href="#i-export"/></svg>Save as CSV…</button>
        <button type="button" class="btn btn-ghost xd-xlsx"><svg><use href="#i-export"/></svg>Save as Excel…</button>
        <button type="button" class="btn btn-ghost xd-json"><svg><use href="#i-export"/></svg>Save as JSON…</button>
        <button class="btn btn-primary" value="done">Done</button></div></form>`);
    const which = d.querySelector(".xd-which");
    if (state.list.length > 1) {
      which.hidden = false;
      which.replaceChildren(...state.list.map((r, i) => el("option", { value: i,
        text: r.template.rows && r.template.id !== "table" ? `${r.template.label} · ${plural(r.rows.length, "row")}` : r.template.label })));
      which.value = state.at;
      which.onchange = () => { state.at = +which.value; render(); if (cur().template.id === "found_values") recheck(); };
    }
    const cur = () => state.list[state.at];
    const report = d.querySelector(".xd-report");      // the AI's results only: "Tables as they are" are the document's own
    report.hidden = state.list.every(r => r.template.id === "table");
    report.onclick = () => window.AI?.report("data");
    let timer = 0;
    const recheck = () => {
      clearTimeout(timer);
      timer = setTimeout(async () => {
        const r = cur();
        if (r.template.id === "table") return;
        // everything in it: its values are checked against the tables found with them
        const tables = r.template.id === "found_values"
          ? state.list.filter(x => x.template.id === "found").map(x => ({ template: x.template, rows: x.rows })) : null;
        const c = await api.extract_check(r.template, r.fields, r.rows, tables);
        if (!c.error) { r.checks = c.checks; renderChecks(); }
      }, 350);
    };
    const pageBtn = p => p ? el("button", { type: "button", class: "ai-cite", title: "Show the page", onclick: () => { goTo(p - 1); } },
      icon("file"), `p.${p}`) : el("span", { class: "xd-nop" });

    function renderChecks() {
      const r = cur();
      const ul = d.querySelector(".xd-checks");
      // what the run seems to have left out (a table or page whose figures aren't in the results) - over every table
      const missed = (state.list[0].missed || []).map(c => el("li", { class: "xd-check warn xd-missed" },
        icon("alert"), el("span", { text: c.text }), pageBtn(c.page)));
      ul.replaceChildren(...missed, ...(r.checks || []).map(c => el("li", { class: "xd-check " + c.level },
        icon(c.level === "ok" ? "check" : "alert"), el("span", { text: c.text }))),
        ...(r.doubts || []).map(t => el("li", { class: "xd-check warn" }, icon("alert"), el("span", { text: `Not sure: ${t}` }))));
      ul.hidden = !ul.children.length;
      const flagged = new Set((r.checks || []).filter(c => c.level !== "ok").flatMap(c => c.rows));
      d.querySelectorAll(".xd-table tbody tr").forEach((tr, i) => tr.classList.toggle("flag", flagged.has(i)));
      const ff = new Set((r.checks || []).filter(c => c.level !== "ok").flatMap(c => c.fields));
      d.querySelectorAll(".xd-field").forEach(f => f.classList.toggle("flag", ff.has(f.dataset.key)));
    }

    function render() {
      const r = cur(), t = r.template;
      const found = t.id === "found" || t.id === "found_values";
      d.querySelector(".xd-title").textContent = t.id === "table" ? "Tables" : found ? (r.document_type || "Everything in it") : t.label;
      const n = r.rows.length;
      d.querySelector(".xd-sub").textContent = t.id === "table"
        ? `${plural(n, "row")} · read from the document's own text`
        : `${found && state.list.length === 1 ? t.label + " · " : ""}${t.rows && state.list.length === 1 ? plural(n, "row") + " · " : ""}${t.note ? t.note + " · " : ""}from ${r.provider || "the AI"} - check the figures against the pages`;
      const fields = d.querySelector(".xd-fields");
      let section = null;
      fields.replaceChildren(...t.fields.flatMap(f => {
        const v = r.fields[f.key] || (r.fields[f.key] = { value: "", page: 0 });
        const inp = el("input", { class: "st-in", value: v.value, "aria-label": f.label,
          oninput: () => { v.value = inp.value; recheck(); } });
        const head = found && (f.section || "") !== section ? [el("h4", { class: "xd-sec", text: (section = f.section || "") || "Other" })] : [];
        return [...head, el("label", { class: "xd-field", "data-key": f.key, title: f.unit ? `${f.label} (${f.unit})` : f.label },
          el("span", {}, f.label, f.unit ? el("small", { class: "muted", text: ` · ${f.unit}` }) : ""), inp, pageBtn(v.page))];
      }));
      fields.hidden = !t.fields.length;
      const table = d.querySelector(".xd-table");
      table.replaceChildren();
      d.querySelector(".xd-add").hidden = !t.rows;
      if (t.rows) {
        const cols = t.rows.columns;
        table.append(el("thead", {}, el("tr", {}, ...cols.map(c => el("th", { class: c.type, text: c.label })), el("th", { text: "Page" }), el("th"))));
        const body = el("tbody");
        const nums = cols.filter(c => c.type === "number");
        const heading = row => t.id === "found" && nums.length && nums.every(c => !row.cells[c.key]) && cols.some(c => row.cells[c.key]);
        r.rows.forEach((row, i) => body.append(el("tr", { class: heading(row) ? "head" : "" }, ...cols.map(c => {
          const inp = el("input", { value: row.cells[c.key] || "", class: c.type, "aria-label": `${c.label}, row ${i + 1}`,
            oninput: () => { row.cells[c.key] = inp.value; recheck(); } });
          return el("td", {}, inp);
        }), el("td", {}, pageBtn(row.page)),
        el("td", {}, el("button", { type: "button", class: "icon-btn sm", "aria-label": `Delete row ${i + 1}`, title: "Delete this row",
          onclick: () => { r.rows.splice(i, 1); render(); recheck(); } }, icon("trash"))))));
        table.append(body);
      }
      // the table's footnotes, as printed (a row's marks are in its Footnote column)
      const foot = d.querySelector(".xd-foot");
      foot.replaceChildren(...(t.footnotes || []).map(f => el("li", {}, f.mark ? el("b", { text: f.mark + " " }) : "", f.text)));
      foot.hidden = !foot.children.length;
      renderChecks();
    }
    render();

    // what's saved / copied: the table shown (CSV, Copy) or every table (Excel), as the person left them
    const sheet = r => ({
      kind: r.template.id, name: r.template.rows?.label || r.template.label, note: r.template.note || "", document_type: r.document_type || "",
      fields: r.template.fields.map(f => ({ key: f.key, label: f.label, value: r.fields[f.key]?.value || "",
        page: r.fields[f.key]?.page || 0, type: f.type, section: f.section || "", unit: f.unit || "" })),
      columns: (r.template.rows?.columns || []).map(c => ({ key: c.key, label: c.label, type: c.type })),
      rows: r.rows.map(row => ({ cells: (r.template.rows?.columns || []).map(c => row.cells[c.key] || ""), page: row.page })),
      footnotes: r.template.footnotes || [],
      checks: (r.checks || []).map(c => ({ level: c.level, text: c.text })), doubts: r.doubts || [],
      missed: (r.missed || []).map(c => ({ level: c.level, text: c.text, page: c.page })) });
    d.querySelector(".xd-add").onclick = () => {
      const r = cur();
      r.rows.push({ cells: {}, page: 0 });
      render();
      d.querySelector(".xd-table tbody tr:last-child input")?.focus();
    };
    d.querySelector(".xd-copy").onclick = async () => {
      const s = sheet(cur());
      const clean = v => String(v ?? "").replace(/[\t\r\n]+/g, " ");
      const sectioned = s.fields.some(f => f.section || f.unit);
      const lines = s.rows.length ? [[...s.columns.map(c => c.label), "Page"], ...s.rows.map(r => [...r.cells, r.page || ""])]
        : sectioned ? s.fields.map(f => [f.section, f.label, f.value, f.unit]) : s.fields.map(f => [f.label, f.value]);
      try { await navigator.clipboard.writeText(lines.map(l => l.map(clean).join("\t")).join("\n")); }
      catch { return toast("Couldn't copy - try Save as CSV instead.", { error: true }); }
      toast(s.rows.length ? `Copied ${plural(s.rows.length, "row")} - paste into a spreadsheet.` : "Copied - paste into a spreadsheet.");
    };
    const save = async fmt => {
      const r = await api.extract_save(fmt, fmt === "csv" ? [sheet(cur())] : state.list.map(sheet));      // Excel, JSON: all of it
      if (r.cancelled || fail(r)) return;
      setStatus(r.status);
      toast(r.status, { actions: [{ label: "Show in folder", run: () => api.show_in_folder(r.saved) }] });
    };
    d.querySelector(".xd-csv").onclick = () => save("csv");
    d.querySelector(".xd-xlsx").onclick = () => save("xlsx");
    d.querySelector(".xd-json").onclick = () => save("json");
    d.showModal();
  }

  window.Extract = { open };
})();
