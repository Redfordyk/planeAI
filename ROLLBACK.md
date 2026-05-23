# ROLLBACK — план отката planeAI (ТЗ 6.5)

> **Кому это.** On-call дежурный, когда «ИИ что-то сделал плохо в проде» или «после релиза горят алерты». Цель — за **минуты** вернуть стабильное состояние, **не теряя** данные Plane (issue/comment/page) и, по возможности, не теряя `WorkspaceAIConfig` / историю расхода.
>
> **Главный принцип архитектуры.** Слой `ai` отделён от Plane: при проблеме именно с ИИ-функционалом первым шагом всегда — **kill switch уровня L1** ниже. Plane остаётся жив, ИИ-эндпоинты возвращают 403, ингест-сигналы и триггеры агента — no-op'ы. Это секунды и нулевые потери. Откатывать образ или миграции — только если L1 не помог.

---

## Дерево решений

```
┌──────────────────────────────────────────────────────────────────┐
│  Релиз сломал прод? Алерт горит? Юзеры жалуются?                 │
└──────────────┬───────────────────────────────────────────────────┘
               ▼
       Симптом — только в ИИ-фичах?
       (поиск, агент, дашборд расхода, бэкафилл)
        │                            │
        ▼ ДА                         ▼ НЕТ (Plane сам сломан)
   ┌────────────────────────┐   ┌─────────────────────────────────┐
   │ L1: KILL SWITCH        │   │ Это не ИИ-инцидент. Откатывать │
   │ (секунды, 0 потерь)    │   │ образ Plane upstream — НЕ      │
   │                        │   │ покрывается этим документом.   │
   │ disable_ai --workspace │   │ См. README-deploy.md / Plane   │
   │   <id>                 │   │ release notes.                 │
   └──────────┬─────────────┘   └─────────────────────────────────┘
              ▼
       Помогло? Алерт перестал гореть?
        │                            │
        ▼ ДА → STOP, разбираться     ▼ НЕТ → проблема глубже
   ┌─────────────────────────────────────────────┐
   │ L2: ОТКАТ ОБРАЗА plane-backend-ai           │
   │ на предыдущий SHA-тег (минуты)              │
   │                                             │
   │ docker compose pull plane-backend-ai:<sha>  │
   │ docker compose up -d api worker beat-worker │
   │   migrator                                  │
   └──────────┬──────────────────────────────────┘
              ▼
       Релиз содержал ai-миграции?
        │                            │
        ▼ ДА                         ▼ НЕТ → STOP
   ┌─────────────────────────────────────────────┐
   │ L3: ОТКАТ МИГРАЦИЙ ai                       │
   │ ⚠️ ДЕСТРУКТИВНО — reverse уносит данные     │
   │ новых полей/таблиц. Делать только если     │
   │ backward-compatible НЕ выйдет.              │
   │                                             │
   │ migrate ai <предыдущий-номер>               │
   └──────────┬──────────────────────────────────┘
              ▼
       Данные повреждены / база несогласована?
        │                            │
        ▼ ДА                         ▼ НЕТ → STOP
   ┌─────────────────────────────────────────────┐
   │ L4: RESTORE ИЗ БЭКАПА                       │
   │ ⚠️ Теряем всё после точки бэкапа           │
   │ (RPO = 24 ч).                               │
   │                                             │
   │ См. BACKUP.md → restore_postgres.sh         │
   └─────────────────────────────────────────────┘
```

Принцип «не пропускать уровень». Каждый следующий уровень дороже и опаснее предыдущего. L4 — последняя черта.

---

## L1: Kill switch

**Когда использовать.** Любая жалоба «поиск зависает», «агент закомментировал не то», «расход внезапно скакнул», «алерт `PlaneAIAgentLoop` / `PlaneAIBudgetCritical`». Симптом локализован в ИИ-слое.

**Что делает.** Флипает `WorkspaceAIConfig.enabled=False`. Все ИИ-эндпоинты возвращают 403 ([`require_ai_budget`](ai/guards.py), [`SearchView`](ai/views.py)). Ингест-сигналы — no-op ([`ai/signals.py:_ai_enabled`](ai/signals.py)). Триггеры агента — no-op (inline filter в [`ai/agent_triggers.py`](ai/agent_triggers.py)). Plane полностью функционален как обычный трекер.

