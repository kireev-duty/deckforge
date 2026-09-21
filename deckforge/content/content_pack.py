"""Контент-пакет: папка с брифом и данными → фрагменты с идентификаторами для `OutlineSlide.sources`.

    brief.md          → `brief` + секции `brief:<slug>`
    *.md              → `doc:<stem>` + `doc:<stem>#<slug>`
    data/*.json       → каждый top-level ключ — фрагмент (series / table / quote / metric / record по форме)
    data/*.csv        → `csv:<stem>`
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

FragmentKind = Literal["text", "metric", "series", "table", "quote", "record"]

PROMPT_CHAR_LIMIT = 12_000  # больше модель начинает терять факты
CSV_MAX_ROWS = 30

# `## Заголовок` — целая строка; `**Жирный.** текст…` — жирное начало абзаца
_HEADING_RE = re.compile(r"^(?:#{1,6}\s+(?P<h>.+?)\s*#*\s*$|\*\*(?P<b>[^*\n]+?)\*\*)", re.MULTILINE)
_SLUG_RE = re.compile(r"[^0-9a-zа-яё]+", re.IGNORECASE)


class Fragment(BaseModel):
    id: str
    kind: FragmentKind
    title: str = ""
    text: str = ""  # для text/quote — сам текст; для данных — краткая подпись
    data: dict[str, Any] | list[Any] | None = None  # как в источнике
    source: str = ""  # относительный путь к файлу

    def to_prompt(self) -> str:
        head = f"[{self.id}] ({self.kind}{': ' + self.title if self.title else ''})"
        if self.data is None:
            return f"{head}\n{self.text.strip()}"
        body = json.dumps(self.data, ensure_ascii=False, separators=(", ", ": "))
        return f"{head}\n{body}" if not self.text else f"{head} {self.text}\n{body}"


class ContentPack(BaseModel):
    root: str
    fragments: list[Fragment] = Field(default_factory=list)

    def ids(self) -> set[str]:
        return {f.id for f in self.fragments}

    def get(self, frag_id: str) -> Fragment | None:
        return next((f for f in self.fragments if f.id == frag_id), None)

    @property
    def brief(self) -> str:
        b = self.get("brief")
        return b.text if b else ""

    def to_prompt_text(self, limit: int = PROMPT_CHAR_LIMIT) -> tuple[str, list[str]]:
        """Текст для {{content_pack}} и предупреждения об обрезке; бриф идёт отдельным входом."""
        warnings: list[str] = []
        parts: list[str] = []
        total = 0
        for f in self.fragments:
            if f.id == "brief" or f.id.startswith("brief:"):
                continue
            chunk = f.to_prompt()
            if total + len(chunk) > limit:
                warnings.append(f"контент-пакет обрезан по лимиту {limit} символов: фрагмент {f.id} и далее не переданы")
                break
            parts.append(chunk)
            total += len(chunk) + 2
        return "\n\n".join(parts), warnings


# ──────────────────────────── загрузка ────────────────────────────


def load_content_pack(root: str | Path) -> ContentPack:
    root = Path(root)
    if not root.is_dir():
        raise FileNotFoundError(f"контент-пакет не найден: {root}")
    frags: list[Fragment] = []
    for md in sorted(root.glob("*.md")):
        frags.extend(_md_fragments(md, root))
    data_dir = root / "data"
    if data_dir.is_dir():
        for js in sorted(data_dir.glob("*.json")):
            frags.extend(_json_fragments(js, root))
        for cs in sorted(data_dir.glob("*.csv")):
            frags.append(_csv_fragment(cs, root))
    # outline.json рядом — результат, не источник
    return ContentPack(root=str(root), fragments=_dedupe(frags))


def _dedupe(frags: list[Fragment]) -> list[Fragment]:
    seen: dict[str, int] = {}
    out: list[Fragment] = []
    for f in frags:
        n = seen.get(f.id, 0)
        seen[f.id] = n + 1
        if n:
            f = f.model_copy(update={"id": f"{f.id}~{n + 1}"})
        out.append(f)
    return out


def slug(text: str) -> str:
    s = _SLUG_RE.sub("-", text.strip().lower()).strip("-")
    return s[:40] or "section"


def _md_fragments(path: Path, root: Path) -> list[Fragment]:
    text = path.read_text("utf-8").strip()
    rel = str(path.relative_to(root)).replace("\\", "/")
    base = "brief" if path.stem.lower() == "brief" else f"doc:{path.stem}"
    title = ""
    m = re.match(r"^#\s+(.+)$", text, re.MULTILINE)
    if m:
        title = m.group(1).strip()
    frags = [Fragment(id=base, kind="text", title=title, text=text, source=rel)]
    # секции по markdown-заголовкам (кроме H1) и абзацам с жирным началом
    body = text[m.end():] if m else text
    sections = _split_sections(body)
    if len(sections) >= 2:
        for head, chunk in sections:
            frags.append(Fragment(id=f"{base}:{slug(head)}", kind="text", title=head, text=chunk.strip(), source=rel))
    return frags


def _split_sections(body: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    pos = 0
    head: str | None = None
    for m in _HEADING_RE.finditer(body):
        if head is not None:
            out.append((head, body[pos:m.start()]))
        head = (m.group("h") or m.group("b")).strip().rstrip(".")
        pos = m.start() if m.group("b") else m.end()  # жирный заголовок остаётся частью абзаца
    if head is not None:
        out.append((head, body[pos:]))
    return [(h, c) for h, c in out if c.strip()]


def _json_fragments(path: Path, root: Path) -> list[Fragment]:
    rel = str(path.relative_to(root)).replace("\\", "/")
    raw = json.loads(path.read_text("utf-8"))
    if isinstance(raw, list):
        return [Fragment(id=f"json:{path.stem}", kind="record", data=raw, source=rel)]
    frags = []
    for key, val in raw.items():
        if key.startswith("_"):
            continue
        frag_id = key if ":" in key else f"{path.stem}:{key}"
        if isinstance(val, dict):
            kind, title, text = _classify_record(val)
            frags.append(Fragment(id=frag_id, kind=kind, title=title, text=text, data=val, source=rel))
        elif isinstance(val, list):
            frags.append(Fragment(id=frag_id, kind="record", data=val, source=rel))
        else:
            frags.append(Fragment(id=frag_id, kind="text", text=str(val), source=rel))
    return frags


def _classify_record(val: dict) -> tuple[FragmentKind, str, str]:
    """Вид фрагмента по форме объекта."""
    title = str(val.get("title") or val.get("label") or "")
    if "categories" in val and "series" in val:
        return "series", title, ""
    if "header" in val and "rows" in val:
        return "table", title, ""
    if "text" in val and "author" in val:
        return "quote", title, ""
    if "value" in val or ("before" in val and "after" in val):
        return "metric", title, ""
    return "record", title, ""


def _csv_fragment(path: Path, root: Path) -> Fragment:
    rel = str(path.relative_to(root)).replace("\\", "/")
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = [r for r in csv.reader(fh) if any(c.strip() for c in r)]
    header, body = (rows[0], rows[1:]) if rows else ([], [])
    text = ""
    if len(body) > CSV_MAX_ROWS:
        text = f"(показаны первые {CSV_MAX_ROWS} из {len(body)} строк)"
        body = body[:CSV_MAX_ROWS]
    return Fragment(id=f"csv:{path.stem}", kind="table", title=path.stem, text=text,
                    data={"header": header, "rows": body}, source=rel)


__all__ = ["ContentPack", "Fragment", "FragmentKind", "load_content_pack", "slug"]
