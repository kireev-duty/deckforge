"""check_env — проверка окружения перед работой: репозиторий, .env, доступ к LLM, зрение, картинки, LibreOffice.

    .venv\\Scripts\\python.exe tools\\check_env.py      (Windows)
    .venv/bin/python tools/check_env.py                (Linux, macOS)

Без ключа проверяются репозиторий и LibreOffice — этого хватает для прогона без LLM
(`run --outline … --no-judge --no-images --no-notes`). Код выхода 1, если есть [FAIL].
"""

from __future__ import annotations

import os
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FAILS: list[str] = []


def load_dotenv() -> bool:
    p = ROOT / ".env"
    if not p.exists():
        return False
    for line in p.read_text("utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())
    return True


def ok(msg: str) -> None:
    print(f"  [OK]   {msg}")


def fail(msg: str) -> None:
    FAILS.append(msg)
    print(f"  [FAIL] {msg}")


def skip(msg: str) -> None:
    print(f"  [SKIP] {msg}")


def check_repo() -> None:
    """Python и шаблоны датасета: после clone без git-lfs вместо .pptx лежат указатели ~130 байт."""
    v = sys.version_info
    (ok if v >= (3, 12) else fail)(f"Python {v.major}.{v.minor}.{v.micro} (нужен ≥ 3.12)")
    templates = sorted((ROOT / "data" / "templates").glob("*.pptx")) + sorted((ROOT / "data" / "holdout").glob("*.pptx"))
    pointers = [p.name for p in templates if not zipfile.is_zipfile(p)]
    if not templates:
        fail("в data/templates нет .pptx — репозиторий склонирован не целиком")
    elif pointers:
        fail(f"указатели Git LFS вместо шаблонов: {', '.join(pointers)} — поставьте git-lfs и выполните `git lfs pull`")
    else:
        ok(f"шаблоны датасета: {len(templates)} .pptx")
    labels = list((ROOT / "data" / "archetypes").glob("*.json"))
    (ok if labels else fail)(f"разметка образцов data/archetypes: файлов — {len(labels)}")


def vision_probe() -> Path | None:
    """PNG для проверки зрения: слайд из прошлого рендера или картинка из README (есть в любом clone)."""
    return next((ROOT / "out" / "render").rglob("slide_01.png"), None) or next(
        (p for p in (ROOT / "docs" / "img" / "strategies.png",) if p.exists()), None)


def check_models() -> None:
    from deckforge.llm.client import LLMClient

    c = LLMClient()

    print("3. Текстовая модель (через скилл slide_filler, JSON по схеме, без «размышлений»)")
    from deckforge.llm.skills import load_skill

    t0 = time.perf_counter()
    try:
        res = c.run_skill(
            load_skill("slide_filler"), language="ru",
            slide_outline={"title": "Выручка выросла на 18% за счёт B2B", "bullets": ["B2B +31% г/г", "Retail −4%"]},
            slots=[{"id": "t1", "role": "title", "max_chars": 60}, {"id": "b1", "role": "body", "max_items": 2}],
        )
        assert isinstance(res, dict) and "fills" in res, res
        ok(f"{c.text_model} за {time.perf_counter() - t0:.1f}с, fills={res['fills']}")
    except Exception as e:  # noqa: BLE001
        fail(f"{c.text_model}: {e}")
        return

    print("4. Зрение (VLM)")
    png = vision_probe()
    if png is None:
        skip("нет PNG для проверки — сначала tools/render_deck.py")
    else:
        from deckforge.llm.client import _data_url

        t0 = time.perf_counter()
        try:
            r = c._client.chat.completions.create(
                model=c.vision_model, max_tokens=60, temperature=0, extra_body=c.no_think_extra(),
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": "Опиши картинку одним предложением по-русски."},
                    {"type": "image_url", "image_url": {"url": _data_url(png)}}]}],
            )
            ok(f"{c.vision_model} видит картинку ({time.perf_counter() - t0:.1f}с): {r.choices[0].message.content!r}")
        except Exception as e:  # noqa: BLE001
            fail(f"{c.vision_model}: {e}")

    print("5. Text-to-image")
    if not c.images_enabled:
        skip("картинки отключены (T2I_MODEL пуст или нет ключа) — допустимо, задача со звёздочкой")
    else:
        out = ROOT / "out" / "t2i_check.png"
        out.parent.mkdir(exist_ok=True)
        t0 = time.perf_counter()
        try:
            p = c.generate_image("abstract blue gradient shapes, dark background, no text", out)
            ok(f"{c.image_model} @ {c.image_base_url} за {time.perf_counter() - t0:.1f}с → {p}")
        except Exception as e:  # noqa: BLE001
            fail(f"{c.image_model}: {e}")


def main() -> int:
    print("1. Репозиторий")
    check_repo()

    print("2. .env")
    key = False
    if not load_dotenv():
        fail(".env не найден — скопируйте .env.example в .env и заполните LLM_API_KEY")
    elif not os.environ.get("LLM_API_KEY") or os.environ["LLM_API_KEY"].endswith("..."):
        skip("LLM_API_KEY не заполнен — доступен только прогон без LLM (готовый outline, без судьи, картинок и речи)")
    else:
        key = True
        ok(f"LLM_BASE_URL={os.environ.get('LLM_BASE_URL')}  LLM_MODEL={os.environ.get('LLM_MODEL')}")

    if key:
        check_models()
    else:
        print("3–5. Модели")
        skip("без ключа модели не проверяются")

    print("6. LibreOffice (PDF, PNG, VLM-судья)")
    try:
        from deckforge.export.render import find_soffice

        ok(find_soffice())
    except Exception as e:  # noqa: BLE001
        fail(str(e))

    print(f"\nИтог: {'всё в порядке' if not FAILS else f'ошибок — {len(FAILS)}'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
