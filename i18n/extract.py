"""Собирает из кода все строки, которые видит пользователь, в i18n/source.json.

Запуск: python3 i18n/extract.py. Переводы лежат рядом: en.json и uk.json —
{"русский шаблон": "перевод"}. В шаблонах {0}, {1}… — подставляемые значения.
Переводить построчно: строка шаблона = строка сообщения.
"""
import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY_FILES = ["bot.py", "business_triggers.py", "room_games.py", "core/events.py", "core/business.py",
            "core/casino.py", "quests.json"]
UK_ONLY = re.compile(r"[ЇїІіЄєҐґ]")       # уже украинский вариант — переводить нечего
JS_FILES = ["webapp/app.js", "webapp/index.html"]
CYR = re.compile(r"[А-Яа-яЁёЇїІіЄєҐґ]")
# Логи, ошибки и служебное — не для пользователя
SKIP_CALLS = {"info", "warning", "error", "exception", "debug", "log_exception", "print",
              "log_admin_action", "LOGGER"}
SKIP_FUNC = re.compile(r"admin|_log|backup|migrat", re.I)


def _split(template):
    for line in template.split("\n"):
        line = line.strip()
        if CYR.search(line) and not UK_ONLY.search(line):
            n = iter(range(100))
            # в каждой строке шаблона подстановки нумеруются заново: {0}, {1}…
            yield re.sub(r"(?<!\{)\{\d+\}(?!\})", lambda _: "{%d}" % next(n), line)


def _joined(node):
    parts, n = [], 0
    for value in node.values:
        if isinstance(value, ast.Constant):
            parts.append(str(value.value).replace("{", "{{").replace("}", "}}"))
        else:
            parts.append("{%d}" % n)
            n += 1
    return "".join(parts)


class Collector(ast.NodeVisitor):
    def __init__(self):
        self.found = []
        self.func = []
        self.skip = 0

    def visit_FunctionDef(self, node):
        self.func.append(node.name)
        body = node.body
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            body = body[1:]                                  # докстринг
        skip = bool(SKIP_FUNC.search(node.name))
        self.skip += skip
        for child in body:
            self.visit(child)
        for d in node.decorator_list:
            self.visit(d)
        self.skip -= skip
        self.func.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node):
        name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        if name in SKIP_CALLS or (isinstance(node.func, ast.Attribute)
                                  and getattr(node.func.value, "id", "") == "LOGGER"):
            return
        self.generic_visit(node)

    def visit_Raise(self, node):
        return

    def _word_list(self, node):
        # Словари игр («виселица», «слова») переводить нельзя — сломается игра
        words = [e for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
        return len(words) >= 15 and all(" " not in w.value and w.value.islower() for w in words)

    def visit_List(self, node):
        if not self._word_list(node):
            self.generic_visit(node)

    visit_Tuple = visit_Set = visit_List

    def visit_Expr(self, node):
        if isinstance(node.value, ast.Constant):
            return                                           # строка-комментарий
        self.generic_visit(node)

    def visit_JoinedStr(self, node):
        if not self.skip:
            text = _joined(node)
            if CYR.search(text):
                self.found.append(text)

    def visit_Constant(self, node):
        if not self.skip and isinstance(node.value, str) and CYR.search(node.value):
            self.found.append(node.value.replace("{", "{{").replace("}", "}}"))


def collect():
    templates = {}
    for rel in PY_FILES:
        path = ROOT / rel
        if not path.exists():
            continue
        if path.suffix == ".json":
            def walk(obj):
                if isinstance(obj, str) and CYR.search(obj):
                    yield obj.replace("{", "{{").replace("}", "}}")
                elif isinstance(obj, dict):
                    for v in obj.values():
                        yield from walk(v)
                elif isinstance(obj, list):
                    for v in obj:
                        yield from walk(v)
            texts = list(walk(json.loads(path.read_text(encoding="utf-8"))))
        else:
            c = Collector()
            c.visit(ast.parse(path.read_text(encoding="utf-8")))
            texts = c.found
        for text in texts:
            for line in _split(text):
                templates.setdefault(line, rel)
    js_lit = re.compile(r"'((?:[^'\\\n]|\\.)*)'|\"((?:[^\"\\\n]|\\.)*)\"|`((?:[^`\\]|\\.)*)`")
    for rel in JS_FILES:
        src = (ROOT / rel).read_text(encoding="utf-8")
        if rel.endswith(".html"):
            src = re.sub(r"<script.*?</script>", "", src, flags=re.S)
            for text in re.findall(r">([^<>]+)<", src) + re.findall(r'(?:placeholder|title|aria-label)="([^"]+)"', src):
                for line in _split(text):
                    templates.setdefault(line.replace("{", "{{").replace("}", "}}"), rel)
            continue
        for m in js_lit.finditer(src):
            text = next(g for g in m.groups() if g is not None)
            if m.group(3) is not None:
                n = iter(range(100))
                text = re.sub(r"\$\{[^}]*\}", lambda _: "\0%d\0" % next(n), text)
            text = text.replace("{", "{{").replace("}", "}}")
            text = re.sub(r"\0(\d+)\0", r"{\1}", text)
            text = text.replace("\\n", "\n").replace("\\'", "'").replace('\\"', '"')
            for line in _split(text):
                templates.setdefault(line, rel)
    return templates


if __name__ == "__main__":
    found = collect()
    out = ROOT / "i18n" / "source.json"
    out.write_text(json.dumps(found, ensure_ascii=False, indent=0, sort_keys=True), encoding="utf-8")
    by = {}
    for rel in found.values():
        by[rel] = by.get(rel, 0) + 1
    print(len(found), "строк", by, file=sys.stderr)
