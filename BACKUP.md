# BACKUP — резервное копирование и восстановление (ТЗ 6.1)

> Этот документ — runbook бэкапов для **Кости (FullStack)** на проде и **Никиты (QA)** на регулярном восстановлении-тесте.
>
> Главный тезис: **бэкап без теста восстановления = иллюзия бэкапа.** Этот документ описывает не только «как бэкапим», но и «как проверяем, что восстанавливается» — отдельный пункт DoD ТЗ 6.1.

## RPO / RTO

| Метрика | Цель | Как достигается |
|---|---|---|
| **RPO** (recovery point objective — сколько данных потеряем) | **≤ 24 ч** | Полный `pg_dump --format=custom` каждый день в 02:00 UTC + `mc mirror` MinIO в 02:30 UTC. Между ночами теряем ≤ сутки правок. |
| **RTO** (recovery time objective — сколько времени восстанавливаемся) | **≤ 4 ч** | Скачать дамп (5 минут на 100 МБ), `pg_restore` в чистый pgvector (~30 минут на 100k чанков, HNSW пересобирается параллельно), `mc mirror` MinIO обратно (≤ 1 ч на 5 ГБ аплоадов), запустить Plane-стек (10 минут). |

Если задача требует RPO < 24 ч (например, регулятор настаивает) — переходим к WAL-стримингу через `pg_basebackup`/`pgbackrest`. Это **отдельная задача** и в текущий гейт не входит.

## Что бэкапится

### 1. Postgres (`plane-db`)

`pg_dump` всей БД `plane` в custom format. Сюда попадает:

- **Plane CE данные** — workspaces, projects, issues, comments, pages, members, permissions.
- **Наши таблицы `ai_*`** (см. [`ai/models.py`](ai/models.py)):
  - `ai_document_chunk` — вектора 1536-d + текст + хеш контента (восстанавливается с HNSW-индексом, см. ниже);
  - `ai_workspace_config` — настройки воркспейса и **зашифрованные** ключи Anthropic/OpenAI (шифрование через `FIELD_ENCRYPTION_KEY`, ключ хранится **вне** дампа — см. ¶ «Что НЕ в бэкапе»);
  - `ai_usage_log` — учёт токенов;
  - `ai_agent_action_log` — append-only аудит агента (ТЗ 5.6);
  - `ai_agent`, `ai_project_settings`.
- **DDL индексов**, включая `ai_chunk_hnsw_idx` (HNSW по cosine, m=16, ef_construction=64) — индекс пересобирается `pg_restore` на этапе post-data.

### 2. MinIO (`uploads`)

`mc mirror` бакета `${AWS_S3_BUCKET_NAME:-uploads}` (вложения задач, аватары, ассеты страниц) в offsite-бакет с `--remove` (удалённые в источнике объекты удаляются и в offsite). Time-travel на offsite-стороне — через bucket versioning + lifecycle (см. ¶ «Offsite retention»).

### Что НЕ в бэкапе (умышленно)

- **`FIELD_ENCRYPTION_KEY`** — ключ шифрования полей `EncryptedCharField` для `anthropic_key` / `openai_key`. Хранится в [SECRETS.md](SECRETS.md) procedure (на проде — в менеджере секретов оператора, например 1Password / Vault). Без этого ключа дамп бесполезен для злоумышленника, но и нам нужен **отдельно**: восстановление = (дамп) + (ключ из секрет-менеджера).
- **Redis / Valkey** (`plane-redis`) — это кэш и сессии. Потеря приемлема: пользователи перелогинятся, кэш заполнится при работе. Бэкап не нужен.
- **RabbitMQ** — очереди задач. Не критично: незавершённые celery-задачи перезапустятся (триггеры идемпотентны — см. `ai/agent_triggers.py`).
- **Docker volumes плана**, кроме `pgdata` и `uploads` — служебные (логи, временные файлы).

## Откуда скрипты

Всё в [`scripts/backup/`](scripts/backup/):

| Файл | Назначение |
|---|---|
| `lib_common.sh` | Общие хелперы — логирование, env-проверки, mc alias setup, retention |
| `backup_postgres.sh` | Делает `pg_dump`, льёт в offsite, чистит локальный спул по retention |
| `backup_minio.sh` | `mc mirror` бакетов в offsite |
| `restore_postgres.sh` | `pg_restore <file|latest>` — поднимает дамп в указанный Postgres |
| `restore_test.sh` | **End-to-end авто-тест восстановления** — спинит throwaway pgvector контейнер, восстанавливает, проверяет HNSW (см. ниже) |
| `Dockerfile` | Образ сайдкара (`postgresql15-client` + `mc` + busybox crond) |
| `entrypoint.sh` | Запускает либо cron, либо один скрипт по имени |
| `crontab` | Расписание (02:00 / 02:30 / 03:00 1-го числа) |

## Конфигурация — env vars

Все скрипты не имеют хардкода вендора. Оператор выбирает S3-совместимое хранилище и кладёт креды в `.env`:

