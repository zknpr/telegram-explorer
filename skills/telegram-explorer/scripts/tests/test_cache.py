"""Unit tests for cache.py. Uses stdlib unittest; no external deps."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from cache import Cache


class TestCacheLoadSave(unittest.TestCase):
    """Tests the fundamental file I/O: creating a fresh cache,
    writing it atomically, and reading it back."""

    def setUp(self):
        # Each test gets its own temp directory so parallel runs don't collide.
        self.tmpdir = tempfile.mkdtemp()
        self.cache_path = Path(self.tmpdir) / "cache.json"

    def tearDown(self):
        # Clean up temp files after each test.
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_load_missing_file_returns_empty_cache(self):
        # A cache pointing at a non-existent file should initialize empty.
        cache = Cache(self.cache_path)
        self.assertEqual(cache.entries, {})
        self.assertEqual(cache.version, 1)

    def test_save_then_load_roundtrip(self):
        # Save a cache with one entry, create a new Cache on the same path,
        # verify the entry is preserved.
        cache = Cache(self.cache_path)
        cache.entries["123"] = {
            "id": "123",
            "name": "Test Chat",
            "type": "channel",
            "aliases": [],
            "last_used_at": None,
            "first_seen_at": "2026-04-21T12:00:00+00:00",
        }
        cache.save_atomic()

        cache2 = Cache(self.cache_path)
        self.assertIn("123", cache2.entries)
        self.assertEqual(cache2.entries["123"]["name"], "Test Chat")

    def test_save_is_atomic_uses_temp_and_rename(self):
        # After a successful save, the target file exists AND no .tmp leftovers
        # linger in the directory. We glob because mkstemp injects random chars
        # between the prefix and suffix (e.g. "cache.json.n9o9cqta.tmp"), so a
        # literal "cache.json.tmp" check would be trivially true regardless of
        # implementation correctness.
        cache = Cache(self.cache_path)
        cache.entries["1"] = {"id": "1", "name": "x", "type": "user",
                              "aliases": [], "last_used_at": None,
                              "first_seen_at": "2026-04-21T12:00:00+00:00"}
        cache.save_atomic()
        self.assertTrue(self.cache_path.exists())
        leftover_tmps = list(Path(self.tmpdir).glob("cache.json.*.tmp"))
        self.assertEqual(leftover_tmps, [])

    def test_saved_file_is_valid_json_with_expected_shape(self):
        # The on-disk format must match the spec: {"version": 1, "entries": {...}}.
        cache = Cache(self.cache_path)
        cache.entries["1"] = {"id": "1", "name": "x", "type": "user",
                              "aliases": [], "last_used_at": None,
                              "first_seen_at": "2026-04-21T12:00:00+00:00"}
        cache.save_atomic()

        with open(self.cache_path) as f:
            data = json.load(f)
        self.assertEqual(data["version"], 1)
        self.assertIn("entries", data)
        self.assertIn("1", data["entries"])


class TestCacheCorruptRecovery(unittest.TestCase):
    """Tests that a corrupt JSON file is moved aside and an empty cache
    takes over, with a warning recorded."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.cache_path = Path(self.tmpdir) / "cache.json"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_corrupt_file_is_quarantined_and_cache_starts_empty(self):
        # Write garbage to the cache path.
        self.cache_path.write_text("{ this is not valid json ]]]")

        # Loading must not raise.
        cache = Cache(self.cache_path)

        # Cache should be empty.
        self.assertEqual(cache.entries, {})
        # A warning must be recorded for the caller to surface.
        self.assertTrue(any("corrupt" in w.lower() for w in cache.warnings))
        # The original file must have been moved aside with a .corrupt. prefix
        # suffix so the user can inspect it.
        quarantined = list(Path(self.tmpdir).glob("cache.json.corrupt.*"))
        self.assertEqual(len(quarantined), 1)

    def test_corrupt_recovery_allows_immediate_save(self):
        # After corrupt recovery, saving a new entry should work normally.
        self.cache_path.write_text("not json")
        cache = Cache(self.cache_path)
        cache.entries["1"] = {"id": "1", "name": "x", "type": "user",
                              "aliases": [], "last_used_at": None,
                              "first_seen_at": "2026-04-21T12:00:00+00:00"}
        cache.save_atomic()

        # Reloading should now succeed.
        cache2 = Cache(self.cache_path)
        self.assertIn("1", cache2.entries)

    def test_wrong_shape_json_is_treated_as_corrupt(self):
        # Well-formed JSON whose top level is not an object (list, null, string,
        # number) is semantically corrupt for our schema. It must be quarantined
        # and recovered the same way syntactically-bad JSON is.
        self.cache_path.write_text("[]")
        cache = Cache(self.cache_path)
        self.assertEqual(cache.entries, {})
        self.assertTrue(any("corrupt" in w.lower() for w in cache.warnings))
        quarantined = list(Path(self.tmpdir).glob("cache.json.corrupt.*"))
        self.assertEqual(len(quarantined), 1)

    def test_multiple_corrupt_recoveries_do_not_clobber_each_other(self):
        # Two corrupt recoveries in quick succession must produce two distinct
        # quarantine files, not silently overwrite the first. This exercises
        # the nanosecond-resolution filename scheme.
        self.cache_path.write_text("garbage 1")
        Cache(self.cache_path)  # first recovery
        self.cache_path.write_text("garbage 2")
        Cache(self.cache_path)  # second recovery
        quarantined = list(Path(self.tmpdir).glob("cache.json.corrupt.*"))
        self.assertEqual(len(quarantined), 2)


