#!/usr/bin/env python3
"""StopFailure hook: when a usage limit stops a turn, push a notification once it resets.

The hook payload arrives on stdin. The foreground call hands it to a detached copy of this
script (--watch) and returns at once. The watcher reads the reset time from the limit error in
the transcript, sleeps until then, and posts to ntfy, which also emails it. One watcher runs per reset time.
"""
import json
import os
import re
import subprocess
import sys
import time
import traceback
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# Both overridable so the whole path can be tested against a local server.
DIR = os.environ.get('LIMIT_WATCH_DIR') or os.path.expanduser('~/.claude/limit-watch')
NTFY_URL = os.environ.get('LIMIT_WATCH_NTFY_URL') or 'https://ntfy.sh/'
MARGIN = timedelta(seconds=60)  # pad past the stated time so the push never precedes the restore
LOOKUP_TRIES = 10  # the limit error may not be in the transcript yet when the hook fires

# The message states local time plus the IANA zone of the machine running Claude Code:
# "You've hit your session limit · resets 2:20pm (UTC)", "... weekly limit · resets Oct 6, 3am (Europe/Lisbon)"
# (with the year too when the reset falls in the next one), and for the org spend limit
# "You've hit your org's monthly spend limit · ... · your session limit resets 7:10pm (UTC)"
RESET = re.compile(r'resets (?:([A-Z][a-z]{2}) (\d{1,2}), (?:(\d{4}), )?)?(\d{1,2})(?::(\d{2}))?(am|pm) \(([\w/+-]+)\)')
WINDOW = re.compile(r'(session|weekly) limit')


def log(msg):
    with open(os.path.join(DIR, 'log.txt'), 'a') as f:
        f.write(f'{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z {msg}\n')


def parse_reset(text, at):
    """Reset time stated in a limit message as UTC, or None if absent or in an unknown zone.
    `at` is when the message was shown; a time without a date is the next such time after it."""
    m = RESET.search(text)
    if not m:
        return None
    mon, day, year, hh, mm, ap, tz = m.groups()
    try:
        zone = timezone.utc if tz == 'UTC' else ZoneInfo(tz)
    except (KeyError, ValueError, OSError):
        return None
    now = at.astimezone(zone)
    hour, minute = int(hh) % 12 + (12 if ap == 'pm' else 0), int(mm or 0)
    if mon:
        d = datetime.strptime(f'{year or now.year} {mon} {day}', '%Y %b %d').replace(hour=hour, minute=minute, tzinfo=zone)
        if not year and d < now - timedelta(days=1):
            d = d.replace(year=d.year + 1)
    else:
        d = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if d <= now:
            d += timedelta(days=1)
    return d.astimezone(timezone.utc)


def last_limit_error(path):
    """(text, time) of the newest limit error in the transcript's tail, if logged in the last 5 minutes."""
    try:
        with open(path, 'rb') as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 262144))
            lines = f.read().decode('utf-8', 'replace').splitlines()
    except (OSError, TypeError):
        return None
    for line in reversed(lines):
        if '"isApiErrorMessage":true' not in line:
            continue
        try:
            e = json.loads(line)
            c = e['message']['content']
            text = c if isinstance(c, str) else ' '.join(x.get('text', '') for x in c)
            at = datetime.fromisoformat(e['timestamp'].replace('Z', '+00:00'))
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
        if RESET.search(text) and datetime.now(timezone.utc) - at < timedelta(minutes=5):
            return text, at
    return None


def holder_alive(marker):
    try:
        with open(marker) as f:
            pid = int(f.read())
        with open(f'/proc/{pid}/cmdline') as f:
            return 'limit_restore_notify' in f.read()
    except ValueError:  # the holder created the file but has not written its pid yet
        return True
    except OSError:  # no such process, or the marker is already gone
        return False


def claim(marker):
    """Atomically take the marker for one reset time; False if a live watcher already holds it."""
    for _ in range(2):
        try:
            fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if holder_alive(marker):
                return False
            try:
                os.unlink(marker)  # left by a watcher that died
            except FileNotFoundError:
                pass
            continue
        with os.fdopen(fd, 'w') as f:
            f.write(str(os.getpid()))
        return True
    return False


def sleep_until(t):
    while (left := (t - datetime.now(timezone.utc)).total_seconds()) > 0:
        time.sleep(min(left, 60))  # re-check the wall clock so a clock jump cannot oversleep


def read_setting(name):
    with open(os.path.join(DIR, name)) as f:
        return f.read().strip()


def push(window):
    req = urllib.request.Request(
        NTFY_URL + read_setting('ntfy_topic'),
        data=f'Your {window} limit has reset. You can continue.'.encode(),
        headers={
            'Title': 'Claude limit restored',
            'Tags': 'white_check_mark',
            'Email': read_setting('notify_email'),
            'Authorization': f"Bearer {read_setting('ntfy_token')}",  # ntfy.sh refuses anonymous email
        },
    )
    for attempt in range(1, 4):
        try:
            urllib.request.urlopen(req, timeout=15).read()
            log(f'pushed (attempt {attempt})')
            return
        except Exception as e:  # network blips; the error text carries no topic
            log(f'push failed (attempt {attempt}): {e!r}')
            time.sleep(30)
    log('gave up pushing')


def watch(payload):
    hit = None
    for _ in range(LOOKUP_TRIES):
        hit = last_limit_error(payload.get('transcript_path'))
        if hit:
            break
        time.sleep(1)
    if not hit:
        log(f"no limit message in transcript (error={payload.get('error')!r}, "
            f"details={str(payload.get('error_details'))[:120]!r})")
        return
    text, at = hit
    reset = parse_reset(text, at)
    if not reset or reset - datetime.now(timezone.utc) > timedelta(days=8):
        log(f'unusable reset time in: {text[:200]!r}')
        return
    m = WINDOW.search(text)
    window = m.group(1) if m else 'usage'
    marker = os.path.join(DIR, f'{int(reset.timestamp())}.pid')
    if not claim(marker):
        log(f'already watching the {reset:%Y-%m-%d %H:%M}Z reset')
        return
    try:
        log(f'{window} limit; will push at {reset + MARGIN:%Y-%m-%d %H:%M:%S}Z')
        sleep_until(reset + MARGIN)
        push(window)
    finally:
        try:
            os.unlink(marker)
        except OSError:
            pass


def main():
    os.makedirs(DIR, mode=0o700, exist_ok=True)
    if sys.argv[1:] == ['--watch']:
        try:
            watch(json.load(sys.stdin))
        except Exception:
            log(traceback.format_exc())
        return
    raw = sys.stdin.buffer.read()
    child = subprocess.Popen([sys.executable, os.path.abspath(__file__), '--watch'], stdin=subprocess.PIPE,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    child.stdin.write(raw)
    child.stdin.close()


if __name__ == '__main__':
    main()
