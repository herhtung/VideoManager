CREATE TABLE compression_profiles (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    encoder TEXT NOT NULL,
    quality_mode TEXT NOT NULL,
    quality_value INTEGER NOT NULL,
    speed_preset TEXT NOT NULL,
    resolution_mode TEXT NOT NULL DEFAULT 'source',
    fps_mode TEXT NOT NULL DEFAULT 'source',
    audio_mode TEXT NOT NULL DEFAULT 'auto',
    built_in INTEGER NOT NULL DEFAULT 0 CHECK (built_in IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE compression_jobs (
    id TEXT PRIMARY KEY,
    video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    library_id TEXT NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
    source_rel_path TEXT NOT NULL,
    output_rel_path TEXT,
    profile_snapshot_json TEXT NOT NULL,
    queue_order INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL CHECK (status IN
        ('queued','analyzing','analyzed','encoding','validating','completed','cancelled','failed','replaced')),
    source_size INTEGER NOT NULL,
    source_mtime_ns INTEGER NOT NULL,
    estimated_size_min INTEGER,
    estimated_size_mid INTEGER,
    estimated_size_max INTEGER,
    actual_size INTEGER,
    estimated_reduction_pct REAL,
    actual_reduction_pct REAL,
    sample_quality_metric TEXT,
    sample_quality_score REAL,
    sample_quality_details_json TEXT,
    progress REAL NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 100),
    error_code TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX ix_compression_jobs_library_status ON compression_jobs(library_id, status, queue_order);
CREATE INDEX ix_compression_jobs_video ON compression_jobs(video_id, created_at);

CREATE TABLE compression_samples (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES compression_jobs(id) ON DELETE CASCADE,
    start_seconds REAL NOT NULL,
    duration_seconds REAL NOT NULL,
    encoded_video_bytes INTEGER NOT NULL,
    quality_metric TEXT,
    quality_score REAL,
    sample_stats_json TEXT NOT NULL
);
CREATE INDEX ix_compression_samples_job ON compression_samples(job_id, start_seconds);

CREATE TABLE video_variants (
    id TEXT PRIMARY KEY,
    source_video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    library_id TEXT NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
    rel_path TEXT NOT NULL,
    codec TEXT,
    size_bytes INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    active_variant INTEGER NOT NULL DEFAULT 0 CHECK (active_variant IN (0, 1)),
    original_preserved INTEGER NOT NULL DEFAULT 1 CHECK (original_preserved IN (0, 1)),
    UNIQUE(library_id, rel_path)
);
CREATE INDEX ix_video_variants_source ON video_variants(source_video_id);

CREATE TABLE compression_replacements (
    job_id TEXT PRIMARY KEY REFERENCES compression_jobs(id) ON DELETE CASCADE,
    source_rel_path TEXT NOT NULL,
    output_rel_path TEXT NOT NULL,
    preserved_rel_path TEXT NOT NULL,
    state TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    error_message TEXT
);

INSERT INTO compression_profiles
    (id,name,encoder,quality_mode,quality_value,speed_preset,built_in,created_at,updated_at)
VALUES
    ('builtin-fidelity','極致保真','libx265','crf',18,'slow',1,datetime('now'),datetime('now')),
    ('builtin-high','高品質','libx265','crf',20,'medium',1,datetime('now'),datetime('now')),
    ('builtin-balanced','一般（平衡）','libx265','crf',23,'medium',1,datetime('now'),datetime('now')),
    ('builtin-small','高壓縮','libx265','crf',27,'medium',1,datetime('now'),datetime('now')),
    ('builtin-minimum','極限壓縮','libx265','crf',30,'medium',1,datetime('now'),datetime('now'));

CREATE TABLE video_edits (
    id TEXT PRIMARY KEY,
    video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    library_id TEXT NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
    source_rel_path TEXT NOT NULL,
    output_rel_path TEXT,
    source_size INTEGER NOT NULL,
    source_mtime_ns INTEGER NOT NULL,
    cut_start REAL NOT NULL,
    cut_end REAL NOT NULL,
    original_duration REAL,
    output_duration REAL,
    output_size INTEGER,
    status TEXT NOT NULL CHECK (status IN ('queued','running','validating','completed','cancelled','failed','replaced')),
    progress REAL NOT NULL DEFAULT 0,
    error_message TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX ix_video_edits_library ON video_edits(library_id,created_at);

CREATE TABLE edit_replacements (
    edit_id TEXT PRIMARY KEY REFERENCES video_edits(id) ON DELETE CASCADE,
    source_rel_path TEXT NOT NULL,
    output_rel_path TEXT NOT NULL,
    preserved_rel_path TEXT NOT NULL,
    state TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    error_message TEXT
);
