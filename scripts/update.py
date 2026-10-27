#!/usr/bin/env python3
"""Собирает матчи игрока 226241953 в локальную SQLite-базу и строит data.json для страницы.

Источники:
- Dotabuff (главная профиля через r.jina.ai) — 15 последних матчей и календарь W-L по дням.
- OpenDota /matches/{id} — детали матча (предметы, NW, GPM). История игрока на OpenDota скрыта,
  но отдельные матчи по ID отдаются.

База: data/borov.db (matches, calendar, kv). Старые матчи заливаются scripts/import.py.
"""
import json
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ACCOUNT = 226241953
ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "borov.db"
OUT = ROOT / "data.json"

# База: 6781 на 16.09.2026, дальше ±25 за игру по календарю Dotabuff (день 16.09 включительно).
BASE_DATE, BASE_MMR = "2026-09-16", 6781
STEP = 25
KEEP = 20
OD_PER_RUN = 40  # сколько матчей без деталей догружать из OpenDota за один запуск
CDN = "https://cdn.cloudflare.steamstatic.com"
UA = {"User-Agent": "borov-tracker/1.0 (+github actions)"}
RADIANCE = 137
# Расходники и мелочь, которые не считаем «популярными предметами».
SKIP_ITEMS = {"tpscroll", "branches", "ward_observer", "ward_sentry", "ward_dispenser", "dust", "smoke_of_deceit",
              "tango", "flask", "clarity", "faerie_fire", "enchanted_mango", "blood_grenade", "cheese", "aegis",
              "refresher_shard", "famango", "great_famango", "greater_famango"}

SCHEMA = """
create table if not exists matches (
  id integer primary key,
  start text not null,            -- ISO UTC
  hero_id integer,
  hero_name text,
  win integer not null,
  type text,                      -- Ranked / Unranked / Turbo ...
  mode text,
  duration integer,
  k integer, d integer, a integer,
  side text,
  level integer, net_worth integer, gpm integer, xpm integer, lh integer, dn integer, hero_damage integer,
  items text,                     -- json: id предметов item_0..5
  backpack text,                  -- json: id предметов backpack_0..2
  neutral integer,
  purchased text,                 -- json: ключи всех купленных предметов (purchase_log), если матч распаршен
  score text,                     -- json [свои, чужие]
  od_done integer default 0,
  od_tries integer default 0,
  source text
);
create table if not exists calendar (date text primary key, w integer, l integer);
create table if not exists kv (k text primary key, v text);
"""


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


def connect():
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def kv_get(db, k, default=None):
    r = db.execute("select v from kv where k=?", (k,)).fetchone()
    return json.loads(r["v"]) if r else default


def kv_set(db, k, v):
    db.execute("insert or replace into kv(k, v) values(?, ?)", (k, json.dumps(v, ensure_ascii=False)))


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
        headers={"X-Return-Format": "html"},
        timeout=120,
    )
    # Вместо профиля может прийти заглушка Cloudflare — тогда считаем, что данных нет.
    if not html or "Latest Matches" not in html:
        return None, None
    rows = []
    section = html[html.find("Latest Matches"):]
    for chunk in section.split('<div class="r-row">')[1:]:
        m = ROW_RE.search(chunk)
        if not m:
            continue
        mid, hero, res, ts, typ, mode, dur, k, d, a = m.groups()
        rows.append({
            "id": int(mid), "hero_name": hero.strip(), "win": int(res == "won"),
            "start": ts, "type": typ.strip(), "mode": mode.strip(), "duration": parse_dur(dur.strip()),
            "k": int(k), "d": int(d), "a": int(a),
        })
    cal = {d: (int(w), int(l)) for d, w, l in CAL_RE.findall(html)}
    return rows, cal


def upsert_basic(db, r, source):
    """Базовые поля из Dotabuff; детали OpenDota не трогаем."""
    db.execute(
        """insert into matches(id, start, hero_name, win, type, mode, duration, k, d, a, source)
           values(:id, :start, :hero_name, :win, :type, :mode, :duration, :k, :d, :a, :source)
           on conflict(id) do update set hero_name=excluded.hero_name, win=excluded.win,
             type=coalesce(excluded.type, type), mode=coalesce(excluded.mode, mode),
             duration=coalesce(duration, excluded.duration), k=excluded.k, d=excluded.d, a=excluded.a""",
        {"type": None, "mode": None, "duration": None, **r, "source": source},
    )


