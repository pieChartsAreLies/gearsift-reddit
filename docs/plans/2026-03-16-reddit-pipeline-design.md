# Reddit Trend Pipeline Design

**Date:** 2026-03-16
**Status:** Approved (revised after Gemini adversarial review)

## Goal

Automated daily collection and weekly analysis of Reddit posts from outdoor recreation subreddits. Feeds GearSift content strategy with product mentions, trending topics, pain points, purchase intent signals, and guide opportunities.

## Architecture

Two Airflow DAGs on CT 203, single script file.

| DAG | Schedule | Purpose |
|-----|----------|---------|
| `reddit_scrape` | Daily 6 AM UTC | Fetch .json from 6 subreddits via Webshare proxy, upsert posts + comments to PostgreSQL |
| `reddit_analyze` | Weekly Sunday 7 AM UTC | Pull week's posts from Postgres, send to Gemini for structured analysis, store results |

Single script: `reddit_pipeline.py` with both functions. Thin DAG wrapper: `dag_reddit_pipeline.py`.

## Data Flow

```
Reddit .json endpoints (listing + per-post comment trees)
    |  (via Webshare rotating residential proxy)
    v
reddit_scrape DAG (daily)
    |  parse posts + top 20 comments, dedup on reddit post ID
    v
PostgreSQL CT 201 (project_data, reddit schema)
    |  reddit.posts table
    v
reddit_analyze DAG (weekly)
    |  query week's posts, format top 80 per subreddit by engagement
    |  include post body + top comments + link URL in prompt
    v
Gemini 2.0 Flash (REST API, response_mime_type=application/json)
    |  structured extraction prompt -> JSON output
    v
PostgreSQL CT 201
    |  reddit.analyses table (structured JSON columns)
    v
Lightdash (optional, already connected to project_data)
```

## Target Subreddits

```python
SUBREDDITS = [
    "ultralight",
    "hiking",
    "campinggear",
    "WildernessBackpacking",
    "backpacking",
    "trailrunning",
]
```

### Scrape Strategy

- **Daily (hot, new):** 2 sort types x 6 subs x 100 posts = 12 listing requests
- **Weekly Saturday (top/month):** 1 sort type x 6 subs x 100 posts = 6 listing requests
- **Comment fetching:** For each new post with num_comments > 5, fetch the post's .json to get comment tree. Estimated ~50-100 additional requests/day for new posts only.
- **Total daily:** ~60-120 requests through rotating proxy, 4-second delays. ~8 minutes worst case.

## Database Schema

On CT 201, `project_data` database, new `reddit` schema.

### reddit.posts

| Column | Type | Notes |
|--------|------|-------|
| reddit_id | text PK | Reddit's post ID (dedup key) |
| subreddit | text | Lowercase normalized |
| title | text | |
| selftext | text | Truncated to 5000 chars |
| url | text | Link target for link posts (nullable) |
| score | integer | Upvotes at fetch time |
| num_comments | integer | At fetch time |
| flair | text | Nullable |
| author | text | |
| permalink | text | |
| top_comments | text | Top 20 comments by score, newline-separated |
| created_utc | timestamptz | When posted |
| fetched_at | timestamptz | When we scraped it |
| sort_source | text | "hot", "top", "new" |

Upsert on conflict: `SET score = EXCLUDED.score, num_comments = EXCLUDED.num_comments, selftext = EXCLUDED.selftext, top_comments = EXCLUDED.top_comments, fetched_at = EXCLUDED.fetched_at`.

### reddit.analyses

| Column | Type | Notes |
|--------|------|-------|
| id | serial PK | |
| subreddit | text | |
| analysis_date | date | Sunday of the analysis week |
| posts_analyzed | integer | How many posts fed to Gemini |
| products_json | jsonb | Structured product mentions |
| topics_json | jsonb | Trending topics |
| questions_json | jsonb | Common questions |
| pain_points_json | jsonb | Frustrations/complaints |
| purchase_signals_json | jsonb | Active buying intent |
| content_opportunities_json | jsonb | Suggested guides/articles |
| raw_analysis | text | Full Gemini response as fallback |
| created_at | timestamptz | |

Unique constraint on `(subreddit, analysis_date)`.

## Configuration

New Airflow Variables on CT 203:

| Variable | Value |
|----------|-------|
| `REDDIT_PROXY_URL` | Webshare rotating endpoint URL from Bitwarden |
| `GEMINI_API_KEY` | Already exists |

PG connection via existing `postgres_analytics` Airflow Connection.

## Patterns

- Config resolution: Airflow Variables -> env vars -> defaults
- Gemini: REST API with JSON payload + `response_mime_type: application/json` (matches youtube_aggregator pattern, adds strict JSON mode)
- Postgres: PostgresHook with `postgres_analytics` conn ID (matches garmin_sync)
- Logging: Dual to `/var/log/reddit_pipeline.log` + stdout
- DAG structure: Thin wrapper + standalone script (matches job_discovery)
- `catchup=False`, `max_active_runs=1`

## Analysis Prompt

Structured prompt requesting JSON output with keys: `products`, `topics`, `questions`, `pain_points`, `purchase_signals`, `content_opportunities`. Uses `response_mime_type="application/json"` in Gemini API config. Includes post body + top comments + link URL for full context. `raw_analysis` column stores the response as fallback.

## Proxy Strategy

All Reddit HTTP requests route through Webshare rotating residential proxy endpoint (single URL, auto-rotates IP per request). 4-second delay between requests. User-Agent rotation with realistic browser strings. Retry with exponential backoff on 429/403. Never hits Reddit from the home IP.

## Gemini Rate Limiting

15-second delay between per-subreddit analysis calls during weekly run. 180-second timeout on each Gemini API request. 7 calls total per weekly run.

## Adversarial Review Findings (incorporated)

1. Added comment fetching (top 20 by score per post)
2. Removed duplicate subreddit (campinggear/CampingGear), normalized to lowercase
3. Added url column for link posts
4. Set response_mime_type for reliable JSON parsing
5. Clarified proxy as rotating endpoint (not static IP selection)
6. Added 180s Gemini timeout + 15s inter-call delay
7. Moved top/month to weekly schedule (avoids fetching same posts 30 days)
8. Added selftext + top_comments to upsert conflict clause