**Что не отключает.** Read-only audit-эндпоинты остаются доступными — они нужны для диагностики:
- `GET /api/ai/workspaces/<id>/index-status/` — стат покрытия.
- `GET /api/ai/workspaces/<id>/usage/stats/` — дашборд расхода ([BUDGET.md](BUDGET.md)). Админ должен видеть, что было потрачено перед kill switch.
- `GET /api/ai/workspaces/<id>/agent/actions/` — лента действий агента (отменять `set_labels` через undo всё ещё можно — это «уборка», не «новое ИИ-действие»).
- `GET /api/ai/health/`, `GET /api/ai/metrics/` — мониторинг.

**Команды.**

```bash
# Один воркспейс по UUID
docker compose -p plane-ce exec api python manage.py disable_ai \
  --workspace <workspace-id>

# Один воркспейс по slug (на случай если UUID под рукой нет)
docker compose -p plane-ce exec api python manage.py disable_ai \
  --workspace <slug>

# Глобальный инцидент — все воркспейсы. --confirm обязателен.
docker compose -p plane-ce exec api python manage.py disable_ai \
  --all-workspaces --confirm

# Re-enable после фикса
docker compose -p plane-ce exec api python manage.py enable_ai \
  --workspace <id-или-slug>
```

Команды идемпотентны: повторный запуск выводит `already_disabled=1`, ничего не меняет. См. [`ai/management/commands/disable_ai.py`](ai/management/commands/disable_ai.py).

**Альтернатива через UI.** Toggle агента на странице `/<slug>/settings/ai-agent` — `AIAgent.enabled` ([TZ 5.6](apps/web/core/components/ai/agent-toggle.tsx)). Он отключает **только агента**, не весь ИИ-слой. Для агентских инцидентов (`PlaneAIAgentLoop`) — этого достаточно и быстрее: один клик в браузере.

**Аудит.** Команда логирует `WARNING plane.ai.rollback: disabled AI for ...` в stdout API-контейнера. `WorkspaceAIConfig.updated_at` обновляется. Если нужна полная история — `git log` коммитов оператора в [`docker compose exec`](RUNBOOK.md) (на проде on-call действия попадают в shell history через `docker exec`).

---

## L2: Откат образа `plane-backend-ai`

**Когда использовать.** L1 не помог: проблема — в самом коде нового образа, не в данных. Например, новый релиз поломал сериализатор и `IndexStatusView` отдаёт 500 даже после kill switch (бывает редко, но возможно при изменении общего кода).

**Префикс.** Образы тегируются по SHA коммита в CI ([CICD.md](CICD.md)): `ghcr.io/<org>/plane-backend-ai:<short-sha>`. Тег предыдущего успешного билда лежит в:
- артефакте workflow `planeai-deploy-staging` (последний "успешный" run),
- environment variable `PLANE_AI_IMAGE` в `.env` на staging/prod — она хранит **текущий** SHA. Перед каждым deploy CI делает backup этой переменной как `PLANE_AI_IMAGE_PREVIOUS`.

```bash
# 1. На сервере: посмотреть, какой образ был ДО релиза.
grep "PLANE_AI_IMAGE" deploy-local/.env
# →  PLANE_AI_IMAGE=ghcr.io/.../plane-backend-ai:abcd1234           (сломанный)
# →  PLANE_AI_IMAGE_PREVIOUS=ghcr.io/.../plane-backend-ai:9876fedc  (рабочий)

# 2. Откатить переменную и пересоздать сервисы.
sed -i 's|^PLANE_AI_IMAGE=.*|PLANE_AI_IMAGE=ghcr.io/.../plane-backend-ai:9876fedc|' \
  deploy-local/.env

docker compose -p plane-ce \
  -f docker-compose.yml \
  -f docker-compose.staging.yml \
  pull api worker beat-worker migrator

docker compose -p plane-ce \
  -f docker-compose.yml \
  -f docker-compose.staging.yml \
  up -d --force-recreate api worker beat-worker migrator

# 3. Дождаться migrator → Exited 0
docker inspect -f '{{.State.Status}} {{.State.ExitCode}}' plane-ce-migrator-1

# 4. Smoke — health
curl -sS http://localhost/api/ai/health/ | python -m json.tool
```

**Тонкость с миграциями.** Если новый образ содержал миграцию `ai/0006_*`, а откат идёт на образ без неё — после `docker compose up -d` migrator увидит миграцию `0006_*` в БД, но не в коде, и **не упадёт** (Django считает её как "applied externally"). Эта неконсистентность безвредна для чтения, но новое поле, добавленное миграцией 0006, может теперь стоять в БД без кода, который его использует. Если поле NOT NULL — INSERT'ы упадут. См. L3.

