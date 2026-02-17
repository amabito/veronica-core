#!/usr/bin/env python3
"""
Production-safe 7-day X/Twitter scheduler for veronica-core launch.

Safety guarantees:
- Idempotent: never double-posts (state file tracks step + date)
- Observable: structured JSON logs to stderr + optional file
- Time-aware: JST timezone, 09:00-22:00 posting window
- Fail-loud: non-zero exit on any failure

State file: .post_state.json (project root)
Log file:   logs/x_poster.jsonl (append, structured JSON)

Usage:
  python scripts/post_scheduled.py                 # post today's step
  python scripts/post_scheduled.py --dry-run       # preview only
  python scripts/post_scheduled.py --step 3        # force step 3
  python scripts/post_scheduled.py --force          # ignore idempotency
  python scripts/post_scheduled.py --schedule-check # validate config only
"""

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

# Ensure Unicode output on Windows
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr.encoding and sys.stderr.encoding.lower() not in ("utf-8", "utf8"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

JST = ZoneInfo("Asia/Tokyo")
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCHEDULE_FILE = Path(__file__).resolve().parent / "schedule.json"
STATE_FILE = PROJECT_ROOT / ".post_state.json"
LOG_FILE = PROJECT_ROOT / "logs" / "x_poster.jsonl"

# Posting window (JST)
WINDOW_START_HOUR = 9   # 09:00 JST
WINDOW_END_HOUR = 22    # 22:00 JST

# ISO weekday -> schedule day (1=Mon -> Day 1, 7=Sun -> Day 7)
WEEKDAY_TO_DAY = {i: i for i in range(1, 8)}


# ---------------------------------------------------------------------------
# State management (atomic writes)
# ---------------------------------------------------------------------------

def load_state() -> dict:
    """Load .post_state.json. Returns empty dict if missing or corrupt."""
    if not STATE_FILE.exists():
        return {"thread_id": "veronica-launch-7day", "last_posted_step": 0, "posts": []}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"thread_id": "veronica-launch-7day", "last_posted_step": 0, "posts": []}
        return data
    except (json.JSONDecodeError, OSError):
        return {"thread_id": "veronica-launch-7day", "last_posted_step": 0, "posts": []}


