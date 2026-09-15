# deckforge — цифровой дизайнер презентаций

Сервис читает произвольный .pptx-шаблон как набор правил (дизайн-система + слайды-образцы) и собирает по нему новую презентацию из краткого брифа и контент-пакета: структура → тексты → вёрстка нативными объектами → аудит → экспорт в .pptx / .pdf / .html. Три стратегии вёрстки на один и тот же контент.

Кейс VK Tech, хакатон «Лидеры цифровой трансформации 2026».

## Документация

- [ARCHITECTURE.md](docs/ARCHITECTURE.md) — пайплайн и границы слоёв
- [AUDIT.md](docs/AUDIT.md) — проверки и их покрытие
- [MODELS.md](docs/MODELS.md) — модели, лицензии, требования
- [PLAN.md](docs/PLAN.md) — план разработки

## Сетап

Требования: Python ≥ 3.12, LibreOffice (для рендера/PDF), доступ к OpenAI-совместимому inference API.

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e .[dev]     # Windows
cp .env.example .env                                 # заполнить LLM_API_KEY и модели
.venv\Scripts\python.exe -m pytest -q
```

Переменные окружения — см. [.env.example](.env.example).

## Запуск

```bash
# разбор шаблона
deckforge parse "path/to/template.pptx"
# генерация трёх вариантов по конфигу
deckforge run --config configs/run.example.yaml
# веб-интерфейс
streamlit run deckforge/ui/app.py
```

Инструменты разработчика: `tools/pptx_xray.py` (структура шаблона), `tools/render_deck.py` (PNG-превью и PDF), `tools/build_variants.py` (три стратегии на одном контенте: `examples/content_pack/` × шаблон → `out/variants/`).

## Стратегии вёрстки

`strategies/{executive,narrative,visual}.yaml` — три варианта одной и той же презентации. Стратегия не меняет стиль шаблона, а задаёт плотность (число слайдов, буллетов, слов) и способ показа данных (таблица / диаграмма / крупные цифры). Подробнее — [ARCHITECTURE.md](docs/ARCHITECTURE.md#три-стратегии-ось-различий).

## Ограничения

- Только модели с открытыми весами (Apache 2.0 / MIT) до 35B; text-to-image до 20B.
- Десктопные браузеры (Chrome, Firefox, Safari, Яндекс).
- Целевой объём 10–15 слайдов, генерация колоды ≤ 5 минут.
