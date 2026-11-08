from datetime import datetime, timezone, timedelta
import html
import re
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit


class SearchError(Exception):
    """搜索源不可用或返回不受支持的数据，区别于正常的零结果。"""


def normalize_share_url(value, password="", context=""):
    """仅接受夸克分享链接，将独立提取码合并到下载器识别的 pwd 参数。"""
    if not isinstance(value, str):
        return ""
    match = re.search(r"https?://[^\s<>\"'，。；）)\]]+", html.unescape(value))
    if not match:
        return ""
    try:
        parts = urlsplit(match.group(0))
        if (parts.hostname or "").lower() != "pan.quark.cn" or parts.username or parts.port:
            return ""
        if not re.match(r"^/s/[a-zA-Z0-9]+(?:/|$)", parts.path):
            return ""
        query = parse_qs(parts.query)
        code = (query.get("pwd") or query.get("passcode") or [password or ""])[0]
        if not code:
            code_match = re.search(r"(?:提取码|提取碼|访问码|密[码碼])\s*[:：]?\s*([a-zA-Z0-9]{4,8})(?![a-zA-Z0-9])", value + " " + str(context))
            code = code_match.group(1) if code_match else ""
        code = str(code).strip()
        if code and not re.fullmatch(r"[a-zA-Z0-9]{4,8}", code):
            code = ""
        return urlunsplit(("https", "pan.quark.cn", parts.path, urlencode({"pwd": code}) if code else "", parts.fragment))
    except ValueError:
        return ""


def iso_to_cst(iso_time_str: str) -> str:
    """将 ISO 格式的时间字符串转换为 CST(China Standard Time) 时间并格式化为 %Y-%m-%d %H:%M:%S 格式

    Args:
        iso_time_str (str): ISO 格式时间字符串

    Returns:
        str: CST(China Standard Time) 时间字符串
    """
    try:
        dt = datetime.fromisoformat(str(iso_time_str).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    tz = timezone(timedelta(hours=8))
    dt_cst = dt.astimezone(tz)
    return dt_cst.strftime("%Y-%m-%d %H:%M:%S") if dt_cst.year >= 1970 else ""
