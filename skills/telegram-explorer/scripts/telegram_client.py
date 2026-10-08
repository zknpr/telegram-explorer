#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["telethon>=1.42.0"]
# ///
"""
Telegram client CLI for AI coding agents.

Wraps Telethon into a CLI with JSON output for all operations:
dialogs, messages, send, reply, forward, react, edit, delete,
info, participants, download, upload, search, pinned, buttons, click.

State lives in one per-account directory (see HOME_DIR below):
  credentials.json   api_id / api_hash (or TELEGRAM_API_ID / TELEGRAM_API_HASH)
  telegram.session   Telethon session, created by the `login` subcommand
  cache.json         chat/alias cache

All output is JSON to stdout. Errors go to stderr with non-zero exit.
"""

import argparse
import asyncio
import base64
import contextlib
import getpass
import json
import os
import sys
from pathlib import Path

# --- Resolve paths for config, session, and cache ---
# TELEGRAM_EXPLORER_HOME selects the account directory; point it at a different
# directory per account. Credentials, session, and cache all live inside it, so
# an alias learned on one account can never resolve (and send) to a chat ID
# from another.
HOME_DIR = Path(os.environ.get(
    "TELEGRAM_EXPLORER_HOME",
    Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "telegram-explorer",
)).expanduser()
CREDS_PATH = HOME_DIR / "credentials.json"
# Telethon appends ".session" to the name it is given.
SESSION_PATH = HOME_DIR / "telegram"
SESSION_FILE = HOME_DIR / "telegram.session"
# Cache file location; overridable via TELEGRAM_CACHE_PATH for tests.
CACHE_PATH = Path(os.environ.get("TELEGRAM_CACHE_PATH", HOME_DIR / "cache.json"))
# Default media download directory. Deliberately outside the current working
# directory: media filenames are chosen by the sender, and dropping e.g. an
# attacker-named conftest.py into whatever repo the agent is working in is a
# code-execution foothold.
DOWNLOAD_DIR = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") \
    / "telegram-explorer" / "downloads"

# Import the cache module (same directory as this file).
from cache import Cache

# Module-level cache instance. Loaded once per script invocation.
# Each CLI call is a fresh process, so there is no cross-invocation memory
# concern; state lives entirely in the on-disk JSON file.
_CACHE = Cache(CACHE_PATH)


def _emit_json(data):
    """
    Emit JSON to stdout with a 'warnings' field if the cache recorded any
    (e.g. from corrupt-file recovery). This is the standard output envelope
    for commands that want to surface cache warnings alongside their
    results.

    When _CACHE.warnings is non-empty the payload is wrapped:
      {"warnings": [...], "data": <original payload>}
    When empty the original payload is printed directly so existing callers
    are not broken (no wrapping on the happy path).
    """
    if _CACHE.warnings:
        # Wrap the result in an envelope only when warnings exist; otherwise
        # preserve the original shape (plain array or object) so existing
        # callers are not broken.
        wrapped = {"warnings": list(_CACHE.warnings), "data": data}
        print(json.dumps(wrapped, indent=2, ensure_ascii=False))
    else:
        print(json.dumps(data, indent=2, ensure_ascii=False))


class NotLoggedInError(RuntimeError):
    """Raised when credentials or an authorized session are missing."""


def _creds_from_env():
    """
    Return (api_id, api_hash) from TELEGRAM_API_ID / TELEGRAM_API_HASH, or
    None if either is unset. Env vars take precedence over credentials.json
    so harnesses that inject secrets via environment need no file at all.
    """
    api_id = os.environ.get("TELEGRAM_API_ID")
    api_hash = os.environ.get("TELEGRAM_API_HASH")
    if not api_id or not api_hash:
        return None
    try:
        return int(api_id), api_hash
    except ValueError:
        raise ValueError("TELEGRAM_API_ID must be an integer") from None


def load_creds():
    """
    Load api_id and api_hash from the environment, falling back to
    HOME_DIR/credentials.json. Raises NotLoggedInError if neither exists,
    ValueError if the file is malformed.
    """
    env = _creds_from_env()
    if env:
        return env
    if not CREDS_PATH.exists():
        raise NotLoggedInError(
            f"No Telegram API credentials: set TELEGRAM_API_ID/TELEGRAM_API_HASH "
            f"or run the `login` subcommand in a terminal to create {CREDS_PATH}"
        )
    with open(CREDS_PATH, "r") as f:
        creds = json.load(f)
    if "api_id" not in creds or "api_hash" not in creds:
        raise ValueError(f"{CREDS_PATH} must contain 'api_id' and 'api_hash'")
    return creds["api_id"], creds["api_hash"]


def _write_creds(api_id, api_hash):
    """Write credentials.json, owner-only (0600) from the moment it exists."""
    HOME_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(CREDS_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"api_id": api_id, "api_hash": api_hash}, f, indent=2)
    # O_CREAT's mode only applies to new files; tighten a pre-existing one too.
    os.chmod(CREDS_PATH, 0o600)


def _restrict_permissions():
    """
    chmod the session and credentials files to 0600 if they exist.

    The session file holds the account's MTProto auth key: anyone who can
    read it is logged in as the user, with no login code or 2FA prompt.
    Telethon creates it with the process umask (0644 under the common 022),
    which on a multi-user machine hands the account to every local user.
    """
    for path in (SESSION_FILE, CREDS_PATH):
        if path.exists():
            os.chmod(path, 0o600)


def make_client():
    """
    Create and return a TelegramClient using stored creds and session.
    The session file in HOME_DIR is reused across calls so no
    re-authentication is needed.
    """
    from telethon import TelegramClient
    api_id, api_hash = load_creds()
    HOME_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    client = TelegramClient(str(SESSION_PATH), api_id, api_hash)
    _restrict_permissions()
    # Disable default markdown parsing to prevent escaping special chars
    # like ! → \! in sent/edited messages.
    client.parse_mode = None
    return client


@contextlib.asynccontextmanager
async def open_client():
    """
    Connect an already-authorized client, without Telethon's interactive
    start(). `async with TelegramClient` calls start(), which prompts on
    stdin for a phone number when the session is not authorized; under an
    agent harness that either fails with an opaque EOFError or hangs on a
    pty. Fail fast with an actionable error instead.
    """
    client = make_client()
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise NotLoggedInError(
                f"No authorized Telegram session at {SESSION_FILE}. The user must "
                f"run the `login` subcommand themselves in an interactive terminal."
            )
        yield client
    finally:
        await client.disconnect()


