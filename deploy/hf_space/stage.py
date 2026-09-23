"""Собрать staging-папку Hugging Face Space: код, промпты, шаблоны датасета и Dockerfile стенда в корне.

`python deploy/hf_space/stage.py [out/hf_space]` → папку заливает в Space `.github/workflows/deploy-space.yml`,
локально её собирает `docker build` (DEVELOPMENT.md, «Демо-стенд»). Примеры `examples/output` не берутся (225 МБ):
на стенде нужен только outline примеров — режим «Готовый outline» без LLM.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
TREES = ["deckforge", "skills", "strategies", "configs", "tools",
         "data/templates", "data/holdout", "data/archetypes", "examples/content_pack"]
FILES = ["pyproject.toml", "examples/output/vk_tech/outline.json", "examples/output/vk_tech/brief.md"]
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info")
LFS_POINTER_MAX = 1000  # байт: указатель git-lfs вместо .pptx — шаблоны не скачаны


def stage(dest: Path) -> Path:
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for rel in TREES:
        shutil.copytree(ROOT / rel, dest / rel, ignore=IGNORE)
    for rel in FILES:
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, dest / rel)
    shutil.copy2(HERE / "Dockerfile", dest / "Dockerfile")
    shutil.copy2(HERE / "space_readme.md", dest / "README.md")  # front matter Space; pyproject ссылается на README.md
    pointers = [p for p in (dest / "data").rglob("*.pptx") if p.stat().st_size < LFS_POINTER_MAX]
    if pointers:
        raise SystemExit(f"LFS-указатели вместо шаблонов ({len(pointers)}): git lfs pull --include \"data/**\"")
    return dest


if __name__ == "__main__":
    out = stage(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "out" / "hf_space")
    size = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    print(f"{out}: {size / 2**20:.0f} МБ")
