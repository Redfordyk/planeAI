# pgvector — версия и CVE-2026-3172 (ТЗ 0.4)

## TL;DR

| | |
|---|---|
| Было | `postgres:15.7-alpine` — расширение `vector` **не установлено и недоступно** (`pg_available_extensions` пуст для `vector`) |
| Стало | `pgvector/pgvector:0.8.2-pg15` (в `deploy-local/docker-compose.override.yml`) |
| `SELECT extversion FROM pg_extension WHERE extname='vector';` | `0.8.2` ✅ |
| CVE-2026-3172 (buffer overflow в parallel HNSW build, CVSS 8.1) | закрыт версией 0.8.2 |
| Workaround при невозможности апгрейда | `SET max_parallel_maintenance_workers = 0;` сессионно перед `CREATE INDEX ... USING hnsw` — нам не нужен |

## Что было сделано

1. Проверили текущее состояние:
   ```sh
   docker compose exec -e PGPASSWORD=plane plane-db \
     psql -U plane -d plane -tAc \
     "SELECT extversion FROM pg_extension WHERE extname='vector';"
   # => пусто (расширение не установлено)

   docker compose exec -e PGPASSWORD=plane plane-db \
     psql -U plane -d plane -tAc \
     "SELECT name, version FROM pg_available_extension_versions WHERE name='vector';"
   # => пусто (бинарей расширения нет в образе)
   ```
   Дефолтный образ Plane (`postgres:15.7-alpine`) вообще не содержит pgvector.

2. В `deploy-local/docker-compose.override.yml` подменили образ `plane-db`:
   ```yaml
   services:
     plane-db:
       image: pgvector/pgvector:0.8.2-pg15
   ```
   Этот тег официальный, постгрес мажор 15 (та же мажорная версия, что и раньше — Plane совместим), pgvector версии 0.8.2 (фикс CVE).

3. Пересоздали базу с нуля:
   ```powershell
   docker compose -p plane-ce down -v   # снос pgdata-volume
   docker compose -p plane-ce up -d     # пересборка
   ```
   `down -v` нужен, потому что переход с Alpine (musl) на Debian (glibc) бинарей Postgres делает старый pgdata несовместимым по сортировкам/коллациям. На прототипе данных не было — потеря пустая.
   На проде делать **бэкап → swap image → restore через `pg_dump` / `pg_restore`** (не копированием volume).

4. Дождались `migrator` → `Exited (0)`, проверили что Plane жив (UI отдаёт `200 OK` на `http://localhost:8088/`).

5. Создали расширение и проверили версию:
   ```sh
   docker compose exec -e PGPASSWORD=plane plane-db \
     psql -U plane -d plane -c "CREATE EXTENSION IF NOT EXISTS vector;"
   docker compose exec -e PGPASSWORD=plane plane-db \
     psql -U plane -d plane -tAc \
     "SELECT extversion FROM pg_extension WHERE extname='vector';"
   # => 0.8.2
   ```

## Почему 0.8.2 — нижняя граница

- **0.7.x, 0.8.0, 0.8.1**: уязвимы (CVE-2026-3172, buffer overflow при параллельной сборке HNSW-индекса). При неудачной сборке возможен либо краш сервера, либо чтение чужой памяти процесса Postgres — а в этой памяти могут лежать данные несвязанных таблиц/баз.
- HNSW-индекс мы будем строить в спринте 1 (ТЗ 1.2) поверх таблицы `DocumentChunk.embedding`. Это ровно тот путь кода, где срабатывает CVE.
- Хот-фикс на месте требовал бы либо `SET max_parallel_maintenance_workers = 0;` на сессии (до момента `CREATE INDEX`), либо опасной молитвы. Простой апгрейд образа лучше.

## Совместимость с Plane

После пересборки:

- Все 12 сервисных контейнеров `Up`, `migrator Exited (0)`.
- `python manage.py migrate` Plane прошёл целиком, миграции не сломались на чужом дистрибутиве Postgres.
- UI отвечает 200 на `:8088/`.

## Что осталось на след. итерации

- Перед задачей 1.2 убедиться, что миграция `ai.0001_initial` (создающая `DocumentChunk.embedding VECTOR(1536)` + HNSW индекс) выполняется без `SET max_parallel_maintenance_workers`. С 0.8.2 — должна.
- На проде: создавать backup-cron `pg_dump` до того, как `DocumentChunk` начнёт расти. Volume-копирование на проде запрещено — только pg_dump.

## Связи

- Опирается на ТЗ 0.1.
- Разблокирует ТЗ 1.2 (миграции pgvector + HNSW).
