# borov-tracker

Лендинг с примерным MMR и последними 20 матчами игрока Dota 2 `226241953`.

- `scripts/update.py` — сборщик: Dotabuff (через r.jina.ai) + OpenDota по ID матча. Пишет `data/state.json` (накопленная история) и `data.json` (то, что читает страница).
- `.github/workflows/update.yml` — запуск каждые 15 минут, коммит данных.
- `index.html` — страница, GitHub Pages из корня `main`.

Локально: `python3 scripts/update.py && python3 -m http.server`.
