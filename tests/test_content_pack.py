"""Контент-пакет: фрагменты с идентификаторами из brief.md / *.md / data/*.json / data/*.csv."""

from pathlib import Path

from deckforge.content import load_content_pack

REPO = Path(__file__).resolve().parents[1]
PACK = REPO / "examples" / "content_pack"


def test_example_pack_fragments() -> None:
    pack = load_content_pack(PACK)
    by_id = {f.id: f for f in pack.fragments}
    assert "brief" in by_id and by_id["brief"].kind == "text" and "Пульс команды" in pack.brief
    assert by_id["m:coordination_hours"].kind == "series"
    assert by_id["m:time_structure"].kind == "table"
    assert by_id["m:quote"].kind == "quote"
    assert by_id["m:overload_share"].kind == "metric"
    assert by_id["m:response_days"].kind == "metric"  # before/after без value
    assert "doc:product" in by_id and any(i.startswith("doc:product:") for i in by_id)
    assert any(i.startswith("brief:") for i in by_id)  # секции брифа по **жирным** заголовкам


def test_prompt_text_has_all_data_ids_but_not_brief() -> None:
    pack = load_content_pack(PACK)
    text, warnings = pack.to_prompt_text()
    assert not warnings
    for frag in pack.fragments:
        if frag.id == "brief" or frag.id.startswith("brief:"):
            assert f"[{frag.id}]" not in text  # бриф идёт отдельным входом скилла
        else:
            assert f"[{frag.id}]" in text
    assert '"series"' in text and "Пилотные команды" in text


def test_prompt_text_truncates_with_warning() -> None:
    pack = load_content_pack(PACK)
    text, warnings = pack.to_prompt_text(limit=300)
    assert len(text) <= 300 and warnings and "обрезан" in warnings[0]


def test_csv_and_plain_md(tmp_path: Path) -> None:
    (tmp_path / "brief.md").write_text("# Бриф\n\nТекст брифа.\n", "utf-8")
    (tmp_path / "notes.md").write_text("# Заметки\n\n## Раздел A\n\nодин\n\n## Раздел B\n\nдва\n", "utf-8")
    (tmp_path / "data").mkdir()
    rows = "\n".join(["месяц,значение"] + [f"m{i},{i}" for i in range(40)])
    (tmp_path / "data" / "sales.csv").write_text(rows, "utf-8")
    pack = load_content_pack(tmp_path)
    ids = pack.ids()
    assert {"brief", "doc:notes", "doc:notes:раздел-a", "doc:notes:раздел-b", "csv:sales"} <= ids
    csv_frag = pack.get("csv:sales")
    assert csv_frag and csv_frag.kind == "table" and csv_frag.data["header"] == ["месяц", "значение"]
    assert len(csv_frag.data["rows"]) == 30 and "первые 30" in csv_frag.text
    assert pack.get("brief").text.startswith("# Бриф")
