# Claude Portavoce

Claude Code stops when a session, weekly or org spend limit is hit. This plugin watches the clock for you: it sends a push, and an email through ntfy, a minute after the limit resets, with no terminal or Claude session left open.

**Token-free.** It makes no model calls: the waiting is done by a plain Python process, and the hook fires after Claude has already stopped and prints nothing, so it adds nothing to the conversation and costs none of your usage.

How it works: a `StopFailure` hook (matcher `rate_limit`) hands the event to a detached watcher and returns at once. The watcher reads the `resets ... (zone)` time from the limit error at the end of the transcript, sleeps until reset + 60 s, then POSTs to `ntfy.sh/<topic>`. One watcher runs per reset time. A limit message with no reset time is only logged. The watcher is an ordinary background process: it outlives the terminal and the Claude session, but not a reboot or shutdown, and a push that falls due while the machine is off is never sent.

What leaves your machine: one HTTPS request to ntfy.sh (or to the server in `LIMIT_WATCH_NTFY_URL`). The topic is in the URL; your email address and token are in headers, next to a fixed title and tag; the body is one line, "Your session limit has reset. You can continue." (`weekly` or `usage` in place of `session` for the other limits). Nothing else is sent.

## Requirements

- A POSIX system (the script uses `fcntl.flock`; only Linux is tested) and Python 3.9 or newer, standard library only.
- An [ntfy.sh](https://ntfy.sh) account and access token. ntfy.sh refuses emails from anonymous requests (HTTP 400, code 40053 when last tried, 2026-10-20), and the free tier allowed 5 emails a day.

## Install

At the prompt of a terminal session:

```
/plugin install portavoce --marketplace carlocontaldi/claude-portavoce
```

From a shell:

```
claude plugin install portavoce --marketplace carlocontaldi/claude-portavoce
```

If `~/.claude/settings.json` already registers `limit_restore_notify.py` under `hooks.StopFailure`, remove that entry so the hook is not registered twice.

## Configure

Claude Code asks for three values when you enable the plugin and hands them to the hook as environment variables. The topic and the token are not written to `settings.json` (the email address is). Claude Code keeps them in its secure storage: the macOS Keychain, or a Secret Service keyring on Linux when one is available. Without one, typical on a headless server, it falls back to `~/.claude/.credentials.json`, a plaintext file readable only by you (behaviour of Claude Code 2.1.319).

| Value | Meaning |
|---|---|
| ntfy topic | A hard-to-guess topic name; on the public server anyone who knows it can read and publish. |
| Notification email | Where ntfy sends the email. |
| ntfy access token | Your ntfy access token. |

Versions before 0.2.0 read these three values from files in `~/.claude/limit-watch/`; those files are now ignored, so enter the values when prompted.

Subscribe to the topic in the ntfy app if you also want the phone push.

Generate a topic with `python3 -c 'import secrets; print(secrets.token_urlsafe(24))'`.

## Check it

`~/.claude/limit-watch/log.txt` records each limit seen (`weekly limit; will push at ...`), each push (`pushed (attempt 1)`), and failures. A value you left unset or empty shows up there as `missing plugin options: ...`, and no push is sent. The folder is `~/.claude/limit-watch/`; set `LIMIT_WATCH_DIR` to use another. To test against a local server, set `LIMIT_WATCH_NTFY_URL`.

The zone in the limit message is the IANA zone of the machine running Claude Code, so each machine needs its own copy of the hook and config. A hook only fires for sessions on the machine where it is installed.

## Test

```
python3 -m unittest discover -s tests
```

## License

[MIT](LICENSE)
