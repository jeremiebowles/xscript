#!/usr/bin/env python3
"""Cute animal + poetry service for AWS Lambda or Google Cloud Run.

Features:
- Pulls a random image post from animal-focused subreddits.
- Pulls a random poetry line (PoetryDB primary, fallback local lines).
- Applies basic censorship to titles and poetry lines.
- Avoids previously used image posts via a history file.
- Exposes:
  - lambda_handler(event, context) for AWS Lambda
  - Flask app for Cloud Run
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import time
from typing import Any, Dict, List, Optional, Set

import requests
from flask import Flask, jsonify, request

USER_AGENT = os.getenv("USER_AGENT", "cute-poetry-bot/1.0 (+https://example.local)")
REDDIT_BASE = "https://www.reddit.com"
REDDIT_OAUTH_BASE = "https://oauth.reddit.com"
REDDIT_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
REDDIT_CLIENT_ID = os.getenv("REDDIT_CLIENT_ID", "").strip()
REDDIT_CLIENT_SECRET = os.getenv("REDDIT_CLIENT_SECRET", "").strip()
POETRY_API_URL = os.getenv("POETRY_API_URL", "https://poetrydb.org/random/20")
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "10"))
HISTORY_FILE = os.getenv("HISTORY_FILE", "/tmp/cute_poetry_history.json")
MAX_HISTORY_ITEMS = int(os.getenv("MAX_HISTORY_ITEMS", "2000"))

SUBREDDITS_BY_SPECIES: Dict[str, List[str]] = {
    "dog": ["rarepuppers", "dogs", "dogpictures"],
    "cat": ["cats", "catpictures", "kittens"],
    "fox": ["foxes", "aww"],
    "badger": ["badgers", "aww"],
    "raccoon": ["raccoons", "trashpandas"],
    "opossum": ["opossum_irl", "opossums"],
}

ALLOWED_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp")

BANNED_WORDS = {
    "damn",
    "hell",
    "shit",
    "fuck",
    "bitch",
    "asshole",
    "bastard",
}

FALLBACK_POETRY_LINES = [
    "Hope is the thing with feathers.",
    "I wandered lonely as a cloud.",
    "Shall I compare thee to a summer's day?",
    "Two roads diverged in a yellow wood.",
    "Do not go gentle into that good night.",
]

_REDDIT_ACCESS_TOKEN: Optional[str] = None
_REDDIT_ACCESS_TOKEN_EXPIRY: float = 0.0


def censor_text(text: str) -> str:
    """Basic profanity censorship by masking inner letters."""

    def _mask(match: re.Match[str]) -> str:
        token = match.group(0)
        lower = token.lower()
        if lower not in BANNED_WORDS:
            return token
        if len(token) <= 2:
            return "*" * len(token)
        return token[0] + ("*" * (len(token) - 2)) + token[-1]

    return re.sub(r"\b[a-zA-Z']+\b", _mask, text)


def _get_reddit_access_token() -> str:
    global _REDDIT_ACCESS_TOKEN, _REDDIT_ACCESS_TOKEN_EXPIRY

    now = time.time()
    if _REDDIT_ACCESS_TOKEN and now < _REDDIT_ACCESS_TOKEN_EXPIRY:
        return _REDDIT_ACCESS_TOKEN

    if not REDDIT_CLIENT_ID or not REDDIT_CLIENT_SECRET:
        raise RuntimeError("Missing Reddit API credentials.")

    resp = requests.post(
        REDDIT_TOKEN_URL,
        auth=(REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET),
        data={"grant_type": "client_credentials"},
        headers={"User-Agent": USER_AGENT},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    token_data = resp.json()
    token = token_data.get("access_token")
    expires_in = int(token_data.get("expires_in", 3600))

    if not token:
        raise RuntimeError("Reddit token response missing access_token.")

    _REDDIT_ACCESS_TOKEN = str(token)
    _REDDIT_ACCESS_TOKEN_EXPIRY = now + max(60, expires_in - 60)
    return _REDDIT_ACCESS_TOKEN


def reddit_json(path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    query = dict(params or {})
    query["raw_json"] = 1
    headers = {"User-Agent": USER_AGENT}

    base_url = REDDIT_BASE
    if REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET:
        base_url = REDDIT_OAUTH_BASE
        headers["Authorization"] = f"bearer {_get_reddit_access_token()}"

    resp = requests.get(f"{base_url}{path}", headers=headers, params=query, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def is_image_post(post: Dict[str, Any]) -> bool:
    if post.get("over_18"):
        return False
    if post.get("is_video"):
        return False

    url = (post.get("url_overridden_by_dest") or post.get("url") or "").lower()
    if any(url.endswith(ext) for ext in ALLOWED_EXTENSIONS):
        return True

    preview = post.get("preview", {})
    images = preview.get("images", []) if isinstance(preview, dict) else []
    return bool(images)


def load_history() -> List[str]:
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return [str(x) for x in data if x]
    except FileNotFoundError:
        return []
    except (json.JSONDecodeError, OSError):
        return []
    return []


def save_history(keys: List[str]) -> None:
    dir_name = os.path.dirname(HISTORY_FILE)
    if dir_name:
        os.makedirs(dir_name, exist_ok=True)

    trimmed = keys[-MAX_HISTORY_ITEMS:]
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(trimmed, f)


def add_history_key(history: List[str], key: str) -> List[str]:
    filtered = [item for item in history if item != key]
    filtered.append(key)
    return filtered[-MAX_HISTORY_ITEMS:]


def pick_image_post(species: Optional[str] = None, seen_keys: Optional[Set[str]] = None) -> Dict[str, Any]:
    seen = seen_keys or set()
    species_choices = list(SUBREDDITS_BY_SPECIES.keys())
    selected_species = (species or "").strip().lower()
    if selected_species not in SUBREDDITS_BY_SPECIES:
        selected_species = random.choice(species_choices)

    subreddit_choices = SUBREDDITS_BY_SPECIES[selected_species][:]
    random.shuffle(subreddit_choices)

    for subreddit in subreddit_choices:
        listing = reddit_json(f"/r/{subreddit}/hot.json", {"limit": 75})
        posts = listing.get("data", {}).get("children", [])
        random.shuffle(posts)

        for child in posts:
            post = child.get("data", {})
            if not is_image_post(post):
                continue

            image_url = post.get("url_overridden_by_dest") or post.get("url")
            if not image_url:
                preview_images = post.get("preview", {}).get("images", [])
                if preview_images:
                    image_url = preview_images[0].get("source", {}).get("url")

            if not image_url:
                continue

            post_key = post.get("id") or image_url
            if post_key in seen:
                continue

            return {
                "post_key": str(post_key),
                "species": selected_species,
                "subreddit": subreddit,
                "title": post.get("title", ""),
                "image_url": image_url.replace("&amp;", "&"),
                "post_url": f"https://reddit.com{post.get('permalink', '')}",
            }

    raise RuntimeError(
        f"No new unused image post found for species '{selected_species}'. "
        "If Reddit returns 403, configure REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET."
    )


def pick_poetry_line() -> str:
    try:
        resp = requests.get(POETRY_API_URL, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        poems = resp.json()
        if isinstance(poems, dict):
            poems = [poems]

        lines: List[str] = []
        for poem in poems:
            poem_lines = poem.get("lines", []) if isinstance(poem, dict) else []
            for line in poem_lines:
                line = (line or "").strip()
                if 20 <= len(line) <= 120:
                    lines.append(line)

        if lines:
            return random.choice(lines)
    except requests.RequestException:
        pass

    return random.choice(FALLBACK_POETRY_LINES)


def generate_combo(species: Optional[str] = None) -> Dict[str, Any]:
    history = load_history()
    seen_keys = set(history)

    post = pick_image_post(species=species, seen_keys=seen_keys)
    poetry_line = pick_poetry_line()

    clean_title = censor_text(post["title"])
    clean_line = censor_text(poetry_line)

    caption = f"{clean_line} [{post['species']} from r/{post['subreddit']}]"

    updated_history = add_history_key(history, post["post_key"])
    save_history(updated_history)

    return {
        "species": post["species"],
        "subreddit": post["subreddit"],
        "image_url": post["image_url"],
        "post_url": post["post_url"],
        "title": clean_title,
        "poetry_line": clean_line,
        "caption": caption,
        "already_posted_check": "passed",
    }


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    params = (event or {}).get("queryStringParameters") or {}
    species = params.get("species") if isinstance(params, dict) else None

    try:
        result = generate_combo(species=species)
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(result),
        }
    except Exception as exc:
        return {
            "statusCode": 500,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"error": str(exc)}),
        }


app = Flask(__name__)


@app.get("/")
def root() -> Any:
    species = request.args.get("species")
    try:
        return jsonify(generate_combo(species=species)), 200
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


def main() -> None:
    parser = argparse.ArgumentParser(description="Cute animal + poetry service")
    parser.add_argument("--once", action="store_true", help="Run once and print JSON result")
    parser.add_argument("--species", type=str, default=None, help="Optional species override")
    args = parser.parse_args()

    if args.once:
        print(json.dumps(generate_combo(species=args.species)))
        return

    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