async def resolve_entity(client, identifier):
    """
    Resolve a chat identifier to a Telethon entity.
    Handles numeric IDs correctly by detecting the ID range:
      - IDs starting with -100 are channels/supergroups (strip -100 prefix)
      - Negative IDs are legacy group chats
      - Positive IDs are users
    Strings (usernames, phones, links) are passed through to get_entity directly.
    """
    from telethon.tl.types import PeerUser, PeerChat, PeerChannel

    # --- Try to parse as integer for numeric ID handling ---
    try:
        num_id = int(identifier)
    except (ValueError, TypeError):
        # Not numeric — treat as username, phone, or link
        return await client.get_entity(identifier)

    # --- Numeric ID: determine type from sign and prefix ---
    if num_id > 0:
        # Positive = user ID
        return await client.get_entity(PeerUser(num_id))
    elif str(num_id).startswith("-100"):
        # Channel/supergroup: strip the -100 prefix to get the real channel ID
        channel_id = int(str(num_id)[4:])
        return await client.get_entity(PeerChannel(channel_id))
    else:
        # Negative without -100 prefix = legacy group chat
        return await client.get_entity(PeerChat(abs(num_id)))


def _entity_to_cache_dict(entity):
    """
    Convert a Telethon entity (User/Chat/Channel) into the dict shape
    that cache.upsert_* methods expect.

    The `id` field stores Telegram's SIGNED WIRE-FORMAT id, not Telethon's
    raw positive `entity.id`. This matters because:

      - Telegram's own URLs, share sheets, and official clients all use
        the signed form (`-1001234567890` for channels). Storing the same
        format means `cache-list` output is copy-pasteable into other
        contexts and matches what `dialogs` already emits via `dialog.id`.
      - It lets us re-resolve cache hits with a single call to
        `resolve_entity(client, cached["id"])`, whose existing int-sign
        branching handles each type correctly. No type-dispatched
        `_entity_from_cached` helper is needed.

    Wire-form encoding per Telethon convention:
      - User: `entity.id` (positive)
      - Chat (legacy group): `-entity.id`
      - Channel / supergroup: `-(1_000_000_000_000 + entity.id)` (the
        `-100…` prefix form)
    """
    entity_type = type(entity).__name__
    if entity_type == "User":
        simple_type = "user"
        wire_id = entity.id
        first = getattr(entity, "first_name", None) or ""
        last = getattr(entity, "last_name", None) or ""
        display = f"{first} {last}".strip() or getattr(entity, "username", None) or str(entity.id)
    elif entity_type == "Chat":
        simple_type = "group"
        # Legacy chat ids go negative in wire form.
        wire_id = -entity.id
        display = getattr(entity, "title", None) or str(entity.id)
    elif entity_type == "Channel":
        # Channels and supergroups share wire-form: -1000000000000 - id.
        simple_type = "channel"
        wire_id = -(1_000_000_000_000 + entity.id)
        display = getattr(entity, "title", None) or str(entity.id)
    else:
        # Unknown entity class — fall back to the bare id and let downstream
        # callers decide.
        simple_type = entity_type.lower()
        wire_id = entity.id
        display = str(entity.id)

    return {
        "id": str(wire_id),
        "name": display,
        "type": simple_type,
    }



async def resolve_chat_with_cache(client, query, strict=False):
    """
    Resolve a <chat> argument via the cache first, with invalidate-on-failure
    and retry. On any non-synthetic resolution path (cache hit, cache miss,
    or direct-ID), the fresh Telethon entity is upserted operationally so
    the cache accretes from every call.

    Direct IDs (numeric, -100..., @username, +phone, t.me/...) skip the
    cache LOOKUP (cache.resolve returns a `_synthetic` dict) but still
    populate the cache afterwards — this fixes the inconsistency where
    `send "dev chat"` would cache but `send -1001234567890` would not.

    strict=True (outbound commands) disables the recency tiebreak between
    several partial name matches, so a look-alike display name whose chat
    was merely read more recently can't silently receive outbound content.

    Invalidation: if a cache-hit's re-resolution fails with "not found" /
    "invalid peer" class errors, drop the stale entry and retry via the
    original query string. Does NOT retry for `last` (no meaningful query
    to retry against) — clean error instead.

    Returns: the Telethon entity object.
    """
    from telethon.errors import (
        PeerIdInvalidError, ChannelPrivateError,
        UsernameNotOccupiedError, UserIdInvalidError,
    )
    STALE_ERRORS = (PeerIdInvalidError, ChannelPrivateError,
                    UsernameNotOccupiedError, UserIdInvalidError)

    cached = None
    try:
        cached = _CACHE.resolve(query, api_fallback_fn=None, strict=strict)
    except LookupError:
        pass  # cache miss — use the query string directly

    # Pick the identifier we'll hand to Telethon. For a cache hit, that's
    # the stored wire-form id; for miss or direct-ID, it's the raw query.
    if cached is not None and not cached.get("_synthetic"):
        # Cache hit (non-direct): use the stored wire-form id.
        ident = cached["id"]
        try:
            entity = await resolve_entity(client, ident)
        except STALE_ERRORS:
            # Stale cached entry: drop it and retry with the original query.
            # Works for direct-ID forms and @usernames; a plain name that only
            # matched via cache partial-match has no retry target and will
            # surface the Telethon error to the caller. `last` has no
            # meaningful retry target — raise a clean LookupError instead.
            _CACHE.drop(cached["id"])
            _CACHE.save_atomic()
            if query == "last":
                raise LookupError(
                    "last-used chat no longer exists; cache entry removed"
                )
            entity = await resolve_entity(client, query)
    else:
        # Direct-ID short-circuit OR cache miss: resolve via the raw query.
        # `resolve_entity` already handles all direct-ID forms and will
        # raise a Telethon error if the chat is unknown.
        entity = await resolve_entity(client, query)

    # Always populate the cache operationally post-resolution. This is the
    # unified tail for all three paths (hit, miss, direct-ID): keeps the
    # cache in sync with server-side renames, records last_used_at, and
    # makes `cmd_alias_add` work uniformly on direct-ID input without any
    # special case.
    _CACHE.upsert_from_operation(_entity_to_cache_dict(entity))
    _CACHE.save_atomic()
    return entity


def resolve_cached_only(query):
    """
    Cache-only resolver for destructive commands (cache remove, alias remove).
    Returns the cache entry dict on hit; raises LookupError on miss.
    Never hits the network. This is a thin wrapper around Cache.resolve_cached_only
    that gives callers a module-level function to call without accessing _CACHE
    directly.
    """
    return _CACHE.resolve_cached_only(query)


