"""Writes prompts.json "translate"."other_languages": every other language Translate accepts when it is typed (desktop)
or picked under "Other language…" (iPad/iPhone) - Apple's ISO 639-1 list (build/languages.swift, run with `swift`),
less the ones already in "languages" (whose English and own names become their aliases there), less dead and made-up
ones.  Each: name (English), own (the language's own name), code, script, sample (a few letters, for the PDF font
checks in the self-tests).  Run on a Mac from the repo root:  .venv/bin/python3 build/make_languages.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP = {"ae", "cu", "pi", "vo", "ie", "io", "ia", "zh"}       # dead / constructed; Chinese is two of "languages"
# extra names people use for a language in "languages"
ALIASES = {"fa": ["Persian"], "zh-Hans": ["Chinese", "Mandarin", "Simplified Chinese"], "zh-Hant": ["Chinese", "Traditional Chinese"],
           "bn": ["Bangla"], "pa": ["Panjabi"], "el": ["Greek"], "cy": ["Cymraeg"], "so": ["Soomaali"]}
OWN = {"zh-Hans": "简体中文", "zh-Hant": "繁體中文"}
NAMES = {"ckb": "Kurdish (Sorani)"}             # clearer than Apple's "Kurdish, Sorani"
# usual names for languages in "other_languages"
OTHER_ALIASES = {"az": ["Azeri"], "si": ["Sinhalese"], "fil": ["Tagalog"], "ckb": ["Sorani"],
                 "ku": ["Kurmanji"], "ht": ["Creole", "Kreyol"], "ky": ["Kirghiz"], "my": ["Myanmar"],
                 "ps": ["Pushto"], "gd": ["Scots Gaelic", "Gaelic"], "ga": ["Gaelic (Irish)"]}


def main():
    out = Path(tempfile.mkdtemp()) / "all.json"
    subprocess.run(["swift", str(ROOT / "build" / "languages.swift"), str(out)], check=True, stdout=subprocess.DEVNULL)
    apple = {x["code"]: x for x in json.loads(out.read_text(encoding="utf-8"))}
    p = ROOT / "prompts.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    spec = data["translate"]
    for lang in spec["languages"]:
        base = lang["code"].split("-")[0]
        a = apple.get(base, {})
        lang["own"] = OWN.get(lang["code"]) or a.get("own") or lang["name"]
        names = [*ALIASES.get(lang["code"], []), *ALIASES.get(base, []), a.get("name", "")]
        lang["aliases"] = [n for n in dict.fromkeys(names) if n and n.lower() not in (lang["name"].lower(), lang["own"].lower())]
        if not lang["aliases"]:
            del lang["aliases"]
        lang["script"] = {"zh-Hans": "Hans", "zh-Hant": "Hant"}.get(lang["code"], a.get("script", ""))
        lang["sample"] = a.get("sample", "")
    taken = {x["code"].split("-")[0] for x in spec["languages"]}
    spec["other_languages"] = sorted(
        ({"name": NAMES.get(c, x["name"]), "own": x["own"], "code": c, "script": x["script"], "sample": x["sample"],
          **({"aliases": OTHER_ALIASES[c]} if c in OTHER_ALIASES else {})}
         for c, x in apple.items() if c not in taken and c not in SKIP),
        key=lambda x: x["name"])
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{len(spec['languages'])} languages, {len(spec['other_languages'])} others")


if __name__ == "__main__":
    sys.exit(main())
