# DEVELOPMENT

Памятка для работы с кодом. Пользовательские команды (`run`, `audit`, `export`, UI, API, Docker) — в README, устройство — в ARCHITECTURE.

## Окружение

```bash
python -m venv .venv && .venv/Scripts/activate      # Python 3.12+
pip install -e ".[dev]"
pytest -q                                          # LLM не вызывается: кассеты в tests/cassettes
ruff check deckforge tools tests
```

Шаблоны датасета лежат в Git LFS (`.gitattributes`) — без `git lfs` вместо `.pptx` будут указатели.

## Служебные скрипты

| Скрипт | Что делает |
|---|---|
| `tools/pptx_xray.py <file.pptx>` | структура любого .pptx: размер, тема vs реальные цвета/шрифты, лейауты, повторяющиеся фигуры |
| `tools/render_deck.py <file.pptx> [--contact]` | .pptx → PNG через LibreOffice, при `--contact` — контактный лист |
| `tools/render_smoke.py <template.pptx> [--all]` | синтетический DeckIR по образцам шаблона → колода, без LLM |
| `tools/classify_layouts.py [--vlm] [--publish] <glob>` | таблица «слайд → архетип → слоты»; `--publish` кладёт ответы VLM в `data/archetypes/` |
| `tools/build_variants.py <template.pptx>` | три стратегии на готовом outline, без LLM — быстрый цикл после правок `layout/`, `render/`, `strategies/` |
| `tools/stress_test.py [--wild] [--cases …] [--templates …]` | крайние outline × шаблоны × стратегии, каждая пара в подпроцессе; отчёт в `out/stress/` |
| `tools/make_fixtures.py --audit` | «плохие» слайды из `tests/fixtures/bad_slides.py` в `out/fixtures/` — посмотреть глазами |
| `tools/powerpoint_check.py <glob>` | открытие и сохранение колод в настоящем PowerPoint (Windows, COM) |
| `tools/check_env.py` | проверка `.env` и доступности моделей |

Полная матрица стресс-теста идёт около 40 минут; пока она идёт, код в `deckforge/` не править — каждая пара стартует новым подпроцессом.

## Правила

- Слои и направление зависимостей: `ui, api, cli → pipeline → {parsing, content, layout, render, audit, export} → core, llm`. Импорт вверх или между соседями (`audit → layout`, `export → audit`) запрещён. Контракты между слоями — `core/ir.py`.
- Промпты, схемы ответов и параметры моделей — только в `skills/<name>/v<N>/`; в коде строковых промптов нет. Изменение промпта = новая папка `v<N+1>` + запись в `skills/registry.yaml`, старые версии не редактируются.
- Стратегии вёрстки — `strategies/*.yaml`, схема в `core/strategy.py`.
- Пороги проверок аудита — константы в начале модулей `audit/deterministic/*.py`. Ложное срабатывание чинится порогом или чистой фикстурой в `tests/fixtures/bad_slides.py`, пропущенный дефект — новой фикстурой.
- Новый автофикс — запись в `core/autofix.FIXES`, обработчик в `layout/autofix.py`, тест в `tests/test_autofix.py`.
- Странный ответ модели чинится в `content/outline_writer.repair_outline` или `audit/contextual/judge.findings_from_answers` с тестом на синтетическом ответе, а не ретраем.
- Артефакты (`out/`, `examples/output/*.pptx`) руками не редактируются.

## Ловушки

- lxml `append` переносит элемент из кэшированного дерева `Package.xml` — копировать через `copy.deepcopy`.
- LibreOffice рисует плейсхолдеры лейаута, у которых нет пары на слайде (PowerPoint — нет); поэтому `render/pptx_writer.py` материализует и гасит их.
- Кэш VLM-разметки (`out/archetypes/`, `data/archetypes/`) хранит только ответы теггера; слоты и архетипы пересчитываются правилами при каждой загрузке.
- Модель по умолчанию «думает»; `LLMClient.run_skill` выключает reasoning, если в `skill.yaml` не стоит `reasoning: true`.
