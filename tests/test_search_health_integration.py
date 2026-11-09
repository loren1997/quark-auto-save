import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.sdk.common import SearchError
from resource_search import search_error_reason, search_resources, search_web_resources
with contextlib.redirect_stdout(io.StringIO()):
    from quark_auto_save import Quark


class SearchHealthIntegrationTests(unittest.TestCase):
    def test_pansou_timeout_is_shared_by_separate_searches(self):
        config = {"source": {"pansou": {"server": "https://index.example"},
                             "cloudsaver": {"enable": False}}}
        with tempfile.TemporaryDirectory() as directory, patch("resource_search.PanSou") as constructor:
            path = str(Path(directory) / "health.sqlite3")
            constructor.return_value.search.side_effect = SearchError("网络请求失败 (ReadTimeout)")
            first = search_resources("第一部", config, include_web=False, health_path=path)
            second = search_resources("另一部", config, include_web=False, health_path=path)
            self.assertFalse(first["success"])
            self.assertFalse(second["success"])
            self.assertIn("来源冷却中：请求超时", second["message"])
            self.assertEqual(constructor.return_value.search.call_count, 1)

    def test_web_cooldown_reason_remains_visible_and_client_uses_shared_path(self):
        message = "来源冷却中：需要人工验证（剩余 1800 秒）"
        with patch("resource_search.WebSearch") as constructor:
            constructor.return_value.search.return_value = []
            constructor.return_value.errors = ["Google: " + message]
            result = search_web_resources("作品", {}, health_path="config/health.sqlite3")
            constructor.assert_called_once_with(health_path="config/health.sqlite3")
            self.assertEqual(result["errors"], [{"source": "网页搜索 Google", "message": message}])
            constructor.return_value.session.close.assert_called_once()
        self.assertEqual(search_error_reason("Network is unreachable [Errno 101] https://user:secret@proxy.example"),
                         "代理连接失败")
        self.assertEqual(search_error_reason("Network is unreachable [Errno 101]"),
                         "网络不可达，请检查容器出口与代理")
        self.assertNotIn("unsafe-secret", search_error_reason("来源冷却中：unsafe-secret（剩余 2 秒）"))

    def test_automatic_resource_search_passes_cooldown_to_index_and_web(self):
        account = Quark("")
        account.search_health_path = "config/health.sqlite3"
        empty = {"success": True, "data": [], "errors": []}
        with patch("resource_search.search_resources", return_value=empty) as index, \
                patch("resource_search.search_web_resources", return_value=empty) as web:
            result = account.find_valid_resource({"taskname": "测试作品"}, {})
        self.assertFalse(result["success"])
        self.assertTrue(index.called and web.called)
        for call in index.call_args_list + web.call_args_list:
            self.assertEqual(call.kwargs["health_path"], account.search_health_path)


if __name__ == "__main__":
    unittest.main()
