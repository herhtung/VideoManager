CREATE TABLE relation_types (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL COLLATE NOCASE UNIQUE CHECK (length(trim(name)) > 0),
    sort_order INTEGER NOT NULL CHECK (sort_order BETWEEN 0 AND 2)
);
INSERT INTO relation_types VALUES ('person', '人物', 0), ('studio', '片商', 1);
CREATE TRIGGER relation_types_limit BEFORE INSERT ON relation_types
WHEN (SELECT COUNT(*) FROM relation_types) >= 3
BEGIN SELECT RAISE(ABORT, '最多三種關聯類別'); END;

CREATE TABLE entities_new (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL REFERENCES relation_types(id),
    name TEXT NOT NULL,
    thumbnail_png BLOB,
    UNIQUE (type, name)
);
INSERT INTO entities_new SELECT id, type, name, thumbnail_png FROM entities;
CREATE TABLE video_entities_new (
    video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    entity_id TEXT NOT NULL REFERENCES entities_new(id) ON DELETE CASCADE,
    PRIMARY KEY (video_id, entity_id)
);
INSERT INTO video_entities_new SELECT video_id, entity_id FROM video_entities;
DROP TABLE video_entities;
DROP TABLE entities;
ALTER TABLE entities_new RENAME TO entities;
ALTER TABLE video_entities_new RENAME TO video_entities;
CREATE INDEX ix_video_entities_entity ON video_entities(entity_id, video_id);
