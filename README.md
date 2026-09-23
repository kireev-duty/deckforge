# DECKFORGE — цифровой дизайнер презентаций

[![Попробовать онлайн — Streamlit](https://static.streamlit.io/badges/streamlit_badge_black_white.svg)](https://deckforge.streamlit.app)

**Попробовать без установки:** [демо-стенд](https://deckforge.streamlit.app) → выбрать шаблон слева (или загрузить свой .pptx) → «Только шаблон» → «Сгенерировать варианты»: три колоды за 1–3 минуты, с аудитом и скачиванием .pptx / .pdf / .html. Если ключ LLM стенда исчерпан или API недоступен, работает режим «Готовый outline»: он собирает колоды без LLM. **Готовые 12 колод** (4 шаблона × 3 стратегии) можно открыть в браузере: [галерея](https://kireev-duty.github.io/deckforge/).

Сервис читает произвольный .pptx-шаблон как набор правил (дизайн-система + слайды-образцы) и собирает по нему новую презентацию: структура → тексты → вёрстка нативными объектами → аудит → экспорт в .pptx / .pdf / .html. Три стратегии вёрстки на один и тот же контент.

Контент — лестница входа: контент-пакет с брифом и данными, если он есть; иначе тема одной строкой; а если на входе **только шаблон**, бриф выводится из него самого (бренд, тексты слайдов-образцов, палитра — скилл `template_brief`), и прогон идёт дальше без изменений. Чем меньше исходников, тем меньше в колоде проверяемых чисел: то, чего нет в источнике, не выдумывается.

Кейс VK Tech, хакатон «Лидеры цифровой трансформации 2026».

## Документация

- [ARCHITECTURE.md](docs/ARCHITECTURE.md) — пайплайн и границы слоёв
- [AUDIT.md](docs/AUDIT.md) — проверки и их покрытие
- [MODELS.md](docs/MODELS.md) — модели, лицензии, требования
- [DEMO.md](docs/DEMO.md) — сценарий живого прогона / видео-демо (≤ 7 минут) на неизвестном шаблоне
- [DEVELOPMENT.md](docs/DEVELOPMENT.md) — окружение, служебные скрипты, правила для кода
- [PLAN.md](docs/PLAN.md) — план разработки и чек-лист сдачи, [BACKLOG.md](docs/BACKLOG.md) — известные дефекты и что дальше

## Сетап

Требования: Python ≥ 3.12, Git LFS, LibreOffice (для рендера/PDF), доступ к OpenAI-совместимому inference API. Либо Docker — см. ниже.

```bash
git lfs install                                      # один раз на машине, ДО clone
git clone https://github.com/kireev-duty/deckforge.git && cd deckforge
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e .[dev]     # Windows
cp .env.example .env                                 # заполнить LLM_API_KEY и модели
.venv\Scripts\python.exe tools\check_env.py           # проверка ключей и LibreOffice
.venv\Scripts\python.exe -m pytest -q
```

Переменные окружения — см. [.env.example](.env.example): `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` / `VLM_MODEL` — текст и зрение (одна модель), `T2I_MODEL` (+ `T2I_BASE_URL` / `T2I_API_KEY`, если провайдер другой) — text-to-image; пустой `T2I_MODEL` отключает картинки, сервис работает без них. `.env` (с ключом) в репо не хранится; какой сервис, модели и параметры использованы — раздел «Инференс и модели» ниже.

Шаблоны датасета (`data/templates/*.pptx`), holdout-шаблон (`data/holdout/`) и ТЗ (`docs/tz/`) лежат в Git LFS. Если после clone файлы весят ~130 байт — это LFS-указатели: поставьте git-lfs и выполните `git lfs pull`.

Разметка образцов этих шаблонов (ответы VLM `template_tagger` по неоднозначным слайдам, привязаны к sha1 файла) лежит в репо в `data/archetypes/<шаблон>.json` — с ней `examples/output` воспроизводятся без вызова VLM на этапе парсинга. Для нового шаблона разметка — правилами; VLM-уточнение: `tools/classify_layouts.py --vlm "path/to/template.pptx"` (кэш в `out/archetypes/`, `--publish` — в `data/archetypes/`).

## Инференс и модели

Все колоды в репозитории (`examples/output/`) сгенерированы **через OpenRouter** (`https://openrouter.ai/api/v1`, OpenAI-совместимый API) этими моделями и с этими параметрами. [.env.example](.env.example) — это рабочая конфигурация проекта без ключа: `cp .env.example .env`, вписать `LLM_API_KEY` — и прогон воспроизводится. Ключ в репозитории не хранится.

| Роль | Сервис | Модель (id в API) | Веса / лицензия | Параметры вызова | Где задано |
|---|---|---|---|---|---|
| текст (`LLM_MODEL`) — бриф из шаблона, outline, промпты картинок | OpenRouter | `qwen/qwen3.8-27b-20260814` (Qwen3.8-27B) | 27B dense, Apache 2.0 | `template_brief`: temperature 0.5, max_tokens 2000; `outline_writer`: 0.4 / 6000; `image_prompter`: 0.6 / 400; `response_format=json_object`, thinking выключен | `skills/<name>/v<N>/skill.yaml` (действующая версия — `current` в `skills/registry.yaml`), `deckforge/llm/client.py` |
| зрение (`VLM_MODEL`) — разметка образцов шаблона, VLM-судья | OpenRouter | та же `qwen/qwen3.8-27b-20260814` | — | `template_tagger`: 0.1 / 1500; `audit_judge`: 0.0 / 1200; PNG слайдов как `image_url` (data-URL), thinking выключен | то же |
| text-to-image (`T2I_MODEL`) — иллюстрации | OpenRouter, тот же ключ (`POST /images`) | `black-forest-labs/flux.2-klein-4b` (FLUX.2 [klein] 4B) | 4B, Apache 2.0 | размер 1024×576, ≤ 4 картинки на колоду, кэш по sha1 промпта | `deckforge/content/images.py` |

Общие настройки (`.env`): `RUN_TIME_BUDGET_S=300` — бюджет времени на весь прогон, то есть на три варианта вместе (ТЗ и уточнение организаторов: все три ≤ 5 минут, независимо от латентности инференса; прежнее имя `DECK_TIME_BUDGET_S` тоже читается). Часы стартуют с начала прогона — разбор шаблона, бриф и outline входят. Колоды стратегий собираются параллельно (`RUN_MAX_PARALLEL_DECKS=3`, `1` — по очереди) под одним дедлайном: если до него остаётся мало времени, пропускаются сначала иллюстрации, затем VLM-судья (непроверенные слайды получают info-находку `C00`), а вызовы моделей после дедлайна не повторяются — вёрстка, детерминированный аудит, автофиксы и экспорт выполняются всегда. Факт — в manifest (`timings_s.deck_total` — через сколько от старта готова колода, `deck_build` — её собственная работа, `time_budget_s`) и в `run.json` (`timings_s.total`), при превышении — предупреждение. `DECK_MAX_PARALLEL_LLM=4` — параллельных вызовов внутри колоды (судья по слайдам, картинки); `LLM_MAX_CONCURRENCY` — общий лимит одновременных запросов на все колоды (0 — без лимита; для инференса с ограничением RPS). Замер на VK Tech с судьёй и картинками: три колоды готовы за 200 с от старта, из них 77 с — бриф и outline; на готовом outline 91 с против 213 с по очереди (ARCHITECTURE, «Бюджет времени на прогон»). Таймаут вызова 120 с, 2 ретрая (`LLMClient`). «Размышления» Qwen3.x выключаются в каждом вызове (`reasoning.enabled=false` для OpenRouter, `chat_template_kwargs.enable_thinking=false` для vLLM / инференса VK; переопределяется `LLM_NO_THINK_JSON`) — иначе они съедают `max_tokens` и втрое замедляют ответ. Переход на инференс VK — только `LLM_BASE_URL` и `LLM_API_KEY`, модель та же.

Провенанс каждой колоды — её `manifest.json`: `models` (`text`, `vision`, `image`, `base_url`), `skills` (версии промптов), `strategy`, `llm_calls` (модель, длительность, токены каждого вызова). Лицензии, HF-ссылки и системные требования — [MODELS.md](docs/MODELS.md).

## Запуск

```bash
# разбор шаблона: палитра с ролями, шрифты, шкала, сетка, образцы по архетипам
deckforge parse "path/to/template.pptx" [--json dna.json]
# генерация трёх вариантов по конфигу: контент (пакет / тема / сам шаблон) → outline (LLM) → иллюстрации (T2I)
#   → колоды по стратегиям → аудит → safe-автофиксы → PNG → VLM-судья → .pptx / .pdf + manifest.json
deckforge run --config configs/run.example.yaml            # или: python -m deckforge.cli run -c ... [--png] [--no-judge] [--no-images] [--outline готовый.json]
# на входе только шаблон: ни брифа, ни файлов — бриф выводит из шаблона скилл template_brief
deckforge run --config configs/template_only.yaml [--topic "тема одной строкой"]
# аудит любой колоды по шаблону (колода не меняется; --fix-plan — что чинилось бы)
deckforge audit deck.pptx -t template.pptx [--ir deck.ir.json] [--contextual] [--fix-plan]
# экспорт готовой колоды (своей или чужой): .html — один файл без LibreOffice; --pdf / --png — через LibreOffice
deckforge export deck.pptx [--html out.html] [--pdf] [--png] [--ir deck.ir.json]
# веб-интерфейс: шаблон → бриф → варианты → аудит с выбором фиксов → экспорт
streamlit run deckforge/ui/app.py
# HTTP API (Swagger на /docs): GET /templates, POST /generate → GET /jobs/{id} → .../decks/{strategy}/audit | fix | files/{name}
uvicorn deckforge.api.app:app --reload
```

Результат прогона (`out/run/<name>/` или `out/ui/runs/<время>/`): `outline.json`, `dna.json`, `brief.md` (если бриф выведен из темы или из шаблона), на каждую стратегию — `<strategy>.pptx`, `.pdf`, `.html`, `.ir.json`, `.audit.json`, `.manifest.json` (провенанс: версии скиллов/моделей/стратегии, план, образцы, автофиксы, картинки, тайминги) и PNG в `<strategy>/`; сгенерированные иллюстрации — в `images/` (кэш по sha1 входов, повторный прогон их не платит); сводка — `compare.md`, `run.json`.

### UI

Один экран, пять шагов: шаблон (датасет или свой .pptx) → контент (только шаблон / тема одной строкой / бриф и файлы .md/.txt/.docx/.pdf, .json/.csv/.xlsx) → три варианта с превью → аудит с выбором фиксов и подсветкой находок на слайде → скачивание .pptx / .pdf / .html / json. В режимах «только шаблон» и «тема» выведенный бриф показывается рядом с вариантами и лежит в прогоне как `brief.md`.

![Шаблон и параметры: палитра по использованию, шрифты, шкала, образцы по архетипам, контактный лист](docs/img/ui_1_setup.png)

![Варианты: сводка по стратегиям и PNG-превью каждой колоды](docs/img/ui_2_variants.png)

![Аудит: метрики, фиксы по выбору (безопасные и «по выбору»), подсветка находок на слайде](docs/img/ui_3_audit.png)

HTML-экспорт — собственный рендер по XML готовой колоды (`deckforge/export/html.py`), а не растр: один самодостаточный файл, текст остаётся текстом, диаграммы — SVG, таблицы — `<table>`, картинки вшиты (WebP), фон/декор мастера и лейаута на месте. Открывается с `file://` в Chrome, Firefox, Яндекс и WebKit (движок Safari); ←/→ — листать, F — режим показа, печать — слайд на страницу. Встроенные шрифты датасета (Play) хранятся в .pptx в сжатом EOT и в HTML не вшиваются — при наличии сети подключаются с Google Fonts (OFL), офлайн — Arial. Примеры: `examples/output/<template>/<strategy>.html`.

## Docker

```bash
cp .env.example .env                 # ключи и модели; без ключей работают parse и run --outline --no-judge --no-images
docker compose build                 # python 3.12 + LibreOffice Impress, ≈1,6 ГБ
docker compose up                    # API http://localhost:8000/docs и UI http://localhost:8501
docker compose run --rm cli run -c configs/run.example.yaml --png     # прогон конфигом → ./out/run/vk_tech
docker compose run --rm cli parse "data/templates/VK Tech шаблон.pptx"
```

`./data` (шаблоны из Git LFS и разметка их образцов `data/archetypes` — ответы VLM, с которыми собраны примеры; без API чистый clone размечает шаблоны датасета так же), `./examples`, `./out` (в т.ч. рабочий кэш разметки `out/archetypes`) и `./configs` монтируются с хоста. Шрифтов Play/Montserrat в образе нет — LibreOffice подставляет DejaVu/Liberation, поэтому PDF/PNG из контейнера чуть отличаются от локальных; .pptx и .html от этого не зависят.

Инструменты разработчика: `tools/pptx_xray.py` (структура шаблона), `tools/render_deck.py` (PNG-превью и PDF), `tools/build_variants.py` (три стратегии на одном готовом outline без LLM: по умолчанию `examples/content_pack/outline.json` × шаблон → `out/variants/`), `tools/browser_check.py` (HTML-колоды и UI в Chromium / Firefox / WebKit через Playwright: `pip install playwright && python -m playwright install chromium firefox webkit` → `out/browsers/report.md`). Примеры: `examples/output/<template>/` — 4 шаблона × 3 стратегии (.pptx/.pdf/.html с судьёй и картинками). Контент-пакет датасета — это только три шаблона, поэтому на входе примеров нет ничего, кроме .pptx: бриф выведен из шаблона VK Tech (`examples/output/vk_tech/brief.md`, `content_source: template`), его `outline.json` идёт на остальные три шаблона — один контент × 4 шаблона (`configs/final/*.yaml`; пути в manifest относительные). `examples/content_pack/` — синтетический пакет «Пульс команды»: пример ступени «бриф и файлы» и фикстура тестов (в нём есть ряды, таблица и KPI), к датасету не относится. Презентация для защиты — `docs/deckforge_pitch.pptx`.

## Стратегии вёрстки

`strategies/{executive,narrative,visual}.yaml` — три варианта одной и той же презентации. Стратегия не меняет стиль шаблона, а задаёт плотность (число слайдов, буллетов, слов) и способ показа данных (таблица / диаграмма / крупные цифры). Для кого какой: **executive** — руководителю на 5 минут, читается без докладчика; **narrative** — доклад со сцены, обучение; **visual** — публичная защита, конференция (`audience_hint` в YAML, показывается в UI и `compare.md`). Подробнее — [ARCHITECTURE.md](docs/ARCHITECTURE.md#три-стратегии-ось-различий).

## Ограничения

- Только модели с открытыми весами (Apache 2.0 / MIT) до 35B; text-to-image до 20B.
- Десктопные браузеры. HTML-экспорт проверен в Chrome, Firefox 156 и Яндекс.Браузере (Chromium 150) — слайды рендерятся пиксель в пиксель; Streamlit UI — в тех же трёх браузерах. Safari — через его движок WebKit (Playwright, `tools/browser_check.py`): 12 HTML-колод без ошибок JS, расхождение с Chromium ≤ 1 % яркости; UI проходит весь сценарий (шаблон → генерация → вкладки вариантов). Живой Safari на macOS не проверялся (нет Mac); специфичных для WebKit возможностей экспорт не использует (SVG, `@font-face`, flex, `clip-path`).
- Целевой объём 10–15 слайдов или заданный пользователем (`target_slides` сдвигает диапазоны стратегий: executive 10–11, narrative 12–15, visual 10–12 при объёме 12; стратегия может дать меньше, если контента мало — например 5). Три варианта собираются параллельно и укладываются в 5 минут вместе независимо от латентности инференса — бюджет прогона `RUN_TIME_BUDGET_S`, см. «Инференс и модели».
- Демо-стенд ([Streamlit Community Cloud](https://deckforge.streamlit.app), бесплатный): генерации всех посетителей идут по одной, остальные видят «В очереди»; VLM-судья по умолчанию выключен (+1–2 мин на колоду, включается в боковой панели); иллюстрации выключены. Приложение засыпает без посетителей — на заставке нужно нажать «Yes, get this app back up», запуск занимает 1–2 минуты. Шрифта Play (шаблоны VK) на стенде нет, PNG-превью и PDF рисуются с подменой; .pptx и .html от этого не зависят. Если ключ LLM стенда недоступен, остаётся режим «Готовый outline» без LLM. Устройство стенда — [DEVELOPMENT.md](docs/DEVELOPMENT.md#демо-стенд).
- Streamlit UI выполняет прогон in-process: новый прогон, запущенный поверх идущего, обрывает предыдущий (папка прогона остаётся без `run.json` и части колод) — дождитесь завершения; параллельные задания — через API (`POST /generate`, job'ы в пуле).
- Схемы (замена SmartArt): шаги процесса рисуются нативными автофигурами — ряд шевронов с номерами и подписями, одной группой, в цветах палитры и шрифте шаблона, кегли — из его шкалы (`render/diagrams.py`). visual рисует схему всегда, narrative — если в шаблоне нет своего process-образца, executive оставляет образец шаблона или нумерованный список (`process_form` в стратегии). Это редактируемые фигуры, а не объект SmartArt (`dgm`): в PowerPoint их двигают и правят как обычные фигуры, «Преобразовать в SmartArt» к ним не применяется. Схемы cycle / pyramid пока нет — только процесс, 2–6 шагов.
- Пиктограммы: иконки берутся только из образцов самого шаблона (`SlotKind.ICON`) — на шаблоне без иконок их не будет; собственная библиотека иконок — в бэклоге.
- В `.pptx` объекты нативные (текст, фигуры, chart, table, picture) — открытие, сохранение и редактируемость проверены в PowerPoint (Microsoft 365, `tools/powerpoint_check.py`); в `.html` диаграммы рисуются SVG по данным chart, таблицы — HTML-таблицами.
