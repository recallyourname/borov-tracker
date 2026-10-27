#!/usr/bin/env python3
"""Собирает свежие матчи игрока 226241953 и считает примерный MMR.

Источники:
- Dotabuff (главная профиля через r.jina.ai) — 15 последних матчей и календарь W-L по дням.
- OpenDota /matches/{id} — детали матча (предметы, NW, GPM). История игрока на OpenDota скрыта,
  но отдельные матчи по ID отдаются.

Состояние копится в data/state.json, страница читает data.json.
"""
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ACCOUNT = 226241953
ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "data" / "state.json"
OUT = ROOT / "data.json"

# База: 6781 на 16.09.2026; 16.09–09.10 по OpenDota 93W/99L → 6631 на конец 09.10.
# Дальше OpenDota историю не отдаёт, считаем по календарю Dotabuff ±25 за игру.
BASE_DATE, BASE_MMR = "2026-09-16", 6781
ANCHOR_DATE, ANCHOR_MMR = "2026-10-09", 6631
STEP = 25
KEEP = 20
CDN = "https://cdn.cloudflare.steamstatic.com"
UA = {"User-Agent": "gabriel-tracker/1.0 (+github actions)"}


def get(url, headers=None, timeout=60, tries=3):
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={**UA, **(headers or {})})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            print(f"GET {url} failed ({attempt + 1}/{tries}): {e}", file=sys.stderr)
            if isinstance(e, urllib.error.HTTPError) and e.code == 404:
                return None
            time.sleep(5 * (attempt + 1))
    return None


def get_json(url):
    body = get(url)
    try:
        return json.loads(body) if body else None
    except json.JSONDecodeError:
        return None


ROW_RE = re.compile(
    r'<a href="/matches/(\d+)">([^<]+)</a>.*?class="(won|lost)" href="/matches/\1">'
    r'.*?datetime="([^"]+)".*?r-first"><div class="r-label r-always-hidden">Type</div>'
    r'<div class="r-body">([^<]+)<.*?<div class="subtext">([^<]+)</div>'
    r'.*?Duration</div><div class="r-body">([^<]+)<'
    r'.*?kda-record"><span class="value">(\d+)</span>/<span class="value">(\d+)</span>/<span class="value">(\d+)',
    re.S,
)
CAL_RE = re.compile(
    r'<h3>(\d{4}-\d\d-\d\d)</h3>Record: <span[^>]*>(\d+)</span><span[^>]*>-</span><span[^>]*>(\d+)</span>'
)


def scrape_dotabuff():
    html = get(
        f"https://r.jina.ai/https://www.dotabuff.com/players/{ACCOUNT}",
        headers={"X-Return-Format": "html", "X-No-Cache": "true"},
        timeout=120,
    )
    if not html:
        return None, None
    rows = []
    # Режем по строкам таблицы, чтобы ленивый regex не перескакивал между матчами.
    section = html[html.find("Latest Matches"):]
    for chunk in section.split('<div class="r-row">')[1:]:
        m = ROW_RE.search(chunk)
        if not m:
            continue
        mid, hero, res, ts, typ, mode, dur, k, d, a = m.groups()
        rows.append({
            "id": int(mid), "hero_name": hero.strip(), "win": res == "won",
            "start": ts, "type": typ.strip(), "mode": mode.strip(), "duration_str": dur.strip(),
            "k": int(k), "d": int(d), "a": int(a),
        })
    cal = {d: [int(w), int(l)] for d, w, l in CAL_RE.findall(html)}
    return rows, cal


def load_constants(state):
    if state.get("heroes") and state.get("items") and state.get("const_ts", 0) > time.time() - 7 * 86400:
        return
    heroes = get_json("https://api.opendota.com/api/constants/heroes")
    items = get_json("https://api.opendota.com/api/constants/items")
    if heroes and items:
        state["heroes"] = {str(h["id"]): {"name": h["localized_name"], "img": CDN + h["img"].split("?")[0]} for h in heroes.values()}
        state["items"] = {str(v["id"]): {"name": v.get("dname") or k, "img": CDN + v["img"].split("?")[0]}
                          for k, v in items.items() if v.get("id") is not None and v.get("img")}
        state["const_ts"] = int(time.time())