def load_constants(db):
    const = kv_get(db, "const")
    if const and const.get("ts", 0) > time.time() - 7 * 86400:
        return const
    heroes = get_json("https://api.opendota.com/api/constants/heroes")
    items = get_json("https://api.opendota.com/api/constants/items")
    if not (heroes and items):
        return const or {"heroes": {}, "items": {}}
    const = {
        "ts": int(time.time()),
        "heroes": {str(h["id"]): {"name": h["localized_name"], "img": CDN + h["img"].split("?")[0]} for h in heroes.values()},
        "items": {str(v["id"]): {"key": k, "name": v.get("dname") or k, "img": CDN + v["img"].split("?")[0]}
                  for k, v in items.items() if v.get("id") is not None and v.get("img")},
    }
    kv_set(db, "const", const)
    return const


def apply_opendota(db, mid, d):
    """Записывает детали матча из ответа OpenDota /matches/{id}. True, если игрок найден."""
    if not d or not d.get("players"):
        return False
    p = next((p for p in d["players"] if p.get("account_id") == ACCOUNT), None)
    if not p:
        return False
    radiant = p["player_slot"] < 128
    score = [d.get("radiant_score"), d.get("dire_score")]
    purchased = sorted({x["key"] for x in (p.get("purchase_log") or [])}) or None
    db.execute(
        """update matches set hero_id=?, side=?, win=?, duration=?, level=?, net_worth=?, gpm=?, xpm=?, lh=?, dn=?,
             hero_damage=?, items=?, backpack=?, neutral=?, purchased=?, score=?, od_done=1,
             k=?, d=?, a=?, type=coalesce(type, ?)
           where id=?""",
        (p.get("hero_id"), "radiant" if radiant else "dire", int(d["radiant_win"] == radiant), d.get("duration"),
         p.get("level"), p.get("net_worth"), p.get("gold_per_min"), p.get("xp_per_min"), p.get("last_hits"),
         p.get("denies"), p.get("hero_damage"),
         json.dumps([p.get(f"item_{i}") or 0 for i in range(6)]),
         json.dumps([p.get(f"backpack_{i}") or 0 for i in range(3)]),
         p.get("item_neutral") or 0,
         json.dumps(purchased) if purchased else None,
         json.dumps(score if radiant else score[::-1]),
         p.get("kills"), p.get("deaths"), p.get("assists"),
         "Ranked" if d.get("lobby_type") == 7 else None,
         mid),
    )
    return True


def fill_details(db, limit):
    todo = db.execute(
        "select id from matches where od_done=0 and od_tries<8 order by start desc limit ?", (limit,)
    ).fetchall()
    for r in todo:
        d = get_json(f"https://api.opendota.com/api/matches/{r['id']}")
        db.execute("update matches set od_tries=od_tries+1 where id=?", (r["id"],))
        apply_opendota(db, r["id"], d)
        db.commit()
        time.sleep(1.1)
    return len(todo)


