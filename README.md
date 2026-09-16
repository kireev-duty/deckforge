# deckforge — цифровой дизайнер презентаций

Сервис читает произвольный .pptx-шаблон как набор правил (дизайн-система + слайды-образцы) и собирает по нему новую презентацию из краткого брифа и контент-пакета: структура → тексты → вёрстка нативными объектами → аудит → экспорт в .pptx / .pdf / .html. Три стратегии вёрстки на один и тот же контент.

Кейс VK Tech, хакатон «Лидеры цифровой трансформации 2026».

## Документация

- [ARCHITECTURE.md](docs/ARCHITECTURE.md) — пайплайн и границы слоёв
- [AUDIT.md](docs/AUDIT.md) — проверки и их покрытие
- [MODELS.md](docs/MODELS.md) — модели, лицензии, требования
- [PLAN.md](docs/PLAN.md) — план разработки, [BACKLOG.md](docs/BACKLOG.md) — известные дефекты и что дальше

## Сетап

Требования: Python ≥ 3.12, Git LFS, LibreOffice (для рендера/PDF), доступ к OpenAI-совместимому inference API.

```bash
git lfs install                                      # один раз на машине, ДО clone
git clone https://github.com/kireev-duty/deckforge.git && cd deckforge
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e .[dev]     # Windows
cp .env.example .env                                 # заполнить LLM_API_KEY и модели
.venv\Scripts\python.exe tools\check_env.py           # проверка ключей и LibreOffice
.venv\Scripts\python.exe -m pytest -q
```

Переменные окружения — см. [.env.example](.env.example). `.env` в репо не хранится — переносить между машинами вручную.

Шаблоны датасета (`data/templates/*.pptx`), holdout-шаблон (`data/holdout/`) и ТЗ (`docs/tz/`) лежат в Git LFS. Если после clone файлы весят ~130 байт — это LFS-указатели: поставьте git-lfs и выполните `git lfs pull`.

## Запуск

```bash
# разбор шаблона: палитра с ролями, шрифты, шкала, сетка, образцы по архетипам
deckforge parse "path/to/template.pptx" [--json dna.json]
# генерация трёх вариантов по конфигу: бриф + контент-пакет → outline (LLM) → колоды по стратегиям
#   → аудит → safe-автофиксы → PNG → VLM-судья → .pptx / .pdf + manifest.json
deckforge run --config configs/run.example.yaml            # или: python -m deckforge.cli run -c ... [--png] [--no-judge] [--outline готовый.json]
# аудит любой колоды по шаблону (колода не меняется; --fix-plan — что чинилось бы)
deckforge audit deck.pptx -t template.pptx [--ir deck.ir.json] [--contextual] [--fix-plan]
# веб-интерфейс: шаблон → бриф → варианты → аудит с выбором фиксов → экспорт
streamlit run deckforge/ui/app.py
# HTTP API (Swagger на /docs): GET /templates, POST /generate → GET /jobs/{id} → .../decks/{strategy}/audit | fix | files/{name}
uvicorn deckforge.api.app:app --reload
```

Результат прогона (`out/run/<name>/` или `out/ui/runs/<время>/`): `outline.json`, `dna.json`, на каждую стратегию — `<strategy>.pptx`, `.pdf`, `.ir.json`, `.audit.json`, `.manifest.json` (провенанс: версии скиллов/моделей/стратегии, план, образцы, автофиксы, тайминги) и PNG в `<strategy>/`; сводка — `compare.md`, `run.json`. HTML-экспорт и генерация картинок — в работе (`run.json → not_implemented`).

Инструменты разработчика: `tools/pptx_xray.py` (структура шаблона), `tools/render_deck.py` (PNG-превью и PDF), `tools/build_variants.py` (три стратегии на одном контенте: `examples/content_pack/` × шаблон → `out/variants/`).

## Стратегии вёрстки

`strategies/{executive,narrative,visual}.yaml` — три варианта одной и той же презентации. Стратегия не меняет стиль шаблона, а задаёт плотность (число слайдов, буллетов, слов) и способ показа данных (таблица / диаграмма / крупные цифры). Подробнее — [ARCHITECTURE.md](docs/ARCHITECTURE.md#три-стратегии-ось-различий).

## Ограничения

- Только модели с открытыми весами (Apache 2.0 / MIT) до 35B; text-to-image до 20B.
- Десктопные браузеры (Chrome, Firefox, Safari, Яндекс).
- Целевой объём 10–15 слайдов, генерация колоды ≤ 5 минут.
