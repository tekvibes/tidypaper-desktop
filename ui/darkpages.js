/* Dark pages that keep photos (Batch 5, `pagedark`) - on screen only: the file and printing never change.
   The page picture's colours are flipped with their hue kept (white paper dark, black words light, a red heading still
   red), then each photo is shown again in its own colours on top - the pictures page_layout() lists, except one
   covering (nearly) the whole page: that's a scan, a picture of text, and goes dark like a page.
   Off / On / With dark mode (the app's theme, or the computer's when the theme is Auto) - remembered (localStorage,
   like Two pages).  The iPad's DarkPages.swift does the same. */
(() => {
  const KEY = "tk.darkPages";
  const D = {
    mode: (() => { try { return localStorage.getItem(KEY) || "system"; } catch { return "system"; } })(),
    on: false,
    layouts: new Map(),          // "rev:pv:i" -> the photos' boxes on page i
  };
  const media = matchMedia("(prefers-color-scheme: dark)");

  function appDark() {
    const t = document.documentElement.dataset.theme || "system";
    return t === "dark" || (t === "system" && media.matches);
  }

  function apply() {
    const on = D.mode === "on" || (D.mode === "system" && appDark());
    const changed = on !== D.on;
    D.on = on;
    document.body.classList.toggle("dark-pages", on);
    if (changed) for (const img of document.querySelectorAll(".page > img, .thumb-img > img")) {
      const box = img.parentElement;
      rendered(box, +(box.dataset.i ?? box.closest("[data-i]")?.dataset.i), img);
    }
  }

  function set(mode) {
    D.mode = mode;
    try { localStorage.setItem(KEY, mode); } catch { /* private mode */ }
    apply();
    setStatus(mode === "on" ? "Dark pages: on (photos keep their colours)." : mode === "off" ? "Dark pages: off."
      : "Dark pages: when the app is dark.");
  }

  /** The photos on page i: [{x, y, w, h}] as fractions of the page (as shown). */
  async function photos(i) {
    const key = `${S.rev}:${S.pv[i] || 0}:${i}`;
    if (D.layouts.has(key)) return D.layouts.get(key);
    const r = await api.page_layout(i);
    const p = S.doc?.pages[i];
    const out = r.error || !p ? [] : (r.images || []).filter(im => !im.page_sized).map(im => {
      const [x0, y0, x1, y1] = im.rect;
      return { x: x0 / p.w, y: y0 / p.h, w: (x1 - x0) / p.w, h: (y1 - y0) / p.h };
    }).filter(b => b.w > 0.004 && b.h > 0.004);
    if (D.layouts.size > 400) D.layouts.clear();
    D.layouts.set(key, out);
    return out;
  }

  /** A page (or thumbnail) picture was drawn: put its photos back on top, in their own colours. */
  async function rendered(box, i, img) {
    box.querySelector(":scope > .keep-photos")?.remove();
    if (!D.on || !Number.isInteger(i) || !img?.src) return;
    const src = img.src;
    const list = await photos(i);
    if (!D.on || img.src !== src || !list.length) return;
    box.querySelector(":scope > .keep-photos")?.remove();
    const layer = el("div", { class: "keep-photos", "aria-hidden": "true" });
    for (const b of list) {
      const win = el("div", { class: "keep-photo" });
      win.style.cssText = `left:${b.x * 100}%;top:${b.y * 100}%;width:${b.w * 100}%;height:${b.h * 100}%`;
      const pic = el("img", { src, alt: "", draggable: "false" });
      pic.style.cssText = `width:${100 / b.w}%;height:${100 / b.h}%;left:${-b.x / b.w * 100}%;top:${-b.y / b.h * 100}%`;
      win.append(pic);
      layer.append(win);
    }
    img.after(layer);
  }

  function menuItem() {
    const now = { off: "Off", on: "On", system: "With dark mode" }[D.mode];
    return {
      label: `Dark pages: ${now}`, icon: "moon", run: () => showMenu([
        { label: "Off - white pages", checked: D.mode === "off", run: () => set("off") },
        { label: "On - dark pages, photos keep their colours", checked: D.mode === "on", run: () => set("on") },
        { label: "With dark mode - when the app is dark", checked: D.mode === "system", run: () => set("system") },
      ], innerWidth - 360, 64),
    };
  }

  media.addEventListener("change", apply);
  new MutationObserver(apply).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  apply();
  window.DarkPages = { rendered, menuItem, set, get on() { return D.on; }, get mode() { return D.mode; } };
})();
