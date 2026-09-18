ALTER TABLE source_revision
ADD COLUMN indexed_parser_version TEXT;

ALTER TABLE source_revision
ADD COLUMN indexed_config_hash TEXT;
