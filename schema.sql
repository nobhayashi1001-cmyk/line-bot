CREATE TABLE IF NOT EXISTS users (
    id             BIGSERIAL PRIMARY KEY,
    line_user_id   TEXT UNIQUE NOT NULL,
    name           TEXT,
    region         TEXT,
    prefecture     TEXT,
    city           TEXT,
    birthdate      TEXT,
    referral_code  TEXT UNIQUE,
    referred_by    TEXT,
    is_paid        BOOLEAN DEFAULT FALSE,
    daily_count    INTEGER DEFAULT 0,
    bonus_count    INTEGER DEFAULT 0,
    last_used_date TEXT,
    created_at     TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Migration: add columns to existing table
ALTER TABLE users ADD COLUMN IF NOT EXISTS prefecture     TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS city           TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS referral_code  TEXT UNIQUE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS referred_by    TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_paid        BOOLEAN DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS daily_count    INTEGER DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS bonus_count    INTEGER DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_used_date TEXT;

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
