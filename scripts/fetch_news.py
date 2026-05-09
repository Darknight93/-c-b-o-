#!/usr/bin/env python3
"""
NewsBot - Fetch and summarize news from multiple sources
Sources: Reddit, RSS feeds (major newspapers), Voz (via RSS/scraping)
"""

import os
import json
import time
import hashlib
import logging
from datetime import datetime, timezone
from typing import Optional

import feedparser
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"

NEWS_JSON_PATH = os.path.join(os.path.dirname(__file__), "../docs/news.json")
MAX_ARTICLES_TOTAL = 40   # keep newest N articles across all sources
MAX_PER_SOURCE    = 8     # fetch at most N articles per source per run

CATEGORY_KEYWORDS = {
    "politics":   ["politics","election","government","congress","senate","president","policy","diplomacy","war","military","treaty","chính trị","bầu cử","quốc hội","chính phủ"],
    "economy":    ["economy","economic","market","stock","gdp","inflation","fed","bank","trade","finance","recession","kinh tế","thị trường","tài chính","ngân hàng"],
    "technology": ["technology","tech","ai","artificial intelligence","software","hardware","startup","silicon","chip","robot","công nghệ","trí tuệ nhân tạo","phần mềm"],
    "science":    ["science","research","study","space","nasa","climate","biology","physics","discovery","khoa học","nghiên cứu","vũ trụ","khí hậu"],
}

SOURCES = [
    # --- Reddit (via JSON API) ---
    {"type": "reddit", "name": "Reddit r/worldnews",    "url": "https://www.reddit.com/r/worldnews/hot.json",    "icon": "🌍"},
    {"type": "reddit", "name": "Reddit r/technology",   "url": "https://www.reddit.com/r/technology/hot.json",   "icon": "💻"},
    {"type": "reddit", "name": "Reddit r/science",      "url": "https://www.reddit.com/r/science/hot.json",      "icon": "🔬"},
    {"type": "reddit", "name": "Reddit r/economics",    "url": "https://www.reddit.com/r/Economics/hot.json",    "icon": "📈"},

    # --- RSS Feeds ---
    {"type": "rss", "name": "BBC World",         "url": "https://feeds.bbci.co.uk/news/world/rss.xml",          "icon": "🇬🇧"},
    {"type": "rss", "name": "BBC Technology",    "url": "https://feeds.bbci.co.uk/news/technology/rss.xml",     "icon": "🇬🇧"},
    {"type": "rss", "name": "Reuters Top News",  "url": "https://feeds.reuters.com/reuters/topNews",            "icon": "📰"},
    {"type": "rss", "name": "Reuters Technology","url": "https://feeds.reuters.com/reuters/technologyNews",     "icon": "📰"},
    {"type": "rss", "name": "AP Top News",       "url": "https://feeds.apnews.com/ApNewsAlert",                 "icon": "🗞️"},
    {"type": "rss", "name": "The Guardian World","url": "https://www.theguardian.com/world/rss",                "icon": "🇬🇧"},
    {"type": "rss", "name": "Ars Technica",      "url": "https://feeds.arstechnica.com/arstechnica/index",      "icon": "💡"},
    {"type": "rss", "name": "Hacker News",       "url": "https://hnrss.org/frontpage",                         "icon": "🟠"},
    {"type": "rss", "name": "Nature",            "url": "https://www.nature.com/nature.rss",                   "icon": "🧬"},
    {"type": "rss", "name": "Science Daily",     "url": "https://www.sciencedaily.com/rss/all.xml",            "icon": "🔭"},

    # --- Voz (Vietnamese forum - RSS nếu có, fallback scrape) ---
    {"type": "voz", "name": "Voz Trending",      "url": "https://voz.vn",                                      "icon": "🇻🇳"},
]

HEADERS = {"User-Agent": "Mozilla/5.0 NewsBot/1.0 (educational project)"}


def classify_category(text: str) -> str:
    text_lower = text.lower()
    scores = {}
    for cat, keywords in CATEGORY_KEYWORDS.items():
        scores[cat] = sum(1 for kw in keywords if kw in text_lower)
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "other"


def fetch_reddit(source: dict) -> list[dict]:
    try:
        r = requests.get(source["url"], headers=HEADERS, timeout=10)
        r.raise_for_status()
        posts = r.json()["data"]["children"]
        articles = []
        for p in posts[:MAX_PER_SOURCE]:
            d = p["data"]
            if d.get("is_self") or d.get("stickied"):
                continue
            articles.append({
                "title":       d.get("title", ""),
                "url":         d.get("url", ""),
                "source":      source["name"],
                "icon":        source["icon"],
                "score":       d.get("score", 0),
                "comments":    d.get("num_comments", 0),
                "description": d.get("selftext", "")[:800] or d.get("title", ""),
            })
        return articles
    except Exception as e:
        log.warning(f"Reddit fetch failed ({source['name']}): {e}")
        return []


def fetch_rss(source: dict) -> list[dict]:
    try:
        feed = feedparser.parse(source["url"])
        articles = []
        for entry in feed.entries[:MAX_PER_SOURCE]:
            desc = entry.get("summary", "") or entry.get("description", "")
            # strip basic HTML tags
            import re
            desc = re.sub(r"<[^>]+>", " ", desc).strip()[:800]
            articles.append({
                "title":       entry.get("title", ""),
                "url":         entry.get("link", ""),
                "source":      source["name"],
                "icon":        source["icon"],
                "score":       0,
                "comments":    0,
                "description": desc,
            })
        return articles
    except Exception as e:
        log.warning(f"RSS fetch failed ({source['name']}): {e}")
        return []