class TestCacheUpsert(unittest.TestCase):
    """Tests the two upsert paths and their different semantics for
    last_used_at, first_seen_at, and aliases preservation.

    Timestamps are injected via patch.object(Cache, "_now_iso") so each
    call in a test returns a distinct, known value. `datetime.now()` has
    microsecond precision, but back-to-back calls collide frequently
    enough (~30% in benchmarks) that a buggy implementation that
    *incorrectly* reassigns a timestamp could still produce a byte-
    identical result and slip past a straightforward `assertEqual`. Using
    explicit scripted timestamps eliminates that ambiguity."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.cache_path = Path(self.tmpdir) / "cache.json"
        self.cache = Cache(self.cache_path)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_upsert_from_listing_creates_entry_with_null_last_used(self):
        # A brand-new entry from a listing call should have first_seen_at set
        # but last_used_at as None (it hasn't been operationally used yet).
        self.cache.upsert_from_listing({"id": "123", "name": "Dev Chat", "type": "channel"})
        entry = self.cache.entries["123"]
        self.assertEqual(entry["name"], "Dev Chat")
        self.assertEqual(entry["type"], "channel")
        self.assertIsNone(entry["last_used_at"])
        self.assertIsNotNone(entry["first_seen_at"])
        self.assertEqual(entry["aliases"], [])

    def test_upsert_from_operation_creates_entry_with_timestamp(self):
        # A brand-new entry from an operational call should have both
        # first_seen_at AND last_used_at set to roughly now.
        self.cache.upsert_from_operation({"id": "456", "name": "Mom", "type": "user"})
        entry = self.cache.entries["456"]
        self.assertIsNotNone(entry["last_used_at"])
        self.assertIsNotNone(entry["first_seen_at"])

    def test_upsert_from_listing_on_existing_entry_preserves_aliases_and_last_used(self):
        # Use a patched clock so the operation-create timestamp is distinct
        # from the listing-update timestamp. If upsert_from_listing were
        # buggy and reassigned last_used_at, this test would catch it
        # deterministically instead of ~70% of the time.
        from unittest.mock import patch
        scripted = iter([
            "2026-04-21T10:00:00+00:00",  # operation create: first_seen_at (from _base_entry)
            "2026-04-21T10:00:00+00:00",  # operation create: last_used_at
            "2026-04-21T11:00:00+00:00",  # listing update: first_seen_at (should not be used for existing entry)
        ])
        with patch.object(Cache, "_now_iso", staticmethod(lambda: next(scripted))):
            self.cache.upsert_from_operation({"id": "789", "name": "Old Name", "type": "channel"})
            self.cache.entries["789"]["aliases"] = ["mine"]
            self.assertEqual(self.cache.entries["789"]["last_used_at"], "2026-04-21T10:00:00+00:00")

            # A later listing call refreshes the name (chat renamed server-side) but
            # leaves aliases, first_seen_at, and last_used_at untouched.
            self.cache.upsert_from_listing({"id": "789", "name": "New Name", "type": "channel"})

        entry = self.cache.entries["789"]
        self.assertEqual(entry["name"], "New Name")
        self.assertEqual(entry["aliases"], ["mine"])
        self.assertEqual(entry["last_used_at"], "2026-04-21T10:00:00+00:00")
        self.assertEqual(entry["first_seen_at"], "2026-04-21T10:00:00+00:00")

    def test_upsert_from_operation_on_existing_entry_bumps_last_used(self):
        # Patched clock: operation create t=10:00, operation update t=11:00.
        # Verifies not just "last_used_at became non-null" but "it advanced
        # from the earlier timestamp to the later one".
        from unittest.mock import patch
        scripted = iter([
            "2026-04-21T10:00:00+00:00",  # first operation: first_seen_at (from _base_entry)
            "2026-04-21T10:00:00+00:00",  # first operation: last_used_at
            "2026-04-21T11:00:00+00:00",  # second operation: last_used_at must bump
        ])
        with patch.object(Cache, "_now_iso", staticmethod(lambda: next(scripted))):
            self.cache.upsert_from_operation({"id": "111", "name": "Test", "type": "user"})
            self.assertEqual(self.cache.entries["111"]["last_used_at"], "2026-04-21T10:00:00+00:00")

            # Second operational use must advance the timestamp, not leave it fixed.
            self.cache.upsert_from_operation({"id": "111", "name": "Test", "type": "user"})

        self.assertEqual(self.cache.entries["111"]["last_used_at"], "2026-04-21T11:00:00+00:00")

    def test_upsert_preserves_first_seen_at_on_update(self):
        # first_seen_at is set once and never changes. Patched clock: listing
        # create t=10:00, operation update t=11:00 — so first_seen_at must
        # still read 10:00 after the update regardless of which call happens.
        from unittest.mock import patch
        scripted = iter([
            "2026-04-21T10:00:00+00:00",  # listing create: first_seen_at
            "2026-04-21T11:00:00+00:00",  # operation update: first_seen_at (must not be used)
            "2026-04-21T11:00:00+00:00",  # operation update: last_used_at
        ])
        with patch.object(Cache, "_now_iso", staticmethod(lambda: next(scripted))):
            self.cache.upsert_from_listing({"id": "222", "name": "A", "type": "user"})
            self.cache.upsert_from_operation({"id": "222", "name": "A", "type": "user"})

        self.assertEqual(self.cache.entries["222"]["first_seen_at"], "2026-04-21T10:00:00+00:00")


class TestResolveDirectId(unittest.TestCase):
    """Tests that syntactic IDs (numeric, -100 channels, +phone, @username,
    t.me links, JID-style) pass through the resolver unchanged."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.cache_path = Path(self.tmpdir) / "cache.json"
        self.cache = Cache(self.cache_path)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_numeric_id_passes_through(self):
        # A plain integer-looking string is a user ID.
        result = self.cache.resolve("123456789")
        self.assertEqual(result["id"], "123456789")
        self.assertTrue(result.get("_synthetic"))

    def test_channel_id_with_minus_100_prefix_passes_through(self):
        result = self.cache.resolve("-1001234567890")
        self.assertEqual(result["id"], "-1001234567890")
        self.assertTrue(result.get("_synthetic"))

    def test_phone_number_passes_through(self):
        result = self.cache.resolve("+14155551234")
        self.assertEqual(result["id"], "+14155551234")
        self.assertTrue(result.get("_synthetic"))

    def test_username_passes_through(self):
        result = self.cache.resolve("@channelname")
        self.assertEqual(result["id"], "@channelname")
        self.assertTrue(result.get("_synthetic"))

    def test_tme_link_passes_through(self):
        result = self.cache.resolve("https://t.me/joinchat/ABC")
        self.assertEqual(result["id"], "https://t.me/joinchat/ABC")
        self.assertTrue(result.get("_synthetic"))

    def test_direct_id_does_not_touch_cache(self):
        # Passing a raw ID must not create a cache entry.
        self.cache.resolve("-1009999999")
        self.assertEqual(self.cache.entries, {})

    def test_telegram_me_legacy_domain_passes_through(self):
        # telegram.me/ is the legacy Telegram URL domain, still active.
        result = self.cache.resolve("https://telegram.me/channelname")
        self.assertTrue(result.get("_synthetic"))

    def test_tme_link_is_case_insensitive(self):
        # Real users paste URLs with capitalization quirks from mobile shares.
        result = self.cache.resolve("HTTPS://T.ME/foo")
        self.assertTrue(result.get("_synthetic"))

    def test_looks_like_id_rejects_plain_names(self):
        # Non-ID strings must NOT short-circuit. We expect a NotImplementedError
        # (not a hit) because no other resolver steps are implemented yet —
        # but the assertion that matters is "did NOT return a _synthetic dict".
        self.assertFalse(Cache.looks_like_id("alice"))
        self.assertFalse(Cache.looks_like_id("dev chat"))
        self.assertFalse(Cache.looks_like_id("channel-name"))

    def test_looks_like_id_rejects_empty_and_non_string(self):
        # Defensive checks for type / emptiness.
        self.assertFalse(Cache.looks_like_id(""))
        self.assertFalse(Cache.looks_like_id(None))
        self.assertFalse(Cache.looks_like_id(123))
        self.assertFalse(Cache.looks_like_id([]))

    def test_looks_like_id_rejects_unicode_digits(self):
        # Python's int() accepts Unicode decimal digits (Arabic-Indic etc.),
        # which would silently map to a different Latin-digit ID. ASCII-only.
        self.assertFalse(Cache.looks_like_id("١٢٣"))  # Arabic-Indic 123
        self.assertFalse(Cache.looks_like_id("+١٢٣"))

    def test_looks_like_id_rejects_int_with_underscores(self):
        # Python's int() accepts '1_000'; we don't.
        self.assertFalse(Cache.looks_like_id("1_000"))


