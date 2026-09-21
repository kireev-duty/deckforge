"""Текст-заглушка шаблонов («Вставить фото», «Образец текста», lorem ipsum) — общий словарь аудита и рендера."""

from __future__ import annotations

import re

PLACEHOLDER_PATTERNS = [re.compile(p, re.IGNORECASE) for p in (
    r"lorem\s+ipsum", r"\bxxx+\b", r"\btodo\b", r"\btbd\b", r"\{\{", r"\[\s*(текст|text|\.\.\.)\s*\]",
    r"вставьте\s+(текст|заголовок|фото)", r"вставить\s+(текст|заголовок|фото)", r"insert\s+(text|photo|title)",
    r"click\s+to\s+add", r"нажмите,?\s+чтобы", r"текст\s+слайда", r"заголовок\s+слайда", r"образец\s+текста",
)]
PLACEHOLDER_WHOLE = {"заголовок", "текст", "подзаголовок", "название", "описание", "title", "text", "subtitle", "heading",
                     "body", "caption", "подпись"}


def is_placeholder_text(text: str) -> bool:
    t = " ".join(text.split()).strip().lower()
    return bool(t) and (t in PLACEHOLDER_WHOLE or any(p.search(t) for p in PLACEHOLDER_PATTERNS))


__all__ = ["PLACEHOLDER_PATTERNS", "PLACEHOLDER_WHOLE", "is_placeholder_text"]