def serialize_buttons(reply_markup):
    """
    Extract inline keyboard buttons from a message's reply_markup.
    Parses each button's type, text label, and type-specific data:
      - callback: has 'data' field (base64-encoded callback bytes)
      - url: has 'url' field
      - switch_inline: has 'query' field (inline query string)
      - text: plain keyboard button with no extra data
    Returns a flat list of button dicts with row/col indices for click targeting.
    Returns None if no reply_markup or no rows present.
    """
    if not reply_markup or not hasattr(reply_markup, "rows"):
        return None

    buttons = []
    for row_idx, row in enumerate(reply_markup.rows):
        for col_idx, btn in enumerate(row.buttons):
            # --- Base button info: position and visible label ---
            btn_data = {
                "row": row_idx,
                "col": col_idx,
                "text": btn.text,
            }

            # --- Determine button type from Telethon class name ---
            btn_type = type(btn).__name__

            if btn_type == "KeyboardButtonCallback":
                # Callback button: data is bytes, encode as base64 for JSON safety
                btn_data["type"] = "callback"
                btn_data["data"] = base64.b64encode(btn.data).decode("ascii") if btn.data else None
            elif btn_type == "KeyboardButtonUrl":
                # URL button: opens a link when clicked
                btn_data["type"] = "url"
                btn_data["url"] = btn.url
            elif btn_type == "KeyboardButtonSwitchInline":
                # Switch-inline button: triggers inline query in another chat
                btn_data["type"] = "switch_inline"
                btn_data["query"] = btn.query
            elif btn_type == "KeyboardButtonUrlAuth":
                # URL auth button: opens URL with authorization
                btn_data["type"] = "url_auth"
                btn_data["url"] = btn.url
            else:
                # Fallback for any other button type (plain text keyboard, etc.)
                btn_data["type"] = "text"

            buttons.append(btn_data)

    return buttons if buttons else None


def serialize_message(msg):
    """
    Convert a Telethon Message object into a JSON-serializable dict.
    Extracts: id, date, sender_id, text, reply_to, media type, forward info,
    reactions, views, edit_date, and inline keyboard buttons.
    """
    data = {
        "id": msg.id,
        "date": msg.date.isoformat() if msg.date else None,
        "sender_id": msg.sender_id,
        "text": msg.raw_text or "",
        "reply_to_msg_id": msg.reply_to.reply_to_msg_id if msg.reply_to else None,
    }

    # --- Media detection: report the type string if media is attached ---
    if msg.media:
        media_type = type(msg.media).__name__
        data["media_type"] = media_type
    else:
        data["media_type"] = None

    # --- Forward info: who originally sent the forwarded message ---
    if msg.forward:
        data["forward"] = {
            "from_id": getattr(msg.forward, "from_id", None),
            "from_name": getattr(msg.forward, "from_name", None),
            "date": msg.forward.date.isoformat() if msg.forward.date else None,
        }
        # from_id can be a PeerUser/PeerChannel object, extract the raw int
        fid = data["forward"]["from_id"]
        if fid and hasattr(fid, "user_id"):
            data["forward"]["from_id"] = fid.user_id
        elif fid and hasattr(fid, "channel_id"):
            data["forward"]["from_id"] = fid.channel_id
    else:
        data["forward"] = None

    # --- Reactions: list of (emoji, count) pairs if present ---
    if msg.reactions and msg.reactions.results:
        data["reactions"] = [
            {"emoji": str(r.reaction.emoticon) if hasattr(r.reaction, "emoticon") else str(r.reaction), "count": r.count}
            for r in msg.reactions.results
        ]
    else:
        data["reactions"] = []

    data["views"] = msg.views
    data["edit_date"] = msg.edit_date.isoformat() if msg.edit_date else None

    # --- Inline keyboard buttons: extract from reply_markup if present ---
    data["buttons"] = serialize_buttons(msg.reply_markup)

    return data


def serialize_dialog(dialog):
    """
    Convert a Telethon Dialog object into a JSON-serializable dict.
    Includes: entity id, name, type (User/Chat/Channel), unread count,
    last message date, and pinned status.
    """
    entity = dialog.entity
    entity_type = type(entity).__name__  # User, Chat, Channel, etc.

    return {
        "id": dialog.id,
        "name": dialog.name,
        "type": entity_type,
        "unread_count": dialog.unread_count,
        "last_message_date": dialog.date.isoformat() if dialog.date else None,
        "pinned": dialog.pinned,
    }


def serialize_entity(entity):
    """
    Convert a Telethon entity (User, Chat, Channel) into a JSON-serializable dict.
    Handles User fields (first_name, last_name, username, phone, bot, verified)
    and Chat/Channel fields (title, username, megagroup, participants_count).
    """
    data = {
        "id": entity.id,
        "type": type(entity).__name__,
    }
    # --- User-specific fields ---
    if hasattr(entity, "first_name"):
        data["first_name"] = entity.first_name
        data["last_name"] = entity.last_name
        data["username"] = entity.username
        data["phone"] = entity.phone
        data["bot"] = getattr(entity, "bot", False)
        data["verified"] = getattr(entity, "verified", False)
    # --- Chat/Channel-specific fields ---
    if hasattr(entity, "title"):
        data["title"] = entity.title
        data["username"] = getattr(entity, "username", None)
        data["megagroup"] = getattr(entity, "megagroup", False)
        data["participants_count"] = getattr(entity, "participants_count", None)
    return data


def serialize_participant(p):
    """
    Convert a Telethon participant (ChatParticipant or ChannelParticipant)
    into a JSON-serializable dict. Extracts user id, name, username, and role
    (admin/creator/banned/member).
    """
    data = {
        "id": p.id,
        "first_name": getattr(p, "first_name", None),
        "last_name": getattr(p, "last_name", None),
        "username": getattr(p, "username", None),
        "bot": getattr(p, "bot", False),
    }
    # --- Determine participant role from the Telethon type name ---
    ptype = type(p.participant).__name__ if hasattr(p, "participant") else ""
    if "Admin" in ptype:
        data["role"] = "admin"
    elif "Creator" in ptype:
        data["role"] = "creator"
    elif "Banned" in ptype:
        data["role"] = "banned"
    else:
        data["role"] = "member"
    return data


# ============================================================
# Subcommand handlers — each is an async function that receives
# the parsed args, creates a client, does the work, and prints
# JSON output.
# ============================================================


