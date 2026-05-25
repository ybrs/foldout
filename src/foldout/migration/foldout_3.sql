-- Add base-snapshot linkage for 3-way diff.
--
-- A branch can have a "base" snapshot: a COW copy of the parent at branch
-- creation time. The base is used as the merge base for `fld diff` so we
-- can distinguish "branch added X" from "parent removed X".

ALTER TABLE fld_databases ADD COLUMN base_oid INTEGER;

UPDATE fld_dbversion SET version = '3';
