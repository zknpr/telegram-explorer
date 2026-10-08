"""
Offline tests for telegram_client.py's security-relevant behavior.
No network: every Telegram call goes to a fake client. Requires telethon
to be importable (some helpers import telethon types/errors lazily).
"""

import asyncio
import contextlib
import io
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

# Point all state at a throwaway directory BEFORE importing the module,
# because paths are resolved at import time.
_TMP_ROOT = tempfile.mkdtemp()
os.environ["TELEGRAM_EXPLORER_HOME"] = str(Path(_TMP_ROOT) / "home")
os.environ["XDG_DATA_HOME"] = str(Path(_TMP_ROOT) / "data")
os.environ.pop("TELEGRAM_CACHE_PATH", None)
os.environ.pop("TELEGRAM_API_ID", None)
os.environ.pop("TELEGRAM_API_HASH", None)

import telegram_client as tc  # noqa: E402
from cache import AmbiguousMatchError, Cache  # noqa: E402


def tearDownModule():
    shutil.rmtree(_TMP_ROOT, ignore_errors=True)


def run(coro):
    return asyncio.run(coro)


class User:
    """Stand-in for telethon.tl.types.User; only the class name matters to
    _entity_to_cache_dict."""

    def __init__(self, id_, first_name):
        self.id = id_
        self.first_name = first_name
        self.last_name = None
        self.username = None


class FakeClient:
    def __init__(self, entities=None, authorized=True):
        self.entities = entities or {}
        self.authorized = authorized
        self.sent = []
        self.disconnected = False

    async def connect(self):
        pass

    async def is_user_authorized(self):
        return self.authorized

    async def disconnect(self):
        self.disconnected = True

    async def get_entity(self, peer):
        return self.entities[peer.user_id]

    async def get_messages(self, **kwargs):
        return []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        raise AssertionError("send_message must not be reached in these tests")


class StateDirTestCase(unittest.TestCase):
    def setUp(self):
        tc.HOME_DIR.mkdir(parents=True, exist_ok=True)
        self.outside = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(tc.HOME_DIR, ignore_errors=True)
        shutil.rmtree(self.outside, ignore_errors=True)


class TestUploadRefusesSecrets(StateDirTestCase):
    def _upload(self, path):
        def no_network():
            raise AssertionError("refused upload must not connect")
        args = SimpleNamespace(chat="@someone", file=str(path), caption=None, voice_note=False)
        orig = tc.open_client
        tc.open_client = no_network
        try:
            run(tc.cmd_upload(args))
        finally:
            tc.open_client = orig

    def test_refuses_session_file(self):
        tc.SESSION_FILE.write_bytes(b"auth key")
        with self.assertRaises(PermissionError):
            self._upload(tc.SESSION_FILE)

    def test_refuses_any_file_in_state_dir(self):
        tc.CREDS_PATH.write_text("{}")
        with self.assertRaises(PermissionError):
            self._upload(tc.CREDS_PATH)

    def test_refuses_symlink_to_session(self):
        tc.SESSION_FILE.write_bytes(b"auth key")
        link = self.outside / "innocent.jpg"
        link.symlink_to(tc.SESSION_FILE)
        with self.assertRaises(PermissionError):
            self._upload(link)

    def test_refuses_hardlink_to_session(self):
        tc.SESSION_FILE.write_bytes(b"auth key")
        link = self.outside / "innocent.jpg"
        os.link(tc.SESSION_FILE, link)
        with self.assertRaises(PermissionError):
            self._upload(link)

    def test_allows_ordinary_file(self):
        # Control arm: the guard must not block normal uploads.
        tc.SESSION_FILE.write_bytes(b"auth key")
        normal = self.outside / "photo.jpg"
        normal.write_bytes(b"jpeg")
        tc._refuse_secret_upload(normal)  # must not raise


