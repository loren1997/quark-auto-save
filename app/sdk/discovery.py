"""Public metadata for the weekly discovery panel; no share links are invented."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from math import isfinite
from threading import Lock
from time import monotonic
import logging
import re

import requests


CHINA_TZ = timezone(timedelta(hours=8))
USER_AGENT = "quark-auto-save/1.0 (+https://github.com/Cp0204/quark-auto-save)"
TV_LIMIT = 10
_cache = None
_expires = 0
_lock = Lock()
_aliases = {}


def _get_json(url):
    with requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=(2, 4)) as response:
        response.raise_for_status()
        data = response.json()
    if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
        raise ValueError("数据格式不是条目列表")
    return data


def _fetch_many(urls):
    results, errors = {}, {}
    with ThreadPoolExecutor(max_workers=2) as executor:
        pending = {executor.submit(_get_json, url): key for key, url in urls.items()}
        for future in as_completed(pending):
            key = pending[future]
            try:
                results[key] = future.result()
            except (requests.RequestException, ValueError) as exc:
                logging.warning("Calendar source %s failed: %s", key, exc)
                errors[key] = "连接超时，请检查 NAS 网络或 HTTPS_PROXY" if isinstance(exc, requests.Timeout) else str(exc)
    return results, errors


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value) else 0


def _poster(images):
    if not isinstance(images, dict):
        return ""
    url = images.get("medium") or images.get("large") or ""
    return url if isinstance(url, str) and url.startswith(("https://", "http://")) else ""


def _anime_items(groups, days):
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("weekday"), dict):
            continue
        weekday = group["weekday"].get("id")
        if not isinstance(weekday, int) or weekday not in range(1, 8) or not isinstance(group.get("items"), list):
            continue
        seen = {item["id"] for item in days[weekday - 1]["anime"]}
        for item in group["items"]:
            if not isinstance(item, dict) or item.get("type") != 2:
                continue
            subject_id = item.get("id")
            title = item.get("name_cn") or item.get("name")
            if not isinstance(subject_id, int) or not isinstance(title, str) or not title.strip():
                continue
            key = f"bangumi:{subject_id}"
            if key in seen:
                continue
            seen.add(key)
            collection, rating = item.get("collection") or {}, item.get("rating") or {}
            days[weekday - 1]["anime"].append({
                "id": key, "title": title, "search_title": title,
                "source": "Bangumi", "source_url": f"https://bgm.tv/subject/{subject_id}",
                "poster": _poster(item.get("images")),
                "heat": _number(collection.get("doing")) if isinstance(collection, dict) else 0,
                "heat_label": "Bangumi 在看人数",
                "rating": _number(rating.get("score")) if isinstance(rating, dict) else 0,
                "year": str(item.get("air_date") or "")[:4],
                "schedule_note": "当季周播表；具体停播、调档以来源为准",
            })


def _tv_items(results, days, warnings, fallback_anime=False):
    shows, episodes = {}, {}
    day_dates = {day["date"] for day in days}
    for key, entries in results.items():
        if key == "anime":
            continue
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("airdate"), str) or entry["airdate"] not in day_dates:
                continue
            show = entry.get("show")
            if not isinstance(show, dict) or show.get("type") not in ("Scripted", "Animation"):
                continue
            show_id, title = show.get("id"), show.get("name")
            if not isinstance(show_id, int) or not isinstance(title, str) or not title.strip():
                continue
            shows[show_id] = show
            season, number = entry.get("season"), entry.get("number")
            if any(value is not None and not isinstance(value, int) for value in (season, number)):
                continue
            episodes.setdefault((entry["airdate"], show_id), set()).add((season, number))
    # shortcut: show the week's top 10 dramas; add pagination if a full catalogue is needed.
    popular = sorted((key for key in shows if shows[key]["type"] == "Scripted"),
                     key=lambda key: (-_number(shows[key].get("weight")), key))[:TV_LIMIT]
    if fallback_anime:
        popular += sorted((key for key in shows if shows[key]["type"] == "Animation"),
                          key=lambda key: (-_number(shows[key].get("weight")), key))[:TV_LIMIT]
    aliases, failures = _fetch_many({
        key: f"https://api.tvmaze.com/shows/{key}/akas" for key in popular if key not in _aliases
    })
    for key, entries in aliases.items():
        chinese = [item.get("name") for item in entries if isinstance(item, dict)
                   and isinstance(item.get("name"), str)
                   and re.search(r"[\u4e00-\u9fff]", item["name"])
                   and (not item.get("country") or
                        isinstance(item["country"], dict) and item["country"].get("code") == "CN")]
        _aliases[key] = chinese[0] if chinese else shows[key]["name"]
    if failures:
        warnings.append(f"{len(failures)} 部作品的中文别名暂不可用，已保留原名")
    untranslated = sum(not re.search(r"[\u4e00-\u9fff]", _aliases.get(key, shows[key]["name"])) for key in popular)
    if untranslated:
        warnings.append(f"{untranslated} 部作品尚无可用中文名，显示原名；搜索资源时可修改关键词")
    for day in days:
        for key in popular:
            aired = episodes.get((day["date"], key))
            if not aired:
                continue
            show = shows[key]
            title = _aliases.get(key, show["name"])
            rating = show.get("rating") or {}
            category = "anime" if show["type"] == "Animation" else "tv"
            for season in sorted({value[0] for value in aired}, key=lambda value: value or 0):
                numbers = sorted({number for current, number in aired if current == season}, key=lambda value: value or 0)
                search_title = f"{title} 第{season}季" if season and season > 1 else title
                season_label = f"第{season}季" if season is not None else "季数未标注"
                episode_label = "、".join(str(number) for number in numbers if number is not None)
                day[category].append({
                    "id": f"tvmaze:{key}:s{season if season is not None else 'unknown'}",
                    "title": search_title, "search_title": search_title,
                    "source": "TVmaze", "source_url": f"https://www.tvmaze.com/shows/{key}",
                    "poster": _poster(show.get("image")), "heat": _number(show.get("weight")),
                    "heat_label": "TVmaze 热度（0–100）",
                    "rating": _number(rating.get("average")) if isinstance(rating, dict) else 0,
                    "year": str(show.get("premiered") or "")[:4] if season == 1 else "",
                    "airdate": day["date"],
                    "episodes": [{"season": season, "number": number} for number in numbers],
                    "schedule_note": f"{season_label} 第{episode_label or '?'}集；TVmaze 中国播出排期，不代表网盘资源已更新",
                })


def get_calendar(config=None):
    """Return this Shanghai week's TV episodes and Bangumi's current weekly timetable."""
    global _cache, _expires
    now = datetime.now(CHINA_TZ)
    monday = now.date() - timedelta(days=now.weekday())
    week_start = monday.isoformat()
    with _lock:
        if _cache and _cache["week_start"] == week_start and monotonic() < _expires:
            cached = deepcopy(_cache)
            cached["cached"] = True
            return cached
        days = [{"weekday": index + 1, "label": f"星期{label}",
                 "date": (monday + timedelta(days=index)).isoformat(), "anime": [], "tv": []}
                for index, label in enumerate("一二三四五六日")]
        urls = {day["date"]: f"https://api.tvmaze.com/schedule?country=CN&date={day['date']}" for day in days}
        urls["anime"] = "https://api.bgm.tv/calendar"
        results, errors = _fetch_many(urls)
        warnings = [f"{'Bangumi 周播表' if key == 'anime' else 'TVmaze ' + key} 获取失败：{error}"
                    for key, error in sorted(errors.items())]
        old = _cache if _cache and _cache["week_start"] == week_start else None
        cached_bangumi = old and any(item["source"] == "Bangumi" for day in old["days"] for item in day["anime"])
        fallback_anime = "anime" in errors and not cached_bangumi
        _anime_items(results.get("anime", []), days)
        _tv_items(results, days, warnings, fallback_anime)
        if fallback_anime:
            warnings.append("Bangumi 暂不可用，动漫已降级为 TVmaze 中国播出动画前 10；按 TVmaze 热度排序，覆盖范围不含完整日番周播表")
        if old:
            stale_used = False
            for day, previous in zip(days, old["days"]):
                for category, key in (("anime", "anime"), ("tv", day["date"])):
                    if key in errors and previous[category] and not day[category]:
                        day[category] = deepcopy(previous[category])
                        stale_used = True
                        for item in day[category]:
                            item["stale"] = True
            if stale_used:
                warnings.append("失败的来源保留本周上次成功数据，已标注缓存；排期可能已变更")
        for day in days:
            for category in ("anime", "tv"):
                day[category].sort(key=lambda item: (-item["heat"], item["title"]))
        _cache = {
            "week_start": week_start, "week_end": days[-1]["date"], "timezone": "Asia/Shanghai",
            "updated_at": now.isoformat(), "cached": False, "days": days, "warnings": warnings,
            "notes": ["动漫：TVmaze 中国播出动画，本周热度前 10；按 TVmaze 热度排序。" if fallback_anime else
                      "动漫：Bangumi 当季周播表，不保证本周每一部都更新；按在看人数排序。",
                      f"电视剧：TVmaze 中国播出数据，本周热度前 {TV_LIMIT} 部，按实际集日期归日、按 TVmaze 热度排序。",
                      "热度口径按条目标注，各分类独立排序。TVmaze 数据按 CC BY-SA 4.0 使用。"],
        }
        _expires = monotonic() + (300 if errors else 3600)
        return deepcopy(_cache)
