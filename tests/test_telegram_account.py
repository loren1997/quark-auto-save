import asyncio
from collections import defaultdict
import io
import json
import logging
import os
from pathlib import Path
import ssl
import tempfile
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

    def client(self, session, api_id, api_hash, **options):
        self.calls.append(("client", options))
        driver = self

        class Client:
            def __init__(self):
                self.session = session
                session.set_dc(2, "149.154.167.51", 443)
                session.auth_key = AuthKey(bytes(range(256)))

            async def connect(self):
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
                assert isinstance(request, functions.chatlists.CheckChatlistInviteRequest)
                return driver.folder

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
