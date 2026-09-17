"""HTTP API поверх `pipeline/` — точка входа без бизнес-логики.

    uvicorn deckforge.api.app:app --reload          # http://127.0.0.1:8000/docs

Цикл клиента: `GET /templates` (или `POST /templates` с .pptx) → `POST /generate` (бриф + файлы контент-пакета)
→ `GET /jobs/{id}` до `done` → `GET .../decks/{strategy}/audit` → `POST .../fix` с выбранными находками
→ `GET .../files/{name}` (.pptx / .pdf / manifest / PNG). Job'ы в памяти, файлы — в `out/api/`.
"""

# без `from __future__ import annotations`: FastAPI резолвит аннотации, а `S` — локальный alias внутри фабрики
import json
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Annotated, Callable

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from deckforge import __version__
from deckforge.api.jobs import (
    BadUpload,
    Job,
    JobStore,
    TemplateEntry,
    TemplateStore,
    check_pptx,
    safe_name,
    write_content_pack,
)
from deckforge.api.schemas import (
    AuditResponse,
    DeckInfo,
    FixRequest,
    FixResponse,
    Health,
    JobCreated,
    JobInfo,
    StrategyInfo,
    TemplateInfo,
)
from deckforge.core.autofix import fix_plan_rows
from deckforge.core.strategy import list_strategies, load_strategy
from deckforge.llm import load_dotenv
from deckforge.llm.client import LLMClient
from deckforge.pipeline import RunConfig, refine_deck, run, soffice_available
from deckforge.pipeline.config import Purpose

DECK_FILES = ("pptx", "pdf", "ir.json", "audit.json", "manifest.json")
RUN_FILES = ("outline.json", "outline.raw.json", "run.json", "compare.md", "dna.json")
_PNG = re.compile(r"^(slide_\d{2}\.png|contact\.png)$")


class State:
    def __init__(self, root: Path, client_factory: Callable[[], object], executor: ThreadPoolExecutor | None) -> None:
        self.root = root
        self.templates = TemplateStore(root)
        self.jobs = JobStore(root, executor)
        self.client_factory = client_factory


