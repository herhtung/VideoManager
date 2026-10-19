CREATE TABLE entities (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL CHECK (type IN ('person', 'studio')),
    name TEXT NOT NULL,
    UNIQUE (type, name)
);

CREATE TABLE video_entities (
    video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    entity_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    PRIMARY KEY (video_id, entity_id)
);

CREATE INDEX ix_video_entities_entity ON video_entities(entity_id, video_id);
