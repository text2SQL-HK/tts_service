-- Schema for tts_service (PostgreSQL)
-- Source: implementation_plan.md

CREATE TABLE IF NOT EXISTS sessions (
    session_id BIGSERIAL PRIMARY KEY,
    user_id VARCHAR(255) NOT NULL,
    current_db_id VARCHAR(255) NOT NULL,
    db_list JSONB NOT NULL DEFAULT '[]'::jsonb,
    sql_exec_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_accessed TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS sessions_user_last_accessed_idx
    ON sessions (user_id, last_accessed);

CREATE TABLE IF NOT EXISTS chat_messages (
    id BIGSERIAL PRIMARY KEY,
    session_id BIGINT NOT NULL REFERENCES sessions(session_id),
    role VARCHAR(20) NOT NULL,
    content TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS chat_messages_session_created_at_idx
    ON chat_messages (session_id, created_at);

CREATE TABLE IF NOT EXISTS tool_calls (
    id BIGSERIAL PRIMARY KEY,
    session_id BIGINT NOT NULL REFERENCES sessions(session_id),
    chat_message_id BIGINT REFERENCES chat_messages(id),
    tool_name VARCHAR(100) NOT NULL,
    arguments JSONB,
    result JSONB,
    status VARCHAR(20) NOT NULL DEFAULT 'success',
    csv_file_path VARCHAR(500),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS tool_calls_session_created_at_idx
    ON tool_calls (session_id, created_at);

CREATE INDEX IF NOT EXISTS tool_calls_chat_message_id_idx
    ON tool_calls (chat_message_id);

CREATE TABLE IF NOT EXISTS system_events (
    id BIGSERIAL PRIMARY KEY,
    session_id BIGINT NOT NULL REFERENCES sessions(session_id),
    event_type VARCHAR(50) NOT NULL,
    content JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS system_events_session_created_at_idx
    ON system_events (session_id, created_at);

CREATE TABLE IF NOT EXISTS context_notes (
    id BIGSERIAL PRIMARY KEY,
    session_id BIGINT NOT NULL REFERENCES sessions(session_id),
    note_type VARCHAR(20) NOT NULL,
    topic VARCHAR(500),
    content JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS context_notes_session_created_at_idx
    ON context_notes (session_id, created_at);