def create_app(root: Path | str = Path("out/api"), client_factory: Callable[[], object] | None = None,
               executor: ThreadPoolExecutor | None | bool = True) -> FastAPI:
    """`client_factory` — как создавать LLM-клиент (в тестах FakeClient); `executor=None` — job'ы синхронно."""
    load_dotenv()
    root = Path(root).resolve()
    if executor is True:
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="deckforge-job")
    state = State(root, client_factory or LLMClient, executor or None)
    app = FastAPI(title="deckforge", version=__version__, description=__doc__)
    app.state.df = state

    def st(request: Request) -> State:
        return request.app.state.df

    S = Annotated[State, Depends(st)]

    # ──────────────── справочное ────────────────

    @app.get("/health", response_model=Health)
    def health(s: S) -> Health:
        c = LLMClient()
        return Health(version=__version__, api_key_set=bool(c.api_key), soffice=soffice_available(),
                      models={"text": c.text_model, "vision": c.vision_model, "image": c.image_model,
                              "base_url": c.base_url}, strategies=list_strategies())

    @app.get("/strategies", response_model=list[StrategyInfo])
    def strategies() -> list[StrategyInfo]:
        out = []
        for name in list_strategies():
            x = load_strategy(name)
            out.append(StrategyInfo(
                name=x.name, version=x.version, target_slides=x.target_slides.model_dump(),
                density=x.density.model_dump(), data_visualization=x.data_visualization.model_dump(),
                sections=x.sections, images=x.images, icons=x.icons,
                archetype_priority=[a.value for a in x.archetype_priority]))
        return out

    # ──────────────── шаблоны ────────────────

    @app.get("/templates", response_model=list[TemplateInfo])
    def templates(s: S) -> list[TemplateInfo]:
        return [_template_info(e) for e in s.templates.list()]

    @app.post("/templates", response_model=TemplateInfo)
    async def upload_template(s: S, file: UploadFile = File(...)) -> TemplateInfo:
        """Загрузить .pptx и разобрать его в TemplateDNA; ответ — сводка (палитра, шрифты, архетипы)."""
        data = await file.read()
        try:
            entry = s.templates.add_upload(file.filename or "template.pptx", data)
            s.templates.parsed(entry)
        except BadUpload as e:
            raise HTTPException(400, str(e)) from e
        return _template_info(entry)

    @app.get("/templates/{template_id}", response_model=TemplateInfo)
    def template(s: S, template_id: str) -> TemplateInfo:
        entry = _template(s, template_id)
        s.templates.parsed(entry)
        return _template_info(entry)

    # ──────────────── генерация ────────────────

    @app.post("/generate", response_model=JobCreated, status_code=202)
    async def generate(
        s: S,
        template_id: str = Form(...),
        brief: str = Form(..., min_length=20, description="текст брифа (brief.md)"),
        purpose: Purpose = Form("other"),
        audience: str = Form(""),
        language: str = Form("ru"),
        target_slides: int | None = Form(None, ge=3, le=25),
        strategies: str = Form("executive,narrative,visual", description="через запятую"),
        judge: bool = Form(True, description="VLM-судья по PNG"),
        autofix: bool = Form(True, description="безопасные автофиксы"),
        render_png: bool = Form(True),
        export: str = Form("pptx,pdf", description="через запятую: pptx, pdf"),
        files: list[UploadFile] = File(default=[], description="контент-пакет: *.md, *.txt, data/*.json, data/*.csv"),
    ) -> JobCreated:
        """Запустить прогон: бриф + файлы → outline (LLM) → колоды по стратегиям → аудит → PDF. Ответ — id job'а."""
        entry = _template(s, template_id)
        job = s.jobs.new(entry)
        try:
            pack_dir = write_content_pack(job.dir / "content_pack", brief,
                                          [(f.filename or "file", await f.read()) for f in files])
        except BadUpload as e:
            raise HTTPException(400, str(e)) from e
        names = [x.strip() for x in strategies.split(",") if x.strip()]
        unknown = [n for n in names if n not in list_strategies()]
        if unknown:
            raise HTTPException(400, f"неизвестные стратегии: {', '.join(unknown)}")
        try:
            cfg = RunConfig(
                template=entry.path, content_pack=pack_dir, purpose=purpose, audience=audience, language=language,
                target_slides=target_slides, strategies=names, images="off", output_dir=job.dir,
                render_png=render_png, export=[e.strip() for e in export.split(",") if e.strip()],  # type: ignore[arg-type]
                audit={"deterministic": True, "contextual": judge, "autofix": autofix},
            )
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        factory = s.client_factory
        s.jobs.submit(job, lambda j: run(cfg, client=factory(), progress=j.say))
        return JobCreated(id=job.id, status=job.status)

    @app.get("/jobs", response_model=list[JobInfo])
    def jobs(s: S) -> list[JobInfo]:
        return [_job_info(j) for j in s.jobs.list()]

    @app.get("/jobs/{job_id}", response_model=JobInfo)
    def job(s: S, job_id: str) -> JobInfo:
        return _job_info(_job(s, job_id))

    @app.get("/jobs/{job_id}/files/{name}")
    def job_file(s: S, job_id: str, name: str) -> FileResponse:
        j = _job(s, job_id)
        if name not in RUN_FILES:
            raise HTTPException(404, "нет такого файла")
        return _file(j.dir / name)

    # ──────────────── колода: аудит, фиксы, файлы ────────────────

    @app.get("/jobs/{job_id}/decks/{strategy}/audit", response_model=AuditResponse)
    def deck_audit(s: S, job_id: str, strategy: str) -> AuditResponse:
        """Отчёт аудита + план фиксов: `how` = safe (уже применён в run) / ir (по выбору) / replan / template / n/a."""
        d = _deck(_job(s, job_id), strategy)
        report = d.load_report()
        if report is None:
            raise HTTPException(404, "у колоды нет аудита (audit.deterministic: false)")
        return AuditResponse(summary=d.audit_summary, report=report, fix_plan=fix_plan_rows(report))

    @app.post("/jobs/{job_id}/decks/{strategy}/fix", response_model=FixResponse)
    def deck_fix(s: S, job_id: str, strategy: str, body: FixRequest) -> FixResponse:
        """Применить выбранные пользователем фиксы (индексы находок) → правка IR → рендер → повторный аудит."""
        j = _job(s, job_id)
        d = _deck(j, strategy)
        if j.status != "done" or j.parsed is None:
            raise HTTPException(409, f"job в состоянии {j.status}")
        try:
            new = refine_deck(d, j.parsed, body.findings, render_png=body.render_png, progress=j.say)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        j.decks[strategy] = new
        fi = new.audit_summary.get("autofix", {})
        return FixResponse(strategy=strategy, applied=fi.get("applied", 0), skipped=fi.get("skipped", 0),
                           before=fi.get("before", {}), after=fi.get("after", {}), items=fi.get("items", []),
                           audit={k: v for k, v in new.audit_summary.items() if k != "autofix"},
                           contextual_stale=bool(new.audit_summary.get("contextual_stale")))

    @app.get("/jobs/{job_id}/decks/{strategy}/files/{name}")
    def deck_file(s: S, job_id: str, strategy: str, name: str) -> FileResponse:
        """Только белый список имён: <strategy>.pptx/.pdf/.ir.json/.audit.json/.manifest.json, slide_NN.png, contact.png."""
        j = _job(s, job_id)
        _deck(j, strategy)  # 404, если стратегии нет в job'е
        if name in {f"{strategy}.{ext}" for ext in DECK_FILES}:
            return _file(j.dir / name)
        if _PNG.match(name):
            return _file(j.dir / strategy / name)
        raise HTTPException(404, "нет такого файла")

    # ──────────────── аудит чужой колоды ────────────────

    @app.post("/audit", response_model=AuditResponse)
    async def audit(
        s: S,
        deck: UploadFile = File(...),
        template_id: str | None = Form(None),
        template: UploadFile | None = File(None),
    ) -> AuditResponse:
        """Детерминированный аудит любой .pptx по шаблону (id из реестра или файл). Колода не меняется."""
        from deckforge.audit import audit_deck, summary

        if template is not None:
            entry = s.templates.add_upload(template.filename or "template.pptx", await template.read())
        elif template_id:
            entry = _template(s, template_id)
        else:
            raise HTTPException(400, "нужен template_id или файл template")
        parsed = s.templates.parsed(entry)
        adir = s.root / "audits"
        adir.mkdir(exist_ok=True)
        path = adir / f"{safe_name(Path(deck.filename or 'deck').stem, 'deck')[:40]}_{uuid.uuid4().hex[:8]}.pptx"
        path.write_bytes(await deck.read())
        try:
            check_pptx(path)
        except BadUpload as e:
            path.unlink(missing_ok=True)
            raise HTTPException(400, str(e)) from e
        report = audit_deck(path, parsed.dna)
        return AuditResponse(summary=summary(report), report=report, fix_plan=fix_plan_rows(report))

    # ──────────────── помощники ────────────────

    def _template(s: State, template_id: str) -> TemplateEntry:
        entry = s.templates.get(template_id)
        if entry is None:
            raise HTTPException(404, f"шаблон {template_id} не найден — см. GET /templates")
        return entry

    def _template_info(e: TemplateEntry) -> TemplateInfo:
        return TemplateInfo(id=e.id, name=e.name, builtin=e.builtin, path=str(e.path),
                            summary=e.parsed.summary() if e.parsed else None)

    def _job(s: State, job_id: str) -> Job:
        j = s.jobs.get(job_id)
        if j is None:
            raise HTTPException(404, "job не найден")
        return j

    def _deck(j: Job, strategy: str):
        d = j.decks.get(strategy)
        if d is None:
            raise HTTPException(404, f"колоды {strategy} нет в job {j.id} (status {j.status})")
        return d

    def _file(path: Path) -> FileResponse:
        if not path.is_file():
            raise HTTPException(404, "файл ещё не создан")
        return FileResponse(path, filename=path.name)

    def _job_info(j: Job) -> JobInfo:
        base = f"/jobs/{j.id}"
        decks = []
        for d in j.decks.values():
            files: dict[str, str | list[str]] = {
                ext: f"{base}/decks/{d.strategy}/files/{d.strategy}.{ext}" for ext in DECK_FILES
                if (j.dir / f"{d.strategy}.{ext}").exists()}
            files["pngs"] = [f"{base}/decks/{d.strategy}/files/{p.name}" for p in d.pngs]
            if (j.dir / d.strategy / "contact.png").exists():
                files["contact"] = f"{base}/decks/{d.strategy}/files/contact.png"
            decks.append(DeckInfo(strategy=d.strategy, stats=d.stats, audit=d.audit_summary, warnings=d.warnings,
                                  timings_s=d.timings_s, files=files))
        r = j.result
        not_impl: list[str] = []
        if r and r.run_json and r.run_json.exists():
            not_impl = json.loads(r.run_json.read_text("utf-8")).get("not_implemented", [])
        return JobInfo(id=j.id, status=j.status, created=j.created, progress=list(j.progress), error=j.error,
                       template=j.parsed.meta if j.parsed else ({"id": j.template.id, "name": j.template.name}
                                                                if j.template else None),
                       outline=f"{base}/files/outline.json" if r else None, decks=decks,
                       warnings=r.warnings if r else [], timings_s=r.timings_s if r else {},
                       not_implemented=not_impl)

    return app


app = create_app()

__all__ = ["app", "create_app"]
