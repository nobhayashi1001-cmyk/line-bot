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

CREATE TABLE IF NOT EXISTS restaurants (
    id           BIGSERIAL PRIMARY KEY,
    place_id     TEXT UNIQUE NOT NULL,
    name         TEXT NOT NULL,
    genre        TEXT,
    area         TEXT,
    address      TEXT,
    phone        TEXT,
    rating       NUMERIC(2,1),
    price_level  INTEGER,
    description  TEXT,
    updated_at   TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
