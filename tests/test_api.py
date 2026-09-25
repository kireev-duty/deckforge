"""HTTP API: реестр шаблонов, генерация job'ом на FakeClient, аудит, фиксы, скачивание."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from deckforge.api.app import create_app
from deckforge.api.jobs import safe_name, write_content_pack
from tests.conftest import FakeClient, cassette, fake_notes

REPO = Path(__file__).resolve().parents[1]
BRIEF = (REPO / "examples" / "content_pack" / "brief.md").read_text("utf-8")


@pytest.fixture
def api(tmp_path: Path):
    app = create_app(root=tmp_path / "api", client_factory=lambda: FakeClient(cassette("outline_writer_pulse"),
                                                           by_skill={"speaker_notes": [fake_notes]}),
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

    from tests.fixtures.bad_slides import make_clean

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


def test_generate_audit_fix_download(api: TestClient, no_fitting) -> None:
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
    assert set(files_) >= {"pptx", "speech.md", "ir.json", "audit.json", "manifest.json", "pngs"} and "pdf" not in files_
    assert api.get("/jobs").json()[0]["id"] == jid

    # контент-пакет собран из брифа и файлов
    pack = Path(api.app.state.df.jobs.root) / jid / "content_pack"
    assert (pack / "brief.md").exists() and (pack / "product.md").exists() and (pack / "data" / "metrics.json").exists()

    a = api.get(f"/jobs/{jid}/decks/executive/audit").json()
    assert a["summary"]["checks_run"] == 26 and a["report"]["findings"] and a["fix_plan"]
    assert not [f for f in a["report"]["findings"] if f["check_id"].startswith("N0")]  # текст к каждому слайду
    l03 = [i for i, f in enumerate(a["report"]["findings"])
           if f["check_id"] == "L03_text_overflow" and f["severity"] == "error"]
    assert l03, "на VK Tech без подгонки текста и автофиксов ожидаются L03-ошибки"

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
    r = api.get(f"/jobs/{jid}/decks/executive/files/executive.speech.md")
    assert r.status_code == 200 and "Текст выступления" in r.text
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
    assert api.post("/generate", data={**base, "images": "sometimes"}).status_code == 422
    r = api.post("/generate", data=base, files=[("files", ("evil.exe", b"MZ", "application/octet-stream"))])
    assert r.status_code == 400


def test_generate_without_brief_is_accepted(api: TestClient) -> None:
    """На входе только шаблон: короткий бриф больше не 422, пустой — тоже (бриф выведет template_brief)."""
    vk = _vk_tech(api)
    base = {"template_id": vk["id"], "strategies": "executive", "judge": False, "autofix": False,
            "render_png": False, "export": "pptx"}
    # images=auto принимается: у executive картинки только из контента — генерации нет
    r = api.post("/generate", data={**base, "brief": "коротко", "images": "auto"})
    assert r.status_code == 202, r.text
    assert api.get(f"/jobs/{r.json()['id']}").json()["status"] == "done"

    r = api.post("/generate", data={**base, "brief": ""})
    assert r.status_code == 202, r.text
    job = api.get(f"/jobs/{r.json()['id']}").json()
    assert job["status"] == "done", job.get("error")
    assert api.get(f"/jobs/{job['id']}/files/brief.md").status_code == 200


def test_audit_foreign_deck(api: TestClient) -> None:
    vk = _vk_tech(api)
    deck = Path(vk["path"])  # шаблон как «чужая колода», без DeckIR
    with deck.open("rb") as fh:
        r = api.post("/audit", data={"template_id": vk["id"]}, files={"deck": ("deck.pptx", fh, "application/octet-stream")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["summary"]["checks_run"] == 26 and isinstance(body["fix_plan"], list)
    r = api.post("/audit", files={"deck": ("deck.pptx", b"PK\x03\x04", "application/octet-stream")})
    assert r.status_code == 400


def test_safe_name_and_pack(tmp_path: Path) -> None:
    assert safe_name("../../etc/passwd") == "passwd"
    assert safe_name("C:\\Users\\x\\Отчёт Q3.md") == "Отчёт Q3.md"
    assert safe_name("...") == "file" and safe_name("", "t") == "t"
    d = write_content_pack(tmp_path / "pack", "бриф", [("brief.md", b"x"), ("notes.txt", b"y"), ("m.csv", b"a,b"),
                                                       ("Отчёт.docx", b"z"), ("scan.PDF", b"p"), ("sales.xlsx", b"s")])
    assert (d / "brief.md").read_text("utf-8") == "бриф\n" and (d / "brief_extra.md").exists()
    assert (d / "notes.txt").exists() and (d / "data" / "m.csv").exists()
    assert (d / "Отчёт.docx").exists() and (d / "scan.PDF").exists() and (d / "data" / "sales.xlsx").exists()


# ──────────────────────────── стресс: зомби-job'ы, конкуренция, загрузки ────────────────────────────


def test_rejected_generate_leaves_no_zombie_job(api: TestClient) -> None:
    """400 на входе не оставляет job в реестре и папку на диске."""
    vk = _vk_tech(api)
    base = {"template_id": vk["id"], "brief": BRIEF, "strategies": "executive", "judge": False, "render_png": False}
    before = len(api.get("/jobs").json())
    assert api.post("/generate", data={**base, "strategies": "executive,fancy"}).status_code == 400
    assert api.post("/generate", data=base, files=[("files", ("evil.exe", b"MZ", "application/octet-stream"))]).status_code == 400
    assert api.post("/generate", data={**base, "export": "docx"}).status_code == 400
    assert len(api.get("/jobs").json()) == before
    jobs_dir = Path(api.app.state.df.jobs.root)
    assert not [p for p in jobs_dir.iterdir() if p.is_dir() and not any(p.iterdir())], "пустые папки job'ов"


def test_concurrent_jobs_and_fixes(template_path, tmp_path: Path, no_fitting) -> None:
    """Настоящий executor: пять /generate подряд не путают файлы; два /fix на одну колоду из двух потоков — оба 200."""
    import json
    import threading
    from concurrent.futures import ThreadPoolExecutor

    template_path("VK Tech")
    app = create_app(root=tmp_path / "api", client_factory=lambda: FakeClient(cassette("outline_writer_pulse"),
                                                           by_skill={"speaker_notes": [fake_notes]}),
                     executor=ThreadPoolExecutor(max_workers=1))
    with TestClient(app) as api:
        vk = _vk_tech(api)
        data = {"template_id": vk["id"], "brief": BRIEF, "strategies": "executive", "judge": False, "autofix": False,
                "render_png": False, "export": "pptx"}
        ids = [api.post("/generate", data={**data, "audience": f"аудитория {i}"}).json()["id"] for i in range(5)]
        for jid in ids:
            api.app.state.df.jobs.get(jid).wait(timeout=300)
            j = api.get(f"/jobs/{jid}").json()
            assert j["status"] == "done", j
        for i, jid in enumerate(ids):
            run_json = json.loads((tmp_path / "api" / "jobs" / jid / "run.json").read_text("utf-8"))
            assert run_json["config"]["audience"] == f"аудитория {i}"
            assert run_json["decks"][0]["pptx"] == "executive.pptx"  # пути относительные
            assert (tmp_path / "api" / "jobs" / jid / "executive.pptx").exists()

        jid = ids[0]
        a = api.get(f"/jobs/{jid}/decks/executive/audit").json()
        l03 = [i for i, f in enumerate(a["report"]["findings"]) if f["check_id"] == "L03_text_overflow" and f["severity"] == "error"]
        assert len(l03) >= 2
        results: list[int] = []

        def fix(sel: list[int]) -> None:
            results.append(api.post(f"/jobs/{jid}/decks/executive/fix", json={"findings": sel}).status_code)

        threads = [threading.Thread(target=fix, args=(l03[: len(l03) // 2],)), threading.Thread(target=fix, args=(l03[len(l03) // 2:],))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=300)
        assert results == [200, 200], results
        r = api.get(f"/jobs/{jid}/decks/executive/files/executive.pptx")
        assert r.status_code == 200 and r.content[:2] == b"PK"
        m = json.loads((tmp_path / "api" / "jobs" / jid / "executive.manifest.json").read_text("utf-8"))
        assert m["audit"]["autofix"]["user_applied"] >= 1 and m["audit"]["errors"] <= a["summary"]["errors"]
        a2 = api.get(f"/jobs/{jid}/decks/executive/audit").json()
        assert a2["summary"]["errors"] == m["audit"]["errors"]


def test_upload_edge_cases(api: TestClient, template_path, tmp_path: Path) -> None:
    from deckforge.pipeline.workspace import MAX_TEMPLATE_BYTES, check_pptx

    # 0 байт, docx-подобный zip без presentation.xml, «слишком большой»
    assert api.post("/templates", files={"file": ("empty.pptx", b"", "application/octet-stream")}).status_code == 400
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", "<w/>")
    assert api.post("/templates", files={"file": ("doc.pptx", buf.getvalue(), "application/octet-stream")}).status_code == 400
    big = tmp_path / "big.pptx"
    with big.open("wb") as fh:
        fh.truncate(MAX_TEMPLATE_BYTES + 1)
    with pytest.raises(Exception):
        check_pptx(big)
    # тот же файл под другим именем — один id; спецсимволы в имени не ломают путь
    holdout = template_path("ЛЦТ2026")
    ids = set()
    for name in ("🚀🚀.pptx", "../../x.pptx", "   .pptx"):
        with holdout.open("rb") as fh:
            r = api.post("/templates", files={"file": (name, fh, "application/octet-stream")})
        assert r.status_code == 200, r.text
        ids.add(r.json()["id"])
    assert len(ids) == 1
    # /audit с не-pptx → 400, а загруженный файл после аудита не остаётся на диске
    vk = _vk_tech(api)
    with holdout.open("rb") as fh:
        r = api.post("/audit", data={"template_id": vk["id"]}, files={"deck": ("d.pptx", fh, "application/octet-stream")})
    assert r.status_code == 200
    audits = Path(api.app.state.df.root) / "audits"
    assert not list(audits.glob("*.pptx"))


def test_generate_with_repo_context(tmp_path: Path) -> None:
    """`repo` — .zip репозитория: job готовит контекст до прогона, колоды собираются по фактам (content_source: context)."""
    import io
    import json
    import zipfile

    from tests.test_context import _digest_answer, _repo

    app = create_app(root=tmp_path / "api", executor=None, client_factory=lambda: FakeClient(
        cassette("outline_writer_pulse"), by_skill={"speaker_notes": [fake_notes], "context_digest": [_digest_answer]}))
    repo = _repo(tmp_path / "pulse-main", git=False)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for p in repo.rglob("*"):
            if p.is_file():
                z.write(p, p.relative_to(tmp_path).as_posix())
    with TestClient(app) as api:
        vk = _vk_tech(api)
        base = {"template_id": vk["id"], "strategies": "executive", "judge": False, "autofix": False,
                "render_png": False, "export": "pptx", "topic": "Питч «Пульса» на 7 минут"}
        r = api.post("/generate", data=base, files={"repo": ("pulse.zip", buf.getvalue(), "application/zip")})
        assert r.status_code == 202, r.text
        job = api.get(f"/jobs/{r.json()['id']}").json()
        assert job["status"] == "done", job.get("error")
        run_json = json.loads(api.get(f"/jobs/{job['id']}/files/run.json").content)
        assert run_json["content_source"] == "context"
        assert api.get(f"/jobs/{job['id']}/files/brief.md").text.startswith("# Питч «Пульса»")
        assert list((tmp_path / "api" / "contexts").glob("*.json"))  # кэш — в папке API, а не в out/ репозитория

        bad = api.post("/generate", data=base, files={"repo": ("x.zip", b"not a zip", "application/zip")})
        assert bad.status_code == 400 and "zip" in bad.text
