"""Перевод исходящих текстов по каталогу.

Тексты бота написаны по-русски прямо в коде. Вместо того чтобы переписывать
каждое сообщение, бот переводит то, что уходит пользователю: каждую строку
сообщения ищет в каталоге i18n/<язык>.json. Каталог собирает
i18n/extract.py из исходников: ключ — русская строка, где подставляемые
значения заменены на {0}, {1}…; значение — перевод с теми же {0}, {1}.

Строка без перевода остаётся русской — бот от этого не ломается.
"""
import json
import re
from pathlib import Path

LANGS = ("en", "uk")
CYRILLIC = re.compile(r"[А-Яа-яЁё]")
_PLACEHOLDER = re.compile(r"(?<!\{)\{(\d+)\}(?!\})")
_WRAP = re.compile(r"^((?:<[^>]+>|[\s•·—–\-]|[^\w<\s]{1,3}\s)*)(.*?)((?:\s*</?[^>]+>)*)$", re.S)


def _unescape(text):
    return text.replace("{{", "{").replace("}}", "}")


class Catalog:
    def __init__(self, directory):
        self._exact = {}
        self._templates = {}
        for lang in LANGS:
            path = Path(directory) / f"{lang}.json"
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            self._load(lang, data)

    def _load(self, lang, data):
        exact, buckets = {}, {}
        for source, target in data.items():
            if not target or not isinstance(target, str):
                continue
            if not _PLACEHOLDER.search(source):
                exact[_unescape(source)] = _unescape(target)
                continue
            pieces = _PLACEHOLDER.split(source)
            literal = [_unescape(p) for p in pieces[0::2]]
            order = [int(i) for i in pieces[1::2]]
            pattern = "".join(re.escape(lit) + ("(.+?)" if k < len(order) else "")
                              for k, lit in enumerate(literal))
            prefix = literal[0]
            entry = (re.compile(pattern + r"\Z", re.S), order, target, len("".join(literal)))
            buckets.setdefault(prefix[:3] if len(prefix) >= 3 else "", []).append(entry)
        for entries in buckets.values():
            entries.sort(key=lambda e: -e[3])          # длинные шаблоны точнее — их первыми
        self._exact[lang] = exact
        self._templates[lang] = buckets

    def size(self, lang):
        return len(self._exact.get(lang, {})) + sum(len(v) for v in self._templates.get(lang, {}).values())

    def all(self, lang):
        """Весь каталог языка — его переводит у себя Mini App."""
        return self._raw(lang)

    def _raw(self, lang):
        return getattr(self, "_raw_" + lang, {})

    # ------------------------------------------------------------------ перевод

    def line(self, text, lang, depth=0):
        if not text or not CYRILLIC.search(text):
            return text
        exact = self._exact.get(lang)
        if exact is None:
            return text
        found = exact.get(text)
        if found is not None:
            return found
        buckets = self._templates[lang]
        for entry in buckets.get(text[:3], []) + buckets.get("", []):
            match = entry[0].match(text)
            if not match:
                continue
            values = list(match.groups())
            if depth < 2:
                values = [self.line(v, lang, depth + 1) for v in values]
            args = [None] * (max(entry[1]) + 1)
            for value, index in zip(values, entry[1]):
                args[index] = value
            try:
                return entry[2].format(*args)
            except (IndexError, KeyError, ValueError):
                return text
        if depth == 0:
            # «Подпись: значение», склеенные в коде через +
            head, sep, tail = text.partition(": ")
            if sep and exact.get(head + ":") is not None:
                return exact[head + ":"] + " " + self.line(tail, lang, depth + 1)
            # «<b>Заголовок</b>», «• пункт» — переводим то, что внутри обёртки
            m = _WRAP.match(text)
            if m and m.group(2) and m.group(2) != text:
                inner = self.line(m.group(2), lang, depth + 1)
                if inner != m.group(2):
                    return m.group(1) + inner + m.group(3)
        return text

    def text(self, text, lang):
        """Переводит многострочный текст построчно; отступы сохраняются."""
        if not text or lang not in self._exact or not isinstance(text, str) or not CYRILLIC.search(text):
            return text
        out = []
        for raw in text.split("\n"):
            stripped = raw.strip()
            if not stripped:
                out.append(raw)
                continue
            start = raw.index(stripped[0])
            out.append(raw[:start] + self.line(stripped, lang) + raw[start + len(stripped):])
        return "\n".join(out)


def load(directory):
    catalog = Catalog(directory)
    for lang in LANGS:
        path = Path(directory) / f"{lang}.json"
        setattr(catalog, "_raw_" + lang, json.loads(path.read_text(encoding="utf-8")) if path.exists() else {})
    return catalog
