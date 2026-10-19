CREATE TABLE trash_entries (
    video_id TEXT PRIMARY KEY REFERENCES videos(id) ON DELETE CASCADE,
    original_rel_path TEXT NOT NULL,
    trash_rel_path TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK (state IN ('moving','active','restoring','purging')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX ix_trash_entries_state ON trash_entries(state, created_at);
