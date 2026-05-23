-- Add base-snapshot linkage for 3-way diff.
--
-- A branch can have a "base" snapshot: a COW copy of the parent at branch
-- creation time. The base is used as the merge base for `vka diff` so we
-- can distinguish "branch added X" from "parent removed X".

ALTER TABLE vka_databases ADD COLUMN base_oid INTEGER;

UPDATE vka_dbversion SET version = '3';
