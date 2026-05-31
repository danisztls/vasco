"""Guard test: config.yaml.template must not reference config keys that don't
exist. The template is hand-maintained (CLAUDE.md asks contributors to keep it
in sync with the *Cfg dataclasses); this fails CI if a section or field is
renamed/removed in code but left dangling in the template.

The template is fully commented out, so we parse it ourselves: section headers
are `# name:` (one space after the hash), fields are `#   name: ...` (the YAML
2-space indent). Prose header lines start with a capital letter and are ignored.
"""

from __future__ import annotations

import re
from dataclasses import fields
from pathlib import Path

from vasco import config as config_mod

_KEY_RE = re.compile(r"^([a-z_][a-z0-9_]*):")
_TEMPLATE = Path(__file__).resolve().parent.parent / "config.yaml.template"


def test_template_keys_are_real_config_fields() -> None:
    section_fields = {
        name: {f.name for f in fields(cls)}
        for name, cls in config_mod._SECTIONS.items()
    }

    current_section: str | None = None
    seen_a_field = False
    for raw in _TEMPLATE.read_text().splitlines():
        if not raw.lstrip().startswith("#"):
            continue
        body = raw.lstrip()[1:]  # text after the leading '#'
        indent = len(body) - len(body.lstrip(" "))
        match = _KEY_RE.match(body.strip())
        if not match:
            continue
        key = match.group(1)
        if indent <= 1:  # '# section:'
            assert key in section_fields, f"template references unknown section '{key}'"
            current_section = key
        else:  # '#   field:'
            assert current_section is not None, f"field '{key}' before any section"
            assert key in section_fields[current_section], (
                f"template field '{current_section}.{key}' is not a field of the "
                f"{current_section} config dataclass"
            )
            seen_a_field = True

    assert current_section is not None, "parsed no sections from the template"
    assert seen_a_field, "parsed no fields from the template"
