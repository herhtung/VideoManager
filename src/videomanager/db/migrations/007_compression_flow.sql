ALTER TABLE compression_jobs ADD COLUMN encode_queued INTEGER NOT NULL DEFAULT 0
    CHECK (encode_queued IN (0, 1));
CREATE INDEX ix_compression_jobs_ready ON compression_jobs(library_id, status, encode_queued, queue_order);
