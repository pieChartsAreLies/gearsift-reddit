-- 001_reddit_schema.sql
-- Reddit trend pipeline schema

CREATE SCHEMA IF NOT EXISTS reddit;

CREATE TABLE reddit.posts (
    reddit_id   TEXT PRIMARY KEY,
    subreddit   TEXT NOT NULL,
    title       TEXT NOT NULL,
    selftext    TEXT,
    url         TEXT,
    score       INTEGER NOT NULL DEFAULT 0,
    num_comments INTEGER NOT NULL DEFAULT 0,
    flair       TEXT,
    author      TEXT,
    permalink   TEXT,
    top_comments TEXT,
    created_utc TIMESTAMPTZ,
    fetched_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    sort_source TEXT
);

CREATE INDEX idx_reddit_posts_subreddit ON reddit.posts (subreddit);
CREATE INDEX idx_reddit_posts_fetched_at ON reddit.posts (fetched_at);
CREATE INDEX idx_reddit_posts_created_utc ON reddit.posts (created_utc);

CREATE TABLE reddit.analyses (
    id                          SERIAL PRIMARY KEY,
    subreddit                   TEXT NOT NULL,
    analysis_date               DATE NOT NULL,
    posts_analyzed              INTEGER NOT NULL DEFAULT 0,
    products_json               JSONB,
    topics_json                 JSONB,
    questions_json              JSONB,
    pain_points_json            JSONB,
    purchase_signals_json       JSONB,
    content_opportunities_json  JSONB,
    raw_analysis                TEXT,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (subreddit, analysis_date)
);
