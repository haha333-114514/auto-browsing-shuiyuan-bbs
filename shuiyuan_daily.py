#!/usr/bin/env python3
"""Generate daily-activity or incremental Shuiyuan reports with a persistent Edge session.

The collector deliberately uses same-origin browser fetches.  This keeps the
normal logged-in session, avoids copying passwords/cookies, and bypasses the
broken process-level proxy settings with Edge's --no-proxy-server switch.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlencode

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


# Scheduled runs may inherit a GBK console even when topic titles contain emoji.
# Keep progress logging from aborting the actual collection.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")


BASE_URL = "https://shuiyuan.sjtu.edu.cn"
SHANGHAI = timezone(timedelta(hours=8))
FOCUS_CATEGORIES = {"校园生活", "人生经验", "广而告之"}
FOCUS_TAGS = {"保研", "实习", "ai", "友校", "海峡两岸", "吃瓜", "涉政"}
DIARY_MARKERS = (
    "日记", "水楼", "记录楼", "打卡", "灌水楼", "投喂", "碎碎念",
    "成长记录", "手记", "轶事楼", "daily", "日常", "子楼", "测评楼", "交流楼",
    "投资记录", "投资实录", "秋招记录", "求职记录", "每日一首",
    "摄影记录", "锻炼记录", "轻松一刻", "无人倾诉",
)
SEXUAL_MARKERS = (
    "性行为", "性生活", "性经验", "性关系", "性需求", "性欲", "性冲动",
    "性癖", "性取向", "性健康", "性教育", "性病", "性交", "性侵",
    "性骚扰", "两性话题", "两性关系", "生殖器",
    "做爱", "约炮", "一夜情", "处男", "处女", "自慰", "手淫",
    "避孕", "安全套", "套套", "嫖娼", "卖淫", "援交", "强奸", "猥亵",
    "情色", "色情", "黄文", "成人用品", "情趣用品", "成人内容", "成人话题",
    "18禁", "r18", "nsfw", "涩涩", "瑟瑟",
)
SEXUAL_TAGS = {"性", "性话题", "性健康", "两性", "成人内容", "nsfw"}
NOISE_REPLIES = {"cy", "蹲", "收藏", "mark", "顶", "同问", "插眼", "围观", "来了"}


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self._skip += 1
        elif not self._skip and tag in {"br", "p", "div", "li", "blockquote", "pre", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._skip:
            self._skip -= 1
        elif not self._skip and tag in {"p", "div", "li", "blockquote", "pre"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(value: str | None) -> str:
    parser = TextExtractor()
    parser.feed(value or "")
    text = html.unescape("".join(parser.parts))
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(SHANGHAI)
    except ValueError:
        return None


def fmt_time(value: str | datetime | None) -> str:
    dt = value if isinstance(value, datetime) else parse_time(value)
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else "未知"


def clip(text: str, limit: int = 360) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def md_escape(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def chunks(values: list[int], size: int = 30) -> Iterable[list[int]]:
    for i in range(0, len(values), size):
        yield values[i : i + size]


def find_edge() -> str:
    candidates = [
        os.environ.get("SHUIYUAN_EDGE"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for item in candidates:
        if item and Path(item).is_file():
            return item
    raise RuntimeError("未找到 Microsoft Edge。可用环境变量 SHUIYUAN_EDGE 指定 msedge.exe。")


def same_origin_json(page: Any, path: str, timeout_ms: int = 30_000) -> dict[str, Any]:
    result = page.evaluate(
        """async ({path, timeoutMs}) => {
          const ctl = new AbortController();
          const timer = setTimeout(() => ctl.abort(), timeoutMs);
          try {
            const r = await fetch(path, {
              credentials: 'include',
              headers: {'Accept': 'application/json', 'X-Requested-With': 'XMLHttpRequest'},
              signal: ctl.signal
            });
            const text = await r.text();
            return {ok: r.ok, status: r.status, contentType: r.headers.get('content-type') || '', text};
          } finally { clearTimeout(timer); }
        }""",
        {"path": path, "timeoutMs": timeout_ms},
    )
    if not result.get("ok"):
        raise RuntimeError(f"读取 {path} 失败：HTTP {result.get('status')}，{clip(result.get('text', ''), 160)}")
    try:
        return json.loads(result["text"])
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{path} 未返回 JSON，可能登录已过期：{clip(result.get('text', ''), 160)}") from exc


def ensure_login(page: Any, timeout_seconds: int = 420, allow_wait: bool = True) -> None:
    page.goto(f"{BASE_URL}/latest", wait_until="domcontentloaded", timeout=45_000)
    if page.url.startswith(BASE_URL) and "login" not in page.url:
        return
    if not allow_wait:
        raise RuntimeError("水源登录态不存在或已过期")
    print("\n需要登录水源。请在刚打开的 Edge 窗口中完成交大统一身份认证。")
    print(f"脚本最多等待 {timeout_seconds // 60} 分钟；成功进入水源首页后会自动继续。\n")
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if page.url.startswith(BASE_URL) and "login" not in page.url:
            return
        page.wait_for_timeout(1000)
    raise RuntimeError("等待登录超时。请重新运行“首次登录水源.bat”。")


def category_maps(data: dict[str, Any]) -> tuple[dict[int, dict[str, Any]], dict[int, str]]:
    roots = list(data.get("category_list", {}).get("categories", []) or [])
    roots.extend(data.get("categories", []) or [])
    categories: list[dict[str, Any]] = []

    def add(items: list[dict[str, Any]], parent_id: int | None = None) -> None:
        for original in items:
            cat = dict(original)
            if cat.get("parent_category_id") is None and parent_id is not None:
                cat["parent_category_id"] = parent_id
            categories.append(cat)
            children = cat.get("subcategory_list") or []
            if children:
                add(children, int(cat["id"]))

    add(roots)
    by_id = {int(c["id"]): c for c in categories if c.get("id") is not None}
    paths: dict[int, str] = {}
    for cid, cat in by_id.items():
        names = [str(cat.get("name") or "未分类")]
        seen = {cid}
        parent = cat.get("parent_category_id")
        while parent is not None and int(parent) in by_id and int(parent) not in seen:
            seen.add(int(parent))
            parent_cat = by_id[int(parent)]
            names.insert(0, str(parent_cat.get("name") or "未分类"))
            parent = parent_cat.get("parent_category_id")
        paths[cid] = " / ".join(names)
    return by_id, paths


def collect_topic_index(page: Any, start: datetime, end: datetime) -> tuple[list[dict[str, Any]], dict[int, str]]:
    selected: dict[int, dict[str, Any]] = {}
    _, paths = category_maps(same_origin_json(page, "/categories.json"))
    # /site.json contains the authoritative flat category table on this
    # Discourse installation, including categories hidden from the category
    # landing page.
    _, site_paths = category_maps(same_origin_json(page, "/site.json"))
    paths.update(site_paths)
    older_streak = 0
    for page_no in range(0, 80):
        data = same_origin_json(page, f"/latest.json?order=created&page={page_no}")
        topics = data.get("topic_list", {}).get("topics", [])
        if not topics:
            break
        times = [parse_time(t.get("created_at")) for t in topics]
        for topic, created in zip(topics, times):
            if created and start <= created < end:
                selected[int(topic["id"])] = topic
        valid = [t for t in times if t]
        if valid and max(valid) < start:
            older_streak += 1
        else:
            older_streak = 0
        if older_streak >= 2:
            break
        time.sleep(0.12)
    return sorted(selected.values(), key=lambda x: x.get("created_at", "")), paths


def collect_updated_topic_index(page: Any, start: datetime, end: datetime) -> tuple[list[dict[str, Any]], dict[int, str]]:
    """Collect topics whose latest visible activity falls in [start, end).

    Discourse's activity-ordered latest feed includes both newly created topics
    and older topics receiving new replies.  This is deliberately different
    from collect_topic_index(), which filters on the first-post timestamp.
    """
    selected: dict[int, dict[str, Any]] = {}
    _, paths = category_maps(same_origin_json(page, "/categories.json"))
    _, site_paths = category_maps(same_origin_json(page, "/site.json"))
    paths.update(site_paths)
    older_streak = 0
    for page_no in range(0, 120):
        data = same_origin_json(page, f"/latest.json?order=activity&page={page_no}")
        topics = data.get("topic_list", {}).get("topics", [])
        if not topics:
            break
        # Track actual content activity. Administrative bumps and pin changes
        # can alter bumped_at without adding or editing a visible post.
        times = [parse_time(t.get("last_posted_at") or t.get("created_at")) for t in topics]
        for topic, activity in zip(topics, times):
            if activity and start <= activity < end:
                selected[int(topic["id"])] = topic
        valid = [t for t in times if t]
        if valid and max(valid) < start:
            older_streak += 1
        else:
            older_streak = 0
        if older_streak >= 2:
            break
        time.sleep(0.12)
    return sorted(
        selected.values(),
        key=lambda x: x.get("last_posted_at") or x.get("created_at") or "",
    ), paths


def normalize_tags(raw: Any) -> list[str]:
    result: list[str] = []
    for tag in raw or []:
        if isinstance(tag, str):
            result.append(tag)
        elif isinstance(tag, dict):
            result.append(str(tag.get("name") or tag.get("id") or ""))
    return [x for x in result if x]


def metadata_diary_reason(topic_meta: dict[str, Any]) -> str:
    title = str(topic_meta.get("title") or "").strip()
    haystack = " ".join([title, *normalize_tags(topic_meta.get("tags"))]).lower()
    for marker in DIARY_MARKERS:
        if marker.lower() in haystack:
            return f"标题或标签含“{marker}”"
    if re.search(r"(?:建一个|求职|推歌|评测|讨论|交流|吐槽|占卜|发疯|投喂)[^\n]{0,30}楼|楼\s*[：:｜|—【]", title, re.IGNORECASE):
        return "标题显示为持续更新楼/讨论楼"
    if re.search(r"楼(?:\s*\d+(?:\.\d+)?)?\s*[！!。.～~…]*\s*$", title, re.IGNORECASE):
        return "标题显示为持续更新楼/讨论楼"
    return ""


def sexual_content_reason(title: str, tags: list[str], body: str = "") -> str:
    normalized_tags = {tag.strip().lower() for tag in tags}
    matched_tags = sorted(normalized_tags & SEXUAL_TAGS)
    if matched_tags:
        return f"标签属于性相关内容：{'、'.join(matched_tags)}"
    haystack = f"{title}\n{body}".lower()
    for marker in SEXUAL_MARKERS:
        if marker.lower() in haystack:
            return f"标题或内容涉及性相关主题（命中“{marker}”）"
    return ""


def metadata_exclusion(topic_meta: dict[str, Any], category_path: str) -> tuple[str, str] | None:
    if "相约鹊桥" in category_path:
        return "matchmaking", "属于“相约鹊桥”板块"
    diary_reason = metadata_diary_reason(topic_meta)
    if diary_reason:
        return "diary", diary_reason
    title = str(topic_meta.get("title") or "").strip()
    sexual_reason = sexual_content_reason(title, normalize_tags(topic_meta.get("tags")))
    if sexual_reason:
        return "sexual", sexual_reason
    return None


def post_in_window(post: dict[str, Any], start: datetime, end: datetime) -> bool:
    created = parse_time(post.get("created_at"))
    updated = parse_time(post.get("updated_at"))
    return bool(
        (created and start <= created < end)
        or (updated and start <= updated < end)
    )


def collect_topic(
    page: Any,
    topic_meta: dict[str, Any],
    category_paths: dict[int, str],
    window_start: datetime | None = None,
    window_end: datetime | None = None,
) -> dict[str, Any]:
    tid = int(topic_meta["id"])
    data = same_origin_json(page, f"/t/{tid}.json", 45_000)
    stream = [int(x) for x in data.get("post_stream", {}).get("stream", [])]
    posts_by_id = {int(p["id"]): p for p in data.get("post_stream", {}).get("posts", []) if p.get("id")}
    errors: list[str] = []

    def fetch_batch(batch: list[int]) -> bool:
        missing_batch = [pid for pid in batch if pid not in posts_by_id]
        if not missing_batch:
            return True
        query = urlencode([("post_ids[]", pid) for pid in missing_batch])
        try:
            extra = same_origin_json(page, f"/t/{tid}/posts.json?{query}", 45_000)
            for post in extra.get("post_stream", {}).get("posts", extra.get("posts", [])):
                if post.get("id"):
                    posts_by_id[int(post["id"])] = post
        except Exception as exc:  # keep the rest of the daily report usable
            errors.append(str(exc))
            return False
        time.sleep(0.10)
        return True

    topic_created = parse_time(data.get("created_at") or topic_meta.get("created_at"))
    scoped = bool(window_start and window_end and topic_created and topic_created < window_start)
    if scoped:
        # For an older topic, retain only the first post and posts created or
        # edited inside the report window. Walk backward from the tail and stop
        # after the first completely old batch, avoiding a full-thread download.
        if stream:
            fetch_batch([stream[0]])
        tail = stream[1:]
        for offset in range(len(tail), 0, -30):
            batch = tail[max(0, offset - 30) : offset]
            if not fetch_batch(batch):
                break
            batch_posts = [posts_by_id[pid] for pid in batch if pid in posts_by_id]
            if batch_posts and not any(post_in_window(p, window_start, window_end) for p in batch_posts):
                break
        selected_ids = {
            pid for pid, post in posts_by_id.items()
            if post_in_window(post, window_start, window_end)
        }
        if stream:
            selected_ids.add(stream[0])
        retained_stream = [pid for pid in stream if pid in selected_ids]
        collection_scope = "first_post_and_window_updates"
    else:
        remaining = [pid for pid in stream if pid not in posts_by_id]
        for batch in chunks(remaining, 30):
            fetch_batch(batch)
        retained_stream = stream
        collection_scope = "full_stream"

    ordered_posts: list[dict[str, Any]] = []
    for pid in retained_stream:
        post = posts_by_id.get(pid)
        if not post:
            continue
        ordered_posts.append(
            {
                "id": pid,
                "post_number": post.get("post_number"),
                "username": post.get("username"),
                "created_at": post.get("created_at"),
                "updated_at": post.get("updated_at"),
                "reply_to_post_number": post.get("reply_to_post_number"),
                "text": html_to_text(post.get("cooked")),
            }
        )
    missing = [pid for pid in retained_stream if pid not in posts_by_id]
    category_id = int(data.get("category_id") or topic_meta.get("category_id") or 0)
    category_path = category_paths.get(category_id, "未分类")
    title = str(data.get("title") or topic_meta.get("title") or f"主题 {tid}")
    slug = str(data.get("slug") or topic_meta.get("slug") or "topic")
    return {
        "id": tid,
        "title": title,
        "url": f"{BASE_URL}/t/{slug}/{tid}",
        "category_id": category_id,
        "category_path": category_path,
        "main_category": category_path.split(" / ")[0],
        "subcategory": category_path.split(" / ")[-1],
        "tags": normalize_tags(data.get("tags") or topic_meta.get("tags")),
        "created_at": data.get("created_at") or topic_meta.get("created_at"),
        "last_posted_at": data.get("last_posted_at") or topic_meta.get("last_posted_at"),
        "posts_count": int(data.get("posts_count") or topic_meta.get("posts_count") or len(stream)),
        "reply_count": max(0, int(data.get("posts_count") or topic_meta.get("posts_count") or len(stream)) - 1),
        "views": int(data.get("views") or topic_meta.get("views") or 0),
        "stream_count": len(stream),
        "collection_scope": collection_scope,
        "intentionally_skipped_historical_posts": max(0, len(stream) - len(retained_stream)) if scoped else 0,
        "posts": ordered_posts,
        "missing_post_ids": missing,
        "errors": errors,
        "verification": (
            "首帖及窗口内可识别更新已采集"
            if scoped and not missing and not errors
            else ("全部可访问回复已采集" if not missing and not errors else "部分采集")
        ),
    }


def representative_replies(posts: list[dict[str, Any]], limit: int = 4) -> list[str]:
    candidates: list[tuple[float, str]] = []
    for idx, post in enumerate(posts[1:], 1):
        text = re.sub(r"\s+", " ", post.get("text", "")).strip()
        if not text or text.lower() in NOISE_REPLIES or len(text) < 8:
            continue
        score = min(len(text), 260) + (35 if re.search(r"建议|应该|可以|不妨|最好|推荐|因为|但是|不过|未必|不同意|结果|解决", text) else 0)
        score += 8 if idx > len(posts) * 0.65 else 0
        candidates.append((score, text))
    chosen: list[str] = []
    for _, text in sorted(candidates, reverse=True):
        short = clip(text, 220)
        if all(short[:35] not in old and old[:35] not in short for old in chosen):
            chosen.append(short)
        if len(chosen) >= limit:
            break
    return chosen


def auto_summary(topic: dict[str, Any]) -> list[str]:
    posts = topic["posts"]
    if not posts:
        return ["未能读取正文。"]
    scoped = topic.get("collection_scope") == "first_post_and_window_updates"
    lines = [f"首帖：{clip(posts[0].get('text', ''), 520) or '（仅图片或附件，未提取到文字）'}"]
    replies = posts[1:]
    if not replies:
        lines.append("本窗口更新：仅检测到首帖编辑，没有新增回复。" if scoped else "讨论：抓取时尚无可访问回复。")
        return lines
    reps = representative_replies(posts)
    if reps:
        lines.append(("本窗口更新中的代表性信息：" if scoped else "讨论中的代表性信息：") + "；".join(reps))
    else:
        prefix = "本窗口更新" if scoped else "讨论"
        lines.append(f"{prefix}：共 {len(replies)} 条可访问回复，多为短回复或图片，自动文本摘要信息有限。")
    tail = [p.get("text", "") for p in posts[-4:] if p.get("text")]
    if tail:
        lines.append(("本窗口末段进展：" if scoped else "末段进展：") + clip("；".join(tail), 360))
    return lines


def is_diary(topic: dict[str, Any]) -> tuple[bool, str]:
    title = topic.get("title", "").strip()
    haystack = " ".join([title, *topic.get("tags", [])]).lower()
    for marker in DIARY_MARKERS:
        if marker.lower() in haystack:
            return True, f"标题或标签含“{marker}”"
    if re.search(r"(?:建一个|求职|推歌|评测|讨论|交流|吐槽|占卜|发疯|投喂)[^\n]{0,30}楼|楼\s*[：:｜|—【]", title, re.IGNORECASE):
        return True, "标题显示为持续更新楼/讨论楼"
    if re.search(r"楼(?:\s*\d+(?:\.\d+)?)?\s*[！!。.～~…]*\s*$", title, re.IGNORECASE):
        return True, "标题显示为持续更新楼/讨论楼"
    first = topic.get("posts", [{}])[0].get("text", "") if topic.get("posts") else ""
    if re.search(r"长期记录|持续更新|每日记录|开个楼|本楼用于", first[:800]):
        return True, "首帖显示为长期连续记录/水楼"
    return False, ""


def topic_exclusion(topic: dict[str, Any]) -> tuple[str, str] | None:
    if "相约鹊桥" in topic.get("category_path", ""):
        return "matchmaking", "属于“相约鹊桥”板块"
    diary, diary_reason = is_diary(topic)
    if diary:
        return "diary", diary_reason
    body = "\n".join(str(post.get("text") or "") for post in topic.get("posts", []))
    sexual_reason = sexual_content_reason(topic.get("title", ""), topic.get("tags", []), body)
    if sexual_reason:
        return "sexual", sexual_reason
    return None


def mark_window_updates(topic: dict[str, Any], start: datetime, end: datetime) -> None:
    new_numbers: list[int] = []
    edited_numbers: list[int] = []
    for post in topic.get("posts", []):
        number = post.get("post_number")
        created = parse_time(post.get("created_at"))
        updated = parse_time(post.get("updated_at"))
        if number is None:
            continue
        if created and start <= created < end:
            new_numbers.append(int(number))
        elif updated and start <= updated < end:
            edited_numbers.append(int(number))
    topic["window_new_post_numbers"] = new_numbers
    topic["window_edited_post_numbers"] = edited_numbers
    topic["window_update_count"] = len(new_numbers) + len(edited_numbers)


def focus_hits(topic: dict[str, Any]) -> list[str]:
    return [tag for tag in topic.get("tags", []) if tag.lower() in FOCUS_TAGS]


def render_report(
    topics: list[dict[str, Any]],
    start: datetime,
    end: datetime,
    fetched_at: datetime,
    mode: str = "new_topics",
    excluded_topics: list[dict[str, Any]] | None = None,
) -> str:
    activity_report = mode in {"today_activity", "incremental_activity"}
    incremental = mode == "incremental_activity"
    exclusion_counts = Counter(item.get("kind", "other") for item in (excluded_topics or []))
    counts = Counter(t["main_category"] for t in topics)
    high: list[dict[str, Any]] = []
    for topic in topics:
        diary, reason = is_diary(topic)
        topic["diary_excluded"] = diary
        topic["diary_reason"] = reason
        if topic["views"] > 1000 and not diary:
            high.append(topic)
    high.sort(key=lambda t: (t["views"], t["reply_count"]), reverse=True)
    report_name = "水源社区增量日报" if incremental else ("水源社区今日日报" if mode == "today_activity" else "水源社区日报")
    topic_count_label = "筛选后有更新的主题数" if activity_report else "新主题总数"
    high_title = "本窗口更新主题中的高阅读量（严格 > 1,000，已排除指定内容）" if activity_report else "高阅读量主题（严格 > 1,000，非日记/水楼）"
    lines = [
        f"# {report_name}（{end:%Y-%m-%d %H:%M}）",
        "",
        f"- 统计窗口（北京时间）：{start:%Y-%m-%d %H:%M:%S}（含）—{end:%Y-%m-%d %H:%M:%S}（不含）",
        f"- 抓取完成时间：{fetched_at:%Y-%m-%d %H:%M:%S}",
        f"- {topic_count_label}：{len(topics)}",
        "- 大类别数量：" + ("；".join(f"{k} {v}" for k, v in sorted(counts.items())) if counts else "无"),
        f"- 阅读量严格超过 1,000 的筛选后主题：{len(high)}",
        ("- 核验口径：窗口前已发布的旧主题只采集首帖和本窗口内可识别的新增/编辑楼层；窗口内新建主题采集其完整可访问楼层。"
         if activity_report else
         "- 核验口径：正文与回复来自站内主题结构化数据；“全部可访问回复已采集”表示已覆盖主题返回的完整 post stream。"),
        *( [
            "- 本窗口排除主题："
            f"共 {len(excluded_topics or [])}；日记/水楼 {exclusion_counts.get('diary', 0)}；"
            f"相约鹊桥 {exclusion_counts.get('matchmaking', 0)}；性相关 {exclusion_counts.get('sexual', 0)}"
        ] if activity_report else [] ),
        "",
        "## 重点关注",
        "",
    ]
    focus = [t for t in topics if t["main_category"] in FOCUS_CATEGORIES or focus_hits(t)]
    if not focus:
        lines.append("本窗口没有命中重点类别或重点标签的主题。")
    for topic in focus:
        tags = "、".join(topic["tags"]) or "无"
        hit = "、".join(focus_hits(topic)) or "—"
        lines.append(f"- [{md_escape(topic['title'])}]({topic['url']})｜{topic['category_path']}｜标签：{md_escape(tags)}｜重点标签：{md_escape(hit)}｜{topic['views']} 阅读 / {topic['reply_count']} 回复")
    lines += ["", f"## {high_title}", ""]
    if not high:
        lines.append("没有符合条件的主题。")
    for topic in high:
        lines.append(f"- [{md_escape(topic['title'])}]({topic['url']})｜{topic['views']} 阅读 / {topic['reply_count']} 回复｜{topic['category_path']}｜见下方完整条目")
    for category in sorted(counts, key=lambda c: (c not in FOCUS_CATEGORIES, c)):
        lines += ["", f"## {'⭐ ' if category in FOCUS_CATEGORIES else ''}{category}（{counts[category]}）", ""]
        for topic in [x for x in topics if x["main_category"] == category]:
            tags = "、".join(topic["tags"]) or "无"
            lines += [
                f"### [{md_escape(topic['title'])}]({topic['url']})",
                "",
                f"- 主题 ID：{topic['id']}；板块：{topic['category_path']}；标签：{md_escape(tags)}",
                f"- 首次发布时间：{fmt_time(topic['created_at'])}；最后活动：{fmt_time(topic['last_posted_at'])}",
                (
                    f"- 阅读量：{topic['views']}；回复数：{topic['reply_count']}；采集：首帖 + 本窗口更新，共 {len(topic['posts'])} 条（全楼 {topic['stream_count']} 条）；状态：{topic['verification']}"
                    if topic.get("collection_scope") == "first_post_and_window_updates" else
                    f"- 阅读量：{topic['views']}；回复数：{topic['reply_count']}；已读取：{len(topic['posts'])}/{topic['stream_count']} 个可访问帖子；状态：{topic['verification']}"
                ),
            ]
            if activity_report:
                new_numbers = topic.get("window_new_post_numbers", [])
                edited_numbers = topic.get("window_edited_post_numbers", [])
                lines.append(
                    f"- 本窗口更新：新增楼层 {new_numbers or '无'}；编辑楼层 {edited_numbers or '无'}；共 {topic.get('window_update_count', 0)} 条可识别更新"
                )
            for paragraph in auto_summary(topic):
                lines.append(f"- {paragraph}")
            if topic["missing_post_ids"] or topic["errors"]:
                lines.append(f"- 限制：缺失 post ID {topic['missing_post_ids'] or '无'}；错误：{md_escape('；'.join(topic['errors']) or '无')}")
            lines.append("")
    partial = [t for t in topics if t["verification"] in {"部分采集", "未核验"}]
    excluded = [t for t in topics if t.get("diary_excluded") and t["views"] > 1000]
    lines += ["## 可能遗漏 / 访问限制", ""]
    if not partial:
        lines.append("本次没有发现 post stream 内的缺失回复。删除、隐藏或无权限且未出现在 post stream 的内容无法识别。")
    else:
        for topic in partial:
            lines.append(f"- [{md_escape(topic['title'])}]({topic['url']})（ID {topic['id']}）：已读取 {len(topic['posts'])}/{topic['stream_count']}；缺失 {topic['missing_post_ids'] or '未明确'}；{md_escape('；'.join(topic['errors']))}")
    if excluded and not activity_report:
        lines += ["", "### 阅读量超过 1,000 但因日记/水楼排除", ""]
        for topic in excluded:
            lines.append(f"- [{md_escape(topic['title'])}]({topic['url']})：{topic['views']} 阅读；判断理由：{topic['diary_reason']}")
    lines += ["", "## 文件说明", "", "同目录 `*_完整资料.json` 保存全部已采集楼层文本，可交给 ChatGPT 做更深入的主流观点、分歧和结论归纳。", ""]
    return "\n".join(lines)


def latest_completed_end(output_dir: Path) -> datetime | None:
    state_path = output_dir / "增量状态.json"
    if state_path.is_file():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            value = parse_time(state.get("last_successful_end"))
            if value:
                return value
        except (OSError, json.JSONDecodeError):
            pass
    candidates = sorted(output_dir.glob("*_完整资料.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for candidate in candidates:
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
            value = parse_time(payload.get("window", {}).get("end"))
            if value:
                return value
        except (OSError, json.JSONDecodeError):
            continue
    return None


def run(args: argparse.Namespace) -> tuple[Path, Path]:
    now = datetime.now(SHANGHAI).replace(microsecond=0)
    end = parse_time(args.end) if args.end else now
    if end is None:
        raise RuntimeError("--end 必须是 ISO 时间，例如 2026-09-28T10:10:00+08:00")
    root = Path(__file__).resolve().parent
    output_dir = root / "水源日报输出"
    output_dir.mkdir(parents=True, exist_ok=True)
    if args.incremental:
        start = latest_completed_end(output_dir) or (end - timedelta(hours=args.hours))
        if start >= end:
            start = end - timedelta(hours=args.hours)
    elif args.today:
        start = end.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        start = end - timedelta(hours=args.hours)
    activity_mode = args.incremental or args.today
    profile_dir = root / ".shuiyuan_edge_profile"
    edge = find_edge()

    print(f"统计窗口：{start:%Y-%m-%d %H:%M:%S} — {end:%Y-%m-%d %H:%M:%S}（北京时间）")
    print("正在启动专用 Edge 会话（直连，不使用系统代理）……")
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(profile_dir),
            executable_path=edge,
            headless=not args.login,
            args=["--no-proxy-server", "--proxy-bypass-list=*", "--disable-background-networking"],
            viewport={"width": 1280, "height": 900},
        )
        page = context.pages[0] if context.pages else context.new_page()
        try:
            ensure_login(page, allow_wait=args.login)
        except Exception:
            if not args.login and not args.no_interactive:
                context.close()
                print("登录态不存在或已过期，改为显示浏览器供你登录……")
                context = p.chromium.launch_persistent_context(
                    str(profile_dir), executable_path=edge, headless=False,
                    args=["--no-proxy-server", "--proxy-bypass-list=*"], viewport={"width": 1280, "height": 900},
                )
                page = context.pages[0] if context.pages else context.new_page()
                ensure_login(page, allow_wait=True)
            else:
                raise
        print("正在获取窗口内有活动的主题清单……" if activity_mode else "正在获取窗口内主题清单……")
        if activity_mode:
            topics_meta, category_paths = collect_updated_topic_index(page, start, end)
        else:
            topics_meta, category_paths = collect_topic_index(page, start, end)
        noun = "个有更新的主题" if activity_mode else "个新主题"
        print(f"发现 {len(topics_meta)} {noun}，开始逐楼采集……")
        topics: list[dict[str, Any]] = []
        excluded_topics: list[dict[str, Any]] = []
        for index, meta in enumerate(topics_meta, 1):
            tid = meta.get("id")
            title = meta.get("title") or ""
            category_id = int(meta.get("category_id") or 0)
            category_path = category_paths.get(category_id, "未分类")
            early_exclusion = metadata_exclusion(meta, category_path) if activity_mode else None
            if early_exclusion:
                exclusion_kind, exclusion_reason = early_exclusion
                excluded_topics.append({
                    "id": int(tid),
                    "title": str(title or f"主题 {tid}"),
                    "url": f"{BASE_URL}/t/topic/{tid}",
                    "kind": exclusion_kind,
                    "reason": exclusion_reason,
                })
                print(f"[{index}/{len(topics_meta)}] 跳过已排除主题 {tid} {clip(str(title), 42)}：{exclusion_reason}")
                continue
            print(f"[{index}/{len(topics_meta)}] {tid} {clip(str(title), 54)}")
            try:
                topics.append(collect_topic(page, meta, category_paths, start, end) if activity_mode else collect_topic(page, meta, category_paths))
            except Exception as exc:
                category_id = int(meta.get("category_id") or 0)
                path = category_paths.get(category_id, "未分类")
                topics.append({
                    "id": int(tid), "title": str(title or f"主题 {tid}"),
                    "url": f"{BASE_URL}/t/topic/{tid}", "category_id": category_id,
                    "category_path": path, "main_category": path.split(" / ")[0],
                    "subcategory": path.split(" / ")[-1], "tags": normalize_tags(meta.get("tags")),
                    "created_at": meta.get("created_at"), "last_posted_at": meta.get("last_posted_at"),
                    "posts_count": int(meta.get("posts_count") or 0),
                    "reply_count": max(0, int(meta.get("posts_count") or 0) - 1),
                    "views": int(meta.get("views") or 0), "stream_count": 0,
                    "collection_scope": "first_post_and_window_updates" if activity_mode else "full_stream",
                    "intentionally_skipped_historical_posts": 0, "posts": [],
                    "missing_post_ids": [], "errors": [str(exc)], "verification": "未核验",
                })
            time.sleep(0.15)
        context.close()

    ignored_without_post_update: list[dict[str, Any]] = []
    if activity_mode:
        kept: list[dict[str, Any]] = []
        for topic in topics:
            mark_window_updates(topic, start, end)
            if topic["window_update_count"] == 0:
                ignored_without_post_update.append({
                    "id": topic["id"],
                    "title": topic["title"],
                    "url": topic["url"],
                    "reason": "本窗口未核验到新增或编辑楼层",
                })
                continue
            exclusion = topic_exclusion(topic)
            if exclusion:
                exclusion_kind, exclusion_reason = exclusion
                excluded_topics.append({
                    "id": topic["id"], "title": topic["title"], "url": topic["url"],
                    "kind": exclusion_kind, "reason": exclusion_reason,
                })
                continue
            kept.append(topic)
        topics = kept

    stamp = end.strftime("%Y-%m-%d_%H%M")
    prefix = "水源增量日报" if args.incremental else "水源日报"
    mode = "incremental_activity" if args.incremental else ("today_activity" if args.today else "new_topics")
    json_path = output_dir / f"{prefix}_{stamp}_完整资料.json"
    md_path = output_dir / f"{prefix}_{stamp}.md"
    payload = {
        "window": {"start": start.isoformat(), "end": end.isoformat(), "timezone": "Asia/Shanghai"},
        "fetched_at": datetime.now(SHANGHAI).isoformat(),
        "mode": mode,
        "excluded_topics": excluded_topics,
        "excluded_diaries": [item for item in excluded_topics if item.get("kind") == "diary"],
        "ignored_without_post_update": ignored_without_post_update,
        "topics": topics,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(
        render_report(topics, start, end, datetime.now(SHANGHAI), mode, excluded_topics),
        encoding="utf-8-sig",
    )
    if args.incremental:
        state_path = output_dir / "增量状态.json"
        state_path.write_text(
            json.dumps(
                {"last_successful_end": end.isoformat(), "last_json": str(json_path), "updated_at": datetime.now(SHANGHAI).isoformat()},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    return md_path, json_path


def establish_login_session() -> None:
    """Open the dedicated Edge profile and only establish/refresh login state."""
    root = Path(__file__).resolve().parent
    profile_dir = root / ".shuiyuan_edge_profile"
    edge = find_edge()
    print("正在启动水源专用 Edge 登录窗口（本次不会采集帖子）……")
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(profile_dir),
            executable_path=edge,
            headless=False,
            args=["--no-proxy-server", "--proxy-bypass-list=*"],
            viewport={"width": 1280, "height": 900},
        )
        page = context.pages[0] if context.pages else context.new_page()
        ensure_login(page, allow_wait=True)
        context.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="生成水源社区今日日报或增量更新日报")
    parser.add_argument("--hours", type=float, default=24.0, help="统计时长，默认 24 小时")
    parser.add_argument("--end", help="固定截止时间（ISO 8601）；默认脚本启动时刻")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--today", action="store_true", help="统计北京时间当天 00:00 后新建或有更新的主题")
    mode_group.add_argument("--incremental", action="store_true", help="按上次成功运行后有活动的主题生成增量日报；首次回退到 --hours")
    mode_group.add_argument("--login-only", action="store_true", help="仅建立或刷新水源登录会话，不采集帖子")
    parser.add_argument("--login", action="store_true", help="显示浏览器并建立/刷新登录会话")
    parser.add_argument("--no-interactive", action="store_true", help="登录失效时直接失败，不弹出浏览器（供定时任务使用）")
    args = parser.parse_args()
    try:
        if args.login_only:
            establish_login_session()
            print("\n登录会话已就绪。本次未采集帖子，也未生成日报。")
            return 0
        md_path, json_path = run(args)
        print("\n完成：")
        print(f"日报：{md_path}")
        print(f"完整资料：{json_path}")
        return 0
    except (RuntimeError, PlaywrightError, PlaywrightTimeoutError) as exc:
        print(f"\n执行失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
