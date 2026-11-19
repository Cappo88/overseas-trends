#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在 GitHub Actions（境外网络）上抓取 YouTube 热门与海外热搜，输出 JSON。

数据源（每个都独立 try，失败不影响其它）：
  youtube  : Innertube browse → Piped 公共实例 → Invidious 公共实例 → 热门页 HTML
  trends   : Google Trends 每日热搜 RSS（新地址 /trending/rss）
  tiktok   : Creative Center 热门话题 API（数据中心 IP 常被拒，失败就留空不影响其它）

约定：
  - 失败时保留上一次成功的数据文件，不覆盖成空；
  - 每次运行都写 data/_status.json，记录每个源的成功/失败原因；
  - 整体永远返回 0，让 workflow 的提交步骤一定跑，诊断信息才能回传。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import requests

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

INNERTUBE_KEY = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8"
CLIENT_VERSION = "2.20241101.00.00"

PIPED = ["https://pipedapi.kavin.rocks", "https://api.piped.private.coffee", "https://pipedapi.adminforge.de"]
INVIDIOUS = ["https://inv.nadeko.net", "https://invidious.nerdvpn.de", "https://yewtu.be"]


def piped_instances() -> list[str]:
    """从 Piped 官方实例目录拉可用 API 列表，硬编码的那几个只作兜底。"""
    try:
        resp = requests.get("https://piped-instances.kavin.rocks/", timeout=20,
                            headers={"User-Agent": UA, "Accept": "application/json"})
        if resp.status_code == 200:
            found = [row.get("api_url", "").rstrip("/") for row in resp.json()
                     if row.get("api_url") and not row.get("locations", "").lower().startswith("tor")]
            urls = [u for u in found if u.startswith("https://")]
            if urls:
                return urls[:8]
    except Exception:  # noqa: BLE001
        pass
    return PIPED


def invidious_instances() -> list[str]:
    """从 api.invidious.io 拉实例列表，优先 api=true 的。"""
    try:
        resp = requests.get("https://api.invidious.io/instances.json?sort_by=health", timeout=20,
                            headers={"User-Agent": UA})
        if resp.status_code == 200:
            urls = []
            for _name, info in resp.json():
                uri = (info or {}).get("uri", "")
                if (info or {}).get("api") and uri.startswith("https://"):
                    urls.append(uri.rstrip("/"))
            if urls:
                return urls[:8]
    except Exception:  # noqa: BLE001
        pass
    return INVIDIOUS


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def save(name: str, items: list[dict]) -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    payload = {"source": name, "fetched_at": now_iso(), "items": items[:50]}
    with open(os.path.join(OUT_DIR, f"{name}.json"), "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)


def save_text(name: str, text: str) -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, name), "w", encoding="utf-8") as fh:
        fh.write(text)


def text_of(node) -> str:
    """把 YouTube 各种 title 结构压成字符串。"""
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


def walk_titles(data) -> list[dict]:
    """从任意 YT 数据结构里收集视频，兼容 videoRenderer / lockupViewModel。"""
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

    def walk(node) -> None:
        if isinstance(node, dict):
            if isinstance(node.get("videoRenderer"), dict):
                v = node["videoRenderer"]
                add(v.get("videoId", ""), text_of(v.get("title")))
            if isinstance(node.get("lockupViewModel"), dict):
                v = node["lockupViewModel"]
                lm = (v.get("metadata") or {}).get("lockupMetadataViewModel") or {}
                title = text_of(lm.get("title"))
                if not title:
                    rows = ((lm.get("metadata") or {}).get("contentMetadataViewModel") or {}).get("metadataRows")
                    for row in rows or []:
                        for part in (row or {}).get("metadataParts", []) or []:
                            cand = text_of((part or {}).get("text"))
                            if cand and not cand.lower().endswith("views") and not re.match(r"^[\d.,KMB]+", cand):
                                title = cand
                                break
                        if title:
                            break
                add(v.get("contentId", ""), title)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(data)
    return items


def yt_from_innertube() -> list[dict]:
    body = {
        "context": {"client": {
            "clientName": "WEB", "clientVersion": CLIENT_VERSION,
            "hl": "en", "gl": "US",
        }},
        "browseId": "FEtrending",
    }
    resp = requests.post(
        f"https://www.youtube.com/youtubei/v1/browse?key={INNERTUBE_KEY}&prettyPrint=false",
        json=body,
        headers={
            "User-Agent": UA,
            "Content-Type": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "X-Youtube-Client-Name": "1",
            "X-Youtube-Client-Version": CLIENT_VERSION,
            "Origin": "https://www.youtube.com",
            "Referer": "https://www.youtube.com/",
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:160]}")
    items = walk_titles(resp.json())
    if not items:
        raise RuntimeError(f"返回里没有视频 (len={len(resp.text)})")
    return items


def yt_from_piped() -> list[dict]:
    last = ""
    for base in piped_instances():
        try:
            resp = requests.get(f"{base}/trending?region=US", timeout=15,
                                headers={"User-Agent": UA, "Accept": "application/json"})
            if resp.status_code != 200:
                last = f"{base} HTTP {resp.status_code}"
                continue
            items = []
            for i, row in enumerate(resp.json().get("items") or [], start=1):
                title = str(row.get("title") or "").strip()
                url = str(row.get("url") or "")
                vid = url.split("v=")[-1] if "v=" in url else url.rsplit("/", 1)[-1]
                if title and vid:
                    items.append({
                        "title": title, "rank": i,
                        "url": f"https://www.youtube.com/watch?v={vid}",
                    })
            if items:
                return items
            last = f"{base} 空列表"
        except Exception as exc:  # noqa: BLE001
            last = f"{base} {type(exc).__name__}: {str(exc)[:60]}"
    raise RuntimeError(last or "所有 Piped 实例都失败")


