CREATE TABLE IF NOT EXISTS users (
    id          BIGSERIAL PRIMARY KEY,
    line_user_id TEXT UNIQUE NOT NULL,
    name        TEXT NOT NULL,
    region      TEXT NOT NULL,
    birthdate   TEXT NOT NULL,
    created_at  TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
