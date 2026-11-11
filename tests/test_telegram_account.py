import asyncio
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import io
import json
import logging
import os
from pathlib import Path
import ssl
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.sdk.telegram_account import TelegramAccount, _TLSProxyConnection, _proxy, REASONS
from telethon import functions, types
from telethon.crypto.authkey import AuthKey
from telethon.sessions import StringSession


def rpc_error(name, seconds=None):
    error = type(name, (Exception,), {})("SECRET_PHONE SECRET_HASH SECRET_PASSWORD")
    if seconds is not None:
        error.seconds = seconds
    return error


def channel(name="FilmChannel", **kwargs):
    return types.Channel(id=kwargs.pop("id", 1), title="影视资源", photo=types.ChatPhotoEmpty(),
                         date=None, broadcast=kwargs.pop("broadcast", True), username=name, **kwargs)


def post(text, message_id=12, entities=None):
    return SimpleNamespace(message=text, id=message_id, entities=entities or [])


class Driver:
    def __init__(self):
        self.calls = []
        self.authorized = True
        self.error = {}
        self.messages = defaultdict(list)
        self.entities = {}
        self.folder = SimpleNamespace(peers=[types.PeerChannel(1)], chats=[channel()])
        self.qr_responses = []
        self.handlers = []
        self.discovery = SimpleNamespace(chats=[channel()])

    def emit_login(self):
        for client, handler in list(self.handlers):
            client.loop.call_soon_threadsafe(lambda h=handler: asyncio.create_task(h(types.UpdateLoginToken())))

    def client(self, session, api_id, api_hash, **options):
        self.calls.append(("client", options))
        driver = self

        class Client:
            def __init__(self):
                self.session = session
                session.set_dc(2, "149.154.167.51", 443)
                session.auth_key = AuthKey(bytes(range(256)))

            async def connect(self):
                self.loop = asyncio.get_running_loop()
                driver.calls.append(("connect",))
                if "connect" in driver.error:
                    raise driver.error["connect"]
                if "connect_wait" in driver.error:
                    await asyncio.sleep(driver.error["connect_wait"])

            async def disconnect(self):
                driver.calls.append(("disconnect",))
                if "disconnect_wait" in driver.error:
                    await asyncio.sleep(driver.error["disconnect_wait"])

            async def is_user_authorized(self):
                return driver.authorized

            async def send_code_request(self, phone):
                driver.calls.append(("send_code",))
                if "send_code" in driver.error:
                    raise driver.error["send_code"]
                return SimpleNamespace(phone_code_hash="private-code-hash")

            async def sign_in(self, **kwargs):
                driver.calls.append(("sign_in", sorted(kwargs)))
                if "sign_in" in driver.error:
                    raise driver.error["sign_in"]

            async def log_out(self):
                driver.calls.append(("log_out",))
                if "log_out" in driver.error:
                    raise driver.error["log_out"]
                return True

            async def __call__(self, request):
                driver.calls.append(("request", type(request).__name__))
                if "request" in driver.error:
                    raise driver.error["request"]
                if isinstance(request, functions.chatlists.CheckChatlistInviteRequest):
                    return driver.folder
                if isinstance(request, (functions.auth.ExportLoginTokenRequest, functions.auth.ImportLoginTokenRequest)):
                    value = driver.qr_responses.pop(0) if driver.qr_responses else types.auth.LoginToken(
                        expires=datetime.now(timezone.utc) + timedelta(seconds=30), token=b"private-qr-token")
                    if isinstance(value, Exception):
                        raise value
                    if callable(value):
                        return await value()
                    return value
                if isinstance(request, functions.contacts.SearchRequest):
                    driver.calls.append(("discover_query", request.q))
                    return driver.discovery
                if isinstance(request, functions.channels.GetChannelRecommendationsRequest):
                    assert request.channel is not None
                    return driver.discovery
                if isinstance(request, functions.channels.JoinChannelRequest):
                    return SimpleNamespace()
                raise AssertionError(type(request).__name__)

            def add_event_handler(self, handler, event):
                driver.handlers.append((self, handler))

            def remove_event_handler(self, handler):
                driver.handlers[:] = [item for item in driver.handlers if item[1] is not handler]

            async def _switch_dc(self, dc):
                driver.calls.append(("switch_dc", dc))
                self.session.set_dc(dc, "149.154.167.51", 443)

            async def _on_login(self, user):
                driver.calls.append(("on_login",))

            async def get_entity(self, name):
                driver.calls.append(("entity", name))
                result = driver.entities.get(name, channel(name))
                if isinstance(result, Exception):
                    raise result
                return result

            async def iter_messages(self, entity, **kwargs):
                driver.calls.append(("messages", entity.username, kwargs))
                for item in driver.messages[entity.username]:
                    if isinstance(item, Exception):
                        raise item
                    if callable(item):
                        await item()
                    else:
                        yield item

        return Client()


class TelegramAccountTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = str(Path(folder.name) / "health.sqlite")
        self.driver = Driver()
        self.account = TelegramAccount(self.path, client_factory=self.driver.client)
        self.api_hash = "a" * 32
        self.phone = "+12345678901"
        self.addCleanup(self.stop_workers)

    def stop_workers(self):
        self.account._stop_qr()
        for thread in threading.enumerate():
            if thread.name == "telegram-qr-login":
                thread.join(timeout=3)

    def wait_for(self, predicate, timeout=2):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            value = predicate()
            if value:
                return value
            time.sleep(0.01)
        self.fail("Background mock operation did not complete within its bounded wait")

    def start_qr(self):
        result = self.account.request_qr(12345, self.api_hash)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["state"], "waiting_qr")
        return result["qr_id"]

    def qr_success(self):
        return types.auth.LoginTokenSuccess(authorization=types.auth.Authorization(user=types.User(id=42)))

    def pending(self):
        result = self.account.request_code(12345, self.api_hash, self.phone)
        self.assertTrue(result["ok"], result)
        return result

    def authorized(self):
        self.pending()
        result = self.account.sign_in(code="12345")
        self.assertTrue(result["authorized"], result)
        self.driver.calls.clear()
        return result

    def test_missing_sdk_and_no_path_do_not_connect(self):
        with patch("app.sdk.telegram_account.TelegramClient", None):
            self.assertFalse(self.account.status()["available"])
            result = self.account.request_code(12345, self.api_hash, self.phone)
            self.assertEqual(result["code"], "SDK_MISSING")
        result = TelegramAccount(client_factory=self.driver.client).request_code(12345, self.api_hash, self.phone)
        self.assertEqual(result["code"], "DISABLED")
        self.assertEqual(self.driver.calls, [])
        self.assertFalse(self.account.path.exists())

    def test_login_is_explicit_status_is_redacted_and_success_removes_pending(self):
        pending = self.pending()
        self.assertEqual(pending["state"], "waiting_code")
        stored = json.loads(self.account.path.read_text())
        self.assertEqual(stored["phone_code_hash"], "private-code-hash")
        result = self.account.sign_in(code="12345")
        self.assertTrue(result["authorized"])
        stored = json.loads(self.account.path.read_text())
        self.assertFalse({"phone", "phone_code_hash", "expires", "code", "password"} & stored.keys())
        public = json.dumps([pending, result, self.account.status()])
        for secret in (self.api_hash, self.phone, stored["session"], "private-code-hash", "12345"):
            self.assertNotIn(secret, public)
        self.assertEqual(sum(call[0] == "send_code" for call in self.driver.calls), 1)
        options = next(call[1] for call in self.driver.calls if call[0] == "client")
        self.assertFalse(options["receive_updates"])
        self.assertFalse(options["auto_reconnect"])
        self.assertEqual(options["flood_sleep_threshold"], 0)

    def test_two_factor_password_is_never_persisted(self):
        self.pending()
        self.driver.error["sign_in"] = rpc_error("SessionPasswordNeededError")
        result = self.account.sign_in(code="12345")
        self.assertEqual(result["state"], "waiting_password")
        self.driver.error.clear()
        result = self.account.sign_in(password="VERY-PRIVATE-2FA-PASSWORD")
        self.assertTrue(result["authorized"])
        self.assertNotIn("VERY-PRIVATE-2FA-PASSWORD", self.account.path.read_text())
        self.assertNotIn("12345", self.account.path.read_text().replace('"api_id": 12345', ""))

    def test_expired_pending_is_removed_without_network_or_automatic_resend(self):
        self.pending()
        stored = json.loads(self.account.path.read_text())
        stored["expires"] = time.time() - 1
        self.account._save(stored)
        self.driver.calls.clear()
        result = self.account.sign_in(code="12345")
        self.assertEqual(result["code"], "CODE_EXPIRED")
        self.assertEqual(json.loads(self.account.path.read_text()), {})
        self.assertEqual(self.driver.calls, [])

    def test_invalid_code_and_password_are_sanitized_and_retryable(self):
        self.pending()
        for name, expected in (("PhoneCodeInvalidError", "CODE_INVALID"), ("PasswordHashInvalidError", "PASSWORD_INVALID")):
            self.driver.error["sign_in"] = rpc_error(name)
            result = self.account.sign_in(code="12345")
            self.assertEqual(result["code"], expected)
            self.assertEqual(result["state"], "waiting_code")
            self.assertNotIn("SECRET", json.dumps(result))
            self.assertNotIn("SECRET", self.account.path.read_text())

    def test_bad_auth_and_proxy_inputs_do_not_send_codes(self):
        for values in ((True, self.api_hash, self.phone), (0, self.api_hash, self.phone),
                       (1, "not-hex", self.phone), (1, self.api_hash, "not-phone")):
            self.assertEqual(self.account.request_code(*values)["code"], "INVALID_INPUT")
        for proxy in ("ftp://example.com", "http://example.com/x", "http://example.com:0", "http://example.com/?secret=yes"):
            self.assertEqual(self.account.request_code(1, self.api_hash, self.phone, proxy)["code"], "INVALID_INPUT")
        self.assertEqual(self.driver.calls, [])
        self.assertEqual(self.account.sign_in()["code"], "INVALID_INPUT")
        self.assertEqual(self.account.sign_in(code="12345", password="pwd")["code"], "INVALID_INPUT")
        self.assertEqual(self.account.sign_in(code="12345")["code"], "AUTH_REQUIRED")

    def test_proxy_schemes_remain_explicit_and_https_keeps_tls(self):
        for scheme, expected in (("http", "http"), ("https", "https"), ("socks5", "socks5"), ("socks5h", "socks5"), ("socks4a", "socks4")):
            proxy = _proxy(f"{scheme}://user:p%40ss@127.0.0.1:7890")
            self.assertEqual(proxy["proxy_type"], expected)
            self.assertEqual(proxy["password"], "p@ss")
        self.account.request_code(1, self.api_hash, self.phone, "https://127.0.0.1:7890")
        self.assertIs(self.driver.calls[0][1]["connection"], _TLSProxyConnection)
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://secret:password@proxy.invalid:7890"}):
            self.assertIsNone(_proxy(None))

    def test_https_proxy_verifies_certificate_and_uses_tls_connect(self):
        connection = object.__new__(_TLSProxyConnection)
        connection._proxy = {"addr": "proxy.example", "port": 8443, "username": "user", "password": "password"}
        connection._ip, connection._port = "149.154.167.51", 443
        connection.packet_codec = MagicMock()
        connection._init_conn = MagicMock()
        stream = SimpleNamespace(reader=object(), writer=SimpleNamespace(drain=AsyncMock()))
        proxy = MagicMock()
        proxy.return_value.connect = AsyncMock(return_value=stream)
        with patch("app.sdk.telegram_account.Proxy", proxy):
            asyncio.run(connection._connect(timeout=4))
        context = proxy.call_args.kwargs["proxy_ssl"]
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        proxy.return_value.connect.assert_awaited_once_with("149.154.167.51", 443, timeout=4)
        self.assertIs(connection._reader, stream.reader)

    def test_real_telethon_child_logger_cannot_leak_private_transport_to_root(self):
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        root = logging.getLogger()
        root.addHandler(handler)
        self.addCleanup(root.removeHandler, handler)
        async def construct_offline():
            account = TelegramAccount(self.path)
            client = account._make_client({"api_id": 12345, "api_hash": self.api_hash,
                                           "proxy": "http://private-user:private-pass@proxy.example:7890"})
            child = client._log["telethon.network.connection.connection"]
            self.assertIs(child.parent, logging.getLogger("telegram_account.transport"))
            self.assertFalse(child.parent.propagate)
            child.warning("SECRET_PROXY private-user private-pass SECRET_SESSION")
            return child
        child = asyncio.run(construct_offline())
        self.assertEqual(output.getvalue(), "")
        # Repeated construction must not add handlers or change the child's parent.
        parent = child.parent
        count = len(parent.handlers)
        asyncio.run(construct_offline())
        self.assertEqual(len(parent.handlers), count)
        self.assertEqual(output.getvalue(), "")

    def test_read_folder_only_returns_public_unrestricted_broadcast_channels(self):
        self.authorized()
        self.driver.folder = SimpleNamespace(peers=[types.PeerChannel(i) for i in range(1, 8)], chats=[
            channel(), channel(None, id=2), channel("Discussion", id=3, broadcast=False, megagroup=True),
            channel("Restricted", id=4, restricted=True), channel("ScamChannel", id=5, scam=True),
            types.ChannelForbidden(id=6, access_hash=1, title="forbidden"), channel("SomeBot", id=7),
            channel("UnrelatedExtra", id=999)])
        result = self.account.read_folder("https://t.me/addlist/FictionalFolderSlug123")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["channels"], [{"name": "FilmChannel", "title": "影视资源"}])
        self.assertEqual(len(result["skipped"]), 6)
        self.assertEqual([call[1] for call in self.driver.calls if call[0] == "request"], ["CheckChatlistInviteRequest"])

    def test_already_known_folder_merges_peers_and_deduplicates_names(self):
        self.authorized()
        self.driver.folder = SimpleNamespace(missing_peers=[types.PeerChannel(1)], already_peers=[types.PeerChannel(2)],
                                             chats=[channel(), channel("OtherChannel", id=2), channel("FilmChannel")])
        result = self.account.read_folder("https://t.me/addlist/FictionalFolderSlug123")
        self.assertEqual([item["name"] for item in result["channels"]], ["FilmChannel", "OtherChannel"])

    def test_invalid_folder_and_search_inputs_never_connect(self):
        for url in ("https://evil.test/addlist/FictionalFolderSlug123", "https://t.me/addlist/FictionalFolderSlug123?token=x", "http://t.me/addlist/FictionalFolderSlug123"):
            self.assertEqual(self.account.read_folder(url)["code"], "FOLDER_INVALID")
        for channels in (["../private"], ["https://t.me/FilmChannel"], ["bot?query=x"]):
            self.assertEqual(self.account.search("影视", channels)["code"], "INVALID_INPUT")
        self.assertEqual(self.driver.calls, [])

    def test_search_extracts_actual_quark_links_and_message_titles_not_query(self):
        self.authorized()
        self.driver.messages["FilmChannel"] = [
            post("名称：庆余年 第二季\n类型：电视剧\nhttps://pan.quark.cn/s/abcdef123456 提取码：1234"),
            post("名称：另一部动画\n公开正文", 13, [types.MessageEntityTextUrl(0, 4, "https://pan.quark.cn/s/0123456789ab")]),
            post("伪夸克\nhttps://pan.qoark.cn/s/abc"),
            post("重复\nhttps://pan.quark.cn/s/abcdef123456?pwd=1234"),
        ]
        result = self.account.search("搜索词不是可信片名", ["FilmChannel"])
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(result["candidates"]), 2)
        item = result["candidates"][0]
        self.assertEqual(item["taskname"], "名称：庆余年 第二季")
        self.assertEqual(item["shareurl"], "https://pan.quark.cn/s/abcdef123456?pwd=1234")
        self.assertEqual(item["source_url"], "https://t.me/FilmChannel/12")
        self.assertIn("电视剧", item["content"])
        self.assertFalse(any(call[0] in {"send_code", "request", "sign_in"} for call in self.driver.calls))

    def test_private_and_restricted_channels_are_skipped_without_joining(self):
        self.authorized()
        self.driver.entities["PrivateChan"] = rpc_error("ChannelPrivateError")
        self.driver.entities["BannedChan"] = channel("BannedChan", restricted=True)
        self.driver.messages["GoodChannel"] = [post("测试动画\nhttps://pan.quark.cn/s/abcdef123456")]
        result = self.account.search("测试", ["PrivateChan", "BannedChan", "GoodChannel"])
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual([item["code"] for item in result["errors"]], ["CHANNEL_UNAVAILABLE", "CHANNEL_RESTRICTED"])
        self.assertEqual([item["channel"] for item in result["errors"]], ["PrivateChan", "BannedChan"])
        self.assertEqual([item["name"] for item in result["unavailable_channels"]], ["PrivateChan", "BannedChan"])
        self.assertEqual([item["retry_after"] for item in result["unavailable_channels"]], [3600, 86400])
        self.assertTrue(result["authorized"])
        self.assertEqual(self.account.status()["retry_after"], 0)

    def test_floodwait_preserves_partial_results_and_blocks_later_processes(self):
        self.authorized()
        self.driver.messages["FilmChannel"] = [post("测试动画\nhttps://pan.quark.cn/s/abcdef123456"), rpc_error("FloodWaitError", 120)]
        result = self.account.search("测试", ["FilmChannel"])
        self.assertEqual(result["code"], "FLOOD_WAIT")
        self.assertEqual(result["retry_after"], 120)
        self.assertEqual(len(result["candidates"]), 1)
        self.driver.calls.clear()
        second = TelegramAccount(self.path, client_factory=self.driver.client)
        self.assertEqual(second.search("测试", ["FilmChannel"])["code"], "FLOOD_WAIT")
        self.assertEqual(self.driver.calls, [])

    def test_revoked_session_is_cleared_and_never_resends_login(self):
        self.authorized()
        self.driver.authorized = False
        result = self.account.search("测试", ["FilmChannel"])
        self.assertEqual(result["code"], "ACCOUNT_REVOKED")
        self.assertEqual(result["state"], "reauthorize")
        self.assertNotIn("session", json.loads(self.account.path.read_text()))
        self.driver.calls.clear()
        self.assertEqual(self.account.search("测试", ["FilmChannel"])["code"], "AUTH_REQUIRED")
        self.assertEqual(self.driver.calls, [])

    def test_banned_account_is_suspended_without_retaining_login_secrets(self):
        self.driver.error["send_code"] = rpc_error("PhoneNumberBannedError")
        result = self.account.request_code(12345, self.api_hash, self.phone)
        self.assertEqual(result["code"], "ACCOUNT_BANNED")
        self.assertEqual(result["state"], "suspended")
        stored = json.loads(self.account.path.read_text())
        self.assertFalse({"session", "api_id", "api_hash", "phone", "phone_code_hash"} & stored.keys())
        self.driver.calls.clear()
        self.assertEqual(self.account.search("测试", ["FilmChannel"])["code"], "AUTH_REQUIRED")
        self.assertEqual(self.driver.calls, [])

    def test_deadline_and_limit_bound_reads_and_keep_partial_results(self):
        self.authorized()
        self.driver.messages["FilmChannel"] = [post("测试动画\nhttps://pan.quark.cn/s/abcdef123456"), lambda: asyncio.sleep(2)]
        started = time.monotonic()
        result = self.account.search("测试", ["FilmChannel"], deadline=started + 0.08)
        self.assertEqual(result["code"], "TIMEOUT")
        self.assertLess(time.monotonic() - started, 0.8)
        self.assertEqual(len(result["candidates"]), 1)
        self.driver.calls.clear()
        result = self.account.search("测试", ["FilmChannel", "OtherChannel"], limit=1)
        self.assertEqual(len(result["candidates"]), 1)
        self.assertNotIn(("entity", "OtherChannel"), self.driver.calls)

    def test_session_lock_prevents_concurrent_reads_or_logins(self):
        self.authorized()
        second = TelegramAccount(self.path, client_factory=self.driver.client)
        with self.account._locked():
            result = second.search("测试", ["FilmChannel"])
        self.assertEqual(result["code"], "BUSY")
        self.assertEqual(self.driver.calls, [])

    def test_deadline_includes_slow_connect_and_disconnect(self):
        self.authorized()
        self.driver.error.update(connect_wait=2, disconnect_wait=2)
        started = time.monotonic()
        result = self.account.search("测试", ["FilmChannel"], deadline=started + 0.08)
        self.assertEqual(result["code"], "TIMEOUT")
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertFalse(any(call[0] == "messages" for call in self.driver.calls))

    def test_logout_clears_local_secrets_even_when_remote_is_unreachable(self):
        self.authorized()
        self.driver.error["connect"] = OSError("private proxy password")
        result = self.account.logout()
        self.assertTrue(result["ok"])
        self.assertFalse(result["authorized"])
        self.assertEqual(result["state"], "disconnected")
        self.assertEqual(result["code"], "LOGOUT_UNCONFIRMED")
        self.assertEqual(json.loads(self.account.path.read_text()), {})
        self.driver.calls.clear()
        self.assertEqual(self.account.search("测试", ["FilmChannel"])["code"], "AUTH_REQUIRED")
        self.assertEqual(self.driver.calls, [])

    def test_logout_revokes_only_this_session(self):
        self.authorized()
        result = self.account.logout()
        self.assertTrue(result["ok"])
        self.assertEqual([call for call in self.driver.calls if call[0] == "log_out"], [("log_out",)])
        self.assertFalse(result["authorized"])

    def test_corrupt_storage_is_reported_without_leaking_contents(self):
        self.account.path.write_text("{not json SECRET}", encoding="utf-8")
        self.assertEqual(self.account.status()["reason"], REASONS["STORAGE_ERROR"])
        result = self.account.search("测试", ["FilmChannel"])
        self.assertEqual(result["code"], "STORAGE_ERROR")
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertEqual(self.driver.calls, [])

    def test_qr_request_is_nonblocking_poll_is_local_and_status_has_no_qr_secrets(self):
        started = time.monotonic()
        qr_id = self.start_qr()
        self.assertLess(time.monotonic() - started, 0.3)
        result = self.wait_for(lambda: (value if (value := self.account.poll_qr(qr_id)).get("qr_url") else None))
        self.assertTrue(result["qr_url"].startswith("tg://login?token="))
        self.assertGreater(result["expires_at"], time.time())
        before = list(self.driver.calls)
        for _ in range(5):
            self.account.poll_qr(qr_id)
            self.account.status()
        self.assertEqual(self.driver.calls, before)
        public = json.dumps(self.account.status())
        for secret in (qr_id, "qr_url", "qr_id", "private-qr-token", self.api_hash):
            self.assertNotIn(secret, public)
        self.assertTrue(self.account.status()["credentials_saved"])
        self.assertTrue(self.driver.calls[0][1]["receive_updates"])
        self.assertFalse(any(call[0] == "send_code" for call in self.driver.calls))

    def test_qr_requires_own_app_credentials_and_can_reuse_private_saved_ones(self):
        self.assertEqual(self.account.request_qr()["code"], "API_CREDENTIALS_REQUIRED")
        self.assertEqual(self.driver.calls, [])
        self.account._save({"api_id": 12345, "api_hash": self.api_hash})
        result = self.account.request_qr()
        self.assertTrue(result["ok"])
        self.assertEqual(result["state"], "waiting_qr")
        self.assertNotIn(self.api_hash, json.dumps(result))

    def test_qr_update_confirms_login_and_changes_authorization_generation(self):
        qr_id = self.start_qr()
        self.wait_for(lambda: self.account.poll_qr(qr_id).get("qr_url"))
        self.driver.qr_responses.append(self.qr_success())
        self.driver.emit_login()
        result = self.wait_for(lambda: (value if (value := self.account.poll_qr(qr_id)).get("authorized") else None))
        self.assertNotIn("qr_url", result)
        self.assertNotIn("qr_id", self.account.status())
        generation = self.account.authorization_generation()
        self.assertRegex(generation, r"^[a-f0-9]{32}$")
        self.assertEqual(TelegramAccount(self.path).authorization_generation(), generation)
        self.assertNotIn(generation, json.dumps(self.account.status()))
        stored = json.loads(self.account.path.read_text())
        self.assertFalse({"qr_url", "qr_id", "qr_owner", "qr_expires"} & stored.keys())

    def test_qr_dc_migration_imports_before_publishing_authorized(self):
        self.driver.qr_responses = [types.auth.LoginTokenMigrateTo(dc_id=4, token=b"private-migration-token"), self.qr_success()]
        qr_id = self.start_qr()
        self.wait_for(lambda: self.account.poll_qr(qr_id).get("authorized"))
        calls = [call for call in self.driver.calls if call[0] in {"request", "switch_dc", "on_login"}]
        self.assertEqual(calls, [("request", "ExportLoginTokenRequest"), ("switch_dc", 4),
                                 ("request", "ImportLoginTokenRequest"), ("on_login",)])
        self.assertEqual(StringSession(json.loads(self.account.path.read_text())["session"]).dc_id, 4)

    def test_qr_two_factor_releases_worker_and_uses_existing_password_flow(self):
        self.driver.qr_responses = [rpc_error("SessionPasswordNeededError")]
        qr_id = self.start_qr()
        self.wait_for(lambda: self.account.poll_qr(qr_id).get("state") == "waiting_password")
        self.wait_for(lambda: not self.driver.handlers)
        result = self.account.sign_in(password="private-qr-password")
        self.assertTrue(result["authorized"], result)
        self.assertNotIn("private-qr-password", self.account.path.read_text())
        self.assertNotIn("phone_code_hash", self.account.path.read_text())
        self.assertFalse(any(call[0] == "send_code" for call in self.driver.calls))

    def test_old_code_submission_during_qr_wait_does_not_connect_or_block_confirmation(self):
        qr_id = self.start_qr()
        self.wait_for(lambda: self.account.poll_qr(qr_id).get("qr_url"))
        self.driver.error["connect_wait"] = 1.2
        before = sum(call[0] == "connect" for call in self.driver.calls)
        started = time.monotonic()
        result = self.account.sign_in(code="12345")
        self.assertEqual(result["code"], "AUTH_REQUIRED")
        self.assertLess(time.monotonic() - started, 0.2)
        self.assertEqual(sum(call[0] == "connect" for call in self.driver.calls), before)
        self.driver.qr_responses.append(self.qr_success())
        self.driver.emit_login()
        self.wait_for(lambda: self.account.poll_qr(qr_id).get("authorized"))
        self.assertFalse(any(call[0] == "log_out" for call in self.driver.calls))

    def test_qr_expiry_refreshes_token_within_same_bounded_flow(self):
        self.driver.qr_responses = [types.auth.LoginToken(expires=datetime.now(timezone.utc) + timedelta(seconds=0.08), token=b"first-token"),
                                    types.auth.LoginToken(expires=datetime.now(timezone.utc) + timedelta(seconds=30), token=b"second-token")]
        qr_id = self.start_qr()
        result = self.wait_for(lambda: (value if "c2Vjb25kLXRva2Vu" in (value := self.account.poll_qr(qr_id)).get("qr_url", "") else None))
        self.assertEqual(result["qr_id"], qr_id)
        self.assertEqual(sum(call == ("request", "ExportLoginTokenRequest") for call in self.driver.calls), 2)

    def test_qr_flow_timeout_removes_token_but_retains_only_reusable_app_credentials(self):
        with patch("app.sdk.telegram_account.QR_TTL", 0.08):
            qr_id = self.start_qr()
        result = self.wait_for(lambda: (value if (value := self.account.poll_qr(qr_id)).get("code") == "QR_EXPIRED" else None))
        self.assertFalse(result["ok"])
        self.assertTrue(result["credentials_saved"])
        self.wait_for(lambda: not self.driver.handlers)
        data = json.loads(self.account.path.read_text())
        self.assertFalse({"session", "qr_url", "qr_id", "qr_owner", "qr_expires"} & data.keys())

    def test_qr_flood_wait_is_persistent_and_does_not_automatically_send_sms(self):
        self.driver.qr_responses = [rpc_error("FloodWaitError", 120)]
        qr_id = self.start_qr()
        result = self.wait_for(lambda: (value if (value := self.account.poll_qr(qr_id)).get("code") == "FLOOD_WAIT" else None))
        self.assertEqual(result["retry_after"], 120)
        before = list(self.driver.calls)
        self.assertEqual(self.account.request_qr()["code"], "FLOOD_WAIT")
        self.assertEqual(self.driver.calls, before)
        self.assertFalse(any(call[0] == "send_code" for call in self.driver.calls))

    def test_old_qr_poll_cannot_read_new_qr_and_old_worker_cannot_resurrect_after_logout(self):
        first = self.start_qr()
        self.wait_for(lambda: self.account.poll_qr(first).get("qr_url"))
        second = self.account.request_qr()["qr_id"]
        self.assertNotEqual(first, second)
        self.assertEqual(self.account.poll_qr(first)["code"], "QR_EXPIRED")
        self.wait_for(lambda: self.account.poll_qr(second).get("qr_url"))
        result = self.account.logout()
        self.assertTrue(result["ok"])
        self.stop_workers()
        self.assertEqual(json.loads(self.account.path.read_text()), {})
        self.assertFalse(self.account.status()["authorized"])
        self.assertEqual(self.account.poll_qr(second)["code"], "QR_EXPIRED")

    def test_sms_login_replaces_qr_flow_without_old_worker_restoring_it(self):
        qr_id = self.start_qr()
        self.wait_for(lambda: self.account.poll_qr(qr_id).get("qr_url"))
        result = self.pending()
        self.stop_workers()
        self.assertEqual(result["state"], "waiting_code")
        self.assertEqual(self.account.status()["state"], "waiting_code")
        self.assertNotIn("qr_id", json.loads(self.account.path.read_text()))

    def test_late_qr_success_after_logout_is_revoked_and_cannot_restore_session(self):
        entered, release = threading.Event(), threading.Event()
        async def late_response():
            entered.set()
            while not release.is_set():
                await asyncio.sleep(0.01)
            return self.qr_success()
        self.driver.qr_responses = [late_response]
        self.start_qr()
        self.assertTrue(entered.wait(2))
        result = self.account.logout()
        self.assertTrue(result["ok"])
        self.assertEqual(result["code"], "LOGOUT_UNCONFIRMED")
        release.set()
        self.stop_workers()
        self.assertEqual(json.loads(self.account.path.read_text()), {})
        self.assertFalse(any(call[0] == "on_login" for call in self.driver.calls))
        self.assertTrue(any(call[0] == "log_out" for call in self.driver.calls))

    def test_late_qr_success_after_replacement_cannot_overwrite_new_nonce(self):
        entered, release = threading.Event(), threading.Event()
        async def late_response():
            entered.set()
            while not release.is_set():
                await asyncio.sleep(0.01)
            return self.qr_success()
        self.driver.qr_responses = [late_response]
        first = self.start_qr()
        self.assertTrue(entered.wait(2))
        second = self.account.request_qr()["qr_id"]
        release.set()
        self.wait_for(lambda: self.account.poll_qr(second).get("qr_url"))
        self.wait_for(lambda: any(call[0] == "log_out" for call in self.driver.calls))
        self.assertEqual(json.loads(self.account.path.read_text())["qr_id"], second)
        self.assertFalse(self.account.status()["authorized"])
        self.assertEqual(self.account.poll_qr(first)["code"], "QR_EXPIRED")

    def test_orphaned_qr_owner_expires_without_network_and_never_waits_forever(self):
        qr_id = "a" * 32
        self.account._save({"api_id": 1, "api_hash": self.api_hash, "state": "waiting_qr", "qr_id": qr_id,
                            "qr_owner": 999999, "qr_url": "tg://login?token=PRIVATE", "qr_heartbeat": time.time() - 46,
                            "expires": time.time() + 180, "qr_expires": time.time() + 30})
        self.assertEqual(self.account.status()["state"], "disconnected")
        result = self.account.poll_qr(qr_id)
        self.assertEqual(result["code"], "QR_EXPIRED")
        self.assertNotIn("qr_url", self.account.path.read_text())
        self.assertEqual(self.driver.calls, [])

    def test_account_discovery_is_public_only_bounded_and_keyword_rotation_is_explicit(self):
        self.authorized()
        self.driver.discovery = SimpleNamespace(chats=[channel(), channel(None), channel("PrivateGroup", broadcast=False),
                                                       channel("ScamSource", scam=True)], users=[types.User(id=99)])
        for cursor, query in enumerate(("quark", "kuake", "夸克网盘", "影视资源", "动漫资源")):
            self.driver.calls.clear()
            result = self.account.discover_channels(cursor=cursor)
            self.assertEqual(result["channels"], [{"name": "FilmChannel", "title": "影视资源"}])
            self.assertIn(("discover_query", query), self.driver.calls)
            self.assertEqual(sum(call[0] == "request" for call in self.driver.calls), 1)
        self.driver.calls.clear()
        result = self.account.discover_channels(["FilmChannel"], cursor=5)
        self.assertTrue(result["ok"])
        self.assertIn(("entity", "FilmChannel"), self.driver.calls)
        self.assertIn(("request", "GetChannelRecommendationsRequest"), self.driver.calls)
        self.assertFalse(any(call == ("request", "JoinChannelRequest") for call in self.driver.calls))

    def test_join_is_explicit_public_only_and_existing_members_are_idempotent(self):
        self.authorized()
        self.driver.entities["FilmChannel"] = channel(left=True)
        result = self.account.join_channel("FilmChannel")
        self.assertTrue(result["joined"])
        self.assertFalse(result["already_member"])
        self.assertIn(("request", "JoinChannelRequest"), self.driver.calls)
        self.driver.calls.clear()
        self.driver.entities["FilmChannel"] = channel(left=False)
        result = self.account.join_channel("FilmChannel")
        self.assertTrue(result["already_member"])
        self.assertFalse(any(call[0] == "request" for call in self.driver.calls))

    def test_join_skips_restricted_approval_paid_and_private_targets(self):
        self.authorized()
        for entity, code in ((channel(left=True, join_request=True), "CHANNEL_APPROVAL_REQUIRED"),
                             (channel(left=True, subscription_until_date=datetime.now(timezone.utc)), "CHANNEL_PAID"),
                             (channel(left=True, scam=True), "CHANNEL_RESTRICTED"),
                             (channel(None, left=True), "CHANNEL_UNAVAILABLE")):
            self.driver.entities["FilmChannel"] = entity
            self.driver.calls.clear()
            self.assertEqual(self.account.join_channel("FilmChannel")["code"], code)
            self.assertFalse(any(call[0] == "request" for call in self.driver.calls))

    def test_join_flood_wait_blocks_account_until_server_deadline(self):
        self.authorized()
        self.driver.entities["FilmChannel"] = channel(left=True)
        self.driver.error["request"] = rpc_error("FloodWaitError", 240)
        result = self.account.join_channel("FilmChannel")
        self.assertEqual(result["code"], "FLOOD_WAIT")
        self.assertEqual(result["retry_after"], 240)
        self.assertEqual(self.account.status()["retry_after"], 240)

    def test_join_rejects_changed_account_before_resolving_or_joining_channel(self):
        self.authorized()
        old_generation = self.account.authorization_generation()
        self.account.logout()
        self.authorized()
        self.driver.entities["FilmChannel"] = channel(left=True)
        result = self.account.join_channel("FilmChannel", expected_generation=old_generation)
        self.assertEqual(result["code"], "ACCOUNT_CHANGED")
        self.assertFalse(any(call[0] in {"entity", "request"} for call in self.driver.calls))
        self.driver.calls.clear()
        result = self.account.join_channel("FilmChannel", expected_generation=self.account.authorization_generation())
        self.assertTrue(result["joined"])

    def test_authorization_generation_changes_only_on_new_authorization(self):
        self.authorized()
        first = self.account.authorization_generation()
        self.assertEqual(self.account.authorization_generation(), first)
        self.account.logout()
        self.assertEqual(self.account.authorization_generation(), "")
        self.authorized()
        self.assertNotEqual(self.account.authorization_generation(), first)

    def test_logout_clears_corrupt_private_storage_and_bad_session(self):
        self.account.path.write_text("{broken SECRET SESSION", encoding="utf-8")
        result = self.account.logout()
        self.assertTrue(result["ok"])
        self.assertEqual(result["code"], "LOGOUT_UNCONFIRMED")
        self.assertEqual(json.loads(self.account.path.read_text()), {})
        self.authorized()
        data = json.loads(self.account.path.read_text())
        data["session"] = "invalid-session"
        self.account._save(data)
        result = self.account.search("测试", ["FilmChannel"])
        self.assertEqual(result["code"], "ACCOUNT_REVOKED")
        self.assertFalse(result["authorized"])

    @unittest.skipIf(os.name == "nt", "POSIX file permissions are validated in Linux CI")
    def test_private_file_permissions_are_owner_only(self):
        self.authorized()
        self.assertEqual(self.account.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.account.path.with_suffix(".lock").stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