async def cmd_login(args):
    """
    One-time interactive setup, run by the human in a real terminal:
    prompts for api_id/api_hash (if not configured), then Telethon prompts
    for the phone number, the login code Telegram sends, and the 2FA
    password if one is set. Writes credentials.json and the session file
    with 0600 permissions.
    Output: JSON object with the logged-in account.
    """
    if not sys.stdin.isatty():
        raise NotLoggedInError(
            "login is interactive and must be run by the user in a terminal "
            "(it asks for the phone number, login code, and 2FA password)"
        )
    if _creds_from_env() is None and not CREDS_PATH.exists():
        print("Create an app at https://my.telegram.org/apps to get these.", file=sys.stderr)
        api_id = input("api_id: ").strip()
        api_hash = getpass.getpass("api_hash: ").strip()
        if not api_id.isdigit() or not api_hash:
            raise ValueError("api_id must be an integer and api_hash must be non-empty")
        _write_creds(int(api_id), api_hash)
    client = make_client()
    try:
        # start() is Telethon's interactive login (phone, code, 2FA password).
        await client.start()
        _restrict_permissions()
        me = await client.get_me()
        _emit_json({"ok": True, "account": serialize_entity(me), "session": str(SESSION_FILE)})
    finally:
        await client.disconnect()


async def cmd_dialogs(args):
    """
    List recent dialogs (chats, channels, groups, DMs).
    --limit controls how many to fetch (default 20).
    Output: JSON array of dialog objects.
    Side effect: upserts each returned dialog into the persistent cache
    (listing-style — does not bump last_used_at).
    """
    async with open_client() as client:
        dialogs = await client.get_dialogs(limit=args.limit)
        result = [serialize_dialog(d) for d in dialogs]
        # --- Populate cache from the listing. Each dialog's underlying entity
        # is upserted as a listing entry (first_seen_at set, last_used_at None). ---
        for d in dialogs:
            _CACHE.upsert_from_listing(_entity_to_cache_dict(d.entity))
        _CACHE.save_atomic()
        _emit_json(result)


async def cmd_topics(args):
    """
    List forum topics in a group/channel that has topics enabled.
    Scans recent messages for MessageActionTopicCreate service messages
    to discover topic IDs and titles (Telethon 1.x lacks GetForumTopicsRequest).
    --limit controls how many messages to scan (default 1000, higher = more topics found).
    Output: JSON array of {topic_id, title} objects.
    """
    from telethon.tl.types import MessageActionTopicCreate

    async with open_client() as client:
        entity = await resolve_chat_with_cache(client, args.chat)
        topics = []
        seen = set()
        # --- Scan messages for topic-creation service messages ---
        async for msg in client.iter_messages(entity, limit=args.limit):
            if msg.action and isinstance(msg.action, MessageActionTopicCreate):
                if msg.id not in seen:
                    seen.add(msg.id)
                    topics.append({"topic_id": msg.id, "title": msg.action.title})
            # --- Also collect topic IDs from reply_to fields of regular messages ---
            elif msg.reply_to and hasattr(msg.reply_to, "forum_topic") and msg.reply_to.forum_topic:
                tid = msg.reply_to.reply_to_top_id or msg.reply_to.reply_to_msg_id
                if tid and tid not in seen:
                    seen.add(tid)
                    # Try to fetch the topic-creation message to get the title
                    try:
                        topic_msg = await client.get_messages(entity, ids=tid)
                        if topic_msg and topic_msg.action and hasattr(topic_msg.action, "title"):
                            topics.append({"topic_id": tid, "title": topic_msg.action.title})
                        else:
                            topics.append({"topic_id": tid, "title": None})
                    except Exception:
                        topics.append({"topic_id": tid, "title": None})
        # Sort by topic_id ascending for stable output
        topics.sort(key=lambda t: t["topic_id"])
        _emit_json(topics)


async def cmd_messages(args):
    """
    Fetch recent messages from a chat/channel/group.
    <chat> can be a username, phone, numeric ID, or invite link.
    --limit controls count (default 20).
    --search filters messages by text content.
    --min-id / --max-id constrain the message ID range.
    --topic restricts to a specific forum topic by its ID.
    Output: JSON array of message objects (newest first).
    """
    async with open_client() as client:
        entity = await resolve_chat_with_cache(client, args.chat)
        kwargs = {"entity": entity, "limit": args.limit}
        if args.search:
            kwargs["search"] = args.search
        if args.min_id:
            kwargs["min_id"] = args.min_id
        if args.max_id:
            kwargs["max_id"] = args.max_id
        if args.topic:
            # reply_to filters messages within a specific forum topic thread
            kwargs["reply_to"] = args.topic
        messages = await client.get_messages(**kwargs)
        result = [serialize_message(m) for m in messages]
        _emit_json(result)


async def cmd_send(args):
    """
    Send a new text message to a chat.
    --topic sends to a specific forum topic by its ID.
    Output: JSON object of the sent message.
    """
    async with open_client() as client:
        entity = await resolve_chat_with_cache(client, args.chat, strict=True)
        kwargs = {"entity": entity, "message": args.text, "parse_mode": None}
        if args.topic:
            # reply_to=topic_id sends the message into that forum topic thread
            kwargs["reply_to"] = args.topic
        msg = await client.send_message(**kwargs)
        _emit_json(serialize_message(msg))


async def cmd_reply(args):
    """
    Reply to a specific message by its ID.
    --message-id is the target message, --text is the reply body.
    Output: JSON object of the sent reply message.
    """
    async with open_client() as client:
        entity = await resolve_chat_with_cache(client, args.chat, strict=True)
        msg = await client.send_message(entity, args.text, reply_to=args.message_id, parse_mode=None)
        _emit_json(serialize_message(msg))


async def cmd_forward(args):
    """
    Forward a message from one chat to another.
    --message-id is the message to forward in <from_chat>.
    <to_chat> is the destination.
    Output: JSON object of the forwarded message.
    """
    async with open_client() as client:
        from_entity = await resolve_chat_with_cache(client, args.from_chat)
        to_entity = await resolve_chat_with_cache(client, args.to_chat, strict=True)
        result = await client.forward_messages(to_entity, args.message_id, from_entity)
        # forward_messages returns a list when given a single ID; unwrap it
        if isinstance(result, list):
            result = result[0]
        _emit_json(serialize_message(result))


