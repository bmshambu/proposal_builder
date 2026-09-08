"""ppt_gen_v2 engine — deterministic deck assembly from an owned library.

    from engine import Template, import_deck, build_template

No AI in this path, no network, no Python dependencies: same reproducibility and
auditability guarantee as v1. Adding a template is a file operation — see
`engine/template.py`.

Slide previews are drawn in-process by `engine/svg.py` — no LibreOffice, no
PowerPoint, nothing to install on the server.
"""
from .assemble import AssemblyError, build, build_template
from .library import Library, LibraryError
from .rules import Rules, RulesError
from .svg import block_card, render_block, render_slide, render_template
from .template import (Template, TemplateError, find_template, import_deck,
                       list_templates)

__all__ = ["build", "build_template", "Library", "Rules", "Template",
           "import_deck", "list_templates", "find_template",
           "render_slide", "render_block", "render_template", "block_card",
           "AssemblyError", "LibraryError", "RulesError", "TemplateError"]
