import errno
from contextlib import redirect_stderr
import io
import json
import socket
import unittest
from unittest.mock import Mock, patch

import requests

from scripts import probe_sources as probe


class SourceProbeTests(unittest.TestCase):
    def test_health_url_rejects_credentials_and_keeps_base_path(self):
        self.assertEqual(probe.health_url("https://example.com/base/api/search"),
                         "https://example.com/base/api/health")
        for value in ("https://user:secret@example.com", "https://example.com?token=secret",
                      "file:///etc/passwd", "https://example.com:wrong"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                probe.health_url(value)

    def test_malformed_credential_url_never_appears_in_error_output(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit):
            probe.main(["--pansou", "https://user:secret＠example.com"])
        self.assertNotIn("secret", stderr.getvalue())
        self.assertIn("PanSou 地址格式无效", stderr.getvalue())

    def test_http_has_no_netrc_no_cookie_and_no_redirects(self):
        response = Mock(status_code=200, headers={"Content-Type": "application/json"})
        response.iter_content.return_value = [b'{"status":"ok","auth_enabled":false,"plugin_count":5,"secret":"never-print"}']
        session = Mock(headers={})
        session.get.return_value.__enter__ = Mock(return_value=response)
        session.get.return_value.__exit__ = Mock(return_value=False)
        with patch.object(probe.requests, "Session") as factory, patch.object(
                probe.requests.utils, "get_environ_proxies", return_value={"https": "http://user:secret@proxy:8080"}):
            factory.return_value.__enter__.return_value = session
            result = probe.check_http("https://example.com/api/health")
        self.assertIs(session.trust_env, False)
        self.assertTrue(result["proxy_used"])
        self.assertEqual(result["plugin_count"], 5)
        self.assertNotIn("secret", json.dumps(result))
        self.assertNotIn("proxy:8080", json.dumps(result))
        self.assertFalse(session.get.call_args.kwargs["allow_redirects"])
        self.assertNotIn("cookies", session.get.call_args.kwargs)

    def test_denied_response_is_not_read_or_retried(self):
        response = Mock(status_code=429)
        session = Mock(headers={})
        session.get.return_value.__enter__ = Mock(return_value=response)
        session.get.return_value.__exit__ = Mock(return_value=False)
        with patch.object(probe.requests, "Session") as factory:
            factory.return_value.__enter__.return_value = session
            result = probe.check_http("https://example.com", direct=True)
        self.assertEqual(result["http_status"], 429)
        self.assertFalse(result["proxy_used"])
        self.assertEqual(session.proxies, {})
        response.iter_content.assert_not_called()
        session.get.assert_called_once()

    def test_exception_text_never_leaks(self):
        self.assertEqual(probe.error_reason(requests.exceptions.ProxyError("http://user:secret@host")),
                         "proxy_connection_failed")
        self.assertEqual(probe.error_reason(OSError(errno.ENETUNREACH, "secret")), "network_unreachable")
        self.assertEqual(probe.error_reason(socket.gaierror("secret")), "dns_failed")
        self.assertEqual(probe.error_reason(requests.ReadTimeout("secret")), "timeout")

    def test_ipv4_ipv6_separated_and_dns_failure_does_not_connect(self):
        with patch.object(probe, "resolve", return_value=([], "dns_failed")), patch.object(probe.socket, "socket") as sock:
            result = probe.direct_tcp("example.com", 443)
        sock.assert_not_called()
        self.assertEqual(result["ipv4"]["status"], "no_address")
        self.assertEqual(result["ipv6"]["status"], "no_address")
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443)),
                     (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:4700::1111", 443, 0, 0))]
        with patch.object(probe, "resolve", return_value=(addresses, "ok")), patch.object(probe.socket, "socket") as sock:
            sock.return_value.__enter__.return_value.connect.side_effect = [None, OSError(errno.ENETUNREACH, "error")]
            result = probe.direct_tcp("example.com", 443)
        self.assertEqual(result["ipv4"]["status"], "ok")
        self.assertEqual(result["ipv4"]["address_type"], "public")
        self.assertEqual(result["ipv6"]["status"], "network_unreachable")

    def test_fake_ip_dns_is_visible_without_disabling_safety(self):
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.8", 443))]
        with patch.object(probe, "resolve", return_value=(addresses, "ok")), patch.object(probe.socket, "socket"):
            result = probe.direct_tcp("example.com", 443)
        self.assertEqual(result["ipv4"]["address_type"], "fake_ip_range")

    def test_response_shape_only_reports_allowlisted_metadata(self):
        value = {"data": {"status": "secret", "auth_enabled": True, "plugin_count": 8, "token": "secret"}}
        result = probe.response_shape(json.dumps(value), "application/json")
        self.assertEqual(result["plugin_count"], 8)
        self.assertNotIn("secret", json.dumps(result))
        self.assertEqual(probe.response_shape(b"<html>secret</html>", "text/html"), {"structure": "html"})

    def test_oversize_body_stops_before_next_chunk(self):
        response = Mock(status_code=200, headers={"Content-Type": "text/html"})

        def chunks(**kwargs):
            yield b"x" * (probe.MAX_BYTES + 1)
            self.fail("probe read beyond size limit")

        response.iter_content.side_effect = chunks
        session = Mock(headers={})
        session.get.return_value.__enter__ = Mock(return_value=response)
        session.get.return_value.__exit__ = Mock(return_value=False)
        with patch.object(probe.requests, "Session") as factory:
            factory.return_value.__enter__.return_value = session
            result = probe.check_http("https://example.com", direct=True)
        self.assertEqual(result["structure"], "read_limit")


if __name__ == "__main__":
    unittest.main()
