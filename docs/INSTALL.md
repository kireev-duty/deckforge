# Установка и запуск на Linux

Пошаговая инструкция: от чистой машины до трёх вариантов презентации, собранных конфиг-файлом.
Проверена копипастой на чистой **Ubuntu 24.04 LTS** (x86_64, обычный пользователь с `sudo`).

Другие дистрибутивы: нужен Python ≥ 3.12. В Debian 12 и Ubuntu 22.04 системный Python старше (3.11 и 3.10) —
там проще собрать [Docker-образ](#9-вариант-docker), всё остальное в нём уже есть.

## Коротко

Для тех, кто уже знает, что делает. Подробности и ожидаемый вывод — в разделах ниже.

```bash
sudo apt update
sudo apt install -y --no-install-recommends git git-lfs ca-certificates curl \
    python3.12 python3.12-venv libreoffice-impress libreoffice-calc fonts-liberation fonts-dejavu-core fontconfig
git lfs install
git clone --branch v0.1.4 https://github.com/kireev-duty/deckforge.git && cd deckforge
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env && nano .env                      # вписать LLM_API_KEY
python tools/check_env.py                              # всё [OK]
pytest -q                                              # тесты без обращения к модели
deckforge run -c configs/run.example.yaml --png        # три варианта → out/run/vk_tech/
```

## 1. Что нужно

| Что | Требование |
|---|---|
| ОС | Ubuntu 24.04 LTS, x86_64 |
| CPU / RAM | 2 ядра / 4 ГБ (пик одного прогона на трёх вариантах ≈ 1,3 ГБ) |
| Диск | ≈ 3 ГБ: LibreOffice ≈ 0,5 ГБ, репозиторий с шаблонами и примерами ≈ 0,5 ГБ, окружение Python ≈ 1 ГБ |
| Сеть | GitHub (клон и Git LFS ≈ 335 МБ), PyPI, OpenAI-совместимый API модели |
| Ключ API | OpenRouter или инференс VK — [раздел 5](#5-ключ-и-модели-env). Без ключа работает сборка по готовому outline — [раздел 7](#без-ключа-api) |

GPU не нужен: модели вызываются через API.

## 2. Системные пакеты

```bash
sudo apt update
sudo apt install -y --no-install-recommends git git-lfs ca-certificates curl \
    python3.12 python3.12-venv libreoffice-impress libreoffice-calc fonts-liberation fonts-dejavu-core fontconfig
```

Что зачем:
- `git-lfs` — шаблоны `.pptx` и готовые колоды лежат в Git LFS. Без него вместо файлов будут текстовые указатели.
- `python3.12`, `python3.12-venv` — сервис требует Python ≥ 3.12.
- `libreoffice-impress`, `libreoffice-calc` — PDF, PNG-превью и картинки слайдов для VLM-судьи. Без LibreOffice колоды `.pptx` и `.html` собираются, но PDF, PNG и судьи не будет.
- `fonts-liberation`, `fonts-dejavu-core` — шрифты для LibreOffice и для аудита. Liberation Sans метрически совпадает с Arial. Аудит мерит ширину текста шрифтов шаблона (Play, Montserrat) через Arial с поправкой, поэтому с этим набором он даёт те же результаты, что у нас.

> [!NOTE]
> Фирменные шрифты шаблонов (Play, Montserrat) ставить не нужно. На PNG и в PDF LibreOffice подставит вместо них Liberation или DejaVu; `.pptx` и `.html` от этого не зависят. Если Montserrat всё же стоит в системе (`fonts-montserrat`), аудит начнёт мерить текст по нему, и находки L03 «текст не влез» могут отличаться от примеров в `examples/output`.

На Ubuntu 24.04 установка занимает 3–5 минут, почти всё время — LibreOffice.

## 3. Клонирование

```bash
git lfs install                  # один раз на пользователя, ДО clone
git clone --branch v0.1.4 https://github.com/kireev-duty/deckforge.git
cd deckforge
```

`--branch v0.1.4` — зафиксированная версия для экспертизы; без флага склонируется текущий `master`.

Проверка, что шаблоны скачались, а не остались указателями LFS:

```bash
ls -l data/templates/
# ожидается три файла по 13–24 МБ:
#   VK Tech шаблон.pptx, VK_WorkSpace_Клиентская_конференция_Шаблон_03.pptx, Шаблон презентации VK Education.pptx
```

Если файлы весят около 130 байт, git-lfs не сработал: `git lfs install && git lfs pull`.

**Без готовых колод.** Git LFS скачивает ≈ 335 МБ, из них 170 МБ — шаблоны в `data/`, остальное — 9 готовых колод и сценарий жюри в `examples/output/`. Колоды трёх шаблонов открываются и в [галерее](https://kireev-duty.github.io/deckforge/). Если качать их не нужно:

```bash
GIT_LFS_SKIP_SMUDGE=1 git clone --branch v0.1.4 https://github.com/kireev-duty/deckforge.git
cd deckforge && git lfs pull --include="data/**"
```

**Если LFS не отдаёт файлы** (у GitHub есть месячная квота на трафик LFS): те же шаблоны лежат архивом в [релизе v0.1.4](https://github.com/kireev-duty/deckforge/releases/tag/v0.1.4):

```bash
curl -fLO https://github.com/kireev-duty/deckforge/releases/download/v0.1.4/deckforge-templates-dataset-v0.1.4.zip
python3.12 -m zipfile -e deckforge-templates-dataset-v0.1.4.zip .    # распакует в data/
```

## 4. Окружение Python

```bash
python3.12 -m venv .venv
source .venv/bin/activate        # дальше python, pip, pytest и deckforge — из .venv
pip install -e '.[dev]'
```

`[dev]` добавляет pytest, ruff и playwright. Браузеры playwright нужны только для `tools/browser_check.py`, ставить их не обязательно. Установка занимает 2–5 минут при нормальном доступе к PyPI.

`deckforge …` — то же, что `python -m deckforge.cli …`. Справка: `deckforge --help`, `deckforge run --help`.

## 5. Ключ и модели (`.env`)

```bash
cp .env.example .env
nano .env                        # LLM_API_KEY=…
```

`.env` в git не попадает. По умолчанию модели вызываются через [OpenRouter](https://openrouter.ai/settings/keys):
- текст и зрение — Qwen3.8-27B (Apache 2.0);
- картинки — FLUX.2 [klein] 4B (Apache 2.0).

Колоды в `examples/output` собраны именно так. Полный список переменных — README, «Переменные окружения».

**Инференс VK или свой vLLM** — меняются адрес и ключ, модель та же:

```bash
LLM_BASE_URL=https://<endpoint>/v1
LLM_API_KEY=<ключ>
LLM_MODEL=<имя модели из GET /v1/models>
VLM_MODEL=<то же имя: одна модель и для текста, и для зрения>
T2I_MODEL=                       # пусто — без иллюстраций; или картинки с OpenRouter: T2I_BASE_URL + T2I_API_KEY
```

Имя модели на endpoint: `curl -s -H "Authorization: Bearer $LLM_API_KEY" https://<endpoint>/v1/models`.

Два параметра для инференса с ограничениями:
- **«Размышления» Qwen.** Клиент выключает их в каждом вызове: `reasoning` для OpenRouter, `chat_template_kwargs.enable_thinking` для vLLM. Если сервер отвергает незнакомое поле, оставьте только своё: `LLM_NO_THINK_JSON={"chat_template_kwargs": {"enable_thinking": false}}`.
- **Лимит параллельных запросов.** `LLM_MAX_CONCURRENCY=4` ставит общий лимит одновременных запросов. Иначе три колоды с судьёй дают до 12 запросов разом.

## 6. Проверка

```bash
python tools/check_env.py
```

Ожидаемый вывод (время ответа модели будет другим):

```text
1. Репозиторий
  [OK]   Python 3.12.3 (нужен ≥ 3.12)
  [OK]   шаблоны датасета: 3 .pptx
  [OK]   разметка образцов data/archetypes: 3 из 3
2. .env
  [OK]   LLM_BASE_URL=https://openrouter.ai/api/v1  LLM_MODEL=qwen/qwen3.8-27b-20260814
3. Текстовая модель (через скилл slide_filler, JSON по схеме, без «размышлений»)
  [OK]   qwen/qwen3.8-27b-20260814 за 4.6с, fills=…
4. Зрение (VLM)
  [OK]   qwen/qwen3.8-27b-20260814 видит картинку (6.3с): …
5. Text-to-image
  [OK]   black-forest-labs/flux.2-klein-4b @ https://openrouter.ai/api/v1 за 4.3с → …/out/t2i_check.jpg
6. LibreOffice (PDF, PNG, VLM-судья)
  [OK]   /usr/bin/soffice

Итог: всё в порядке
```

Без ключа пункты 3–5 будут `[SKIP]`. Код выхода 1, если есть `[FAIL]`.

```bash
pytest -q
```

Тесты модель не вызывают: реальные ответы Qwen записаны в кассеты `tests/cassettes/`, ключ в тестах подменяется пустым. Ожидается `393 passed` без пропусков — на проверочной машине за 6 минут. Часть тестов собирает колоды и зовёт LibreOffice. Если шаблонов нет (LFS не скачался), тесты с ними пропускаются (`skipped`), а не падают.

## 7. Запуск конфиг-файлом

Главный сценарий: один конфиг — три варианта вёрстки на одном контенте.

```bash
deckforge run -c configs/run.example.yaml --png
```

Что происходит (прогресс печатается по шагам):
1. Разбор шаблона `data/templates/VK Tech шаблон.pptx` в Template DNA.
2. Бриф: контент-пакета в конфиге нет, поэтому его выводит из шаблона скилл `template_brief`.
3. Outline — один вызов LLM на все варианты.
4. Три колоды параллельно — executive, narrative, visual. У каждой: иллюстрации → вёрстка → рендер `.pptx` → 26 детерминированных проверок → безопасные автофиксы → PNG → VLM-судья и текст выступления → PDF и HTML.

Бюджет — 5 минут на все три варианта вместе. На Ubuntu 24.04 с OpenRouter прогон занял 118 с: бриф и outline — 40 с, дальше три колоды параллельно. Детерминированных ошибок аудита — 0, текст выступления — 5,7–6,1 мин из 7. Время зависит от провайдера: на тех же колодах бывает и 90, и 200 с. Если инференс медленный, сначала пропускаются картинки, потом судья; вёрстка, аудит и экспорт выполняются всегда.

Результат — `out/run/vk_tech/`:

```text
compare.md                         сравнение трёх вариантов: слайды, архетипы, аудит, речь, время
brief.md, outline.json             бриф, выведенный из шаблона, и структура — одна на все варианты
<strategy>.pptx | .pdf | .html     колода (текст выступления — в заметках .pptx и по клавише N в .html)
<strategy>.speech.md               текст выступления по слайдам
<strategy>.audit.json              находки аудита
<strategy>.manifest.json           провенанс: версии скиллов и моделей, стратегия, автофиксы, тайминги
<strategy>/                        PNG-превью слайдов и contact.png
run.json                           сводка прогона
```

Посмотреть: `cat out/run/vk_tech/compare.md`, `xdg-open out/run/vk_tech/visual.html` (или скопировать файл к себе и открыть в браузере), `libreoffice out/run/vk_tech/visual.pptx`.

Флаги `run`:

| флаг | что делает |
|---|---|
| `--png` | PNG-превью слайдов |
| `-t путь/шаблон.pptx` | другой шаблон вместо `template` из конфига |
| `--topic "тема одной строкой"` | контент по теме вместо вывода брифа из шаблона |
| `--context context.json` | факты из репозитория (`prepare-context`); `--topic` тогда — задача |
| `--outline путь/outline.json` | готовый outline — без LLM на этапе контента |
| `--minutes 7` | длительность выступления, под неё пишется текст к слайдам |
| `--no-judge`, `--no-images`, `--no-notes`, `--no-fix` | без VLM-судьи, иллюстраций, текста выступления, автофиксов |
| `-o out/dir` | папка результата вместо `output_dir` из конфига |

### Без ключа API

Сборка без модели: outline — готовый из примеров, судья, картинки и текст выступления выключены.

```bash
deckforge run -c configs/run.example.yaml --outline examples/output/vk_tech/outline.json \
    --no-judge --no-images --no-notes --png -o out/run/offline
```

Три колоды за 25 с. Вёрстка, аудит, автофиксы, PDF и HTML — те же. Без ключа текста выступления нет: его пишет модель (без `--no-notes` будет предупреждение, а не ошибка).

### Другие конфиги

| конфиг | что собирает |
|---|---|
| `configs/template_only.yaml` | на входе только шаблон — VK WorkSpace (тёмный фон, другой набор образцов) |
| `configs/final/vk_tech.yaml` | колоды из `examples/output/vk_tech`; остальные шаблоны — `configs/final/*.yaml` с `--outline examples/output/vk_tech/outline.json` (один контент на все шаблоны) |

### Свой шаблон

```bash
deckforge parse "путь/к/шаблону.pptx"          # что сервис увидел: палитра, шрифты, шкала, образцы
deckforge prepare "путь/к/шаблону.pptx"        # необязательно: VLM уточняет разметку образцов (30–60 с, вне бюджета генерации)
deckforge run -c configs/run.example.yaml -t "путь/к/шаблону.pptx" -o out/run/my --png [--topic "о чём презентация"]
```

Подходят `.pptx`, `.potx`, `.ppsx` и `.pptm`. Шаблон без слайдов-примеров (только мастер и лейауты) размечается по лейаутам.

Свой контент — ключ `content_pack:` в конфиге. Это папка: `brief.md`, другие тексты (`.md`, `.txt`, `.docx`, `.pdf`), данные в `data/*.json|csv|xlsx`. Пример — `examples/content_pack`.

### Контекст из репозитория

Вход — репозиторий, его документация и история. Подготовка идёт до генерации и в 5 минут не входит:

```bash
deckforge prepare-context /путь/к/репозиторию -o out/context.json    # папка или .zip; с .git — и история коммитов
deckforge run -c configs/run.example.yaml --context out/context.json --topic "Питч проекта на 7 минут" --png
```

`prepare-context` читает README, `docs/`, прочие тексты, `pyproject.toml` или `package.json`, структуру папок и `git log`. Скилл `context_digest` выписывает из них факты со ссылками на источники — по вызову на каждые ≈ 8 тыс. символов. На репозитории deckforge это 74 источника, 24 вызова модели и ≈ 1,5 минуты; из 276 фактов в промпт уходит 71, самые важные. Результат кэшируется по содержимому: повторная подготовка того же репозитория модель не вызывает. `--topic` — задача презентации; без неё — рассказ о проекте по его материалам. Числа на слайдах — только из фактов.

## 8. Веб-интерфейс и API

```bash
streamlit run deckforge/ui/app.py                                # http://localhost:8501
uvicorn deckforge.api.app:app --host 127.0.0.1 --port 8000       # http://localhost:8000/docs
```

На удалённом сервере — SSH-туннель с вашей машины: `ssh -L 8501:localhost:8501 -L 8000:localhost:8000 user@server`, затем те же адреса в локальном браузере. Или `--server.address 0.0.0.0` для Streamlit и `--host 0.0.0.0` для uvicorn, если порты открыты.

В интерфейсе:
1. Слева — шаблон из датасета или свой `.pptx`.
2. «Контент» — «Только шаблон», тема или бриф с файлами.
3. «Сгенерировать варианты».
4. Дальше — вкладки трёх вариантов с превью, аудитом, выбором фиксов и скачиванием `.pptx` / `.pdf` / `.html`.

Без ключа доступен режим «Готовый outline».

## 9. Вариант: Docker

Для любого дистрибутива с Docker Engine и Compose v2. Внутри — Python 3.12 и LibreOffice. Шаблоны монтируются из клона, поэтому шаги 3 и 5 те же.

```bash
sudo apt install -y docker.io docker-compose-v2      # Ubuntu; для других — https://docs.docker.com/engine/install/
sudo usermod -aG docker $USER && newgrp docker
cp .env.example .env                                 # вписать LLM_API_KEY
docker compose build                                 # ≈ 1,6 ГБ, 5–10 минут
docker compose run --rm --user "$(id -u):$(id -g)" cli run -c configs/run.example.yaml --png   # → ./out/run/vk_tech
docker compose up                                    # API http://localhost:8000/docs и UI http://localhost:8501
```

`--user` — чтобы файлы в `./out` принадлежали вам, а не root. Без него: `sudo chown -R $USER: out`.

## 10. Неполадки

| симптом | причина и что делать |
|---|---|
| `check_env`: «указатели Git LFS вместо шаблонов»; `BadZipFile` при разборе | clone без git-lfs: `git lfs install && git lfs pull`. Квота LFS исчерпана — архив из релиза ([раздел 3](#3-клонирование)) |
| `python3.12: command not found` | Ubuntu 22.04 / Debian 12: Docker ([раздел 9](#9-вариант-docker)) или `uv python install 3.12` + `uv venv -p 3.12` |
| `LibreOffice не найден` | `sudo apt install libreoffice-impress`. Нестандартный путь (snap, `/opt`) — `SOFFICE_PATH=/путь/к/soffice` в `.env` |
| PDF/PNG не создаются, «LibreOffice: код 1» | висит `soffice.bin` от прошлого прогона: `pkill -f soffice.bin` и повторить |
| шрифты на PNG и в PDF отличаются от PowerPoint | подмена шрифтов в LibreOffice: Play → Liberation Sans. `.pptx` и `.html` от этого не зависят |
| `401` / `404 model not found` | ключ или имя модели: `LLM_MODEL` должно совпадать с `GET /v1/models` провайдера |
| прогон дольше 5 минут, в manifest «судья пропущен» | медленный инференс: лимит сработал, колоды готовы. Ускорить — `--no-judge`, `--no-images` |
| порт 8501 или 8000 занят | `streamlit run … --server.port 8601`, `uvicorn … --port 8001` |
| медленный PyPI | зеркало: `pip install -i https://<зеркало>/simple -e '.[dev]'` |

Устройство сервиса — [ARCHITECTURE.md](ARCHITECTURE.md), проверки аудита — [AUDIT.md](AUDIT.md), модели — [MODELS.md](MODELS.md).
