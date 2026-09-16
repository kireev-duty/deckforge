"""HTTP API: реестр шаблонов, генерация job'ом (синхронно, FakeClient), аудит, фиксы, скачивание по белому списку."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from deckforge.api.app import create_app
from deckforge.api.jobs import safe_name, write_content_pack
from tests.conftest import FakeClient, cassette

REPO = Path(__file__).resolve().parents[1]
BRIEF = (REPO / "examples" / "content_pack" / "brief.md").read_text("utf-8")


@pytest.fixture
def api(tmp_path: Path):
    app = create_app(root=tmp_path / "api", client_factory=lambda: FakeClient(cassette("outline_writer_pulse")),
                     executor=None)
    with TestClient(app) as c:
        yield c


def _vk_tech(api: TestClient) -> dict:
    items = api.get("/templates").json()
    hit = [t for t in items if "VK Tech" in t["name"]]
    if not hit:
        pytest.skip("шаблон VK Tech не найден (LFS?)")
    return hit[0]


def test_health_and_strategies(api: TestClient) -> None:
    h = api.get("/health").json()
    assert h["version"] and set(h["models"]) == {"text", "vision", "image", "base_url"} and "executive" in h["strategies"]
    assert "LLM_API_KEY" not in str(h)
    s = api.get("/strategies").json()
    assert [x["name"] for x in s] == ["executive", "narrative", "visual"]
    assert s[0]["data_visualization"]["numeric_series"] == "table" and s[1]["sections"] is True


def test_templates_registry_and_upload(api: TestClient, template_path, tmp_path: Path) -> None:
    vk = _vk_tech(api)
    assert vk["builtin"] and len(vk["id"]) == 12 and vk["summary"] is None
    full = api.get(f"/templates/{vk['id']}").json()
    assert full["summary"]["archetypes"] and full["summary"]["palette"]["accent"] == ["0077FF"]

    holdout = template_path("ЛЦТ2026")
    with holdout.open("rb") as fh:
        r = api.post("/templates", files={"file": ("copy.pptx", fh, "application/octet-stream")})
    assert r.status_code == 200 and r.json()["builtin"] and r.json()["summary"]["fonts"][0] == "Montserrat"

    from tests.fixtures.bad_slides import make_clean  # неизвестный шаблон: синтетический .pptx

    foreign = make_clean(tmp_path).pptx
    with foreign.open("rb") as fh:
        r = api.post("/templates", files={"file": ("../../evil name.pptx", fh, "application/octet-stream")})
    assert r.status_code == 200, r.text
    up = r.json()
    assert not up["builtin"] and up["name"] == "evil name" and up["summary"]["slides"] >= 2
    saved = Path(up["path"])
    assert saved.name == f"evil name__{up['id']}.pptx" and saved.parent.name == up["id"]
    assert [t["id"] for t in api.get("/templates").json()].count(up["id"]) == 1

    r = api.post("/templates", files={"file": ("x.pptx", b"PK\x03\x04 not a pptx", "application/octet-stream")})
    assert r.status_code == 400
    assert api.get("/templates/nope").status_code == 404


def test_generate_audit_fix_download(api: TestClient) -> None:
    vk = _vk_tech(api)
    data = {"template_id": vk["id"], "brief": BRIEF, "purpose": "product", "audience": "руководители",
            "target_slides": 12, "strategies": "executive", "judge": False, "autofix": False, "render_png": False,
            "export": "pptx"}
    product = REPO / "examples" / "content_pack" / "product.md"
    metrics = REPO / "examples" / "content_pack" / "data" / "metrics.json"
    files = [("files", ("product.md", product.read_bytes(), "text/markdown")),
             ("files", ("metrics.json", metrics.read_bytes(), "application/json"))]
    r = api.post("/generate", data=data, files=files)
    assert r.status_code == 202, r.text
    jid = r.json()["id"]

    j = api.get(f"/jobs/{jid}").json()
    assert j["status"] == "done", j
    assert j["template"]["sha1"] == vk["id"] and j["not_implemented"] == []
    assert any(p.startswith("outline:") for p in j["progress"])
    assert len(j["decks"]) == 1 and j["decks"][0]["strategy"] == "executive"
    files_ = j["decks"][0]["files"]
    assert set(files_) >= {"pptx", "ir.json", "audit.json", "manifest.json", "pngs"} and "pdf" not in files_
    assert api.get("/jobs").json()[0]["id"] == jid

    # контент-пакет собран из брифа и файлов
    pack = Path(api.app.state.df.jobs.root) / jid / "content_pack"
    assert (pack / "brief.md").exists() and (pack / "product.md").exists() and (pack / "data" / "metrics.json").exists()

    a = api.get(f"/jobs/{jid}/decks/executive/audit").json()
    assert a["summary"]["checks_run"] == 24 and a["report"]["findings"] and a["fix_plan"]
    l03 = [i for i, f in enumerate(a["report"]["findings"])
           if f["check_id"] == "L03_text_overflow" and f["severity"] == "error"]
    assert l03, "на VK Tech без автофиксов ожидаются L03-ошибки"

    r = api.post(f"/jobs/{jid}/decks/executive/fix", json={"findings": []})
    assert r.status_code == 200 and r.json()["applied"] == 0
    r = api.post(f"/jobs/{jid}/decks/executive/fix", json={"findings": l03})
    assert r.status_code == 200, r.text
    fx = r.json()
    assert fx["applied"] >= 1 and fx["after"]["errors"] < fx["before"]["errors"] and fx["contextual_stale"] is False
    a2 = api.get(f"/jobs/{jid}/decks/executive/audit").json()
    assert a2["summary"]["errors"] == fx["after"]["errors"]
    assert not [f for f in a2["report"]["findings"] if f["check_id"] == "L03_text_overflow" and f["severity"] == "error"]

    r = api.get(f"/jobs/{jid}/decks/executive/files/executive.pptx")
    assert r.status_code == 200 and r.content[:2] == b"PK" and len(r.content) > 100_000
    assert api.get(f"/jobs/{jid}/decks/executive/files/executive.pdf").status_code == 404
    assert api.get(f"/jobs/{jid}/decks/executive/files/..%2Frun.json").status_code == 404
    assert api.get(f"/jobs/{jid}/decks/executive/files/dna.json").status_code == 404
    assert api.get(f"/jobs/{jid}/decks/narrative/files/narrative.pptx").status_code == 404
    assert api.get(f"/jobs/{jid}/files/outline.json").status_code == 200
    assert api.get(f"/jobs/{jid}/files/content_pack").status_code == 404
    assert api.get("/jobs/zzz").status_code == 404


def test_generate_rejects_bad_input(api: TestClient) -> None:
    vk = _vk_tech(api)
    base = {"template_id": vk["id"], "brief": BRIEF, "strategies": "executive", "judge": False, "render_png": False}
    assert api.post("/generate", data={**base, "template_id": "nope"}).status_code == 404
    assert api.post("/generate", data={**base, "strategies": "executive,fancy"}).status_code == 400
    assert api.post("/generate", data={**base, "brief": "коротко"}).status_code == 422
    r = api.post("/generate", data=base, files=[("files", ("evil.exe", b"MZ", "application/octet-stream"))])
    assert r.status_code == 400


def test_audit_foreign_deck(api: TestClient) -> None:
    vk = _vk_tech(api)
    deck = Path(vk["path"])  # сам шаблон как «чужая колода» — аудит должен пройти без DeckIR
    with deck.open("rb") as fh:
        r = api.post("/audit", data={"template_id": vk["id"]}, files={"deck": ("deck.pptx", fh, "application/octet-stream")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["summary"]["checks_run"] == 24 and isinstance(body["fix_plan"], list)
    r = api.post("/audit", files={"deck": ("deck.pptx", b"PK\x03\x04", "application/octet-stream")})
    assert r.status_code == 400


def test_safe_name_and_pack(tmp_path: Path) -> None:
    assert safe_name("../../etc/passwd") == "passwd"
    assert safe_name("C:\\Users\\x\\Отчёт Q3.md") == "Отчёт Q3.md"
    assert safe_name("...") == "file" and safe_name("", "t") == "t"
    d = write_content_pack(tmp_path / "pack", "бриф", [("brief.md", b"x"), ("notes.txt", b"y"), ("m.csv", b"a,b")])
    assert (d / "brief.md").read_text("utf-8") == "бриф\n" and (d / "brief_extra.md").exists()
    assert (d / "notes.txt").exists() and (d / "data" / "m.csv").exists()
