# Reddit Trend Pipeline Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Automated daily Reddit scraping and weekly Gemini-powered trend analysis for GearSift content strategy.

**Architecture:** Two Airflow DAGs on CT 203 (daily scrape + weekly analyze), PostgreSQL storage on CT 201, Webshare residential proxy for all Reddit requests. Single script file + thin DAG wrapper following existing homelab patterns.

**Tech Stack:** Python 3, requests, psycopg2, Airflow, Gemini 2.0 Flash REST API, PostgreSQL

---

### Task 1: Create PostgreSQL Schema

**Files:**
- Create: `sql/001_reddit_schema.sql`

**Step 1: Write the migration SQL**

```sql
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
```

**Step 2: Apply migration to CT 201**

Run from local machine:
```bash
ssh root@192.168.2.10 "pct exec 201 -- psql -U postgres -d project_data -f -" < sql/001_reddit_schema.sql
```

Expected: `CREATE SCHEMA`, `CREATE TABLE` x2, `CREATE INDEX` x3

**Step 3: Verify schema exists**

```bash
ssh root@192.168.2.10 "pct exec 201 -- psql -U postgres -d project_data -c '\dt reddit.*'"
```

Expected: `reddit.posts` and `reddit.analyses` listed.

**Step 4: Grant access to analytics user**

```bash
ssh root@192.168.2.10 "pct exec 201 -- psql -U postgres -d project_data -c \"GRANT USAGE ON SCHEMA reddit TO analytics; GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA reddit TO analytics; GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA reddit TO analytics;\""
```

**Step 5: Commit**

```bash
git add sql/001_reddit_schema.sql
git commit -m "Add Reddit pipeline PostgreSQL schema (posts + analyses)"
```

---

### Task 2: Write the Reddit Scraper

**Files:**
- Create: `homelab-docs/scripts/reddit_pipeline.py`

This is the main script with both `scrape()` and `analyze()` entry points. This task covers the scraper half.

**Step 1: Create the script with config, logging, and scrape function**