def opendota_details(mid):
    d = get_json(f"https://api.opendota.com/api/matches/{mid}")
    if not d or not d.get("players"):
        return None
    p = next((p for p in d["players"] if p.get("account_id") == ACCOUNT), None)
    if not p:
        return None
    radiant = p["player_slot"] < 128
    return {
        "hero_id": p.get("hero_id"),
        "side": "radiant" if radiant else "dire",
        "win": d["radiant_win"] == radiant,
        "duration": d.get("duration"),
        "start_time": d.get("start_time"),
        "lobby_type": d.get("lobby_type"),
        "level": p.get("level"),
        "net_worth": p.get("net_worth"),
        "gpm": p.get("gold_per_min"),
        "xpm": p.get("xp_per_min"),
        "lh": p.get("last_hits"),
        "dn": p.get("denies"),
        "hero_damage": p.get("hero_damage"),
        "items": [p.get(f"item_{i}") or 0 for i in range(6)],
        "neutral": p.get("item_neutral") or 0,
        "score": [d.get("radiant_score"), d.get("dire_score")] if radiant else [d.get("dire_score"), d.get("radiant_score")],
    }


def main():
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    state.setdefault("matches", {})
    state.setdefault("calendar", {})

    rows, cal = scrape_dotabuff()
    if rows is None:
        print("dotabuff unavailable, keeping previous data", file=sys.stderr)
    else:
        print(f"dotabuff: {len(rows)} matches, {len(cal)} calendar days")
        for r in rows:
            cur = state["matches"].get(str(r["id"]), {})
            cur.update(r)
            state["matches"][str(r["id"])] = cur
        state["calendar"].update(cal)
        state["scraped_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    profile = get_json(f"https://api.opendota.com/api/players/{ACCOUNT}")
    if profile and profile.get("profile"):
        pr = profile["profile"]
        state["profile"] = {"name": pr.get("personaname"), "avatar": pr.get("avatarfull"), "rank_tier": profile.get("rank_tier"),
                            "leaderboard_rank": profile.get("leaderboard_rank")}

    load_constants(state)

    # Детали из OpenDota: только для последних KEEP матчей, не больше 8 попыток на матч.
    recent = sorted(state["matches"].values(), key=lambda m: m["start"], reverse=True)[:KEEP]
    for m in recent:
        if m.get("od") or m.get("od_tries", 0) >= 8:
            continue
        det = opendota_details(m["id"])
        m["od_tries"] = m.get("od_tries", 0) + 1
        if det:
            m["od"] = det
        time.sleep(1.1)

    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True))
    OUT.write_text(json.dumps(build_output(state), ensure_ascii=False, separators=(",", ":")))
    print("ok")


def build_output(state):
    heroes, items = state.get("heroes", {}), state.get("items", {})
    cal = state["calendar"]

    # Кривая рейтинга по дням после якоря.
    days, mmr = [], ANCHOR_MMR
    for d in sorted(k for k in cal if k > ANCHOR_DATE):
        w, l = cal[d]
        mmr += STEP * (w - l)
        days.append({"date": d, "w": w, "l": l, "mmr": mmr})
    current = mmr

    # Последние матчи; рейтинг после каждого — откатываем от текущего назад.
    recent = sorted(state["matches"].values(), key=lambda m: m["start"], reverse=True)[:KEEP]
    after = current
    matches = []
    for m in recent:
        od = m.get("od") or {}
        hero = heroes.get(str(od.get("hero_id")), {})
        ranked = m.get("type") == "Ranked"
        delta = (STEP if m["win"] else -STEP) if ranked else 0
        matches.append({
            "id": m["id"],
            "start": m["start"],
            "hero": hero.get("name") or m["hero_name"],
            "hero_img": hero.get("img") or f"{CDN}/apps/dota2/images/dota_react/heroes/{slug(m['hero_name'])}.png",
            "win": m["win"],
            "type": m.get("type"),
            "mode": m.get("mode"),
            "duration": od.get("duration") or parse_dur(m["duration_str"]),
            "k": m["k"], "d": m["d"], "a": m["a"],
            "nw": od.get("net_worth"), "gpm": od.get("gpm"), "xpm": od.get("xpm"),
            "lh": od.get("lh"), "dn": od.get("dn"), "level": od.get("level"),
            "hd": od.get("hero_damage"), "side": od.get("side"), "score": od.get("score"),
            "items": [item_ref(items, i) for i in od.get("items", [])],
            "neutral": item_ref(items, od.get("neutral", 0)),
            "delta": delta,
            "mmr_after": after,
        })
        after -= delta

    return {
        "account_id": ACCOUNT,
        "profile": state.get("profile", {}),
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scraped_at": state.get("scraped_at"),
        "base": {"date": BASE_DATE, "mmr": BASE_MMR},
        "anchor": {"date": ANCHOR_DATE, "mmr": ANCHOR_MMR},
        "step": STEP,
        "mmr": current,
        "days": days,
        "matches": matches,
    }


def item_ref(items, iid):
    if not iid:
        return None
    it = items.get(str(iid))
    return {"name": it["name"], "img": it["img"]} if it else None


def slug(name):
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def parse_dur(s):
    parts = [int(x) for x in s.split(":")]
    sec = 0
    for p in parts:
        sec = sec * 60 + p
    return sec


if __name__ == "__main__":
    main()