**Возврат к новому образу.** После фикса:
```bash
# Бамп .env обратно
sed -i 's|^PLANE_AI_IMAGE=.*|PLANE_AI_IMAGE=ghcr.io/.../plane-backend-ai:<новый-sha>|' \
  deploy-local/.env
docker compose ... pull && docker compose ... up -d --force-recreate api worker beat-worker migrator
```

---

## L3: Откат миграций `ai`

**Когда использовать.** L2 откатил образ, но БД содержит миграции, которых в откатном образе нет, и это вызывает несогласованность (NOT NULL колонка без кода, удалённая модель и т.п.).

**⚠️ Деструктивно.** Reverse миграций уносит данные новых таблиц/колонок. Например, reverse `0004_aiagentactionlog` сделает `DROP TABLE ai_agent_action_log` — **вся история действий агента теряется навсегда**. Reverse `0001_enable_pgvector` сделает `DROP EXTENSION vector CASCADE` — **все эмбеддинги в `ai_document_chunk` теряются** (придётся бэкафиллить заново; это ~часы и десятки $).

**Перед reverse — обязательно сделать `pg_dump`** ровно тех таблиц, которые reverse удалит. Если выкатка пойдёт не так — restore из этого дампа.

```bash
# 1. Посмотреть текущее состояние миграций
docker compose -p plane-ce exec api python manage.py showmigrations ai
# → ai
#    [X] 0001_enable_pgvector
#    [X] 0002_initial
#    [X] 0003_aiagent
#    [X] 0004_aiagentactionlog
#    [X] 0005_aiagentactionlog_undo
#    [X] 0006_<новая_сломанная_миграция>     ← хотим откатить

# 2. Pre-rollback дамп ВСЕХ таблиц ai_*
docker compose -p plane-ce exec -e PGPASSWORD=$POSTGRES_PASSWORD plane-db \
  pg_dump -U plane -d plane \
  --table='ai_*' --data-only --column-inserts \
  > /tmp/pre-rollback-ai-tables-$(date +%Y%m%d-%H%M).sql

# 3. Откат до предыдущей миграции
docker compose -p plane-ce exec api python manage.py migrate ai 0005_aiagentactionlog_undo

# 4. Подтверждение
docker compose -p plane-ce exec api python manage.py showmigrations ai
# → 0006 теперь без [X]
```

**Reversibility — гарантия.** Все наши миграции — стандартные `CreateModel` / `AddField` / `AddIndex` / `CreateExtension`. Django reverse'ит их автоматически. Регрессионный тест [`ai/tests/test_rollback.py::test_all_ai_migrations_have_reverse`](ai/tests/test_rollback.py) проверяет, что новых миграций без `reverse_code` не появилось — он падает в CI, если кто-то добавил `RunPython(..., reverse_code=migrations.RunPython.noop)` без объяснения.

**Когда reverse — НЕ опция.** Если миграция изменила формат данных в существующих таблицах (например, переколонка с потерей данных) — reverse её не вернёт. В этом случае единственный путь — L4: restore из бэкапа.

---

## L4: Restore из бэкапа

**Когда использовать.** L3 невозможен или потерял данные. Состояние БД фундаментально несогласовано. Готовы потерять всё, что было после последнего бэкапа (RPO = 24 часа, см. [BACKUP.md](BACKUP.md)).

**⚠️ Самый дорогой шаг.** Останавливает сервис на время restore (RTO ≤ 4 ч). Все ИИ-данные **и** все Plane-данные (issues, comments, pages) откатываются на 24 часа назад.

**Команды** — см. [BACKUP.md → Restore](BACKUP.md). Кратко:

```bash
# 1. Остановить запись
docker compose -p plane-ce stop api worker beat-worker

# 2. Restore Postgres (полный дамп с ai_* и Plane-таблицами)
docker compose -p plane-ce exec planeai-backup /usr/local/bin/restore_postgres.sh \
  --dump /backups/postgres/latest.dump

# 3. Restore MinIO (если повреждены загрузки)
docker compose -p plane-ce exec planeai-backup /usr/local/bin/restore_minio.sh

# 4. Поднять сервисы
docker compose -p plane-ce up -d api worker beat-worker

# 5. Smoke
curl -sS http://localhost/api/ai/health/
```