```python
#!/usr/bin/env python3
"""Reddit Trend Pipeline - scrapes subreddits and analyzes trends via Gemini.

Configuration (resolved in order):
    1. Airflow Variables (when running as a DAG)
    2. Environment variables (standalone or .env file)
"""

import json
import logging
import os
import random
import sys
import time
from datetime import datetime, timezone

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def load_env(path="/root/.env"):
    """Source key=value pairs from a .env file into os.environ."""
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


def get_config(key, default=None):
    """Try Airflow Variable, then env var, then default."""
    try:
        from airflow.models import Variable
        val = Variable.get(key, default_var=None)
        if val is not None:
            return val
    except (ImportError, Exception):
        pass
    return os.environ.get(key, default)


# Constants
SUBREDDITS = [
    "ultralight",
    "hiking",
    "campinggear",
    "wildernessbackpacking",
    "backpacking",
    "trailrunning",
]

DAILY_SORT_TYPES = ["hot", "new"]
WEEKLY_SORT_TYPES = ["top"]
TOP_TIME_FILTER = "month"
POSTS_PER_LISTING = 100
REQUEST_DELAY = 4.0
COMMENT_FETCH_THRESHOLD = 5  # only fetch comments if num_comments > this
MAX_COMMENTS = 20
SELFTEXT_MAX_LENGTH = 5000

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:125.0) Gecko/20100101 Firefox/125.0",
]

GEMINI_MODEL = "gemini-2.0-flash"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging():
    handlers = [logging.StreamHandler(sys.stdout)]
    try:
        handlers.insert(0, logging.FileHandler("/var/log/reddit_pipeline.log"))
    except (PermissionError, FileNotFoundError):
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=handlers,
    )


log = logging.getLogger("reddit_pipeline")

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def reddit_get(url, proxy_url=None):
    """GET a Reddit .json URL through a proxy with a random User-Agent."""
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
    }
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    resp = requests.get(url, headers=headers, proxies=proxies, timeout=30)
    resp.raise_for_status()
    return resp.json()


def fetch_listing(subreddit, sort_type, proxy_url=None):
    """Fetch a subreddit listing (hot/new/top) and return parsed posts."""
    if sort_type == "top":
        url = f"https://www.reddit.com/r/{subreddit}/{sort_type}.json?t={TOP_TIME_FILTER}&limit={POSTS_PER_LISTING}"
    else:
        url = f"https://www.reddit.com/r/{subreddit}/{sort_type}.json?limit={POSTS_PER_LISTING}"

    data = reddit_get(url, proxy_url)
    children = data.get("data", {}).get("children", [])

    posts = []
    for child in children:
        p = child.get("data", {})
        posts.append({
            "reddit_id": p.get("id", ""),
            "subreddit": subreddit.lower(),
            "title": p.get("title", ""),
            "selftext": (p.get("selftext", "") or "")[:SELFTEXT_MAX_LENGTH],
            "url": p.get("url", ""),
            "score": p.get("score", 0),
            "num_comments": p.get("num_comments", 0),
            "flair": p.get("link_flair_text"),
            "author": str(p.get("author", "[deleted]")),
            "permalink": p.get("permalink", ""),
            "created_utc": datetime.fromtimestamp(
                p.get("created_utc", 0), tz=timezone.utc
            ).isoformat(),
            "sort_source": sort_type,
        })
    return posts


def fetch_comments(permalink, proxy_url=None):
    """Fetch top comments for a post by its permalink."""
    url = f"https://www.reddit.com{permalink}.json?sort=top&limit={MAX_COMMENTS}"
    try:
        data = reddit_get(url, proxy_url)
    except Exception as e:
        log.warning("Failed to fetch comments for %s: %s", permalink, e)
        return ""

    # Reddit returns [listing, comments_listing]
    if not isinstance(data, list) or len(data) < 2:
        return ""

    comments_data = data[1].get("data", {}).get("children", [])
    comment_texts = []
    for c in comments_data[:MAX_COMMENTS]:
        body = c.get("data", {}).get("body", "")
        score = c.get("data", {}).get("score", 0)
        if body and body != "[deleted]" and body != "[removed]":
            comment_texts.append(f"[score:{score}] {body}")

    return "\n---\n".join(comment_texts)


def upsert_posts(posts, cursor):
    """Upsert posts into reddit.posts."""
    sql = """
        INSERT INTO reddit.posts
            (reddit_id, subreddit, title, selftext, url, score, num_comments,
             flair, author, permalink, top_comments, created_utc, fetched_at, sort_source)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), %s)
        ON CONFLICT (reddit_id) DO UPDATE SET
            score = EXCLUDED.score,
            num_comments = EXCLUDED.num_comments,
            selftext = EXCLUDED.selftext,
            top_comments = COALESCE(EXCLUDED.top_comments, reddit.posts.top_comments),
            fetched_at = EXCLUDED.fetched_at
    """
    for p in posts:
        cursor.execute(sql, (
            p["reddit_id"], p["subreddit"], p["title"], p["selftext"],
            p["url"], p["score"], p["num_comments"], p["flair"],
            p["author"], p["permalink"], p.get("top_comments"),
            p["created_utc"], p["sort_source"],
        ))


# ---------------------------------------------------------------------------
# Scrape entry point
# ---------------------------------------------------------------------------

def scrape(include_top=False):
    """Scrape subreddits and upsert to PostgreSQL.

    Args:
        include_top: If True, also fetch top/month (for weekly Saturday run).
    """
    load_env()
    setup_logging()

    proxy_url = get_config("REDDIT_PROXY_URL", "")
    if not proxy_url:
        log.error("REDDIT_PROXY_URL not configured")
        return {"status": "error", "message": "REDDIT_PROXY_URL not configured"}

    # Connect to PostgreSQL
    try:
        from airflow.providers.postgres.hooks.postgres import PostgresHook
        hook = PostgresHook(postgres_conn_id="postgres_analytics")
        conn = hook.get_conn()
    except (ImportError, Exception):
        import psycopg2
        conn = psycopg2.connect(
            host=get_config("PG_HOST", "192.168.2.67"),
            port=int(get_config("PG_PORT", "5432")),
            dbname=get_config("PG_DATABASE", "project_data"),
            user=get_config("PG_USER", "analytics"),
            password=get_config("PG_PASSWORD", ""),
        )

    cursor = conn.cursor()

    sort_types = list(DAILY_SORT_TYPES)
    if include_top:
        sort_types.extend(WEEKLY_SORT_TYPES)

    total_posts = 0
    total_comments_fetched = 0

    for sub in SUBREDDITS:
        for sort_type in sort_types:
            log.info("Fetching r/%s/%s...", sub, sort_type)
            try:
                posts = fetch_listing(sub, sort_type, proxy_url)
                log.info("  Got %d posts", len(posts))
            except Exception as e:
                log.error("  Failed to fetch r/%s/%s: %s", sub, sort_type, e)
                time.sleep(REQUEST_DELAY)
                continue

            time.sleep(REQUEST_DELAY)

            # Fetch comments for posts with enough discussion
            for post in posts:
                if post["num_comments"] > COMMENT_FETCH_THRESHOLD:
                    # Check if we already have comments for this post
                    cursor.execute(
                        "SELECT top_comments FROM reddit.posts WHERE reddit_id = %s",
                        (post["reddit_id"],)
                    )
                    row = cursor.fetchone()
                    if row and row[0]:
                        # Already have comments, skip
                        post["top_comments"] = None  # don't overwrite
                        continue

                    log.info("  Fetching comments for: %s", post["title"][:60])
                    post["top_comments"] = fetch_comments(post["permalink"], proxy_url)
                    total_comments_fetched += 1
                    time.sleep(REQUEST_DELAY)

            upsert_posts(posts, cursor)
            conn.commit()
            total_posts += len(posts)

    cursor.close()
    conn.close()

    log.info("Scrape complete: %d posts, %d comment fetches", total_posts, total_comments_fetched)
    return {
        "status": "ok",
        "posts": total_posts,
        "comment_fetches": total_comments_fetched,
    }
```

