"""Collect the English text Doty shows or says, for translation: writes doty/locales/en.json (a sorted list).

    .venv\\Scripts\\python tools\\extract_strings.py

Every string literal in the user-facing modules, except docstrings, print() and log text, stylesheets, file
paths, URLs and identifiers. f-strings aren't collected (their numbers change); text built from them stays in
English until a call site passes a template through i18n.tr().
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCES = [*sorted((ROOT / "doty" / "ui").glob("*.py")), *sorted((ROOT / "doty" / "rules").glob("*.py")),
           ROOT / "doty" / "knowledge" / "lessons.py", ROOT / "doty" / "overlay.py", ROOT / "doty" / "tray.py",
           ROOT / "doty" / "checks.py", ROOT / "doty" / "review.py", ROOT / "doty" / "hud.py",
           ROOT / "doty" / "knowledge" / "stats.py", ROOT / "doty" / "knowledge" / "profile.py",
           ROOT / "doty" / "kokoro.py", ROOT / "doty" / "kitten.py", ROOT / "doty" / "coach" / "backends.py"]
OUT = ROOT / "doty" / "locales" / "en.json"
SKIP_CALLS = {"print", "getattr", "setattr", "hasattr", "setObjectName", "setProperty", "property", "get",
              "startswith", "endswith", "removeprefix", "removesuffix", "split", "rsplit", "replace", "join",
              "glob", "Path", "open", "setStyleSheet", "QColor", "connect", "findData", "environ", "run",
              "strftime", "re", "compile", "sub", "match", "search", "fullmatch", "encode", "decode", "Signal"}


CSS = re.compile(r"(?<![\w-])[a-z-]+\s*:\s*[^;\"\s]+(?:\s+[^;\"\s]+){0,3}\s*;")  # "color: #fff;", "border: 1px solid x;"


NOT_TEXT = {  # names, protocol words and test phrases that are the same in every language
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz", "Authorization", "Content-Type", "POST", "CPU", "GPU",
    "CPUExecutionProvider", "Claude", "ChatGPT", "OpenAI", "OpenRouter", "Ollama", "Google Gemini", "Ready.",
}


def _ui_text(s: str) -> bool:
    t = s.strip()
    if len(t) < 2 or not re.search(r"[A-Za-z]{2}", t) or t in NOT_TEXT:
        return False
    if " " not in t and ("/" in t or re.search(r"[ˈˌəɛɪʊ]", t)):
        return False  # model ids ("org/model"), phonemes
    if re.fullmatch(r"[a-z0-9_.\-/]+", t) or re.fullmatch(r"[A-Z0-9_]+", t) and "_" in t:
        return False  # identifiers, keys, file names
    if t.startswith(("http", "item_", "npc_", "special_bonus", "#", "rgba(", "data:", "Q", "font-", "color:")):
        return t.startswith("Q") and " " in t and not t.startswith(("QPushButton", "QLabel", "QFrame", "QWidget",
                                                                      "QScroll", "QLine", "QCombo", "QSlider"))
    if CSS.search(t) or re.search(r"[\w#]\s*\{[^}]*:", t) or re.search(r"style\s*=\s*[\"']", t):
        return False  # stylesheets and styled HTML fragments (prose with ": ...;" and "{name}" templates are kept)
    if ("\\" in t and t.count(" ") < 4) or t.endswith((".py", ".png", ".json", ".toml", ".ico", ".svg", ".jsonl", ".txt", ".exe")):
        return False
    if re.fullmatch(r"[a-z_]+=?", t):
        return False
    return True


def _docstrings(tree: ast.AST) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


def extract() -> list[str]:
    found: set[str] = set()
    for path in SOURCES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docs = _docstrings(tree)
        skip: set[int] = set(docs)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = fn.attr if isinstance(fn, ast.Attribute) else fn.id if isinstance(fn, ast.Name) else ""
                if name in SKIP_CALLS:
                    for sub in ast.walk(node):
                        skip.add(id(sub))
            if isinstance(node, ast.JoinedStr):  # f-string parts are fragments, not phrases
                for sub in ast.walk(node):
                    skip.add(id(sub))
            if isinstance(node, ast.Subscript):  # dict["key"] lookups
                for sub in ast.walk(node.slice):
                    skip.add(id(sub))
            if isinstance(node, ast.Compare):  # comparisons against keys ("live", "draft")
                for sub in ast.walk(node):
                    skip.add(id(sub))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
                if _ui_text(node.value):
                    found.add(node.value.strip())
    return sorted(found)


if __name__ == "__main__":
    strings = extract()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(strings, ensure_ascii=False, indent=0), encoding="utf-8")
    print(f"{len(strings)} strings -> {OUT}")
