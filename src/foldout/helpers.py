"""Small, reusable helpers shared across foldout modules.

This module exists so we can avoid list comprehensions and lambdas (per
the project style guide) without sprinkling the same `for` loops all
over the codebase.
"""

from __future__ import annotations

from typing import Any, Iterable


def pick(rows: Iterable[tuple[Any, ...]],
         *field_names: str) -> list[dict[str, Any]]:
    """Convert positional cursor rows into a list of named dicts.

    Example:
        cur.execute("SELECT oid, datname FROM pg_database")
        return pick(cur.fetchall(), "oid", "name")

    Equivalent in intent to a list comprehension like
    `[{"oid": r[0], "name": r[1]} for r in rows]`, but written as an
    explicit loop so it's easier to read at a glance and consistent
    with the project's no-comprehensions style rule.

    Args:
        rows: Any iterable of positional tuples (typically a DB cursor's
            `fetchall()` result).
        *field_names: Field names to assign to each column, in order. The
            number of names should match the row width; extra columns
            beyond the supplied names are ignored.

    Returns:
        A list of dicts with one entry per row.
    """
    out: list[dict[str, Any]] = []
    for row in rows:
        item: dict[str, Any] = {}
        for index, name in enumerate(field_names):
            item[name] = row[index]
        out.append(item)
    return out
