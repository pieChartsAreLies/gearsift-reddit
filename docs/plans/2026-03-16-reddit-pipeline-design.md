# Reddit Trend Pipeline Design

**Date:** 2026-03-16
**Status:** Approved

## Goal

Automated daily collection and weekly analysis of Reddit posts from outdoor recreation subreddits. Feeds GearSift content strategy with product mentions, trending topics, pain points, purchase intent signals, and guide opportunities.

## Architecture

Two Airflow DAGs on CT 203, single script file.

| DAG | Schedule | Purpose |
|-----|----------|---------|
| `reddit_scrape` | Daily 6 AM UTC | Fetch .json from 7 subreddits via Webshare proxy, upsert posts to PostgreSQL |
| `reddit_analyze` | Weekly Sunday 7 AM UTC | Pull week's posts from Postgres, send to Gemini for structured analysis, store results |

Single script: `reddit_pipeline.py` with both functions. Thin DAG wrapper: `dag_reddit_pipeline.py`.

## Data Flow

```
Reddit .json endpoints
    |  (via Webshare residential proxy, random rotation)
    v
reddit_scrape DAG (daily)
    |  parse posts, dedup on reddit post ID
    v
PostgreSQL CT 201 (project_data, reddit schema)
    |  reddit.posts table
    v
reddit_analyze DAG (weekly)
    |  query week's posts, format top 80 per subreddit by engagement
    v
Gemini 2.0 Flash (REST API, key from Airflow Variable)
    |  structured extraction prompt
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
    "CampingGear",
]
```

Three sort types per sub (hot, top/month, new), 100 posts each = ~21 requests total per day. Single random Webshare proxy per request, 4-second delays between requests. ~90 seconds total runtime.

## Database Schema

On CT 201, `project_data` database, new `reddit` schema.

### reddit.posts

| Column | Type | Notes |
|--------|------|-------|
| reddit_id | text PK | Reddit's post ID (dedup key) |
| subreddit | text | e.g. "ultralight" |
| title | text | |
| selftext | text | Truncated to 5000 chars |
| score | integer | Upvotes at fetch time |
| num_comments | integer | At fetch time |
| flair | text | Nullable |
| author | text | |
| permalink | text | |
| created_utc | timestamptz | When posted |
| fetched_at | timestamptz | When we scraped it |
| sort_source | text | "hot", "top", "new" |

Score and num_comments update on re-fetch (upsert with `SET score = EXCLUDED.score, num_comments = EXCLUDED.num_comments, fetched_at = EXCLUDED.fetched_at`).

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
| raw_analysis | text | Full Gemini markdown response |
| created_at | timestamptz | |

Unique constraint on `(subreddit, analysis_date)`.

## Configuration

New Airflow Variables on CT 203:

| Variable | Value |
|----------|-------|
| `REDDIT_PROXY_URL` | Single Webshare proxy URL from Bitwarden |
| `GEMINI_API_KEY` | Already exists |

PG connection via existing `postgres_analytics` Airflow Connection.

## Patterns

- Config resolution: Airflow Variables -> env vars -> defaults
- Gemini: REST API with JSON payload (matches youtube_aggregator)
- Postgres: PostgresHook with `postgres_analytics` conn ID (matches garmin_sync)
- Logging: Dual to `/var/log/reddit_pipeline.log` + stdout
- DAG structure: Thin wrapper + standalone script (matches job_discovery)
- `catchup=False`, `max_active_runs=1`

## Analysis Prompt

Same structured prompt (products, topics, questions, pain points, purchase signals, content opportunities) but requesting JSON output for structured JSONB storage. `raw_analysis` column stores full text as fallback.

## Proxy Strategy

All Reddit HTTP requests route through Webshare residential proxies (single random proxy per request). 4-second delay between requests. ~21 requests/day is invisible traffic volume. Never hits Reddit from the home IP.