class TestSessionPermissions(StateDirTestCase):
    def test_new_session_file_is_owner_only_under_permissive_umask(self):
        # Telethon alone creates the session 0644 under umask 022; make_client
        # must tighten it even when main()'s umask(0o077) isn't in effect.
        os.environ["TELEGRAM_API_ID"] = "1"
        os.environ["TELEGRAM_API_HASH"] = "x"
        old_umask = os.umask(0o022)
        try:
            client = tc.make_client()
            client.session.close()
        finally:
            os.umask(old_umask)
            del os.environ["TELEGRAM_API_ID"]
            del os.environ["TELEGRAM_API_HASH"]
        mode = stat.S_IMODE(tc.SESSION_FILE.stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_existing_world_readable_creds_are_tightened(self):
        tc.CREDS_PATH.write_text('{"api_id": 1, "api_hash": "x"}')
        os.chmod(tc.CREDS_PATH, 0o644)
        client = tc.make_client()
        client.session.close()
        self.assertEqual(stat.S_IMODE(tc.CREDS_PATH.stat().st_mode), 0o600)


class TestNonInteractiveAuth(StateDirTestCase):
    def test_missing_creds_raise_not_logged_in(self):
        with self.assertRaises(tc.NotLoggedInError):
            tc.load_creds()

    def test_unauthorized_session_fails_fast_and_disconnects(self):
        fake = FakeClient(authorized=False)
        orig = tc.make_client
        tc.make_client = lambda: fake
        try:
            async def use():
                async with tc.open_client():
                    raise AssertionError("must not yield an unauthorized client")
            with self.assertRaises(tc.NotLoggedInError):
                run(use())
        finally:
            tc.make_client = orig
        self.assertTrue(fake.disconnected)

    def test_login_refuses_without_tty(self):
        orig = sys.stdin
        sys.stdin = io.StringIO("")
        try:
            with self.assertRaises(tc.NotLoggedInError):
                run(tc.cmd_login(SimpleNamespace()))
        finally:
            sys.stdin = orig


class TestOutboundResolutionIsStrict(StateDirTestCase):
    """A look-alike contact whose chat was read more recently must not
    silently receive outbound messages; reads keep the recency tiebreak."""

    def setUp(self):
        super().setUp()
        self.orig_cache, self.orig_open = tc._CACHE, tc.open_client
        tc._CACHE = Cache(self.outside / "cache.json")
        tc._CACHE.entries = {
            "111": {"id": "111", "name": "Alice Smith", "type": "user", "aliases": [],
                    "last_used_at": "2026-04-01T10:00:00+00:00",
                    "first_seen_at": "2026-04-01T10:00:00+00:00"},
            "666": {"id": "666", "name": "Alice Smith.", "type": "user", "aliases": [],
                    "last_used_at": "2026-04-21T10:00:00+00:00",
                    "first_seen_at": "2026-04-21T10:00:00+00:00"},
        }
        self.fake = FakeClient(entities={111: User(111, "Alice Smith"),
                                         666: User(666, "Alice Smith.")})

        @contextlib.asynccontextmanager
        async def fake_open():
            yield self.fake
        tc.open_client = fake_open

    def tearDown(self):
        tc._CACHE, tc.open_client = self.orig_cache, self.orig_open
        super().tearDown()

    def test_send_by_ambiguous_name_raises(self):
        args = SimpleNamespace(chat="alice", text="the contract", topic=None)
        with self.assertRaises(AmbiguousMatchError):
            run(tc.cmd_send(args))
        self.assertEqual(self.fake.sent, [])

    def test_forward_destination_is_strict(self):
        args = SimpleNamespace(from_chat="111", to_chat="alice", message_id=1)
        with self.assertRaises(AmbiguousMatchError):
            run(tc.cmd_forward(args))

    def test_read_by_same_name_uses_recency(self):
        # Control arm: `messages` is a read and resolves via recency.
        args = SimpleNamespace(chat="alice", limit=1, search=None,
                               min_id=None, max_id=None, topic=None)
        with contextlib.redirect_stdout(io.StringIO()):
            run(tc.cmd_messages(args))
        self.assertIsNotNone(tc._CACHE.entries["666"]["last_used_at"])


class TestDownloadDefault(unittest.TestCase):
    def test_default_download_dir_is_not_cwd(self):
        args = tc.build_parser().parse_args(["download", "x", "--message-id", "1"])
        self.assertIsNone(args.out)
        self.assertNotEqual(tc.DOWNLOAD_DIR.resolve(), Path.cwd().resolve())
        self.assertNotIn(Path.cwd().resolve(), tc.DOWNLOAD_DIR.resolve().parents)


if __name__ == "__main__":
    unittest.main()
