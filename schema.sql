-- Supabase の SQL Editor で実行してください

CREATE TABLE messages (
  id           UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  line_user_id TEXT NOT NULL,
  role         TEXT NOT NULL,
  content      TEXT NOT NULL,
  created_at   TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_messages_user_created ON messages (line_user_id, created_at);
