"""Приведение шаблона к виду, который разбирают парсинг и рендер: обычный .pptx со слайдами-образцами.

Два случая, которые иначе ломают прогон на незнакомом шаблоне:
- тип пакета не «презентация» (.potx, .ppsx, .pptm, .potm): python-pptx такой файл не открывает вовсе,
  а PowerPoint не откроет сохранённый с этим типом .pptx — тип основной части переписывается, макросы удаляются;
- в шаблоне нет ни одного слайда (обычный .potx — только мастера и лейауты): образцы берутся только
  со слайдов, поэтому на каждый лейаут с контентным плейсхолдером добавляется слайд. Геометрию и стиль
  плейсхолдеры наследуют от лейаута — так же PowerPoint создаёт новый слайд; классификатор читает её
  через цепочку наследования (`layout_classifier._inherited_bbox`).
"""

from __future__ import annotations

import posixpath
import uuid
import zipfile
from pathlib import Path

from lxml import etree

from deckforge.core.ooxml import NS, placeholder

CONTENT_TYPES = "[Content_Types].xml"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
RELS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
PRESENTATION_MAIN = "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"
# основная часть «не презентации»: шаблон, показ, их варианты с макросами
OTHER_MAINS = {
    "application/vnd.openxmlformats-officedocument.presentationml.template.main+xml": ".potx",
    "application/vnd.openxmlformats-officedocument.presentationml.slideshow.main+xml": ".ppsx",
    "application/vnd.ms-powerpoint.presentation.macroEnabled.main+xml": ".pptm",
    "application/vnd.ms-powerpoint.template.macroEnabled.main+xml": ".potm",
    "application/vnd.ms-powerpoint.slideshow.macroEnabled.main+xml": ".ppsm",
}
VBA_REL = "/vbaProject"
# колонтитулы в образец не превращают: лейаут только с ними — пустой слайд
SERVICE_PH = {"dt", "ftr", "sldNum"}
UPLOAD_SUFFIXES = (".pptx", ".potx", ".ppsx", ".pptm", ".potm", ".ppsm")


def _main_part(z: zipfile.ZipFile) -> str:
    """Имя основной части по связи officeDocument из `_rels/.rels` (обычно `ppt/presentation.xml`)."""
    rels = etree.fromstring(z.read("_rels/.rels"))
    for rel in rels:
        if rel.get("Type", "").endswith("/officeDocument"):
            return rel.get("Target", "").lstrip("/")
    return "ppt/presentation.xml"


def _main_type(z: zipfile.ZipFile, main: str) -> str:
    ct = etree.fromstring(z.read(CONTENT_TYPES))
    for o in ct.findall(f"{{{CT_NS}}}Override"):
        if o.get("PartName", "").lstrip("/") == main:
            return o.get("ContentType", "")
    return ""


def _slide_count(z: zipfile.ZipFile, main: str) -> int:
    pres = etree.fromstring(z.read(main))
    return len(pres.findall("p:sldIdLst/p:sldId", NS))


def needs_normalize(path: Path) -> str | None:
    """Почему шаблон нужно привести к .pptx со слайдами (`None` — не нужно)."""
    reasons = []
    with zipfile.ZipFile(path) as z:
        main = _main_part(z)
        kind = _main_type(z, main)
        if kind != PRESENTATION_MAIN:
            reasons.append(f"тип {OTHER_MAINS.get(kind, kind or 'не указан')} → .pptx")
        if _slide_count(z, main) == 0:
            reasons.append("нет слайдов — образцы из лейаутов")
    return "; ".join(reasons) or None


def _vba_parts(z: zipfile.ZipFile, main: str) -> set[str]:
    """Части макросов: vbaProject.bin и то, на что ссылаются его связи (vbaData.xml)."""
    d, b = main.rsplit("/", 1)
    rels_name = f"{d}/_rels/{b}.rels"
    if rels_name not in z.namelist():
        return set()
    out: set[str] = set()
    for rel in etree.fromstring(z.read(rels_name)):
        if rel.get("Type", "").endswith(VBA_REL):
            part = posixpath.normpath(posixpath.join(d, rel.get("Target", "")))
            out.add(part)
            pd, pb = part.rsplit("/", 1)
            sub = f"{pd}/_rels/{pb}.rels"
            if sub in z.namelist():
                out.add(sub)
                for r in etree.fromstring(z.read(sub)):
                    out.add(posixpath.normpath(posixpath.join(pd, r.get("Target", ""))))
    return out


def _rewrite_package(src: Path, dest: Path) -> None:
    """Копия пакета с типом «презентация» и без макросов."""
    with zipfile.ZipFile(src) as z:
        main = _main_part(z)
        drop = _vba_parts(z, main)
        ct = etree.fromstring(z.read(CONTENT_TYPES))
        for o in ct.findall(f"{{{CT_NS}}}Override"):
            name = o.get("PartName", "").lstrip("/")
            if name == main:
                o.set("ContentType", PRESENTATION_MAIN)
            elif name in drop:
                ct.remove(o)
        d, b = main.rsplit("/", 1)
        main_rels = f"{d}/_rels/{b}.rels"
        rels_xml = None
        if drop and main_rels in z.namelist():
            rels_xml = etree.fromstring(z.read(main_rels))
            for rel in list(rels_xml):
                if rel.get("Type", "").endswith(VBA_REL):
                    rels_xml.remove(rel)
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as out:
            for info in z.infolist():
                if info.filename in drop:
                    continue
                if info.filename == CONTENT_TYPES:
                    data = etree.tostring(ct, xml_declaration=True, encoding="UTF-8", standalone=True)
                elif info.filename == main_rels and rels_xml is not None:
                    data = etree.tostring(rels_xml, xml_declaration=True, encoding="UTF-8", standalone=True)
                else:
                    data = z.read(info.filename)
                out.writestr(info, data)


def _content_placeholders(layout) -> list:
    return [ph for ph in layout.placeholders if (placeholder(ph.element) or ("", None))[0] not in SERVICE_PH]


def _slides_from_layouts(path: Path) -> int:
    """По слайду на каждый лейаут с контентным плейсхолдером; возвращает число добавленных слайдов."""
    from pptx import Presentation  # python-pptx нужен только здесь

    prs = Presentation(str(path))
    added = 0
    for master in prs.slide_masters:
        for layout in master.slide_layouts:
            if _content_placeholders(layout):
                prs.slides.add_slide(layout)
                added += 1
    if added:
        prs.save(str(path))
    return added


def normalize_template(src: Path, dest: Path) -> Path:
    """Привести шаблон к .pptx со слайдами-образцами в `dest`; исходник не меняется.

    Файл собирается рядом под временным именем и заменяет `dest` целиком — параллельный разбор
    того же шаблона не увидит недописанный пакет."""
    src, dest = Path(src), Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{uuid.uuid4().hex}.pptx")
    try:
        _rewrite_package(src, tmp)
        with zipfile.ZipFile(tmp) as z:
            empty = _slide_count(z, _main_part(z)) == 0
        if empty:
            _slides_from_layouts(tmp)
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)
    return dest


__all__ = ["UPLOAD_SUFFIXES", "needs_normalize", "normalize_template"]
