/* Keyboard shortcuts (Batch 5 of the Folio features): every shortcut the app has, in one list, shown by ? (anywhere,
   not while typing) or More > Keyboard shortcuts.  Written the Windows way; keys() shows ⌘ ⇧ ⌥ on a Mac.
   When a shortcut is added or changed in app.js / edit.js / reading.js / ai.js, change it here too.
   Uses app.js globals: el, keys, $, APP. */
"use strict";
(() => {
  const LIST = [
    ["Anywhere", [
      ["Ctrl+O", "Open a file"],
      ["Ctrl+Shift+O", "Open a link (a PDF or a webpage)"],
      ["?", "This list of shortcuts"],
    ]],
    ["Open documents (tabs)", [
      ["Ctrl+T", "Home - open another document"],
      ["Ctrl+W", "Close the document"],
      ["Ctrl+Tab / Ctrl+Shift+Tab", "Next / previous document"],
      ["Ctrl+1 … Ctrl+8", "That document"],
      ["Ctrl+9", "The last one"],
    ]],
    ["Reading", [
      ["Page Down / Space", "Next page"],
      ["Page Up / Shift+Space", "Previous page"],
      ["Home / End", "First page / last page"],
      ["Ctrl+F", "Find words"],
      ["Enter / Shift+Enter", "Next / previous match (in Find)"],
      ["Ctrl++ / Ctrl+-", "Zoom in / out"],
      ["Ctrl+0", "Fit the page to the window"],
      ["F", "Focus mode (Esc to come back)"],
      ["B", "Bookmark this page"],
    ]],
    ["Marking up and editing", [
      ["V", "Select"],
      ["H", "Mark up (highlight, underline, strike)"],
      ["D", "Draw"],
      ["T", "Text, ticks, the date, notes, pictures"],
      ["E", "Edit text"],
      ["R", "Redact"],
      ["S", "Sign"],
      ["Esc", "Cancel, or back to Select"],
      ["Del", "Delete the picked item"],
      ["Ctrl+C", "Copy the selected words"],
      ["Ctrl+Z", "Undo"],
      ["Ctrl+Y", "Redo"],
      ["Ctrl+S", "Save (a copy - the original is never changed)"],
      ["Ctrl+Shift+S", "Save a copy as…"],
      ["Ctrl+P", "Print"],
    ]],
    ["Placing a signature or picture", [
      ["Arrow keys", "Nudge it (Shift for bigger steps)"],
      ["+ / -", "Bigger / smaller"],
      ["Enter", "Put it there"],
      ["Esc", "Cancel"],
    ]],
    ["Pages panel", [
      ["Arrow keys", "Move between pages"],
      ["Del", "Delete the page"],
      ["Shift+F10", "The page's menu (turn, copy, move…)"],
    ]],
    ["Ask and notes", [
      ["Enter", "Ask (Shift+Enter starts a new line)"],
      ["Ctrl+Enter", "Add the note"],
      ["Ctrl+A", "Select the whole conversation (in Ask)"],
    ]],
  ];

  let dlg = null;
  function dialog() {
    if (dlg) return dlg;
    dlg = el("dialog", { class: "dialog keys-dialog", "aria-labelledby": "keys-title" });
    dlg.innerHTML = `
      <form method="dialog">
        <div class="td-head"><h3 id="keys-title">Keyboard shortcuts</h3>
          <button class="icon-btn sm td-x" value="cancel" aria-label="Close"><svg><use href="#i-x"/></svg></button></div>
        <div class="keys-body" tabindex="0"></div>
        <div class="dlg-actions"><button class="btn btn-primary" value="ok">Done</button></div>
      </form>`;
    document.body.append(dlg);
    return dlg;
  }

  /** "Ctrl++ / Ctrl+-" -> <kbd> pieces, written for this computer. */
  function keyCells(spec) {
    const out = [];
    spec.split(" / ").forEach((alt, n) => {
      if (n) out.push(el("span", { class: "keys-or", text: "or" }));
      let shown = keys(alt.replace(/Ctrl\+\+/, "Ctrl+Plus")).replace(/Plus/, "+");
      if (APP.mac) shown = shown.replace(/Shift\+/g, "⇧").replace(/Enter/g, "↩").replace(/^Del$/, "⌫").replace(/^Esc$/, "esc");
      else shown = shown.replace(/^Del$/, "Delete");
      out.push(el("kbd", { text: shown }));
    });
    return out;
  }

  function open() {
    const d = dialog();
    // two columns of about the same length (they stack when the window is narrow); the list scrolls down
    const cols = [el("div", { class: "keys-col" }), el("div", { class: "keys-col" })], len = [0, 0];
    for (const [group, rows] of LIST) {
      const c = len[0] <= len[1] ? 0 : 1;
      len[c] += rows.length + 2;
      cols[c].append(el("section", { class: "keys-group" },
        el("h4", { text: group }),
        el("dl", {}, ...rows.flatMap(([k, what]) => [el("dt", {}, ...keyCells(k)), el("dd", { text: what })]))));
    }
    d.querySelector(".keys-body").replaceChildren(...cols);
    if (!d.open) d.showModal();
    d.querySelector(".btn-primary").focus();
  }

  // ? anywhere, except while typing (and not over another dialog)
  document.addEventListener("keydown", e => {
    if (e.key !== "?" || e.ctrlKey || e.metaKey || e.altKey) return;
    if (/^(input|textarea|select)$/i.test(document.activeElement?.tagName || "") || document.activeElement?.isContentEditable) return;
    if (document.querySelector("dialog[open]:not(.keys-dialog)")) return;
    e.preventDefault();
    open();
  });

  window.Shortcuts = { open, LIST };
})();