def fetch_voz(source: dict) -> list[dict]:
    """
    Voz doesn't have a public RSS; scrape the trending threads lightly.
    Falls back to empty list if blocked.
    """
    try:
        r = requests.get(source["url"], headers=HEADERS, timeout=10)
        r.raise_for_status()
        from html.parser import HTMLParser

        class VozParser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.articles = []
                self._in_link = False
                self._current = {}

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == "a" and "href" in attrs:
                    href = attrs["href"]
                    if "/threads/" in href and "title" in attrs:
                        full = href if href.startswith("http") else "https://voz.vn" + href
                        self._current = {"url": full, "title": attrs.get("title", attrs.get("href",""))}
                        self._in_link = True

            def handle_endtag(self, tag):
                if tag == "a" and self._in_link and self._current.get("title"):
                    self.articles.append(self._current)
                    self._in_link = False
                    self._current = {}

        parser = VozParser()
        parser.feed(r.text)
        seen = set()
        results = []
        for a in parser.articles:
            if a["url"] not in seen and len(results) < MAX_PER_SOURCE:
                seen.add(a["url"])
                results.append({
                    "title":       a["title"],
                    "url":         a["url"],
                    "source":      source["name"],
                    "icon":        source["icon"],
                    "score":       0,
                    "comments":    0,
                    "description": a["title"],
                })
        return results
    except Exception as e:
        log.warning(f"Voz fetch failed: {e}")
        return []


def summarize_article(article: dict) -> Optional[dict]:
    prompt = f"""You are a concise news summarizer for a Vietnamese news aggregator.

Article:
Title: {article['title']}
Source: {article['source']}
URL: {article['url']}
Content: {article['description']}

Return ONLY a JSON object (no markdown, no extra text) with these exact fields:
{{
  "headline": "one punchy sentence summarizing the news in Vietnamese (max 120 chars)",
  "bullets": ["3-4 key points in Vietnamese, each starting with a verb", "..."],
  "full_summary": "3-5 sentence paragraph in Vietnamese with full context",
  "category": "politics|economy|technology|science|other",
  "importance": 1-5
}}"""

    try:
        payload = {"contents": [{"parts": [{"text": prompt}]}]}
        r = requests.post(GEMINI_URL, json=payload, timeout=30)
        r.raise_for_status()
        raw = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        data = json.loads(raw.strip())
        return data
    except Exception as e:
        log.warning(f"Summarization failed for '{article['title']}': {e}")
        return None


def article_id(article: dict) -> str:
    return hashlib.md5(article["url"].encode()).hexdigest()[:12]


def load_existing_news() -> list[dict]:
    try:
        with open(NEWS_JSON_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("articles", [])
    except FileNotFoundError:
        return []
    except Exception as e:
        log.warning(f"Could not load existing news: {e}")
        return []


def save_news(articles: list[dict]):
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "articles": articles,
    }
    os.makedirs(os.path.dirname(NEWS_JSON_PATH), exist_ok=True)
    with open(NEWS_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    log.info(f"Saved {len(articles)} articles to {NEWS_JSON_PATH}")


def run():
    log.info("=== NewsBot starting ===")
    existing_ids = {a["id"] for a in load_existing_news()}
    existing_articles = load_existing_news()

    raw_articles = []
    for source in SOURCES:
        log.info(f"Fetching: {source['name']}")
        if source["type"] == "reddit":
            raw_articles.extend(fetch_reddit(source))
        elif source["type"] == "rss":
            raw_articles.extend(fetch_rss(source))
        elif source["type"] == "voz":
            raw_articles.extend(fetch_voz(source))
        time.sleep(1)  # be polite

    new_raw = [a for a in raw_articles if article_id(a) not in existing_ids]
    log.info(f"Found {len(new_raw)} new articles (out of {len(raw_articles)} fetched)")

    # Summarize new articles
    new_articles = []
    for a in new_raw[:20]:  # cap at 20 new articles per run to save API tokens
        log.info(f"Summarizing: {a['title'][:60]}...")
        summary = summarize_article(a)
        if summary:
            new_articles.append({
                "id":           article_id(a),
                "title":        a["title"],
                "url":          a["url"],
                "source":       a["source"],
                "icon":         a["icon"],
                "fetched_at":   datetime.now(timezone.utc).isoformat(),
                "category":     summary.get("category", classify_category(a["title"])),
                "importance":   summary.get("importance", 3),
                "headline":     summary.get("headline", a["title"]),
                "bullets":      summary.get("bullets", []),
                "full_summary": summary.get("full_summary", ""),
                "comments":     a.get("comments", 0),
                "score":        a.get("score", 0),
            })
        time.sleep(0.5)

    # Merge: new first, then existing, trimmed to MAX_ARTICLES_TOTAL
    merged = new_articles + existing_articles
    merged = merged[:MAX_ARTICLES_TOTAL]
    save_news(merged)
    log.info(f"=== Done. {len(new_articles)} new articles added ===")


if __name__ == "__main__":
    run()
