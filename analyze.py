"""
GearSift Reddit Trend Analyzer

Reads Reddit JSON data (from .json endpoints or PRAW output) and uses
Gemini to extract product mentions, trending topics, common questions,
and content opportunities.

Requirements:
    pip install google-generativeai

Setup:
    Set GEMINI_API_KEY environment variable.

Usage:
    python analyze.py                          # analyze all fixtures
    python analyze.py output/fixtures/Ultralight.json  # analyze one file
"""

import json
import os
import sys
from pathlib import Path

try:
    import google.generativeai as genai
except ImportError:
    print("Install google-generativeai first: pip install google-generativeai")
    sys.exit(1)

FIXTURES_DIR = Path("output/fixtures")

ANALYSIS_PROMPT = """You are analyzing Reddit posts from outdoor recreation subreddits (hiking, ultralight backpacking, camping gear, wilderness backpacking).

From the following posts, extract a structured analysis with these sections:

## Products & Gear Mentioned
List every specific product, brand, or gear item mentioned. Group by category (shelter, sleep system, pack, clothing, footwear, cooking, water, electronics, other). For each, note:
- Product/brand name
- Sentiment (positive, negative, mixed, neutral)
- Context (why it was mentioned)
- Frequency (how many posts mention it)

## Trending Topics
What subjects are getting the most engagement (upvotes + comments)? List the top 10 with a one-line summary of the discussion.

## Common Questions
What are people repeatedly asking about? These map directly to guide/article opportunities. List them as specific questions.

## Pain Points & Complaints
What frustrations, product failures, or unmet needs are people expressing? These signal product opportunities.

## Purchase Intent Signals
Posts where someone is actively looking to buy something. What are they shopping for, what's their budget, what criteria matter to them?

## Content Opportunities
Based on all of the above, suggest 5 specific guide or article topics that would serve this audience. For each, explain why the data supports it.

Be specific. Use exact product names, not generics. Quote short snippets from posts when relevant.

Here are the posts:

"""


def load_reddit_json(filepath):
    """Load and normalize Reddit JSON (handles both .json endpoint and PRAW formats)."""
    with open(filepath) as f:
        data = json.load(f)

    # .json endpoint format: {kind: "Listing", data: {children: [...]}}
    if isinstance(data, dict) and "data" in data:
        children = data["data"].get("children", [])
        posts = []
        for child in children:
            post = child.get("data", {})
            posts.append({
                "title": post.get("title", ""),
                "selftext": post.get("selftext", "")[:2000],  # truncate long posts
                "score": post.get("score", 0),
                "num_comments": post.get("num_comments", 0),
                "flair": post.get("link_flair_text"),
                "subreddit": post.get("subreddit", ""),
            })
        return posts

    # PRAW/scrape.py format: {subreddit: [{...}, ...]}
    if isinstance(data, dict):
        posts = []
        for sub_posts in data.values():
            if isinstance(sub_posts, list):
                posts.extend(sub_posts)
        return posts

    return data


def prepare_posts_for_analysis(posts, max_posts=80):
    """Sort by engagement and format for the prompt."""
    # Score by engagement: upvotes + 2x comments (comments signal deeper discussion)
    posts.sort(key=lambda p: p.get("score", 0) + 2 * p.get("num_comments", 0), reverse=True)
    posts = posts[:max_posts]

    lines = []
    for i, post in enumerate(posts, 1):
        flair = f" [{post['flair']}]" if post.get("flair") else ""
        lines.append(f"### Post {i} (score: {post['score']}, comments: {post['num_comments']}){flair}")
        lines.append(f"**{post['title']}**")
        if post.get("selftext", "").strip():
            lines.append(post["selftext"].strip())
        lines.append("")

    return "\n".join(lines)


def analyze_with_gemini(text, subreddit_name):
    """Send posts to Gemini for structured analysis."""
    model = genai.GenerativeModel("gemini-2.0-flash")

    print(f"  Sending to Gemini for analysis...")
    response = model.generate_content(ANALYSIS_PROMPT + text)

    return response.text


def main():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("Set GEMINI_API_KEY environment variable.")
        sys.exit(1)

    genai.configure(api_key=api_key)

    # Determine input files
    if len(sys.argv) > 1:
        files = [Path(f) for f in sys.argv[1:]]
    else:
        files = sorted(FIXTURES_DIR.glob("*.json"))

    if not files:
        print(f"No JSON files found. Place Reddit JSON in {FIXTURES_DIR}/")
        sys.exit(1)

    output_dir = Path("output/analysis")
    output_dir.mkdir(parents=True, exist_ok=True)

    for filepath in files:
        name = filepath.stem
        print(f"\nAnalyzing {name}...")

        posts = load_reddit_json(filepath)
        print(f"  Loaded {len(posts)} posts")

        text = prepare_posts_for_analysis(posts)
        analysis = analyze_with_gemini(text, name)

        out_file = output_dir / f"{name}-analysis.md"
        with open(out_file, "w") as f:
            f.write(f"# r/{name} Trend Analysis\n\n")
            f.write(analysis)

        print(f"  Saved to {out_file}")

    print("\nDone. Analysis files in output/analysis/")


if __name__ == "__main__":
    main()
