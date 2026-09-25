"""Контент-пакет: папка с брифом и данными → фрагменты с идентификаторами для `OutlineSlide.sources`.

    brief.md          → `brief` + секции `brief:<slug>`
    *.md, *.txt       → `doc:<stem>` + `doc:<stem>:<slug>` по заголовкам `#`/`**жирным**`
    *.docx            → то же по стилям Heading; таблицы документа — `doc:<stem>:table-<n>`
    *.pdf             → `doc:<stem>` + `doc:<stem>:p<n>` по страницам
    data/*.json       → каждый top-level ключ — фрагмент (series / table / quote / metric / record по форме)
    data/*.csv        → `csv:<stem>`
    data/*.xlsx       → `xlsx:<stem>` (один лист) или `xlsx:<stem>:<slug(лист)>`
Формат пакета ТЗ не задаёт — принимаем любой текст и таблицы; нечитаемый файл пропускается с предупреждением.
"""

from __future__ import annotations

import csv
import json
import logging
import re
import zipfile
from pathlib import Path
from typing import Any, Literal

from lxml import etree
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

FragmentKind = Literal["text", "metric", "series", "table", "quote", "record"]

PROMPT_CHAR_LIMIT = 12_000  # больше модель начинает терять факты
CSV_MAX_ROWS = 30
PDF_MAX_CHARS = 20_000
TEXT_EXT = (".md", ".txt", ".docx", ".pdf")
DATA_EXT = (".json", ".csv", ".xlsx")
_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

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
    warnings: list[str] = Field(default_factory=list)  # файлы, которые не удалось прочитать

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
        warnings: list[str] = list(self.warnings)
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
    warnings: list[str] = []
    readers = {".md": _md_fragments, ".txt": _md_fragments, ".docx": _docx_fragments, ".pdf": _pdf_fragments,
               ".json": _json_fragments, ".csv": _csv_fragments, ".xlsx": _xlsx_fragments}
    # outline.json рядом с брифом — результат, не источник: данные только из data/
    files = [p for p in sorted(root.iterdir()) if p.suffix.lower() in TEXT_EXT]
    if (root / "data").is_dir():
        files += [p for p in sorted((root / "data").iterdir()) if p.suffix.lower() in DATA_EXT]
    for path in files:
        try:
            frags.extend(readers[path.suffix.lower()](path, root))
        except Exception as e:  # noqa: BLE001 — один битый файл не должен ронять пакет
            warnings.append(f"{_rel(path, root)}: не прочитан ({type(e).__name__}: {str(e)[:80]})")
            log.warning("content_pack: %s", warnings[-1])
    return ContentPack(root=str(root), fragments=_dedupe(frags), warnings=warnings)


def _rel(path: Path, root: Path) -> str:
    return str(path.relative_to(root)).replace("\\", "/")


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
    return _text_fragments(path.read_text("utf-8").strip(), path, root)


def _text_fragments(text: str, path: Path, root: Path) -> list[Fragment]:
    """Документ целиком + секции по заголовкам (кроме H1) — для md/txt и для docx, сведённого к markdown."""
    rel = _rel(path, root)
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


def _docx_fragments(path: Path, root: Path) -> list[Fragment]:
    """word/document.xml → markdown (стили Heading/Title → `#`) + таблицы документа отдельными фрагментами."""
    with zipfile.ZipFile(path) as z:
        body = etree.fromstring(z.read("word/document.xml")).find(f"{{{_W}}}body")
    lines: list[str] = []
    tables: list[list[list[str]]] = []
    for el in body if body is not None else []:
        tag = etree.QName(el).localname
        if tag == "p":
            txt = "".join(el.itertext()).strip()
            if not txt:
                continue
            style = el.find(f"{{{_W}}}pPr/{{{_W}}}pStyle")
            level = _heading_level(style.get(f"{{{_W}}}val", "") if style is not None else "")
            if level and not lines:
                level = 1  # первый заголовок документа — его название, как `#` в markdown
            lines.append(f"{'#' * level} {txt}" if level else txt)
            lines.append("")
        elif tag == "tbl":
            rows = [["".join(tc.itertext()).strip() for tc in tr.iter(f"{{{_W}}}tc")] for tr in el.iter(f"{{{_W}}}tr")]
            tables.append([r for r in rows if any(r)])
    frags = _text_fragments("\n".join(lines).strip(), path, root)
    base = frags[0].id if frags else f"doc:{path.stem}"
    for n, rows in enumerate(tables, start=1):
        if rows:
            frags.append(_table_fragment(f"{base}:table-{n}", f"таблица {n}", rows, _rel(path, root)))
    return frags


