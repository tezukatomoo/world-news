import json
import time
import threading
from datetime import datetime, timezone
from pathlib import Path

import feedparser
from deep_translator import GoogleTranslator
from flask import Flask, render_template, jsonify, request

app = Flask(__name__)

# --- Configuration ---
FEEDS_FILE = Path(__file__).parent / "feeds.json"
CACHE_FILE = Path(__file__).parent / "cache.json"
FETCH_INTERVAL = 1800  # 30 minutes

translator = GoogleTranslator(source="en", target="ja")

# In-memory article cache
articles_cache = {"general": [], "conflict": [], "architecture": [], "last_updated": None}
cache_lock = threading.Lock()


def load_feeds_config():
    with open(FEEDS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def translate_text(text, lang="en"):
    """Translate text to Japanese. Skip if already Japanese."""
    if not text or lang == "ja":
        return text
    try:
        # deep-translator has a 5000 char limit per request
        if len(text) > 4500:
            text = text[:4500] + "..."
        result = translator.translate(text)
        return result if result else text
    except Exception:
        return text


def parse_date(entry):
    """Extract published date from feed entry."""
    for attr in ("published_parsed", "updated_parsed"):
        t = getattr(entry, attr, None)
        if t:
            try:
                return datetime(*t[:6], tzinfo=timezone.utc).isoformat()
            except Exception:
                pass
    return datetime.now(timezone.utc).isoformat()


def matches_keywords(text, keywords):
    """Check if text contains any of the keywords (case-insensitive)."""
    if not keywords:
        return True
    text_lower = text.lower()
    return any(kw.lower() in text_lower for kw in keywords)


def fetch_category(category, config):
    """Fetch and translate articles for a category."""
    results = []
    keywords = config.get("keywords", [])
    seen_titles = set()

    for source in config["feeds"]:
        try:
            feed = feedparser.parse(source["url"])
            for entry in feed.entries[:15]:
                title = entry.get("title", "").strip()
                if not title or title in seen_titles:
                    continue

                summary = entry.get("summary", entry.get("description", "")).strip()
                # Remove HTML tags simply
                import re
                summary = re.sub(r"<[^>]+>", "", summary).strip()

                full_text = f"{title} {summary}"

                # For conflict category, filter by keywords
                if keywords and not matches_keywords(full_text, keywords):
                    continue

                seen_titles.add(title)

                lang = source.get("lang", "en")
                title_ja = translate_text(title, lang)
                summary_ja = translate_text(summary[:500], lang) if summary else ""

                # Add small delay to avoid rate limiting
                if lang != "ja":
                    time.sleep(0.3)

                results.append({
                    "title": title_ja,
                    "title_orig": title,
                    "summary": summary_ja,
                    "link": entry.get("link", ""),
                    "source": source["name"],
                    "lang": lang,
                    "published": parse_date(entry),
                    "category": category,
                })
        except Exception as e:
            app.logger.warning(f"Failed to fetch {source['name']}: {e}")
            continue

    # Sort by date descending
    results.sort(key=lambda x: x["published"], reverse=True)
    return results[:30]


def fetch_all_feeds():
    """Fetch all categories."""
    import sys
    print("==> Fetching feeds...", flush=True)
    config = load_feeds_config()
    new_cache = {"last_updated": datetime.now(timezone.utc).isoformat()}

    for category, cat_config in config.items():
        articles = fetch_category(category, cat_config)
        new_cache[category] = articles
        print(f"  {category}: {len(articles)} articles", flush=True)

    with cache_lock:
        global articles_cache
        articles_cache = new_cache

    # Save to disk
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(new_cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        app.logger.warning(f"Failed to save cache: {e}")

    print("==> Feed fetch complete.", flush=True)


def load_cache_from_disk():
    """Load cached articles from disk on startup."""
    global articles_cache
    if CACHE_FILE.exists():
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                articles_cache = json.load(f)
            app.logger.info("Loaded cache from disk.")
        except Exception:
            pass


def start_scheduler():
    """Start background feed fetching."""
    def run():
        while True:
            try:
                fetch_all_feeds()
            except Exception as e:
                app.logger.error(f"Scheduler error: {e}")
            time.sleep(FETCH_INTERVAL)

    t = threading.Thread(target=run, daemon=True)
    t.start()


# --- Routes ---

@app.route("/")
def index():
    config = load_feeds_config()
    categories = {k: v["label"] for k, v in config.items()}
    return render_template("index.html", categories=categories)


@app.route("/api/articles")
def api_articles():
    category = request.args.get("category", "general")
    with cache_lock:
        data = articles_cache.get(category, [])
        last_updated = articles_cache.get("last_updated")
    return jsonify({"articles": data, "last_updated": last_updated})


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    """Trigger manual refresh (runs in background)."""
    t = threading.Thread(target=fetch_all_feeds, daemon=True)
    t.start()
    return jsonify({"status": "refreshing"})


# --- Startup ---
print("==> Starting World News Japan...", flush=True)
load_cache_from_disk()
start_scheduler()
print("==> Scheduler started. Fetching feeds in background.", flush=True)

if __name__ == "__main__":
    app.run(debug=False, port=5001)