**Restored `FIELD_ENCRYPTION_KEY`** — критично. Если ключ потерян, восстановленные `WorkspaceAIConfig` нечитаемы. Ключ хранится **отдельно** от pgdump (1Password / GPG), см. [SECRETS.md](SECRETS.md).

---

## Staging — полный цикл отката (drill)

Один раз перед каждым релизом — на staging:

```bash
# 1. На staging: текущее состояние работает. Зафиксировать.
curl -sS http://staging.plane.../api/ai/health/ | grep '"status": "ok"'

# 2. «Сломать» через L1 (имитация инцидента).
ssh deploy@staging "docker compose -p plane-ce exec api python manage.py disable_ai --workspace <staging-ws-id>"

# 3. Проверить, что search 403, Plane жив, дашборд расхода жив.
curl -X POST http://staging.plane.../api/ai/workspaces/<id>/search/ -H "Cookie: ..." -d '{"query":"x"}'
# → 403, {"error": "AI disabled for this workspace"}

curl http://staging.plane.../
# → 200 (Plane UI)

curl http://staging.plane.../api/ai/workspaces/<id>/usage/stats/ -H "Cookie: ..."
# → 200, payload с budget.exceeded=true (потому что enabled=False)

# 4. Восстановить через enable_ai.
ssh deploy@staging "docker compose -p plane-ce exec api python manage.py enable_ai --workspace <staging-ws-id>"

# 5. Smoke search снова работает.
curl -X POST http://staging.plane.../api/ai/workspaces/<id>/search/ -H "Cookie: ..." -d '{"query":"x"}'
# → 200, SSE stream

# 6. Откат образа (если CI/CD выпустил предыдущий релиз) — повторить L2 на staging.
ssh deploy@staging "
  sed -i 's|^PLANE_AI_IMAGE=.*|PLANE_AI_IMAGE=ghcr.io/.../plane-backend-ai:<prev-sha>|' .env
  docker compose ... up -d --force-recreate api worker beat-worker migrator
"
# Через 2-3 минуты — снова рабочее состояние.
```

**Зелёный drill = можно катить релиз на прод.** Красный drill = найти причину **до** prod-релиза.

DoD ТЗ 6.5 требует staging-цикл прогонять не реже раза в спринт (как минимум перед TZ 6.7). Запись результата — в [SPRINT-5-ACCEPTANCE.md](SPRINT-5-ACCEPTANCE.md) / следующих acceptance-документах.

---

## Связи

- L1 kill switch уровне Workspace — [`ai/guards.py`](ai/guards.py), [`ai/signals.py`](ai/signals.py), [`ai/agent_triggers.py`](ai/agent_triggers.py). Команды — [`ai/management/commands/disable_ai.py`](ai/management/commands/disable_ai.py), [`enable_ai.py`](ai/management/commands/enable_ai.py).
- L2 image rollback — теги в [CICD.md](CICD.md), процедура — [IMAGE.md](IMAGE.md).
- L3 migration reverse — миграции в [`ai/migrations/`](ai/migrations/), регрессия — [`ai/tests/test_rollback.py`](ai/tests/test_rollback.py).
- L4 data restore — [BACKUP.md](BACKUP.md) (ТЗ 6.1).
- Главный operator runbook — [RUNBOOK.md](RUNBOOK.md) (ТЗ 6.4).
- Алерты и реакция — [MONITORING.md](MONITORING.md) (ТЗ 6.2).
- При сомнении «ИИ это вообще или Plane?» — [RUNBOOK.md → Troubleshooting](RUNBOOK.md#11-troubleshooting).

## DoD ТЗ 6.5 — статус

- [x] Kill switch (L1): `disable_ai` / `enable_ai` команды + behavioural regression в тестах.
- [x] Procedure L2 (rollback образа) задокументирована, опирается на `PLANE_AI_IMAGE_PREVIOUS` из CI.
- [x] L3: миграции `ai/` все стандартные, обратимы; CI-тест `test_all_ai_migrations_have_reverse` гарантирует это и для будущих миграций.
- [x] L4: restore — ссылка на [BACKUP.md](BACKUP.md), включая шифровальный ключ из `SECRETS.md`.
- [x] Staging-drill процедура (см. выше) — прогон записывается в acceptance перед каждым релизом.
- [x] ROLLBACK.md (этот файл) — дерево решений + 4 уровня + связи.
