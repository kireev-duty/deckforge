# MODELS

Все модели — открытые веса, лицензия Apache 2.0 / MIT, ≤ 35B (text-to-image ≤ 20B), как требует ТЗ.
Доступ — через OpenAI-совместимый endpoint; смена провайдера или переход на инференс VK = смена `.env`, не кода.

| Роль | Модель | Размер | Лицензия | Модальности | Где используется | HuggingFace |
|---|---|---|---|---|---|---|
| text + vision (`LLM_MODEL`, `VLM_MODEL`) | Qwen3.8-27B | 27B dense | Apache 2.0 | текст, изображения, видео → текст; контекст 262k | `outline_writer`, `slide_filler`, `image_prompter`, `template_tagger`, `audit_judge` | https://huggingface.co/Qwen/Qwen3.8-27B |
| image (`T2I_MODEL`) | FLUX.2 [klein] 4B | 4B | Apache 2.0 | текст → изображение | иллюстрации в слайдах (`image_prompter` → картинка) | https://huggingface.co/black-forest-labs/FLUX.2-klein-4B |

Почему одна модель на текст и зрение: Qwen3.8-27B — та же модель, которую VK предоставляет командам топ-10 на своём инференсе (ТЗ, раздел 3), и она нативно мультимодальна. Один и тот же промпт-стек работает на отборе (через OpenRouter) и в финале (инференс VK) без переписывания.

Почему FLUX.2 [klein] 4B: открытые веса Apache 2.0 (9B-версия — non-commercial, не подходит), укладывается в лимит ≤20B, доступна на том же OpenRouter тем же ключом (endpoint `POST /api/v1/images`, ≈$0.014 за картинку 1024×576), локально — ~13 GB VRAM.

Проверено 15.09.2026: `qwen/qwen3.8-27b-20260814` и `black-forest-labs/flux.2-klein-4b` в каталоге OpenRouter, оба отвечают.

Запасные варианты: Qwen3-32B + Qwen2.5-VL-32B-Instruct (Apache 2.0) для текста/зрения; FLUX.1-schnell (12B, Apache 2.0, Together AI `/v1/images/generations`) или Qwen-Image (20B, Apache 2.0) для картинок. Без T2I-провайдера сервис работает полностью — слайды собираются с иконками и нативной графикой, без иллюстраций.

## Системные требования

- Через API: любой ПК; стоимость колоды — центы.
- Локально (vLLM): Qwen3.8-27B в 4-bit — ≥ 24 GB VRAM; FLUX.2 [klein] 4B — ≥ 13 GB (fp8/GGUF — меньше).
- Рендер: LibreOffice ≥ 7.x, шрифты шаблона (извлекаются из `ppt/fonts/` при парсинге).

## Не-ML компоненты

- Иконки: Tabler Icons (MIT) — SVG, перекрашиваются в акцентный цвет палитры, вставляются как PNG.
- Метрики шрифтов для оценки вместимости текста: Pillow `ImageFont` по TTF из шаблона; при отсутствии — Arial как приближение.
