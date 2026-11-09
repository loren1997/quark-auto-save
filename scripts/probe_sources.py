"""匿名、只读检查容器搜索出口；不读取应用配置、Cookie、token 或 .netrc。"""

import argparse
import errno
import ipaddress
import json
import os
from queue import Empty, Queue
import socket
import ssl
from threading import Thread
import time
from urllib.parse import urlsplit, urlunsplit

import requests


TIMEOUT = 3
MAX_BYTES = 65536


def error_reason(error):
    """Never include exception text: proxy URLs can contain credentials."""
    if isinstance(error, requests.exceptions.ProxyError):
        return "proxy_connection_failed"
    if isinstance(error, (requests.exceptions.SSLError, ssl.SSLError)):
        return "tls_failed"
    if isinstance(error, (requests.exceptions.Timeout, TimeoutError)):
        return "timeout"
    if isinstance(error, socket.gaierror):
        return "dns_failed"
    if isinstance(error, OSError) and error.errno in (errno.ENETUNREACH, errno.EHOSTUNREACH, 10051, 10065):
        return "network_unreachable"
    if isinstance(error, OSError) and error.errno in (errno.ECONNREFUSED, 10061):
        return "connection_refused"
    return "connection_failed"


def resolve(host, port):
    # getaddrinfo has no portable timeout; a daemon bounds this one-shot probe.
    queue = Queue(maxsize=1)

    def lookup():
        try:
            queue.put(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
        except OSError as error:
            queue.put(error)

    Thread(target=lookup, daemon=True).start()
    try:
        result = queue.get(timeout=TIMEOUT)
    except Empty:
        return [], "dns_timeout"
    return ([], error_reason(result)) if isinstance(result, Exception) else (result, "ok")


def direct_tcp(host, port):
    addresses, dns = resolve(host, port)
    result = {"dns": dns}
    for family, label in ((socket.AF_INET, "ipv4"), (socket.AF_INET6, "ipv6")):
        matches = [item for item in addresses if item[0] == family]
        if not matches:
            result[label] = {"status": "no_address"}
            continue
        address = matches[0][4]
        started = time.monotonic()
        try:
            with socket.socket(family, socket.SOCK_STREAM) as connection:
                connection.settimeout(TIMEOUT)
                connection.connect(address)
            status = "ok"
        except OSError as error:
            status = error_reason(error)
        ip = ipaddress.ip_address(address[0].split("%", 1)[0])
        address_type = ("fake_ip_range" if ip.version == 4 and ip in ipaddress.ip_network("198.18.0.0/15")
                        else "public" if ip.is_global else "reserved" if ip.is_reserved else "private")
        result[label] = {"address": address[0], "address_type": address_type, "status": status,
                         "seconds": round(time.monotonic() - started, 2)}
    return result


def response_shape(body, content_type):
    try:
        value = json.loads(body)
    except (ValueError, UnicodeError):
        return {"structure": "html" if "html" in content_type.lower() else "non_json"}
    if isinstance(value, list):
        return {"structure": "json_array", "items": len(value)}
    if not isinstance(value, dict):
        return {"structure": "json_scalar"}
    result = {"structure": "json_object"}
    data = value.get("data", value)
    if isinstance(data, dict):
        if data.get("status") in ("ok", "healthy", "error"):
            result["status"] = data["status"]
        if isinstance(data.get("auth_enabled"), bool):
            result["auth_enabled"] = data["auth_enabled"]
        for key in ("plugin_count", "channels_count", "total"):
            if type(data.get(key)) is int:
                result[key] = data[key]
        result["search_fields"] = any(key in data for key in ("merged_by_type", "results", "total"))
    return result


def check_http(url, direct=False):
    proxies = {} if direct else requests.utils.get_environ_proxies(url)
    result = {"proxy_used": bool(proxies.get(urlsplit(url).scheme) or proxies.get("all"))}
    started = time.monotonic()
    with requests.Session() as session:
        session.trust_env = False  # Disable .netrc; only explicit environment proxies are copied.
        session.proxies = proxies
        session.headers["User-Agent"] = "quark-auto-save/network-probe"
        try:
            with session.get(url, timeout=(TIMEOUT, TIMEOUT), allow_redirects=False, stream=True) as response:
                result["http_status"] = response.status_code
                if response.status_code in (401, 403, 429) or 300 <= response.status_code < 400:
                    return result
                body = bytearray()
                for chunk in response.iter_content(chunk_size=4096):
                    body.extend(chunk)
                    if len(body) > MAX_BYTES or time.monotonic() - started > TIMEOUT * 2:
                        result["structure"] = "read_limit"
                        return result
                result.update(response_shape(body, response.headers.get("Content-Type", "")))
        except (requests.RequestException, OSError, ValueError) as error:
            result["error"] = error_reason(error)
        finally:
            result["seconds"] = round(time.monotonic() - started, 2)
    return result


def health_url(value):
    try:
        parts = urlsplit(value.strip())
        parts.port
        if parts.hostname:
            parts.hostname.encode("idna")
    except (ValueError, UnicodeError):
        raise ValueError("PanSou 地址格式无效") from None
    if (parts.scheme not in ("http", "https") or not parts.hostname
            or parts.username or parts.password or parts.query or parts.fragment):
        raise ValueError("PanSou 地址必须是无账号、参数和片段的 HTTP(S) URL")
    path = parts.path.rstrip("/")
    if path.endswith("/api/search"):
        path = path[:-len("/api/search")]
    elif path.endswith("/api/health"):
        path = path[:-len("/api/health")]
    return urlunsplit((parts.scheme, parts.netloc, path + "/api/health", "", ""))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--direct", action="store_true", help="HTTP 也直连，不使用环境代理；不会修改应用设置")
    parser.add_argument("--pansou", action="append", help="覆盖待检查的 PanSou 地址，最多三个；不提供认证信息")
    args = parser.parse_args(argv)
    servers = args.pansou if args.pansou is not None else [
        os.getenv("PANSOU_SERVER") or "http://pansou:8888",
        *(os.getenv("PANSOU_FALLBACK_SERVERS") or "https://so.252035.xyz").splitlines(),
    ]
    try:
        servers = list(dict.fromkeys(health_url(value) for value in servers if value.strip()))
    except ValueError as error:
        parser.error(str(error))
    if len(servers) > 3:
        parser.error("最多检查三个 PanSou 地址")
    targets = [("PanSou" if index == 0 else f"PanSou 聚合 {index}", url)
               for index, url in enumerate(servers)] + [
        ("TVmaze", "https://api.tvmaze.com/shows/1/akas"),
        ("Bangumi", "https://api.bgm.tv/calendar"),
        ("Telegram", "https://t.me/s/tgsearchers7"),
        ("腾讯频道", "https://pd.qq.com/g/pd26778899"),
        ("Google", "https://www.google.com/"),
    ]
    print(json.dumps({"mode": "direct" if args.direct else "environment_proxy",
                      "notice": "健康及连通性检查不代表资源搜索成功；未读取应用配置或认证信息；direct_tcp 始终直连，http 按 mode 使用代理"}, ensure_ascii=False), flush=True)
    for source, url in targets:
        parts = urlsplit(url)
        result = {"source": source, "host": parts.hostname,
                  "direct_tcp": direct_tcp(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)),
                  "http": check_http(url, args.direct)}
        print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
