"""Текст-заглушка шаблонов («Вставить фото», «Образец текста», lorem ipsum) — общий словарь аудита и рендера."""

from __future__ import annotations

import re

PLACEHOLDER_PATTERNS = [re.compile(p, re.IGNORECASE) for p in (
    r"lorem\s+ipsum", r"\bxxx+\b", r"\btodo\b", r"\btbd\b", r"\{\{", r"\[\s*(текст|text|\.\.\.)\s*\]",
    r"вставьте\s+(текст|заголовок|фото)", r"вставить\s+(текст|заголовок|фото)", r"insert\s+(text|photo|title)",
    r"click\s+to\s+add", r"нажмите,?\s+чтобы", r"текст\s+слайда", r"заголовок\s+слайда", r"образец\s+текста",
    # обращения шаблона к автору презентации: на слайдах-инструкциях чужих шаблонов («шаблон-макет МТУСИ»)
    r"не\s+забудьте\s+удалить", r"удалите\s+(этот|эту|данн\w+)\s+(слайд|страниц\w+)",
    r"(этот|данный)\s+слайд\s+(можно|нужно|следует|необходимо)\s+удалить",
    r"(delete|remove)\s+(this\s+)?slide",
)]
PLACEHOLDER_WHOLE = {"заголовок", "текст", "подзаголовок", "название", "описание", "title", "text", "subtitle", "heading",
                     "body", "caption", "подпись"}


# подсказка «сюда фото»: без картинки рамка под ней — пустой квадрат (фото докладчика на титуле VK Tech);
# \s* — строки абзацев склеиваются без пробела («Вставить\nфото» → «Вставитьфото»)
PHOTO_PROMPT = re.compile(r"(вставьте|вставить|добавьте|добавить)\s*(фото|изображение|картинку)|место\s*для\s*фото|"
                          r"insert\s*(photo|image|picture)", re.IGNORECASE)


def is_photo_prompt(text: str) -> bool:
    return bool(PHOTO_PROMPT.search(" ".join(text.split())))


def is_placeholder_text(text: str) -> bool:
    t = " ".join(text.split()).strip().lower()
    return bool(t) and (t in PLACEHOLDER_WHOLE or any(p.search(t) for p in PLACEHOLDER_PATTERNS))


__all__ = ["PHOTO_PROMPT", "PLACEHOLDER_PATTERNS", "PLACEHOLDER_WHOLE", "is_photo_prompt", "is_placeholder_text"]