async def cmd_edit(args):
    """
    Edit an existing message sent by the authenticated user.
    --message-id targets the message, --text is the new content.
    Output: JSON object of the edited message.
    """
    async with open_client() as client:
        entity = await resolve_chat_with_cache(client, args.chat, strict=True)
        msg = await client.edit_message(entity, args.message_id, args.text, parse_mode=None)
        _emit_json(serialize_message(msg))


async def cmd_delete(args):
    """
    Delete one or more messages by ID from a chat.
    --message-ids is a comma-separated list of IDs.
    Output: JSON object with list of deleted IDs and success status.
    """
    async with open_client() as client:
        entity = await resolve_chat_with_cache(client, args.chat, strict=True)
        ids = [int(x.strip()) for x in args.message_ids.split(",")]
        result = await client.delete_messages(entity, ids)
        _emit_json({"deleted_ids": ids, "affected_messages": getattr(result, "pts_count", len(ids))})


async def cmd_react(args):
    """
    Send a reaction emoji to a specific message.
    --message-id targets the message, --emoji is the reaction (e.g. "👍").
    Output: confirmation JSON with chat, message_id, and emoji.
    """
    from telethon.tl.functions.messages import SendReactionRequest
    from telethon.tl.types import ReactionEmoji

    async with open_client() as client:
        entity = await resolve_chat_with_cache(client, args.chat, strict=True)
        await client(SendReactionRequest(
            peer=entity,
            msg_id=args.message_id,
            reaction=[ReactionEmoji(emoticon=args.emoji)]
        ))
        _emit_json({"ok": True, "chat": str(args.chat), "message_id": args.message_id, "emoji": args.emoji})


async def cmd_info(args):
    """
    Get detailed info about a Telegram entity (user, chat, or channel).
    <entity> can be a username, phone number, or numeric ID.
    Output: JSON object with entity details.
    Side effect: upserts the entity into the cache as an operational use.
    """
    async with open_client() as client:
        # resolve_chat_with_cache already upserts on miss. If it's a cache
        # hit, we still want last_used_at bumped (which the wrapper does).
        entity = await resolve_chat_with_cache(client, args.entity)
        _emit_json(serialize_entity(entity))


async def cmd_participants(args):
    """
    List participants of a group or channel.
    --limit caps the number returned (default 50).
    --search filters by name/username.
    Output: JSON array of participant objects.
    """
    async with open_client() as client:
        entity = await resolve_chat_with_cache(client, args.chat)
        kwargs = {"entity": entity, "limit": args.limit}
        if args.search:
            kwargs["search"] = args.search
        participants = await client.get_participants(**kwargs)
        result = [serialize_participant(p) for p in participants]
        # Upsert each participant as a listing entry (they are users whose IDs
        # we may reference later). The parent chat was already upserted
        # operationally by resolve_chat_with_cache.
        for p in participants:
            _CACHE.upsert_from_listing({
                "id": str(p.id),
                "name": f"{getattr(p, 'first_name', '') or ''} {getattr(p, 'last_name', '') or ''}".strip()
                        or getattr(p, "username", None) or str(p.id),
                "type": "user",
            })
        _CACHE.save_atomic()
        _emit_json(result)


async def cmd_download(args):
    """
    Download media from a specific message.
    --message-id targets the message with media attached.
    --out sets the output directory (default: DOWNLOAD_DIR).
    Output: JSON object with the downloaded file path.
    """
    async with open_client() as client:
        entity = await resolve_chat_with_cache(client, args.chat)
        # Fetch the specific message by ID
        msgs = await client.get_messages(entity, ids=args.message_id)
        msg = msgs if not isinstance(msgs, list) else msgs[0] if msgs else None
        if not msg:
            print(json.dumps({"error": f"Message {args.message_id} not found"}), file=sys.stderr)
            sys.exit(1)
        if not msg.media:
            print(json.dumps({"error": f"Message {args.message_id} has no media"}), file=sys.stderr)
            sys.exit(1)
        # Ensure output directory exists
        out_dir = Path(args.out) if args.out else DOWNLOAD_DIR
        out_dir.mkdir(parents=True, exist_ok=True)
        # Download the media file to the output directory
        path = await client.download_media(msg, file=str(out_dir))
        _emit_json({"ok": True, "path": str(path)})


def _refuse_secret_upload(file_path):
    """
    Refuse to upload this tool's own secrets: anything inside HOME_DIR
    (session, credentials, cache) or a hard link to one of those files.

    Message text read by the agent is attacker-controlled, and a single
    injected `upload @attacker --file <session>` would hand over the account.
    resolve() follows symlinks; the inode comparison catches hard links.
    A prior copy of the file defeats this check — it narrows the one-step
    path, the harness's command approval is the real control.
    """
    target = file_path.resolve()
    home = HOME_DIR.resolve()
    if target == home or home in target.parents:
        raise PermissionError(
            f"refusing to upload {target}: it is inside the telegram-explorer "
            f"state directory {home} (session/credentials/cache)"
        )
    st = target.stat()
    for protected in (SESSION_FILE, CREDS_PATH, CACHE_PATH):
        try:
            ps = protected.stat()
        except FileNotFoundError:
            continue
        if (ps.st_dev, ps.st_ino) == (st.st_dev, st.st_ino):
            raise PermissionError(f"refusing to upload {target}: it is a link to {protected}")


async def cmd_upload(args):
    """
    Upload and send a file (photo, video, document) to a chat.
    --file is the local path to the file.
    --caption is optional text sent alongside the file.
    Output: JSON object of the sent message with media.
    """
    # Validate the file before connecting so a refused upload never touches
    # the network.
    file_path = Path(args.file)
    if not file_path.exists():
        print(json.dumps({"error": f"File not found: {args.file}"}), file=sys.stderr)
        sys.exit(1)
    _refuse_secret_upload(file_path)
    async with open_client() as client:
        entity = await resolve_chat_with_cache(client, args.chat, strict=True)
        # Detect video files. For Telegram to render the upload as an inline playable
        # video (rather than a download-only document) we need:
        #   1. force_document=False
        #   2. supports_streaming=True
        #   3. an explicit DocumentAttributeVideo with duration + dimensions
        # ffprobe gives us the duration / dimensions reliably.
        video_exts = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
        is_video = file_path.suffix.lower() in video_exts
        # voice_note=True sends the file as a playable voice message in Telegram
        send_kwargs = {"caption": args.caption or "", "voice_note": getattr(args, "voice_note", False)}
        if is_video:
            try:
                import subprocess
                from telethon.tl.types import DocumentAttributeVideo

                probe = subprocess.run(
                    [
                        "ffprobe", "-v", "error",
                        "-select_streams", "v:0",
                        "-show_entries", "stream=width,height,duration",
                        "-of", "default=noprint_wrappers=1:nokey=1",
                        str(file_path),
                    ],
                    capture_output=True, text=True, check=True,
                )
                w_s, h_s, dur_s = probe.stdout.strip().split("\n")
                attrs = [DocumentAttributeVideo(
                    duration=int(float(dur_s)),
                    w=int(w_s), h=int(h_s),
                    supports_streaming=True,
                    round_message=False,
                )]
                send_kwargs["attributes"] = attrs
                send_kwargs["supports_streaming"] = True
                send_kwargs["force_document"] = False
            except Exception as e:
                # ffprobe missing or failed — fall back to plain send (will likely
                # render as document but the upload still goes through).
                print(json.dumps({"warning": f"video probe failed: {e}; sending as document"}), file=sys.stderr)
        msg = await client.send_file(entity, str(file_path), **send_kwargs)
        _emit_json(serialize_message(msg))