class TestResolveLast(unittest.TestCase):
    """Tests the `last` reserved keyword."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.cache_path = Path(self.tmpdir) / "cache.json"
        self.cache = Cache(self.cache_path)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_last_returns_entry_with_greatest_last_used_at(self):
        # Two operational entries with explicit timestamps.
        self.cache.entries["A"] = {
            "id": "A", "name": "First", "type": "user", "aliases": [],
            "last_used_at": "2026-04-20T10:00:00+00:00",
            "first_seen_at": "2026-04-20T10:00:00+00:00",
        }
        self.cache.entries["B"] = {
            "id": "B", "name": "Second", "type": "user", "aliases": [],
            "last_used_at": "2026-04-21T10:00:00+00:00",
            "first_seen_at": "2026-04-21T10:00:00+00:00",
        }
        result = self.cache.resolve("last")
        self.assertEqual(result["id"], "B")

    def test_last_ignores_entries_with_null_last_used(self):
        # Listing-only entries (last_used_at=None) should not qualify as "last".
        self.cache.entries["L"] = {
            "id": "L", "name": "ListOnly", "type": "user", "aliases": [],
            "last_used_at": None,
            "first_seen_at": "2026-04-21T12:00:00+00:00",
        }
        self.cache.entries["O"] = {
            "id": "O", "name": "Operated", "type": "user", "aliases": [],
            "last_used_at": "2026-04-20T10:00:00+00:00",
            "first_seen_at": "2026-04-20T10:00:00+00:00",
        }
        result = self.cache.resolve("last")
        self.assertEqual(result["id"], "O")

    def test_last_on_empty_cache_raises(self):
        with self.assertRaises(ValueError) as ctx:
            self.cache.resolve("last")
        self.assertIn("last", str(ctx.exception).lower())

    def test_last_with_only_listing_entries_raises(self):
        # Cache has listings but nothing operationally used.
        self.cache.entries["L"] = {
            "id": "L", "name": "ListOnly", "type": "user", "aliases": [],
            "last_used_at": None,
            "first_seen_at": "2026-04-21T12:00:00+00:00",
        }
        with self.assertRaises(ValueError):
            self.cache.resolve("last")

    def test_last_handles_mixed_timezone_offsets(self):
        # Hand-editable cache can contain timestamps in different timezone
        # offsets. The "greatest" one must reflect the actual later instant,
        # not the lexicographically-greater string. In this pair, B's
        # -05:00 wall clock is actually later in UTC than A's +00:00.
        self.cache.entries["A"] = {
            "id": "A", "name": "UTC Entry", "type": "user", "aliases": [],
            "last_used_at": "2026-04-21T10:00:00+00:00",  # 10:00 UTC
            "first_seen_at": "2026-04-21T10:00:00+00:00",
        }
        self.cache.entries["B"] = {
            "id": "B", "name": "NY Entry", "type": "user", "aliases": [],
            "last_used_at": "2026-04-21T06:00:00-05:00",  # 11:00 UTC (later!)
            "first_seen_at": "2026-04-21T06:00:00-05:00",
        }
        self.assertEqual(self.cache.resolve("last")["id"], "B")

    def test_last_with_insertion_order_unrelated_to_timestamp(self):
        # Guard against a buggy implementation that returns "last inserted"
        # rather than "greatest timestamp". Insert B first (older), then A
        # (newer); A must still win.
        self.cache.entries["B"] = {
            "id": "B", "name": "Older Insert", "type": "user", "aliases": [],
            "last_used_at": "2026-04-20T10:00:00+00:00",
            "first_seen_at": "2026-04-20T10:00:00+00:00",
        }
        self.cache.entries["A"] = {
            "id": "A", "name": "Newer Insert", "type": "user", "aliases": [],
            "last_used_at": "2026-04-21T10:00:00+00:00",
            "first_seen_at": "2026-04-21T10:00:00+00:00",
        }
        self.assertEqual(self.cache.resolve("last")["id"], "A")


class TestResolveAliasAndName(unittest.TestCase):
    """Tests steps 3 (alias exact), 4 (name exact), 5 (name partial with
    recency tiebreak) and ambiguous-match error."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.cache_path = Path(self.tmpdir) / "cache.json"
        self.cache = Cache(self.cache_path)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _add(self, id_, name, aliases=None, last_used_at=None):
        """Helper to add an entry inline."""
        self.cache.entries[id_] = {
            "id": id_, "name": name, "type": "user",
            "aliases": aliases or [],
            "last_used_at": last_used_at,
            "first_seen_at": "2026-04-21T12:00:00+00:00",
        }

    # --- Step 3: alias exact match ---

    def test_alias_exact_match_wins(self):
        self._add("A", "Some Group", aliases=["dev chat"])
        result = self.cache.resolve("dev chat")
        self.assertEqual(result["id"], "A")

    def test_alias_match_is_case_insensitive(self):
        self._add("A", "Some Group", aliases=["Dev Chat"])
        result = self.cache.resolve("DEV chat")
        self.assertEqual(result["id"], "A")

    def test_alias_beats_name(self):
        # Alias "mom" exists on entry A; entry B is literally named "Mom".
        # Alias wins.
        self._add("A", "Some Person", aliases=["mom"])
        self._add("B", "Mom")
        result = self.cache.resolve("mom")
        self.assertEqual(result["id"], "A")

    # --- Step 4: exact name match ---

    def test_exact_name_match(self):
        self._add("A", "Dev Chat")
        result = self.cache.resolve("Dev Chat")
        self.assertEqual(result["id"], "A")

    def test_exact_name_match_case_insensitive(self):
        self._add("A", "Dev Chat")
        result = self.cache.resolve("dev chat")
        self.assertEqual(result["id"], "A")

    def test_exact_name_beats_partial(self):
        # "dev" matches partial on "Dev Leads" but exact on "dev".
        # Exact must win.
        self._add("A", "Dev Leads")
        self._add("B", "dev")
        result = self.cache.resolve("dev")
        self.assertEqual(result["id"], "B")

    # --- Step 5: partial name match with recency tiebreak ---

    def test_partial_match_single_hit(self):
        self._add("A", "Dev Leads")
        result = self.cache.resolve("leads")
        self.assertEqual(result["id"], "A")

    def test_partial_match_recency_tiebreak(self):
        # Two entries both match "eng"; the more recently used wins.
        self._add("A", "Engineering", last_used_at="2026-04-20T10:00:00+00:00")
        self._add("B", "Eng Team", last_used_at="2026-04-21T10:00:00+00:00")
        result = self.cache.resolve("eng")
        self.assertEqual(result["id"], "B")

    def test_partial_match_ambiguous_no_recency_raises(self):
        # Both entries match, neither has a last_used_at, should raise.
        self._add("A", "Engineering")
        self._add("B", "Engine Room")
        with self.assertRaises(ValueError) as ctx:
            self.cache.resolve("eng")
        # The error should list the candidates.
        msg = str(ctx.exception).lower()
        self.assertIn("ambiguous", msg)

    def test_cache_miss_raises_lookup_error(self):
        # No match at all, no fallback: must raise LookupError.
        self._add("A", "Nothing to see here")
        with self.assertRaises(LookupError):
            self.cache.resolve("does not exist")

    def test_partial_match_one_none_one_timestamp_picks_timestamped(self):
        # The realistic production case: some entries are listing-only
        # (last_used_at=None), others have been operationally used (real
        # timestamp). The timestamped one must win the tiebreak — and this
        # must not crash with TypeError from mixing None and datetime.
        self._add("A", "Engineering")  # listing-only, last_used_at=None
        self._add("B", "Eng Team", last_used_at="2026-04-21T10:00:00+00:00")
        result = self.cache.resolve("eng")
        self.assertEqual(result["id"], "B")

    def test_partial_match_insertion_order_does_not_matter(self):
        # Reverse of the previous test: insert the timestamped entry first,
        # the listing-only second. A buggy implementation that picked
        # "first inserted" would silently return the wrong entry.
        self._add("B", "Eng Team", last_used_at="2026-04-21T10:00:00+00:00")
        self._add("A", "Engineering")  # listing-only
        result = self.cache.resolve("eng")
        self.assertEqual(result["id"], "B")

    def test_ambiguous_match_raises_structured_candidates(self):
        # AmbiguousMatchError should carry the candidate list as a structured
        # attribute, not just embed it in the exception message.
        self._add("A", "Engineering")
        self._add("B", "Engine Room")
        with self.assertRaises(ValueError) as ctx:
            self.cache.resolve("eng")
        # Must be the specific subclass so callers can branch on it.
        from cache import AmbiguousMatchError
        self.assertIsInstance(ctx.exception, AmbiguousMatchError)
        # Candidates carried as structured list of dicts.
        self.assertEqual(ctx.exception.query, "eng")
        self.assertEqual(len(ctx.exception.candidates), 2)
        candidate_ids = {c["id"] for c in ctx.exception.candidates}
        self.assertEqual(candidate_ids, {"A", "B"})

    # --- Display-name impersonation: identical names must never be
    # silently resolved, regardless of insertion order or recency. ---

    def test_exact_name_duplicates_raise_regardless_of_insertion_order(self):
        # An attacker who DMs the user (or shares a group the user lists)
        # can set their display name to match a real contact. Whichever
        # entry happened to be cached first must not win silently.
        from cache import AmbiguousMatchError
        self._add("ATTACKER", "Alice Smith")
        self._add("REAL", "Alice Smith")
        with self.assertRaises(AmbiguousMatchError) as ctx:
            self.cache.resolve("Alice Smith")
        self.assertEqual({c["id"] for c in ctx.exception.candidates},
                         {"ATTACKER", "REAL"})

    def test_exact_name_duplicates_raise_even_with_recency_difference(self):
        # Reading the attacker's DM bumps their last_used_at, so recency
        # cannot be trusted to pick between identical display names.
        from cache import AmbiguousMatchError
        self._add("REAL", "Alice Smith", last_used_at="2026-04-01T10:00:00+00:00")
        self._add("ATTACKER", "alice smith", last_used_at="2026-04-21T10:00:00+00:00")
        with self.assertRaises(AmbiguousMatchError):
            self.cache.resolve("Alice Smith")

    # --- strict=True (used for outbound commands): no recency tiebreak ---

    def test_strict_partial_match_does_not_use_recency_tiebreak(self):
        from cache import AmbiguousMatchError
        self._add("REAL", "Alice Smith", last_used_at="2026-04-01T10:00:00+00:00")
        self._add("ATTACKER", "Alice Smith.", last_used_at="2026-04-21T10:00:00+00:00")
        with self.assertRaises(AmbiguousMatchError):
            self.cache.resolve("alice", strict=True)

    def test_strict_partial_match_single_hit_still_resolves(self):
        self._add("A", "Dev Leads")
        self.assertEqual(self.cache.resolve("leads", strict=True)["id"], "A")

    def test_strict_does_not_affect_alias_or_unique_exact_match(self):
        self._add("A", "Alice Smith", aliases=["alice"])
        self._add("B", "Alice Jones")
        self.assertEqual(self.cache.resolve("alice", strict=True)["id"], "A")
        self.assertEqual(self.cache.resolve("Alice Jones", strict=True)["id"], "B")

    def test_non_strict_keeps_recency_tiebreak_for_distinct_names(self):
        # Control arm for the strict tests: read paths keep the convenience.
        self._add("A", "Engineering", last_used_at="2026-04-20T10:00:00+00:00")
        self._add("B", "Eng Team", last_used_at="2026-04-21T10:00:00+00:00")
        self.assertEqual(self.cache.resolve("eng")["id"], "B")


