#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在 GitHub Actions（境外网络）上抓取 YouTube 热门与 TikTok 趋势，输出 JSON。

设计原则：
  - 不依赖任何 API Key；
  - 每个源独立 try，失败不影响其它源；
  - 失败时保留上一次成功的文件（workflow 里只覆盖抓取成功的）；
  - 输出结构统一：{"source": "...", "fetched_at": "...", "items": [{"title","url","rank"}]}
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
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)
OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def save(name: str, items: list[dict]) -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    payload = {"source": name, "fetched_at": now_iso(), "items": items[:50]}
    path = os.path.join(OUT_DIR, f"{name}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)


def fetch_youtube() -> list[dict]:
    """抓 YouTube 热门页，从 ytInitialData 里提取视频标题。"""
    url = "https://www.youtube.com/feed/trending?gl=US&hl=en"
    resp = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}, timeout=30)
    resp.raise_for_status()

    m = re.search(r"var ytInitialData = (\{.*?\});</script>", resp.text, re.S)
    if not m:
        m = re.search(r"ytInitialData\"\]\s*=\s*(\{.*?\});", resp.text, re.S)
    if not m:
        raise RuntimeError("未找到 ytInitialData")

    data = json.loads(m.group(1))
    items: list[dict] = []
    seen: set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            if "videoRenderer" in node:
                v = node["videoRenderer"]
                title = ""
                try:
                    title = v["title"]["runs"][0]["text"]
                except Exception:
                    title = v.get("title", {}).get("simpleText", "") if isinstance(v.get("title"), dict) else ""
                vid = v.get("videoId")
                if title and vid and vid not in seen:
                    seen.add(vid)
                    items.append({
                        "title": title.strip(),
                        "url": f"https://www.youtube.com/watch?v={vid}",
                        "rank": len(items) + 1,
                    })
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(data)
    if not items:
        raise RuntimeError("ytInitialData 中未解析出视频")
    return items


def fetch_tiktok() -> list[dict]:
    """抓 TikTok Creative Center 的热门话题榜（无需登录，境外 IP 可用）。"""
    url = (
        "https://ads.tiktok.com/creative_radar_api/v1/popular_trend/hashtag/list"
        "?page=1&limit=30&period=7&country_code=US&sort_by=popular"
    )
    headers = {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://ads.tiktok.com/business/creativecenter/inspiration/popular/hashtag/pc/en",
        "Accept-Language": "en-US,en;q=0.9",
    }
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()

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
        raise RuntimeError("Creative Center 未返回话题")
    return items


def main() -> int:
    results: dict[str, str] = {}

    for name, fn in (("youtube", fetch_youtube), ("tiktok", fetch_tiktok)):
        for attempt in (1, 2):
            try:
                items = fn()
                save(name, items)
                results[name] = f"ok({len(items)})"
                break
            except Exception as exc:  # noqa: BLE001
                results[name] = f"fail:{type(exc).__name__}:{str(exc)[:80]}"
                if attempt == 1:
                    time.sleep(5)

    print("抓取结果:", json.dumps(results, ensure_ascii=False))
    # 两个都失败才算整体失败，否则保留已成功的
    return 0 if any(v.startswith("ok") for v in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
