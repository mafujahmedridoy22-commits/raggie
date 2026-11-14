# SPDX-License-Identifier: Apache-2.0

"""Shared SQL helpers for the repositories."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

# A fragment that may be interpolated into SQL text: a lowercase identifier,
# optionally assigned a bind placeholder (`col = $3`, `col = $3::jsonb`), or a
# bare placeholder (`$3`). Values never appear in the text — they are bound.
_SQL_FRAGMENT = re.compile(r"^(?:[a-z_][a-z0-9_]*(?: = \$\d+(?:::[a-z]+)?)?|\$\d+)$")


def compose_sql(template: str, **fragments: Iterable[str]) -> str:
    """Fill ``{slot}`` placeholders in ``template`` with comma-joined fragments
    (column lists, ``col = $n`` assignments, ``$n`` placeholders). The single
    place dynamic SQL text is assembled: every fragment is checked against
    ``_SQL_FRAGMENT`` at runtime, so request data can never reach the SQL text
    even if a caller forgets to allow-list a column name."""
    rendered: dict[str, str] = {}
    for slot, items in fragments.items():
        parts = list(items)
        if not parts or not all(_SQL_FRAGMENT.match(part) for part in parts):
            raise ValueError(f"unsafe SQL fragment for {slot!r}")
        rendered[slot] = ", ".join(parts)
    return template.format_map(rendered)


def patch_set(
    allowed: Iterable[str], fields: Mapping[str, object], lead: list[object]
) -> tuple[list[str], list[object]]:
    """Build the ``col = $n`` SET fragments for a partial update.

    Walks ``allowed`` in order and, for each column present in ``fields``, appends
    the value and a ``col = $n`` placeholder numbered after ``lead``. A key
    present is written verbatim (including ``None`` to clear it); absent keys are
    untouched. Returns ``(sets, vals)``: pass ``sets=sets`` to :func:`compose_sql`
    and bind ``*vals``. ``sets`` is empty when no allowed column was supplied, so
    a caller can fall back to a plain read. A caller adding a trailing assignment
    can append to ``sets`` and ``vals`` before binding.
    """
    vals = list(lead)
    sets: list[str] = []
    for col in allowed:
        if col in fields:
            vals.append(fields[col])
            sets.append(f"{col} = ${len(vals)}")
    return sets, vals
