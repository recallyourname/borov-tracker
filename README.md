# borov-tracker

Дашборд с примерным MMR, последними 20 матчами и статистикой игрока Dota 2 `226241953`.

- `scripts/update.py` — сборщик: список матчей из Steam Web API (секрет `STEAM_API_KEY`, без него — Dotabuff через r.jina.ai) + OpenDota по ID матча. Пишет SQLite `data/borov.db` и `data.json`, который читает страница.
- `scripts/import.py` — заливка старых матчей в базу.
- `history.json` — все матчи с 2015 года в компактном виде для блока «Вся история»; собирает `scripts/update.py`. Рейтинг и остальная статистика по-прежнему считаются с `HISTORY_FROM`.
- `.github/workflows/update.yml` — запуск каждые 15 минут, коммит данных и деплой на GitHub Pages.
- `index.html` — страница, в ней же казино и игра «Поросёнок» (кнопка над казино; рекорд и скин хранятся в браузере).

Локально: `python3 scripts/update.py && python3 -m http.server`.
