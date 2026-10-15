-- Preserve existing classification as tags before retiring unique assignment.
-- Legacy tables remain for old saved filters and rollback compatibility.
INSERT OR IGNORE INTO tag_groups(id, name)
SELECT 'migrated-virtual-categories', '原分類'
WHERE EXISTS (SELECT 1 FROM video_categories);

INSERT OR IGNORE INTO tags(id, group_id, name)
WITH RECURSIVE paths(id, name) AS (
    SELECT id, name FROM categories WHERE parent_id IS NULL
    UNION ALL
    SELECT c.id, paths.name || ' › ' || c.name
    FROM categories c JOIN paths ON c.parent_id = paths.id
)
SELECT 'migrated-category-' || paths.id, g.id, paths.name
FROM paths JOIN tag_groups g ON g.name = '原分類'
WHERE EXISTS (SELECT 1 FROM video_categories vc WHERE vc.category_id = paths.id);

INSERT OR IGNORE INTO video_tags(video_id, tag_id)
SELECT vc.video_id, t.id
FROM video_categories vc
JOIN tags t ON t.id = 'migrated-category-' || vc.category_id;

INSERT OR IGNORE INTO tag_groups(id, name)
SELECT 'migrated-unique-categories', '原唯一分類'
WHERE EXISTS (SELECT 1 FROM videos WHERE unique_category_id IS NOT NULL);

INSERT OR IGNORE INTO tags(id, group_id, name)
SELECT 'migrated-unique-' || u.id, g.id, u.name
FROM unique_categories u JOIN tag_groups g ON g.name = '原唯一分類'
WHERE EXISTS (SELECT 1 FROM videos v WHERE v.unique_category_id = u.id);

INSERT OR IGNORE INTO video_tags(video_id, tag_id)
SELECT v.id, t.id
FROM videos v JOIN unique_categories u ON u.id = v.unique_category_id
JOIN tag_groups g ON g.name = '原唯一分類'
JOIN tags t ON t.group_id = g.id AND t.name = u.name;

-- Saved filters referring to an exclusive category now refer to its new tag.
UPDATE saved_filters
SET filter_json = json_set(
    json_remove(filter_json, '$.unique_category_id'), '$.tag_all',
    json_insert(
        COALESCE(json_extract(filter_json, '$.tag_all'), json('[]')),
        '$[#]', 'migrated-unique-' || json_extract(filter_json, '$.unique_category_id')
    )
)
WHERE json_type(filter_json, '$.unique_category_id') = 'text';

UPDATE videos SET unique_category_id = NULL WHERE unique_category_id IS NOT NULL;
