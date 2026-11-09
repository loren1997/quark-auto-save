"""Bounded, process-shared cooldowns for public sources; no URLs or credentials stored."""

from contextlib import closing, contextmanager
from email.utils import parsedate_to_datetime
import logging
import math
import sqlite3
import time
from urllib.parse import urlsplit

from .common import SearchError


class SourceHealth:
    DURATIONS = {"HTTP 429": 300, "HTTP 403": 900, "需要人工验证": 1800, "请求超时": 60}

    def __init__(self, path=None):
        self.path = path

    @contextmanager
    def _connect(self):
        with closing(sqlite3.connect(self.path, timeout=1)) as connection, connection:
            connection.execute("CREATE TABLE IF NOT EXISTS cooldowns (host TEXT PRIMARY KEY, reason TEXT NOT NULL, until REAL NOT NULL)")
            yield connection

    def check(self, url):
        if not self.path:
            return
        host = urlsplit(url).hostname
        try:
            with self._connect() as connection:
                row = connection.execute("SELECT reason, until FROM cooldowns WHERE host = ?", (host,)).fetchone()
                if row:
                    try:
                        until = float(row[1])
                        valid = row[0] in self.DURATIONS and math.isfinite(until)
                    except (TypeError, ValueError, OverflowError):
                        valid = False
                    if not valid:
                        connection.execute("DELETE FROM cooldowns WHERE host = ?", (host,))
                        logging.warning("搜索来源冷却记录格式无效，已清除此来源记录并继续查询")
                        return
        except (sqlite3.Error, OSError) as error:
            logging.warning("搜索来源冷却记录读取失败 (%s)，继续查询", type(error).__name__)
            return
        remaining = math.ceil(until - time.time()) if row else 0
        if remaining > 0:
            raise SearchError(f"来源冷却中：{row[0]}（剩余 {remaining} 秒）")

    def record(self, url, reason, retry_after=None):
        host = urlsplit(url).hostname
        if not self.path or not host or reason not in self.DURATIONS:
            return
        now = time.time()
        duration = self.DURATIONS[reason]
        if retry_after is not None:
            try:
                value = str(retry_after).strip()
                duration = float(value) if value.isdecimal() else parsedate_to_datetime(value).timestamp() - now
                # shortcut: 服务端冷却最多保留 7 天；若来源要求更久，改为显式停用该来源。
                duration = max(1, min(duration, 7 * 86400))
            except (ValueError, TypeError, OverflowError):
                pass
        try:
            with self._connect() as connection:
                connection.execute("DELETE FROM cooldowns WHERE until <= ?", (now,))
                connection.execute(
                    "INSERT INTO cooldowns VALUES (?, ?, ?) ON CONFLICT(host) DO UPDATE SET "
                    "reason=excluded.reason, until=MAX(cooldowns.until, excluded.until)",
                    (host, reason, now + duration),
                )
                connection.execute("DELETE FROM cooldowns WHERE host NOT IN (SELECT host FROM cooldowns ORDER BY until DESC LIMIT 128)")
        except (sqlite3.Error, OSError) as error:
            logging.warning("搜索来源冷却记录写入失败 (%s)，下轮可能重新请求", type(error).__name__)
