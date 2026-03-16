"""
GearSift Reddit Trend Analyzer

Pulls public posts and comments from outdoor recreation subreddits
for offline content analysis. Read-only — no posting or voting.

Requirements:
    pip install praw

Setup:
    1. Get API credentials from https://www.reddit.com/prefs/apps
    2. Copy .env.example to .env and fill in your credentials
    3. Run: python scrape.py

Output:
    JSON files in ./output/ with posts and comments from target subreddits.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import praw
except ImportError:
    print("Install praw first: pip install praw")
    sys.exit(1)

# --- Configuration ---

SUBREDDITS = [
    "ultralight",
    "hiking",
    "campinggear",
    "WildernessBackpacking",
]

POSTS_PER_SUBREDDIT = 100  # per sort type
COMMENTS_PER_POST = 50
SORT_TYPES = ["hot", "top", "new"]
TOP_TIME_FILTER = "month"

OUTPUT_DIR = Path("output")


def get_reddit_client():
    """Create Reddit client from environment variables."""
    client_id = os.environ.get("REDDIT_CLIENT_ID")
    client_secret = os.environ.get("REDDIT_CLIENT_SECRET")
    user_agent = os.environ.get("REDDIT_USER_AGENT", "gearsift-reddit/0.1")

    if not client_id or not client_secret:
        print("Set REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET environment variables.")
        print("See .env.example for details.")
        sys.exit(1)

    return praw.Reddit(
        client_id=client_id,
        client_secret=client_secret,
        user_agent=user_agent,
    )


def extract_post(post):
    """Extract relevant fields from a post."""
    return {
        "id": post.id,
        "title": post.title,
        "selftext": post.selftext,
        "score": post.score,
        "num_comments": post.num_comments,
        "created_utc": post.created_utc,
        "url": post.url,
        "permalink": f"https://reddit.com{post.permalink}",
        "flair": post.link_flair_text,
        "author": str(post.author) if post.author else "[deleted]",
    }


def extract_comments(post, limit=COMMENTS_PER_POST):
    """Extract top-level comments from a post."""
    post.comments.replace_more(limit=0)
    comments = []
    for comment in post.comments[:limit]:
        comments.append({
            "id": comment.id,
            "body": comment.body,
            "score": comment.score,
            "created_utc": comment.created_utc,
            "author": str(comment.author) if comment.author else "[deleted]",
        })
    return comments


def scrape_subreddit(reddit, subreddit_name):
    """Pull posts from a subreddit across multiple sort types."""
    subreddit = reddit.subreddit(subreddit_name)
    seen_ids = set()
    results = []

    for sort_type in SORT_TYPES:
        if sort_type == "hot":
            posts = subreddit.hot(limit=POSTS_PER_SUBREDDIT)
        elif sort_type == "top":
            posts = subreddit.top(time_filter=TOP_TIME_FILTER, limit=POSTS_PER_SUBREDDIT)
        elif sort_type == "new":
            posts = subreddit.new(limit=POSTS_PER_SUBREDDIT)
        else:
            continue

        for post in posts:
            if post.id in seen_ids:
                continue
            seen_ids.add(post.id)

            post_data = extract_post(post)
            post_data["sort_source"] = sort_type
            post_data["comments"] = extract_comments(post)
            results.append(post_data)

    print(f"  r/{subreddit_name}: {len(results)} posts collected")
    return results


def main():
    reddit = get_reddit_client()
    OUTPUT_DIR.mkdir(exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    all_data = {}

    for sub in SUBREDDITS:
        print(f"Scraping r/{sub}...")
        all_data[sub] = scrape_subreddit(reddit, sub)

    output_file = OUTPUT_DIR / f"reddit-{timestamp}.json"
    with open(output_file, "w") as f:
        json.dump(all_data, f, indent=2)

    total = sum(len(posts) for posts in all_data.values())
    print(f"\nDone. {total} posts saved to {output_file}")


if __name__ == "__main__":
    main()
