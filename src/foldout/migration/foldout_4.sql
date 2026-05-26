-- Move the per-branch page-index ("what did the branch look like at
-- branch time, used to filter `foldout diff`") from ~/.foldout/snapshots/*.json
-- into the foldout metadata database.
--
-- Reasons (see TASKS.md): cleanup is now atomic with branch deletion,
-- no $HOME isolation needed in tests, no orphan JSON files when a
-- branch DB is dropped behind foldout's back, and metadata is shared
-- across machines that point at the same cluster.
--
-- Two kinds of page-index per branch:
--   'branch'  — state of the branch's relations at branch creation
--   'parent'  — state of the parent's relations at branch creation
--               (used by 3-way diff so the parent side can also be
--               stat-skipped where it hasn't drifted)

CREATE TABLE fld_page_index (
    branch_oid    INTEGER NOT NULL,
    kind          TEXT    NOT NULL CHECK (kind IN ('branch', 'parent')),
    dbname        TEXT    NOT NULL,
    lsn           TEXT    NOT NULL,
    xid_snapshot  TEXT,
    captured_at   TIMESTAMP NOT NULL DEFAULT now(),
    relations     JSONB   NOT NULL,
    PRIMARY KEY (branch_oid, kind)
);

UPDATE fld_dbversion SET version = '4';
