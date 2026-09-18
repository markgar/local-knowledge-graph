CREATE TABLE blocker (
    record_id TEXT PRIMARY KEY,
    anchor_id TEXT NOT NULL REFERENCES source_anchor(anchor_id),
    text TEXT NOT NULL,
    event_time TEXT
);

CREATE TABLE conflict (
    record_id TEXT PRIMARY KEY,
    anchor_id TEXT NOT NULL REFERENCES source_anchor(anchor_id),
    text TEXT NOT NULL,
    event_time TEXT
);

CREATE INDEX decision_anchor_idx ON decision(anchor_id);
CREATE INDEX blocker_anchor_idx ON blocker(anchor_id);
CREATE INDEX conflict_anchor_idx ON conflict(anchor_id);
