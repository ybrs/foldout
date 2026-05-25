CREATE TABLE fld_dbversion (
    version VARCHAR(255) DEFAULT '0'
);

INSERT INTO fld_dbversion (version) VALUES ('0');

CREATE TABLE fld_databases (
    oid INTEGER,
    datname VARCHAR(255),
    parent INTEGER,
    created_at TIMESTAMP,
    type VARCHAR(254)
);

UPDATE fld_dbversion SET version = '1';