# deckforge — цифровой дизайнер презентаций

Сервис читает произвольный .pptx-шаблон как набор правил (дизайн-система + слайды-образцы) и собирает по нему новую презентацию из краткого брифа и контент-пакета: структура → тексты → вёрстка нативными объектами → аудит → экспорт в .pptx / .pdf / .html. Три стратегии вёрстки на один и тот же контент.

Кейс VK Tech, хакатон «Лидеры цифровой трансформации 2026».

## Документация

- [ARCHITECTURE.md](docs/ARCHITECTURE.md) — пайплайн и границы слоёв
- [AUDIT.md](docs/AUDIT.md) — проверки и их покрытие
- [MODELS.md](docs/MODELS.md) — модели, лицензии, требования
- [DEMO.md](docs/DEMO.md) — сценарий живого прогона / видео-демо (≤ 7 минут) на неизвестном шаблоне
- [PLAN.md](docs/PLAN.md) — план разработки и статус по дням, [BACKLOG.md](docs/BACKLOG.md) — известные дефекты и что дальше

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

Все колоды в репозитории (`examples/output/`, `examples/pitch/`) сгенерированы **через OpenRouter** (`https://openrouter.ai/api/v1`, OpenAI-совместимый API) этими моделями и с этими параметрами. [.env.example](.env.example) — это рабочая конфигурация проекта без ключа: `cp .env.example .env`, вписать `LLM_API_KEY` — и прогон воспроизводится. Ключ в репозитории не хранится.

| Роль | Сервис | Модель (id в API) | Веса / лицензия | Параметры вызова | Где задано |
|---|---|---|---|---|---|
| текст (`LLM_MODEL`) — outline, промпты картинок | OpenRouter | `qwen/qwen3.8-27b-20260814` (Qwen3.8-27B) | 27B dense, Apache 2.0 | `outline_writer`: temperature 0.4, max_tokens 6000; `image_prompter`: 0.6 / 400; `response_format=json_object`, thinking выключен | `skills/<name>/v1/skill.yaml`, `deckforge/llm/client.py` |
| зрение (`VLM_MODEL`) — разметка образцов шаблона, VLM-судья | OpenRouter | та же `qwen/qwen3.8-27b-20260814` | — | `template_tagger`: 0.1 / 1500; `audit_judge`: 0.0 / 1200; PNG слайдов как `image_url` (data-URL), thinking выключен | то же |
| text-to-image (`T2I_MODEL`) — иллюстрации | OpenRouter, тот же ключ (`POST /images`) | `black-forest-labs/flux.2-klein-4b` (FLUX.2 [klein] 4B) | 4B, Apache 2.0 | размер 1024×576, ≤ 4 картинки на колоду, кэш по sha1 промпта | `deckforge/content/images.py` |

Общие настройки (`.env`): `DECK_TIME_BUDGET_S=300` — бюджет времени на колоду, `DECK_MAX_PARALLEL_LLM=4` — параллельных вызовов (судья по слайдам, картинки). Таймаут вызова 120 с, 2 ретрая (`LLMClient`). «Размышления» Qwen3.x выключаются в каждом вызове (`reasoning.enabled=false` для OpenRouter, `chat_template_kwargs.enable_thinking=false` для vLLM / инференса VK; переопределяется `LLM_NO_THINK_JSON`) — иначе они съедают `max_tokens` и втрое замедляют ответ. Переход на инференс VK — только `LLM_BASE_URL` и `LLM_API_KEY`, модель та же.

Провенанс каждой колоды — её `manifest.json`: `models` (`text`, `vision`, `image`, `base_url`), `skills` (версии промптов), `strategy`, `llm_calls` (модель, длительность, токены каждого вызова). Лицензии, HF-ссылки и системные требования — [MODELS.md](docs/MODELS.md).

## Запуск

```bash
# разбор шаблона: палитра с ролями, шрифты, шкала, сетка, образцы по архетипам
deckforge parse "path/to/template.pptx" [--json dna.json]
# генерация трёх вариантов по конфигу: бриф + контент-пакет → outline (LLM) → иллюстрации (T2I) → колоды по стратегиям
#   → аудит → safe-автофиксы → PNG → VLM-судья → .pptx / .pdf + manifest.json
deckforge run --config configs/run.example.yaml            # или: python -m deckforge.cli run -c ... [--png] [--no-judge] [--no-images] [--outline готовый.json]
# аудит любой колоды по шаблону (колода не меняется; --fix-plan — что чинилось бы)
deckforge audit deck.pptx -t template.pptx [--ir deck.ir.json] [--contextual] [--fix-plan]
# экспорт готовой колоды (своей или чужой): .html — один файл без LibreOffice; --pdf / --png — через LibreOffice
deckforge export deck.pptx [--html out.html] [--pdf] [--png] [--ir deck.ir.json]
# веб-интерфейс: шаблон → бриф → варианты → аудит с выбором фиксов → экспорт
streamlit run deckforge/ui/app.py
# HTTP API (Swagger на /docs): GET /templates, POST /generate → GET /jobs/{id} → .../decks/{strategy}/audit | fix | files/{name}
uvicorn deckforge.api.app:app --reload
```