def yt_from_invidious() -> list[dict]:
    last = ""
    for base in invidious_instances():
        try:
            resp = requests.get(f"{base}/api/v1/trending?region=US", timeout=15,
                                headers={"User-Agent": UA, "Accept": "application/json"})
            if resp.status_code != 200:
                last = f"{base} HTTP {resp.status_code}"
                continue
            items = []
            for i, row in enumerate(resp.json() or [], start=1):
                title = str(row.get("title") or "").strip()
                vid = str(row.get("videoId") or "").strip()
                if title and vid:
                    items.append({
                        "title": title, "rank": i,
                        "url": f"https://www.youtube.com/watch?v={vid}",
                    })
            if items:
                return items
            last = f"{base} 空列表"
        except Exception as exc:  # noqa: BLE001
            last = f"{base} {type(exc).__name__}: {str(exc)[:60]}"
    raise RuntimeError(last or "所有 Invidious 实例都失败")


def yt_from_html() -> list[dict]:
    session = requests.Session()
    session.headers.update({
        "User-Agent": UA,
        "Accept-Language": "en-US,en;q=0.9",
        "Cookie": "CONSENT=YES+cb.20240101-00-p0.en+FX+410; SOCS=CAI",
    })
    resp = session.get("https://www.youtube.com/feed/trending?gl=US&hl=en&ucbcb=1", timeout=30)
    resp.raise_for_status()
    html = resp.text
    idx = html.find("lockupViewModel")
    save_text("_yt_debug.txt", "\n".join([
        "len=%d lockup=%d videoId=%d consent=%s" % (
            len(html), html.count("lockupViewModel"), html.count('"videoId"'), "consent" in html.lower()),
        html[max(0, idx - 200): idx + 1200] if idx >= 0 else html[:600],
    ]))
    match = re.search(r"var ytInitialData\s*=\s*(\{.*?\});</script>", html, re.S)
    if not match:
        match = re.search(r'ytInitialData"\]\s*=\s*(\{.*?\});', html, re.S)
    if not match:
        raise RuntimeError(f"页面里没有 ytInitialData (len={len(html)}, consent={'consent' in html.lower()})")
    items = walk_titles(json.loads(match.group(1)))
    if not items:
        raise RuntimeError(f"ytInitialData 里没有视频节点 (len={len(html)})")
    return items


def fetch_youtube() -> list[dict]:
    errors: list[str] = []
    for label, fn in (("innertube", yt_from_innertube),
                      ("piped", yt_from_piped),
                      ("invidious", yt_from_invidious),
                      ("html", yt_from_html)):
        try:
            items = fn()
            print(f"youtube: {label} 成功 {len(items)} 条", flush=True)
            return items
        except Exception as exc:  # noqa: BLE001
            msg = f"{label}: {type(exc).__name__}: {str(exc)[:120]}"
            errors.append(msg)
            print("youtube " + msg, flush=True)
    raise RuntimeError(" | ".join(errors)[:900])


def fetch_googletrends() -> list[dict]:
    """Google Trends 热搜 RSS（2026 年新地址 /trending/rss）。"""
    urls = [
        "https://trends.google.com/trending/rss?geo=US",
        "https://trends.google.com/trends/trendingsearches/daily/rss?geo=US",
    ]
    last = ""
    for url in urls:
        try:
            resp = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}, timeout=30)
            if resp.status_code != 200:
                last = f"HTTP {resp.status_code} {url}"
                continue
            root = ET.fromstring(resp.content)
            items: list[dict] = []
            for i, node in enumerate(root.iter("item"), start=1):
                title = (node.findtext("title") or "").strip()
                if not title:
                    continue
                link = (node.findtext("link") or "").strip()
                traffic = ""
                for child in node:
                    if child.tag.endswith("approx_traffic"):
                        traffic = (child.text or "").strip()
                items.append({
                    "title": title,
                    "url": link or ("https://trends.google.com/trends/explore?geo=US&q=" + title.replace(" ", "+")),
                    "rank": i,
                    "traffic": traffic,
                })
            if items:
                return items
            last = f"RSS 没有条目 {url}"
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}: {str(exc)[:80]} {url}"
    raise RuntimeError(last)


def fetch_tiktok() -> list[dict]:
    """Creative Center 热门话题（数据中心 IP 常返回 40101 no permission）。"""
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
        raise RuntimeError(f"list 为空: {snippet}")
    return items


def main() -> int:
    status: dict[str, dict] = {}

    for name, fn in (("youtube", fetch_youtube), ("trends", fetch_googletrends), ("tiktok", fetch_tiktok)):
        last_error = ""
        for attempt in (1, 2):
            try:
                items = fn()
                save(name, items)
                status[name] = {"ok": True, "count": len(items)}
                print(f"{name}: ok, {len(items)}", flush=True)
                break
            except Exception as exc:  # noqa: BLE001
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt == 1:
                    time.sleep(4)
        else:
            status[name] = {"ok": False, "error": last_error[:900]}

    save_text("_status.json", json.dumps({"checked_at": now_iso(), "sources": status},
                                         ensure_ascii=False, indent=2))
    print("status:", json.dumps(status, ensure_ascii=True)[:500], flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