def _heading_level(style_id: str) -> int:
    """`Heading1`…`Heading6`, `Title` → уровень markdown; styleId не локализуется, в отличие от имени стиля."""
    s = style_id.lower().replace(" ", "")
    if s == "title":
        return 1
    m = re.fullmatch(r"heading(\d)", s)
    return min(int(m.group(1)) + 1, 6) if m else 0


def _pdf_fragments(path: Path, root: Path) -> list[Fragment]:
    """Текст по страницам; документ целиком + при ≥2 страницах секции `:p<n>`."""
    import pymupdf  # тяжёлый импорт — только когда в пакете есть PDF

    rel = _rel(path, root)
    with pymupdf.open(path) as doc:
        pages = [p.get_text().strip() for p in doc]
    pages = [p for p in pages if p]
    text = "\n\n".join(pages)
    note = ""
    if len(text) > PDF_MAX_CHARS:
        note = f"(показаны первые {PDF_MAX_CHARS} из {len(text)} символов)"
        text = text[:PDF_MAX_CHARS]
    title = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")[:90]
    base = f"doc:{path.stem}"
    frags = [Fragment(id=base, kind="text", title=title, text=f"{note}\n{text}".strip(), source=rel)]
    if len(pages) >= 2:
        budget = PDF_MAX_CHARS
        for n, page in enumerate(pages, start=1):
            if budget <= 0:
                break
            frags.append(Fragment(id=f"{base}:p{n}", kind="text", title=f"стр. {n}", text=page[:budget], source=rel))
            budget -= len(page)
    return frags


def _xlsx_fragments(path: Path, root: Path) -> list[Fragment]:
    """Каждый непустой лист — таблица; первая строка — заголовок."""
    import openpyxl

    rel = _rel(path, root)
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    sheets: list[tuple[str, list[list[str]]]] = []
    for ws in wb.worksheets:
        rows = [[_cell(v) for v in r] for r in ws.iter_rows(values_only=True)]
        rows = [r for r in rows if any(r)]
        # ширина — по последней непустой ячейке: openpyxl отдаёт хвосты пустых колонок
        width = max((max(i for i, v in enumerate(r) if v) + 1 for r in rows), default=0)
        rows = [r[:width] + [""] * (width - len(r)) for r in rows]
        if rows:
            sheets.append((ws.title, rows))
    wb.close()
    return [_table_fragment(f"xlsx:{path.stem}" if len(sheets) == 1 else f"xlsx:{path.stem}:{slug(name)}", name, rows, rel)
            for name, rows in sheets]


def _cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def _table_fragment(frag_id: str, title: str, rows: list[list[str]], rel: str) -> Fragment:
    header, body = rows[0], rows[1:]
    text = ""
    if len(body) > CSV_MAX_ROWS:
        text = f"(показаны первые {CSV_MAX_ROWS} из {len(body)} строк)"
        body = body[:CSV_MAX_ROWS]
    return Fragment(id=frag_id, kind="table", title=title, text=text, data={"header": header, "rows": body}, source=rel)


def _json_fragments(path: Path, root: Path) -> list[Fragment]:
    rel = _rel(path, root)
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


def _csv_fragments(path: Path, root: Path) -> list[Fragment]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = [r for r in csv.reader(fh) if any(c.strip() for c in r)]
    return [_table_fragment(f"csv:{path.stem}", path.stem, rows or [[]], _rel(path, root))]


def document_text(path: Path) -> str:
    """Текст документа целиком (.md/.txt/.docx/.pdf) — тем же чтением, что у пакета; для контекста репозитория."""
    readers = {".md": _md_fragments, ".txt": _md_fragments, ".docx": _docx_fragments, ".pdf": _pdf_fragments}
    frags = readers[path.suffix.lower()](path, path.parent)
    return frags[0].text if frags else ""


__all__ = ["ContentPack", "Fragment", "FragmentKind", "document_text", "load_content_pack", "slug"]
