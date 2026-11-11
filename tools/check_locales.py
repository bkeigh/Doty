"""Check the translation catalogs in doty/locales against en.json.

    .venv\\Scripts\\python tools\\check_locales.py [code ...]

For each <code>.json: it's a JSON object, every English text in en.json is a key, every value is a non-empty
string, the {placeholders} match, HTML tags match, and capitals stay capitals. Exit 1 on any problem.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

LOCALES = Path(__file__).resolve().parent.parent / "doty" / "locales"
FIELD = re.compile(r"\{[a-z_]+\}")
TAG = re.compile(r"</?[a-zA-Z][^>]*>")


def check(code: str, english: list[str]) -> list[str]:
    problems = []
    path = LOCALES / f"{code}.json"
    try:
        cat = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"{code}: can't read {path.name}: {exc}"]
    if not isinstance(cat, dict):
        return [f"{code}: not a JSON object"]
    missing = [e for e in english if e not in cat]
    if missing:
        problems.append(f"{code}: {len(missing)} missing, e.g. {missing[:3]}")
    for en, out in cat.items():
        if en not in english:
            continue
        if not isinstance(out, str) or not out.strip():
            problems.append(f"{code}: empty for {en!r}")
            continue
        if sorted(FIELD.findall(en)) != sorted(FIELD.findall(out)):
            problems.append(f"{code}: placeholders differ for {en!r}: {out!r}")
        if sorted(TAG.findall(en)) != sorted(TAG.findall(out)):
            problems.append(f"{code}: HTML differs for {en!r}")
        if en.isupper() and len(en) > 3 and out != out.upper():
            problems.append(f"{code}: should be in capitals: {en!r} -> {out!r}")
    return problems


def main(codes: list[str]) -> int:
    english = json.loads((LOCALES / "en.json").read_text(encoding="utf-8"))
    codes = codes or sorted(p.stem for p in LOCALES.glob("*.json") if p.stem != "en")
    problems = [p for code in codes for p in check(code, english)]
    for p in problems:
        print(p)
    print(f"{len(codes)} catalog(s), {len(english)} texts, {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
