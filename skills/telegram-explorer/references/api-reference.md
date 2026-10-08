# Telegram Client API Reference

Complete reference for all subcommands of `telegram_client.py`.

## Global Behavior

- All output is JSON to stdout
- Errors are JSON to stderr with non-zero exit code
- Error format: `{"error": "message", "type": "ExceptionType"}`
- State directory: `$TELEGRAM_EXPLORER_HOME`, default `~/.config/telegram-explorer/` (`$XDG_CONFIG_HOME` respected)
  - `credentials.json`: `{"api_id": <int>, "api_hash": "<str>"}`; `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` env vars take precedence
  - `telegram.session`: Telethon session created by `login`
  - `cache.json`: chat/alias cache
- Files the tool creates are owner-only (umask 077); existing session/credentials files are tightened to `0600` on every run
- Commands never prompt: without an authorized session they fail with `NotLoggedInError`

---

## login

One-time interactive authentication. **Run by the user in a terminal, never by an agent.**

**Usage:**
```bash
telegram_client.py login
```

Prompts for `api_id`/`api_hash` (if neither the env vars nor `credentials.json` provide them), then Telethon prompts for the phone number, the login code, and the 2FA password if set. Refuses to run when stdin is not a terminal.

**Output:**
```json
{"ok": true, "account": {"id": 123456789, "type": "User", "first_name": "...", "username": "...", ...}, "session": "/home/user/.config/telegram-explorer/telegram.session"}
```

---

## dialogs

List recent dialogs (chats, channels, groups, DMs).

**Usage:**
```bash
telegram_client.py dialogs [--limit N]
```

**Arguments:**
| Arg | Type | Default | Description |
|-----|------|---------|-------------|
| `--limit` | int | 20 | Maximum dialogs to return |

**Output schema:**
```json
[
  {
    "id": 123456789,
    "name": "Chat Name",
    "type": "Channel",
    "unread_count": 5,
    "last_message_date": "2026-02-26T10:30:00+00:00",
    "pinned": false
  }
]
```

**Types:** `User`, `Chat`, `Channel`

---

## messages

Fetch messages from a specific chat.

**Usage:**
```bash
telegram_client.py messages <chat> [--limit N] [--search "query"] [--min-id X] [--max-id Y] [--topic ID]
```

**Arguments:**
| Arg | Type | Default | Description |
|-----|------|---------|-------------|
| `chat` | str | required | Chat identifier |
| `--limit` | int | 20 | Maximum messages to return |
| `--search` | str | None | Filter by text content |
| `--min-id` | int | None | Only messages with ID > this |
| `--max-id` | int | None | Only messages with ID < this |
| `--topic` | int | None | Forum topic ID to restrict to |

**Output schema:**
```json
[
  {
    "id": 42,
    "date": "2026-02-26T10:30:00+00:00",
    "sender_id": 123456789,
    "text": "Hello world",
    "reply_to_msg_id": null,
    "media_type": null,
    "forward": null,
    "reactions": [{"emoji": "👍", "count": 3}],
    "views": 150,
    "edit_date": null,
    "buttons": null
  }
]
```

`buttons` is `null` or a list of inline buttons, same shape as the `buttons` subcommand output.

**Media types:** `MessageMediaPhoto`, `MessageMediaDocument`, `MessageMediaWebPage`, `MessageMediaGeo`, `MessageMediaContact`, `MessageMediaPoll`

---

## send

Send a text message to a chat.

**Usage:**
```bash
telegram_client.py send <chat> --text "message" [--topic ID]
```

**Arguments:**
| Arg | Type | Default | Description |
|-----|------|---------|-------------|
| `chat` | str | required | Chat identifier |
| `--text` | str | required | Message content |
| `--topic` | int | None | Forum topic ID to send into |

**Output:** Single message object (same schema as `messages`).

---

## reply

Reply to a specific message.

**Usage:**
```bash
telegram_client.py reply <chat> --message-id ID --text "reply text"
```

