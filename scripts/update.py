#!/usr/bin/env python3
"""Собирает матчи игрока 226241953 в локальную SQLite-базу и строит data.json для страницы.

Источники:
- Steam Web API GetMatchHistory (нужен STEAM_API_KEY) — id последних матчей; базовые поля нового матча берём
  из OpenDota /matches/{id}, а если там его ещё нет — из Steam GetMatchDetails.
- Без ключа — Dotabuff (главная профиля через r.jina.ai): 15 последних матчей и календарь W-L по дням.
- OpenDota /matches/{id} — детали матча (предметы, NW, GPM). История игрока на OpenDota скрыта,
  но отдельные матчи по ID отдаются.

База: data/borov.db (matches, calendar, kv). Старые матчи заливаются scripts/import.py.
"""
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ACCOUNT = 226241953
ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "borov.db"
OUT = ROOT / "data.json"

# Опорная точка рейтинга; от неё ±25 за каждую ранкед-игру вперёд и назад.
# Назад считаем до 31.07.2026 — патч 7.41e пересчитал шкалу MMR.
BASE_TS, BASE_MMR = "2026-09-16T09:00:00+00:00", 6781
BASE_DATE = BASE_TS[:10]
HISTORY_FROM = "2026-07-31"
STEP = 25
KEEP = 20
SHAME = 10  # сколько позорных матчей показывать; позорный — от 10 смертей, сортировка по смертям минус убийства
SHAME_MIN_D = 10
FORM = 50  # по скольким последним ранкед-играм считать винрейт для прогноза до 7к
OD_PER_RUN = 40  # сколько матчей без деталей догружать из OpenDota за один запуск
OD_RETRY_DAYS = 3  # матчи моложе стольких дней перезапрашиваем в OpenDota без лимита попыток
REFILL_PER_RUN = 150  # сколько старых матчей перезапрашивать ради региона и составов за один запуск, пока не догрузим все
CDN = "https://cdn.cloudflare.steamstatic.com"
UA = {"User-Agent": "borov-tracker/1.0 (+github actions)"}
STEAM_KEY = os.environ.get("STEAM_API_KEY")
STEAM = "https://api.steampowered.com/IDOTA2Match_570"
STEAM_HISTORY = 25  # сколько последних матчей спрашивать у Steam за запуск
MODES = {1: "All Pick", 2: "Captains Mode", 3: "Random Draft", 4: "Single Draft", 5: "All Random", 16: "Captains Draft",
         18: "Ability Draft", 22: "All Pick", 23: "Turbo"}
# С ключом jina ходит через свои резидентные IP, и Cloudflare Dotabuff пускает свежие запросы.
JINA_KEY = os.environ.get("JINA_API_KEY")
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
  allies text,                    -- json: id героев союзников
  enemies text,                   -- json: id героев противников
  region integer,                 -- регион сервера по OpenDota (constants/region), -1 — OpenDota не знает
  cluster integer,                -- кластер сервера; по нему подписываем матчи с неизвестным регионом
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
            print(f"GET {re.sub(r'key=[^&]+', 'key=***', url)} failed ({attempt + 1}/{tries}): {e}", file=sys.stderr)
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
    # Колонки region, cluster, allies и enemies добавили позже — в старой базе их нет.
    cols = {r["name"] for r in db.execute("pragma table_info(matches)")}
    for col, typ in (("region", "integer"), ("cluster", "integer"), ("allies", "text"), ("enemies", "text")):
        if col not in cols:
            db.execute(f"alter table matches add column {col} {typ}")
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
    """(rows, cal, fresh). Сначала свежая страница; если Cloudflare не пустил jina — её последний снимок из кэша."""
    for fresh in (True, False):
        html = get(
            f"https://r.jina.ai/https://www.dotabuff.com/players/{ACCOUNT}",
            # Без X-No-Cache jina отдаёт снимок страницы из своего кэша, и свежие матчи приходят с опозданием.
            headers={"X-Return-Format": "html", **({"X-No-Cache": "true"} if fresh else {}),
                     **({"Authorization": f"Bearer {JINA_KEY}"} if JINA_KEY else {})},
            timeout=120,
        )
        rows, cal = parse_dotabuff(html)
        if rows:
            return rows, cal, fresh
        title = re.search(r"<title>([^<]*)", html or "")
        print(f"dotabuff {'fresh' if fresh else 'cached'}: no profile ({len(html or '')} bytes, "
              f"title {title.group(1) if title else None!r}, start {(html or '')[:120]!r})", file=sys.stderr)
    return None, None, False


