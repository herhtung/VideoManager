-- Saving details alone no longer changes the organization state.
UPDATE videos SET status = 'unprocessed' WHERE status = 'in_progress';
