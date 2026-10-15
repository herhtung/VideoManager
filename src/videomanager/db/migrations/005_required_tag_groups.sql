ALTER TABLE tag_groups
ADD COLUMN is_required INTEGER NOT NULL DEFAULT 0 CHECK (is_required IN (0, 1));