**Arguments:**
| Arg | Type | Default | Description |
|-----|------|---------|-------------|
| `chat` | str | required | Chat identifier |
| `--message-id` | int | required | Target message ID |
| `--text` | str | required | Reply content |

**Output:** Single message object.

---

## forward

Forward a message from one chat to another.

**Usage:**
```bash
telegram_client.py forward <from_chat> <to_chat> --message-id ID
```

**Arguments:**
| Arg | Type | Default | Description |
|-----|------|---------|-------------|
| `from_chat` | str | required | Source chat |
| `to_chat` | str | required | Destination chat |
| `--message-id` | int | required | Message to forward |

**Output:** Single message object (the forwarded copy).

---

## edit

Edit an existing message.

**Usage:**
```bash
telegram_client.py edit <chat> --message-id ID --text "new text"
```

**Arguments:**
| Arg | Type | Default | Description |
|-----|------|---------|-------------|
| `chat` | str | required | Chat identifier |
| `--message-id` | int | required | Message to edit |
| `--text` | str | required | New message content |

**Output:** Single message object (edited).

**Note:** Only own messages can be edited (unless admin in a channel).

---

## delete

Delete one or more messages.

**Usage:**
```bash
telegram_client.py delete <chat> --message-ids "1,2,3"
```

**Arguments:**
| Arg | Type | Default | Description |
|-----|------|---------|-------------|
| `chat` | str | required | Chat identifier |
| `--message-ids` | str | required | Comma-separated message IDs |

**Output:**
```json
{
  "deleted_ids": [1, 2, 3],
  "affected_messages": 3
}
```

**Note:** Only own messages can be deleted (unless admin).

---

## react

Send a reaction to a message.

**Usage:**
```bash
telegram_client.py react <chat> --message-id ID --emoji "👍"
```

**Arguments:**
| Arg | Type | Default | Description |
|-----|------|---------|-------------|
| `chat` | str | required | Chat identifier |
| `--message-id` | int | required | Target message |
| `--emoji` | str | required | Reaction emoji |

**Output:**
```json
{
  "ok": true,
  "chat": "@channelname",
  "message_id": 42,
  "emoji": "👍"
}
```

**Common emojis:** 👍 👎 ❤️ 🔥 🎉 😢 😮 🤔 👏 🤯

---

## info

Get detailed info about any entity.

**Usage:**
```bash
telegram_client.py info <entity>
```

**Arguments:**
| Arg | Type | Default | Description |
|-----|------|---------|-------------|
| `entity` | str | required | Username, phone, or ID |

**Output for User:**
```json
{
  "id": 123456789,
  "type": "User",
  "first_name": "John",
  "last_name": "Doe",
  "username": "johndoe",
  "phone": "+1234567890",
  "bot": false,
  "verified": false
}
```

**Output for Channel/Chat:**
```json
{
  "id": -1001234567890,
  "type": "Channel",
  "title": "Channel Name",
  "username": "channelname",
  "megagroup": true,
  "participants_count": 5000
}
```

---

## participants

List members of a group or channel.

**Usage:**
```bash
telegram_client.py participants <chat> [--limit N] [--search "query"]
```

**Arguments:**
| Arg | Type | Default | Description |
|-----|------|---------|-------------|
| `chat` | str | required | Chat identifier |
| `--limit` | int | 50 | Maximum participants |
| `--search` | str | None | Filter by name/username |

**Output schema:**
```json
[
  {
    "id": 123456789,
    "first_name": "John",
    "last_name": "Doe",
    "username": "johndoe",
    "bot": false,
    "role": "admin"
  }
]
```

**Roles:** `creator`, `admin`, `member`, `banned`

---

## download

Download media from a message.

**Usage:**
```bash
telegram_client.py download <chat> --message-id ID [--out DIR]
```

**Arguments:**
| Arg | Type | Default | Description |
|-----|------|---------|-------------|
| `chat` | str | required | Chat identifier |
| `--message-id` | int | required | Message with media |
| `--out` | str | `$XDG_DATA_HOME/telegram-explorer/downloads` (default `~/.local/share/...`) | Output directory |

