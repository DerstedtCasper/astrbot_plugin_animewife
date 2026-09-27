import asyncio
import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


PLUGIN_DATA = tempfile.TemporaryDirectory(prefix="animewife-tests-")
os.environ["ANIMEWIFE_TEST_DATA_DIR"] = PLUGIN_DATA.name


class _Star:
    def __init__(self, context):
        self.context = context


class _Segment:
    def __init__(self, value=None, **kwargs):
        self.value = value
        self.__dict__.update(kwargs)


class _Image:
    @staticmethod
    def fromFileSystem(path):
        return ("file", path)

    @staticmethod
    def fromURL(url):
        return ("url", url)


def _identity_decorator(*args, **kwargs):
    def decorate(value):
        return value

    return decorate


def _install_astrbot_stubs():
    astrbot = types.ModuleType("astrbot")
    astrbot.__path__ = []
    api = types.ModuleType("astrbot.api")
    api.__path__ = []
    all_api = types.ModuleType("astrbot.api.all")
    all_api.__all__ = [
        "register",
        "Star",
        "Context",
        "AstrBotConfig",
        "AstrMessageEvent",
        "EventMessageType",
        "event_message_type",
        "At",
        "Plain",
        "Image",
    ]
    all_api.register = _identity_decorator
    all_api.Star = _Star
    all_api.Context = type("Context", (), {})
    all_api.AstrBotConfig = dict
    all_api.AstrMessageEvent = type("AstrMessageEvent", (), {})
    all_api.EventMessageType = types.SimpleNamespace(GROUP_MESSAGE="group")
    all_api.event_message_type = _identity_decorator
    all_api.At = _Segment
    all_api.Plain = _Segment
    all_api.Image = _Image

    star_api = types.ModuleType("astrbot.api.star")

    class _StarTools:
        @staticmethod
        def get_data_dir(name):
            return os.environ["ANIMEWIFE_TEST_DATA_DIR"]

    star_api.StarTools = _StarTools
    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.all": all_api,
            "astrbot.api.star": star_api,
        }
    )


_install_astrbot_stubs()
SOURCE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "main.py")
SPEC = importlib.util.spec_from_file_location("animewife_under_test", SOURCE)
animewife = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = animewife
SPEC.loader.exec_module(animewife)


class _FakeResponse:
    def __init__(self, body, status=200):
        self.body = body
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def text(self):
        return self.body


class _FakeSession:
    def __init__(self, responses):
        self.responses = responses
        self.get_calls = 0
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        await self.close()
        return False

    def get(self, url):
        response = self.responses[min(self.get_calls, len(self.responses) - 1)]
        self.get_calls += 1
        return response

    async def close(self):
        self.closed = True


class _FakeEvent:
    def __init__(self, sender_id, message_str, segments, group_id="group-1"):
        self.message_obj = types.SimpleNamespace(group_id=group_id, message=segments)
        self.message_str = message_str
        self._sender_id = sender_id
        self.bot = _FakeBot()

    def get_sender_id(self):
        return self._sender_id

    def get_sender_name(self):
        return "Sender"

    def plain_result(self, text):
        return text

    def chain_result(self, chain):
        return ("chain", chain)


class _FakeBot:
    async def get_group_member_info(self, group_id, user_id):
        return {"card": "Target", "nickname": "Target"}


def _make_plugin(**overrides):
    config = {
        "need_prefix": False,
        "ntr_max": 3,
        "ntr_possibility": 0.5,
        "change_max_per_day": 2,
        "swap_max_per_day": 2,
        "reset_max_uses_per_day": 2,
        "reset_success_rate": 0.5,
        "reset_mute_duration": 60,
        "image_base_url": "https://images.invalid/",
        "image_list_url": "https://images.invalid/list.txt",
        "backpack_size": 3,
    }
    config.update(overrides)
    return animewife.WifePlugin(None, config)


class HttpListTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="animewife-http-tests-")
        animewife.PLUGIN_DIR = self.temp.name
        animewife.CONFIG_DIR = os.path.join(self.temp.name, "config")
        animewife.IMG_DIR = os.path.join(self.temp.name, "img", "wife")
        os.makedirs(animewife.CONFIG_DIR, exist_ok=True)
        os.makedirs(animewife.IMG_DIR, exist_ok=True)

    def tearDown(self):
        self.temp.cleanup()

    async def test_reuses_session_and_list_cache_until_expiry(self):
        sessions = []

        def create_session(**kwargs):
            session = _FakeSession([_FakeResponse("one.jpg\n")])
            sessions.append(session)
            return session

        plugin = _make_plugin()
        with patch.object(animewife.aiohttp, "ClientSession", side_effect=create_session):
            first = await plugin._list_wife_images()
            second = await plugin._list_wife_images()

        self.assertEqual(first, ["one.jpg"])
        self.assertEqual(second, ["one.jpg"])
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].get_calls, 1)
        await plugin.terminate()
        self.assertTrue(sessions[0].closed)

    async def test_failed_refresh_falls_back_to_plugin_data_list(self):
        local_list = Path(animewife.PLUGIN_DIR, "list.txt")
        local_list.write_text("offline-one.jpg\noffline-two.png\n", encoding="utf-8")
        session = _FakeSession([_FakeResponse("unavailable", status=503)])
        plugin = _make_plugin()

        with patch.object(animewife.aiohttp, "ClientSession", return_value=session):
            result = await plugin._list_wife_images()

        self.assertEqual(result, ["offline-one.jpg", "offline-two.png"])
        self.assertEqual(local_list.read_text(encoding="utf-8"), "offline-one.jpg\noffline-two.png\n")

    async def test_empty_remote_list_falls_back_to_plugin_data_list(self):
        Path(animewife.PLUGIN_DIR, "list.txt").write_text(
            "offline.jpg\n", encoding="utf-8"
        )
        session = _FakeSession([_FakeResponse("../invalid.jpg\nhttps://bad/image.jpg\n")])
        plugin = _make_plugin()

        with patch.object(animewife.aiohttp, "ClientSession", return_value=session):
            result = await plugin._list_wife_images()

        self.assertEqual(result, ["offline.jpg"])

    async def test_expired_cache_refreshes_remote_list(self):
        session = _FakeSession(
            [_FakeResponse("first.jpg\n"), _FakeResponse("second.jpg\n")]
        )
        plugin = _make_plugin()

        with patch.object(animewife.aiohttp, "ClientSession", return_value=session):
            self.assertEqual(await plugin._list_wife_images(), ["first.jpg"])
            plugin._wife_list_cache_expires_at = 0
            self.assertEqual(await plugin._list_wife_images(), ["second.jpg"])

        self.assertEqual(session.get_calls, 2)

    async def test_failed_refresh_prefers_stale_cache_over_local_list(self):
        Path(animewife.PLUGIN_DIR, "list.txt").write_text(
            "offline.jpg\n", encoding="utf-8"
        )
        session = _FakeSession(
            [_FakeResponse("cached.jpg\n"), _FakeResponse("unavailable", status=503)]
        )
        plugin = _make_plugin()

        with patch.object(animewife.aiohttp, "ClientSession", return_value=session):
            self.assertEqual(await plugin._list_wife_images(), ["cached.jpg"])
            plugin._wife_list_cache_expires_at = 0
            plugin._wife_list_retry_after = 0
            self.assertEqual(await plugin._list_wife_images(), ["cached.jpg"])

        self.assertEqual(session.get_calls, 2)

    async def test_remote_list_filters_invalid_paths_and_deduplicates(self):
        session = _FakeSession(
            [_FakeResponse("one.jpg\none.jpg\n../bad.jpg\nhttps://bad/img.jpg\ntwo.png\n")]
        )
        plugin = _make_plugin()

        with patch.object(animewife.aiohttp, "ClientSession", return_value=session):
            result = await plugin._list_wife_images()

        self.assertEqual(result, ["one.jpg", "two.png"])

    async def test_local_image_directory_is_last_fallback(self):
        Path(animewife.IMG_DIR, "z-last.png").write_bytes(b"image")
        Path(animewife.IMG_DIR, "a-first.jpg").write_bytes(b"image")
        session = _FakeSession([_FakeResponse("unavailable", status=503)])
        plugin = _make_plugin()

        with patch.object(animewife.aiohttp, "ClientSession", return_value=session):
            result = await plugin._list_wife_images()

        self.assertEqual(result, ["a-first.jpg", "z-last.png"])

    async def test_invalid_utf8_local_list_still_falls_back_to_image_directory(self):
        Path(animewife.PLUGIN_DIR, "list.txt").write_bytes(b"\xff\xfe\xfa")
        Path(animewife.IMG_DIR, "fallback.jpg").write_bytes(b"image")
        session = _FakeSession([_FakeResponse("unavailable", status=503)])
        plugin = _make_plugin()

        with patch.object(animewife.aiohttp, "ClientSession", return_value=session):
            result = await plugin._list_wife_images()

        self.assertEqual(result, ["fallback.jpg"])