def steam_history():
    """id последних матчей игрока из Steam GetMatchHistory или None, если Valve не отдал историю."""
    d = get_json(f"{STEAM}/GetMatchHistory/v1/?key={STEAM_KEY}&account_id={ACCOUNT}&matches_requested={STEAM_HISTORY}")
    res = (d or {}).get("result") or {}
    if res.get("status") != 1:
        print(f"steam history: {res.get('statusDetail') or 'no response'}", file=sys.stderr)
        return None
    return [m["match_id"] for m in res.get("matches") or []]


def basic_row(d, heroes):
    """Базовые поля матча для upsert_basic из ответа OpenDota /matches/{id} или Steam GetMatchDetails — поля у них общие."""
    p = next((p for p in (d or {}).get("players") or [] if p.get("account_id") == ACCOUNT), None)
    if not p or d.get("radiant_win") is None:
        return None
    return {
        "id": d["match_id"], "hero_name": heroes.get(str(p["hero_id"]), {}).get("name") or str(p["hero_id"]),
        "win": int(d["radiant_win"] == (p["player_slot"] < 128)),
        "start": datetime.fromtimestamp(d["start_time"], timezone.utc).isoformat(timespec="seconds"),
        "type": "Ranked" if d.get("lobby_type") == 7 else "Turbo" if d.get("game_mode") == 23 else "Unranked",
        "mode": MODES.get(d.get("game_mode")), "duration": d.get("duration"),
        "k": p.get("kills"), "d": p.get("deaths"), "a": p.get("assists"),
    }


def from_steam(db, heroes):
    """Догружает новые матчи из истории Steam. Число новых или None, если история недоступна."""
    ids = steam_history()
    if ids is None:
        return None
    known = {r["id"] for r in db.execute("select id from matches")}
    added = 0
    for mid in (i for i in ids if i not in known):
        od = get_json(f"https://api.opendota.com/api/matches/{mid}")
        row, source = basic_row(od, heroes), "opendota"
        if not row:
            # Совсем свежего матча в OpenDota может ещё не быть.
            row, source = basic_row((get_json(f"{STEAM}/GetMatchDetails/v1/?key={STEAM_KEY}&match_id={mid}") or {}).get("result"), heroes), "steam"
        if not row:
            print(f"steam: no details for {mid} yet", file=sys.stderr)
            continue
        upsert_basic(db, row, source)
        if source == "opendota":
            apply_opendota(db, mid, od)
        db.commit()
        added += 1
        time.sleep(1.1)
    return added


def from_dotabuff(db):
    """Старый путь без ключа Steam: главная профиля Dotabuff через jina. Статус для scrape_status."""
    rows, cal, fresh = scrape_dotabuff()
    if rows and not fresh:
        # Снимок из кэша нужен, только если в нём есть новые матчи: старый откатил бы календарь за сегодня.
        known = {r["id"] for r in db.execute("select id from matches")}
        if all(r["id"] in known for r in rows):
            rows = None
    if rows is None:
        return "unavailable"
    print(f"dotabuff{'' if fresh else ' (jina cache)'}: {len(rows)} matches, {len(cal)} calendar days")
    for r in rows:
        upsert_basic(db, r, "dotabuff-main")
    db.executemany("insert or replace into calendar(date, w, l) values(?, ?, ?)", [(k, w, l) for k, (w, l) in cal.items()])
    return "ok" if fresh else "cached"


def parse_dotabuff(html):
    # Вместо профиля может прийти заглушка Cloudflare — тогда считаем, что данных нет.
    if not html or "Latest Matches" not in html:
        return None, None
    rows = []
    section = html[html.find("Latest Matches"):]
    # Если страница прошла через JS (рендер с ключом jina), у строк класс "r-row " с пробелом.
    for chunk in re.split(r'<div class="r-row\s*">', section)[1:]:
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
    # Изменение разметки не должно выдавать пустой результат за успешное обновление.
    if not rows or not cal:
        return None, None
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
    if const and "regions" in const and const.get("ts", 0) > time.time() - 7 * 86400:
        return const
    heroes = get_json("https://api.opendota.com/api/constants/heroes")
    items = get_json("https://api.opendota.com/api/constants/items")
    regions = get_json("https://api.opendota.com/api/constants/region")
    if not (heroes and items and regions):
        return const or {"heroes": {}, "items": {}, "regions": {}}
    const = {
        "ts": int(time.time()),
        "regions": {k: region_name(v) for k, v in regions.items()},
        "heroes": {str(h["id"]): {"name": h["localized_name"], "img": CDN + h["img"].split("?")[0]} for h in heroes.values()},
        "items": {str(v["id"]): {"key": k, "name": v.get("dname") or k, "img": CDN + v["img"].split("?")[0]}
                  for k, v in items.items() if v.get("id") is not None and v.get("img")},
    }
    kv_set(db, "const", const)
    return const