**Step 2: Test locally against fixture data**

Verify the script imports and the helper functions parse correctly by running standalone:
```bash
cd /Users/llama/Development/homelab-docs/scripts
python3 -c "import reddit_pipeline; print('Import OK')"
```

Expected: `Import OK` (no import errors)

**Step 3: Commit**

```bash
git add scripts/reddit_pipeline.py
git commit -m "Add reddit_pipeline.py scraper with proxy support and comment fetching"
```

---

### Task 3: Write the Gemini Analyzer

**Files:**
- Modify: `homelab-docs/scripts/reddit_pipeline.py` (append analyze function)

**Step 1: Add the analysis prompt and analyze function to reddit_pipeline.py**

Append after the scrape function:

```python
# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

ANALYSIS_SYSTEM_PROMPT = """You analyze Reddit posts from outdoor recreation subreddits.
Extract structured data from the posts and their comments. Be specific: use exact product
names, not generics. Include brief quotes when relevant."""

ANALYSIS_USER_PROMPT = """Analyze these Reddit posts from r/{subreddit} and return a JSON object
with exactly these keys:

- "products": array of objects with keys: "name", "brand", "category" (shelter/sleep_system/pack/clothing/footwear/cooking/water/electronics/other), "sentiment" (positive/negative/mixed/neutral), "context", "mentions" (integer)
- "topics": array of objects with keys: "topic", "summary", "engagement_score" (integer)
- "questions": array of strings (specific questions people are asking)
- "pain_points": array of objects with keys: "issue", "details", "frequency" (integer)
- "purchase_signals": array of objects with keys: "looking_for", "budget", "criteria"
- "content_opportunities": array of objects with keys: "title", "rationale"

Posts:

{posts_text}"""


def format_posts_for_analysis(posts):
    """Format posts + comments for the Gemini prompt."""
    # Sort by engagement
    posts.sort(key=lambda p: (p[4] or 0) + 2 * (p[5] or 0), reverse=True)
    posts = posts[:80]

    lines = []
    for i, row in enumerate(posts, 1):
        # row: reddit_id, subreddit, title, selftext, score, num_comments,
        #       flair, url, top_comments
        reddit_id, sub, title, selftext, score, num_comments, flair, url, top_comments = row
        flair_str = f" [{flair}]" if flair else ""
        lines.append(f"### Post {i} (score: {score}, comments: {num_comments}){flair_str}")
        lines.append(f"**{title}**")
        if url and not url.startswith("https://www.reddit.com"):
            lines.append(f"Link: {url}")
        if selftext and selftext.strip():
            lines.append(selftext.strip()[:2000])
        if top_comments and top_comments.strip():
            lines.append(f"\n**Top Comments:**\n{top_comments[:3000]}")
        lines.append("")

    return "\n".join(lines)


def call_gemini(system_prompt, user_prompt, api_key):
    """Call Gemini REST API with JSON response mode."""
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={api_key}"
    )
    payload = {
        "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "generationConfig": {
            "temperature": 0.3,
            "maxOutputTokens": 8192,
            "responseMimeType": "application/json",
        },
    }
    resp = requests.post(
        url,
        headers={"Content-Type": "application/json"},
        json=payload,
        timeout=180,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["candidates"][0]["content"]["parts"][0]["text"].strip()


def analyze():
    """Analyze the past week's posts per subreddit via Gemini and store results."""
    load_env()
    setup_logging()

    gemini_key = get_config("GEMINI_API_KEY", "")
    if not gemini_key:
        log.error("GEMINI_API_KEY not configured")
        return {"status": "error", "message": "GEMINI_API_KEY not configured"}

    # Connect to PostgreSQL
    try:
        from airflow.providers.postgres.hooks.postgres import PostgresHook
        hook = PostgresHook(postgres_conn_id="postgres_analytics")
        conn = hook.get_conn()
    except (ImportError, Exception):
        import psycopg2
        conn = psycopg2.connect(
            host=get_config("PG_HOST", "192.168.2.67"),
            port=int(get_config("PG_PORT", "5432")),
            dbname=get_config("PG_DATABASE", "project_data"),
            user=get_config("PG_USER", "analytics"),
            password=get_config("PG_PASSWORD", ""),
        )

    cursor = conn.cursor()
    analysis_date = datetime.now(timezone.utc).date()
    total_analyzed = 0

    for sub in SUBREDDITS:
        log.info("Analyzing r/%s...", sub)

        # Fetch this week's posts
        cursor.execute("""
            SELECT reddit_id, subreddit, title, selftext, score, num_comments,
                   flair, url, top_comments
            FROM reddit.posts
            WHERE subreddit = %s
              AND fetched_at >= NOW() - INTERVAL '7 days'
            ORDER BY score + 2 * num_comments DESC
            LIMIT 80
        """, (sub,))
        rows = cursor.fetchall()

        if not rows:
            log.info("  No posts found for r/%s this week, skipping", sub)
            continue

        log.info("  Found %d posts", len(rows))
        posts_text = format_posts_for_analysis(rows)

        user_prompt = ANALYSIS_USER_PROMPT.format(
            subreddit=sub,
            posts_text=posts_text,
        )

        try:
            log.info("  Sending to Gemini...")
            raw = call_gemini(ANALYSIS_SYSTEM_PROMPT, user_prompt, gemini_key)
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            log.error("  Failed to parse Gemini JSON for r/%s: %s", sub, e)
            # Store raw response anyway
            cursor.execute("""
                INSERT INTO reddit.analyses
                    (subreddit, analysis_date, posts_analyzed, raw_analysis, created_at)
                VALUES (%s, %s, %s, %s, NOW())
                ON CONFLICT (subreddit, analysis_date) DO UPDATE SET
                    raw_analysis = EXCLUDED.raw_analysis,
                    posts_analyzed = EXCLUDED.posts_analyzed
            """, (sub, analysis_date, len(rows), raw))
            conn.commit()
            time.sleep(15)
            continue
        except Exception as e:
            log.error("  Gemini API error for r/%s: %s", sub, e)
            time.sleep(15)
            continue

        # Upsert analysis
        cursor.execute("""
            INSERT INTO reddit.analyses
                (subreddit, analysis_date, posts_analyzed,
                 products_json, topics_json, questions_json,
                 pain_points_json, purchase_signals_json,
                 content_opportunities_json, raw_analysis, created_at)
            VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb,
                    %s::jsonb, %s::jsonb, %s::jsonb, %s, NOW())
            ON CONFLICT (subreddit, analysis_date) DO UPDATE SET
                posts_analyzed = EXCLUDED.posts_analyzed,
                products_json = EXCLUDED.products_json,
                topics_json = EXCLUDED.topics_json,
                questions_json = EXCLUDED.questions_json,
                pain_points_json = EXCLUDED.pain_points_json,
                purchase_signals_json = EXCLUDED.purchase_signals_json,
                content_opportunities_json = EXCLUDED.content_opportunities_json,
                raw_analysis = EXCLUDED.raw_analysis
        """, (
            sub, analysis_date, len(rows),
            json.dumps(parsed.get("products", [])),
            json.dumps(parsed.get("topics", [])),
            json.dumps(parsed.get("questions", [])),
            json.dumps(parsed.get("pain_points", [])),
            json.dumps(parsed.get("purchase_signals", [])),
            json.dumps(parsed.get("content_opportunities", [])),
            raw,
        ))
        conn.commit()
        total_analyzed += 1
        log.info("  Analysis saved for r/%s (%d posts)", sub, len(rows))

        time.sleep(15)  # Rate limit between Gemini calls

    cursor.close()
    conn.close()

    log.info("Analysis complete: %d subreddits analyzed", total_analyzed)
    return {"status": "ok", "subreddits_analyzed": total_analyzed}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    """CLI entry point for standalone testing."""
    if len(sys.argv) < 2:
        print("Usage: reddit_pipeline.py [scrape|scrape-with-top|analyze]")
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "scrape":
        result = scrape(include_top=False)
    elif cmd == "scrape-with-top":
        result = scrape(include_top=True)
    elif cmd == "analyze":
        result = analyze()
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
```