**Output:**
```json
{
  "ok": true,
  "path": "/home/user/.local/share/telegram-explorer/downloads/photo_2026-02-26.jpg"
}
```

**Errors:** Exits 1 if message not found or has no media.

The default directory is deliberately not the current directory: file names are chosen by the sender, and Telethon only strips path components and refuses to overwrite, so a file named e.g. `conftest.py` would otherwise land in whatever project the agent is working in.

---

## upload

Upload and send a file.

**Usage:**
```bash
telegram_client.py upload <chat> --file ./path [--caption "text"] [--voice-note]
```

**Arguments:**
| Arg | Type | Default | Description |
|-----|------|---------|-------------|
| `chat` | str | required | Chat identifier |
| `--file` | str | required | Local file path |
| `--caption` | str | None | Caption text |
| `--voice-note` | flag | off | Send audio as a playable voice message |

**Output:** Single message object (with `media_type` set).

**Supported:** Photos (jpg/png), videos (mp4/mov/mkv/webm/avi/m4v; `ffprobe` is used if available so they play inline), documents (any file type). Telethon auto-detects the type.

**Refused:** any file inside the state directory, or a symlink/hard link to the session, credentials, or cache file (`PermissionError`, raised before connecting).

---

## pinned

Get pinned messages from a chat.

**Usage:**
```bash
telegram_client.py pinned <chat> [--limit N]
```

**Arguments:**
| Arg | Type | Default | Description |
|-----|------|---------|-------------|
| `chat` | str | required | Chat identifier |
| `--limit` | int | 10 | Maximum pinned messages |

**Output:** Array of message objects.

---

## search-global

Search messages across all chats.

**Usage:**
```bash
telegram_client.py search-global --query "text" [--limit N]
```

**Arguments:**
| Arg | Type | Default | Description |
|-----|------|---------|-------------|
| `--query` | str | required | Search text |
| `--limit` | int | 20 | Maximum results |

**Output:** Array of message objects with additional `chat_id` field.

---

## topics

List forum topics in a forum-enabled group, discovered by scanning recent messages.

**Usage:**
```bash
telegram_client.py topics <chat> [--limit N]
```

| Arg | Type | Default | Description |
|-----|------|---------|-------------|
| `chat` | str | required | Chat identifier |
| `--limit` | int | 1000 | Messages to scan (higher finds more topics) |

**Output:** `[{"topic_id": 1, "title": "General"}, ...]` sorted by `topic_id`. `title` is `null` when the creation message wasn't found.

---

## buttons

Show the inline keyboard buttons on a message.

**Usage:**
```bash
telegram_client.py buttons <chat> --message-id ID
```

**Output:**
```json
{
  "message_id": 42,
  "chat": "@somebot",
  "buttons": [
    {"row": 0, "col": 0, "text": "Yes", "type": "callback", "data": "eWVz"},
    {"row": 0, "col": 1, "text": "Docs", "type": "url", "url": "https://example.com"}
  ]
}
```

Button `type` is one of `callback` (base64 `data`), `url`, `url_auth`, `switch_inline` (`query`), `text`.

---

## click

Press an inline button. Outbound: the bot sees it as the user's action.

**Usage:**
```bash
telegram_client.py click <chat> --message-id ID [--row R] [--col C]
```

| Arg | Type | Default | Description |
|-----|------|---------|-------------|
| `--row` | int | 0 | Button row (0-based, from `buttons`) |
| `--col` | int | 0 | Button column (0-based) |

**Output:**
```json
{
  "ok": true, "chat": "@somebot", "message_id": 42, "button_text": "Yes", "row": 0, "col": 0,
  "callback_response": {"message": "Done", "alert": false, "url": null}
}
```

`callback_response` is `null`, a bot answer object as above, or a message object when the click returns a message.

---

## Common Errors

