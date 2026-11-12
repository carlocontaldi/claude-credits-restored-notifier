# Claude Portavoce

> **por·ta·vo·ce** · Italian /portaˈvotʃe/ (*por-ta-VOH-cheh*) · *noun*, masculine and feminine, plural unchanged
>
> 1. One who speaks on behalf of another person or a group; a spokesperson.
> 2. A megaphone; on ships, a speaking tube that carries the voice from one room to the next.
>
> *From* portare, *to carry, and* voce, *voice.*

Claude Code stops when a 5-hour, weekly, per-model (Opus, Sonnet, Fable) or spend limit is hit. Claude Portavoce speaks for it while it can't: it watches the clock for you and sends a push, and an email through ntfy, a minute after each cap resets, with no terminal or Claude session left open. The message says which caps came back, and whether another one still holds.

**Token-free.** It makes no model calls: the waiting is done by a plain Python process, and the hook fires after Claude has already stopped and prints nothing, so it adds nothing to the conversation and costs none of your usage. The `/portavoce` commands run a local script and make no model call either.

How it works: a `StopFailure` hook (matcher `rate_limit`) hands the event to a detached watcher and returns at once. The watcher reads the caps from the limit error at the end of the transcript (its `rate_limit_info`, else the `resets ... (zone)` time in the message), sleeps until reset + 60 s, then POSTs to `ntfy.sh/<topic>`. When the error also reports a spend cap (Claude Code's "individual" or "monthly spend limit"), a cap that resets later gets its own push when it does, and caps resetting within two minutes of each other share one. The error does not say whether a spend cap is daily, weekly or monthly, so Portavoce infers it from the reset time (more than a week away or at the turn of a month: monthly; one to seven days: weekly; the error's own daily flag: daily) and keeps "individual spend limit" when it cannot tell. One watcher runs per reset time. A limit message with no reset time is only logged. A window cap is waited for up to 8 days, a spend cap up to 35. The watcher is an ordinary background process: it outlives the terminal and the Claude session, but not a reboot or shutdown, and a push that falls due while the machine is off is never sent.

What leaves your machine: one HTTPS request to ntfy.sh (or to the server in `PORTAVOCE_NTFY_URL`). The topic is in the URL; your email address and token are in headers, next to a fixed title and tag; the body is one or two lines such as "Your 5-hour limit has reset. You can continue.", "Your 5-hour limit and weekly spend limit have both reset. You can continue." or "Your 5-hour limit has reset. You can continue. Usage credits stay capped by your weekly spend limit until Nov 17, 00:00 UTC." (a spend cap limits only the usage credits, so the plan is usable again) or "Your 5-hour limit has reset. Still in force: weekly limit (until Dec 3, 03:00 UTC)." Nothing else is sent.

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

Claude Code asks for the notification email and the access token when you enable the plugin and hands them to the hook as environment variables. The token is kept in Claude Code's secure storage: the macOS Keychain, or a Secret Service keyring on Linux when one is available. Without one, typical on a headless server, it falls back to `~/.claude/.credentials.json`, a plaintext file readable only by you (behaviour of Claude Code 2.1.319).

| Value | Required | Meaning |
|---|---|---|
| ntfy topic | No | Empty: a private topic Portavoce makes, `claude-portavoce-<random>` (see below). Set a name to use your own. |
| Notification email | Yes | Where ntfy sends the email. |
| ntfy access token | Yes | Your ntfy access token. |

The email and the token are both required: without either, the watcher logs `missing plugin options: ...` and sends nothing, so a push to the phone alone is not possible. [Claude Goon](https://github.com/carlocontaldi/claude-goon) makes both optional.

Change a value later from a shell; options you leave out keep their values:

```
claude plugin configure portavoce@claude-portavoce          # shows which are set
echo '{"ntfy_topic": "my-own-topic"}' | claude plugin configure portavoce@claude-portavoce --values-stdin
echo '{"ntfy_topic": ""}' | claude plugin configure portavoce@claude-portavoce --values-stdin    # back to the private topic
```

Versions before 0.2.0 read these values from files in `~/.claude/limit-watch/`, the folder this plugin used before it was renamed Portavoce; those files and that folder are no longer used, and you can delete them.

## Set up ntfy

1. **Email and token (required).** ntfy.sh refuses emails from anonymous requests, so [sign up](https://ntfy.sh/app) (free), open Account, create an access token and enter it as the access token above. The free tier allows 5 emails a day, shared with [Claude Goon](https://github.com/carlocontaldi/claude-goon) if you use its email option. The email arrives whether or not a phone is subscribed.
2. **Phone (optional).** In a Claude Code session run `/portavoce topic`. The first time, Portavoce makes a private topic, `claude-portavoce-` and 22 random letters and digits, keeps it in `~/.claude/portavoce/topic` (readable only by you) and shows it. Install the ntfy app (Android: Google Play or F-Droid; iOS: App Store), tap **+**, enter that topic and keep the default server, `ntfy.sh`. On Android, opening `ntfy://ntfy.sh/<topic>` subscribes in one step.
3. **Try it.** `/portavoce test` sends one push to the topic, without an email, and says whether it went through.

`/portavoce topic new` makes another topic if the first one leaks (subscribe to it and drop the old one). The commands run a local script and make no model call. The email's sender reads `ntfy.sh/<topic>`; that comes from ntfy and the topic name.

A topic on the public server is readable and writable by anyone who knows its name, which is why the name is random and not a fixed one such as `claude-portavoce`. ntfy.sh [offers reserved topics with access control on paid plans](https://docs.ntfy.sh/terms/).

## Check it

`~/.claude/portavoce/log.txt` records each cap seen (`5-hour limit and weekly spend limit; will push at ...`), each push (`pushed (attempt 1)`), and failures. A value you left unset or empty shows up there as `missing plugin options: ...`, and no push is sent. The folder is `~/.claude/portavoce/`; set `PORTAVOCE_DIR` to use another. To test against a local server, set `PORTAVOCE_NTFY_URL`.

The zone in the limit message is the IANA zone of the machine running Claude Code, so each machine needs its own copy of the hook and config. A hook only fires for sessions on the machine where it is installed.

## Test

`hooks/limitwatch.py` is a copy of [claude-meridiana](https://github.com/carlocontaldi/claude-meridiana), shared with Claude Goon; change it there and run its `sync.py`, not here. A test fails if the copy no longer matches its header.

```
python3 -m unittest discover -s tests
claude plugin test .
```

## License

[MIT](LICENSE)
