"""
Persistent chat cache for telegram-explorer.

Stores resolved chat IDs and user-defined aliases in a JSON file
(location chosen by telegram_client.py; injectable for tests). Has zero
Telethon imports so it's fully unit-testable in isolation. The resolver's
API fallback (step 6) is supplied by the caller as a callable.

Schema:
    {
      "version": 1,
      "entries": {
        "<chat_id>": {
          "id": "<chat_id>",
          "name": "<display name>",
          "type": "<platform-specific>",
          "aliases": ["..."],
          "last_used_at": "<ISO timestamp or null>",
          "first_seen_at": "<ISO timestamp>"
        }
      }
    }
"""

import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path


class AmbiguousMatchError(ValueError):
    """
    Raised when a partial-name match has multiple equally-plausible
    candidates and no recency data to break the tie. Carries the list
    of candidates so the CLI layer can surface them as structured JSON
    for the user to pick from, instead of embedding them in an
    unstructured error string.

    Subclasses ValueError so callers that catch ValueError generically
    (as the existing tests do) still work unchanged.
    """

    def __init__(self, query, candidates):
        # `query` is the original resolver input that matched ambiguously.
        # `candidates` is a list of dicts shaped like {"id": ..., "name": ...}
        # — a narrow projection of the full cache entries that's safe to emit
        # as JSON without leaking internal fields.
        self.query = query
        self.candidates = candidates
        super().__init__(
            f"ambiguous match for {query!r}: {len(candidates)} candidates"
        )