async def cmd_pinned(args):
    """
    Fetch pinned messages from a chat/channel.
    --limit caps the number returned (default 10).
    Output: JSON array of pinned message objects.
    """
    from telethon.tl.types import InputMessagesFilterPinned

    async with open_client() as client:
        entity = await resolve_chat_with_cache(client, args.chat)
        # search with pinned filter returns only pinned messages
        messages = await client.get_messages(entity, limit=args.limit, filter=InputMessagesFilterPinned)
        result = [serialize_message(m) for m in messages]
        _emit_json(result)


async def cmd_buttons(args):
    """
    Inspect inline keyboard buttons on a specific message.
    Fetches a single message by ID and returns its button layout as JSON.
    Each button includes row/col indices (for use with the 'click' command),
    the visible text label, button type, and type-specific data (callback data, URL, etc.).
    Output: JSON object with message_id and buttons array, or error if no buttons found.
    """
    async with open_client() as client:
        entity = await resolve_chat_with_cache(client, args.chat)
        # --- Fetch the single message by its ID ---
        msg = await client.get_messages(entity, ids=args.message_id)
        if not msg:
            print(json.dumps({"error": f"Message {args.message_id} not found"}), file=sys.stderr)
            sys.exit(1)

        buttons = serialize_buttons(msg.reply_markup)
        if not buttons:
            print(json.dumps({"error": f"Message {args.message_id} has no inline buttons"}), file=sys.stderr)
            sys.exit(1)

        _emit_json({
            "message_id": msg.id,
            "chat": str(args.chat),
            "buttons": buttons
        })


async def cmd_click(args):
    """
    Click an inline keyboard button on a bot message.
    Targets a button by its row and column index (0-based, as shown by 'buttons' command).
    Uses Telethon's Message.click(row, col) which handles the underlying
    GetBotCallbackAnswerRequest automatically.
    Output: JSON object with the bot's callback response (message and/or alert text).
    """
    async with open_client() as client:
        entity = await resolve_chat_with_cache(client, args.chat, strict=True)
        # --- Fetch the target message ---
        msg = await client.get_messages(entity, ids=args.message_id)
        if not msg:
            print(json.dumps({"error": f"Message {args.message_id} not found"}), file=sys.stderr)
            sys.exit(1)

        if not msg.reply_markup or not hasattr(msg.reply_markup, "rows"):
            print(json.dumps({"error": f"Message {args.message_id} has no clickable buttons"}), file=sys.stderr)
            sys.exit(1)

        # --- Validate row/col bounds before clicking ---
        rows = msg.reply_markup.rows
        if args.row >= len(rows):
            print(json.dumps({"error": f"Row {args.row} out of range (message has {len(rows)} rows)"}), file=sys.stderr)
            sys.exit(1)
        if args.col >= len(rows[args.row].buttons):
            print(json.dumps({"error": f"Col {args.col} out of range (row {args.row} has {len(rows[args.row].buttons)} buttons)"}), file=sys.stderr)
            sys.exit(1)

        # --- Click the button; Telethon sends the callback query to the bot ---
        result = await msg.click(args.row, args.col)

        # --- Build response from the callback answer ---
        response = {
            "ok": True,
            "chat": str(args.chat),
            "message_id": args.message_id,
            "button_text": rows[args.row].buttons[args.col].text,
            "row": args.row,
            "col": args.col,
        }

        # result can be a MessagesBotCallbackAnswer or an updated Message
        if result is None:
            response["callback_response"] = None
        elif hasattr(result, "message") and hasattr(result, "alert"):
            # BotCallbackAnswer: contains optional message/alert from the bot
            response["callback_response"] = {
                "message": result.message,
                "alert": result.alert,
                "url": getattr(result, "url", None),
            }
        else:
            # Updated message returned (e.g., URL button opened, or message edited)
            response["callback_response"] = serialize_message(result) if hasattr(result, "id") else str(result)

        _emit_json(response)


async def cmd_search_global(args):
    """
    Search messages globally across all chats.
    --query is the search term, --limit caps results (default 20).
    Output: JSON array of message objects with an added 'chat_id' field.
    """
    async with open_client() as client:
        results = []
        async for msg in client.iter_messages(None, search=args.query, limit=args.limit):
            data = serialize_message(msg)
            data["chat_id"] = msg.chat_id
            results.append(data)
        _emit_json(results)


async def cmd_cache_list(args):
    """
    List all cached chat entries, sorted:
      - Operationally-used first (descending by last_used_at)
      - Then listing-only entries (descending by first_seen_at)
    Output: JSON array.
    No Telegram connection needed — this is pure cache inspection.
    """
    # No Telegram connection needed — this is pure cache inspection.
    _emit_json(_CACHE.list_all())


async def cmd_cache_remove(args):
    """
    Remove one entry from the cache. <chat> is resolved via cache-only
    (no API fallback), so passing a name/alias for something not in the
    cache is an error, not a network call.
    Output: JSON object confirming the removed entry's id and name.
    """
    try:
        entry = resolve_cached_only(args.chat)
    except LookupError:
        print(json.dumps({"error": f"{args.chat!r} not found in cache"}), file=sys.stderr)
        sys.exit(1)
    if entry.get("_synthetic"):
        # Direct IDs bypass the cache — nothing to remove.
        print(json.dumps({"error": f"{args.chat!r} is a direct ID, not a cached entry"}), file=sys.stderr)
        sys.exit(1)
    chat_id = entry["id"]
    chat_name = entry["name"]
    _CACHE.drop(chat_id)
    _CACHE.save_atomic()
    _emit_json({"ok": True, "removed": {"id": chat_id, "name": chat_name}})