| Error | Cause | Fix |
|-------|-------|-----|
| `FloodWaitError` | Too many requests | Wait the specified seconds |
| `ChatAdminRequiredError` | Insufficient permissions | Need admin rights |
| `MessageNotModifiedError` | Edit with same text | Change the text |
| `UserNotParticipantError` | Not in the chat | Join first |
| `ChannelPrivateError` | Private channel | Need invite |
| `PeerIdInvalidError` | Bad entity identifier | Check the ID/username |
| `NotLoggedInError` | No credentials or no authorized session | User runs `login` in a terminal |
| `AmbiguousMatchError` | Name matches several cached chats | Pick from `candidates`, use the id |
| `PermissionError` | `upload` of a protected file | Don't upload session/credential files |

## Chat Identifier Formats

| Format | Example | Notes |
|--------|---------|-------|
| Username | `@channelname` or `channelname` | Most common |
| Phone | `+1234567890` | For contacts |
| User ID | `123456789` | Positive integer |
| Chat ID | `-123456789` | Negative for groups |
| Channel ID | `-1001234567890` | Prefix -100 for channels |
| Invite link | `https://t.me/joinchat/ABC` | For private chats |
| t.me link | `https://t.me/channelname` | Public links |

---

## Cache Management

### `cache-list`

List all cached chat entries.

**Arguments:** none.

**Output:** JSON array of entry objects, sorted with operationally-used entries first, descending `last_used_at`; then listing-only entries, descending `first_seen_at`.

Each entry:
```json
{
  "id": "-1001234567890",
  "name": "Dev Team",
  "type": "channel",
  "aliases": ["dev chat"],
  "last_used_at": "2026-04-21T14:30:00+00:00",
  "first_seen_at": "2026-04-15T10:00:00+00:00"
}
```

### `cache-remove <chat>`

Drop one entry from the cache. `<chat>` is resolved via cache-only (no API fallback): if nothing in the cache matches, you get an error, not a network call.

**Arguments:**
- `<chat>`: chat identifier (name, alias, or ID).

**Output:** `{"ok": true, "removed": {"id": "...", "name": "..."}}` on success; error JSON on not-found or direct-ID input.

### `cache-clear --confirm`

Wipe all cache entries. `--confirm` is required.

**Arguments:**
- `--confirm`: required safety flag.

**Output:** `{"ok": true, "removed_count": N}` on success; error JSON if `--confirm` missing.

## Alias Management

### `alias-add <chat> <alias1> [<alias2> ...]`

Attach one or more aliases to a chat. `<chat>` goes through the full resolver including API fallback — so you can alias a chat the cache has never seen; it gets resolved and cached on the way in.

Duplicate aliases (case-insensitive) are silently skipped. The first-added casing is preserved.

**Output:**
```json
{
  "ok": true,
  "id": "-1001234567890",
  "name": "Dev Team",
  "aliases": ["dev chat", "lev dev"]
}
```

### `alias-remove <chat> <alias>`

Remove one alias from a cached entry. `<chat>` is cache-only. `<alias>` match is case-insensitive.

**Output:**
```json
{
  "ok": true,
  "id": "-1001234567890",
  "name": "Dev Team",
  "aliases": ["lev dev"]
}
```

Error JSON if `<chat>` isn't cached, is a direct ID, or `<alias>` isn't attached.

### `alias-list [<chat>]`

Without `<chat>`: return a flat map of `{alias: chat_name}` across every cached entry.

With `<chat>` (cache-only resolution): return that entry's aliases:

```json
{
  "id": "-1001234567890",
  "name": "Dev Team",
  "aliases": ["dev chat", "lev dev"]
}
```

## Reserved Keywords

Any subcommand accepting `<chat>` accepts these reserved keywords:

| Keyword | Semantics |
|---|---|
| `last` | The entry with the greatest non-null `last_used_at`. Error if nothing has been operationally used. |

## Cache File

Location: `cache.json` in the state directory (overridable via `TELEGRAM_CACHE_PATH` env var).

