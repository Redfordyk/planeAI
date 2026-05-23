# RUNBOOK — planeAI от пустого сервера до первого поиска (ТЗ 6.4)

> **Кому это.** Любой член команды (Костя, Вова, Эдик, Никита, Илья) или новый человек должен по этому документу поднять planeAI на чистом Ubuntu-сервере и сделать первый ИИ-поиск **без устной помощи**. Если на каком-то шаге застрял — это баг runbook'a, дополни шаг или troubleshooting-секцию.
>
> **Что внутри.** Главы 1–10 — линейная последовательность развёртывания. Главы 11–12 — операционка (что делать, когда стоит). Глава 13 — индекс всех документов проекта.

---

## Содержание

| # | Глава | Когда нужна |
|---|---|---|
| 1 | [Что мы строим](#1-что-мы-строим) | Первое знакомство |
| 2 | [Подготовка сервера](#2-подготовка-сервера) | Чистый Ubuntu |
| 3 | [Развёртывание Plane CE](#3-развёртывание-plane-ce) | Шаг 1 |
| 4 | [Сборка и подмена образа `plane-backend-ai`](#4-сборка-и-подмена-образа-plane-backend-ai) | Шаг 2 |
| 5 | [pgvector + миграции `ai`](#5-pgvector--миграции-ai) | Шаг 3 |
| 6 | [Секреты и `WorkspaceAIConfig`](#6-секреты-и-workspaceaiconfig) | Шаг 4 |
| 7 | [Бэкафилл и `index-status`](#7-бэкафилл-и-index-status) | Шаг 5 |
| 8 | [Первый поиск (smoke)](#8-первый-поиск-smoke) | Шаг 6 |
| 9 | [Мониторинг + алерты](#9-мониторинг--алерты) | Шаг 7 |
| 10 | [Бэкап + restore-тест](#10-бэкап--restore-тест) | Шаг 8 |
| 11 | [Troubleshooting](#11-troubleshooting) | Когда стоит |
| 12 | [Эксплуатация](#12-эксплуатация) | Когда работает |
| 13 | [Индекс документов](#13-индекс-документов) | Когда нужна деталь |

---

## 1. Что мы строим

ИИ-надстройка над self-hosted Plane CE 1.3.1: семантический поиск, авто-саммари, bulk-операции на естественном языке, агенты для триажа. Plane мы **не форкаем** — добавляем отдельное Django-app `ai` через свой образ `plane-backend-ai` (см. [IMAGE.md](IMAGE.md)). Embeddings — OpenAI, чат — Claude. Бюджет токенов на воркспейс. Алерты по расходу и петлям агента.

Подробный контекст и инварианты — [CLAUDE.md](CLAUDE.md). Архитектура решений — там же.

## 2. Подготовка сервера

**Параметры (prod):** 4 vCPU, **8 ГБ RAM**, 40 ГБ SSD, Ubuntu 24.04 LTS, регион EU (Франкфурт). GPU не нужен — вся инференция в облаке.

Проверка перед началом:

```bash
# 1. Ресурсы
lscpu | grep "^CPU(s):"        # >= 4
free -g | awk '/^Mem:/ {print $2}'   # >= 7  (kernel ест ~0.5)
df -h /                         # >= 40 GB свободно

# 2. Docker
docker --version                # >= 24.x
docker compose version          # >= 2.20 (subcommand, не docker-compose)

# 3. Сеть
curl -fsS https://api.anthropic.com -o /dev/null   # 200
curl -fsS https://api.openai.com -o /dev/null      # 200/403 OK; важно не connection-error

# 4. Время — критично для TLS и алертов
timedatectl status | grep "synchronized: yes"

# 5. Никаких 80/443 не занято
sudo ss -lntp | grep -E ":(80|443)\s"     # должно быть пусто
```

Если что-то из этого не выполнено — НЕ продолжай. См. [Troubleshooting](#11-troubleshooting) → "OOM при docker build" (для маленькой RAM) и "connection error к api.anthropic.com" (для firewall'a).

## 3. Развёртывание Plane CE

Полная спецификация и обоснование параметров — [README-deploy.md](README-deploy.md).

```bash
# Клон репозитория
git clone https://github.com/Redfordyk/planeAI.git
cd planeAI

# Конфиг
cp deploy-local/.env.example deploy-local/.env
# Отредактировать: на проде LISTEN_HTTP_PORT=80, LISTEN_HTTPS_PORT=443,
# APP_DOMAIN=<реальный домен>, CERT_EMAIL=<реальный e-mail>.
# Заменить все CHANGE_ME — см. главу 6.
vi deploy-local/.env

# Поднять upstream Plane (12 сервисов)
cd deploy-local
docker compose -p plane-ce up -d

# Дождаться migrator → Exited 0
docker inspect -f '{{.State.Status}} {{.State.ExitCode}}' plane-ce-migrator-1
# → exited 0

# Smoke: UI отвечает 200
curl -sS -o /dev/null -w "HTTP %{http_code}\n" http://localhost
# или: http://${APP_DOMAIN}:8088 на dev
```

Должны быть `Up` все 12 сервисов: `api`, `worker`, `beat-worker`, `web`, `space`, `admin`, `live`, `plane-db`, `plane-redis`, `plane-mq`, `plane-minio`, `proxy`. `migrator` — `Exited (0)`.

**Войти в UI → создать первого админа → создать тестовый воркспейс.** Запомни его `slug` и `id` (UUID видно в URL `/<slug>/settings/`). `id` понадобится для AI-конфига.

## 4. Сборка и подмена образа `plane-backend-ai`

Обоснование подхода — [IMAGE.md](IMAGE.md). Кратко: мы не патчим upstream-файлы Plane, а кладём свой settings-модуль `plane.settings.production_ai` рядом и собираем кастомный образ `plane-backend-ai:<tag>` поверх официального.

```bash
# На проде образы тянутся из GHCR через CI/CD (см. CICD.md).
# Для bring-up "с нуля" собираем локально:
cd ~/planeAI
docker build -f Dockerfile.ai -t plane-backend-ai:local .

# Подмена. На LOCAL/DEV действует deploy-local/docker-compose.override.yml,
# на STAGING/PROD — docker-compose.staging.yml. Они взаимоисключающие:
#   - override.yml активируется автоматически в той же папке;
#   - staging.yml активируется флагом -f и берёт ${PLANE_AI_IMAGE} из env.
# На проде:
cd ~/planeAI/deploy-local
docker compose \
  -f docker-compose.yml \
  -f docker-compose.staging.yml \
  up -d

# Проверка: settings-модуль действительно production_ai
docker compose -p plane-ce exec -T api python -c "
import django; django.setup()
from django.conf import settings
print('ai-app:', 'ai' in settings.INSTALLED_APPS,
      'settings:', settings.SETTINGS_MODULE)
"
# → ai-app: True settings: plane.settings.production_ai
```

Образ заменяет backend у **четырёх** сервисов: `api`, `worker`, `beat-worker` и **`migrator`** (без migrator не накатятся миграции `ai`).

## 5. pgvector + миграции `ai`

[PGVECTOR.md](PGVECTOR.md) объясняет, почему мы пинимся в `pgvector/pgvector:0.8.2-pg15` (CVE-2026-3172 в parallel HNSW build). Если ты деплоишь на чистый сервер — override-файл уже подменил образ, остаётся накатить миграции и расширение.

```bash
# 1. Расширение vector (одноразово)
docker compose -p plane-ce exec -e PGPASSWORD=$POSTGRES_PASSWORD plane-db \
  psql -U plane -d plane -c "CREATE EXTENSION IF NOT EXISTS vector;"
# → CREATE EXTENSION  (или: extension "vector" already exists)

# Проверка версии
docker compose -p plane-ce exec -e PGPASSWORD=$POSTGRES_PASSWORD plane-db \
  psql -U plane -d plane -tAc \
  "SELECT extversion FROM pg_extension WHERE extname='vector';"
# → 0.8.2

# 2. Миграции ai (если migrator не прошёл их при старте, гонит вручную)
docker compose -p plane-ce exec -T api python manage.py migrate ai
# → 0001_initial, 0002_*  → OK

# 3. Подтверждение таблиц
docker compose -p plane-ce exec -e PGPASSWORD=$POSTGRES_PASSWORD plane-db \
  psql -U plane -d plane -c "\dt ai_*"
# → ai_document_chunk, ai_workspace_config, ai_usage_log, ai_agent,
#    ai_agent_action_log, ai_project_settings
```

См. также [SCHEMA.md](SCHEMA.md) — почему мы НЕ добавляем колонки на чужие модели Plane (CLAUDE.md инвариант 6).

## 6. Секреты и `WorkspaceAIConfig`

Полный список секретов и ротация — [SECRETS.md](SECRETS.md). На bring-up из абсолютного нуля три обязательных значения:

```bash
# 1. FIELD_ENCRYPTION_KEY — для шифрования ключей в WorkspaceAIConfig.
#    ⚠️ Если потеряешь — все зашифрованные ключи становятся нечитаемыми.
#    Скопировать в .env И в защищённый бэкап (1Password / GPG).
docker compose -p plane-ce run --rm api python /code/scripts/gen_encryption_key.py
# → 44-байтовая строка, например 6T8...= 

# 2. Положить в .env:
#       FIELD_ENCRYPTION_KEY=<сгенерированная строка>
#       ANTHROPIC_API_KEY=<ключ под DPA>
#       OPENAI_API_KEY=<ключ под DPA>
#       SECRET_KEY=<openssl rand -hex 32>
#       LIVE_SERVER_SECRET_KEY=<openssl rand -hex 32>
#       POSTGRES_PASSWORD / RABBITMQ_PASSWORD / AWS_* — стойкие пароли
vi deploy-local/.env

# 3. Рестарт backend-сервисов (без пересборки) — чтобы прочитали новый env
docker compose -p plane-ce up -d api worker beat-worker
```

Теперь нужно создать `WorkspaceAIConfig` для тестового воркспейса (там лежат **зашифрованные** на уровне БД ключи, бюджет, выбор моделей):

```bash
docker compose -p plane-ce exec -T api python manage.py shell <<'PY'
from ai.models import WorkspaceAIConfig
from plane.db.models import Workspace
ws = Workspace.objects.get(slug="<slug>")
WorkspaceAIConfig.objects.update_or_create(
    workspace=ws,
    defaults=dict(
        anthropic_key="<sk-ant-...>",
        openai_key="<sk-...>",
        chat_model="claude-sonnet-4-6",
        embed_model="text-embedding-3-small",
        monthly_token_budget=5_000_000,   # дефолт ~$20-30 для Sonnet
        enabled=True,
    ),
)
print("WorkspaceAIConfig OK:", ws.id)
PY
```

**Запомни workspace.id** — он понадобится для бэкафилла и эндпоинтов.

> **Зачем ключ и в `.env`, и в `WorkspaceAIConfig`?** В `.env` лежат служебные ключи для CI/staging-смоков и инфраструктурных задач. В `WorkspaceAIConfig` — **прод-ключ воркспейса**, шифрованный `FIELD_ENCRYPTION_KEY`. ИИ-фичи читают только из `WorkspaceAIConfig`. Это даёт изоляцию: разные воркспейсы могут использовать разные аккаунты Anthropic/OpenAI.

## 7. Бэкафилл и `index-status`

Ингест-сигналы ([ai/signals.py](ai/signals.py)) ловят только **новые** изменения. Чтобы проиндексировать уже существующие задачи/комментарии/страницы — однократный бэкафилл:

```bash
# Бэкафилл всего воркспейса. --rate 3 = 3 task/сек, чтобы не упереться
# в OpenAI rate-limit. По умолчанию идёт по всем трём source_type-ам.
docker compose -p plane-ce exec -T worker python manage.py backfill_embeddings \
  --workspace <workspace-id> \
  --rate 3

# Прогресс
curl -fsS \
  -H "Cookie: $(cat ~/.plane-cookie)" \
  http://localhost/api/ai/workspaces/<workspace-id>/index-status/ \
  | python -m json.tool
# →
#   {
#     "workspace_id": "...",
#     "total": 512,
#     "indexed": 510,
#     "coverage": 1.0,
#     "ready": true,             ← когда coverage >= 0.95
#     "by_source": {
#       "work_item": {"total": 340, "indexed": 340, "coverage": 1.0},
#       "comment":   {"total": 150, "indexed": 150, "coverage": 1.0},
#       "page":      {"total":  22, "indexed":  20, "coverage": 0.91}
#     }
#   }
```

Поллить раз в 30 секунд. Когда `ready=true` — поиск готов к smoke-проверке. Бэкафилл идемпотентен (короткое замыкание по `content_hash`), повторный запуск ничего не стоит.

> **Внимание на проде:** бэкафилл отправляет **весь** текст задач/комментариев/страниц в OpenAI на эмбеддинг. На staging/dev это OK (синтетика). На проде — гейт через [TZ 6.6 production checklist](tz/sprint-6/06-задача-6.6-приёмочный-прогон.md): DPA, исключённые проекты (`AIProjectSettings.exclude_from_ai`), оценка стоимости.

## 8. Первый поиск (smoke)

```bash
# Куку сессии получить через UI (Network → Application → Cookies → sessionid)
COOKIE="sessionid=...; csrftoken=..."

curl -N \
  -H "Cookie: $COOKIE" \
  -H "Content-Type: application/json" \
  -d '{"query": "что у нас открыто по платежам?", "top_k": 10}' \
  http://localhost/api/ai/workspaces/<workspace-id>/search/

# Ответ — text/event-stream:
#   data: {"sources": [{"work_item_id": "...", "name": "..."}]}
#
#   data: {"delta": "У вас "}
#   data: {"delta": "две открытые задачи "}
#   ...
#   data: {"done": true, "usage": {"model": "claude-sonnet-4-6", "cost_usd": "0.0123"}}
```

Frame-контракт зафиксирован в [STREAMING.md](STREAMING.md). Если первый `sources` frame не пришёл за ~3 секунды — буферизация на proxy; см. [Troubleshooting](#11-troubleshooting) → "SSE не стримит".

Принимочный сценарий поиска целиком — [SPRINT-2-ACCEPTANCE.md](SPRINT-2-ACCEPTANCE.md) (закрывает TZ 2.9). Прогнать его на staging перед раскаткой на прод.

## 9. Мониторинг + алерты

Полный runbook — [MONITORING.md](MONITORING.md) (TZ 6.2). Здесь — только bring-up.

```bash
# Добавить env (см. MONITORING.md "env-конфиг")
echo "PLANEAI_METRICS_TOKEN=$(openssl rand -hex 32)" >> deploy-local/.env
echo "PLANEAI_ALERT_WEBHOOK_TOKEN=$(openssl rand -hex 32)" >> deploy-local/.env
echo "ALERT_WEBHOOK_TOKEN=\${PLANEAI_ALERT_WEBHOOK_TOKEN}" >> deploy-local/.env
echo "ALERT_WEBHOOK_URL=https://hooks.slack.com/services/..." >> deploy-local/.env
echo "ALERT_WEBHOOK_FORMAT=slack" >> deploy-local/.env
echo "GF_SECURITY_ADMIN_PASSWORD=$(openssl rand -hex 16)" >> deploy-local/.env

# Запуск
cd deploy-local
docker compose \
  -f docker-compose.yml \
  -f docker-compose.staging.yml \
  -f docker-compose.monitoring.yml \
  up -d prometheus alertmanager grafana

# Smoke
curl -sS -H "X-Metrics-Token: $PLANEAI_METRICS_TOKEN" \
     http://localhost:8000/api/ai/metrics/ | head -20
# → # HELP planeai_provider_errors_total ...

curl -sS http://localhost:8000/api/ai/health/ | python -m json.tool
# → status: ok, все checks: ok

curl -sS http://localhost:9090/api/v1/targets | python -c \
  "import sys,json; d=json.load(sys.stdin); print(*[(t['labels']['job'], t['health']) for t in d['data']['activeTargets']], sep='\n')"
# → planeai up, prometheus up

# Grafana — http://<host>:3000, login admin / $GF_SECURITY_ADMIN_PASSWORD
# Dashboard "planeAI — overview" должен быть provision-нут автоматически.
```

7 алертов на проде должны иметь зелёный статус в Alertmanager UI (`http://<host>:9093`). Прогнать [acceptance](MONITORING.md#реакция-на-алерты--runbook) — искусственно поднять долю бюджета до 0.85, дождаться `PlaneAIBudgetWarning`, проверить, что сообщение пришло в Slack.

## 10. Бэкап + restore-тест

Полный runbook — [BACKUP.md](BACKUP.md) (TZ 6.1). На bring-up:

```bash
# Поднять backup-сайдкар (запускается по cron внутри контейнера, 03:00 UTC)
cd deploy-local
docker compose \
  -f docker-compose.yml \
  -f docker-compose.staging.yml \
  -f docker-compose.monitoring.yml \
  -f docker-compose.backup.yml \
  up -d planeai-backup

# Принудительный первый бэкап — для смок-проверки
docker compose exec planeai-backup /usr/local/bin/backup_postgres.sh
docker compose exec planeai-backup /usr/local/bin/backup_minio.sh

# Принудительный restore-тест (запускается еженедельно)
docker compose exec planeai-backup /usr/local/bin/restore_test.sh
# → OK: restored, row counts match within tolerance
```

DoD ТЗ 6.1 — restore-тест прошёл хотя бы раз. На проде он запускается еженедельно автоматом, цифры идут в лог `restore_test.log`.

---

## 11. Troubleshooting

Симптом → причина → фикс. Все эти грабли уже наступили хоть раз в спринтах 0–6, поэтому они здесь.

| Симптом | Причина | Фикс |
|---|---|---|
| `docker build` падает с `Killed` или `Cannot allocate memory`. | Хост < 8 ГБ RAM. Билд node-зависимостей жрёт ~3 ГБ. | Только увеличить RAM или собирать в CI. Не лечится. |
| После `up -d` сервис `api` в `Restarting`, в логах `relation "ai_document_chunk" does not exist`. | Миграция `ai` не накатилась — либо migrator не использовал наш образ, либо упал. | Глава 4. Проверить, что `migrator` использует `plane-backend-ai:*`. Доcат `migrate ai` вручную. |
| `psql: ERROR: extension "vector" is not available`. | Postgres-образ дефолтный (`postgres:15.7-alpine`), не `pgvector/pgvector:*`. | Применить `deploy-local/docker-compose.override.yml` (dev) или `staging.yml` (prod) — там подмена. |
| После апгрейда pgvector (с Alpine на Debian) Postgres не стартует, ругается на collation. | Несовместимые бинарные форматы pgdata между musl и glibc. | На проде ⇒ pg_dump → swap image → pg_restore. На dev можно `docker compose down -v` (теряем данные). [PGVECTOR.md](PGVECTOR.md). |
| SSE-поиск шлёт ответ только в конце одной плюхой; нет инкрементального стрима. | Буферизация на gunicorn/uvicorn или на proxy. | Должен быть **gunicorn + uvicorn.workers.UvicornWorker** (ASGI), `--worker-class uvicorn.workers.UvicornWorker`. Заголовки ответа: `X-Accel-Buffering: no`, `Cache-Control: no-cache`. [STREAMING.md](STREAMING.md). |
| `RuntimeError: SynchronousOnlyOperation: You cannot call this from an async context`. | ORM-вызов из async-генератора в `ai/streaming.py` без `sync_to_async`. | Обернуть в `asgiref.sync.sync_to_async(thread_sensitive=False)(...)`. См. `ai/views.py:SearchView.post()` как референс. |
| OpenAI отдаёт 429 на бэкафилле; очередь Celery растёт. | `--rate` слишком высокий или маленький tier OpenAI. | Перезапустить с `--rate 1`. Если воспроизводится — поднять tier или ослабить `--rate 0.5`. Алерт `PlaneAIProviderErrors` сработает, см. [MONITORING.md](MONITORING.md). |
| Бэкафилл "застрял" — `index-status` не растёт 30+ мин. | Celery worker умер / завис на одной задаче. | `docker logs plane-worker --tail 200`. Перезапуск воркера. Очередь не теряется — задачи в RabbitMQ. Алерт `PlaneAIBackfillStuck`. |
| `WorkspaceAIConfig.objects.get(...)` возвращает запись, но `cfg.anthropic_key` пустая строка. | `FIELD_ENCRYPTION_KEY` сменился — старые шифротексты больше не дешифруются (или прокрутили ключ без re-encrypt). | Восстановить **исходный** `FIELD_ENCRYPTION_KEY` ([SECRETS.md](SECRETS.md) → "Ротация" → процедура с `FIELD_ENCRYPTION_KEYS`). Если потерян безвозвратно — переcоздать `WorkspaceAIConfig` руками. |
| ИИ-эндпоинты отвечают 429 даже на одну задачу. | Месячный лимит токенов исчерпан. | Бюджет-дашборд ([BUDGET.md](BUDGET.md)) → понять, какая фича жжёт. Поднять `monthly_token_budget` или выключить агента. |
| Алерт `PlaneAIAgentLoop` → 30+ действий за 5 мин на одном воркспейсе. | Агент пишет комментарий → его пост_save триггерит самого агента → петля. | **Немедленно** отключить агента (UI: `/<ws>/settings/ai-agent` → toggle Off, или через Django shell — см. [MONITORING.md](MONITORING.md#planeaiagentloop--критический)). |
| Дашборд расхода ([BUDGET.md](BUDGET.md)) показывает нули по фиче, которая реально работает. | Фича не вызывает `record_usage(feature=...)` — токены пишутся в `intent_search` по дефолту `claude_sse`. | Найти LLM-вызов фичи, передать `feature=AIUsageLog.FEATURE_*` явно. Регресс зафиксирован в `test_by_feature_pads_all_five_features`. |
| Health-эндпоинт отдаёт 503, секция `vector_ext` — `down`. | Расширение `vector` создано в другой БД / отключено. | `CREATE EXTENSION IF NOT EXISTS vector;` (глава 5). |
| Health-эндпоинт `degraded`, `index_freshness` < 0.85. | Бэкафилл не закончился или ингест отстаёт. | Подождать; если не растёт > 30 мин — см. "бэкафилл застрял". |
| `connection error к api.anthropic.com` в логах воркера. | Firewall режет egress. | Открыть TCP/443 на `api.anthropic.com`, `api.openai.com`. EU-регион предпочтительно. |
| После рестарта Plane `Page` не индексируется — `coverage=0` для source `page`. | Pages не имеют прямого FK на Project (см. [SCHEMA.md](SCHEMA.md) §db.Page). | Не баг, а ограничение upstream. Реализация TZ 1.5 учитывает M2M через `db.ProjectPage`. |
| Логи API содержат `sk-ant-...` или `sk-...`. | Где-то осталось `logger.*(f"key={key}")`. | Срочный фикс кода + ротация ключа ([SECRETS.md](SECRETS.md) → "Ротация" → "если ключ когда-либо появился..."). |

Если симптом не из списка — открыть тикет с тегом `runbook:add`. Пополнить таблицу — обязанность того, кто наступил.

---

## 12. Эксплуатация

### Когда смотреть бюджет

- **Еженедельно (PM):** `Workspace → settings → ИИ-расход` (UI компонент [`UsagePage`](apps/web/core/components/ai/usage-page.tsx)). Прогресс vs `monthly_token_budget`, разбивка по фичам/моделям, топ-10 пользователей. Документ — [BUDGET.md](BUDGET.md).
- **Алерты:** `PlaneAIBudgetWarning` (≥80%) и `PlaneAIBudgetCritical` (≥95%) приходят в командный канал. См. [MONITORING.md → Реакция на алерты](MONITORING.md#реакция-на-алерты--runbook).
- **API:** `GET /api/ai/workspaces/<id>/usage/stats/` (только админ воркспейса).

### Когда сработал алерт

Полный playbook — [MONITORING.md](MONITORING.md). Главное:

1. **`PlaneAIBudgetCritical`** — финансовый предохранитель. Решение: поднять бюджет (через Django shell — см. [BUDGET.md](BUDGET.md)) или временно отключить агента (UI toggle).
2. **`PlaneAIAgentLoop` — critical**. Немедленно: отключить агента в воркспейсе (см. таблицу Troubleshooting выше).
3. **`PlaneAIBackfillStuck`** — `docker logs plane-worker`, перезапуск воркера.
4. **`PlaneAIProviderErrors`** — `status.anthropic.com` / `status.openai.com`. Если инцидент провайдера — ждать, retries в нашем `providers.py` уже работают.
5. **`PlaneAIMetricsDown`** — упал сам API или поменялся токен метрик; `docker logs api`, рестарт Prometheus.

### Когда нужно восстановиться из бэкапа

Полный playbook — [BACKUP.md](BACKUP.md) (TZ 6.1). Кратко:

- **Постгрес.** `scripts/backup/restore_postgres.sh <dump-file>`. RTO < 4 ч.
- **MinIO.** `mc mirror` обратным направлением (offsite → local). Параллельно с pg_restore.
- **WorkspaceAIConfig.** Зашифрован `FIELD_ENCRYPTION_KEY` — этот ключ должен быть в **отдельном** бэкапе (1Password / GPG), не вместе с pgdump. Иначе кража одного бэкапа = кража ключей.
- **Restore-тест.** Запускается еженедельно по cron в `planeai-backup` сайдкаре. Лог — `restore_test.log` в его volume.

### Когда нужно откатиться

Документ TZ 6.5 — [tz/sprint-6/05-задача-6.5-план-отката.md](tz/sprint-6/05-задача-6.5-план-отката.md). Базовый сценарий:

1. **Откат фичи** — поменять флаг в `WorkspaceAIConfig` (отключить `enabled` целиком или для одного воркспейса).
2. **Откат образа** — `docker compose pull plane-backend-ai:<предыдущий-sha>` + `up -d`. Тег предыдущего успешного билда — в [`CICD.md`](CICD.md) (артефакт `planeai-deploy-staging`).
3. **Откат миграции** — `manage.py migrate ai <предыдущий_номер>`. ВНИМАНИЕ: убедиться, что назад совместимо (мы редко отказываемся от полей). Если миграция дропает таблицу — restore из бэкапа быстрее.

### Регулярные операции

| Что | Кто | Когда |
|---|---|---|
| Проверить дашборд расхода | Илья (PM) | Понедельник |
| Триаж алертов | дежурный on-call | Постоянно (Slack/TG) |
| Restore-тест зелёный | Никита (QA) | По понедельникам, читать лог |
| Бэкап-цифры (размер дампа, время выполнения) | Костя/Вова | Раз в месяц |
| Bump pgvector / Plane base image | Костя/Вова | По релизам upstream + CVE-feed |
| Аудит `record_usage(feature=...)` для новых фич | автор фичи + ревьюер | При мерже PR |

---

## 13. Индекс документов

### По спринтам

| Спринт | Документы | TZ |
|---|---|---|
| **0 — Подготовка** | [README-deploy.md](README-deploy.md), [SCHEMA.md](SCHEMA.md), [STREAMING.md](STREAMING.md), [PGVECTOR.md](PGVECTOR.md), [IMAGE.md](IMAGE.md), [ACL.md](ACL.md), [GDPR.md](GDPR.md), [CICD.md](CICD.md), [STAGING.md](STAGING.md), [SECRETS.md](SECRETS.md), [ONBOARDING.md](ONBOARDING.md) | 0.1–0.12 |
| **1 — Ингест + индекс** | (модели, миграции, ингест-хуки, бэкафилл, бюджет, index-status — в коде) | 1.1–1.9 |
| **2 — Поиск + RAG** | [STREAMING.md](STREAMING.md), [SPRINT-2-ACCEPTANCE.md](SPRINT-2-ACCEPTANCE.md) | 2.1–2.9 |
| **5 — Агенты** | [SPRINT-5-ACCEPTANCE.md](SPRINT-5-ACCEPTANCE.md) | 5.1–5.8 |
| **6 — Прод и выпуск** | [BACKUP.md](BACKUP.md), [MONITORING.md](MONITORING.md), [BUDGET.md](BUDGET.md), **этот RUNBOOK.md** | 6.1–6.8 |

### По вопросу

| Вопрос | Документ |
|---|---|
| Как развернуть Plane CE? | [README-deploy.md](README-deploy.md) |
| Какие модели у Plane (Issue/Comment/Page)? | [SCHEMA.md](SCHEMA.md) |
| Почему ASGI, а не WSGI? | [STREAMING.md](STREAMING.md) |
| Почему `pgvector/pgvector:0.8.2-pg15`? | [PGVECTOR.md](PGVECTOR.md) |
| Как пересобрать образ с `ai`? | [IMAGE.md](IMAGE.md) |
| Что есть админ/мембер/гость в Plane? | [ACL.md](ACL.md) |
| GDPR / DPA / приватные проекты? | [GDPR.md](GDPR.md) |
| Что в CI? | [CICD.md](CICD.md) |
| Что в staging? | [STAGING.md](STAGING.md) |
| Какие секреты и как их крутить? | [SECRETS.md](SECRETS.md) |
| Как новый человек начинает? | [ONBOARDING.md](ONBOARDING.md) |
| Как делается бэкап? | [BACKUP.md](BACKUP.md) |
| Что мониторим? Что за алерты? | [MONITORING.md](MONITORING.md) |
| Кто сколько потратил на ИИ? | [BUDGET.md](BUDGET.md) |
| Что делать, когда что-то сломалось? | **этот RUNBOOK.md** |
| Принимочные сценарии поиска | [SPRINT-2-ACCEPTANCE.md](SPRINT-2-ACCEPTANCE.md) |
| Принимочные сценарии агента | [SPRINT-5-ACCEPTANCE.md](SPRINT-5-ACCEPTANCE.md) |
| Контекст и правила для Claude Code | [CLAUDE.md](CLAUDE.md) |

### Полезные пути в коде

| Что | Файл |
|---|---|
| Чанкинг + эмбеддинг | [ai/chunking.py](ai/chunking.py), [ai/providers.py](ai/providers.py), [ai/tasks.py](ai/tasks.py) |
| Ингест-сигналы | [ai/signals.py](ai/signals.py) |
| RAG / поиск | [ai/search.py](ai/search.py), [ai/streaming.py](ai/streaming.py) |
| ACL | [ai/acl.py](ai/acl.py) |
| Учёт токенов + бюджет | [ai/usage.py](ai/usage.py), [ai/guards.py](ai/guards.py) |
| Агент (worker) | [ai/agent_worker.py](ai/agent_worker.py), сценарии: [ai/triage.py](ai/triage.py), [ai/dedupe.py](ai/dedupe.py), [ai/describe.py](ai/describe.py) |
| Транспаренси-фид агента | [ai/agent_views.py](ai/agent_views.py) |
| Мониторинг + health + алерты | [ai/metrics.py](ai/metrics.py), [ai/health.py](ai/health.py), [ai/alerting.py](ai/alerting.py) |
| Дашборд расхода | [ai/usage_views.py](ai/usage_views.py), [apps/web/core/components/ai/usage-dashboard.tsx](apps/web/core/components/ai/usage-dashboard.tsx) |
| Management-команды | [ai/management/commands/backfill_embeddings.py](ai/management/commands/backfill_embeddings.py), [ai/management/commands/create_ai_agent.py](ai/management/commands/create_ai_agent.py) |
| Smoke-скрипты | [scripts/verify_*.py](scripts/) |
| Бэкап-скрипты + сайдкар | [scripts/backup/](scripts/backup/), [deploy-local/docker-compose.backup.yml](deploy-local/docker-compose.backup.yml) |
| Compose-оверлеи | [deploy-local/docker-compose.{yml,override,staging,monitoring,backup}.yml](deploy-local/) |

---

## DoD ТЗ 6.4 — статус

- [x] Линейная последовательность от пустого сервера до первого поиска (главы 2–8)
- [x] Troubleshooting-таблица с 17+ известными граблями (глава 11)
- [x] Раздел эксплуатации: бюджет, алерты, бэкап, откат (глава 12)
- [x] Индекс ссылок на все спринтовые .md (глава 13)
- [ ] **Проверка воспроизводимости** — провести с новым человеком (Никитой / новым нанятым) на чистом VPS и зафиксировать места, где он застрял. Дополнить runbook. **Это не однократно — повторять при каждой смене состава команды.**

При обнаружении пробелов — этот файл не священен, любой PR на `RUNBOOK.md` приветствуется. Главное — фиксировать **что именно** было непонятно, чтобы следующий человек прошёл без помощи.
