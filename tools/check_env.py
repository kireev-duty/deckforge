"""check_env — проверка окружения перед работой: .env, доступ к LLM, зрение, LibreOffice, картинки.

    .venv\\Scripts\\python.exe tools\\check_env.py
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


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
    print(f"  [FAIL] {msg}")


def main() -> None:
    print("1. .env")
    if load_dotenv():
        ok(".env найден")
    else:
        fail(".env не найден — скопируйте .env.example в .env и заполните LLM_API_KEY")
        return
    if not os.environ.get("LLM_API_KEY") or os.environ["LLM_API_KEY"].endswith("..."):
        fail("LLM_API_KEY не заполнен")
        return
    ok(f"LLM_BASE_URL={os.environ.get('LLM_BASE_URL')}  LLM_MODEL={os.environ.get('LLM_MODEL')}")

    from deckforge.llm.client import LLMClient

    c = LLMClient()

    print("2. Текстовая модель (через скилл slide_filler, JSON по схеме, без «размышлений»)")
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

    print("3. Зрение (VLM)")
    png = next((ROOT / "out" / "render").rglob("slide_01.png"), None)
    if png is None:
        print("  [SKIP] нет PNG в out/render — сначала tools/render_deck.py")
    else:
        from deckforge.llm.client import _data_url

        t0 = time.perf_counter()
        try:
            r = c._client.chat.completions.create(
                model=c.vision_model, max_tokens=60, temperature=0, extra_body=c.no_think_extra(),
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": "Опиши слайд одним предложением по-русски."},
                    {"type": "image_url", "image_url": {"url": _data_url(png)}}]}],
            )
            ok(f"{c.vision_model} видит картинку ({time.perf_counter() - t0:.1f}с): {r.choices[0].message.content!r}")
        except Exception as e:  # noqa: BLE001
            fail(f"{c.vision_model}: {e}")

    print("4. Text-to-image")
    if not c.images_enabled:
        print("  [SKIP] картинки отключены (T2I_MODEL пуст или нет ключа) — допустимо, задача со звёздочкой")
    else:
        out = ROOT / "out" / "t2i_check.png"
        out.parent.mkdir(exist_ok=True)
        t0 = time.perf_counter()
        try:
            p = c.generate_image("abstract blue gradient shapes, dark background, no text", out)
            ok(f"{c.image_model} @ {c.image_base_url} за {time.perf_counter() - t0:.1f}с → {p}")
        except Exception as e:  # noqa: BLE001
            fail(f"{c.image_model}: {e}")

    print("5. LibreOffice")
    try:
        from deckforge.export.render import find_soffice

        ok(find_soffice())
    except Exception as e:  # noqa: BLE001
        fail(str(e))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