class TodaySlotRecoveryTests(unittest.TestCase):
    def test_invalid_record_slot_recovers_from_valid_today_mark(self):
        uid = "user-1"
        today = "2026-09-28"
        cfg = {
            uid: {"date": today, "slot": 0, "nick": "Tester"},
            animewife.BACKPACKS_KEY: {uid: ["old.jpg", "daily.jpg", None]},
            animewife.BACKPACK_TODAY_SLOT_KEY: {
                uid: {"date": today, "slot": 2}
            },
        }

        img, slot, nick, _note, changed = animewife.resolve_today_entity(
            cfg, uid, today, 3, nick_default="Tester"
        )

        self.assertEqual((img, slot, nick), ("daily.jpg", 2, "Tester"))
        self.assertTrue(changed)
        self.assertEqual(cfg[uid], {"date": today, "slot": 2, "nick": "Tester"})
        self.assertEqual(
            cfg[animewife.BACKPACKS_KEY][uid], ["old.jpg", "daily.jpg", None]
        )

    def test_empty_explicit_slot_recovers_from_valid_today_mark(self):
        uid = "user-2"
        today = "2026-09-28"
        original_items = [None, "daily.jpg", "archive.jpg"]
        cfg = {
            uid: {"date": today, "slot": 1, "nick": "Tester"},
            animewife.BACKPACKS_KEY: {uid: original_items.copy()},
            animewife.BACKPACK_TODAY_SLOT_KEY: {
                uid: {"date": today, "slot": 2}
            },
        }

        img, slot, _nick, _note, changed = animewife.resolve_today_entity(
            cfg, uid, today, 3, nick_default="Tester"
        )

        self.assertEqual((img, slot), ("daily.jpg", 2))
        self.assertTrue(changed)
        self.assertEqual(cfg[animewife.BACKPACKS_KEY][uid], original_items)
        self.assertEqual(cfg[uid]["slot"], 2)

    def test_ambiguous_image_match_keeps_daily_wife_unsaved(self):
        uid = "user-3"
        today = "2026-09-28"
        cfg = {
            uid: {"date": today, "img": "daily.jpg", "nick": "Tester"},
            animewife.BACKPACKS_KEY: {uid: ["daily.jpg", "daily.jpg", None]},
        }

        img, slot, _nick, _note, _changed = animewife.resolve_today_entity(
            cfg, uid, today, 3, nick_default="Tester"
        )

        self.assertEqual((img, slot), ("daily.jpg", None))
        self.assertEqual(cfg[uid], {"date": today, "img": "daily.jpg", "nick": "Tester"})
        self.assertNotIn(uid, cfg.get(animewife.BACKPACK_TODAY_SLOT_KEY, {}))
        self.assertEqual(
            cfg[animewife.BACKPACKS_KEY][uid], ["daily.jpg", "daily.jpg", None]
        )


class TargetBackpackQueryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="animewife-query-tests-")
        animewife.PLUGIN_DIR = self.temp.name
        animewife.CONFIG_DIR = os.path.join(self.temp.name, "config")
        animewife.IMG_DIR = os.path.join(self.temp.name, "img", "wife")
        os.makedirs(animewife.CONFIG_DIR, exist_ok=True)
        os.makedirs(animewife.IMG_DIR, exist_ok=True)
        animewife.config_locks.clear()

    def tearDown(self):
        self.temp.cleanup()

    async def test_mentioned_user_query_shows_full_backpack_and_today_slot(self):
        target = "target-1"
        today = "2026-09-28"
        cfg = {
            target: {"date": today, "slot": 2, "nick": "Target"},
            animewife.BACKPACKS_KEY: {
                target: ["archive.jpg", "daily.jpg", {"img": "gift.png", "note": "\u7ba1\u7406\u5458\u8d60\u9001"}]
            },
            animewife.BACKPACK_TODAY_SLOT_KEY: {
                target: {"date": today, "slot": 2}
            },
        }
        animewife.save_group_config("group-1", cfg)
        event = _FakeEvent("sender-1", "\u67e5\u8001\u5a46", [animewife.At(qq=target)])
        plugin = _make_plugin()
        results = []

        with patch.object(animewife, "get_today", return_value=today):
            async for result in plugin.search_wife(event):
                results.append(result)

        rendered = "\n".join(map(str, results))
        self.assertIn("Target\u7684\u8001\u5a46\u80cc\u5305", rendered)
        self.assertIn("1. archive", rendered)
        self.assertIn("2. daily\uff08\u4eca\u65e5\u8001\u5a46\uff09", rendered)
        self.assertIn("3. gift\uff08\u7ba1\u7406\u5458\u8d60\u9001\uff09", rendered)

    async def test_mentioned_user_shows_unsaved_daily_wife(self):
        target = "target-2"
        today = "2026-09-28"
        animewife.save_group_config(
            "group-1",
            {
                target: {"date": today, "img": "unsaved.jpg", "nick": "Target"},
                animewife.BACKPACKS_KEY: {target: [None, None, None]},
            },
        )
        event = _FakeEvent("sender-1", "\u67e5\u8001\u5a46", [animewife.At(qq=target)])
        results = []

        with patch.object(animewife, "get_today", return_value=today):
            async for result in _make_plugin().search_wife(event):
                results.append(result)

        rendered = "\n".join(map(str, results))
        self.assertIn("Target\u7684\u8001\u5a46\u80cc\u5305", rendered)
        self.assertIn("unsaved", rendered)
        self.assertIn("\u672a\u5b58\u5165\u80cc\u5305", rendered)

    async def test_numbered_query_still_reads_senders_slot(self):
        animewife.save_group_config(
            "group-1",
            {animewife.BACKPACKS_KEY: {"sender-1": ["own-slot.jpg", None, None]}},
        )
        event = _FakeEvent("sender-1", "\u67e5\u8001\u5a46 1", [])
        results = []

        async for result in _make_plugin().search_wife(event):
            results.append(result)

        chain = results[0][1]
        self.assertIn("1\u53f7\u8001\u5a46", chain[0].value)
        self.assertIn("own-slot", chain[0].value)

    async def test_no_argument_query_still_reads_senders_daily_wife(self):
        today = "2026-09-28"
        animewife.save_group_config(
            "group-1",
            {
                "sender-1": {"date": today, "slot": 1, "nick": "Sender"},
                animewife.BACKPACKS_KEY: {"sender-1": ["own-daily.jpg", None, None]},
                animewife.BACKPACK_TODAY_SLOT_KEY: {
                    "sender-1": {"date": today, "slot": 1}
                },
            },
        )
        event = _FakeEvent("sender-1", "\u67e5\u8001\u5a46", [])
        results = []

        with patch.object(animewife, "get_today", return_value=today):
            async for result in _make_plugin().search_wife(event):
                results.append(result)

        self.assertIn("own-daily", results[0][1][0].value)


class DailyDrawTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="animewife-draw-tests-")
        animewife.PLUGIN_DIR = self.temp.name
        animewife.CONFIG_DIR = os.path.join(self.temp.name, "config")
        animewife.IMG_DIR = os.path.join(self.temp.name, "img", "wife")
        os.makedirs(animewife.CONFIG_DIR, exist_ok=True)
        os.makedirs(animewife.IMG_DIR, exist_ok=True)
        animewife.config_locks.clear()

    def tearDown(self):
        self.temp.cleanup()

    async def test_draw_reuses_daily_wife_when_explicit_slot_is_corrupt(self):
        today = "2026-09-28"
        uid = "sender-1"
        original_items = ["archive.jpg", "daily.jpg", None]
        animewife.save_group_config(
            "group-1",
            {
                uid: {"date": today, "slot": 0, "nick": "Sender"},
                animewife.BACKPACKS_KEY: {uid: original_items.copy()},
                animewife.BACKPACK_TODAY_SLOT_KEY: {
                    uid: {"date": today, "slot": 2}
                },
            },
        )
        plugin = _make_plugin()
        fetch = AsyncMock(return_value="new-draw.jpg")
        plugin._fetch_wife_image = fetch
        event = _FakeEvent(uid, "\u62bd\u8001\u5a46", [])

        with patch.object(animewife, "get_today", return_value=today):
            async for _result in plugin.animewife(event):
                pass

        stored = animewife.load_group_config("group-1")
        self.assertEqual(fetch.await_count, 0)
        self.assertEqual(stored[animewife.BACKPACKS_KEY][uid], original_items)
        self.assertEqual(stored[uid]["slot"], 2)

    async def test_concurrent_draws_allocate_only_one_today_slot(self):
        today = "2026-09-28"
        uid = "sender-2"
        plugin = _make_plugin()

        async def fetch_image():
            await asyncio.sleep(0)
            return "daily.jpg"

        fetch = AsyncMock(side_effect=fetch_image)
        plugin._fetch_wife_image = fetch
        event = _FakeEvent(uid, "\u62bd\u8001\u5a46", [])

        async def consume():
            async for _result in plugin.animewife(event):
                pass

        with patch.object(animewife, "get_today", return_value=today):
            await asyncio.gather(consume(), consume())

        stored = animewife.load_group_config("group-1")
        items = stored[animewife.BACKPACKS_KEY][uid]
        self.assertEqual(sum(1 for entry in items if entry), 1)
        self.assertEqual(stored[uid]["slot"], 1)


class MutationInvariantTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="animewife-mutation-tests-")
        animewife.PLUGIN_DIR = self.temp.name
        animewife.CONFIG_DIR = os.path.join(self.temp.name, "config")
        animewife.IMG_DIR = os.path.join(self.temp.name, "img", "wife")
        os.makedirs(animewife.CONFIG_DIR, exist_ok=True)
        os.makedirs(animewife.IMG_DIR, exist_ok=True)
        animewife.RECORDS_FILE = os.path.join(animewife.CONFIG_DIR, "records.json")
        animewife.SWAP_REQUESTS_FILE = os.path.join(animewife.CONFIG_DIR, "swap_requests.json")
        animewife.NTR_STATUS_FILE = os.path.join(animewife.CONFIG_DIR, "ntr_status.json")
        animewife.config_locks.clear()
        animewife.records = {"ntr": {}, "change": {}, "reset": {}, "swap": {}}
        animewife.swap_requests.clear()
        animewife.ntr_statuses.clear()

    def tearDown(self):
        self.temp.cleanup()

    async def test_replace_moves_today_entity_and_updates_mark(self):
        today = "2026-09-28"
        uid = "111"
        animewife.save_group_config(
            "123",
            {
                uid: {"date": today, "slot": 1, "nick": "Sender"},
                animewife.BACKPACKS_KEY: {uid: ["daily.jpg", "archive.jpg", None]},
                animewife.BACKPACK_TODAY_SLOT_KEY: {
                    uid: {"date": today, "slot": 1}
                },
            },
        )
        event = _FakeEvent(uid, "\u66ff\u6362\u8001\u5a46 3", [], group_id="123")

        with patch.object(animewife, "get_today", return_value=today):
            async for _result in _make_plugin().replace_wife(event):
                pass

        cfg = animewife.load_group_config("123")
        self.assertEqual(cfg[uid]["slot"], 3)
        self.assertEqual(cfg[animewife.BACKPACK_TODAY_SLOT_KEY][uid]["slot"], 3)
        self.assertEqual(cfg[animewife.BACKPACKS_KEY][uid], [None, "archive.jpg", "daily.jpg"])

    async def test_change_replaces_today_slot_without_growing_backpack(self):
        today = "2026-09-28"
        uid = "111"
        animewife.save_group_config(
            "123",
            {
                uid: {"date": today, "slot": 2, "nick": "Sender"},
                animewife.BACKPACKS_KEY: {uid: ["archive.jpg", "daily.jpg", None]},
                animewife.BACKPACK_TODAY_SLOT_KEY: {
                    uid: {"date": today, "slot": 2}
                },
            },
        )
        plugin = _make_plugin()
        plugin._fetch_wife_image = AsyncMock(return_value="changed.jpg")
        plugin.cancel_swap_on_wife_change = AsyncMock(return_value=None)
        event = _FakeEvent(uid, "\u6362\u8001\u5a46", [], group_id="123")

        with patch.object(animewife, "get_today", return_value=today):
            async for _result in plugin.change_wife(event):
                pass

        cfg = animewife.load_group_config("123")
        self.assertEqual(cfg[uid]["slot"], 2)
        self.assertEqual(cfg[animewife.BACKPACK_TODAY_SLOT_KEY][uid]["slot"], 2)
        self.assertEqual(cfg[animewife.BACKPACKS_KEY][uid], ["archive.jpg", "changed.jpg", None])

    async def test_send_overwrites_existing_daily_slot(self):
        today = "2026-09-28"
        admin, target = "111", "222"
        animewife.save_group_config(
            "123",
            {
                target: {"date": today, "slot": 2, "nick": "Target"},
                animewife.BACKPACKS_KEY: {target: ["archive.jpg", "daily.jpg", None]},
                animewife.BACKPACK_TODAY_SLOT_KEY: {
                    target: {"date": today, "slot": 2}
                },
            },
        )
        plugin = _make_plugin()
        plugin.admins = [admin]
        plugin._list_wife_images = AsyncMock(return_value=["anime!test.jpg"])
        plugin.cancel_swap_on_wife_change = AsyncMock(return_value=None)
        event = _FakeEvent(
            admin,
            "\u53d1\u8001\u5a46 @222 test",
            [animewife.At(qq=target)],
            group_id="123",
        )

        with patch.object(animewife, "get_today", return_value=today):
            async for _result in plugin.send_wife(event):
                pass

        cfg = animewife.load_group_config("123")
        self.assertEqual(cfg[target]["slot"], 2)
        self.assertEqual(cfg[animewife.BACKPACK_TODAY_SLOT_KEY][target]["slot"], 2)
        self.assertEqual(cfg[animewife.BACKPACKS_KEY][target], ["archive.jpg", "anime!test.jpg", None])

    async def test_ntr_transfer_clears_victim_daily_binding(self):
        today = "2026-09-28"
        thief, target = "111", "222"
        animewife.save_group_config(
            "123",
            {
                target: {"date": today, "slot": 2, "nick": "Target"},
                animewife.BACKPACKS_KEY: {
                    thief: [None, None, None],
                    target: ["archive.jpg", "daily.jpg", None],
                },
                animewife.BACKPACK_TODAY_SLOT_KEY: {
                    target: {"date": today, "slot": 2}
                },
            },
        )
        plugin = _make_plugin()
        plugin.ntr_possibility = 1.0
        event = _FakeEvent(
            thief,
            "\u725b\u8001\u5a46 @222",
            [animewife.At(qq=target)],
            group_id="123",
        )

        with patch.object(animewife, "get_today", return_value=today), patch.object(
            animewife.random, "random", return_value=0.0
        ):
            async for _result in plugin.ntr_wife(event):
                pass

        cfg = animewife.load_group_config("123")
        self.assertNotIn(target, cfg)
        self.assertNotIn(target, cfg[animewife.BACKPACK_TODAY_SLOT_KEY])
        self.assertIsNone(cfg[animewife.BACKPACKS_KEY][target][1])
        stolen_img, _note = animewife.backpack_entry_to_img_note(
            cfg[animewife.BACKPACKS_KEY][thief][0]
        )
        self.assertEqual(stolen_img, "daily.jpg")

    async def test_swap_keeps_each_users_daily_slot_canonical(self):
        today = "2026-09-28"
        user_a, user_b = "111", "222"
        animewife.save_group_config(
            "123",
            {
                user_a: {"date": today, "slot": 1, "nick": "A"},
                user_b: {"date": today, "slot": 3, "nick": "B"},
                animewife.BACKPACKS_KEY: {
                    user_a: ["a-wife.jpg", "a-archive.jpg", None],
                    user_b: ["b-archive.jpg", None, "b-wife.jpg"],
                },
                animewife.BACKPACK_TODAY_SLOT_KEY: {
                    user_a: {"date": today, "slot": 1},
                    user_b: {"date": today, "slot": 3},
                },
            },
        )
        animewife.swap_requests["123"] = {
            user_a: {"target": user_b, "date": today}
        }
        event = _FakeEvent(
            user_b,
            "\u540c\u610f\u4ea4\u6362\u8001\u5a46 @111",
            [animewife.At(qq=user_a)],
            group_id="123",
        )

        with patch.object(animewife, "get_today", return_value=today):
            async for _result in _make_plugin().agree_swap_wife(event):
                pass

        cfg = animewife.load_group_config("123")
        self.assertEqual(cfg[user_a]["slot"], 1)
        self.assertEqual(cfg[user_b]["slot"], 3)
        self.assertEqual(cfg[animewife.BACKPACK_TODAY_SLOT_KEY][user_a]["slot"], 1)
        self.assertEqual(cfg[animewife.BACKPACK_TODAY_SLOT_KEY][user_b]["slot"], 3)
        self.assertEqual(cfg[animewife.BACKPACKS_KEY][user_a], ["b-wife.jpg", "a-archive.jpg", None])
        self.assertEqual(cfg[animewife.BACKPACKS_KEY][user_b], ["b-archive.jpg", None, "a-wife.jpg"])


if __name__ == "__main__":
    unittest.main()
