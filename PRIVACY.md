# Privacy Policy

Last updated: 2026-10-08

Unofficial Telegram Explorer ("the plugin") is open-source software that runs entirely on your
own computer. The author does not operate any server for it and does not receive, collect,
store, or sell any data from you or the people you talk to.

## What the plugin accesses

When you or your AI agent run a command, the plugin connects to Telegram's servers with your own
Telegram API credentials and session, and reads or changes only what that command asks for:
chats, messages, contacts, group members, media, and bot buttons on your account.

## What the plugin stores, and where

Everything stays on your computer, in files only your user account can read:

| Data | Location | Contents |
|---|---|---|
| API credentials | `~/.config/telegram-explorer/credentials.json` | Your `api_id` and `api_hash` (not created if you use the `TELEGRAM_API_ID`/`TELEGRAM_API_HASH` environment variables) |
| Session | `~/.config/telegram-explorer/telegram.session` | Telethon's login key, its cache of entities it has seen (ids, usernames, names, phone numbers), and hashes of files you uploaded |
| Chat cache | `~/.config/telegram-explorer/cache.json` | Ids, display names, and types of chats you listed or used, your aliases for them, and when they were first seen and last used |
| Downloads | `~/.local/share/telegram-explorer/downloads/` (or the folder you choose) | Media files you asked the agent to download |

The plugin does not store message text. It keeps these files until you delete them; nothing
expires automatically. To remove everything, delete the two `telegram-explorer` directories
above. To also revoke the login, end the session under Telegram → Settings → Devices.

## Who else receives data

- **Telegram** receives your requests, as with any Telegram app. Telegram's own
  [privacy policy](https://telegram.org/privacy) applies.
- **Your AI provider** receives whatever the agent reads through the plugin, because command
  output becomes part of the agent's context. That can include messages, names, and media
  descriptions belonging to other people. Your AI provider's terms and privacy policy apply to
  that data. Don't use the plugin on chats you aren't comfortable sharing with your AI provider.
- **PyPI** serves the pinned Python dependencies once when you first run the plugin with uv. If
  no Python ≥ 3.11 is installed, uv also downloads one from GitHub (astral-sh/python-build-standalone).

No other party receives data. The plugin has no telemetry or analytics.

## Children

The plugin is not intended for people under 18.

## Changes and contact

Changes to this policy are published in this repository with a new "Last updated" date. For
questions, open an issue at <https://github.com/zknpr/telegram-explorer/issues>. For security
reports, use GitHub's private vulnerability reporting on the repository.