class Cache:
    """
    JSON-backed chat cache with atomic writes. Construct with the path to
    the cache file; on construction, the file is loaded (or initialized
    empty if missing/corrupt).
    """

    SCHEMA_VERSION = 1

    def __init__(self, path):
        # Store the cache file path and the list of warnings generated during load
        # (e.g., "corrupt file backed up"). Callers can surface warnings in their
        # JSON output.
        self.path = Path(path)
        self.warnings = []
        # Default state before load
        self.version = self.SCHEMA_VERSION
        self.entries = {}
        # Load existing cache from disk, if present.
        self._load()

    def _load(self):
        """
        Read the cache file from disk. Missing file = empty cache.
        Corrupt/unparseable file = move it aside with a .corrupt.<unix_ns>
        suffix, start fresh with an empty cache, and record a warning for
        the caller to surface in its JSON output.

        "Corrupt" means any of:
          - JSON decode failure (syntax error)
          - Unicode decode failure (non-UTF-8 bytes)
          - OS-level read errors
          - Successfully-parsed JSON that is NOT a dict at the top level
            (a list, null, string, or number would all parse but break
            `.get()` on the next lines — we treat them as corrupt).
        """
        if not self.path.exists():
            # No file yet — stay at defaults (empty cache).
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Well-formed JSON but wrong top-level type is semantically corrupt.
            # Raise so the except branch handles it uniformly with parse errors.
            if not isinstance(data, dict):
                raise json.JSONDecodeError(
                    "cache file top-level must be a JSON object", "", 0
                )
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
            # Move the bad file aside so user can inspect it later.
            # Use a nanosecond-resolution timestamp in the suffix so back-to-back
            # recoveries don't silently clobber each other's evidence.
            quarantine = self.path.with_name(
                f"{self.path.name}.corrupt.{time.time_ns()}"
            )
            try:
                os.replace(self.path, quarantine)
                self.warnings.append(
                    f"cache file was corrupt ({type(e).__name__}); "
                    f"moved to {quarantine.name}, starting empty"
                )
            except OSError as move_err:
                # If we can't even move it, just warn and leave state empty.
                self.warnings.append(
                    f"cache file is corrupt and could not be quarantined: {move_err}"
                )
            return
        # Accept both empty and populated cache files.
        self.version = data.get("version", self.SCHEMA_VERSION)
        self.entries = data.get("entries", {})

    def save_atomic(self):
        """
        Write the cache to disk atomically: write to a .tmp file in the same
        directory (so os.rename is atomic on POSIX), then rename over the target.
        This guarantees either the old file or the new file is visible — never
        a half-written one, even if the process crashes mid-write.
        """
        # Ensure the parent directory exists so new installs don't fail on
        # first save.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Build the on-disk structure exactly matching the schema.
        payload = {
            "version": self.SCHEMA_VERSION,
            "entries": self.entries,
        }
        # Write to a temp file in the same directory (same filesystem, so
        # rename is atomic) then rename. mkstemp gives us a unique name.
        fd, tmp_path = tempfile.mkstemp(
            prefix=self.path.name + ".",
            suffix=".tmp",
            dir=str(self.path.parent),
        )
        try:
            # Write JSON through the file descriptor so we can fsync before close.
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
                # fsync ensures the data is on disk before we rename.
                f.flush()
                os.fsync(f.fileno())
            # Atomic replace — either old or new is visible, never a partial file.
            os.replace(tmp_path, self.path)
        except Exception:
            # Best-effort cleanup if something went wrong before the rename.
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    @staticmethod
    def _now_iso():
        """Return the current UTC time as an ISO-8601 string. Centralized
        so tests can monkey-patch if needed and so the format is consistent
        across all timestamps written by this module."""
        return datetime.now(timezone.utc).isoformat()

    def _base_entry(self, data):
        """
        Build the common fields for a new entry: id, name, type, empty aliases,
        first_seen_at. last_used_at is set by the two upsert paths differently.
        """
        # Coerce id to string because cache keys are always strings (JSON
        # object keys can't be integers anyway).
        return {
            "id": str(data["id"]),
            "name": data.get("name", ""),
            "type": data.get("type", ""),
            "aliases": [],
            "last_used_at": None,
            "first_seen_at": self._now_iso(),
        }

    def upsert_from_listing(self, data):
        """
        Upsert an entry discovered from a listing call (dialogs, topics,
        participants, info). If it's new, create it with last_used_at=None.
        If it already exists, refresh name and type but preserve aliases,
        first_seen_at, and last_used_at.

        data: dict with at minimum 'id'; also 'name' and 'type' recommended.
        """
        key = str(data["id"])
        if key in self.entries:
            # Existing entry: refresh mutable platform-side fields only.
            self.entries[key]["name"] = data.get("name", self.entries[key]["name"])
            self.entries[key]["type"] = data.get("type", self.entries[key]["type"])
        else:
            # New entry from a listing: last_used_at stays None.
            self.entries[key] = self._base_entry(data)

    def upsert_from_operation(self, data):
        """
        Upsert an entry that was the target of an operational call (send,
        messages, reply, etc.). If new, create with last_used_at=now.
        If existing, refresh name/type and bump last_used_at to now; preserve
        aliases and first_seen_at.

        data: dict with at minimum 'id'; also 'name' and 'type' recommended.
        """
        key = str(data["id"])
        now = self._now_iso()
        if key in self.entries:
            # Existing: refresh fields, bump last_used_at.
            self.entries[key]["name"] = data.get("name", self.entries[key]["name"])
            self.entries[key]["type"] = data.get("type", self.entries[key]["type"])
            self.entries[key]["last_used_at"] = now
        else:
            # New via operation: last_used_at set to now.
            entry = self._base_entry(data)
            entry["last_used_at"] = now
            self.entries[key] = entry

    @staticmethod
    def looks_like_id(query):
        """
        Decide whether a query string is already a syntactically-valid
        Telegram identifier. If true, the resolver short-circuits to a
        pass-through (no cache lookup, no cache write).

        Accepted forms:
          - Pure integer, ASCII digits only, with optional leading '-':
            user ID or channel ID (including `-100…` supergroups). ASCII-only
            check because Python's int() would otherwise silently accept
            Unicode decimal digits like Arabic-Indic numerals — a cross-script
            confusion risk.
          - '+phone' where phone is ASCII digits only.
          - '@username' starting with '@'.
          - t.me / telegram.me invite links, case-insensitive, covering:
            `t.me/...`, `http://t.me/...`, `https://t.me/...`,
            `telegram.me/...`, `http://telegram.me/...`, `https://telegram.me/...`.
            Also covers the `t.me/+ABCDEF` newer-style invite links as a
            natural consequence of the `t.me/` prefix match.
        """
        if not isinstance(query, str) or not query:
            return False
        # Username form: @something
        if query.startswith("@"):
            return True
        # Phone form: +<ascii-digits>. Reject Unicode digits explicitly.
        if query.startswith("+"):
            rest = query[1:]
            if rest and rest.isascii() and rest.isdigit():
                return True
        # t.me / telegram.me invite links. Lowercase once for uniform matching.
        lower = query.lower()
        url_prefixes = (
            "t.me/", "http://t.me/", "https://t.me/",
            "telegram.me/", "http://telegram.me/", "https://telegram.me/",
        )
        if any(lower.startswith(p) for p in url_prefixes):
            return True
        # Pure integer, ASCII-only, with optional leading minus.
        stripped = query.lstrip("-")
        if stripped and stripped.isascii() and stripped.isdigit():
            return True
        return False

    def resolve(self, query, api_fallback_fn=None, strict=False):
        """
        Resolve a user-supplied chat reference to a cache entry (or a
        synthetic pass-through). Precedence:
          1. Direct ID pass-through.
          2. 'last' keyword — most recently operationally-used entry.
          3. Alias exact match (case-insensitive).
          4. Name exact match (case-insensitive). More than one entry with
             the same name always raises AmbiguousMatchError.
          5. Name partial match (case-insensitive substring). Ambiguity
             resolved by recency (greater last_used_at wins) unless
             `strict`; raises AmbiguousMatchError if no recency difference.
          6. API fallback via api_fallback_fn callable.

        Display names are chosen by the other party, so anyone who DMs the
        user or shares a listed group can copy a contact's name. Reading
        their message bumps their last_used_at, which makes recency an
        attacker-influenced signal. Hence: identical names never resolve
        silently, and `strict=True` (used for outbound commands) disables
        the recency tiebreak altogether.

        Returns a dict shaped like a cache entry. Direct-ID pass-through
        dicts have '_synthetic': True and are not stored in the cache.
        Raises:
          ValueError: for `last` on empty cache, or ambiguous name match
            (AmbiguousMatchError, a ValueError subclass).
          LookupError: if nothing matches and no api_fallback_fn provided.
        """
        # --- Step 1: Direct ID pass-through ---
        if self.looks_like_id(query):
            return {"id": query, "_synthetic": True}

        # --- Step 2: 'last' keyword ---
        if query == "last":
            # Filter to only operationally-used entries (non-null last_used_at),
            # then pick the one with the greatest timestamp. We parse via
            # datetime.fromisoformat rather than lex-sorting the strings because
            # the cache file is user-editable and a hand-edit like "2026-04-21Z"
            # or a mixed timezone offset (e.g. "2026-04-21T05:00:00-05:00")
            # would silently mis-order a lex-sort. Python 3.11+ fromisoformat
            # accepts both 'Z' suffixes and explicit offsets, normalizing into
            # an absolute instant for comparison.
            operated = [
                e for e in self.entries.values()
                if e.get("last_used_at") is not None
            ]
            if not operated:
                raise ValueError(
                    "`last` keyword: no operationally-used chat in cache"
                )
            return max(operated, key=lambda e: self._parse_iso_safe(e["last_used_at"]))

        q_lower = query.lower()

        # --- Step 3: alias exact match (case-insensitive) ---
        for entry in self.entries.values():
            for alias in entry.get("aliases", []):
                if alias.lower() == q_lower:
                    return entry

        # --- Step 4: name exact match (case-insensitive) ---
        exact_matches = [
            e for e in self.entries.values()
            if e.get("name", "").lower() == q_lower
        ]
        if len(exact_matches) == 1:
            return exact_matches[0]
        if len(exact_matches) > 1:
            # Identical display names are indistinguishable to the user;
            # never pick one by insertion order or recency.
            raise AmbiguousMatchError(query, self._candidates(exact_matches))

        # --- Step 5: name partial match (case-insensitive substring) ---
        partial_matches = [
            e for e in self.entries.values()
            if q_lower in e.get("name", "").lower()
        ]
        if len(partial_matches) == 1:
            return partial_matches[0]
        if len(partial_matches) > 1 and strict:
            raise AmbiguousMatchError(query, self._candidates(partial_matches))
        if len(partial_matches) > 1:
            # Disambiguate by recency using parsed datetimes (same rationale as
            # the `last` keyword: hand-edited cache files can contain Z-suffix
            # or TZ-mixed timestamps where lex-sort misorders). Entries with
            # None or malformed last_used_at return datetime.min via
            # _parse_iso_safe, so they compare as "older than any real timestamp"
            # — no type-mixing issues, no crashes on bad values.
            def sort_key(e):
                last_used = e.get("last_used_at")
                return self._parse_iso_safe(last_used)
            sorted_matches = sorted(partial_matches, key=sort_key, reverse=True)
            top, runner_up = sorted_matches[0], sorted_matches[1]
            # If the top has a real timestamp AND strictly beats the runner-up
            # (a non-None top is strictly greater than datetime.min returned for
            # None/malformed entries, and two real timestamps compare naturally),
            # that's a clean win.
            if top.get("last_used_at") and sort_key(top) > sort_key(runner_up):
                return top
            # Ambiguous: raise with candidate id+name pairs as structured data
            # so the CLI layer can surface them for disambiguation.
            raise AmbiguousMatchError(query, self._candidates(partial_matches))

        # --- Step 6: API fallback ---
        if api_fallback_fn is not None:
            entry_data = api_fallback_fn(query)
            # Caller returns None if the API also can't resolve.
            if entry_data is not None:
                self.upsert_from_operation(entry_data)
                return self.entries[str(entry_data["id"])]

        # --- No fallback or fallback returned None: give up ---
        raise LookupError(f"could not resolve chat reference: {query!r}")

    @staticmethod
    def _candidates(entries):
        """Project entries to the id+name pairs carried by AmbiguousMatchError."""
        return [{"id": e["id"], "name": e.get("name", "?")} for e in entries]

    def resolve_cached_only(self, query):
        """
        Same precedence as resolve() but skips step 6 (API fallback).
        Used by destructive commands (cache remove, alias remove) where
        hitting the network to resolve something the user wants to forget
        is counter-productive.

        Raises LookupError on cache miss. Other errors (ValueError for
        ambiguous matches, 'last' on empty cache) propagate the same as
        resolve().
        """
        # The simplest correct implementation is to delegate to resolve with
        # a None fallback. resolve() already raises LookupError when no
        # fallback is provided and nothing matches steps 1-5.
        return self.resolve(query, api_fallback_fn=None)

    def add_aliases(self, chat_id, aliases):
        """
        Attach one or more aliases to an existing cache entry. Duplicates
        within the same entry (case-insensitive) are silently skipped, and
        the first-added casing is preserved. Aliases already attached to
        a DIFFERENT entry raise ValueError — an alias should map to exactly
        one chat globally, or flat `alias-list` output becomes ambiguous.

        Raises:
          KeyError: if chat_id is not in the cache.
          ValueError: if any provided alias is already attached to a
            different entry.
        """
        key = str(chat_id)
        if key not in self.entries:
            raise KeyError(f"chat_id {key!r} not in cache")

        # Build index of aliases already attached to OTHER entries.
        other_aliases = {}  # alias_lower -> (other_key, other_name)
        for other_key, other_entry in self.entries.items():
            if other_key == key:
                continue
            for alias in other_entry.get("aliases", []):
                other_aliases[alias.lower()] = (other_key, other_entry.get("name", "?"))

        # Validate all aliases before applying any, so a rejection mid-batch
        # doesn't leave the entry in a half-updated state.
        for alias in aliases:
            alias_lower = alias.lower()
            if alias_lower in other_aliases:
                other_key, other_name = other_aliases[alias_lower]
                raise ValueError(
                    f"alias {alias!r} is already attached to chat {other_key!r} "
                    f"({other_name!r}); aliases must be unique across the cache"
                )

        # Apply. Within this entry, dedup case-insensitively and preserve
        # the first-added casing.
        existing_lower = {a.lower() for a in self.entries[key].get("aliases", [])}
        for alias in aliases:
            if alias.lower() not in existing_lower:
                self.entries[key]["aliases"].append(alias)
                existing_lower.add(alias.lower())

    def remove_alias(self, chat_id, alias):
        """
        Remove one alias from an entry. Match is case-insensitive.
        Raises KeyError if chat_id is not in the cache; ValueError if the
        alias isn't attached to that entry.
        """
        key = str(chat_id)
        if key not in self.entries:
            raise KeyError(f"chat_id {key!r} not in cache")
        current = self.entries[key].get("aliases", [])
        # Find the actual stored alias (preserving its original case) by
        # comparing lowercased.
        target_lower = alias.lower()
        for stored in list(current):
            if stored.lower() == target_lower:
                current.remove(stored)
                return
        raise ValueError(f"alias {alias!r} not attached to {key}")

    def drop(self, chat_id):
        """
        Remove one entry from the cache. Raises KeyError if not present.
        """
        key = str(chat_id)
        if key not in self.entries:
            raise KeyError(f"chat_id {key!r} not in cache")
        del self.entries[key]

    def clear(self):
        """
        Remove all entries from the cache. Does not touch the file on disk
        until save_atomic() is called.
        """
        self.entries = {}

    @staticmethod
    def _parse_iso_safe(value):
        """
        Parse an ISO-8601 timestamp string into a timezone-aware datetime.
        On malformed input (hand-edit, schema drift), return datetime.min
        with UTC tzinfo — so the entry sorts as "oldest possible" rather
        than crashing the whole operation. Silent fallback is intentional:
        the cache file is documented as user-editable, and one bad entry
        should not break `cache-list` or `last` for the rest.
        """
        if not value:
            return datetime.min.replace(tzinfo=timezone.utc)
        try:
            return datetime.fromisoformat(value)
        except (ValueError, TypeError):
            return datetime.min.replace(tzinfo=timezone.utc)

    def list_all(self):
        """
        Return all entries as a list, sorted:
          1. Entries with non-null last_used_at first, sorted by that field
             descending.
          2. Then entries with null last_used_at, sorted by first_seen_at
             descending.

        Returns a list of dicts (the entry objects themselves, not copies —
        callers must not mutate).

        Uses datetime.fromisoformat for sorting (not lexicographic string sort)
        to avoid misorders from mixed timezone offsets or 'Z' suffixes in
        hand-edited cache files (same rationale as the `last` keyword handler).
        """
        operated = [e for e in self.entries.values() if e.get("last_used_at") is not None]
        listing_only = [e for e in self.entries.values() if e.get("last_used_at") is None]
        # Sort operationally-used entries by last_used_at descending, parsing
        # timestamps to avoid lex-sort bugs (hand-edited cache may have Z-suffix
        # or mixed TZ offsets that lex-sort would misorder).
        operated.sort(key=lambda e: self._parse_iso_safe(e["last_used_at"]), reverse=True)
        # Sort listing-only entries by first_seen_at descending, also via parsing.
        # Use a safe default if first_seen_at is missing (should not happen in
        # well-formed cache, but be defensive).
        def first_seen_sort_key(e):
            return self._parse_iso_safe(e.get("first_seen_at"))
        listing_only.sort(key=first_seen_sort_key, reverse=True)
        return operated + listing_only
