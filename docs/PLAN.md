# План: «Цифровой дизайнер презентаций» (хакатон ЛЦТ 2026, кейс VK Tech)

## Контекст

2 недели, стек Python (FastAPI) + Streamlit, инференс через внешний API (открытые модели ≤35B, Apache 2.0/MIT). Нужно сдать: репозиторий, документацию (README / ARCHITECTURE / MODELS / AUDIT), питч (≤7 мин, шаблон ЛЦТ2026), работающий прототип, видео-демо и 9 презентаций (1 контент × 3 шаблона × 3 варианта вёрстки).

Архитектура по факту — `docs/ARCHITECTURE.md`, границы слоёв и правила работы с кодом — `CLAUDE.md`, открытые пункты — `docs/BACKLOG.md`.

## График на 14 дней

| День | Результат |
|---|---|
| 1 | Каркас репо, `CLAUDE.md`, проектные скиллы Claude Code, `llm/` клиент + smoke-тест API, `pptx_xray` по 4 шаблонам |
| 2–3 | `parsing/`: токены из использования, классификатор архетипов, индекс образцов, рендер → PNG, VLM-теггер. Тест: 4 шаблона парсятся, DNA выглядит осмысленно |
| 4 | `core/ir.py`, `content/` (outline_writer skill), синтетический контент-пакет |
| 5–6 | `layout/` + `render/pptx_writer.py`: клонирование образцов, заполнение текста, fitting. Первая колода end-to-end на VK Tech |
| 7 | Графики/таблицы/иконки нативно; 3 стратегии; `export/` (pdf, html) |
| 8–9 | `audit/`: детерминированные проверки (≈20 из Приложения 1) + VLM-судья (11 вопросов) + autofix + тесты на «плохих» слайдах |
| 10 | Streamlit UI (загрузка → бриф → 3 варианта → аудит с выбором фиксов → экспорт), FastAPI-обёртка, CLI `run --config` |
| 11 | Прогон 9 колод + holdout ЛЦТ2026; правка самых заметных дефектов; картинки (T2I) |
| 12 | Docs: README / ARCHITECTURE / MODELS / AUDIT; Dockerfile; `/code-review`, `/security-review` |
| 13 | Питч-дек (dogfood через сервис + ручная доводка), репетиция ≤7 мин, запись видео-демо без склеек |
| 14 | Буфер, тег релиза `v0.1.0`, финальная проверка чек-листа сдачи |

Правило: e2e-колода должна существовать к дню 6, даже уродливая. Всё после — улучшение.

### Статус по дням (факт на 17.09.2026)

Дни плана — логические, не календарные: дни 1–12 сделаны 15–17.09.2026. Теги по дням ставятся с `day10` (дни 1–9 — только коммиты). Перенесённое и открытые пункты — `docs/BACKLOG.md`, секции по тем же дням.

