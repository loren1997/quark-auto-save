from copy import deepcopy
import threading
import time
import unittest
from unittest.mock import Mock, patch

from app.sdk.common import SearchError
from resource_search import search_resources, search_web_resources, search_telegram_resources, search_error_reason


class TelegramArchiveSearchIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.config = {"source": {"websearch": {"enable": True},
                       "channelsearch": {"enable": False}, "telegram_web": {"enable": True},
                       "telegram": {"enable": True, "auto_join": True}}}
        self.web = Mock(errors=[])
        self.web.search.return_value = []
        self.browser = Mock()
        self.candidate = {"taskname": "测试动画", "shareurl": "https://pan.quark.cn/s/abc123",
                          "content": "测试动画 S01E02", "source": "Telegram归档"}
        self.browser.search.return_value = {"success": True, "results": [self.candidate], "data": {}}
        self.web_class = patch("resource_search.WebSearch", return_value=self.web).start()
        self.browser_class = patch("app.sdk.telegram_web.TelegramWeb", return_value=self.browser).start()
        self.legacy = patch("app.sdk.telegram_account.TelegramAccount").start()
        self.addCleanup(patch.stopall)

    def test_archive_search_independent_of_public_channel_switch_and_old_lists(self):
        original = deepcopy(self.config)
        result = search_web_resources("测试动画", self.config)
        self.assertEqual(result["data"], [self.candidate])
        self.assertEqual(self.config, original)
        args, kwargs = self.browser.search.call_args
        self.assertEqual(args, ("测试动画",))
        self.assertEqual(kwargs["limit"], 20)
        self.assertGreater(kwargs["deadline"], time.monotonic())
        self.legacy.assert_not_called()

    def test_legacy_enable_does_not_enable_browser_or_old_account(self):
        self.config["source"]["telegram_web"]["enable"] = False
        search_web_resources("测试动画", self.config)
        self.browser_class.assert_not_called()
        self.legacy.assert_not_called()

    def test_archive_can_be_only_enabled_source(self):
        self.config["source"]["websearch"]["enable"] = False
        result = search_web_resources("测试动画", self.config)
        self.assertTrue(result["success"])
        self.assertEqual(result["data"], [self.candidate])
        self.web.search.assert_not_called()

    def test_manual_search_reaches_browser_when_other_sources_disabled(self):
        self.config["source"]["websearch"]["enable"] = False
        self.config["source"]["pansou"] = {"enable": False}
        self.config["source"]["cloudsaver"] = {"enable": False}
        result = search_resources("测试动画", self.config)
        self.assertTrue(result["success"])
        self.assertEqual(len(result["data"]), 1)
        self.browser.search.assert_called_once()

    def test_web_and_archive_run_concurrently_and_partial_links_survive(self):
        started = threading.Event()
        def archive(*_, **__):
            started.set()
            return {"success": False, "partial": True, "results": [self.candidate],
                    "data": {"reason": "本轮查询超时，下一轮继续"}}
        def web(*_, **__):
            self.assertTrue(started.wait(2))
            raise SearchError("TIMEOUT")
        self.browser.search.side_effect = archive
        self.web.search.side_effect = web
        failed = set()
        result = search_web_resources("测试动画", self.config, failed_engines=failed)
        self.assertTrue(result["success"])
        self.assertEqual(result["data"], [self.candidate])
        self.assertIn("Telegram归档", failed)

    def test_transport_exception_is_sanitized_without_losing_web_results(self):
        self.web.search.return_value = [self.candidate]
        self.browser.search.side_effect = RuntimeError("private-chat-name secret-session")
        result = search_web_resources("测试动画", self.config)
        self.assertTrue(result["success"])
        self.assertEqual(result["data"], [self.candidate])
        self.assertNotIn("private-chat-name", str(result))
        self.assertNotIn("secret-session", str(result))

    def test_full_archive_batch_is_not_reported_as_a_failed_source(self):
        self.browser.search.return_value = {"success": True, "results": [self.candidate], "partial": True,
                                           "data": {"archived_count": 28}, "progress": {"current": 154, "total": 784}}
        failed = set()
        result = search_web_resources("测试动画", self.config, failed_engines=failed)
        self.assertEqual(result["errors"], [])
        self.assertNotIn("Telegram归档", failed)
        self.assertEqual(result["data"], [self.candidate])

    def test_web_only_phase_does_not_search_archive_again(self):
        search_web_resources("测试动画", self.config, include_archive=False)
        self.browser.search.assert_not_called()

    def test_resource_button_failure_is_logged_without_losing_candidates(self):
        self.browser.search.return_value.update(bot={"requests": 1, "links": 1, "pending": 0, "failed": 1})
        with patch("resource_search.print") as log:
            result = search_telegram_resources("测试动画", self.config)
        self.assertEqual(result["data"], [self.candidate])
        self.assertEqual(result["errors"][0]["source"], "Telegram归档资源按钮")
        self.assertTrue(any("Oscar4KBot 请求 1 次" in call.args[0] for call in log.call_args_list))

    def test_partial_archive_flood_reports_wait_without_saying_channel_is_missing(self):
        self.browser.search.return_value = {"success": False, "code": "FLOOD_WAIT", "results": [], "partial": True,
            "data": {"archived_count": 28, "retry_after": 60, "reason": "Telegram 暂时限制查询，请等待后自动重试"},
            "progress": {"phase": "archives", "current": 5, "total": 28}, "bot": {"channel_archived": 0}}
        with patch("resource_search.print") as log:
            result = search_telegram_resources("测试动画", self.config)
        text = "\n".join(call.args[0] for call in log.call_args_list)
        self.assertIn("归档列表尚未读完", text)
        self.assertNotIn("未检测到已归档", text)
        self.assertNotIn("已检查 5 条消息", text)
        self.assertEqual(result["errors"][0]["message"], "Telegram 查询限流，剩余 60 秒后自动重试")
        self.assertEqual(search_error_reason(result["errors"][0]["message"]), result["errors"][0]["message"])

    def test_message_details_failure_is_reported_without_exposing_channel_text(self):
        self.browser.search.return_value.update(bot={"channel_archived": 1, "channel_posts": 9,
                                                   "details_read": 8, "details_failed": 1, "button_count": 3, "buttons": 1,
                                                   "button_rejections": {"other_target": 1, "invalid_payload": 1}})
        with patch("resource_search.print") as log:
            result = search_telegram_resources("测试动画", self.config)
        text = "\n".join(call.args[0] for call in log.call_args_list)
        self.assertIn("奥斯卡资源频道在归档中", text)
        self.assertIn("补读 8 条详情", text)
        self.assertIn("读取 3 个按钮", text)
        self.assertIn("其他目标 1，资源编号不支持 1", text)
        self.assertIn("详情未读完整", text)
        self.assertEqual(result["errors"][0]["source"], "Telegram归档消息详情")

    def test_unavailable_browser_is_not_retried_for_every_alias_in_same_run(self):
        self.browser.search.return_value = {"success": False, "results": [],
                                           "data": {"reason": "需要连接 Telegram Web"}}
        failed = set()
        search_web_resources("测试动画", self.config, failed_engines=failed)
        search_web_resources("动画别名", self.config, failed_engines=failed)
        self.assertEqual(self.browser.search.call_count, 1)

    def test_invalid_query_does_not_touch_any_source(self):
        self.assertFalse(search_web_resources("x" * 201, self.config)["success"])
        self.browser_class.assert_not_called()
        self.web_class.assert_not_called()
