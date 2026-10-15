CREATE TABLE unique_categories (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
    folder_name TEXT NOT NULL COLLATE NOCASE UNIQUE
);

ALTER TABLE videos ADD COLUMN unique_category_id TEXT REFERENCES unique_categories(id) ON DELETE SET NULL;
CREATE INDEX ix_videos_unique_category ON videos(unique_category_id);

ALTER TABLE file_operations ADD COLUMN set_unique_category INTEGER NOT NULL DEFAULT 0;
ALTER TABLE file_operations ADD COLUMN target_unique_category_id TEXT;
