# Credits Restored Claude Notifier

Claude Code stops when a session, weekly or org spend limit is hit. This plugin watches the clock for you: it sends a push, and an email through ntfy, a minute after the limit resets, with no terminal or Claude session left open.

How it works: a `StopFailure` hook (matcher `rate_limit`) hands the event to a detached watcher and returns at once. The watcher reads the `resets ... (zone)` time from the limit error at the end of the transcript, sleeps until reset + 60 s, then POSTs to `ntfy.sh/<topic>`. One watcher runs per reset time. A limit message with no reset time is only logged.

## Requirements

- Linux (the script uses `/proc`) and Python 3.9 or newer, standard library only.
- An [ntfy.sh](https://ntfy.sh) account and access token. ntfy.sh refuses emails from anonymous requests (HTTP 400, code 40053 when last tried, 2026-10-20), and the free tier allowed 5 emails a day.

## Install

At the prompt of a terminal session:

```
/plugin install credits-restored-claude-notifier --marketplace carlocontaldi/credits-restored-claude-notifier
```

From a shell:

```
claude plugin install credits-restored-claude-notifier --marketplace carlocontaldi/credits-restored-claude-notifier
```

If `~/.claude/settings.json` already registers `limit_restore_notify.py` under `hooks.StopFailure`, remove that entry so the hook is not registered twice.

## Configure

Three files in `~/.claude/limit-watch/` (set `LIMIT_WATCH_DIR` to use another folder), each holding one value, mode 600. They are never part of this repo.

| File | Value |
|---|---|
| `ntfy_topic` | A hard-to-guess topic name; on the public server anyone who knows it can read and publish. |
| `notify_email` | Where ntfy sends the email. |
| `ntfy_token` | Your ntfy access token. |

Subscribe to the topic in the ntfy app if you also want the phone push.

Generate a topic with `python3 -c 'import secrets; print(secrets.token_urlsafe(24))'`.

## Check it

`~/.claude/limit-watch/log.txt` records each limit seen (`weekly limit; will push at ...`), each push (`pushed (attempt 1)`), and failures. Missing config files show up there as `FileNotFoundError`. To test against a local server, set `LIMIT_WATCH_NTFY_URL`.

The zone in the limit message is the IANA zone of the machine running Claude Code, so each machine needs its own copy of the hook and config. A hook only fires for sessions on the machine where it is installed.

## License

[MIT](LICENSE)