class TestResolveApiFallback(unittest.TestCase):
    """Tests step 6: if no cache match, call api_fallback_fn and upsert the
    result. Uses a stub callable so no network is involved."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.cache_path = Path(self.tmpdir) / "cache.json"
        self.cache = Cache(self.cache_path)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_fallback_called_on_cache_miss(self):
        # Stub fallback returns a dict; resolver must upsert it and return it.
        calls = []
        def fallback(q):
            calls.append(q)
            return {"id": "999", "name": "Found by API", "type": "channel"}

        result = self.cache.resolve("mystery chat", api_fallback_fn=fallback)
        self.assertEqual(calls, ["mystery chat"])
        self.assertEqual(result["id"], "999")
        self.assertEqual(result["name"], "Found by API")
        # The entry should now be cached as operational (last_used_at set).
        self.assertIn("999", self.cache.entries)
        self.assertIsNotNone(self.cache.entries["999"]["last_used_at"])

    def test_fallback_returning_none_raises_lookup_error(self):
        def fallback(q):
            return None
        with self.assertRaises(LookupError):
            self.cache.resolve("nothing", api_fallback_fn=fallback)

    def test_fallback_not_called_if_cache_hit(self):
        # Pre-populate an entry; fallback should never fire.
        self.cache.entries["A"] = {
            "id": "A", "name": "Known", "type": "user", "aliases": ["k"],
            "last_used_at": None,
            "first_seen_at": "2026-04-21T12:00:00+00:00",
        }
        calls = []
        def fallback(q):
            calls.append(q)
            return None
        result = self.cache.resolve("k", api_fallback_fn=fallback)
        self.assertEqual(calls, [])
        self.assertEqual(result["id"], "A")

    def test_fallback_not_called_for_direct_id(self):
        calls = []
        def fallback(q):
            calls.append(q)
            return None
        result = self.cache.resolve("-1001234567890", api_fallback_fn=fallback)
        self.assertEqual(calls, [])
        self.assertTrue(result.get("_synthetic"))


class TestResolveCachedOnly(unittest.TestCase):
    """Tests that resolve_cached_only behaves like resolve but never
    invokes API fallback — it returns LookupError on miss instead."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.cache_path = Path(self.tmpdir) / "cache.json"
        self.cache = Cache(self.cache_path)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_cached_only_hits_existing_entry(self):
        self.cache.entries["A"] = {
            "id": "A", "name": "X", "type": "user", "aliases": ["x"],
            "last_used_at": None,
            "first_seen_at": "2026-04-21T12:00:00+00:00",
        }
        result = self.cache.resolve_cached_only("x")
        self.assertEqual(result["id"], "A")

    def test_cached_only_raises_on_miss(self):
        # Empty cache, no direct-ID form, no fallback — must raise.
        with self.assertRaises(LookupError):
            self.cache.resolve_cached_only("unknown")

    def test_cached_only_still_handles_direct_id(self):
        # Direct IDs always pass through regardless of cache state.
        result = self.cache.resolve_cached_only("-1001234567890")
        self.assertTrue(result.get("_synthetic"))

    def test_cached_only_handles_last_keyword(self):
        # 'last' should work the same in both resolver modes.
        self.cache.entries["A"] = {
            "id": "A", "name": "X", "type": "user", "aliases": [],
            "last_used_at": "2026-04-21T12:00:00+00:00",
            "first_seen_at": "2026-04-21T12:00:00+00:00",
        }
        result = self.cache.resolve_cached_only("last")
        self.assertEqual(result["id"], "A")