def lineup(d, p):
    """Герои союзников и противников игрока p — json-строки для колонок allies и enemies."""
    radiant = p["player_slot"] < 128
    allies = [x["hero_id"] for x in d["players"] if (x["player_slot"] < 128) == radiant and x is not p]
    enemies = [x["hero_id"] for x in d["players"] if (x["player_slot"] < 128) != radiant]
    return json.dumps(allies), json.dumps(enemies)


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
             hero_damage=?, items=?, backpack=?, neutral=?, purchased=?, score=?, region=?, cluster=?, allies=?, enemies=?, od_done=1,
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
         d["region"] if d.get("region") is not None else -1, d.get("cluster"), *lineup(d, p),
         p.get("kills"), p.get("deaths"), p.get("assists"),
         "Ranked" if d.get("lobby_type") == 7 else None,
         mid),
    )
    return True


def fill_details(db, limit):
    # Свежие матчи OpenDota может отдавать с задержкой, а прогоны после пушей идут подряд и быстро сжигают попытки —
    # поэтому матчи младше OD_RETRY_DAYS перезапрашиваем без лимита.
    recent = (datetime.now(timezone.utc) - timedelta(days=OD_RETRY_DAYS)).isoformat(timespec="seconds")
    todo = db.execute(
        "select id from matches where od_done=0 and (od_tries<8 or start>=?) order by start desc limit ?",
        (recent, limit),
    ).fetchall()
    for r in todo:
        d = get_json(f"https://api.opendota.com/api/matches/{r['id']}")
        db.execute("update matches set od_tries=od_tries+1 where id=?", (r["id"],))
        apply_opendota(db, r["id"], d)
        db.commit()
        time.sleep(1.1)
    return len(todo)


def refill(db, limit):
    """Регион и составы не сохраняли для матчей, загруженных до их появления, — перезапрашиваем понемногу."""
    todo = db.execute(
        "select id from matches where od_done=1 and (region is null or region < 0 and cluster is null or enemies is null)"
        " and start >= ? order by start desc limit ?",
        (HISTORY_FROM, limit),
    ).fetchall()
    for r in todo:
        d = get_json(f"https://api.opendota.com/api/matches/{r['id']}")
        p = next((p for p in d["players"] if p.get("account_id") == ACCOUNT), None) if d and d.get("players") else None
        if p:
            # -1 — OpenDota ответил, но региона нет; иначе такой матч перезапрашивался бы каждый запуск.
            db.execute("update matches set region=?, cluster=?, allies=?, enemies=? where id=?",
                       (d["region"] if d.get("region") is not None else -1, d.get("cluster"), *lineup(d, p), r["id"]))
            db.commit()
        time.sleep(1.1)
    return len(todo)


