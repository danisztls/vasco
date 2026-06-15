"""Guard test: config.yaml.template must not reference config keys that don't
exist. The template is hand-maintained (CLAUDE.md asks contributors to keep it
in sync with the *Cfg dataclasses); this fails CI if a section or field is
renamed/removed in code but left dangling in the template.

The template is fully commented out, so we parse it ourselves by indent level:
top-level sections are `# name:` (one space after the hash), their fields are
`#   name: ...` (the YAML 2-space indent). Adapter sections nest one level
deeper under `# adapters:` — `#   <adapter>:` then `#     <field>: ...`. Prose
header/comment lines don't match the lowercase `key:` shape and are ignored.

(`tests/test_config.py::test_template_shows_real_defaults` is the complementary
guard: it de-comments the template and round-trips it to ``Config()``, proving
the shown *values* are the real defaults. Unknown keys are ignored by
``load_config``, so that test can't catch a dangling key — this one does.)
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
    adapter_fields = {
        name: {f.name for f in fields(cls)}
        for name, cls in config_mod._ADAPTER_SECTIONS.items()
    }
    # `answer` is custom-loaded (a provider chain), so it's not in _SECTIONS.
    answer_fields = {f.name for f in fields(config_mod.AnswerCfg)}

    current_section: str | None = None
    current_adapter: str | None = None
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
            assert key in section_fields or key in ("adapters", "domains", "answer"), (
                f"template references unknown section '{key}'"
            )
            current_section = key
            current_adapter = None
        elif current_section == "domains":
            # `domains:` is a free-form host → {headers: …} map, not dataclass
            # fields, so its child keys (hosts) aren't validated here.
            continue
        elif current_section == "answer":  # '#   providers:' under answer
            assert key in answer_fields, (
                f"template field 'answer.{key}' is not a field of AnswerCfg"
            )
            seen_a_field = True
        elif current_section == "adapters" and indent <= 3:  # '#   <adapter>:'
            assert key in adapter_fields, (
                f"template references unknown adapter 'adapters.{key}'"
            )
            current_adapter = key
        elif current_section == "adapters":  # '#     <field>:' under an adapter
            assert current_adapter is not None, (
                f"adapter field '{key}' before any adapter"
            )
            assert key in adapter_fields[current_adapter], (
                f"template field 'adapters.{current_adapter}.{key}' is not a field of "
                f"the {current_adapter} config dataclass"
            )
            seen_a_field = True
        else:  # '#   field:' of a normal section
            assert current_section is not None, f"field '{key}' before any section"
            assert key in section_fields[current_section], (
                f"template field '{current_section}.{key}' is not a field of the "
                f"{current_section} config dataclass"
            )
            seen_a_field = True

    assert current_section is not None, "parsed no sections from the template"
    assert seen_a_field, "parsed no fields from the template"