```env
# Plane-DB target — обычно совпадает с тем, что уже есть у Plane.
POSTGRES_USER=plane
POSTGRES_PASSWORD=<from secrets>
POSTGRES_DB=plane
BACKUP_PG_HOST=plane-db
BACKUP_PG_PORT=5432

# Plane MinIO source — берём из тех же AWS_* что и Plane.
AWS_ACCESS_KEY_ID=<plane-minio key>
AWS_SECRET_ACCESS_KEY=<plane-minio secret>
AWS_S3_ENDPOINT_URL=http://plane-minio:9000
AWS_S3_BUCKET_NAME=uploads

# Offsite — оператор выбирает вендора:
#   AWS S3:        https://s3.<region>.amazonaws.com
#   Backblaze B2:  https://s3.<region>.backblazeb2.com
#   Cloudflare R2: https://<account>.r2.cloudflarestorage.com
#   self-MinIO #2: https://backup-minio.<other-dc>.internal
BACKUP_S3_ENDPOINT=https://s3.eu-central-2.amazonaws.com
BACKUP_S3_ACCESS_KEY=<bucket-scoped key>
BACKUP_S3_SECRET_KEY=<bucket-scoped secret>
BACKUP_S3_BUCKET=planeai-prod-backups

# Опционально:
BACKUP_LOCAL_RETENTION_DAYS=14         # сколько дней храним локальный спул
BACKUP_MINIO_BUCKETS="uploads"          # если позже появится отдельный AI-бакет
BACKUP_RESTORE_TEST_MIN_CHUNKS=1       # 0 если на проде ещё пусто
```

**Важно — offsite-бакет должен быть в другом регионе/датацентре.** AWS S3: другой регион, не тот же, где prod-сервер. Backblaze B2: другая локация. Cloudflare R2: одно glob-хранилище, но в settings — другой jurisdiction.

## Запуск — два варианта

### Вариант A — Docker-сайдкар (рекомендуется)

Так бэкапы — часть стека, переезжают вместе с Plane и не требуют доступа к хосту.

```bash
# в .env на прод-хосте уже стоят BACKUP_S3_* и остальные.
docker compose \
  -f deploy-local/docker-compose.yml \
  -f deploy-local/docker-compose.staging.yml \
  -f deploy-local/docker-compose.backup.yml \
  up -d planeai-backup
```

Сайдкар крутит busybox crond в foreground; cron-таблица — в [`scripts/backup/crontab`](scripts/backup/crontab). Логи cron-jobs видны через `docker logs planeai-backup`.

Ad-hoc запуск (без cron) — например, чтобы прогнать тест прямо сейчас:

```bash
docker compose -f ... -f docker-compose.backup.yml \
  run --rm planeai-backup restore_test.sh latest
```

### Вариант B — host-cron + docker exec (для тех, кто не хочет sidecar)

Если в проде нельзя добавлять контейнеры или хочется видеть cron в `journalctl`:

`/etc/cron.d/planeai-backup`:

```cron
# Расписание совпадает с sidecar-вариантом; команды другие.
PATH=/usr/bin:/usr/local/bin
SHELL=/bin/bash
0 2 * * *  root cd /opt/planeai && docker compose exec -T planeai-backup backup_postgres.sh >> /var/log/planeai-backup.log 2>&1
30 2 * * * root cd /opt/planeai && docker compose exec -T planeai-backup backup_minio.sh    >> /var/log/planeai-backup.log 2>&1
0 3 1 * *  root cd /opt/planeai && docker compose exec -T planeai-backup restore_test.sh latest >> /var/log/planeai-restore-test.log 2>&1
```

Альтернатива без сайдкара (host имеет `postgresql-client` и `mc` напрямую):

```cron
0 2 * * *  root  POSTGRES_* AWS_* BACKUP_* env-vars && /opt/planeai/scripts/backup/backup_postgres.sh
```

Под этот сценарий понадобится загрузка env из `/etc/planeai/backup.env` (`. /etc/planeai/backup.env`).

## Offsite retention

Локальный спул чистится `prune_local_spool` (14 дней). На offsite — лучше **lifecycle rule на стороне бакета**, чтобы скрипт не лазил в чужое хранилище:

| Вендор | Где настраивать |
|---|---|
| AWS S3 | Bucket → Management → Lifecycle rules → expire after 90 days; transition to Glacier after 30 |
| Backblaze B2 | Bucket → Lifecycle Settings → keep last 90 days |
| Cloudflare R2 | Object Lifecycle Rules (через wrangler / API) — delete after 90 days |
| self-MinIO | `mc ilm rule add --expire-days 90 offsite/<bucket>` |

Рекомендация: **90 дней full daily**. Это покрывает обнаружение «тихих» багов данных (когда баг внесён, а замечен через 2-3 месяца). При средней БД 200 МБ дамп × 90 = ~18 ГБ на S3 — копейки.

## Восстановление — runbook

Используется при инциденте (потеря prod-БД, повреждение pgdata) ИЛИ при ручном smoke-тесте.

### 1. Поднять чистый Postgres-контейнер

```bash
docker run -d --name plane-db-restore \
  -e POSTGRES_USER=plane -e POSTGRES_PASSWORD=<new-strong-password> -e POSTGRES_DB=plane \
  -p 5432:5432 \
  -v plane-pgdata-restored:/var/lib/postgresql/data \
  pgvector/pgvector:0.8.2-pg15
```

