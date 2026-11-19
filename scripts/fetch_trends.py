#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在 GitHub Actions（境外网络）上抓取 YouTube 热门与 TikTok 趋势，输出 JSON。

设计原则：
  - 不依赖任何 API Key；
  - 每个源独立 try，失败不影响其它源；
  - 失败时保留上一次成功的文件，并把诊断信息写进 data/_status.json；
  - 整体永远返回 0，这样 workflow 的「提交」步骤一定会跑，出错原因能被拉回来查。

输出结构：{"source": "...", "fetched_at": "...", "items": [{"title","url","rank"}]}
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

# YouTube 网页端公开的 Innertube key（前端 JS 里就有，不是用户凭证）
INNERTUBE_KEY = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def save(name: str, items: list[dict]) -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    payload = {"source": name, "fetched_at": now_iso(), "items": items[:50]}
    path = os.path.join(OUT_DIR, f"{name}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)


def walk_titles(data) -> list[dict]:
    """从任意 YT 数据结构里收集视频：兼容 videoRenderer / lockupViewModel 等新旧结构。"""
    items: list[dict] = []
    seen: set[str] = set()

    def add(vid: str, title: str) -> None:
        title = (title or "").strip()
        if not vid or not title or vid in seen:
            return
        seen.add(vid)
        items.append({
            "title": title,
            "url": f"https://www.youtube.com/watch?v={vid}",
            "rank": len(items) + 1,
        })

    def text_of(node) -> str:
        """把 YT 各种 title 结构压成字符串。"""
        if isinstance(node, str):
            return node
        if not isinstance(node, dict):
            return ""
        if isinstance(node.get("simpleText"), str):
            return node["simpleText"]
        if isinstance(node.get("content"), str):
            return node["content"]
        runs = node.get("runs")
        if isinstance(runs, list) and runs:
            return "".join(str(r.get("text", "")) for r in runs if isinstance(r, dict))
        return ""

    def walk(node) -> None:
        if isinstance(node, dict):
            # 旧结构
            if "videoRenderer" in node and isinstance(node["videoRenderer"], dict):
                v = node["videoRenderer"]
                add(v.get("videoId", ""), text_of(v.get("title")))
            # 新结构（2026 年热门页已改成 lockupViewModel）
            if "lockupViewModel" in node and isinstance(node["lockupViewModel"], dict):
                v = node["lockupViewModel"]
                vid = v.get("contentId", "")
                title = ""
                meta = v.get("metadata", {}) or {}
                lm = meta.get("lockupMetadataViewModel", {}) or {}
                title = text_of(lm.get("title"))
                if not title:
                    rows = ((lm.get("metadata", {}) or {}).get("contentMetadataViewModel", {}) or {}).get("metadataRows")
                    if isinstance(rows, list):
                        for row in rows:
                            for part in (row or {}).get("metadataParts", []) or []:
                                t = text_of((part or {}).get("text"))
                                if t and not t.endswith("views") and not re.match(r"^[\d.,KMB]+", t):
                                    title = t
                                    break
                            if title:
                                break
                add(vid, title)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(data)
    return items


def fetch_youtube() -> list[dict]:
    """抓 YouTube 热门：先解析热门页，失败再走 Innertube browse 接口。"""
    session = requests.Session()
    session.headers.update({
        "User-Agent": UA,
        "Accept-Language": "en-US,en;q=0.9",
        "Cookie": "CONSENT=YES+1; SOCS=CAI",
    })

    errors: list[str] = []

    # 路线 1：热门页 HTML 里的 ytInitialData
    try:
        resp = session.get("https://www.youtube.com/feed/trending?gl=US&hl=en", timeout=30)
        resp.raise_for_status()
        html = resp.text
        match = re.search(r"var ytInitialData\s*=\s*(\{.*?\});</script>", html, re.S)
        if not match:
            match = re.search(r'ytInitialData"\]\s*=\s*(\{.*?\});', html, re.S)
        if match:
            items = walk_titles(json.loads(match.group(1)))
            if items:
                return items
            errors.append("ytInitialData 里没有视频节点（len=%d）" % len(html))
        else:
            errors.append("页面里没找到 ytInitialData（len=%d）" % len(html))
    except Exception as exc:  # noqa: BLE001
        errors.append("热门页 %s: %s" % (type(exc).__name__, str(exc)[:80]))

    # 路线 2：Innertube browse API（FEtrending）
    try:
        url = f"https://www.youtube.com/youtubei/v1/browse?key={INNERTUBE_KEY}&prettyPrint=false"
        body = {
            "context": {
                "client": {
                    "clientName": "WEB",
                    "clientVersion": "2.20241101.00.00",
                    "hl": "en",
                    "gl": "US",
                }
            },
            "browseId": "FEtrending",
        }
        resp = session.post(url, json=body, timeout=30)
        resp.raise_for_status()
        items = walk_titles(resp.json())
        if items:
            return items
        errors.append("Innertube 返回里没有视频（len=%d）" % len(resp.text))
    except Exception as exc:  # noqa: BLE001
        errors.append("Innertube %s: %s" % (type(exc).__name__, str(exc)[:80]))

    raise RuntimeError(" | ".join(errors)[:600])


def fetch_tiktok() -> list[dict]:
    """抓 TikTok Creative Center 的热门话题榜（无需登录，境外 IP 可用）。"""
    base = "https://ads.tiktok.com/creative_radar_api/v1/popular_trend/hashtag/list"
    params = {"page": 1, "limit": 30, "period": 7, "country_code": "US", "sort_by": "popular"}
    headers = {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://ads.tiktok.com/business/creativecenter/inspiration/popular/hashtag/pc/en",
    }
    resp = requests.get(base, params=params, headers=headers, timeout=30)
    snippet = resp.text[:300].replace("\n", " ")
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}: {snippet}")

    try:
        data = resp.json()
    except ValueError:
        raise RuntimeError(f"返回不是 JSON: {snippet}")

    rows = ((data.get("data") or {}).get("list")) or []
    items: list[dict] = []
    for i, row in enumerate(rows, start=1):
        name = str(row.get("hashtag_name") or row.get("hashtag") or "").strip()
        if not name:
            continue
        items.append({
            "title": f"#{name}",
            "url": f"https://www.tiktok.com/tag/{name}",
            "rank": i,
            "posts": row.get("publish_cnt") or row.get("video_cnt"),
        })
    if not items:
        raise RuntimeError(f"list 为空，返回片段: {snippet}")
    return items


def main() -> int:
    status: dict[str, dict] = {}

    for name, fn in (("youtube", fetch_youtube), ("tiktok", fetch_tiktok)):
        last_error = ""
        for attempt in (1, 2):
            try:
                items = fn()
                save(name, items)
                status[name] = {"ok": True, "count": len(items)}
                print(f"{name}: ok, {len(items)} 条", flush=True)
                break
            except Exception as exc:  # noqa: BLE001
                last_error = f"{type(exc).__name__}: {exc}"
                print(f"{name}: 第 {attempt} 次失败 {last_error}", flush=True)
                if attempt == 1:
                    time.sleep(5)
        else:
            status[name] = {"ok": False, "error": last_error[:800]}

    # 诊断文件：即使两个源都挂了，也能从仓库里看到原因
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "_status.json"), "w", encoding="utf-8") as fh:
        json.dump({"checked_at": now_iso(), "sources": status}, fh, ensure_ascii=False, indent=2)

    print("抓取结果:", json.dumps(status, ensure_ascii=False), flush=True)
    # 永远返回 0：让「提交数据」步骤跑起来，诊断信息才能回传
    return 0


if __name__ == "__main__":
    sys.exit(main())