Результат прогона (`out/run/<name>/` или `out/ui/runs/<время>/`): `outline.json`, `dna.json`, на каждую стратегию — `<strategy>.pptx`, `.pdf`, `.html`, `.ir.json`, `.audit.json`, `.manifest.json` (провенанс: версии скиллов/моделей/стратегии, план, образцы, автофиксы, картинки, тайминги) и PNG в `<strategy>/`; сгенерированные иллюстрации — в `images/` (кэш по sha1 входов, повторный прогон их не платит); сводка — `compare.md`, `run.json`.

### UI

Один экран, пять шагов: шаблон (датасет или свой .pptx) → бриф и файлы контент-пакета → три варианта с превью → аудит с выбором фиксов и подсветкой находок на слайде → скачивание .pptx / .pdf / .html / json.

![Шаблон и параметры: палитра по использованию, шрифты, шкала, образцы по архетипам, контактный лист](docs/img/ui_1_setup.png)

![Варианты: сводка по стратегиям и PNG-превью каждой колоды](docs/img/ui_2_variants.png)

![Аудит: метрики, фиксы по выбору (безопасные и «по выбору»), подсветка находок на слайде](docs/img/ui_3_audit.png)

HTML-экспорт — собственный рендер по XML готовой колоды (`deckforge/export/html.py`), а не растр: один самодостаточный файл, текст остаётся текстом, диаграммы — SVG, таблицы — `<table>`, картинки вшиты (WebP), фон/декор мастера и лейаута на месте. Открывается с `file://` в Chrome, Firefox, Яндекс; ←/→ — листать, F — режим показа, печать — слайд на страницу. Встроенные шрифты датасета (Play) хранятся в .pptx в сжатом EOT и в HTML не вшиваются — при наличии сети подключаются с Google Fonts (OFL), офлайн — Arial. Примеры: `examples/output/<template>/<strategy>.html`.

## Docker

```bash
cp .env.example .env                 # ключи и модели; без ключей работают parse и run --outline --no-judge --no-images
docker compose build                 # python 3.12 + LibreOffice Impress, ≈1,6 ГБ
docker compose up                    # API http://localhost:8000/docs и UI http://localhost:8501
docker compose run --rm cli run -c configs/run.example.yaml --png     # прогон конфигом → ./out/run/vk_tech
docker compose run --rm cli parse "data/templates/VK Tech шаблон.pptx"
```

`./data` (шаблоны из Git LFS и разметка их образцов `data/archetypes` — ответы VLM, с которыми собраны примеры; без API чистый clone размечает шаблоны датасета так же), `./examples`, `./out` (в т.ч. рабочий кэш разметки `out/archetypes`) и `./configs` монтируются с хоста. Шрифтов Play/Montserrat в образе нет — LibreOffice подставляет DejaVu/Liberation, поэтому PDF/PNG из контейнера чуть отличаются от локальных; .pptx и .html от этого не зависят.

Инструменты разработчика: `tools/pptx_xray.py` (структура шаблона), `tools/render_deck.py` (PNG-превью и PDF), `tools/build_variants.py` (три стратегии на одном контенте: `examples/content_pack/` × шаблон → `out/variants/`). Примеры: `examples/output/<template>/` — 4 шаблона × 3 стратегии (.pptx/.pdf/.html с судьёй и картинками, один outline на все шаблоны — `configs/final/*.yaml`; перегенерированы финальным кодом, пути в manifest относительные), `examples/pitch/` — бриф питча для защиты (`configs/pitch.yaml`).

## Стратегии вёрстки

`strategies/{executive,narrative,visual}.yaml` — три варианта одной и той же презентации. Стратегия не меняет стиль шаблона, а задаёт плотность (число слайдов, буллетов, слов) и способ показа данных (таблица / диаграмма / крупные цифры). Подробнее — [ARCHITECTURE.md](docs/ARCHITECTURE.md#три-стратегии-ось-различий).

## Ограничения

- Только модели с открытыми весами (Apache 2.0 / MIT) до 35B; text-to-image до 20B.
- Десктопные браузеры. HTML-экспорт проверен в Chrome, Firefox 156 и Яндекс.Браузере (Chromium 150) — слайды рендерятся пиксель в пиксель; Safari не проверялся (нет macOS), специфичных для WebKit возможностей экспорт не использует (SVG, `@font-face`, flex). Streamlit UI проверен в тех же трёх браузерах.
- Целевой объём 10–15 слайдов (стратегия может дать меньше, если контента мало — например 5); генерация колоды ≤ 5 минут.
- Пиктограммы и схемы: иконки берутся только из образцов самого шаблона (`SlotKind.ICON`) — на шаблоне без иконок их не будет; SmartArt-подобные схемы (process / cycle / pyramid) не собираются из автофигур — `process` верстается только на process-образцах шаблона, без них контент уходит в карточки или список. Собственная библиотека иконок и генерация схем — в бэклоге.
- В `.pptx` объекты нативные (текст, фигуры, chart, table, picture); в `.html` диаграммы рисуются SVG по данным chart, таблицы — HTML-таблицами.
