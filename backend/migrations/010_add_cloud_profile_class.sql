-- Server-owned profile classification for synthetic-only Hermes Cloud canaries.
-- Existing and newly provisioned users remain real unless an operator explicitly
-- classifies a disposable profile as synthetic through the protected database path.

ALTER TABLE users
    ADD COLUMN profile_class TEXT NOT NULL DEFAULT 'real',
    ADD CONSTRAINT users_profile_class_check
    CHECK (profile_class IN ('real', 'synthetic'));

CREATE INDEX users_profile_class_idx
    ON users(profile_class, id);