async def cmd_cache_clear(args):
    """
    Remove all entries from the cache. Requires --confirm to avoid accidents.
    Output: JSON object with count of removed entries.
    """
    if not args.confirm:
        print(json.dumps({"error": "refusing to clear cache without --confirm"}), file=sys.stderr)
        sys.exit(1)
    count = len(_CACHE.entries)
    _CACHE.clear()
    _CACHE.save_atomic()
    _emit_json({"ok": True, "removed_count": count})


async def cmd_alias_add(args):
    """
    Attach one or more aliases to a chat. <chat> goes through the FULL
    resolver (including API fallback), so you can alias a chat the cache
    has never seen — it'll be resolved and cached first, then aliased.

    Output: JSON object with the chat id, name, and updated alias list.
    """
    async with open_client() as client:
        # resolve_chat_with_cache always upserts on success, so the entry is
        # guaranteed present in _CACHE.entries by the time we attach aliases.
        # We re-derive the cache key (wire-form id) via _entity_to_cache_dict
        # rather than using entity.id directly (which is the raw positive form).
        entity = await resolve_chat_with_cache(client, args.chat, strict=True)
        cache_key = _entity_to_cache_dict(entity)["id"]
        _CACHE.add_aliases(cache_key, args.aliases)
        _CACHE.save_atomic()
        _emit_json({
            "ok": True,
            "id": cache_key,
            "name": _CACHE.entries[cache_key]["name"],
            "aliases": _CACHE.entries[cache_key]["aliases"],
        })


async def cmd_alias_remove(args):
    """
    Remove one alias from a chat. <chat> is cache-only; <alias> must be
    currently attached.
    Output: JSON object with the updated alias list.
    """
    try:
        entry = resolve_cached_only(args.chat)
    except LookupError:
        print(json.dumps({"error": f"{args.chat!r} not found in cache"}), file=sys.stderr)
        sys.exit(1)
    if entry.get("_synthetic"):
        print(json.dumps({"error": f"{args.chat!r} is a direct ID, not a cached entry"}), file=sys.stderr)
        sys.exit(1)
    try:
        _CACHE.remove_alias(entry["id"], args.alias)
    except ValueError as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(1)
    _CACHE.save_atomic()
    _emit_json({
        "ok": True,
        "id": entry["id"],
        "name": entry["name"],
        "aliases": _CACHE.entries[entry["id"]]["aliases"],
    })


async def cmd_alias_list(args):
    """
    List aliases. If <chat> is given, show aliases attached to that one
    entry (cache-only lookup). If omitted, show a flat map of every alias
    across every entry: {"alias": "chat name", ...}.
    """
    if args.chat:
        try:
            entry = resolve_cached_only(args.chat)
        except LookupError:
            print(json.dumps({"error": f"{args.chat!r} not found in cache"}), file=sys.stderr)
            sys.exit(1)
        if entry.get("_synthetic"):
            print(json.dumps({"error": f"{args.chat!r} is a direct ID"}), file=sys.stderr)
            sys.exit(1)
        _emit_json({
            "id": entry["id"],
            "name": entry["name"],
            "aliases": _CACHE.entries[entry["id"]]["aliases"],
        })
    else:
        # Flat map across all entries: {alias: chat_name, ...}.
        result = {}
        for entry in _CACHE.entries.values():
            for alias in entry.get("aliases", []):
                result[alias] = entry.get("name", "")
        _emit_json(result)


# ============================================================
# CLI argument parser — maps subcommands to handler functions
# ============================================================

