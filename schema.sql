CREATE TABLE IF NOT EXISTS users (
    id           BIGSERIAL PRIMARY KEY,
    line_user_id TEXT UNIQUE NOT NULL,
    name         TEXT NOT NULL,
    region       TEXT NOT NULL,
    birthdate    TEXT NOT NULL,
    created_at   TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS messages (
    id           BIGSERIAL PRIMARY KEY,
    line_user_id TEXT NOT NULL,
    role         TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content      TEXT NOT NULL,
    created_at   TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
