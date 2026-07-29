-- Hermes Game Host Console durable operation store schema, version 1.
CREATE TABLE operations (
    operation_id TEXT PRIMARY KEY,
    game_id TEXT NOT NULL,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    source TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN ('queued', 'running', 'succeeded', 'failed', 'cancelled', 'outcome_unknown')
    ),
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    output TEXT,
    precondition_json TEXT,
    postcondition_json TEXT,
    recovery_note TEXT
);

CREATE INDEX operations_created_at_idx
    ON operations(created_at DESC, operation_id DESC);
CREATE INDEX operations_game_state_created_idx
    ON operations(game_id, state, created_at DESC);

PRAGMA user_version = 1;