def main(od_limit=OD_PER_RUN):
    db = connect()
    rows, cal = scrape_dotabuff()
    if rows is None:
        print("dotabuff unavailable, keeping previous data", file=sys.stderr)
    else:
        print(f"dotabuff: {len(rows)} matches, {len(cal)} calendar days")
        for r in rows:
            upsert_basic(db, r, "dotabuff-main")
        db.executemany("insert or replace into calendar(date, w, l) values(?, ?, ?)", [(k, w, l) for k, (w, l) in cal.items()])
        kv_set(db, "scraped_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))

    profile = get_json(f"https://api.opendota.com/api/players/{ACCOUNT}")
    if profile and profile.get("profile"):
        pr = profile["profile"]
        kv_set(db, "profile", {"name": pr.get("personaname"), "avatar": pr.get("avatarfull"),
                               "rank_tier": profile.get("rank_tier"), "leaderboard_rank": profile.get("leaderboard_rank")})
    const = load_constants(db)
    db.commit()

    n = fill_details(db, od_limit)
    print(f"opendota details tried: {n}")

    OUT.write_text(json.dumps(build_output(db, const), ensure_ascii=False, separators=(",", ":")))
    db.close()
    print("ok")


def build_output(db, const):
    heroes, items = const["heroes"], const["items"]
    cal = {r["date"]: (r["w"], r["l"]) for r in db.execute("select * from calendar")}

    # Кривая рейтинга по дням от базы.
    days, mmr = [], BASE_MMR
    for d in sorted(k for k in cal if k >= BASE_DATE):
        w, l = cal[d]
        mmr += STEP * (w - l)
        days.append({"date": d, "w": w, "l": l, "mmr": mmr})
    current = mmr

    all_rows = [dict(r) for r in db.execute("select * from matches order by start desc")]
    for m in all_rows:
        for f in ("items", "backpack", "purchased", "score"):
            m[f] = json.loads(m[f]) if m[f] else None
        hero = heroes.get(str(m["hero_id"]), {})
        m["hero"] = hero.get("name") or m["hero_name"]
        m["hero_img"] = hero.get("img") or f"{CDN}/apps/dota2/images/dota_react/heroes/{slug(m['hero_name'])}.png"

    # Последние матчи; рейтинг после каждого — откатываем от текущего назад.
    after, recent = current, []
    for m in all_rows[:KEEP]:
        delta = (STEP if m["win"] else -STEP) if m["type"] == "Ranked" else 0
        recent.append({
            "id": m["id"], "start": m["start"], "hero": m["hero"], "hero_img": m["hero_img"],
            "win": bool(m["win"]), "type": m["type"], "duration": m["duration"],
            "k": m["k"], "d": m["d"], "a": m["a"],
            "nw": m["net_worth"], "gpm": m["gpm"], "xpm": m["xpm"], "lh": m["lh"], "dn": m["dn"],
            "level": m["level"], "side": m["side"], "score": m["score"],
            "items": [item_ref(items, i) for i in (m["items"] or [])],
            "neutral": item_ref(items, m["neutral"]),
            "delta": delta, "mmr_after": after,
        })
        after -= delta

    return {
        "account_id": ACCOUNT,
        "profile": kv_get(db, "profile", {}),
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scraped_at": kv_get(db, "scraped_at"),
        "base": {"date": BASE_DATE, "mmr": BASE_MMR},
        "step": STEP,
        "mmr": current,
        "days": days,
        "matches": recent,
        "stats": period_stats(all_rows, items, days),
    }


def period_stats(rows, items, days):
    """Статистика по всем матчам базы с BASE_DATE."""
    rows = [m for m in rows if m["start"][:10] >= BASE_DATE]
    detailed = [m for m in rows if m["items"] is not None]
    cal_games = sum(d["w"] + d["l"] for d in days)
    wr = lambda ms: {"n": len(ms), "w": sum(m["win"] for m in ms)}  # noqa: E731

    by_hero = {}
    for m in rows:
        h = by_hero.setdefault(m["hero"], {"hero": m["hero"], "img": m["hero_img"], "n": 0, "w": 0, "k": 0, "d": 0, "a": 0})
        h["n"] += 1
        h["w"] += m["win"]
        h["k"] += m["k"] or 0
        h["d"] += m["d"] or 0
        h["a"] += m["a"] or 0
    heroes = sorted(by_hero.values(), key=lambda h: (-h["n"], -h["w"]))

    def has(m, iid):
        if m["purchased"] and items.get(str(iid)):
            return items[str(iid)]["key"] in m["purchased"]
        return iid in (m["items"] or []) or iid in (m["backpack"] or [])

    def radiance_block(ms):
        yes = [m for m in ms if has(m, RADIANCE)]
        no = [m for m in ms if not has(m, RADIANCE)]
        return {"with": wr(yes), "without": wr(no),
                "strip": [{"win": bool(m["win"]), "rad": has(m, RADIANCE)} for m in reversed(ms)]}

    spectre = [m for m in detailed if m["hero"] == "Spectre"]

    # Топ-10 предметов финального инвентаря (вместе с рюкзаком) по частоте.
    counts = {}
    for m in detailed:
        for iid in set((m["items"] or []) + (m["backpack"] or [])):
            it = items.get(str(iid))
            if iid and it and it["key"] not in SKIP_ITEMS and not it["key"].startswith("recipe"):
                counts[iid] = counts.get(iid, 0) + 1
    top = sorted(counts, key=lambda i: -counts[i])[:10]
    top_items = []
    for iid in top:
        inv = lambda m: iid in (m["items"] or []) or iid in (m["backpack"] or [])  # noqa: E731
        top_items.append({**item_ref(items, iid), "with": wr([m for m in detailed if inv(m)]),
                          "without": wr([m for m in detailed if not inv(m)])})

    return {
        "since": BASE_DATE,
        "games": len(rows),
        "detailed": len(detailed),
        "calendar_games": cal_games,
        "first": rows[-1]["start"] if rows else None,
        "overall": wr(rows),
        "heroes": heroes,
        "radiance": {"all": radiance_block(detailed), "spectre": radiance_block(spectre)},
        "items": top_items,
    }


def item_ref(items, iid):
    if not iid:
        return None
    it = items.get(str(iid))
    return {"id": iid, "name": it["name"], "img": it["img"]} if it else None


def slug(name):
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def parse_dur(s):
    sec = 0
    for p in s.split(":"):
        sec = sec * 60 + int(p)
    return sec


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else OD_PER_RUN)