class TestAliasManagement(unittest.TestCase):
    """Tests add_aliases and remove_alias on Cache entries."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.cache_path = Path(self.tmpdir) / "cache.json"
        self.cache = Cache(self.cache_path)
        # Seed one entry for alias operations.
        self.cache.entries["A"] = {
            "id": "A", "name": "Dev Group", "type": "channel", "aliases": [],
            "last_used_at": None,
            "first_seen_at": "2026-04-21T12:00:00+00:00",
        }

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_add_single_alias(self):
        self.cache.add_aliases("A", ["dev chat"])
        self.assertEqual(self.cache.entries["A"]["aliases"], ["dev chat"])

    def test_add_multiple_aliases(self):
        self.cache.add_aliases("A", ["dev chat", "dev", "devs"])
        self.assertEqual(
            sorted(self.cache.entries["A"]["aliases"]),
            ["dev", "dev chat", "devs"],
        )

    def test_add_duplicate_alias_is_noop(self):
        self.cache.add_aliases("A", ["dev chat"])
        self.cache.add_aliases("A", ["dev chat"])
        self.assertEqual(self.cache.entries["A"]["aliases"], ["dev chat"])

    def test_add_duplicate_alias_case_insensitive(self):
        # "Dev Chat" and "dev chat" are considered the same alias.
        self.cache.add_aliases("A", ["Dev Chat"])
        self.cache.add_aliases("A", ["dev chat"])
        self.assertEqual(self.cache.entries["A"]["aliases"], ["Dev Chat"])

    def test_add_aliases_to_nonexistent_entry_raises(self):
        with self.assertRaises(KeyError):
            self.cache.add_aliases("does-not-exist", ["foo"])

    def test_remove_alias(self):
        self.cache.add_aliases("A", ["dev chat", "dev"])
        self.cache.remove_alias("A", "dev")
        self.assertEqual(self.cache.entries["A"]["aliases"], ["dev chat"])

    def test_remove_alias_case_insensitive(self):
        self.cache.add_aliases("A", ["Dev Chat"])
        self.cache.remove_alias("A", "dev chat")
        self.assertEqual(self.cache.entries["A"]["aliases"], [])

    def test_remove_nonexistent_alias_raises(self):
        with self.assertRaises(ValueError):
            self.cache.remove_alias("A", "never added")

    def test_add_alias_rejected_if_already_on_another_entry(self):
        # Aliases must be globally unique across the cache — if one is
        # already attached to a different entry, reject the add.
        self.cache.entries["OTHER"] = {
            "id": "OTHER", "name": "Other Chat", "type": "user",
            "aliases": ["shared"], "last_used_at": None,
            "first_seen_at": "2026-04-21T12:00:00+00:00",
        }
        with self.assertRaises(ValueError) as ctx:
            self.cache.add_aliases("A", ["shared"])
        self.assertIn("OTHER", str(ctx.exception))

    def test_add_alias_cross_entry_check_is_case_insensitive(self):
        # Case-insensitivity applies to the cross-entry check too.
        self.cache.entries["OTHER"] = {
            "id": "OTHER", "name": "Other Chat", "type": "user",
            "aliases": ["Shared"], "last_used_at": None,
            "first_seen_at": "2026-04-21T12:00:00+00:00",
        }
        with self.assertRaises(ValueError):
            self.cache.add_aliases("A", ["SHARED"])

    def test_add_alias_atomic_rejects_all_on_any_conflict(self):
        # If one of several aliases conflicts, NONE should be added
        # (no half-update).
        self.cache.entries["OTHER"] = {
            "id": "OTHER", "name": "Other Chat", "type": "user",
            "aliases": ["conflict"], "last_used_at": None,
            "first_seen_at": "2026-04-21T12:00:00+00:00",
        }
        with self.assertRaises(ValueError):
            self.cache.add_aliases("A", ["fine", "conflict", "also_fine"])
        # Entry A should have gained no aliases.
        self.assertEqual(self.cache.entries["A"]["aliases"], [])


class TestCacheBookkeeping(unittest.TestCase):
    """Tests drop, clear, and list_all (with its documented sort order)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.cache_path = Path(self.tmpdir) / "cache.json"
        self.cache = Cache(self.cache_path)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _add(self, id_, name, last_used_at=None, first_seen_at="2026-04-21T12:00:00+00:00"):
        self.cache.entries[id_] = {
            "id": id_, "name": name, "type": "user", "aliases": [],
            "last_used_at": last_used_at,
            "first_seen_at": first_seen_at,
        }

    def test_drop_removes_entry(self):
        self._add("A", "Alpha")
        self._add("B", "Bravo")
        self.cache.drop("A")
        self.assertNotIn("A", self.cache.entries)
        self.assertIn("B", self.cache.entries)

    def test_drop_nonexistent_raises(self):
        with self.assertRaises(KeyError):
            self.cache.drop("X")

    def test_clear_empties_all_entries(self):
        self._add("A", "Alpha")
        self._add("B", "Bravo")
        self.cache.clear()
        self.assertEqual(self.cache.entries, {})

    def test_list_all_returns_list(self):
        self._add("A", "Alpha")
        result = self.cache.list_all()
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 1)

    def test_list_all_sort_order(self):
        # Operationally-used entries first, sorted by last_used_at
        # desc; then listing-only entries by first_seen_at desc.
        self._add("A", "A", last_used_at="2026-04-20T10:00:00+00:00")
        self._add("B", "B", last_used_at="2026-04-21T10:00:00+00:00")
        self._add("C", "C", last_used_at=None, first_seen_at="2026-04-15T00:00:00+00:00")
        self._add("D", "D", last_used_at=None, first_seen_at="2026-04-10T00:00:00+00:00")

        result = self.cache.list_all()
        ids_in_order = [e["id"] for e in result]
        # B (most recent op), A (older op), C (newer first_seen), D (older first_seen)
        self.assertEqual(ids_in_order, ["B", "A", "C", "D"])


