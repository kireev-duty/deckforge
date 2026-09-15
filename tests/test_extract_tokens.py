"""Токены на 4 шаблонах датасета: основной шрифт и accent — из использования, не из темы."""

import pytest

from deckforge.core.colors import contrast_ratio
from deckforge.parsing import TemplateTokens, extract_tokens
from deckforge.parsing.ooxml import Package

TEMPLATES = [
    # (подстрока имени файла, основной шрифт, accent)
    ("VK Tech", "Play", "0077FF"),
    ("VK_WorkSpace", "Play", "0077FF"),
    ("VK Education", "Play", "0077FF"),
    ("ЛЦТ2026", "Montserrat", "FF0053"),
]


@pytest.fixture(scope="module")
def tokens_cache() -> dict[str, TemplateTokens]:
    return {}


@pytest.fixture
def tokens(template_path, tokens_cache, request) -> TemplateTokens:
    name = request.param
    if name not in tokens_cache:
        tokens_cache[name] = extract_tokens(template_path(name))
    return tokens_cache[name]


def _params():
    return [pytest.param(n, f, a, id=n) for n, f, a in TEMPLATES]


@pytest.mark.parametrize("tokens,font,accent", _params(), indirect=["tokens"])
def test_main_font_and_accent(tokens: TemplateTokens, font: str, accent: str) -> None:
    assert tokens.fonts[0] == font
    assert tokens.palette("accent") == [accent]


@pytest.mark.parametrize("tokens,font,accent", _params(), indirect=["tokens"])
def test_roles_are_unique_and_readable(tokens: TemplateTokens, font: str, accent: str) -> None:
    for role in ("background", "text", "accent"):
        assert len(tokens.palette(role)) == 1, role
    bg, text = tokens.palette("background")[0], tokens.palette("text")[0]
    assert contrast_ratio(text, bg) >= 4.5
    assert len({c.hex for c in tokens.colors}) == len(tokens.colors)


@pytest.mark.parametrize("tokens,font,accent", _params(), indirect=["tokens"])
def test_typography_scale(tokens: TemplateTokens, font: str, accent: str) -> None:
    roles = [t.role for t in tokens.typography]
    assert "body" in roles and "h1" in roles
    assert len(roles) == len(set(roles))
    sizes = [t.size_pt for t in tokens.typography]
    assert sizes == sorted(sizes, reverse=True)
    assert tokens.type_token("body").font == font
    assert tokens.type_token("h1").size_pt > tokens.type_token("body").size_pt


@pytest.mark.parametrize("tokens,font,accent", _params(), indirect=["tokens"])
def test_theme_and_stats(tokens: TemplateTokens, font: str, accent: str, template_path, request) -> None:
    assert tokens.theme_colors.get("accent1")
    assert tokens.theme_fonts["minor"]
    assert tokens.stats["slides"] > 0 and tokens.stats["text_chars"] > 0
    pkg = Package(template_path(request.node.callspec.params["tokens"]))
    assert (tokens.slide_w, tokens.slide_h) == pkg.slide_size
    assert len(tokens.template_id) == 12


@pytest.mark.parametrize("tokens,font,accent", _params(), indirect=["tokens"])
def test_dna_kwargs_shape(tokens: TemplateTokens, font: str, accent: str) -> None:
    kw = tokens.dna_kwargs()
    assert "theme_fonts" not in kw
    assert {"colors", "typography", "fonts", "embedded_fonts", "theme_colors", "stats", "slide_w", "slide_h"} <= set(kw)


# ── точечные факты, известные из разведки датасета ──


@pytest.mark.parametrize("tokens", ["VK_WorkSpace"], indirect=True)
def test_workspace_is_dark(tokens: TemplateTokens) -> None:
    assert tokens.palette("background") == ["000000"]
    assert tokens.palette("text") == ["FFFFFF"]
    assert tokens.embedded_fonts == ["Play"]
    assert tokens.theme_fonts == {"major": "Arial", "minor": "Arial"}  # тема врёт — и это нормально


@pytest.mark.parametrize("tokens", ["VK Tech"], indirect=True)
def test_vk_tech_muted_grey(tokens: TemplateTokens) -> None:
    assert tokens.palette("muted")[0] in {"8F8F8F", "798492"}


@pytest.mark.parametrize("tokens", ["ЛЦТ2026"], indirect=True)
def test_lct_resolves_theme_font_refs_and_picture_backgrounds(tokens: TemplateTokens) -> None:
    assert "+mn-lt" not in tokens.fonts and "+mj-lt" not in tokens.fonts
    assert tokens.stats["picture_backgrounds"] > 30
    assert "520977" in tokens.palette("secondary") + tokens.palette("unknown")
