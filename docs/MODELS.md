# MODELS

Все модели — открытые веса, лицензия Apache 2.0 / MIT, ≤ 35B (text-to-image ≤ 20B), как требует ТЗ.
Доступ — через OpenAI-совместимый endpoint; смена провайдера или переход на инференс VK = смена `.env`, не кода.

| Роль | Модель | Размер | Лицензия | Модальности | Где используется | HuggingFace |
|---|---|---|---|---|---|---|
| text + vision (`LLM_MODEL`, `VLM_MODEL`) | Qwen3.8-27B | 27B dense | Apache 2.0 | текст, изображения, видео → текст; контекст 262k | `outline_writer`, `image_prompter`, `template_tagger`, `audit_judge` (`slide_filler` — скилл есть, в пайплайн не подключён) | https://huggingface.co/Qwen/Qwen3.8-27B |
| image (`T2I_MODEL`) | FLUX.2 [klein] 4B | 4B | Apache 2.0 | текст → изображение | иллюстрации в слайдах (`image_prompter` → картинка) | https://huggingface.co/black-forest-labs/FLUX.2-klein-4B |

Почему одна модель на текст и зрение: Qwen3.8-27B — та же модель, которую VK предоставляет командам топ-10 на своём инференсе (ТЗ, раздел 3), и она нативно мультимодальна. Один и тот же промпт-стек работает на отборе (через OpenRouter) и в финале (инференс VK) без переписывания.

Почему FLUX.2 [klein] 4B: открытые веса Apache 2.0 (9B-версия — non-commercial, не подходит), укладывается в лимит ≤20B, доступна на том же OpenRouter тем же ключом (endpoint `POST /api/v1/images`, ≈$0.014 за картинку 1024×576), локально — ~13 GB VRAM.

Проверено: `qwen/qwen3.8-27b-20260814` и `black-forest-labs/flux.2-klein-4b` в каталоге OpenRouter, оба отвечают.

Запасные варианты: Qwen3-32B + Qwen2.5-VL-32B-Instruct (Apache 2.0) для текста/зрения; FLUX.1-schnell (12B, Apache 2.0, Together AI `/v1/images/generations`) или Qwen-Image (20B, Apache 2.0) для картинок. Без T2I-провайдера сервис работает полностью — слайды собираются с иконками и нативной графикой, без иллюстраций.

## Сервис и параметры вызовов

Все колоды в `examples/` сделаны через **OpenRouter** (`LLM_BASE_URL=https://openrouter.ai/api/v1`) — текст, зрение и картинки одним ключом; `.env.example` в репозитории — рабочая конфигурация без ключа. Клиент — `deckforge/llm/client.py` (`LLMClient`, OpenAI SDK): таймаут 120 с, 2 ретрая, `DECK_MAX_PARALLEL_LLM=4` параллельных вызова, `DECK_TIME_BUDGET_S=300` на колоду — вызовы получают дедлайн колоды: таймаут запроса урезается до остатка, после дедлайна ретраев нет, судья и картинки при нехватке времени пропускаются (`pipeline/run.build_deck`), так что 5 минут выдерживаются при любой латентности инференса. Промпты и параметры — только в `skills/<name>/v<N>/skill.yaml`, в коде их нет; версия каждого скилла попадает в `manifest.json` колоды вместе с `models` и журналом `llm_calls`.

| Скилл | Роль модели | temperature | max_tokens | Ответ | Вход |
|---|---|---|---|---|---|
| `outline_writer` v1 | text | 0.4 | 6000 | JSON по `schema.json` (`response_format=json_object`, валидация своя) | бриф + контент-пакет + архетипы шаблона |
| `image_prompter` v1 | text | 0.6 | 400 | JSON | заголовок, текст слайда, палитра |
| `template_tagger` v1 | vision | 0.1 | 1500 | JSON | PNG слайда-образца |
| `audit_judge` v1 | vision | 0.0 | 1200 | JSON (11 вопросов) | PNG готового слайда + его текст |
| `slide_filler` v1 | text | 0.3 | 2000 | JSON | (в пайплайн не подключён) |
| text-to-image | image | — | — | JPEG/PNG b64 | промпт от `image_prompter`, размер 1024×576 |

Qwen3.x по умолчанию «думает» — это съедает `max_tokens` и втрое замедляет ответ, поэтому в каждом вызове без `reasoning: true` в `skill.yaml` клиент передаёт `extra_body = {"reasoning": {"enabled": false}, "chat_template_kwargs": {"enable_thinking": false}}` (первое понимает OpenRouter, второе — vLLM и инференс VK). Для другого провайдера набор переопределяется переменной `LLM_NO_THINK_JSON`.

Переход на инференс VK (топ-10): в `.env` поменять `LLM_BASE_URL` и `LLM_API_KEY`; модель та же. T2I-провайдер задаётся отдельно (`T2I_BASE_URL`/`T2I_API_KEY`, OpenAI-совместимый `/images/generations`), пустые значения = тот же провайдер и ключ, что у текста.

## Системные требования

- Через API: любой ПК; стоимость колоды — центы.
- Локально (vLLM): Qwen3.8-27B в 4-bit — ≥ 24 GB VRAM; FLUX.2 [klein] 4B — ≥ 13 GB (fp8/GGUF — меньше).
- Рендер: LibreOffice ≥ 7.x, шрифты шаблона (извлекаются из `ppt/fonts/` при парсинге).

## Не-ML компоненты

- Иконки: Tabler Icons (MIT) — SVG, перекрашиваются в акцентный цвет палитры, вставляются как PNG.
- Метрики шрифтов для оценки вместимости текста: Pillow `ImageFont` по TTF из шаблона; при отсутствии — Arial как приближение.