def main(od_limit=OD_PER_RUN):
    db = connect()
    const = load_constants(db)
    if STEAM_KEY:
        added = from_steam(db, const["heroes"])
        status = "unavailable" if added is None else "ok"
        if added is not None:
            print(f"steam: {added} new matches")
    else:
        status = from_dotabuff(db)
    kv_set(db, "scrape_status", status)
    if status == "unavailable":
        print("::warning::Match source unavailable, keeping previous data", file=sys.stderr)
    else:
        kv_set(db, "scraped_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))

    profile = get_json(f"https://api.opendota.com/api/players/{ACCOUNT}")
    if profile and profile.get("profile"):
        pr = profile["profile"]
        kv_set(db, "profile", {"name": pr.get("personaname"), "avatar": pr.get("avatarfull"),
                               "rank_tier": profile.get("rank_tier"), "leaderboard_rank": profile.get("leaderboard_rank")})
    db.commit()

    n = fill_details(db, od_limit)
    print(f"opendota details tried: {n}")
    print(f"opendota refill tried: {refill(db, REFILL_PER_RUN)}")

    OUT.write_text(json.dumps(build_output(db, const), ensure_ascii=False, separators=(",", ":")))
    db.close()
    print("ok")


def build_output(db, const):
    heroes, items = const["heroes"], const["items"]
    cal = {r["date"]: (r["w"], r["l"]) for r in db.execute("select * from calendar")}

    all_rows = [dict(r) for r in db.execute("select * from matches where start >= ? order by start desc", (HISTORY_FROM,))]
    for m in all_rows:
        for f in ("items", "backpack", "purchased", "score", "allies", "enemies"):
            m[f] = json.loads(m[f]) if m[f] else None
        hero = heroes.get(str(m["hero_id"]), {})
        m["hero"] = hero.get("name") or m["hero_name"]
        m["hero_img"] = hero.get("img") or f"{CDN}/apps/dota2/images/dota_react/heroes/{slug(m['hero_name'])}.png"

    days, current = rating_series(all_rows, cal)

    recent = []
    for m in all_rows[:KEEP]:
        recent.append({
            "id": m["id"], "start": m["start"], "hero": m["hero"], "hero_img": m["hero_img"],
            "win": bool(m["win"]), "type": m["type"], "duration": m["duration"],
            "k": m["k"], "d": m["d"], "a": m["a"],
            "nw": m["net_worth"], "gpm": m["gpm"], "xpm": m["xpm"], "lh": m["lh"], "dn": m["dn"],
            "level": m["level"], "side": m["side"], "score": m["score"],
            "items": [item_ref(items, i) for i in (m["items"] or [])],
            "neutral": item_ref(items, m["neutral"]),
            "delta": m["delta"], "mmr_after": m["mmr_after"],
        })

    shame = sorted((m for m in all_rows if (m["d"] or 0) >= SHAME_MIN_D),
                   key=lambda m: (-(m["d"] - m["k"]), -m["d"], m["start"]))[:SHAME]
    shame = [{f: m[f] for f in ("id", "start", "hero", "hero_img", "type", "duration", "k", "d", "a")} | {"win": bool(m["win"])}
             for m in shame]

    return {
        "account_id": ACCOUNT,
        "profile": kv_get(db, "profile", {}),
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scraped_at": kv_get(db, "scraped_at"),
        "scrape_status": kv_get(db, "scrape_status"),
        "history_from": HISTORY_FROM,
        "step": STEP,
        "mmr": current,
        "days": days,
        "matches": recent,
        "shame": shame,
        "form": {"n": len(ranked := [m for m in all_rows if m["type"] == "Ranked"][:FORM]),
                 "w": sum(m["win"] for m in ranked)},
        "stats": period_stats(all_rows, items, days, const.get("regions", {}), heroes),
    }


def rating_series(rows, cal):
    """Рейтинг после каждой ранкед-игры от опорной точки BASE_TS (вперёд и назад) и точки на конец дня.

    Если за какой-то день после опорной точки в календаре Dotabuff игр больше, чем матчей в базе (сборщик что-то
    пропустил), недостающие W/L добавляем по календарю — они сдвигают рейтинг этого дня и всех следующих.
    """
    chrono = sorted(rows, key=lambda m: m["start"])
    by_day = {}
    for m in chrono:
        m["day"] = m["start"][:10]
        m["delta"] = (STEP if m["win"] else -STEP) if m["type"] == "Ranked" else 0
        w, l, n = by_day.get(m["day"], (0, 0, 0))
        by_day[m["day"]] = (w + (m["delta"] > 0), l + (m["delta"] < 0), n + 1)

    extra = {}
    for d, (cw, cl) in cal.items():
        w, l, n = by_day.get(d, (0, 0, 0))
        if d >= BASE_DATE and cw + cl > n:
            extra[d] = (cw, cl, STEP * ((cw - w) - (cl - l)))

    mmr = BASE_MMR
    for m in reversed([m for m in chrono if m["start"] < BASE_TS]):
        m["mmr_after"] = mmr
        mmr -= m["delta"]
    start_mmr = mmr
    mmr = BASE_MMR
    for m in (m for m in chrono if m["start"] >= BASE_TS):
        mmr += m["delta"]
        m["mmr_after"] = mmr

    shift, shift_by_day = 0, {}
    for d in sorted(set(by_day) | set(extra)):
        shift += extra[d][2] if d in extra else 0
        shift_by_day[d] = shift
    for m in chrono:
        if m["start"] >= BASE_TS:
            m["mmr_after"] += shift_by_day[m["day"]]

    last_of_day = {m["day"]: m["mmr_after"] for m in chrono}
    points, prev = [], start_mmr
    for d in sorted(set(by_day) | set(extra)):
        end = last_of_day.get(d, prev + (extra[d][2] if d in extra else 0))
        w, l, _ = by_day.get(d, (0, 0, 0))
        if d in extra:
            w, l = extra[d][0], extra[d][1]
        points.append({"date": d, "w": w, "l": l, "mmr": end})
        prev = end
    current = points[-1]["mmr"] if points else BASE_MMR
    return {"start": start_mmr, "points": points}, current


# Часть света по id региона (constants/region + регионы из regions.txt Valve, которых ещё нет в API OpenDota).
REGION_GROUP = {
    1: "na", 2: "na", 27: "na", 31: "na",
    3: "eu", 8: "eu", 9: "eu", 28: "eu",
    10: "sa", 14: "sa", 15: "sa", 38: "sa",
    5: "asia", 16: "asia", 19: "asia", 24: "asia", 37: "asia",
    12: "cn", 13: "cn", 17: "cn", 18: "cn", 20: "cn", 25: "cn",
    6: "me", 7: "oce", 11: "af",
}
NEW_REGIONS = {"24": "Hong Kong", "27": "US North Central", "28": "Warsaw", "31": "US South Central"}
# Новые кластеры, которых нет в constants/cluster OpenDota. Источники: имена серверов из odota/core#2986
# (srcds…-waw2.189 / .436 — Варшава, srcds…-dfw2.407 — Даллас), dotaconstants (410 — US East)
# и страницы матчей Stratz (129, 404–406 — Америка). 190 — сосед 189, Варшава по предположению;
# 404–406 стоят в одном блоке с далласким 407.
NEW_CLUSTERS = {129: 2, 189: 28, 190: 28, 436: 28, 404: 31, 405: 31, 406: 31, 407: 31, 410: 2}


def region_stats(rows, regions):
    """Винрейт ранкеда по частям света, внутри — по регионам сервера."""
    by = {}
    for m in rows:
        if m["region"] is None:
            continue
        rid = m["region"] if m["region"] >= 0 else NEW_CLUSTERS.get(m["cluster"])
        # Кластеры, которых нет ни у OpenDota, ни в NEW_CLUSTERS, идут в группу «другие» (group None).
        group = REGION_GROUP.get(rid)
        name = (regions.get(str(rid)) or NEW_REGIONS.get(str(rid))) if group else None
        g = by.setdefault(group, {"group": group, "n": 0, "w": 0, "parts": {}})
        r = g["parts"].setdefault(name, {"region": name, "n": 0, "w": 0, "clusters": set()})
        for x in (g, r):
            x["n"] += 1
            x["w"] += m["win"]
        if m["region"] < 0 and m["cluster"] is not None:
            r["clusters"].add(m["cluster"])
    out = sorted(by.values(), key=lambda g: (g["group"] is None, -g["n"]))
    for g in out:
        g["parts"] = sorted(g["parts"].values(), key=lambda r: -r["n"])
        for r in g["parts"]:
            r["clusters"] = sorted(r["clusters"])
    return out


def matchup_stats(rows, heroes, side):
    """Винрейт против героев (side="enemies") или вместе с ними (side="allies")."""
    by = {}
    for m in rows:
        for hid in m[side] or []:
            hero = heroes.get(str(hid))
            if not hero:
                continue
            h = by.setdefault(hid, {"hero": hero["name"], "img": hero["img"], "n": 0, "w": 0})
            h["n"] += 1
            h["w"] += m["win"]
    return sorted(by.values(), key=lambda h: -h["n"])


def draft_data(rows, heroes):
    """Все герои и составы ранкед-матчей для калькулятора винрейта: [свой герой, победа, союзники, противники]."""
    return {
        "heroes": {hid: [h["name"], h["img"]] for hid, h in heroes.items()},
        "games": [[m["hero_id"], m["win"], m["allies"], m["enemies"]] for m in rows if m["enemies"] is not None],
    }


def period_stats(rows, items, days, regions, hero_const):
    """Статистика по всем ранкед-матчам базы с HISTORY_FROM."""
    rows = [m for m in rows if m["type"] == "Ranked"]
    days = days["points"]
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
        "since": HISTORY_FROM,
        "games": len(rows),
        "detailed": len(detailed),
        "calendar_games": cal_games,
        "first": rows[-1]["start"] if rows else None,
        "overall": wr(rows),
        "heroes": heroes,
        "radiance": {"all": radiance_block(detailed), "spectre": radiance_block(spectre)},
        "items": top_items,
        "farm": farm_stats(detailed),
        "weeks": week_stats(detailed),
        "streaks": streak_stats(rows),
        "regions": region_stats(rows, regions),
        "lineups": sum(m["enemies"] is not None for m in rows),
        "vs": matchup_stats(rows, hero_const, "enemies"),
        "with": matchup_stats(rows, hero_const, "allies"),
        "draft": draft_data(rows, hero_const),
    }


