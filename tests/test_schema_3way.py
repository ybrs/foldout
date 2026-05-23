"""Unit tests for the 3-way schema merge logic.

These don't touch a real Postgres — they directly exercise the dict-level
merge_schemas_3way / diff_schemas_3way functions with synthetic schema
dumps. Goal: lock down the behavior for added/dropped/modified tables,
columns, indexes, etc., across the three-way matrix.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from vkarious.page_diff import (
    diff_schemas_3way,
    merge_schemas_3way,
)


def empty_schema():
    return {
        "schemas": [],
        "tables": {},
        "indexes": {},
        "constraints": {},
        "views": {},
        "matviews": {},
        "functions": {},
        "sequences": {},
    }


def table(name, cols, pk=None, schema="public"):
    return {
        "schema": schema,
        "name": name,
        "columns": [
            {"name": c[0], "type": c[1],
             "not_null": c[2] if len(c) > 2 else False,
             "default": c[3] if len(c) > 3 else None,
             "identity": c[4] if len(c) > 4 else None}
            for c in cols
        ],
        "primary_key": list(pk) if pk else None,
    }


# ---------- the core 3-way matrix ----------

def test_branch_adds_table_main_unchanged():
    base = empty_schema()
    main = empty_schema()
    branch = empty_schema()
    branch["tables"]["public.t1"] = table("t1", [("id", "integer", True)], pk=["id"])
    pre, post, conflicts, drifts = diff_schemas_3way(base, main, branch)
    assert not conflicts and not drifts, (conflicts, drifts)
    assert any("CREATE TABLE" in s and "t1" in s for s in pre), pre


def test_branch_adds_t1_main_adds_t2():
    """The motivating case: parent independently added a different table.
    We should NOT emit DROP TABLE for main's t2; we SHOULD emit CREATE for t1.
    """
    base = empty_schema()
    main = empty_schema()
    main["tables"]["public.t2"] = table("t2", [("id", "integer", True)], pk=["id"])
    branch = empty_schema()
    branch["tables"]["public.t1"] = table("t1", [("id", "integer", True)], pk=["id"])
    pre, post, conflicts, drifts = diff_schemas_3way(base, main, branch)
    assert not conflicts, conflicts
    assert any("CREATE TABLE" in s and "t1" in s for s in pre), pre
    assert not any("DROP TABLE" in s for s in pre + post), (pre, post)
    assert any(d["kind"] == "table" and d["key"] == "public.t2" for d in drifts), drifts


def test_branch_drops_table_main_unchanged():
    base = empty_schema()
    base["tables"]["public.gone"] = table("gone", [("id", "integer", True)], pk=["id"])
    main = empty_schema()
    main["tables"]["public.gone"] = table("gone", [("id", "integer", True)], pk=["id"])
    branch = empty_schema()
    pre, post, conflicts, drifts = diff_schemas_3way(base, main, branch)
    assert not conflicts, conflicts
    assert any("DROP TABLE" in s and "gone" in s for s in post), post


def test_branch_drops_table_main_modified_it():
    base = empty_schema()
    base["tables"]["public.t"] = table("t", [("id", "integer", True)], pk=["id"])
    main = empty_schema()
    main["tables"]["public.t"] = table("t", [("id", "integer", True), ("x", "integer")], pk=["id"])
    branch = empty_schema()
    pre, post, conflicts, drifts = diff_schemas_3way(base, main, branch)
    assert any(c["kind"] == "table" and c["key"] == "public.t" for c in conflicts), conflicts
    # On conflict, do not drop
    assert not any("DROP TABLE" in s for s in pre + post), (pre, post)


def test_both_add_same_column_same_def():
    base = empty_schema()
    base["tables"]["public.u"] = table("u", [("id", "integer", True)], pk=["id"])
    main = empty_schema()
    main["tables"]["public.u"] = table("u", [("id", "integer", True), ("score", "integer")], pk=["id"])
    branch = empty_schema()
    branch["tables"]["public.u"] = table("u", [("id", "integer", True), ("score", "integer")], pk=["id"])
    pre, post, conflicts, drifts = diff_schemas_3way(base, main, branch)
    assert not conflicts, conflicts
    # main already has the column; no DDL needed
    assert not pre and not post, (pre, post)


def test_both_add_different_columns_to_same_table():
    """Branch adds col x; main adds col y. Compatible — both columns should
    end up on main with no conflict."""
    base = empty_schema()
    base["tables"]["public.u"] = table("u", [("id", "integer", True)], pk=["id"])
    main = empty_schema()
    main["tables"]["public.u"] = table("u", [("id", "integer", True), ("y", "integer")], pk=["id"])
    branch = empty_schema()
    branch["tables"]["public.u"] = table("u", [("id", "integer", True), ("x", "integer")], pk=["id"])
    pre, post, conflicts, drifts = diff_schemas_3way(base, main, branch)
    assert not conflicts, conflicts
    # x must be added; y must NOT be dropped
    assert any("ADD COLUMN" in s and '"x"' in s for s in pre), pre
    assert not any("DROP COLUMN" in s for s in pre + post), (pre, post)


def test_both_add_same_column_different_types_is_conflict():
    base = empty_schema()
    base["tables"]["public.u"] = table("u", [("id", "integer", True)], pk=["id"])
    main = empty_schema()
    main["tables"]["public.u"] = table("u", [("id", "integer", True), ("v", "integer")], pk=["id"])
    branch = empty_schema()
    branch["tables"]["public.u"] = table("u", [("id", "integer", True), ("v", "text")], pk=["id"])
    pre, post, conflicts, drifts = diff_schemas_3way(base, main, branch)
    assert any(c["kind"] == "column" and c["key"] == "public.u.v" for c in conflicts), conflicts


def test_branch_adds_index_main_unchanged():
    base = empty_schema()
    base["tables"]["public.u"] = table("u", [("id", "integer", True), ("x", "integer")], pk=["id"])
    main = empty_schema()
    main["tables"]["public.u"] = table("u", [("id", "integer", True), ("x", "integer")], pk=["id"])
    branch = empty_schema()
    branch["tables"]["public.u"] = table("u", [("id", "integer", True), ("x", "integer")], pk=["id"])
    branch["indexes"]["public.u_x_idx"] = {
        "schema": "public", "name": "u_x_idx", "table": "u",
        "definition": "CREATE INDEX u_x_idx ON public.u USING btree (x)",
    }
    pre, post, conflicts, drifts = diff_schemas_3way(base, main, branch)
    assert not conflicts
    assert any("u_x_idx" in s for s in pre), pre


def test_main_drift_only():
    """Parent added something; branch did nothing. No CREATE/DROP emitted;
    drift is reported informatively."""
    base = empty_schema()
    main = empty_schema()
    main["tables"]["public.parent_added"] = table("parent_added", [("id", "integer", True)], pk=["id"])
    branch = empty_schema()
    pre, post, conflicts, drifts = diff_schemas_3way(base, main, branch)
    assert not conflicts
    assert not pre and not post, (pre, post)
    assert any(d["kind"] == "table" and d["key"] == "public.parent_added" for d in drifts), drifts


# ---------- entry point ----------

ALL = [
    test_branch_adds_table_main_unchanged,
    test_branch_adds_t1_main_adds_t2,
    test_branch_drops_table_main_unchanged,
    test_branch_drops_table_main_modified_it,
    test_both_add_same_column_same_def,
    test_both_add_different_columns_to_same_table,
    test_both_add_same_column_different_types_is_conflict,
    test_branch_adds_index_main_unchanged,
    test_main_drift_only,
]


def main():
    failures = 0
    for t in ALL:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"  FAIL  {t.__name__}: {e}")
        except Exception as e:
            failures += 1
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
    if failures:
        print(f"{failures}/{len(ALL)} failed")
        sys.exit(1)
    print(f"all {len(ALL)} schema 3-way tests passed")


if __name__ == "__main__":
    main()
