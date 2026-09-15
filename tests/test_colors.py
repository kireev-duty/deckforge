"""core/colors: контраст, ΔE, модификаторы OOXML; кластеризация и роли без файлов."""

from deckforge.core.colors import apply_color_mods, chroma, contrast_ratio, delta_e
from deckforge.parsing.extract_tokens import COLOR_MERGE_DE, ColorStat, assign_roles, cluster_colors


def test_contrast_extremes() -> None:
    assert abs(contrast_ratio("000000", "FFFFFF") - 21) < 0.01
    assert contrast_ratio("777777", "777777") == 1.0
    assert contrast_ratio("0077FF", "FFFFFF") > 3.0


def test_delta_e_orders_similarity() -> None:
    # accent ЛЦТ размазан по FF0053/FE095F — должны слиться; синий и голубой — нет
    assert delta_e("FF0053", "FE095F") < COLOR_MERGE_DE < delta_e("0077FF", "00AEE8")
    assert delta_e("8F8F8F", "798492") > COLOR_MERGE_DE  # серый и серо-синий — разные muted


def test_chroma_separates_neutral_from_chromatic() -> None:
    assert chroma("8F8F8F") < 1
    assert chroma("EBF3F9") < 12  # почти белый — нейтральный, хотя HSL-насыщенность у него > 0.5
    assert chroma("0077FF") > 20


def test_apply_color_mods() -> None:
    grey = apply_color_mods("000000", {"lumMod": 65000, "lumOff": 35000})
    assert grey == "595959"
    assert apply_color_mods("0077FF", {"tint": 50000}) == "80BBFF"
    assert apply_color_mods("FFFFFF", {"shade": 50000}) == "808080"
    assert apply_color_mods("0077FF", {}) == "0077FF"


def _stat(**kw) -> ColorStat:
    return ColorStat(**kw)


def test_cluster_snaps_to_theme_color() -> None:
    colors = {"FE095F": _stat(fill_count=50), "FF0053": _stat(fill_count=14, line_count=12), "0077FF": _stat(fill_count=5)}
    clusters = cluster_colors(colors, theme_values={"FF0053"})
    assert set(clusters) == {"FF0053", "0077FF"}
    assert clusters["FF0053"].fill_count == 64
    assert sorted(clusters["FF0053"].members) == ["FE095F", "FF0053"]


def test_assign_roles_dark_template() -> None:
    clusters = {
        "000000": _stat(bg_slides=29, fill_count=34, fill_area=1.1),
        "FFFFFF": _stat(text_chars=667, fill_count=2),
        "0077FF": _stat(fill_count=30, text_chars=310, line_count=5),
        "00AEE8": _stat(line_count=13),
        "212121": _stat(fill_count=21, fill_area=1.2),
        "E4E7EA": _stat(text_chars=144),
        "EE5959": _stat(fill_count=6, line_count=10),
    }
    roles = assign_roles(clusters)
    assert roles["000000"] == "background"
    assert roles["FFFFFF"] == "text"
    assert roles["0077FF"] == "accent"
    assert roles["212121"] == "surface"
    assert roles["E4E7EA"] == "muted"
    assert list(roles.values()).count("accent") == 1


def test_accent_falls_back_to_theme_when_no_chromatic_usage() -> None:
    from deckforge.parsing.extract_tokens import Usage, build_color_tokens

    usage = Usage()
    usage.colors["FFFFFF"].bg_slides = 1
    usage.colors["000000"].text_chars = 100
    tokens = build_color_tokens(usage, {"accent1": "4472C4"})
    accent = [t for t in tokens if t.role == "accent"]
    assert [(t.hex, t.source) for t in accent] == [("4472C4", "theme")]


def test_assign_roles_picture_backgrounds_fall_back_to_area() -> None:
    clusters = {"FFFFFF": _stat(fill_count=49, fill_area=5.7), "000000": _stat(text_chars=1200)}
    roles = assign_roles(clusters)
    assert roles == {"FFFFFF": "background", "000000": "text"}
