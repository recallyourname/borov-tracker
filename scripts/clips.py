#!/usr/bin/env python3
"""Дотягивает к клипам из clips.json название, автора, длительность и превью со страницы клипа на Twitch.

Чтобы добавить клип, достаточно дописать в clips.json {"slug": "..."} или {"url": "https://www.twitch.tv/.../clip/..."}:
остальные поля скрипт заполнит при следующем прогоне. Twitch API требует токен, а og-теги страницы клипа отдаются без него.
"""
import html
import json
import re
import sys
import urllib.request
from pathlib import Path

PATH = Path(__file__).resolve().parent.parent / "clips.json"
UA = "Mozilla/5.0 (compatible; Twitterbot/1.0)"  # обычному браузеру Twitch отдаёт SPA без og-тегов


def slug_of(c):
    if c.get("slug"):
        return c["slug"]
    m = re.search(r"(?:/clip/|clips\.twitch\.tv/(?:embed\?clip=)?)([\w-]+)", c.get("url", ""))
    return m and m.group(1)


def meta(slug):
    req = urllib.request.Request(f"https://clips.twitch.tv/{slug}", headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        page = r.read().decode("utf-8", "replace")
    og = {k: html.unescape(v) for k, v in re.findall(r'<meta property="og:([\w:]+)" content="([^"]*)"', page)}
    m = re.search(r"^Watch (.*?)'s .*clip titled \"(.*)\" on Twitch", og.get("description", ""), re.S)
    if not m:
        return None
    return {"title": m.group(2), "author": m.group(1), "thumb": og.get("image"),
            "duration": int(og["video:duration"]) if og.get("video:duration", "").isdigit() else None}


def main():
    clips = json.loads(PATH.read_text())
    out, seen = [], set()
    for c in clips:
        slug = slug_of(c)
        if not slug or slug in seen:
            continue
        seen.add(slug)
        c = {k: v for k, v in c.items() if k != "url"} | {"slug": slug}
        if not c.get("title"):
            try:
                info = meta(slug)
            except Exception as e:
                info = None
                print(f"::warning::clip {slug}: {e}", file=sys.stderr)
            if info:
                c |= info
            else:
                print(f"::warning::clip {slug}: no metadata", file=sys.stderr)
        out.append(c)
    PATH.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n")
    print(f"clips: {len(out)}, without title: {sum(not c.get('title') for c in out)}")


if __name__ == "__main__":
    main()