def build_parser():
    """
    Build the argparse parser with all subcommands and their arguments.
    Each subcommand maps to an async handler function via set_defaults(func=...).
    """
    parser = argparse.ArgumentParser(description="Telegram CLI client for AI coding agents")
    sub = parser.add_subparsers(dest="command", required=True)

    # --- login: one-time interactive authentication (run by the user) ---
    p = sub.add_parser("login", help="Interactive one-time login (run in a terminal, not by an agent)")
    p.set_defaults(func=cmd_login)

    # --- dialogs: list recent chats ---
    p = sub.add_parser("dialogs", help="List recent dialogs")
    p.add_argument("--limit", type=int, default=20, help="Max dialogs to fetch")
    p.set_defaults(func=cmd_dialogs)

    # --- topics: list forum topics in a chat ---
    p = sub.add_parser("topics", help="List forum topics in a chat")
    p.add_argument("chat", help="Chat identifier (must be a forum-enabled group/channel)")
    p.add_argument("--limit", type=int, default=1000, help="Max messages to scan for topic discovery (default 1000)")
    p.set_defaults(func=cmd_topics)

    # --- messages: fetch messages from a chat ---
    p = sub.add_parser("messages", help="Get messages from a chat")
    p.add_argument("chat", help="Chat identifier (username, phone, ID, or invite link)")
    p.add_argument("--limit", type=int, default=20, help="Max messages to fetch")
    p.add_argument("--search", help="Filter messages by text content")
    p.add_argument("--min-id", type=int, help="Only messages with ID greater than this")
    p.add_argument("--max-id", type=int, help="Only messages with ID less than this")
    p.add_argument("--topic", type=int, help="Forum topic ID to filter messages by")
    p.set_defaults(func=cmd_messages)

    # --- send: send a text message ---
    p = sub.add_parser("send", help="Send a message")
    p.add_argument("chat", help="Chat identifier")
    p.add_argument("--text", required=True, help="Message text to send")
    p.add_argument("--topic", type=int, help="Forum topic ID to send into")
    p.set_defaults(func=cmd_send)

    # --- reply: reply to a specific message ---
    p = sub.add_parser("reply", help="Reply to a message")
    p.add_argument("chat", help="Chat identifier")
    p.add_argument("--message-id", type=int, required=True, help="ID of message to reply to")
    p.add_argument("--text", required=True, help="Reply text")
    p.set_defaults(func=cmd_reply)

    # --- forward: forward a message between chats ---
    p = sub.add_parser("forward", help="Forward a message")
    p.add_argument("from_chat", help="Source chat identifier")
    p.add_argument("to_chat", help="Destination chat identifier")
    p.add_argument("--message-id", type=int, required=True, help="ID of message to forward")
    p.set_defaults(func=cmd_forward)

    # --- edit: edit an existing message ---
    p = sub.add_parser("edit", help="Edit a message")
    p.add_argument("chat", help="Chat identifier")
    p.add_argument("--message-id", type=int, required=True, help="ID of message to edit")
    p.add_argument("--text", required=True, help="New message text")
    p.set_defaults(func=cmd_edit)

    # --- delete: delete messages by ID ---
    p = sub.add_parser("delete", help="Delete messages")
    p.add_argument("chat", help="Chat identifier")
    p.add_argument("--message-ids", required=True, help="Comma-separated message IDs to delete")
    p.set_defaults(func=cmd_delete)

    # --- react: send a reaction to a message ---
    p = sub.add_parser("react", help="React to a message")
    p.add_argument("chat", help="Chat identifier")
    p.add_argument("--message-id", type=int, required=True, help="ID of message to react to")
    p.add_argument("--emoji", required=True, help="Reaction emoji (e.g. 👍)")
    p.set_defaults(func=cmd_react)

    # --- info: get entity details ---
    p = sub.add_parser("info", help="Get entity info")
    p.add_argument("entity", help="Entity identifier (username, phone, ID)")
    p.set_defaults(func=cmd_info)

    # --- participants: list group/channel members ---
    p = sub.add_parser("participants", help="List participants")
    p.add_argument("chat", help="Chat identifier")
    p.add_argument("--limit", type=int, default=50, help="Max participants to fetch")
    p.add_argument("--search", help="Filter participants by name/username")
    p.set_defaults(func=cmd_participants)

    # --- download: download media from a message ---
    p = sub.add_parser("download", help="Download media from a message")
    p.add_argument("chat", help="Chat identifier")
    p.add_argument("--message-id", type=int, required=True, help="ID of message with media")
    p.add_argument("--out", default=None, help=f"Output directory (default: {DOWNLOAD_DIR})")
    p.set_defaults(func=cmd_download)

    # --- upload: send a file to a chat ---
    p = sub.add_parser("upload", help="Upload a file to a chat")
    p.add_argument("chat", help="Chat identifier")
    p.add_argument("--file", required=True, help="Path to file to upload")
    p.add_argument("--caption", help="Caption for the uploaded file")
    p.add_argument("--voice-note", action="store_true", help="Send as a voice message")
    p.set_defaults(func=cmd_upload)

    # --- pinned: get pinned messages ---
    p = sub.add_parser("pinned", help="Get pinned messages")
    p.add_argument("chat", help="Chat identifier")
    p.add_argument("--limit", type=int, default=10, help="Max pinned messages to fetch")
    p.set_defaults(func=cmd_pinned)

    # --- search-global: search messages across all chats ---
    p = sub.add_parser("search-global", help="Search messages globally")
    p.add_argument("--query", required=True, help="Search query")
    p.add_argument("--limit", type=int, default=20, help="Max results")
    p.set_defaults(func=cmd_search_global)

    # --- buttons: inspect inline buttons on a message ---
    p = sub.add_parser("buttons", help="Show inline buttons on a message")
    p.add_argument("chat", help="Chat identifier")
    p.add_argument("--message-id", type=int, required=True, help="ID of message to inspect")
    p.set_defaults(func=cmd_buttons)

    # --- click: click an inline button on a bot message ---
    p = sub.add_parser("click", help="Click an inline button on a message")
    p.add_argument("chat", help="Chat identifier")
    p.add_argument("--message-id", type=int, required=True, help="ID of message with buttons")
    p.add_argument("--row", type=int, default=0, help="Button row index, 0-based (default: 0)")
    p.add_argument("--col", type=int, default=0, help="Button column index, 0-based (default: 0)")
    p.set_defaults(func=cmd_click)

    # --- cache-list: show all cached entries ---
    p = sub.add_parser("cache-list", help="List all cached chat entries")
    p.set_defaults(func=cmd_cache_list)

    # --- cache-remove: drop one entry ---
    p = sub.add_parser("cache-remove", help="Remove one cached entry")
    p.add_argument("chat", help="Chat identifier (name, alias, or ID) — cache-only, no API fallback")
    p.set_defaults(func=cmd_cache_remove)

    # --- cache-clear: wipe everything ---
    p = sub.add_parser("cache-clear", help="Remove all cached entries (requires --confirm)")
    p.add_argument("--confirm", action="store_true", help="Required safety flag")
    p.set_defaults(func=cmd_cache_clear)

    # --- alias-add: attach one or more aliases to a chat ---
    p = sub.add_parser("alias-add", help="Add aliases to a chat (resolves via API if not cached)")
    p.add_argument("chat", help="Chat identifier (name, alias, or ID)")
    p.add_argument("aliases", nargs="+", help="One or more aliases to attach")
    p.set_defaults(func=cmd_alias_add)

    # --- alias-remove: remove one alias from a chat ---
    p = sub.add_parser("alias-remove", help="Remove one alias from a cached chat")
    p.add_argument("chat", help="Chat identifier (cache-only)")
    p.add_argument("alias", help="Alias to remove")
    p.set_defaults(func=cmd_alias_remove)

    # --- alias-list: show aliases for one chat or all ---
    p = sub.add_parser("alias-list", help="List aliases (for one chat, or all)")
    p.add_argument("chat", nargs="?", default=None, help="Optional chat identifier (cache-only)")
    p.set_defaults(func=cmd_alias_list)

    return parser


def main():
    """
    Entry point. Parses CLI args, dispatches to the appropriate async handler,
    and ensures errors are reported as JSON to stderr.

    Error envelopes:
      - AmbiguousMatchError → {"error": msg, "type": ..., "query": ..., "candidates": [...]}
      - Other exceptions    → {"error": msg, "type": ...}

    Any cache warnings collected during load (corrupt-file recovery) are
    included in either envelope so they don't get silently dropped when a
    command fails.
    """
    # Owner-only permissions for everything this process creates: the
    # session file and its SQLite journal, credentials, cache, downloads.
    os.umask(0o077)
    parser = build_parser()
    args = parser.parse_args()
    try:
        asyncio.run(args.func(args))
    except Exception as e:
        # Base error envelope.
        error_data = {"error": str(e), "type": type(e).__name__}

        # Structured ambiguity: surface candidates as first-class JSON
        # so agents / scripts can parse them without regex on the message.
        from cache import AmbiguousMatchError
        if isinstance(e, AmbiguousMatchError):
            error_data["query"] = e.query
            error_data["candidates"] = e.candidates

        # Preserve cache warnings (corrupt-file recovery etc.) even when
        # the command errored — users shouldn't lose that signal.
        if _CACHE.warnings:
            error_data["warnings"] = list(_CACHE.warnings)

        print(json.dumps(error_data), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
