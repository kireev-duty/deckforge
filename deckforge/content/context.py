"""Контекст из репозитория, документации и истории → факты для outline (вводная жюри).

Вводная: «контекст на входе — репозиторий, документация и история; подготовка контекста — без лимита времени».
Поэтому это подготовка, как у шаблона, а не шаг генерации:

    collect_sources(папка | zip)  → источники: README, docs/**, прочие тексты, манифест, дерево, история git
    digest_context(источники)     → факты `fact:<n>` (скилл `context_digest`, по чанку на вызов, параллельно)
    prepare_context(…)            → то же с кэшем по sha1 источников: `context.json`

Генерация берёт готовые факты как контент-пакет (`content_source: context`): их id идут в `OutlineSlide.sources`,
судья сверяет с ними C04. Итог — не больше `PROMPT_CHAR_LIMIT`: столько outline_writer держит без потери фактов.
Задача (тема) в подготовку не входит — она известна только при генерации, а подготовка от неё не зависит.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile
import time
import tomllib
import zipfile
from collections import Counter
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from deckforge.content.content_pack import PROMPT_CHAR_LIMIT, ContentPack, Fragment, document_text
from deckforge.llm.client import LLMClient
from deckforge.llm.skills import load_skill

log = logging.getLogger(__name__)

SKILL_NAME = "context_digest"
CHUNK_CHARS = 8000  # текста источников на один вызов скилла
MAX_CHUNKS = 24  # ≈ 190 тыс. символов источников; остальное не читается (предупреждение)
MAX_FILE_CHARS = 40_000  # одного документа
MAX_FACTS_PER_CHUNK = 12
FACT_MAX_CHARS = 400
GIT_LOG_MAX = 150  # последних коммитов в истории
TREE_DEPTH = 2
ZIP_MAX_BYTES = 300 * 1024 * 1024  # несжатый объём архива
ZIP_MAX_FILES = 30_000
TEXT_EXT = (".md", ".markdown", ".rst", ".txt", ".adoc")
DOC_EXT = (*TEXT_EXT, ".docx", ".pdf")
DOC_DIRS = ("docs", "doc", "documentation")
SKIP_DIRS = {"node_modules", "venv", "env", "out", "dist", "build", "site-packages", "__pycache__", "target",
             "vendor", "coverage", "htmlcov"}
# лицензия, шаблоны GitHub и списки пакетов в .txt — не о проекте
SKIP_NAMES = re.compile(r"^(license|licence|copying|notice|code_of_conduct|security|pull_request_template"
                        r"|requirements|constraints|packages\.txt|runtime\.txt|robots\.txt|cmakelists)", re.IGNORECASE)
NEAR_DUPLICATE = 0.6  # доля общих основ слов, при которой факт — повтор уже выбранного
FACT_KINDS = ("overview", "problem", "solution", "architecture", "feature", "metric", "milestone", "decision",
              "limitation", "plan", "other")
_H1_RE = re.compile(r"^#\s+(.+?)\s*#*\s*$", re.MULTILINE)
_NOISE_LINE = re.compile(r"^\s*(<[^>]+>\s*)+$|!\[|^\s*\[!\[")  # HTML-обёртки, картинки и бейджи README


class ContextError(ValueError):
    """Источник не читается: не папка и не zip, архив слишком большой или с путями наружу."""


class Sources(BaseModel):
    """Сырые источники в порядке приоритета: README → структура и история → docs → прочее."""

    root: str
    title: str = ""
    fragments: list[Fragment] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    def sha1(self) -> str:
        h = hashlib.sha1()
        for f in self.fragments:
            h.update(f.id.encode())
            h.update(f.text.encode())
        return h.hexdigest()


class Fact(BaseModel):
    text: str
    kind: str = "other"
    sources: list[str] = Field(default_factory=list)
    weight: int = 2


class ContextDigest(BaseModel):
    """`context.json`: факты контекстом для outline и откуда они взяты."""

    root: str
    title: str = ""
    sha1: str = ""
    created: str = ""
    skill: str = ""
    model: str = ""
    sources: list[dict[str, Any]] = Field(default_factory=list)  # id, path, chars
    facts: list[Fact] = Field(default_factory=list)  # все факты, до отбора по лимиту
    pack: ContentPack  # отобранные факты `fact:<n>` — это и идёт в outline
    calls: int = 0
    errors: int = 0
    seconds: float = 0.0
    warnings: list[str] = Field(default_factory=list)

    def default_brief(self) -> str:
        """Бриф, когда задачи нет: презентация о проекте по его материалам."""
        name = self.title or Path(self.root).name
        return (f"# {name}\n\n**Задача презентации.** Рассказать о проекте по материалам репозитория: какую "
                "проблему он решает, для кого, как устроен, что уже сделано и что дальше.\n\n"
                "_Бриф по умолчанию: задача не задана; факты — из репозитория, документации и истории._\n")


# ──────────────────────────── источники ────────────────────────────


@contextmanager
def open_sources(path: Path) -> Iterator[Path]:
    """Папка — как есть; zip — во временную папку (с одной корневой папкой, как у GitHub «Download ZIP», — она)."""
    path = Path(path)
    if path.is_dir():
        yield path
        return
    if not zipfile.is_zipfile(path):
        raise ContextError(f"контекст: нужна папка или .zip — {path.name}")
    with tempfile.TemporaryDirectory(prefix="deckforge_ctx_") as tmp:
        dest = Path(tmp)
        extract_zip(path, dest)
        entries = [p for p in dest.iterdir()]
        yield entries[0] if len(entries) == 1 and entries[0].is_dir() else dest


def extract_zip(path: Path, dest: Path) -> None:
    """Распаковка с защитой: пути только внутрь `dest`, лимиты на объём и число файлов."""
    root = dest.resolve()
    with zipfile.ZipFile(path) as z:
        infos = [i for i in z.infolist() if not i.is_dir()]
        if len(infos) > ZIP_MAX_FILES:
            raise ContextError(f"контекст: в архиве {len(infos)} файлов — больше {ZIP_MAX_FILES}")
        if sum(i.file_size for i in infos) > ZIP_MAX_BYTES:
            raise ContextError(f"контекст: архив больше {ZIP_MAX_BYTES // 2**20} МБ в распакованном виде")
        for info in infos:
            target = (root / info.filename).resolve()
            if not target.is_relative_to(root):
                raise ContextError(f"контекст: путь вне архива — {info.filename}")
            if any(part in SKIP_DIRS for part in Path(info.filename).parts):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, target.open("wb") as dst:
                dst.write(src.read())


def collect_sources(path: Path) -> Sources:
    """Папка или zip → источники с id в порядке приоритета. Без LLM, детерминированно."""
    with open_sources(Path(path)) as root:
        return _collect(root, name=Path(path).stem if Path(path).is_file() else Path(path).resolve().name)


def _collect(root: Path, *, name: str) -> Sources:
    warnings: list[str] = []
    docs = _doc_files(root)
    tiers: list[list[Fragment]] = [[], [], [], [], []]
    for path in docs:
        rel = path.relative_to(root).as_posix()
        top = rel.split("/")[0].lower()
        depth = rel.count("/")
        is_readme = path.stem.lower().startswith("readme")
        tier = (0 if is_readme and depth == 0 else 2 if top in DOC_DIRS else 3 if depth == 0 or is_readme else 4)
        try:
            text = _read_doc(path)
        except Exception as e:  # noqa: BLE001 — один нечитаемый файл не роняет подготовку
            warnings.append(f"{rel}: не прочитан ({type(e).__name__})")
            continue
        if len(text) > MAX_FILE_CHARS:
            warnings.append(f"{rel}: взяты первые {MAX_FILE_CHARS} из {len(text)} символов")
            text = text[:MAX_FILE_CHARS]
        if text.strip():
            tiers[tier].append(Fragment(id=f"doc:{rel.rsplit('.', 1)[0]}", kind="text", text=text, source=rel))
    manifest = _manifest(root)
    tiers[1] += [f for f in (manifest, _tree(root), _history(root, warnings)) if f is not None]
    frags = [f for tier in tiers for f in tier]
    if not docs:
        warnings.append("в источниках нет документов (.md, .txt, .rst, .docx, .pdf) — только структура и история")
    readme = next((f for f in tiers[0]), None)
    return Sources(root=name, title=_title(readme, manifest) or name, fragments=frags, warnings=warnings)


def _walk(root: Path) -> list[Path]:
    """Файлы проекта без скрытых папок (.git, .venv), сборок и зависимостей — такие папки не обходятся вовсе."""
    out: list[Path] = []
    for d, dirs, names in os.walk(root):
        dirs[:] = sorted(x for x in dirs if not (x.startswith(".") or x in SKIP_DIRS or x.endswith(".egg-info")))
        out += [Path(d) / n for n in sorted(names) if not n.startswith(".")]
    return out


def _doc_files(root: Path) -> list[Path]:
    return [p for p in _walk(root) if p.suffix.lower() in DOC_EXT and not SKIP_NAMES.match(p.name)]


def _read_doc(path: Path) -> str:
    if path.suffix.lower() in (".docx", ".pdf"):
        return document_text(path)
    text = path.read_text("utf-8", errors="replace")
    if path.suffix.lower() in (".md", ".markdown"):
        # бейджи, картинки и HTML-обёртки README — не факты, а символы
        text = "\n".join(ln for ln in text.splitlines() if not _NOISE_LINE.search(ln))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _title(readme: Fragment | None, manifest: Fragment | None) -> str:
    if readme is not None:
        m = _H1_RE.search(readme.text)
        if m:
            return re.sub(r"[*_`]", "", m.group(1)).strip()
    if manifest is not None:
        m = re.search(r"^название: (.+)$", manifest.text, re.MULTILINE)
        if m:
            return m.group(1).strip()
    return ""


def _manifest(root: Path) -> Fragment | None:
    """pyproject.toml / package.json: название, версия, описание, зависимости — без чтения кода."""
    lines: list[str] = []
    source = ""
    py = root / "pyproject.toml"
    js = root / "package.json"
    try:
        if py.is_file():
            proj = tomllib.loads(py.read_text("utf-8")).get("project", {})
            deps = [re.split(r"[<>=!~;\[ ]", d, maxsplit=1)[0] for d in proj.get("dependencies", [])]
            lines += [f"название: {proj.get('name', '')}", f"версия: {proj.get('version', '')}",
                      f"описание: {proj.get('description', '')}", f"Python: {proj.get('requires-python', '')}",
                      f"зависимости: {', '.join(deps)}"]
            source = "pyproject.toml"
        elif js.is_file():
            pkg = json.loads(js.read_text("utf-8"))
            lines += [f"название: {pkg.get('name', '')}", f"версия: {pkg.get('version', '')}",
                      f"описание: {pkg.get('description', '')}",
                      f"зависимости: {', '.join(pkg.get('dependencies', {}))}"]
            source = "package.json"
    except (ValueError, OSError) as e:
        log.warning("context: манифест не прочитан: %s", e)
    lic = next((p for p in root.iterdir() if p.is_file() and SKIP_NAMES.match(p.name)
                and p.name.lower().startswith(("license", "licence", "copying"))), None)
    if lic is not None:
        first = next((ln.strip() for ln in lic.read_text("utf-8", errors="replace").splitlines() if ln.strip()), "")
        lines.append(f"лицензия: {first[:80]}")
        source = source or lic.name
    lines = [ln for ln in lines if not ln.endswith(": ")]
    return Fragment(id="repo:manifest", kind="text", title="манифест проекта", text="\n".join(lines),
                    source=source) if lines else None


def _tree(root: Path) -> Fragment | None:
    """Дерево до `TREE_DEPTH` с числом файлов и сводкой по расширениям — из чего состоит проект."""
    files = _walk(root)
    if not files:
        return None
    exts = Counter(p.suffix.lower() or p.name for p in files)
    per_dir: Counter[str] = Counter()
    for p in files:
        parts = p.relative_to(root).parts[:-1]
        for d in range(1, min(len(parts), TREE_DEPTH) + 1):
            per_dir["/".join(parts[:d])] += 1
    lines = [f"файлов: {len(files)}; по типам: " + ", ".join(f"{e} {n}" for e, n in exts.most_common(8))]
    for d in sorted(per_dir):
        lines.append(f"{'  ' * d.count('/')}{d.rsplit('/', 1)[-1]}/ — {per_dir[d]}")
    return Fragment(id="repo:tree", kind="text", title="структура репозитория", text="\n".join(lines[:80]),
                    source="")


def _git(root: Path, *args: str) -> str:
    res = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, encoding="utf-8",
                         errors="replace", timeout=30, check=True)
    return res.stdout.strip()


def _history(root: Path, warnings: list[str]) -> Fragment | None:
    """История git: число коммитов, период, теги и последние сообщения коммитов (без авторов)."""
    if not (root / ".git").exists():
        warnings.append("истории git нет (.git не найден) — только документы и структура")
        return None
    try:
        total = _git(root, "rev-list", "--count", "--no-merges", "HEAD")
        first = _git(root, "log", "--max-parents=0", "--format=%ad", "--date=short").splitlines()
        tags = _git(root, "tag", "--sort=creatordate", "--format=%(creatordate:short) %(refname:short)")
        log_ = _git(root, "log", "--no-merges", "--date=short", "--format=%ad %s", f"-n{GIT_LOG_MAX}")
    except (OSError, subprocess.SubprocessError) as e:
        warnings.append(f"история git не прочитана ({type(e).__name__}) — только документы и структура")
        return None
    last = log_.splitlines()[0].split(" ", 1)[0] if log_ else ""
    head = f"коммитов: {total} (без слияний), с {first[-1] if first else '?'} по {last or '?'}"
    text = "\n".join(filter(None, [head, "теги: " + "; ".join(tags.splitlines()) if tags else "",
                                   f"последние коммиты (новые сверху, до {GIT_LOG_MAX}):", log_]))
    return Fragment(id="history:git", kind="text", title="история git", text=text, source=".git")


# ──────────────────────────── факты ────────────────────────────


def chunk_sources(sources: Sources, size: int = CHUNK_CHARS, max_chunks: int = MAX_CHUNKS
                  ) -> tuple[list[tuple[str, list[str]]], list[str]]:
    """Источники → чанки «текст, id внутри» в порядке приоритета; длинный документ режется по абзацам."""
    pieces: list[tuple[str, str]] = []  # (id, текст куска с заголовком)
    for f in sources.fragments:
        parts = _split(f.text, size - 200)
        for k, part in enumerate(parts, start=1):
            head = f"[{f.id}]" + (f" (часть {k}/{len(parts)})" if len(parts) > 1 else "") + \
                (f" — {f.source}" if f.source else "")
            pieces.append((f.id, f"{head}\n{part}"))
    chunks: list[tuple[str, list[str]]] = []
    buf: list[str] = []
    ids: list[str] = []
    for frag_id, text in pieces:
        if buf and sum(len(b) + 2 for b in buf) + len(text) > size:
            chunks.append(("\n\n".join(buf), ids))
            buf, ids = [], []
        buf.append(text)
        if frag_id not in ids:
            ids.append(frag_id)
    if buf:
        chunks.append(("\n\n".join(buf), ids))
    dropped: list[str] = []
    if len(chunks) > max_chunks:
        kept = {i for _, chunk_ids in chunks[:max_chunks] for i in chunk_ids}
        dropped = [i for _, chunk_ids in chunks[max_chunks:] for i in chunk_ids if i not in kept]
        chunks = chunks[:max_chunks]
    return chunks, list(dict.fromkeys(dropped))


def _split(text: str, size: int) -> list[str]:
    if len(text) <= size:
        return [text]
    out: list[str] = []
    buf = ""
    for para in re.split(r"\n\s*\n", text):
        while len(para) > size:  # абзац длиннее куска — режем по строкам (лог коммитов, таблицы)
            cut = para.rfind("\n", 0, size)
            cut = cut if cut > size // 2 else size
            para_head, para = para[:cut], para[cut:].lstrip("\n")
            if buf:
                out.append(buf)
                buf = ""
            out.append(para_head)
        if buf and len(buf) + len(para) + 2 > size:
            out.append(buf)
            buf = ""
        buf = f"{buf}\n\n{para}" if buf else para
    if buf:
        out.append(buf)
    return out


def repair_digest(raw: Any, allowed_ids: list[str], max_facts: int = MAX_FACTS_PER_CHUNK) -> list[Fact]:
    """Мягкое приведение ответа к контракту (как `repair_outline`): строки вместо объектов, `sources` строкой
    или с чужими id, `kind` не из списка, `weight` вне 1–3. Источник не назван — это чанк, из которого факт взят."""
    items = raw.get("facts", []) if isinstance(raw, dict) else raw if isinstance(raw, list) else []
    facts: list[Fact] = []
    seen: set[str] = set()
    for item in items if isinstance(items, list) else []:
        if isinstance(item, str):
            item = {"text": item}
        if not isinstance(item, dict):
            continue
        text = " ".join(str(item.get("text") or item.get("fact") or "").split())
        key = text.lower().rstrip(".")
        if len(text) < 12 or key in seen:
            continue
        seen.add(key)
        src = item.get("sources") or item.get("source") or []
        src = [src] if isinstance(src, str) else src if isinstance(src, list) else []
        src = [s.strip("[] ") for s in map(str, src) if s.strip("[] ") in allowed_ids] or allowed_ids[:1]
        kind = str(item.get("kind") or "other").strip().lower()
        try:
            weight = min(3, max(1, int(item.get("weight", 2))))
        except (TypeError, ValueError):
            weight = 2
        if len(text) > FACT_MAX_CHARS:
            text = text[:FACT_MAX_CHARS].rsplit(" ", 1)[0] + "…"
        facts.append(Fact(text=text, kind=kind if kind in FACT_KINDS else "other", sources=src, weight=weight))
        if len(facts) >= max_facts:
            break
    return facts


def select_facts(facts: list[Fact], paths: dict[str, str], limit: int = PROMPT_CHAR_LIMIT) -> ContentPack:
    """Факты в лимит промпта: сначала вес 3, потом 2 и 1; порядок в пакете — исходный (приоритет источника).

    README и docs часто повторяют одно и то же («три варианта за 200 с»): факт, почти совпадающий по словам
    с уже выбранным, место в лимите не занимает."""
    frags = [_fact_fragment(i + 1, f, paths) for i, f in enumerate(facts)]
    stems = [_stems(f.text) for f in facts]
    chosen: set[int] = set()
    total = 0
    for w in (3, 2, 1):
        for i, (fact, frag) in enumerate(zip(facts, frags)):
            size = len(frag.to_prompt()) + 2
            if fact.weight != w or total + size > limit or any(_overlap(stems[i], stems[j]) for j in chosen):
                continue
            chosen.add(i)
            total += size
    kept = [frag for i, frag in enumerate(frags) if i in chosen]
    return ContentPack(root="context:", fragments=[f.model_copy(update={"id": f"fact:{n}"})
                                                    for n, f in enumerate(kept, start=1)])


def _stems(text: str) -> set[str]:
    """Основы слов: первые 5 букв слов от 4 букв и все числа — грубо, но для «повтора другими словами» хватает."""
    words = re.findall(r"\w+", text.lower())
    return {w[:5] for w in words if len(w) >= 4 or w.isdigit()}


def _overlap(a: set[str], b: set[str]) -> bool:
    return bool(a and b) and len(a & b) / min(len(a), len(b)) >= NEAR_DUPLICATE


def _fact_fragment(n: int, fact: Fact, paths: dict[str, str]) -> Fragment:
    files = list(dict.fromkeys(paths.get(s, "") or s for s in fact.sources))
    return Fragment(id=f"fact:{n}", kind="text", title=fact.kind, text=f"{fact.text} (источник: {', '.join(files)})",
                    source=", ".join(files))


def digest_context(sources: Sources, client: LLMClient, *, language: str = "ru", workers: int = 4,
                   progress: Callable[[str], None] | None = None) -> ContextDigest:
    """Источники → факты: вызов скилла на чанк, параллельно; упавший чанк — предупреждение, а не ошибка."""
    say = progress or log.info
    t0 = time.perf_counter()
    skill = load_skill(SKILL_NAME)
    chunks, dropped = chunk_sources(sources)
    warnings = list(sources.warnings)
    if dropped:
        warnings.append(f"не вошли в лимит {MAX_CHUNKS}×{CHUNK_CHARS} символов: {', '.join(dropped[:8])}"
                        + (f" и ещё {len(dropped) - 8}" if len(dropped) > 8 else ""))
    say(f"контекст: {len(sources.fragments)} источников → {len(chunks)} чанков, скилл {skill.id} ×{workers}…")

    def one(chunk: tuple[str, list[str]]) -> list[Fact] | Exception:
        text, ids = chunk
        try:
            raw = client.run_skill(skill, chunk=text, source_ids=", ".join(ids), language=language,
                                   max_facts=MAX_FACTS_PER_CHUNK)
            return repair_digest(raw, ids)
        except Exception as e:  # noqa: BLE001
            return e

    # прогресс — из этого потока (Streamlit пишет в статус только из потока скрипта), примерно по четвертям
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(one, c) for c in chunks]
        step = max(1, len(chunks) // 4)
        for done, _ in enumerate(as_completed(futures), start=1):
            if done % step == 0 or done == len(chunks):
                say(f"факты: {done}/{len(chunks)} чанков, {time.perf_counter() - t0:.0f} с")
        results = [f.result() for f in futures]
    facts: list[Fact] = []
    seen: set[str] = set()
    errors = 0
    for (_, ids), res in zip(chunks, results):
        if isinstance(res, Exception):
            errors += 1
            warnings.append(f"чанк {ids[0]}…: {type(res).__name__}: {str(res)[:80]}")
            continue
        for f in res:
            key = f.text.lower().rstrip(".")
            if key not in seen:
                seen.add(key)
                facts.append(f)
    paths = {f.id: f.source for f in sources.fragments}
    pack = select_facts(facts, paths)
    if len(pack.fragments) < len(facts):
        warnings.append(f"в лимит {PROMPT_CHAR_LIMIT} символов вошли {len(pack.fragments)} из {len(facts)} фактов "
                        "(сначала самые важные)")
    return ContextDigest(
        root=sources.root, title=sources.title, sha1=sources.sha1(), created=datetime.now(UTC).isoformat(timespec="seconds"),
        skill=skill.id, model=getattr(client, "text_model", ""),
        sources=[{"id": f.id, "path": f.source, "chars": len(f.text)} for f in sources.fragments],
        facts=facts, pack=pack, calls=len(chunks), errors=errors,
        seconds=round(time.perf_counter() - t0, 1), warnings=warnings,
    )


def prepare_context(path: Path, client: LLMClient, *, cache_dir: Path, language: str = "ru", workers: int = 4,
                    progress: Callable[[str], None] | None = None) -> tuple[ContextDigest, Path, bool]:
    """Источники → факты с кэшем по sha1 источников и версии скилла: (дайджест, путь context.json, из кэша ли)."""
    sources = collect_sources(path)
    skill = load_skill(SKILL_NAME)
    key = hashlib.sha1(f"{sources.sha1()}|{skill.id}|{language}|{getattr(client, 'text_model', '')}".encode()
                       ).hexdigest()[:12]
    out = cache_dir / f"{_slug(sources.root)}__{key}.json"
    if out.is_file():
        try:
            return load_context(out), out, True
        except ValueError:
            log.warning("context: кэш %s не читается — подготовка заново", out)
    digest = digest_context(sources, client, language=language, workers=workers, progress=progress)
    if not digest.pack.fragments:
        # пустой дайджест не кэшируется: следующая попытка (другой ключ, живой API) пройдёт заново
        return digest, out, False
    cache_dir.mkdir(parents=True, exist_ok=True)
    out.write_text(digest.model_dump_json(indent=1), "utf-8")
    return digest, out, False


def load_context(path: Path) -> ContextDigest:
    return ContextDigest.model_validate_json(Path(path).read_text("utf-8"))


def _slug(name: str) -> str:
    return re.sub(r"[^0-9A-Za-zА-Яа-яЁё_-]+", "-", name).strip("-")[:40] or "context"


__all__ = ["ContextDigest", "ContextError", "Fact", "Sources", "chunk_sources", "collect_sources", "digest_context",
           "extract_zip", "load_context", "prepare_context", "repair_digest", "select_facts"]
