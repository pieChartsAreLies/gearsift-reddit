# gearsift-reddit

Reddit trend analysis for outdoor recreation content. Pulls public posts and comments from hiking/backpacking subreddits for offline analysis.

## Setup

```bash
pip install praw
cp .env.example .env
# Fill in your Reddit API credentials in .env
```

## Usage

```bash
python scrape.py
```

Output lands in `output/reddit-YYYY-MM-DD.json`.

## Target Subreddits

- r/ultralight
- r/hiking
- r/campinggear
- r/WildernessBackpacking
