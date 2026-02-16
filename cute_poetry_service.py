#!/usr/bin/env python3
"""Cute animal + poetry service for AWS Lambda or Google Cloud Run.

Features:
- Pulls a random image post from animal-focused subreddits.
- Pulls a random poetry line (PoetryDB primary, fallback local lines).
- Applies basic censorship to titles and poetry lines.
- Exposes:
  - lambda_handler(event, context) for AWS Lambda
  - Flask app for Cloud Run
"""

from __future__ import annotations

import os
import random
import re
from typing import Any, Dict, List, Optional

import requests
from flask import Flask, jsonify, request

USER_AGENT = os.getenv("USER_AGENT", "cute-poetry-bot/1.0 (+https://example.local)")
REDDIT_BASE = "https://www.reddit.com"
POETRY_API_URL = os.getenv("POETRY_API_URL", "https://poetrydb.org/random/20")
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "10"))

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


def reddit_json(path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    headers = {"User-Agent": USER_AGENT}
    resp = requests.get(f"{REDDIT_BASE}{path}", headers=headers, params=params or {}, timeout=REQUEST_TIMEOUT)
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


def pick_image_post(species: Optional[str] = None) -> Dict[str, Any]:
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

            if image_url:
                return {
                    "species": selected_species,
                    "subreddit": subreddit,
                    "title": post.get("title", ""),
                    "image_url": image_url.replace("&amp;", "&"),
                    "post_url": f"https://reddit.com{post.get('permalink', '')}",
                }

    raise RuntimeError(f"No suitable image post found for species '{selected_species}'.")


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
                if 20 <= len(line) <= 120 and line.lower() != "":
                    lines.append(line)

        if lines:
            return random.choice(lines)
    except requests.RequestException:
        pass

    return random.choice(FALLBACK_POETRY_LINES)


def generate_combo(species: Optional[str] = None) -> Dict[str, Any]:
    post = pick_image_post(species)
    poetry_line = pick_poetry_line()

    clean_title = censor_text(post["title"])
    clean_line = censor_text(poetry_line)

    caption = f"{clean_line} [{post['species']} from r/{post['subreddit']}]"

    return {
        "species": post["species"],
        "subreddit": post["subreddit"],
        "image_url": post["image_url"],
        "post_url": post["post_url"],
        "title": clean_title,
        "poetry_line": clean_line,
        "caption": caption,
    }


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    params = (event or {}).get("queryStringParameters") or {}
    species = params.get("species") if isinstance(params, dict) else None

    try:
        result = generate_combo(species=species)
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": jsonify(result).get_data(as_text=True),
        }
    except Exception as exc:
        return {
            "statusCode": 500,
            "headers": {"Content-Type": "application/json"},
            "body": jsonify({"error": str(exc)}).get_data(as_text=True),
        }


app = Flask(__name__)


@app.get("/")
def root() -> Any:
    species = request.args.get("species")
    try:
        return jsonify(generate_combo(species=species)), 200
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