### 2. Скачать дамп

```bash
docker compose run --rm planeai-backup bash -c "
  mc cp \$(mc find offsite/\$BACKUP_S3_BUCKET/planeai-backups/postgres/ --name '*.dump' \
            --print '{time} {key}' | sort -r | head -n1 | awk '{print \$2}') \
        /var/backups/planeai/postgres/
"
```

### 3. Восстановить

```bash
docker compose run --rm planeai-backup restore_postgres.sh /var/backups/planeai/postgres/plane-<TS>.dump
```

Или одной командой через сокращение `latest`:

```bash
docker compose run --rm planeai-backup restore_postgres.sh latest
```

### 4. Восстановить MinIO

```bash
docker compose run --rm planeai-backup mc mirror --overwrite \
  offsite/\$BACKUP_S3_BUCKET/planeai-backups/minio/<Y>/<M>/<D>/uploads \
  planeminio/uploads
```

### 5. Прокинуть `FIELD_ENCRYPTION_KEY`

Без ключа `WorkspaceAIConfig.anthropic_key` / `openai_key` нельзя расшифровать. Положить ключ из секрет-менеджера в `.env`:

```env
FIELD_ENCRYPTION_KEY=<тот же ключ, что был на момент дампа>
```

### 6. Сменить `DATABASE_URL` Plane → новый Postgres, перезапустить стек

```bash
docker compose up -d
```

### 7. Проверить вживую

- Открыть Plane UI, залогиниться.
- Открыть AI-поиск (TZ 2.6) — задать запрос → должны прийти `sources` + `delta` стрим.
- Открыть `/api/ai/workspaces/<id>/index-status/` → `ready: true`.

## Тест восстановления — обязательная часть гейта

Это пункт DoD, который часто игнорируется в реальных проектах. У нас он автоматизирован: [`scripts/backup/restore_test.sh`](scripts/backup/restore_test.sh).

**Что проверяет:**

| Проверка | Почему важна |
|---|---|
| `vector` extension загружен | Без него COPY `ai_document_chunk` упадёт сразу |
| `ai_chunk_hnsw_idx` присутствует | DDL индекса сохранён в дампе |
| `ai_document_chunk` имеет строки | Дамп не «пустая оболочка» |
| **ANN-запрос ИСПОЛЬЗУЕТ HNSW (через `EXPLAIN`)** | Самое главное — индекс не просто «есть на диске», он реально работает. Если HNSW побит — `EXPLAIN` покажет seq scan, тест валится |
| `workspaces` / `projects` присутствуют | Дамп не пропустил Plane-схему |

**Запуск вручную:**

```bash
docker compose run --rm planeai-backup restore_test.sh latest
```

**Расписание автомат-теста:** 03:00 UTC 1-го числа каждого месяца (см. [crontab](scripts/backup/crontab)). Падение → docker logs → алерт.

**Что делать при FAIL:**

| Что не прошло | Действие |
|---|---|
| `vector extension present FAIL` | Проверить, что `pgvector/pgvector` image используется в `restore_postgres.sh` target; pg_dump его требует |
| `ai_chunk_hnsw_idx index present FAIL` | Дамп старый (до миграции 0001_enable_pgvector). Откатиться к более свежему |
| `ANN query did not use HNSW FAIL` | Самый плохой случай: индекс есть, но битый. Запустить `REINDEX INDEX CONCURRENTLY ai_chunk_hnsw_idx` после restore, добавить эту команду в `restore_postgres.sh` (пока — НЕ нужна по нашему опыту с pgvector 0.8.2) |
| `plane table workspaces missing` | Дамп взят не из той БД. Проверить `BACKUP_PG_DB` и логи `backup_postgres.sh` за день дампа |

## Алерты

В этой задаче алерт-инфраструктура (`Alertmanager`/Sentry/etc.) не настраивается отдельно — это [ТЗ 6.3 «Логи и алерты»](tz/sprint-6/). До тех пор оператор смотрит `docker logs planeai-backup` ежедневно и явно реагирует на:

- exit code ≠ 0 от `backup_postgres.sh` / `backup_minio.sh`,
- `pg_restore --list failed` (дамп коррапт),
- любой `FAIL` в выводе `restore_test.sh`.

После того как 6.3 будет внедрено — добавить scraping `docker events` + `docker inspect --format '{{.State.ExitCode}}'` в Alertmanager.

## Связи

- Закрывает DoD [ТЗ 6.1](tz/sprint-6/01-задача-6.1-бэкап-restore.md).
- Используется в [ТЗ 6.5 «Процедура отката»](tz/sprint-6/) — откат включает в себя восстановление дампа.
- Используется в [ТЗ 6.6 «Приёмочный прогон»](tz/sprint-6/) — там Никита прогоняет ручной `restore_test.sh` как часть приёмки.
- Связано с [SECRETS.md](SECRETS.md) — `FIELD_ENCRYPTION_KEY` хранится отдельно от дампов и **обязателен** для расшифровки.
