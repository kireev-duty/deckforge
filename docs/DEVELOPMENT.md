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
| `tools/build_gallery.py [--space-url …]` | галерея готовых колод `examples/output` → `out/gallery/` (GitHub Pages) |
| `deploy/hf_space/stage.py [dir]` | staging-папка демо-стенда → `out/hf_space/` |

Полная матрица стресс-теста идёт около 40 минут; пока она идёт, код в `deckforge/` не править — каждая пара стартует новым подпроцессом.

## Демо-стенд

Публичный стенд — Streamlit UI на [Streamlit Community Cloud](https://deckforge.streamlit.app) (бесплатно, из публичного репо, ветка `master`). Галерея готовых колод — [GitHub Pages](https://kireev-duty.github.io/deckforge/).

| Что | Где |
|---|---|
| Python-зависимости стенда (те же, что в `pyproject.toml`; синхронность — `tests/test_workspace.py`) | `requirements.txt` |
| apt-пакеты: LibreOffice Impress, шрифты Montserrat / Liberation / DejaVu | `packages.txt` |
| лимит загрузки 60 МБ, без телеметрии | `.streamlit/config.toml` |
| секреты Cloud (TOML в настройках приложения) → `os.environ` | `ui/app._secrets_to_env` |
| шаблоны датасета: Cloud может клонировать репо без Git LFS — указатели докачиваются из `DECKFORGE_LFS_BASE` | `pipeline/workspace.fetch_lfs` |
| галерея: тег `v*` или ручной запуск → `build_gallery.py` → Pages (кнопка — переменная `DEMO_URL`) | `.github/workflows/pages.yml` |

Публичный режим UI включает `DECKFORGE_PUBLIC=1` (`deckforge/ui/app.py`): одна генерация на процесс (`run_lock`, очередь до 10 минут), судья по умолчанию выключен, в списке шаблонов — датасет и загруженные в этой сессии. Без `LLM_API_KEY` доступен только «Готовый outline» (`examples/output/vk_tech/outline.json`). Конвертации LibreOffice в одном процессе идут строго по одной: общий профиль (`export/render._SOFFICE_LOCK`); растеризация PyMuPDF — тоже (`_MUPDF_LOCK`: MuPDF однопоточный), а колоды одного прогона собираются в потоках и встают к ним в очередь. Лок не межпроцессный: два процесса с LibreOffice на одной машине (UI + pytest, две сессии разработки) делят профиль `%TEMP%/deckforge_lo_profile` и изредка роняют друг другу конвертацию («LibreOffice: код 1» после повтора) — тесты с LibreOffice и замеры не запускать одновременно с другим прогоном.

Деплой на Streamlit Cloud (один раз, дальше приложение само подхватывает push в `master`): share.streamlit.io → Create app → репо `kireev-duty/deckforge`, ветка `master`, файл `deckforge/ui/app.py`, адрес `deckforge` → Advanced settings: Python 3.12, Secrets:

```toml
LLM_API_KEY = "sk-or-…"
LLM_BASE_URL = "https://openrouter.ai/api/v1"
LLM_MODEL = "qwen/qwen3.8-27b-20260814"
VLM_MODEL = "qwen/qwen3.8-27b-20260814"
T2I_MODEL = ""
DECKFORGE_PUBLIC = "1"
DECKFORGE_LFS_BASE = "https://media.githubusercontent.com/media/kireev-duty/deckforge/master"
```

Логи сборки и перезапуск — «Manage app» в правом нижнем углу приложения.

Запасной вариант с полноценным образом (свои шрифты Play, 2 vCPU / 16 ГБ) — Docker-Space на Hugging Face; бесплатный CPU для Docker-Spaces теперь требует PRO. Всё готово: образ `deploy/hf_space/Dockerfile`, staging `deploy/hf_space/stage.py`, деплой — ручной запуск `.github/workflows/deploy-space.yml` (секрет `HF_TOKEN`, секреты Space — те же ключи, что выше). Локальная проверка образа:

```bash
python deploy/hf_space/stage.py                     # → out/hf_space (≈90 МБ; нужны шаблоны из LFS)
docker build -t deckforge-space out/hf_space
docker run --rm -p 7860:7860 -e LLM_API_KEY deckforge-space   # http://localhost:7860; --env-file не режет комментарии
```

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
