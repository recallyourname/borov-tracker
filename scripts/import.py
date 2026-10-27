#!/usr/bin/env python3
"""Заливает старые матчи в data/borov.db.

  python3 scripts/import.py matches.json        # список матчей, снятый со страниц Dotabuff
  python3 scripts/import.py --od-dir DIR        # папка с ответами OpenDota /matches/{id} (<id>.json)

Формат matches.json: [{"id", "start" (ISO), "hero_name", "win", "type", "mode", "duration" (сек), "k", "d", "a"}].
После импорта: python3 scripts/update.py 1000 — догрузит детали из OpenDota и пересоберёт data.json.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import update  # noqa: E402


def main(argv):
    db = update.connect()
    if argv[:1] == ["--od-dir"]:
        n = 0
        for f in sorted(Path(argv[1]).glob("*.json")):
            d = json.loads(f.read_text())
            mid = d.get("match_id")
            p = next((p for p in d.get("players", []) if p.get("account_id") == update.ACCOUNT), None)
            if not mid or not p:
                continue
            radiant = p["player_slot"] < 128
            update.upsert_basic(db, {
                "id": mid,
                "start": datetime.fromtimestamp(d["start_time"], timezone.utc).isoformat(timespec="seconds"),
                "hero_name": "", "win": int(d["radiant_win"] == radiant),
                "type": "Ranked" if d.get("lobby_type") == 7 else "Unranked",
                "duration": d.get("duration"), "k": p.get("kills"), "d": p.get("deaths"), "a": p.get("assists"),
            }, "opendota-cache")
            update.apply_opendota(db, mid, d)
            n += 1
    else:
        rows = json.loads(Path(argv[0]).read_text())
        for r in rows:
            update.upsert_basic(db, r, "dotabuff-history")
        n = len(rows)
    db.commit()
    print(f"imported {n}")


if __name__ == "__main__":
    main(sys.argv[1:])