def save_state(state: dict) -> None:
    """Atomic write: tmp file + rename to .post_state.json."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(state, indent=2, ensure_ascii=False) + "\n"

    fd, tmp_path = tempfile.mkstemp(
        dir=str(STATE_FILE.parent),
        prefix=".post_state_",
        suffix=".tmp",
    )
    try:
        os.write(fd, content.encode("utf-8"))
        os.close(fd)
        # On Windows, target must not exist for rename
        if STATE_FILE.exists():
            STATE_FILE.unlink()
        Path(tmp_path).rename(STATE_FILE)
    except Exception:
        os.close(fd) if not os.get_inheritable(fd) else None
        Path(tmp_path).unlink(missing_ok=True)
        raise


def is_step_posted(state: dict, step: int, date_str: str) -> bool:
    """Check if a specific step was already posted on a given date."""
    for post in state.get("posts", []):
        if post.get("step") == step and post.get("date") == date_str:
            if not post.get("dry_run", False):
                return True
    return False


def record_post(
    state: dict,
    step: int,
    date_str: str,
    tweet_id: str,
    dry_run: bool,
) -> dict:
    """Record a successful post in state. Returns updated state."""
    posts = list(state.get("posts", []))
    posts.append({
        "step": step,
        "date": date_str,
        "tweet_id": tweet_id,
        "posted_at": datetime.now(JST).isoformat(),
        "dry_run": dry_run,
        "tweet_url": (
            f"https://x.com/i/status/{tweet_id}"
            if not tweet_id.startswith("DRY_RUN")
            else None
        ),
    })
    new_last = max(state.get("last_posted_step", 0), step)
    return {
        **state,
        "last_posted_step": new_last,
        "posts": posts,
    }


# ---------------------------------------------------------------------------
# Schedule loader
# ---------------------------------------------------------------------------

def load_schedule() -> list[dict]:
    """Load and validate schedule.json."""
    if not SCHEDULE_FILE.exists():
        raise FileNotFoundError(f"Schedule not found: {SCHEDULE_FILE}")

    data = json.loads(SCHEDULE_FILE.read_text(encoding="utf-8"))
    if not isinstance(data, list) or len(data) == 0:
        raise ValueError("schedule.json must be a non-empty list")

    for i, entry in enumerate(data):
        for field in ("day", "text", "diagram"):
            if field not in entry:
                raise ValueError(f"schedule.json[{i}] missing required field: {field}")
        text_len = len(entry["text"])
        if text_len > 280:
            raise ValueError(
                f"schedule.json[{i}] (day {entry['day']}): "
                f"text is {text_len} chars (max 280)"
            )
        diagram_path = PROJECT_ROOT / entry["diagram"]
        if not diagram_path.exists():
            raise FileNotFoundError(
                f"schedule.json[{i}] (day {entry['day']}): "
                f"diagram not found: {diagram_path}"
            )

    return data


def get_step_entry(schedule: list[dict], step: int) -> dict:
    """Return the schedule entry for a given step (day) number."""
    for entry in schedule:
        if entry.get("day") == step:
            return entry
    raise ValueError(f"Step {step} not found in schedule (valid: 1-7)")


# ---------------------------------------------------------------------------
# Time window enforcement
# ---------------------------------------------------------------------------

def check_time_window(now: datetime) -> tuple[bool, str]:
    """Check if current JST time is within the posting window."""
    jst_now = now.astimezone(JST)
    hour = jst_now.hour

    if WINDOW_START_HOUR <= hour < WINDOW_END_HOUR:
        return True, f"JST {jst_now.strftime('%H:%M')} is within {WINDOW_START_HOUR:02d}:00-{WINDOW_END_HOUR:02d}:00"

    return False, (
        f"JST {jst_now.strftime('%H:%M')} is outside posting window "
        f"({WINDOW_START_HOUR:02d}:00-{WINDOW_END_HOUR:02d}:00). Aborting."
    )


# ---------------------------------------------------------------------------
# Schedule check (--schedule-check)
# ---------------------------------------------------------------------------

def schedule_check() -> int:
    """Validate secrets, schedule, diagrams, and state. Returns exit code."""
    print("=== Schedule Check ===\n")
    errors: list[str] = []

    # 1. Secrets
    print("[1/4] Secrets...")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from x_poster import validate_secrets
    try:
        masked = validate_secrets()
        for name, val in masked.items():
            print(f"  {name}: {val}")
        print("  -> OK\n")
    except EnvironmentError as e:
        print(f"  -> FAIL: {e}\n")
        errors.append(f"Secrets: {e}")

    # 2. Schedule
    print("[2/4] Schedule...")
    schedule: list[dict] = []
    try:
        schedule = load_schedule()
        print(f"  {len(schedule)} entries loaded")
        for entry in schedule:
            print(f"  Day {entry['day']}: {len(entry['text'])} chars, {entry['diagram']}")
        print("  -> OK\n")
    except (FileNotFoundError, ValueError) as e:
        print(f"  -> FAIL: {e}\n")
        errors.append(f"Schedule: {e}")

    # 3. Diagrams
    print("[3/4] Diagrams...")
    if schedule:
        for entry in schedule:
            p = PROJECT_ROOT / entry["diagram"]
            status = "OK" if p.exists() else "MISSING"
            size = f"{p.stat().st_size // 1024}KB" if p.exists() else "---"
            print(f"  Day {entry['day']}: {p.name} [{status}] {size}")
        print("  -> OK\n")
    else:
        print("  -> SKIP (schedule not loaded)\n")

    # 4. State
    print("[4/4] State...")
    state = load_state()
    posted_count = len([p for p in state.get("posts", []) if not p.get("dry_run")])
    last_step = state.get("last_posted_step", 0)
    print(f"  Posts recorded: {posted_count}")
    print(f"  Last posted step: {last_step}")
    print(f"  State file: {STATE_FILE}")
    print(f"  -> OK\n")

    # Summary
    if errors:
        print(f"FAIL: {len(errors)} error(s)")
        for err in errors:
            print(f"  - {err}")
        return 1

    now_jst = datetime.now(JST)
    allowed, reason = check_time_window(now_jst)
    window_status = "OPEN" if allowed else "CLOSED"
    print(f"Time window: {window_status} ({reason})")
    print(f"Current JST: {now_jst.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"\nAll checks passed. Ready to post.")
    return 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Production-safe 7-day X/Twitter scheduler for veronica-core"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview post without actually posting",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Ignore idempotency checks (post even if already posted today)",
    )
    parser.add_argument(
        "--step",
        type=int,
        choices=range(1, 8),
        metavar="N",
        help="Force-post a specific step/day (1-7)",
    )
    parser.add_argument(
        "--day",
        type=int,
        choices=range(1, 8),
        metavar="N",
        help="Alias for --step",
    )
    parser.add_argument(
        "--schedule-check",
        action="store_true",
        help="Validate secrets, schedule, and config only (no posting)",
    )
    parser.add_argument(
        "--ignore-window",
        action="store_true",
        help="Post outside the 09:00-22:00 JST window",
    )
    args = parser.parse_args()

    # Setup logging
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from x_poster import setup_logging, XPoster
    setup_logging(log_file=LOG_FILE)
    import logging
    log = logging.getLogger("x_poster")

    # --schedule-check: validate only
    if args.schedule_check:
        return schedule_check()

    # Resolve step (--step takes precedence over --day)
    step_override = args.step or args.day

    # Current time in JST
    now_jst = datetime.now(JST)
    today_str = now_jst.strftime("%Y-%m-%d")

    log.info("Scheduler started", extra={"_extra": {
        "date": today_str,
        "jst_time": now_jst.strftime("%H:%M:%S"),
        "dry_run": args.dry_run,
        "force": args.force,
        "step_override": step_override,
    }})

    # ---- Time window enforcement ----
    if not args.ignore_window and not args.dry_run:
        allowed, reason = check_time_window(now_jst)
        if not allowed:
            log.warning(f"Time window closed: {reason}")
            print(f"ABORT: {reason}")
            print("Use --ignore-window to override.")
            return 2
        log.info(f"Time window: {reason}")

    # ---- Determine which step to post ----
    if step_override is not None:
        step_number = step_override
        log.info(f"Step override: {step_number}")
    else:
        step_number = WEEKDAY_TO_DAY[now_jst.isoweekday()]
        log.info(f"Auto-detected step: {step_number} ({now_jst.strftime('%A')})")

    # ---- Load schedule ----
    try:
        schedule = load_schedule()
        entry = get_step_entry(schedule, step_number)
    except (FileNotFoundError, ValueError) as e:
        log.error(f"Schedule error: {e}")
        print(f"ERROR: {e}")
        return 1

    # ---- Idempotency check ----
    state = load_state()
    if not args.force and not args.dry_run:
        if is_step_posted(state, step_number, today_str):
            log.info("Idempotency: already posted", extra={"_extra": {
                "step": step_number, "date": today_str,
            }})
            print(f"SKIP: Step {step_number} already posted on {today_str}.")
            print("Use --force to override.")
            return 0

    # ---- Preview ----
    diagram_path = PROJECT_ROOT / entry["diagram"]
    weekday = entry.get("weekday", f"Day {step_number}")
    text = entry["text"]

    print(f"\n{'=' * 60}")
    print(f"Step {step_number} -- {weekday}")
    print(f"Date: {today_str} ({now_jst.strftime('%H:%M JST')})")
    print(f"Diagram: {entry['diagram']} ({diagram_path.stat().st_size // 1024}KB)")
    print(f"Text ({len(text)} chars):")
    print(f"{'-' * 40}")
    print(text)
    print(f"{'-' * 40}")
    print(f"{'=' * 60}")

    if args.dry_run:
        print("\n[DRY RUN] Would post the above. No API calls made.")

    # ---- Secrets validation (fail fast) ----
    if not args.dry_run:
        try:
            from x_poster import validate_secrets
            validate_secrets()
        except EnvironmentError as e:
            log.error(f"Secrets validation failed: {e}")
            print(f"ERROR: {e}")
            return 1

    # ---- Post ----
    poster = XPoster(dry_run=args.dry_run)

    try:
        media_id = poster.upload_media(str(diagram_path))
        tweet_id = poster.post_tweet(
            text=text,
            media_ids=[media_id],
        )
    except Exception as e:
        log.error(f"Posting failed: {e}", exc_info=True)
        print(f"ERROR: {e}")
        return 1

    # ---- Save state (atomic) ----
    updated_state = record_post(state, step_number, today_str, tweet_id, args.dry_run)
    save_state(updated_state)
    log.info("State saved", extra={"_extra": {
        "step": step_number,
        "tweet_id": tweet_id,
        "state_file": str(STATE_FILE),
    }})

    # ---- Success output ----
    if not args.dry_run:
        url = f"https://x.com/i/status/{tweet_id}"
        print(f"\nPosted: {url}")
    else:
        print(f"\n[DRY RUN] State saved to: {STATE_FILE}")

    print(f"Log: {LOG_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