**Step 2: Verify import**

```bash
python3 -c "import reddit_pipeline; print('OK')"
```

Expected: `OK`

**Step 3: Commit**

```bash
git add scripts/reddit_pipeline.py
git commit -m "Add Gemini analysis function and CLI to reddit_pipeline.py"
```

---

### Task 4: Write the Airflow DAG Wrapper

**Files:**
- Create: `homelab-docs/scripts/dags/dag_reddit_pipeline.py`

**Step 1: Create the DAG file**

```python
"""Airflow DAGs for Reddit Trend Pipeline.

Daily scrape of outdoor recreation subreddits via .json endpoints.
Weekly analysis via Gemini for product mentions, trends, and content opportunities.
"""

import sys
from datetime import datetime, timedelta

sys.path.insert(0, "/home/airflow/scripts")

from airflow import DAG
from airflow.operators.python import PythonOperator

from reddit_pipeline import scrape, analyze

# --- Daily scrape (hot + new) ---

scrape_default_args = {
    "owner": "airflow",
    "retries": 2,
    "retry_delay": timedelta(minutes=10),
}

with DAG(
    dag_id="reddit_scrape",
    default_args=scrape_default_args,
    description="Scrape outdoor recreation subreddits daily via .json endpoints",
    schedule="0 6 * * *",  # 6 AM UTC daily
    start_date=datetime(2026, 3, 16),
    catchup=False,
    max_active_runs=1,
    tags=["gearsift", "reddit", "scrape"],
) as scrape_dag:

    daily_scrape = PythonOperator(
        task_id="daily_scrape",
        python_callable=scrape,
        op_kwargs={"include_top": False},
    )

# --- Saturday scrape (hot + new + top/month) ---

with DAG(
    dag_id="reddit_scrape_weekly",
    default_args=scrape_default_args,
    description="Scrape subreddits including top/month (Saturday before analysis)",
    schedule="0 6 * * 6",  # 6 AM UTC Saturdays
    start_date=datetime(2026, 3, 16),
    catchup=False,
    max_active_runs=1,
    tags=["gearsift", "reddit", "scrape"],
) as weekly_scrape_dag:

    weekly_scrape = PythonOperator(
        task_id="weekly_scrape_with_top",
        python_callable=scrape,
        op_kwargs={"include_top": True},
    )

# --- Weekly analysis ---

analyze_default_args = {
    "owner": "airflow",
    "retries": 1,
    "retry_delay": timedelta(minutes=15),
}

with DAG(
    dag_id="reddit_analyze",
    default_args=analyze_default_args,
    description="Analyze week's Reddit posts via Gemini for GearSift content strategy",
    schedule="0 7 * * 0",  # 7 AM UTC Sundays
    start_date=datetime(2026, 3, 16),
    catchup=False,
    max_active_runs=1,
    tags=["gearsift", "reddit", "analysis"],
) as analyze_dag:

    weekly_analyze = PythonOperator(
        task_id="weekly_analyze",
        python_callable=analyze,
    )
```