def avg(ms, f):
    vals = [f(m) for m in ms]
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), 2) if vals else None


FARM = {
    "gpm": lambda m: m["gpm"],
    "xpm": lambda m: m["xpm"],
    "lhm": lambda m: m["lh"] / (m["duration"] / 60) if m["lh"] is not None and m["duration"] else None,
    "nw": lambda m: m["net_worth"],
    "kda": lambda m: (m["k"] + m["a"]) / max(1, m["d"]),
    "hdm": lambda m: m["hero_damage"] / (m["duration"] / 60) if m["hero_damage"] is not None and m["duration"] else None,
    "dur": lambda m: m["duration"],
}


def farm_stats(detailed):
    """Средние за период, за последние 7 дней и за 7 дней до них."""
    now = datetime.now(timezone.utc)
    def window(lo, hi):
        return [m for m in detailed if lo <= (now - datetime.fromisoformat(m["start"])).total_seconds() / 86400 < hi]
    blocks = {"all": detailed, "week": window(0, 7), "prev": window(7, 14)}
    return {k: {"n": len(ms), **{f: avg(ms, fn) for f, fn in FARM.items()}} for k, ms in blocks.items()}


def week_stats(detailed):
    """По календарным неделям (пн–вс, UTC): игры, винрейт, средние GPM/XPM."""
    weeks = {}
    for m in detailed:
        d = datetime.fromisoformat(m["start"])
        key = (d - timedelta(days=d.weekday())).strftime("%Y-%m-%d")
        weeks.setdefault(key, []).append(m)
    return [{"week": k, "n": len(ms), "w": sum(m["win"] for m in ms),
             "gpm": avg(ms, FARM["gpm"]), "xpm": avg(ms, FARM["xpm"]), "lhm": avg(ms, FARM["lhm"])}
            for k, ms in sorted(weeks.items())]


