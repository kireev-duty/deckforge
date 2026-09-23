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

Публичный стенд — Streamlit UI в [Hugging Face Space](https://huggingface.co/spaces/kireev-duty/deckforge) (Docker SDK, CPU basic). Галерея готовых колод — [GitHub Pages](https://kireev-duty.github.io/deckforge/).

| Что | Где |
|---|---|
| образ стенда: шаблоны датасета и outline примеров внутри образа, шрифты Play и Montserrat, uid 1000, порт 7860 | `deploy/hf_space/Dockerfile` |
| карточка Space (front matter: `sdk: docker`, `app_port`) | `deploy/hf_space/space_readme.md` → `README.md` Space |
| состав Space: код, промпты, стратегии, `data/{templates,holdout,archetypes}`, `examples/content_pack`, outline VK Tech | `deploy/hf_space/stage.py` |
| деплой: тег `v*` или ручной запуск → staging → `upload_folder` в Space, дальше Space сам собирает образ (≈10 мин) | `.github/workflows/deploy-space.yml` |
| галерея: тег `v*` или ручной запуск → `build_gallery.py` → Pages | `.github/workflows/pages.yml` |

Публичный режим UI включает `DECKFORGE_PUBLIC=1` (`deckforge/ui/app.py`): одна генерация на процесс (`run_lock`, очередь до 10 минут), судья по умолчанию выключен, в списке шаблонов — датасет и загруженные в этой сессии. Без `LLM_API_KEY` доступен только «Готовый outline» (`examples/output/vk_tech/outline.json`). Конвертации LibreOffice в одном процессе идут строго по одной: общий профиль (`export/render._SOFFICE_LOCK`).

Секреты:
- GitHub → Settings → Secrets and variables → Actions: секрет `HF_TOKEN` (write-токен HF); переменные `HF_SPACE` (`<владелец>/deckforge`, если владелец не совпадает с токеном) и `HF_SPACE_URL` (ссылка для кнопки в галерее).
- Space → Settings → Variables and secrets: секрет `LLM_API_KEY` (отдельный ключ OpenRouter с credit limit), переменные `LLM_BASE_URL`, `LLM_MODEL`, `VLM_MODEL` — как в `.env.example`, `T2I_MODEL` — пусто.
- GitHub → Settings → Pages → Source: GitHub Actions.

Локальная проверка образа:

```bash
python deploy/hf_space/stage.py                     # → out/hf_space (≈90 МБ; нужны шаблоны из LFS)
docker build -t deckforge-space out/hf_space
docker run --rm -p 7860:7860 --env-file .env deckforge-space   # http://localhost:7860
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