**Step 2: Commit**

```bash
git add scripts/dags/dag_reddit_pipeline.py
git commit -m "Add Airflow DAG wrappers for Reddit scrape + analyze"
```

---

### Task 5: Deploy to CT 203

**Step 1: Copy script files to CT 203**

```bash
ssh root@192.168.2.10 "pct push 203 homelab-docs/scripts/reddit_pipeline.py /home/airflow/scripts/reddit_pipeline.py"
ssh root@192.168.2.10 "pct push 203 homelab-docs/scripts/dags/dag_reddit_pipeline.py /home/airflow/airflow/dags/dag_reddit_pipeline.py"
```

**Step 2: Set Airflow Variable for proxy**

Get the Webshare rotating proxy URL from Bitwarden, then:
```bash
ssh root@192.168.2.10 "pct exec 203 -- sudo -u airflow /home/airflow/airflow-venv/bin/airflow variables set REDDIT_PROXY_URL '<proxy-url-from-bitwarden>'"
```

**Step 3: Verify DAGs appear in Airflow**

```bash
ssh root@192.168.2.10 "pct exec 203 -- sudo -u airflow /home/airflow/airflow-venv/bin/airflow dags list" | grep reddit
```

Expected: `reddit_scrape`, `reddit_scrape_weekly`, `reddit_analyze` listed.

