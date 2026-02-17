#!/usr/bin/env python3
"""
X/Twitter posting module for veronica-core launch.

Production-safe:
- Structured JSON logging
- Secrets validation with masking
- Explicit error handling with non-zero exits

Supports:
- Text posting (Twitter API v2)
- Image/media upload (Twitter API v1.1)
- Thread posting (reply chain)

Environment variables:
  TWITTER_API_KEY, TWITTER_API_SECRET,
  TWITTER_ACCESS_TOKEN, TWITTER_ACCESS_TOKEN_SECRET

Usage:
  from x_poster import XPoster
  poster = XPoster()  # or XPoster(dry_run=True)
  poster.post_thread([
      {"text": "Post 1", "image": "path/to/image.png"},
      {"text": "Post 2"},
  ])
"""

import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

# Load .env from project root (scripts/../.env)
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# Ensure Unicode output on Windows
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr.encoding and sys.stderr.encoding.lower() not in ("utf-8", "utf8"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

JST = ZoneInfo("Asia/Tokyo")

REQUIRED_SECRETS = (
    "TWITTER_API_KEY",
    "TWITTER_API_SECRET",
    "TWITTER_ACCESS_TOKEN",
    "TWITTER_ACCESS_TOKEN_SECRET",
)


# ---------------------------------------------------------------------------
# Structured JSON logging
# ---------------------------------------------------------------------------

class JSONFormatter(logging.Formatter):
    """Emit one JSON object per log line (structured logging)."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.now(JST).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0] is not None:
            entry["error"] = self.formatException(record.exc_info)
        extra = getattr(record, "_extra", None)
        if extra:
            entry["data"] = extra
        return json.dumps(entry, ensure_ascii=False)


def setup_logging(log_file: Optional[Path] = None) -> None:
    """Configure structured JSON logging to stderr and optional file."""
    logger = logging.getLogger("x_poster")
    logger.setLevel(logging.DEBUG)

    if not logger.handlers:
        # stderr handler
        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setFormatter(JSONFormatter())
        stderr_handler.setLevel(logging.DEBUG)
        logger.addHandler(stderr_handler)

        # file handler (optional)
        if log_file:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(
                str(log_file), encoding="utf-8"
            )
            file_handler.setFormatter(JSONFormatter())
            file_handler.setLevel(logging.DEBUG)
            logger.addHandler(file_handler)


logger = logging.getLogger("x_poster")


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------

def mask_secret(value: str, show: int = 4) -> str:
    """Mask a secret for safe logging. Shows first `show` chars."""
    if not value or len(value) <= show:
        return "****"
    return value[:show] + "****"


def validate_secrets() -> dict[str, str]:
    """Validate all required Twitter secrets are present.

    Returns:
        dict of {name: masked_value} for logging.

    Raises:
        EnvironmentError if any secret is missing.
    """
    result: dict[str, str] = {}
    missing: list[str] = []

    for name in REQUIRED_SECRETS:
        value = os.environ.get(name, "")
        if value:
            result[name] = mask_secret(value)
        else:
            missing.append(name)

    if missing:
        raise EnvironmentError(
            f"Missing required secrets: {', '.join(missing)}\n"
            "Set them as environment variables or GitHub Secrets."
        )

    logger.info("Secrets validated", extra={"_extra": result})
    return result


# ---------------------------------------------------------------------------
# XPoster
# ---------------------------------------------------------------------------

class XPoster:
    """Twitter/X poster with production safety features."""

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run

        if dry_run:
            logger.info("XPoster initialized in DRY RUN mode")
            self._api_v1 = None
            self._client_v2 = None
        else:
            import tweepy

            api_key = os.environ["TWITTER_API_KEY"]
            api_secret = os.environ["TWITTER_API_SECRET"]
            access_token = os.environ["TWITTER_ACCESS_TOKEN"]
            access_token_secret = os.environ["TWITTER_ACCESS_TOKEN_SECRET"]

            # v1.1 API (for media upload)
            auth = tweepy.OAuth1UserHandler(
                api_key, api_secret,
                access_token, access_token_secret,
            )
            self._api_v1 = tweepy.API(auth)

            # v2 Client (for create_tweet)
            self._client_v2 = tweepy.Client(
                consumer_key=api_key,
                consumer_secret=api_secret,
                access_token=access_token,
                access_token_secret=access_token_secret,
            )

            logger.info("XPoster initialized (live mode)", extra={"_extra": {
                "api_key": mask_secret(api_key),
            }})

    def upload_media(self, image_path: str) -> str:
        """Upload an image and return the media_id string."""
        path = Path(image_path)
        if not path.exists():
            raise FileNotFoundError(f"Image not found: {path}")

        if self.dry_run:
            logger.info("DRY RUN: would upload media", extra={"_extra": {
                "image": path.name,
                "size_kb": path.stat().st_size // 1024,
            }})
            return "DRY_RUN_MEDIA_ID"

        media = self._api_v1.media_upload(filename=str(path))
        media_id = str(media.media_id)
        logger.info("Media uploaded", extra={"_extra": {
            "image": path.name,
            "media_id": media_id,
            "size_kb": path.stat().st_size // 1024,
        }})
        return media_id

    def post_tweet(
        self,
        text: str,
        media_ids: Optional[list[str]] = None,
        reply_to: Optional[str] = None,
    ) -> str:
        """Post a tweet. Returns tweet ID string."""
        if len(text) > 280:
            raise ValueError(f"Tweet too long: {len(text)} chars (max 280)")

        if self.dry_run:
            logger.info("DRY RUN: would post tweet", extra={"_extra": {
                "chars": len(text),
                "has_media": bool(media_ids),
                "reply_to": reply_to,
                "text_preview": text[:80],
            }})
            return "DRY_RUN_TWEET_ID"

        kwargs: dict = {"text": text}
        if media_ids:
            kwargs["media_ids"] = media_ids
        if reply_to:
            kwargs["in_reply_to_tweet_id"] = reply_to

        response = self._client_v2.create_tweet(**kwargs)
        tweet_id = str(response.data["id"])

        logger.info("Tweet posted", extra={"_extra": {
            "tweet_id": tweet_id,
            "chars": len(text),
            "has_media": bool(media_ids),
            "reply_to": reply_to,
            "url": f"https://x.com/i/status/{tweet_id}",
        }})
        return tweet_id

    def post_thread(self, posts: list[dict]) -> list[str]:
        """Post a thread (list of posts). Returns list of tweet IDs."""
        tweet_ids: list[str] = []
        reply_to: Optional[str] = None

        for i, post in enumerate(posts):
            # Upload media if present
            media_ids = None
            if "image" in post and post["image"]:
                media_id = self.upload_media(post["image"])
                media_ids = [media_id]

            # Post tweet (reply to previous if threading)
            tweet_id = self.post_tweet(
                text=post["text"],
                media_ids=media_ids,
                reply_to=reply_to,
            )
            tweet_ids.append(tweet_id)
            reply_to = tweet_id  # chain replies

            logger.info(f"Thread post {i + 1}/{len(posts)} done", extra={"_extra": {
                "tweet_id": tweet_id,
                "position": i + 1,
                "total": len(posts),
            }})

        return tweet_ids


# ---------------------------------------------------------------------------
# CLI: python x_poster.py --validate
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="X/Twitter poster utilities")
    parser.add_argument(
        "--validate", action="store_true",
        help="Validate secrets and exit",
    )
    args = parser.parse_args()

    setup_logging()

    if args.validate:
        try:
            masked = validate_secrets()
            for name, val in masked.items():
                print(f"  {name}: {val}")
            print("Secrets OK")
            sys.exit(0)
        except EnvironmentError as e:
            print(f"FAIL: {e}", file=sys.stderr)
            sys.exit(1)
