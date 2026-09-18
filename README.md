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

Переменные окружения — см. [.env.example](.env.example): `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` / `VLM_MODEL` — текст и зрение (одна модель), `T2I_MODEL` (+ `T2I_BASE_URL` / `T2I_API_KEY`, если провайдер другой) — text-to-image; пустой `T2I_MODEL` отключает картинки, сервис работает без них. `.env` в репо не хранится — переносить между машинами вручную.

Шаблоны датасета (`data/templates/*.pptx`), holdout-шаблон (`data/holdout/`) и ТЗ (`docs/tz/`) лежат в Git LFS. Если после clone файлы весят ~130 байт — это LFS-указатели: поставьте git-lfs и выполните `git lfs pull`.

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

HTML-экспорт — собственный рендер по XML готовой колоды (`deckforge/export/html.py`), а не растр: один самодостаточный файл, текст остаётся текстом, диаграммы — SVG, таблицы — `<table>`, картинки вшиты (WebP), фон/декор мастера и лейаута на месте. Открывается с `file://` в Chrome, Firefox, Яндекс; ←/→ — листать, F — режим показа, печать — слайд на страницу. Встроенные шрифты датасета (Play) хранятся в .pptx в сжатом EOT и в HTML не вшиваются — при наличии сети подключаются с Google Fonts (OFL), офлайн — Arial. Примеры: `examples/output/<template>/<strategy>.html`.

## Docker

```bash
cp .env.example .env                 # ключи и модели; без ключей работают parse и run --outline --no-judge --no-images
docker compose build                 # python 3.12 + LibreOffice Impress, ≈1,6 ГБ
docker compose up                    # API http://localhost:8000/docs и UI http://localhost:8501
docker compose run --rm cli run -c configs/run.example.yaml --png     # прогон конфигом → ./out/run/vk_tech
docker compose run --rm cli parse "data/templates/VK Tech шаблон.pptx"
```

`./data` (шаблоны из Git LFS), `./examples`, `./out` (в т.ч. кэш VLM-разметки `out/archetypes`) и `./configs` монтируются с хоста. Шрифтов Play/Montserrat в образе нет — LibreOffice подставляет DejaVu/Liberation, поэтому PDF/PNG из контейнера чуть отличаются от локальных; .pptx и .html от этого не зависят.

Инструменты разработчика: `tools/pptx_xray.py` (структура шаблона), `tools/render_deck.py` (PNG-превью и PDF), `tools/build_variants.py` (три стратегии на одном контенте: `examples/content_pack/` × шаблон → `out/variants/`). Примеры: `examples/output/<template>/` — 4 шаблона × 3 стратегии (день 11: .pptx/.pdf с судьёй и картинками; .html добавлен командой `export` поверх тех же колод), `examples/pitch/` — бриф питча для защиты (`configs/pitch.yaml`).

## Стратегии вёрстки

`strategies/{executive,narrative,visual}.yaml` — три варианта одной и той же презентации. Стратегия не меняет стиль шаблона, а задаёт плотность (число слайдов, буллетов, слов) и способ показа данных (таблица / диаграмма / крупные цифры). Подробнее — [ARCHITECTURE.md](docs/ARCHITECTURE.md#три-стратегии-ось-различий).

## Ограничения

- Только модели с открытыми весами (Apache 2.0 / MIT) до 35B; text-to-image до 20B.
- Десктопные браузеры (Chrome, Firefox, Safari, Яндекс).
- Целевой объём 10–15 слайдов (стратегия может дать меньше, если контента мало — например 5); генерация колоды ≤ 5 минут.
