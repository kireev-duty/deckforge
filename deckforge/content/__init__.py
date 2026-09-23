"""content: бриф (свой, по теме или выведенный из шаблона) + контент-пакет → DeckOutline."""

from deckforge.content.content_pack import ContentPack, Fragment, load_content_pack
from deckforge.content.outline_writer import OutlineResult, archetypes_prompt, repair_outline, write_outline
from deckforge.content.template_brief import (
    TemplateBrief,
    drop_unsourced_numbers,
    pack_from_brief,
    template_digest,
    write_template_brief,
)

__all__ = [
    "ContentPack", "Fragment", "OutlineResult", "TemplateBrief", "archetypes_prompt", "drop_unsourced_numbers",
    "load_content_pack", "pack_from_brief", "repair_outline", "template_digest", "write_outline",
    "write_template_brief",
]
