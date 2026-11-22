"""Translate: a document's pages into another language, shown in a side panel beside the document, page by page.

One request per page (an answer is capped at 8,000 tokens; a page is well under that), a few pages at once
(at_once), each translation streaming in as it comes.  Before a long document one small request writes notes - its
key terms and how to address the reader - that every page is given, and each page is given the end of the page
before and the start of the page after, so pages translated side by side read as one translation and a sentence
split by a page break is translated whole.  Scanned pages go as pictures when the AI can read them (it reads any language; Tesseract here reads
English only), otherwise as Tesseract's text.  Through a relay, requests are marked X-TidyPaper-Job: translate so
its model for Translate is used (a cheaper one is usually fine).  Translations are kept per document, page and
language - for this session, and with the document's memory (docmemory.py, <key>.translate.json; not for
password-protected documents) so reopening the document shows them again.  A page whose words have changed since
isn't shown its old translation.

The prompt and the languages are in prompts.json ("translate"), shared with the iPad/iPhone app
(ios/TidyPaper/Translate.swift), which uses Apple's on-device Translation when it has the language.
"""

from __future__ import annotations

import hashlib
import unicodedata
import threading
import time
import uuid
from pathlib import Path

import bridge_ai as ai
import translate_pdf
from bridge_util import UserError, log, safe

SPEC = ai.PROMPTS["translate"]
LANGUAGES = [x["name"] for x in SPEC["languages"]]                       # the short list offered first
ALL_LANGUAGES = SPEC["languages"] + SPEC.get("other_languages", [])      # every language that can be typed
RELAY_JOB_HEADER = "X-TidyPaper-Job"
BUSY_TRIES = 5


def busy_wait() -> float:
    """Seconds until the relay's next minute (its per-minute limit starts again then)."""
    return 62 - time.time() % 60


def system_prompt(language: str) -> str:
    return SPEC["system"].format(language=language)


def page_prompt(page: int, name: str, language: str, picture: bool = False) -> str:
    """page: 1-based."""
    return SPEC["pictures_prompt" if picture else "prompt"].format(page=page, name=name, language=language)