| День | Коммиты / тег | Сделано |
|---|---|---|
| 1 | `88ed06b` (15.09) | Каркас пакета, `CLAUDE.md`, скиллы Claude Code (`pptx-xray`, `render-deck`; позже `audit-deck`, `skill-bump`, `classify-layouts`, `build-variants`), `llm/client.py` + `llm/skills.py`, `tools/check_env.py`, `tools/pptx_xray.py`, все 5 скиллов сервиса v1 + `registry.yaml`, заготовки стратегий, `core/ir.py`, `parsing/extract_tokens.py` + тесты на 4 шаблона |
| 2–3 | `c468cc8` (15.09) | `layout_classifier` по геометрии + VLM `template_tagger`, `tools/classify_layouts.py`, кэш `out/archetypes/` (sha1 файла), тесты; токены — уже в дне 1 |
| 4 | `b4e785e` (15.09, контент-пакет), `0fa3cb2` (16.09, `content/`) | `examples/content_pack/` (бриф, product.md, `outline.json`); `content/outline_writer` + `content_pack` + `repair_outline` + кассета. Порядок изменён: `content/` сделан после `layout/`+`render/`, дни 5–6 шли на синтетическом DeckIR и готовом `outline.json` |
| 5–6 | `3e31c90`, `b4e785e` (15.09) | `render/pptx_writer.py` (клонирование образцов, слоты, rels, materialize плейсхолдеров), `layout/` (planner, exemplar_picker, fitting, builder), `core/strategy.py`, `tools/build_variants.py`; e2e-колода к дню 6 есть — три варианта на готовом outline |
| 7 | `3e31c90`, `b4e785e`, `0fa3cb2` (15–16.09) | Нативные `render/charts.py`, `render/tables.py`; `strategies/{executive,narrative,visual}.yaml`; `export/render.py` (pdf/PNG через LibreOffice). Иконки и схемы — из образцов самого шаблона; HTML-экспорт перенесён на день 12 |
| 8–9 | `5ade04c`, `ac19c8b` (16.09) | 24 детерминированные проверки (план — ≈20), фикстуры `tests/fixtures/bad_slides.py` + `tools/make_fixtures.py`, VLM-судья (11 вопросов, кассета `audit_judge_pulse.json`), каталог фиксов `core/autofix` + применение `layout/autofix`, цикл аудит → фиксы → re-render в pipeline, `docs/AUDIT.md` |
| 10 | `9b8c554`, `d0b2e6a`, `aefb5c4`, `6ca4f29`; тег `day10` (16.09) | Этапы `pipeline/run.py` (`parse_template` / `make_outline` / `build_deck` / `refine_deck`), `cli parse`, FastAPI, Streamlit, `pipeline/workspace` |
| 11 | `b7325a7`, `d337546`, `a2436cd`, `e461dde`, `5a84610`; тег `day11` (16.09) | 12 колод в `examples/output/` (4 шаблона × 3 стратегии, включая holdout ЛЦТ2026, с судьёй и картинками), картинки T2I (`content/images.py`), заметные дефекты вёрстки/рендера |
| 12 | `2f2d0df` … `2514165`; тег `day12` (17.09); после тега `0be9c6f`, `e3b8782` | `core/deck_reader`, HTML-экспорт (`export/html.py`), `cli export`, Dockerfile + `compose.yaml`, `/code-review` + `/security-review`, README / ARCHITECTURE / MODELS / AUDIT; `data/wild` (14 чужих шаблонов) — `parse` и `build_variants` без падений, дефекты 1–5, 7 закрыты (`e3b8782`, pytest 199) |
| 13 | `e3b8782` … ; тег `day13` (17.09) | Питч собран сервисом по holdout ЛЦТ2026 (`configs/pitch.yaml`, `examples/pitch/`: бриф по структуре ТЗ с блоком «Демо», `outline.llm.json` → `outline.json` с укороченными заголовками, `deckforge_pitch.pptx/.pdf/.html` + manifest, narrative 12 слайдов, 0 err), `script.md` (текст на 7 минут), `docs/DEMO.md` (сценарий видео на чужом шаблоне). Общие фиксы по дефектам питча: дискретная модель строк в `fitting` (VK Tech — 0 err без автофиксов), «чипы» → `hard_lines` (плашка вокруг/под заголовком, только видимая), белый текст таблиц/диаграмм на тёмном фоне (`background_luminance`), компактные строки таблиц; pytest 199. Не сделано руками: видео, PowerPoint, Firefox/Яндекс — день 14 |
| 14 | — | Следующий: видео-демо и репетиция, PowerPoint/Firefox/Яндекс глазами, перегенерация `examples/output` финальным кодом, `v0.1.0`, чек-лист |

## Чек-лист сдачи

- [ ] Репозиторий публичный, тег версии, `configs/run.example.yaml` + `docker compose up` воспроизводят прогон — compose проверен 17.09 (день 12); тег `v0.1.0`, публичность и прогон на чистом clone — день 14
- [x] README (сетап, `.env` переменные, ограничения), ARCHITECTURE, MODELS (HF-ссылки, лицензии, требования), AUDIT (таблица проверок: id, детерминированная/контекстуальная, покрытие) — день 12; `pytest` зелёный (парсинг 4 шаблонов, каждая проверка на своей фикстуре, e2e с мок-LLM на кассетах); скриншоты UI в README — день 14
- [x] `skills/` с версиями, `manifest.json` в каждой колоде
- [x] 9 колод (3 шаблона × 3 варианта) в `examples/output/` + holdout на ЛЦТ2026 — 12 колод, день 11. ЛЦТ2026 не использовался при разработке правил парсинга; адаптивность дополнительно проверена на `data/wild` (день 12)
- [x] Экспорт .pdf, .html (день 12, HTML проверен в Chrome; Firefox/Яндекс — день 14)
- [ ] Экспорт .pptx нативными объектами — открыть в PowerPoint и проверить редактируемость (день 14; пока проверялся только рендер LibreOffice)
- [x] Аудит визуализирует проблемы в UI, пользователь выбирает фиксы — день 10
- [x] Время генерации колоды ≤5 мин (лог в manifest) — измерено, см. «Бюджет времени» в ARCHITECTURE; латентность на инференсе VK — день 14
- [ ] Видео-демо ≤7 мин без склеек (сценарий — `docs/DEMO.md`, запись — день 14); питч-дек по структуре ТЗ — `examples/pitch/deckforge_pitch.pptx` + `script.md` (день 13)