def streak_stats(rows):
    """Самые длинные серии побед и поражений за период и текущая серия."""
    chrono = sorted(rows, key=lambda m: m["start"])
    best = {1: None, 0: None}
    i = 0
    while i < len(chrono):
        j = i
        while j + 1 < len(chrono) and chrono[j + 1]["win"] == chrono[i]["win"]:
            j += 1
        n, w = j - i + 1, chrono[i]["win"]
        if not best[w] or n > best[w]["n"]:
            best[w] = {"n": n, "from": chrono[i]["start"], "to": chrono[j]["start"]}
        i = j + 1
    cur = {"n": 0, "win": None}
    for m in reversed(chrono):
        if cur["win"] is None:
            cur["win"] = bool(m["win"])
        if bool(m["win"]) != cur["win"]:
            break
        cur["n"] += 1
    return {"win": best[1], "loss": best[0], "current": cur}


def item_ref(items, iid):
    if not iid:
        return None
    it = items.get(str(iid))
    return {"id": iid, "name": it["name"], "img": it["img"]} if it else None


def region_name(raw):
    """«US WEST» → «US West», «SOUTHAFRICA» → «South Africa»."""
    name = raw.title().replace("Southafrica", "South Africa")
    return re.sub(r"\b(Us|Pw)\b", lambda m: m.group(1).upper(), name)


def slug(name):
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def parse_dur(s):
    sec = 0
    for p in s.split(":"):
        sec = sec * 60 + int(p)
    return sec


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else OD_PER_RUN)