**Step 4: Commit deploy state**

```bash
git commit --allow-empty -m "Deploy reddit pipeline DAGs to CT 203"
```

---

### Task 6: Test the Scrape DAG

**Step 1: Trigger the daily scrape manually**

```bash
ssh root@192.168.2.10 "pct exec 203 -- sudo -u airflow /home/airflow/airflow-venv/bin/airflow dags unpause reddit_scrape"
ssh root@192.168.2.10 "pct exec 203 -- sudo -u airflow /home/airflow/airflow-venv/bin/airflow dags trigger reddit_scrape"
```

**Step 2: Wait and check logs**

```bash
ssh root@192.168.2.10 "pct exec 203 -- tail -50 /var/log/reddit_pipeline.log"
```

Expected: Lines showing "Fetching r/ultralight/hot...", "Got N posts", "Scrape complete"

**Step 3: Verify data in PostgreSQL**

```bash
ssh root@192.168.2.10 "pct exec 201 -- psql -U postgres -d project_data -c 'SELECT subreddit, COUNT(*) FROM reddit.posts GROUP BY subreddit ORDER BY subreddit'"
```

Expected: 6 subreddits with ~100-200 posts each.

**Step 4: Spot check a post with comments**

```bash
ssh root@192.168.2.10 "pct exec 201 -- psql -U postgres -d project_data -c \"SELECT reddit_id, title, LEFT(top_comments, 200) FROM reddit.posts WHERE top_comments IS NOT NULL LIMIT 3\""
```

Expected: Posts with truncated comment text visible.

---

### Task 7: Test the Analysis DAG

**Step 1: Trigger the analysis manually**

```bash
ssh root@192.168.2.10 "pct exec 203 -- sudo -u airflow /home/airflow/airflow-venv/bin/airflow dags unpause reddit_analyze"
ssh root@192.168.2.10 "pct exec 203 -- sudo -u airflow /home/airflow/airflow-venv/bin/airflow dags trigger reddit_analyze"
```

**Step 2: Wait (~3 minutes for 6 subs x 15s delays) and check logs**

```bash
ssh root@192.168.2.10 "pct exec 203 -- tail -50 /var/log/reddit_pipeline.log"
```

Expected: "Analyzing r/ultralight...", "Found N posts", "Sending to Gemini...", "Analysis saved"

**Step 3: Verify analysis data in PostgreSQL**

```bash
ssh root@192.168.2.10 "pct exec 201 -- psql -U postgres -d project_data -c \"SELECT subreddit, analysis_date, posts_analyzed, jsonb_array_length(products_json) as products, jsonb_array_length(topics_json) as topics FROM reddit.analyses ORDER BY subreddit\""
```

Expected: Rows for each subreddit with product/topic counts.

**Step 4: Spot check a product extraction**

```bash
ssh root@192.168.2.10 "pct exec 201 -- psql -U postgres -d project_data -c \"SELECT jsonb_pretty(products_json->0) FROM reddit.analyses WHERE subreddit = 'ultralight' LIMIT 1\""
```

Expected: JSON object with name, brand, category, sentiment, context, mentions.

---

### Task 8: Unpause Weekly DAGs and Update Docs

**Step 1: Unpause the Saturday weekly scrape**

```bash
ssh root@192.168.2.10 "pct exec 203 -- sudo -u airflow /home/airflow/airflow-venv/bin/airflow dags unpause reddit_scrape_weekly"
```

**Step 2: Update homelab-docs automation page**

Add the Reddit DAGs to `homelab-docs/docs/services/automation.md` in the DAG table.

**Step 3: Update homelab-docs changelog**

Add dated entry to `homelab-docs/docs/operations/changelog.md`.

**Step 4: Update GearSift service docs**

Add Reddit pipeline as a data source in `homelab-docs/docs/services/gearsift.md`.

**Step 5: Push homelab-docs and gearsift-reddit**

```bash
cd /Users/llama/Development/homelab-docs && git add -A && git commit -m "Add Reddit trend pipeline DAGs and documentation" && git push
cd /Users/llama/Development/gearsift-reddit && git push
```

**Step 6: Update memory**

Update `/Users/llama/.claude/projects/-Users-llama/memory/homelab.md` with CT 203 Reddit pipeline entry.
Update `/Users/llama/.claude/projects/-Users-llama-Development/memory/MEMORY.md` if needed.
