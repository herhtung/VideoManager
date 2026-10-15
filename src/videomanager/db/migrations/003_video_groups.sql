CREATE TABLE video_groups (
    id TEXT PRIMARY KEY,
    library_id TEXT NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    folder_rel_path TEXT
);

CREATE TABLE video_group_members (
    video_id TEXT PRIMARY KEY REFERENCES videos(id) ON DELETE CASCADE,
    group_id TEXT NOT NULL REFERENCES video_groups(id) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK (position >= 1),
    UNIQUE (group_id, position)
);

CREATE INDEX ix_video_group_members_group ON video_group_members(group_id, position);
