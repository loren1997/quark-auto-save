import unittest
from unittest.mock import Mock

from app.sdk.pansou import PanSou


class PanSouArchiveBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.client = PanSou("https://example.org")
        self.addCleanup(self.client.session.close)

    def test_requests_only_plugins_and_ignores_legacy_telegram_channels(self):
        response = Mock(status_code=200)
        response.json.return_value = {"total": 0}
        self.client.session.post = Mock(return_value=response)
        self.assertEqual(self.client.search("作品", channels=["public_movie"], plugins=["qqpd"]), [])
        params = self.client.session.post.call_args.kwargs["json"]
        self.assertEqual(params["src"], "plugin")
        self.assertEqual(params["plugins"], ["qqpd"])
        self.assertNotIn("channels", params)

    def test_mixed_cached_telegram_results_are_dropped_but_plugins_are_preserved(self):
        response = Mock(status_code=200)
        response.json.return_value = {"merged_by_type": {"quark": [
            {"note": "目标作品", "url": "https://pan.quark.cn/s/abcdef", "source": "tg:public_movie"},
            {"note": "目标作品", "url": "https://pan.quark.cn/s/abcdef1", "source": "TG"},
            {"note": "目标作品", "url": "https://pan.quark.cn/s/abcdef2", "source": "plugin:telegram_index"},
            {"note": "目标作品", "url": "https://pan.quark.cn/s/abcdef3", "source": "plugin:qqpd"}
        ]}, "results": [{"title": "目标作品", "source": "tg:public_other", "content": "https://pan.quark.cn/s/abcdef4"}]}
        self.client.session.post = Mock(return_value=response)
        rows = self.client.search("目标作品")
        self.assertEqual({row["shareurl"] for row in rows},
                         {"https://pan.quark.cn/s/abcdef2", "https://pan.quark.cn/s/abcdef3"})


if __name__ == "__main__":
    unittest.main()
