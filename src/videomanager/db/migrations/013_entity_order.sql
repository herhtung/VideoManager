ALTER TABLE entities ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0;
WITH ordered AS (
    SELECT id, ROW_NUMBER() OVER (PARTITION BY type ORDER BY name COLLATE NOCASE,id) - 1 AS position
    FROM entities
)
UPDATE entities SET sort_order = (SELECT position FROM ordered WHERE ordered.id=entities.id);
CREATE INDEX ix_entities_type_order ON entities(type,sort_order);
