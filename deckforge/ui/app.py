"""Streamlit UI: шаблон → бриф → варианты → аудит с выбором фиксов → экспорт (`streamlit run deckforge/ui/app.py`).

Пайплайн вызывается in-process; файлы прогона — `out/ui/runs/<время>/`, загруженные шаблоны — `out/ui/templates/`.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:  # `streamlit run` запускает файл как скрипт
    sys.path.insert(0, str(ROOT))

from deckforge.core.autofix import FIXES, fix_plan_rows
from deckforge.core.ir import AuditReport, Finding
from deckforge.core.strategy import list_strategies
from deckforge.llm import load_dotenv
from deckforge.pipeline import (
    DeckResult,
    ParsedTemplate,
    RunConfig,
    refine_deck,
    run,
    soffice_available,
)
from deckforge.pipeline.workspace import (
    BadUpload,
    TemplateEntry,
    TemplateStore,
    write_content_pack,
)
from deckforge.ui.overlay import draw_findings

UI_ROOT = ROOT / "out" / "ui"
EXAMPLE_PACK = ROOT / "examples" / "content_pack"
PURPOSES = ["product", "feature", "project", "initiative", "report", "other"]
HOW_LABEL = {"safe": "безопасный", "ir": "по выбору (теряет часть контента)", "replan": "нужен пересбор",
             "template": "дизайн шаблона — не чиним", "n/a": "—"}

st.set_page_config(page_title="deckforge", page_icon="🎞️", layout="wide")
load_dotenv()


@st.cache_resource
def store() -> TemplateStore:
    return TemplateStore(UI_ROOT)


@st.cache_resource(show_spinner="Разбираю шаблон…")
def parsed_template(template_id: str) -> ParsedTemplate:
    entry = store().get(template_id)
    assert entry is not None
    return store().parsed(entry)


def ss(key: str, default=None):
    return st.session_state.get(key, default)


# ──────────────────────────── sidebar: шаблон и параметры ────────────────────────────


def sidebar() -> tuple[TemplateEntry | None, dict]:
    st.sidebar.title("deckforge")
    st.sidebar.caption("Цифровой дизайнер презентаций — VK Tech, ЛЦТ 2026")

    st.sidebar.subheader("1. Шаблон")
    entries = store().list()
    names = {f"{'📦 ' if e.builtin else '📤 '}{e.name}": e for e in entries}
    up = st.sidebar.file_uploader("Свой шаблон .pptx", type=["pptx"], key="template_upload")
    if up is not None and ss("uploaded_name") != up.name:
        try:
            entry = store().add_upload(up.name, up.getvalue())
            st.session_state["uploaded_name"] = up.name
            st.session_state["template_choice"] = f"{'📦 ' if entry.builtin else '📤 '}{entry.name}"
            names = {f"{'📦 ' if e.builtin else '📤 '}{e.name}": e for e in store().list()}
        except BadUpload as e:
            st.sidebar.error(str(e))
    choice = st.sidebar.selectbox("Из датасета или загруженный", list(names), key="template_choice")
    entry = names.get(choice) if choice else None

    st.sidebar.subheader("2. Параметры")
    opts = {
        "purpose": st.sidebar.selectbox("Тип презентации", PURPOSES, index=0),
        "audience": st.sidebar.text_input("Аудитория", "руководители продуктовых направлений"),
        "language": st.sidebar.selectbox("Язык", ["ru", "en"], index=0),
        "target_slides": st.sidebar.slider("Ориентир по объёму (слайдов)", 6, 20, 12),
        "strategies": st.sidebar.multiselect("Варианты вёрстки", list_strategies(), default=list_strategies()),
        "autofix": st.sidebar.checkbox("Безопасные автофиксы", True, help="Уменьшить кегль, привести к шкале и т.п. — без потери смысла"),
        "judge": st.sidebar.checkbox("VLM-судья (11 вопросов по PNG)", True, help="≈30 с на колоду, нужны LibreOffice и API"),
        "render_png": st.sidebar.checkbox("PNG-превью", soffice_available(), disabled=not soffice_available(),
                                          help="LibreOffice не найден" if not soffice_available() else "≈10 с на колоду"),
        "pdf": st.sidebar.checkbox("Экспорт PDF", soffice_available(), disabled=not soffice_available()),
        "html": st.sidebar.checkbox("Экспорт HTML", True, help="Один файл, без LibreOffice; открывается в браузере"),
    }
    if not soffice_available():
        st.sidebar.warning("LibreOffice не найден: без PNG, PDF и VLM-судьи. Задайте SOFFICE_PATH.")
    return entry, opts


# ──────────────────────────── шаг 1: шаблон ────────────────────────────


def show_template(entry: TemplateEntry) -> ParsedTemplate:
    parsed = parsed_template(entry.id)
    s = parsed.summary()
    st.subheader(f"Шаблон: {entry.name}")
    c1, c2, c3 = st.columns([2, 2, 3])
    with c1:
        st.markdown("**Палитра** (по использованию на слайдах)")
        sw = "".join(
            f'<div style="display:inline-block;margin:2px 6px 2px 0"><span style="display:inline-block;width:22px;'
            f'height:22px;border-radius:4px;background:#{h};border:1px solid #ccc;vertical-align:middle"></span>'
            f'<span style="font-size:12px;margin-left:4px">{role} #{h}</span></div>'
            for role, hexes in s["palette"].items() for h in hexes[:2])
        st.markdown(sw, unsafe_allow_html=True)
        st.markdown(f"**Шрифты:** {', '.join(s['fonts'][:3]) or '—'}"
                    + (f" *(встроены: {', '.join(s['embedded_fonts'])})*" if s["embedded_fonts"] else ""))
        st.markdown("**Шкала:** " + ", ".join(f"{x['role']} {x['size_pt']:g}" for x in s["typography"]))
    with c2:
        st.markdown("**Образцы по архетипам**")
        st.dataframe({"архетип": list(s["archetypes"]), "образцов": list(s["archetypes"].values())},
                     hide_index=True, height=min(400, 38 + 35 * len(s["archetypes"])))
    with c3:
        st.markdown(f"**Слайд:** {s['slide_w'] / 914400:.2f}×{s['slide_h'] / 914400:.2f} in · "
                    f"**слайдов-образцов:** {s['slides']} · **фиксированных элементов:** {s['fixed_elements']}")
        g = s["grid"]
        st.markdown(f"**Поля:** {g['margin_left'] / 914400:.2f} / {g['margin_right'] / 914400:.2f} / "
                    f"{g['margin_top'] / 914400:.2f} / {g['margin_bottom'] / 914400:.2f} in, "
                    f"колонок {g['columns']}, строк {g['rows']}")
        contact = ROOT / "out" / "render" / entry.path.stem / "contact.png"
        if contact.exists():
            st.image(str(contact), caption="слайды шаблона", width=520)
        else:
            st.caption("Разбор ≈ 1 с; классификация образцов — правилами по геометрии"
                       + ("" if entry.builtin else " (VLM-уточнение для загруженных шаблонов — в следующей версии)"))
    return parsed


# ──────────────────────────── шаг 2: бриф и запуск ────────────────────────────


def brief_form(entry: TemplateEntry, opts: dict) -> None:
    st.subheader("Бриф и контент-пакет")
    default_brief = (EXAMPLE_PACK / "brief.md").read_text("utf-8") if (EXAMPLE_PACK / "brief.md").exists() else ""
    brief = st.text_area("Бриф (brief.md)", default_brief, height=260,
                         help="Цель, аудитория, ключевые тезисы. Факты для слайдов — в файлах ниже.")
    c1, c2 = st.columns([3, 2])
    with c1:
        files = st.file_uploader("Файлы контент-пакета: *.md, *.txt → корень; *.json, *.csv → data/",
                                 type=["md", "txt", "json", "csv"], accept_multiple_files=True)
    with c2:
        use_example = st.checkbox("Добавить пример «Пульс команды» (product.md + metrics.json)",
                                  value=not files, help=f"{EXAMPLE_PACK}")
    disabled = not brief.strip() or not opts["strategies"]
    if st.button("Сгенерировать варианты", type="primary", disabled=disabled, width="stretch"):
        generate(entry, opts, brief, files or [], use_example)


def generate(entry: TemplateEntry, opts: dict, brief: str, files: list, use_example: bool) -> None:
    run_dir = UI_ROOT / "runs" / datetime.now().strftime("%Y%m%d-%H%M%S")
    pack_files = [(f.name, f.getvalue()) for f in files]
    if use_example:
        pack_files += [(p.name, p.read_bytes()) for p in EXAMPLE_PACK.glob("*.md") if p.name != "brief.md"]
        pack_files += [(p.name, p.read_bytes()) for p in (EXAMPLE_PACK / "data").glob("*.*")]
    try:
        pack_dir = write_content_pack(run_dir / "content_pack", brief, pack_files)
    except BadUpload as e:
        st.error(str(e))
        return
    cfg = RunConfig(
        template=entry.path, content_pack=pack_dir, purpose=opts["purpose"], audience=opts["audience"],
        language=opts["language"], target_slides=opts["target_slides"], strategies=opts["strategies"], images="off",
        output_dir=run_dir, render_png=opts["render_png"],
        export=["pptx", *(["pdf"] if opts["pdf"] else []), *(["html"] if opts["html"] else [])],
        audit={"deterministic": True, "contextual": opts["judge"], "autofix": opts["autofix"]},
    )
    with st.status("Генерация…", expanded=True) as status:
        try:
            result = run(cfg, progress=status.write)
        except Exception as e:  # noqa: BLE001
            status.update(label=f"Ошибка: {type(e).__name__}", state="error")
            st.exception(e)
            return
        status.update(label=f"Готово за {result.total_s:.0f} с → {run_dir.name}", state="complete", expanded=False)
    st.session_state["result"] = result
    st.session_state["decks"] = {d.strategy: d for d in result.decks}
    st.session_state["run_dir"] = run_dir
    st.rerun()


# ──────────────────────────── шаги 3–5: варианты, аудит, экспорт ────────────────────────────


def show_results() -> None:
    result = ss("result")
    decks: dict[str, DeckResult] = ss("decks", {})
    if not result or not decks:
        return
    run_dir: Path = ss("run_dir")
    st.subheader("Варианты")
    compare = run_dir / "compare.md"
    if compare.exists():
        st.markdown(compare.read_text("utf-8"))
    if result.warnings:
        with st.expander(f"Предупреждения прогона ({len(result.warnings)})"):
            st.write("\n".join(f"- {w}" for w in result.warnings))
    tabs = st.tabs([f"{name}  ·  {d.stats.get('slides', '?')} сл." for name, d in decks.items()])
    for tab, (name, deck) in zip(tabs, decks.items()):
        with tab:
            show_deck(name, deck)


def show_deck(name: str, deck: DeckResult) -> None:
    result = ss("result")
    parsed: ParsedTemplate = result.parsed
    manifest = deck.load_manifest()
    report = deck.load_report()
    ir = deck.load_ir()

    # ── превью ──
    if deck.pngs:
        cols = st.columns(4)
        for i, p in enumerate(deck.pngs):
            cols[i % 4].image(str(p), caption=f"{i + 1}. {ir.slides[i].archetype.value if i < len(ir.slides) else ''}",
                              width="stretch")
    else:
        st.caption("PNG-превью выключено или LibreOffice не найден — доступны только таблицы и скачивание.")

    # ── аудит ──
    st.markdown("#### Аудит")
    if report is None:
        st.info("Аудит выключен.")
    else:
        audit_block(name, deck, report, ir, parsed, manifest)

    # ── экспорт ──
    st.markdown("#### Экспорт")
    c = st.columns(6)
    c[0].download_button("⬇ .pptx", deck.pptx.read_bytes(), file_name=f"{name}.pptx", key=f"dl_pptx_{name}",
                         mime="application/vnd.openxmlformats-officedocument.presentationml.presentation")
    if deck.pdf and deck.pdf.exists():
        c[1].download_button("⬇ .pdf", deck.pdf.read_bytes(), file_name=f"{name}.pdf", key=f"dl_pdf_{name}",
                             mime="application/pdf")
    else:
        c[1].button("PDF — нет", disabled=True, key=f"dl_pdf_{name}")
    if deck.html and deck.html.exists():
        c[2].download_button("⬇ .html", deck.html.read_bytes(), file_name=f"{name}.html", key=f"dl_html_{name}",
                             mime="text/html")
    else:
        c[2].button("HTML — нет", disabled=True, key=f"dl_html_{name}")
    c[3].download_button("⬇ manifest.json", deck.manifest.read_bytes(), file_name=f"{name}.manifest.json",
                         key=f"dl_m_{name}", mime="application/json")
    c[4].download_button("⬇ ir.json", deck.ir_json.read_bytes(), file_name=f"{name}.ir.json", key=f"dl_ir_{name}",
                         mime="application/json")
    if deck.audit and deck.audit.exists():
        c[5].download_button("⬇ audit.json", deck.audit.read_bytes(), file_name=f"{name}.audit.json",
                             key=f"dl_a_{name}", mime="application/json")
    st.caption("Тайминги: " + ", ".join(f"{k} {v:.1f}с" for k, v in manifest.get("timings_s", {}).items()))


def audit_block(name: str, deck: DeckResult, report: AuditReport, ir, parsed: ParsedTemplate, manifest: dict) -> None:
    s = deck.audit_summary
    fi = s.get("autofix") or {}
    m = st.columns(5)
    m[0].metric("Ошибок", s.get("errors", 0))
    m[1].metric("Предупреждений", s.get("warnings", 0))
    m[2].metric("Контекстуальных", s.get("contextual", 0))
    m[3].metric("Автофиксов применено", fi.get("applied", 0),
                help=f"user_applied: {fi.get('user_applied', 0)}" if fi else None)
    if fi:
        m[4].metric("Ошибок до → после", f"{fi['before']['errors']} → {fi['after']['errors']}")
    if s.get("contextual_stale"):
        st.caption("Контекстуальные находки — от прошлого прогона судьи (после фиксов колода изменилась).")

    rows = {r["n"]: r for r in fix_plan_rows(report)}
    table = []
    for i, f in enumerate(report.findings):
        r = rows.get(i)
        table.append({
            "#": i, "выбрать": False, "слайд": f.slide_idx + 1, "проверка": f.check_id, "уровень": f.severity.value,
            "сообщение": f.message, "фикс": FIXES[r["fix"]].description if r else "",
            "как": HOW_LABEL.get(r["how"], r["how"]) if r else "",
            "_how": r["how"] if r else "",
        })
    # оставшиеся safe-фиксы тоже предлагаем: автофикс был выключен или не помог
    choosable = [t for t in table if t["_how"] in ("safe", "ir")]

    st.markdown(f"**Фиксы по выбору** ({len(choosable)}): безопасные — без потери смысла; «по выбору» — теряют "
                "часть контента, поэтому не применяются сами. Пересбор и дизайн шаблона автофиксом не чинятся.")
    selected: list[int] = []
    if choosable:
        gen = fi.get("applied", 0)  # после применения ключи чекбоксов меняются
        keys = [f"fix_{name}_{gen}_{t['#']}" for t in choosable]

        def _toggle_all(all_key: str = f"all_{name}_{gen}", keys: list[str] = keys) -> None:
            for k in keys:  # значение виджета с key задаётся только через session_state
                st.session_state[k] = st.session_state[all_key]

        st.checkbox("выбрать все", key=f"all_{name}_{gen}", on_change=_toggle_all)
        for t, k in zip(choosable, keys):
            icon = "🟢" if t["_how"] == "safe" else "🟠"
            label = (f"{icon} сл. {t['слайд']} · {t['проверка']} · {t['уровень']} — {t['сообщение'][:110]}"
                     f"  →  *{t['фикс']}*")
            if st.checkbox(label, key=k):
                selected.append(int(t["#"]))
    else:
        st.caption("нет")
    if st.button(f"Применить выбранные ({len(selected)})", disabled=not selected, key=f"apply_{name}"):
        with st.status("Применяю фиксы…", expanded=True) as status:
            new = refine_deck(deck, parsed, selected, progress=status.write)
            status.update(label="Готово", state="complete")
        st.session_state["decks"][name] = new
        st.rerun()

    with st.expander(f"Все находки ({len(table)}) и журнал автофиксов ({len(fi.get('items', []))})"):
        st.dataframe([{k: v for k, v in t.items() if k not in ("_how", "выбрать")} for t in table],
                     hide_index=True, width="stretch")
        if fi.get("items"):
            st.dataframe(fi["items"], hide_index=True, width="stretch")

    if deck.pngs:
        st.markdown("**Подсветка на слайде**")
        with_issues = sorted({f.slide_idx for f in report.findings if f.box is not None})
        if with_issues:
            idx = st.selectbox("Слайд", with_issues, format_func=lambda i: f"{i + 1}", key=f"slide_{name}")
            flist: list[Finding] = [f for f in report.findings if f.slide_idx == idx]
            if idx < len(deck.pngs):
                c1, c2 = st.columns([3, 2])
                c1.image(draw_findings(deck.pngs[idx], flist, ir.slide_w, ir.slide_h), width="stretch")
                c2.dataframe([{"№": n, "проверка": f.check_id, "уровень": f.severity.value, "сообщение": f.message}
                              for n, f in enumerate(flist, start=1)], hide_index=True, width="stretch")
        else:
            st.caption("Находок с координатами нет.")


# ──────────────────────────── страница ────────────────────────────


def main() -> None:
    entry, opts = sidebar()
    st.title("Цифровой дизайнер презентаций")
    st.caption("Шаблон .pptx → дизайн-система → три варианта колоды из брифа → аудит с фиксами → .pptx / .pdf")
    if entry is None:
        st.info("Выберите шаблон в боковой панели или загрузите свой .pptx.")
        return
    try:
        show_template(entry)
    except Exception as e:  # noqa: BLE001
        st.error(f"Шаблон не разобран: {type(e).__name__}: {e}")
        return
    st.divider()
    brief_form(entry, opts)
    st.divider()
    show_results()


main()  # streamlit исполняет модуль как скрипт
