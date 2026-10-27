# borov-tracker

Дашборд с примерным MMR, последними 20 матчами и статистикой игрока Dota 2 `226241953`.

- `scripts/update.py` — сборщик: Dotabuff (через r.jina.ai) + OpenDota по ID матча. Пишет SQLite `data/borov.db` и `data.json`, который читает страница.
- `scripts/import.py` — заливка старых матчей в базу.
- `.github/workflows/update.yml` — запуск каждые 15 минут, коммит данных и деплой на GitHub Pages.
- `index.html` — страница.

Локально: `python3 scripts/update.py && python3 -m http.server`.
