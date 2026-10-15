CREATE TABLE libraries (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    last_root_path TEXT NOT NULL,
    marker_version INTEGER NOT NULL,
    last_verified_at TEXT NOT NULL
);

CREATE TABLE videos (
    id TEXT PRIMARY KEY,
    library_id TEXT NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
    relative_path TEXT NOT NULL,
    path_key TEXT NOT NULL,
    file_name TEXT NOT NULL,
    display_title TEXT NOT NULL,
    file_size INTEGER NOT NULL CHECK (file_size >= 0),
    mtime_ns INTEGER NOT NULL CHECK (mtime_ns >= 0),
    duration_ms INTEGER CHECK (duration_ms >= 0),
    width INTEGER CHECK (width > 0),
    height INTEGER CHECK (height > 0),
    video_codec TEXT,
    audio_codec TEXT,
    recorded_at TEXT,
    added_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'unprocessed'
        CHECK (status IN ('unprocessed', 'in_progress', 'done')),
    is_favorite INTEGER NOT NULL DEFAULT 0 CHECK (is_favorite IN (0, 1)),
    rating INTEGER CHECK (rating BETWEEN 0 AND 5),
    notes TEXT NOT NULL DEFAULT '',
    missing_since TEXT,
    deleted_at TEXT,
    UNIQUE (library_id, path_key)
);

CREATE INDEX ix_videos_library_status ON videos(library_id, status);
CREATE INDEX ix_videos_missing_since ON videos(missing_since);

CREATE TABLE tag_groups (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE tags (
    id TEXT PRIMARY KEY,
    group_id TEXT NOT NULL REFERENCES tag_groups(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    color TEXT,
    sort_order INTEGER NOT NULL DEFAULT 0,
    UNIQUE (group_id, name)
);

CREATE TABLE video_tags (
    video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    tag_id TEXT NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (video_id, tag_id)
);

CREATE TABLE categories (
    id TEXT PRIMARY KEY,
    parent_id TEXT REFERENCES categories(id) ON DELETE RESTRICT,
    name TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE video_categories (
    video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    category_id TEXT NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    PRIMARY KEY (video_id, category_id)
);

CREATE TABLE saved_filters (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    filter_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE scan_jobs (
    id TEXT PRIMARY KEY,
    library_id TEXT NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    state TEXT NOT NULL,
    result_json TEXT,
    error_message TEXT
);

CREATE TABLE thumbnail_jobs (
    video_id TEXT PRIMARY KEY REFERENCES videos(id) ON DELETE CASCADE,
    state TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    cache_signature TEXT,
    updated_at TEXT NOT NULL,
    error_message TEXT
);

CREATE TABLE file_operations (
    id TEXT PRIMARY KEY,
    video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    type TEXT NOT NULL,
    src_rel_path TEXT NOT NULL,
    dst_rel_path TEXT,
    state TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    error TEXT
);

CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