Schema:
```json
{
  "version": 1,
  "entries": {
    "<chat_id>": {
      "id": "<chat_id>",
      "name": "...",
      "type": "user | group | channel | broadcast",
      "aliases": ["..."],
      "last_used_at": "<ISO-8601 UTC or null>",
      "first_seen_at": "<ISO-8601 UTC>"
    }
  }
}
```

Writes are atomic (write `.tmp`, then rename). A corrupt file is moved aside as `<path>.corrupt.<unix_ts>` and a fresh empty cache starts; a warning is surfaced in the next command's JSON output under `"warnings": [...]`.

Cache entry IDs are stored in Telegram's signed wire format — `-1001234567890` for channels/supergroups, negative for legacy groups, positive for users. This matches what Telegram's URLs, share sheets, and official clients all use, and matches the `id` field in `dialogs` output so entries can be cross-referenced between commands.

## Caching behavior

Every operational subcommand (`send`, `messages`, `reply`, `forward`, `edit`, `delete`, `react`, `info`, `topics`, `participants`, `download`, `upload`, `pinned`, `buttons`, `click`) populates the cache with the resolved chat entry, bumping `last_used_at`. This includes direct-ID inputs — running `send -1001234567890 --text "..."` on a chat you've never referenced before will add a cache entry for it. After one use, you can refer to the chat by name or alias on subsequent calls.

Listing subcommands (`dialogs`) populate the cache with every returned entry but do NOT bump `last_used_at` — those entries appear in `cache-list` below the operationally-used ones and don't qualify for the `last` keyword until they're actually used.

## Alias uniqueness

Each alias is unique across the entire cache. `alias-add` rejects an alias that is already attached to a different entry, returning an error with the conflicting entry's id and name. This ensures `alias-list` (without `<chat>`) is unambiguous. Aliases within a single entry are deduplicated case-insensitively (first-added casing preserved).

## Error envelope format

On success, each subcommand emits its documented JSON payload to stdout. When warnings exist (cache recovered from corruption), the payload is wrapped as `{"warnings": [...], "data": <original payload>}`.

On error, JSON goes to stderr with exit code 1. The envelope is:

```json
{"error": "<human-readable message>", "type": "<exception class name>"}
```

Two error classes carry extra structured fields:

**`AmbiguousMatchError`**: raised when a `<chat>` name can't be resolved to a single entry, which happens when:
- two or more cached entries have the same name (case-insensitive). Display names are chosen by their owners and can be copied, so this always raises.
- a partial name matches several entries and the command is outbound (`send`, `reply`, `forward` destination, `edit`, `delete`, `react`, `upload`, `click`, `alias-add`).
- a partial name matches several entries, the command is a read, and no `last_used_at` difference breaks the tie.

```json
{
  "error": "ambiguous match for 'eng': 2 candidates",
  "type": "AmbiguousMatchError",
  "query": "eng",
  "candidates": [
    {"id": "-1001111111111", "name": "Engineering"},
    {"id": "-1002222222222", "name": "Engine Room"}
  ]
}
```

Callers should present `candidates` to the user and prompt for a more specific choice.

**All error envelopes** include cache `warnings` (if any accumulated during load) so corruption-recovery notices aren't swallowed when a later command fails.

## Stale cache handling

Any operational subcommand that resolves a `<chat>` via a cache hit may find the cached ID stale (the chat was deleted, the user was blocked, etc.). On Telethon errors in the `PeerIdInvalidError | ChannelPrivateError | UsernameNotOccupiedError | UserIdInvalidError` class, the cache entry is dropped and resolution is retried once via the original query string. One retry max.

Retry works for direct-ID forms (`@username`, `-100...`, `+phone`, `t.me/...`) and for plain-name resolutions that were originally resolved via API fallback. Plain-name resolutions that only matched via cache partial-match (the cached chat has no alias and the exact name the user typed isn't in `resolve_entity`'s syntactic forms) have no retry target — the Telethon error propagates to the caller.

The `last` keyword has no retry target since it was a pointer to a specific entry, not a name query. When a `last` resolution fails, the cache entry is still dropped but the error is a clear `LookupError("last-used chat no longer exists; cache entry removed")`.
