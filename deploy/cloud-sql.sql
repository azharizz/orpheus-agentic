CREATE TABLE IF NOT EXISTS orpheus_projects (
    project_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    document JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS orpheus_projects_owner_updated
    ON orpheus_projects(owner_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS orpheus_receipts (
    project_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    receipt_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(project_id, receipt_id)
);
CREATE INDEX IF NOT EXISTS orpheus_receipts_owner_project
    ON orpheus_receipts(owner_id, project_id, created_at DESC);

CREATE TABLE IF NOT EXISTS orpheus_runs (
    project_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    run_key TEXT NOT NULL,
    status TEXT NOT NULL,
    operation TEXT,
    cancel_requested BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(project_id, run_key)
);
CREATE INDEX IF NOT EXISTS orpheus_runs_active
    ON orpheus_runs(owner_id, project_id, updated_at DESC)
    WHERE status IN ('submitted', 'running');
CREATE UNIQUE INDEX IF NOT EXISTS orpheus_runs_one_active
    ON orpheus_runs(project_id)
    WHERE status IN ('submitted', 'running');