class TestMalformedTimestamps(unittest.TestCase):
    """Hand-edited cache files can contain malformed timestamps. These must
    not crash the resolver or list_all — they should degrade gracefully by
    treating the bad entry as if it had no timestamp."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.cache_path = Path(self.tmpdir) / "cache.json"
        self.cache = Cache(self.cache_path)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _add(self, id_, name, last_used_at=None, first_seen_at="2026-04-21T12:00:00+00:00"):
        self.cache.entries[id_] = {
            "id": id_, "name": name, "type": "user", "aliases": [],
            "last_used_at": last_used_at,
            "first_seen_at": first_seen_at,
        }

    def test_last_resolver_tolerates_malformed_last_used_at(self):
        # One good entry, one malformed. `last` must still find the good one,
        # not crash.
        self._add("GOOD", "Good Entry", last_used_at="2026-04-21T10:00:00+00:00")
        self._add("BAD", "Bad Entry", last_used_at="not-a-timestamp")
        result = self.cache.resolve("last")
        self.assertEqual(result["id"], "GOOD")

    def test_partial_match_tolerates_malformed_last_used_at(self):
        # Partial match with one malformed and one valid timestamp.
        # The valid-timestamped entry should win the tiebreak.
        self._add("A", "Eng Team", last_used_at="not-a-timestamp")
        self._add("B", "Engineering", last_used_at="2026-04-21T10:00:00+00:00")
        result = self.cache.resolve("eng")
        self.assertEqual(result["id"], "B")

    def test_list_all_tolerates_malformed_timestamps(self):
        # Malformed entries should appear in the output, not crash the call.
        self._add("A", "Good", last_used_at="2026-04-21T10:00:00+00:00")
        self._add("B", "Bad", last_used_at="garbage")
        self._add("C", "GoodFirstSeen", last_used_at=None, first_seen_at="2026-04-20T00:00:00+00:00")
        self._add("D", "BadFirstSeen", last_used_at=None, first_seen_at="garbage")
        result = self.cache.list_all()
        # All 4 should be present.
        self.assertEqual(len(result), 4)
        # Good operated entry should rank first (valid timestamp > datetime.min).
        self.assertEqual(result[0]["id"], "A")


if __name__ == "__main__":
    unittest.main()