def _head(text: str, n: int) -> str:
    """About the first n letters of text, cut at a space or line break (TranslateSpec's head on the iPad)."""
    t = text.strip()
    if len(t) <= n:
        return t
    cut = t[:n]
    k = max(cut.rfind(" "), cut.rfind("\n"))
    return (cut[:k] if k > n // 2 else cut).rstrip()


def _tail(text: str, n: int) -> str:
    """About the last n letters of text, cut at a space or line break."""
    t = text.strip()
    if len(t) <= n:
        return t
    cut = t[-n:]
    ks = [k for k in (cut.find(" "), cut.find("\n")) if k >= 0]
    k = min(ks) if ks else -1
    return (cut[k + 1:] if 0 <= k < n // 2 else cut).lstrip()


def neighbours(texts: list[str], i: int, chars: int | None = None) -> tuple[str, str]:
    """(the end of the page before, the start of the page after) page i (0-based), from the original - "" where
    there's no such page or it has no words."""
    n = chars or SPEC["context_chars"]
    before = _tail(texts[i - 1], n) if i > 0 else ""
    after = _head(texts[i + 1], n) if i + 1 < len(texts) else ""
    return before, after


def page_message(page: int, name: str, language: str, picture: bool = False, before: str = "", after: str = "",
                 skip_start: bool = False, notes: str = "") -> str:
    """What a page's request asks (page: 1-based): the notes, the pages either side, then page_prompt.  skip_start:
    the page before is translated in this job too, so it finishes a sentence that runs onto this page."""
    parts = []
    if notes:
        parts.append(SPEC["notes_block"].format(notes=notes))
    if before:
        parts.append(SPEC["before_block"].format(prev=page - 1, text=before))
    if after:
        parts.append(SPEC["after_block"].format(next=page + 1, text=after))
    if before and skip_start:
        parts.append(SPEC["skip_start"].format(page=page, prev=page - 1))
    if after:
        parts.append(SPEC["finish_end"].format(page=page, next=page + 1))
    parts.append(page_prompt(page, name, language, picture))
    return "\n\n".join(parts)


def notes_system(language: str) -> str:
    return SPEC["notes_system"].format(language=language)


def notes_prompt(name: str, language: str) -> str:
    return SPEC["notes_prompt"].format(name=name, language=language)


def notes_sample(texts: list[str], pages: list[int], budget: int | None = None) -> list[str]:
    """The pages' words for the notes request - each page cut to its share of budget letters, so a long document is
    read from start to end - and every other page empty.  All empty: no notes are needed."""
    use = [i for i in pages if texts[i].strip()]
    out = [""] * len(texts)
    total = sum(len(texts[i].strip()) for i in use)
    if len(use) < 2 or total < SPEC["notes_min_chars"]:
        return out
    share = max(300, (budget or SPEC["notes_chars"]) // len(use))
    for i in use:
        out[i] = _head(texts[i], share)
    return out


def clean_notes(text: str, limit: int | None = None) -> str:
    """The notes as given to every page: at most limit letters, cut at a line break."""
    n = limit or SPEC["notes_limit"]
    t = text.strip()
    if len(t) > n:
        t = t[:n]
        k = t.rfind("\n")
        t = t[:k] if k > 0 else t
    return t.strip()


def at_once(provider: str, base: str) -> int:
    """Pages sent at the same time: one to an AI on this computer (it does one thing at a time anyway)."""
    return ai.at_once(provider, base, SPEC.get("at_once", 4))


def page_hash(text: str) -> str:
    """A page's words, fingerprinted (stable between runs, unlike hash())."""
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:20]


# what the AI is told - a translation kept with a document is only shown again while it's the same (new instructions,
# e.g. headings and bullet points marked, translate the page afresh)
PROMPT_VERSION = page_hash(SPEC["system"] + "\n" + SPEC["prompt"] + "\n" + SPEC["pictures_prompt"])[:8]


def kept_hash(text: str) -> str:
    """What a kept translation is matched with: the page's words and the instructions it was made with."""
    return page_hash(PROMPT_VERSION + "\n" + text)


def language_key(s: str) -> str:
    """A name as it is compared: lower case, accents and other marks off, letters, digits and single spaces only."""
    s = unicodedata.normalize("NFKD", (s or "").lower())
    s = "".join(c if unicodedata.category(c)[0] in "LN" else " " for c in s if unicodedata.category(c) != "Mn")
    return " ".join(s.split())


def _distance(a: str, b: str) -> int:
    """Levenshtein: letters added, dropped or changed."""
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _keys(lang: dict) -> list[str]:
    return [k for k in dict.fromkeys(language_key(x) for x in [lang["name"], lang.get("own", ""), *lang.get("aliases", [])]) if k]


def find_languages(s: str, limit: int = 8) -> tuple[bool, list[str]]:
    """What a typed language name means -> (exact, names).  exact: one language, named exactly (its English name, its
    own name or another usual name, any capitals or accents).  Otherwise the likeliest languages, best first: names
    starting with what was typed, then a word in them starting with it, then names one to three letters away (a typo:
    one for up to 4 letters, two up to 8, three for longer).  ([] = no such language.)"""
    k = language_key(s)[:40]
    if not k:
        return False, []
    exact = [x["name"] for x in ALL_LANGUAGES if k in _keys(x)]
    if exact:
        return len(exact) == 1, exact[:limit]
    allow = 1 if len(k) <= 4 else 2 if len(k) <= 8 else 3
    ranked = []
    for x in ALL_LANGUAGES:
        best = None
        for key in _keys(x):
            if key.startswith(k):
                r = 0
            elif any(w.startswith(k) for w in key.split()[1:]):
                r = 1
            else:
                d = min(_distance(k, key), *(_distance(k, w) for w in key.split())) if len(k) >= 3 else allow + 1
                r = 1 + d if d <= allow else None
            if r is not None and (best is None or r < best):
                best = r
        if best is not None:
            ranked.append((best, x["name"]))
    ranked.sort()
    return False, [n for _, n in ranked[:limit]]


def language(s: str) -> dict:
    """The language typed or chosen -> its entry ({"name", "code", ...}); a UserError that says what was meant
    otherwise ("Did you mean Swahili?")."""
    t = " ".join((s or "").split())
    exact, names = find_languages(t)
    if exact:
        return next(x for x in ALL_LANGUAGES if x["name"] == names[0])
    if not t:
        raise UserError("Choose a language to translate into.")
    raise UserError(not_a_language(t, names))


def not_a_language(typed: str, names: list[str]) -> str:
    typed = typed[:40]
    if not names:
        return f"There's no language called \u201c{typed}\u201d that Translate knows - check the spelling, or choose one from the list."
    if len(names) == 1:
        return f"Did you mean {names[0]}?"
    names = names[:3]
    return f"Did you mean {', '.join(names[:-1])} or {names[-1]}?"


def language_name(s: str) -> str:
    """The language's name as Translate uses it ("french" -> "French", "Persian" -> "Farsi", "Kiswahili" -> "Swahili");
    a UserError for a name that isn't a language (only listed names ever reach the AI's instructions)."""
    return language(s)["name"]


def estimate(chars: list[int], pages: list[int], pics: set[int], see: bool, prices: dict | None, rate: float) -> dict:
    """Roughly what translating these pages sends and costs: a page's words come back about as long again."""
    pages = [i for i in pages if chars[i] or i in pics]          # a blank page isn't sent
    text = sum(chars[i] for i in pages if i not in pics)
    n_pics = sum(1 for i in pages if i in pics)
    tokens_in = text / 3.5 + len(pages) * 350 + (n_pics * 1600 if see else 0)
    tokens_out = text / 3.0 + n_pics * 700
    out = {"pages": len(pages), "chars": text, "pictures": n_pics if see else 0, "read_here": 0 if see else n_pics}
    if prices and prices.get("in") is not None:
        usd = (tokens_in * float(prices.get("in") or 0) + tokens_out * float(prices.get("out") or 0)) / 1_000_000
        out["gbp"] = round(usd * rate, 4)            # a page can be a fraction of a penny: added up in the panel
    return out


class TranslateMixin:
    """Mixed into app.Bridge (uses AiMixin's settings, page texts, pictures and provider calls)."""

    def _translate_jobs(self) -> dict:
        if not hasattr(self, "_tjobs"):
            self._tjobs = {}
        return self._tjobs

    def _translations(self) -> dict:
        """(document, language, page, the page's text) -> the translation, for this session."""
        if not hasattr(self, "_tcache"):
            self._tcache = {}
        return self._tcache

    def _translate_notes(self) -> dict:
        """(document, language) -> the notes written for it, for this session (Translate again uses them too)."""
        if not hasattr(self, "_tnotes"):
            self._tnotes = {}
        return self._tnotes

    def _translate_doc_key(self) -> str:
        return str(self._source or (self._model.path if self._model else "") or id(self._model))

    def _translate_mem(self):
        """(the document's memory key, DocMemory) - key None for a locked or unsaved document."""
        key = None if getattr(self, "_password", None) else getattr(self, "_doc_key", None)
        return key, self._memory()

    def _translate_recall(self, texts: list[str], lang: str):
        """Put translations kept with the document into this session's cache (pages whose words still match)."""
        key, mem = self._translate_mem()
        if not key:
            return
        doc, cache = self._translate_doc_key(), self._translations()
        for i, v in mem.load_translations(key, lang).items():
            if 0 <= i < len(texts) and v["h"] == kept_hash(texts[i]):
                cache.setdefault((doc, lang, i, hash(texts[i])), v["t"])

    def _translate_keep(self, lang: str, i: int, page_text: str, text: str):
        key, mem = self._translate_mem()
        if key:
            try:
                mem.save_translation(key, lang, i, kept_hash(page_text), text)
            except OSError:
                log.exception("keep translation")

    def _translate_target(self, has_pics: bool):
        """(cfg, provider, preset, base, model, key, relay config): through a relay its model for Translate (if it
        has one) - or for scans, its model that reads pictures - else the answer model."""
        cfg = self._ai_cfg()
        p, pre, base, model, key = self._ai_target(cfg)
        conf = self._relay_cfg(base, key) if pre["kind"] == "relay" else None
        if conf:
            x = conf.get("translate_model")
            if x and (not has_pics or ai.can_see_pictures(pre, p, x) or not conf.get("scan_model")):
                model = x
            elif has_pics and conf.get("scan_model") and conf.get("pictures", True):
                model = conf["scan_model"]
        return cfg, p, pre, base, model, key, conf

    @safe
    def translate_options(self):
        """What Translate can offer: the languages, the AI service, and per page what a translation would send and
        roughly cost (the panel adds them up for "this page" / "every page"), plus translations already made."""
        texts, name = self._texts()
        out = {"languages": LANGUAGES, "others": [x["name"] for x in SPEC.get("other_languages", [])], "page_count": len(texts), "ai": False, "provider": "", "month": None,
               "chars": [len(t) for t in texts], "pictures": [], "page_gbp": None, "consent": False}
        cfg = self._ai_cfg()
        if not cfg["enabled"]:
            out["ai_note"] = "Translate uses the AI assistant - turn it on in Settings, or paste an invitation in Ask."
            return out
        pics = self._picture_pages(texts)
        try:
            cfg, p, pre, base, model, key, conf = self._translate_target(bool(pics))
        except UserError as e:
            out["ai_note"] = str(e)
            return out
        see = bool(pics) and ai.can_see_pictures(pre, p, model) and self._relay_pictures_ok(pre, base, key)
        prices = ((conf or {}).get("prices") or {}).get(model)
        rate = float((conf or {}).get("usd_to_gbp") or 0.79)
        if prices and prices.get("in") is not None:
            out["page_gbp"] = [estimate([len(t) for t in texts], [i], pics, see, prices, rate).get("gbp", 0) for i in range(len(texts))]
        out.update(ai=True, provider=ai.short_name(p, base), model=model, pictures=sorted(pics), see=see,
                   consent=bool(cfg["consent"].get(p)), relay=pre["kind"] == "relay", month=(conf or {}).get("month"),
                   local=p == "ollama")
        return out

    @safe
    def translate_language(self, text: str):
        """What the Into box says -> {"language": name} when it's a language, else {"suggest": [names], "message"}."""
        exact, names = find_languages(text)
        if exact:
            return {"language": names[0]}
        t = " ".join((text or "").split())
        return {"suggest": names[:3], "message": not_a_language(t, names) if t else "Choose a language to translate into."}

    @safe
    def translate_cached(self, language: str):
        """Translations already made of this document into this language (this session): {page index: text}."""
        texts, _ = self._texts()
        lang = language_name(language)
        self._translate_recall(texts, lang)
        doc, cache = self._translate_doc_key(), self._translations()
        return {"language": lang, "pages": {str(i): cache[(doc, lang, i, hash(t))] for i, t in enumerate(texts)
                                            if (doc, lang, i, hash(t)) in cache}}

    @safe
    def translate_start(self, pages: list[int], language: str, again: bool = False):
        """Translate these pages (0-based), a request each, a few at once (polled with translate_status).  again:
        send them even if they're translated already (Translate again) - the new translation replaces the kept one."""
        cfg = self._ai_cfg()
        if not cfg["enabled"]:
            raise UserError("The AI assistant is switched off. Turn it on in Settings.")
        lang = language_name(language)
        texts, name = self._texts()
        pages = sorted({int(i) for i in pages if 0 <= int(i) < len(texts)})
        if not pages:
            raise UserError("Choose a page to translate.")
        pics = self._picture_pages(texts)
        self._translate_recall(texts, lang)
        cfg, p, pre, base, model, key, conf = self._translate_target(bool(pics & set(pages)))
        if not cfg["consent"].get(p):
            cfg["consent"][p] = True
            self._ai_save(cfg)
        see = bool(pics) and ai.can_see_pictures(pre, p, model) and self._relay_pictures_ok(pre, base, key)
        doc_key, cache, kept_notes = self._translate_doc_key(), self._translations(), self._translate_notes()
        job_id = uuid.uuid4().hex[:12]
        job = {"done": False, "cancel": False, "progress": "Starting…", "problem": None, "pages": {}, "current": None,
               "order": pages, "language": lang, "started": time.time()}
        self._translate_jobs()[job_id] = job
        headers = {RELAY_JOB_HEADER: "translate"} if pre["kind"] == "relay" else None
        state = {"model": model, "waits": 0, "pause": 0.0, "error": None, "done": 0, "notes": ""}
        system = system_prompt(lang)
        who = ai.short_name(p, base)
        lock, model_lock = threading.Lock(), threading.Lock()
        read: dict[int, str] = {}                  # scans read on this computer (their words, for the pages either side)
        cancelled = lambda: job["cancel"]          # noqa: E731
        workers = at_once(p, base)

        def cache_key(i: int):
            return (doc_key, lang, i, hash(texts[i]))

        send = [i for i in pages if again or cache_key(i) not in cache]
        sending = set(send)

        def ask(system_text: str, doc: str, msg: list, on_text, images=None, max_tokens=SPEC["answer_tokens"], timeout=180):
            """One streamed request with the job's model -> (the model it went to, the result).  When the relay has
            changed its model, ask it again (once per change, whichever page finds out first)."""
            while True:
                m = state["model"]
                kind, url = ai.route(pre, base, m)
                try:
                    if kind == "anthropic":
                        r = ai.stream_claude(key, m, url, system_text, doc, msg, on_text, cancelled, relay=pre["kind"] == "relay",
                                             images=images, max_tokens=max_tokens, headers=headers, timeout=timeout)
                    else:
                        r = ai.stream_openai(url, key, m, system_text + ("\n\n" + doc if doc else ""), msg, on_text, cancelled, p,
                                             timeout=timeout, images=images, max_tokens=max_tokens, headers=headers)
                    return r
                except ai.RelayModelChanged:
                    with model_lock:
                        if state["model"] == m:              # nobody has asked the relay yet
                            self._relay_cfg(base, key, force=True)
                            m2 = self._translate_target(bool(images))[4]
                            if m2 == m:
                                raise
                            state["model"] = m2

        def make_notes() -> str:
            """One small request first: the document's key terms and tone, for every page.  Without them (a short
            document, or the request failed) the pages are translated as before."""
            sample = notes_sample([read.get(i, t) for i, t in enumerate(texts)], send)
            idx = [i for i, t in enumerate(sample) if t]
            if not idx:
                return ""
            buf: list[str] = []
            try:
                ask(notes_system(lang), ai.document_block(sample, idx, name),
                    [{"role": "user", "content": notes_prompt(name, lang)}], buf.append, max_tokens=SPEC["notes_tokens"], timeout=120)
            except ai._Cancelled:
                raise
            except Exception as e:                      # busy, refused, offline...: carry on without notes
                log.info("translate notes skipped: %s", e)
                return ""
            return clean_notes("".join(buf))

        def one(i: int) -> str:
            pic = i in pics and see
            text = texts[i]
            if i in pics and not see and not text.strip():
                job["progress"] = f"Reading page {i + 1} on this computer…"
                text = read[i] = self._ocr_page(i)
                if not text.strip():
                    return ""
            if not pic and not text.strip():
                return ""
            doc = "" if pic else ai.document_block(texts[:i] + [text] + texts[i + 1:], [i], name)
            images = [(i, self._page_jpeg(i))] if pic else None
            before, after = neighbours([read.get(j, t) for j, t in enumerate(texts)], i)
            msg = [{"role": "user", "content": page_message(i + 1, name, lang, pic, before, after, i - 1 in sending, state["notes"])}]
            entry = job["pages"][str(i)]
            buf: list[str] = []

            def on_text(s: str):
                buf.append(s)
                entry["text"] = "".join(buf)

            r = ask(system, doc, msg, on_text, images=images)
            out = "".join(buf).strip()
            if not out:
                raise UserError(ai.empty_answer_message(r.get("stop"), who, pre["kind"] == "relay", False))
            if r.get("stop") in ("max_tokens", "length"):
                out += "\n\n[The translation of this page was cut short - it is too long for one answer.]"
            return out

        queue: list[int] = []
        flying: set[int] = set()

        def say():
            """The progress line and the page to follow (the first one being translated)."""
            job["current"] = min(flying) if flying else None
            if not flying:
                return
            if len(send) == 1:
                job["progress"] = f"Translating page {send[0] + 1}…"
            else:
                job["progress"] = (f"Translating page{'s' if len(flying) > 1 else ''} {ai.page_ranges(sorted(flying))} "
                                   f"({state['done']} of {len(send)} done)…")

        def worker():
            while True:
                while time.time() < state["pause"] and not job["cancel"] and not state["error"]:
                    time.sleep(0.25)
                with lock:
                    if job["cancel"] or state["error"] or not queue:
                        return
                    i = queue.pop(0)
                    flying.add(i)
                    job["pages"][str(i)] = {"text": "", "done": False}
                    say()
                try:
                    text = one(i)
                except ai.Busy as e:
                    with lock:
                        flying.discard(i)
                        queue.insert(next((k for k, q in enumerate(queue) if q > i), len(queue)), i)   # back in page order
                        job["pages"][str(i)] = {"text": "", "done": False}
                        if time.time() >= state["pause"]:          # the first page to hear it: wait once for all
                            state["waits"] += 1
                            if state["waits"] > BUSY_TRIES:
                                state["error"] = e
                                return
                            state["pause"] = time.time() + busy_wait()
                        job["progress"] = (f"Waiting a moment - {who} takes only so many requests a minute "
                                           f"({state['done']} of {len(send)} done)…")
                    continue
                except BaseException as e:                       # stopped, refused, failed: the others stop too
                    with lock:
                        flying.discard(i)
                        state["error"] = state["error"] or e
                    return
                with lock:
                    flying.discard(i)
                    state["waits"] = 0
                    state["done"] += 1
                    job["pages"][str(i)] = {"text": text, "done": True, "empty": not text}
                    if text:
                        cache[cache_key(i)] = text
                        self._translate_keep(lang, i, texts[i], text)     # one at a time: the same file
                    say()

        def run():
            try:
                for i in pages:
                    if i not in sending:
                        job["pages"][str(i)] = {"text": cache[cache_key(i)], "done": True}
                if send:
                    state["notes"] = kept_notes.get((doc_key, lang), "")
                    if not state["notes"] and len(send) > 1:
                        job["progress"] = "Reading the document first, so every page is translated the same way…"
                        state["notes"] = make_notes()
                        if state["notes"]:
                            kept_notes[(doc_key, lang)] = state["notes"]
                    if job["cancel"]:
                        raise ai._Cancelled()
                    queue.extend(send)
                    threads = [self._thread(worker) for _ in range(min(workers, len(send)))]
                    for t in threads:
                        t.start()
                    for t in threads:
                        t.join()
                if job["cancel"]:
                    raise ai._Cancelled()
                if state["error"]:
                    raise state["error"]
                job["progress"] = ""
            except ai._Cancelled:
                job["problem"] = "Stopped."
                job["cancelled"] = True
            except UserError as e:
                job["problem"] = str(e)
            except Exception:
                log.exception("translate failed")
                job["problem"] = "The translation failed. Details were saved to the log file."
            finally:
                for v in job["pages"].values():
                    if not v.get("done") and not v.get("text"):
                        v["done"] = True
                        v["empty"] = True
                job["current"] = None
                job["done"] = True
                log.info("translate %s %s %s pages=%d sent=%d at_once=%d notes=%d problem=%s %.1fs", p, state["model"], lang,
                         len(pages), len(send), workers, len(state["notes"]), bool(job["problem"]), time.time() - job["started"])

        self._thread(run).start()                    # stays with this tab's document
        return {"job": job_id, "pages": len(pages), "provider": who, "model": model, "language": lang}

    @safe
    def translate_status(self, job_id: str):
        job = self._translate_jobs().get(job_id)
        if not job:
            raise UserError("That job is no longer available.")
        return {k: job.get(k) for k in ("done", "progress", "problem", "pages", "current", "cancelled", "language")}

    @safe
    def translate_cancel(self, job_id: str):
        job = self._translate_jobs().get(job_id)
        if job:
            job["cancel"] = True
        return {"ok": True}

    def _translate_save_path(self, language: str, suffix: str, kinds: tuple, path: str | None) -> Path | dict:
        """Where to save "<document> - <language><suffix>": `path` (tests), else a Save dialog starting next to the
        document -> a Path, or {"cancelled"} / {"error"}."""
        src = Path(self._source or (self._model.path if self._model else None) or (Path.home() / "Documents" / "Document.pdf"))
        from bridge_util import unique
        start = unique(src.with_name(translate_pdf.file_name(src.name, language_name(language))[:-4] + suffix))
        if not path:
            if not getattr(self, "_window", None):
                return {"error": "Saving needs the app window."}
            import webview
            res = self._window.create_file_dialog(webview.FileDialog.SAVE, directory=str(start.parent), save_filename=start.name,
                                                  file_types=kinds)
            if not res:
                return {"cancelled": True}
            path = res if isinstance(res, str) else res[0]
        p = Path(path)
        return p if p.suffix.lower() == suffix else p.with_suffix(suffix)

    @safe
    def translate_save(self, text: str, language: str, path: str | None = None):
        """Save the translation shown as a text file (UTF-8) next to the document: "<name> - <language>.txt"."""
        if not (text or "").strip():
            raise UserError("Nothing translated yet.")
        p = self._translate_save_path(language, ".txt", ("Text files (*.txt)",), path)
        if isinstance(p, dict):
            return p
        p.write_text(text.rstrip() + "\n", encoding="utf-8-sig")       # the BOM: Notepad shows accents and scripts right
        return {"saved": str(p), "status": f"Saved {p.name}."}

    @safe
    def translate_save_pdf(self, pages: dict, language: str, path: str | None = None):
        """Save the translation as a new tidy A4 PDF (translate_pdf.py) next to the document: "<name> - <language>.pdf".
        pages: {page index: its translation} as the panel shows them."""
        done = {int(k): str(v) for k, v in (pages or {}).items() if str(v or "").strip()}
        if not done:
            raise UserError("Nothing translated yet.")
        texts, name = self._texts()
        lang = language_name(language)
        p = self._translate_save_path(lang, ".pdf", ("PDF files (*.pdf)",), path)
        if isinstance(p, dict):
            return p
        pictures = {}
        with self._lock:
            m = self._need_doc()
            for i in done:
                if 0 <= i < len(m):
                    try:
                        pictures[i] = translate_pdf.page_pictures(m.page(i), i)
                    except Exception:                 # a picture that won't draw isn't worth losing the PDF over
                        log.exception("translate pdf: pictures of page %d", i + 1)
        try:
            translate_pdf.make_pdf(p, name, lang, done, len(texts), pictures=pictures)
        except OSError as e:
            raise UserError(f"Couldn't save {p.name} - {e.strerror or e}.")
        return {"saved": str(p), "status": f"Saved {p.name}."}
