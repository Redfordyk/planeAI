# MONITORING — метрики и алерты ИИ-слоя (ТЗ 6.2)

> Этот документ — runbook мониторинга для **Вовы (FullStack)** на стадии настройки и **Кости/Никиты** при on-call. ИИ-слой ломается иначе, чем обычное Django-приложение — здесь описаны риски, которые невидимы в общем мониторинге Plane.

## Архитектура

```
   plane-api ──► /metrics  ──► Prometheus ──► Alertmanager ──► /alerts/webhook/ ──► chat
                /health  ◄── liveness probe                      (Slack/Telegram/Discord)
```

Все три pieces — отдельные контейнеры в [`docker-compose.monitoring.yml`](deploy-local/docker-compose.monitoring.yml). API-эндпоинты `/metrics` и `/health` отдают Plane API (новые view в [`ai/metrics.py`](ai/metrics.py) и [`ai/health.py`](ai/health.py)). Алерт-вебхук от Alertmanager обрабатывает [`ai/alerting.py`](ai/alerting.py) и переадресует в команду через `ALERT_WEBHOOK_URL`.

## Что мониторим (DoD ТЗ 6.2)

| Метрика | Алерт | Порог | Severity | DoD-пункт |
|---|---|---|---|---|
| Расход токенов воркспейса | `PlaneAIBudgetWarning` | ratio > 0.80, sustained 5 мин | warning | бюджет 80% |
| Расход токенов воркспейса (критический) | `PlaneAIBudgetCritical` | ratio > 0.95, sustained 5 мин | critical | (бонус) |
| Длина очереди Celery | `PlaneAIBackfillStuck` | queue > 1000 И растёт 15 мин | warning | бэкафилл застрял |
| Coverage индекса | `PlaneAIIndexDrift` | coverage < 0.85, sustained 30 мин | warning | дрейф индекса |
| 429/5xx от Anthropic/OpenAI | `PlaneAIProviderErrors` | rate > 5/мин, sustained 10 мин | warning | ошибки провайдера |
| Аномальная активность агента | `PlaneAIAgentLoop` | applied > 30 за 5 мин на воркспейс, 2 мин | **critical** | петля агента |
| Health-эндпоинт ИИ-слоя | `PlaneAIMetricsDown` | `up == 0`, 5 мин | critical | health-эндпоинт |

**Самый важный алерт — `PlaneAIBudgetCritical`**: это финансовый предохранитель. Бюджет-guard (1.7) останавливает запросы на проде, но *уже когда* лимит достигнут — алерт даёт окно ~5–20% бюджета для реакции.

## Метрики — каталог

### Counter `planeai_provider_errors_total{provider, kind}`

Источник: `_bump_provider_error()` в [`ai/providers.py`](ai/providers.py), вызывается из retry-loop в `ClaudeChat.complete()` и `OpenAIEmbed.embed()`.

- `provider`: `anthropic` | `openai`
- `kind`: `rate_limit` (429) | `api_error` (transient 5xx / network)

Resetable: counter живёт в памяти процесса. Prometheus сам распознаёт reset (`rate()`/`increase()`).

### Counter `planeai_agent_actions_total{workspace_id, tool_name, status}`

Источник: `log_agent_action()` в [`ai/agent_worker.py`](ai/agent_worker.py), вызывается из `apply_agent_action()` для каждого tool_use.

- `tool_name`: `set_priority` / `set_labels` / `suggest_assignee` / `add_comment` / `update_description` / `find_work_items`
- `status`: `applied` / `rejected` / `error`

Резкий рост `rejected` — сигнал, что модель пробивается за границы (полезно вместе с `PlaneAIAgentLoop`).

### Gauge `planeai_workspace_tokens{workspace_id, metric}`

Три значения per workspace:

- `metric=used` — сумма `input_tokens + output_tokens + cache_creation_tokens` за текущий месяц (см. [`ai/usage.py:tokens_used_this_month`](ai/usage.py))
- `metric=budget` — `WorkspaceAIConfig.monthly_token_budget`
- `metric=ratio` — `used / budget`, для удобства алертов

Считается на каждом scrape — SQL-aggregate по `ai_usage_log`. Дорого? Нет: индекс `ai_usage_ws_time_idx` покрывает фильтр.

### Gauge `planeai_index_coverage{workspace_id, source_type}`

Те же числа, что эндпоинт `/api/ai/workspaces/<id>/index-status/` (ТЗ 1.8): `distinct(source_id) в DocumentChunk` / `total(source) в Plane`. Считается на каждом scrape, не кэшируется — отображает текущее состояние.

### Gauge `planeai_agent_actions_5m{workspace_id}`

Количество **applied** действий агента за последние 5 минут per workspace. Считается на scrape (`AIAgentActionLog` за окно). Это reset-friendly альтернатива `rate(planeai_agent_actions_total{status="applied"}[5m])` — мы используем её потому что она работает сразу после рестарта API процесса (counter ещё пустой).

### Gauge `planeai_celery_queue_length{queue}` (+ optional `error=`)

Best-effort пробинг брокера. Если Redis — `LLEN <queue>`. Если RabbitMQ — `queue_declare(passive=True).message_count`. Если broker недоступен — отдаёт один error-маркер вместо падения всего scrape.

## Конфигурация — env

В `.env` на проде:

```env
# Метрики — общий секрет с Prometheus.
PLANEAI_METRICS_TOKEN=<long-random>

# Алерт-webhook — общий секрет между Alertmanager и Django.
PLANEAI_ALERT_WEBHOOK_TOKEN=<long-random>
ALERT_WEBHOOK_TOKEN=${PLANEAI_ALERT_WEBHOOK_TOKEN}   # одно и то же
PLANEAI_ALERTMANAGER_TARGET_URL=http://api:8000/api/ai/alerts/webhook/

# Канал команды.
ALERT_WEBHOOK_URL=https://hooks.slack.com/services/...   # или Telegram, или Discord
ALERT_WEBHOOK_FORMAT=slack                               # slack | telegram | discord | raw

# Grafana админ.
GF_SECURITY_ADMIN_PASSWORD=<from secrets>
GRAFANA_PORT=3000

# Celery queues to scrape (defaults to "celery"); пробел-разделённые.
PLANEAI_CELERY_QUEUES=celery ai_agent ai_reindex
```

Каналы — формат:

| Канал | URL | `ALERT_WEBHOOK_FORMAT` |
|---|---|---|
| Slack incoming-webhook | `https://hooks.slack.com/services/T.../B.../...` | `slack` |
| Discord webhook | `https://discord.com/api/webhooks/.../...` | `discord` |
| Telegram bot | `https://api.telegram.org/bot<TOKEN>/sendMessage?chat_id=<CHAT>` | `telegram` |
| что-то ещё | свой endpoint, принимающий `{"title":..., "body":...}` | `raw` |

## Запуск

```bash
cd deploy-local
docker compose \
  -f docker-compose.yml \
  -f docker-compose.staging.yml \
  -f docker-compose.monitoring.yml \
  up -d prometheus alertmanager grafana
```

Проверка:

```bash
# scrape работает
curl -H "X-Metrics-Token: $PLANEAI_METRICS_TOKEN" http://localhost:8000/api/ai/metrics/

# health
curl http://localhost:8000/api/ai/health/ | jq .

# Prometheus видит таргет
curl http://localhost:9090/api/v1/targets | jq '.data.activeTargets[]|{job,health}'

# Alertmanager здоров
curl http://localhost:9093/-/healthy

# Grafana dashboard: http://<host>:3000, login admin / $GF_SECURITY_ADMIN_PASSWORD
# Уже provision-нут dashboard "planeAI — overview".
```

## Реакция на алерты — runbook

### `PlaneAIBudgetWarning` / `PlaneAIBudgetCritical`

Workspace выбрал большую часть месячного бюджета. Возможные причины + действия:

1. **Массовый bulk-reindex** (например, после `backfill_embeddings` на старом воркспейсе).
   - Действие: посмотреть `rate(planeai_workspace_tokens{metric="used"}[1h])` в Grafana. Если burst закончился — проигнорировать, бюджет восстановится 1-го числа.
2. **Петля агента**.
   - Действие: проверить `planeai_agent_actions_5m`. Если > 10 — вероятно петля, см. `PlaneAIAgentLoop`.
3. **Реальный rost использования.**
   - Действие: через админку Django увеличить `monthly_token_budget` для воркспейса или включить ТЗ 1.7 hard-cap.

### `PlaneAIBackfillStuck`

Очередь Celery растёт без обработки. Причины:

1. **Worker умер**: `docker ps` → `plane-worker` отсутствует / в `Restarting`. Действие: `docker logs plane-worker --tail 200`.
2. **Worker занят медленной задачей**: смотреть процессы воркера, может быть зависший OpenAI batch.
3. **DB lock**: один воркер ждёт другой. `SELECT * FROM pg_stat_activity WHERE state='waiting'`.

### `PlaneAIIndexDrift`

Coverage < 85% — новые задачи не индексируются. Причины:

1. **Worker мёртв** (как в `BackfillStuck`).
2. **Signal-handler отвалился**: проверить, что `ai.signals.connect()` отрабатывает (см. `ai/apps.py.ready`).
3. **OpenAI rate-limit**: коррелировать с `PlaneAIProviderErrors`.

Если расходимость — единичный воркспейс и небольшая (≥80%), можно подождать; самостоятельно догонит. Если падает дальше — запустить вручную: `python manage.py backfill_embeddings --workspace <id>`.

### `PlaneAIProviderErrors`

Anthropic/OpenAI 429/5xx чаще 5/мин. Причины:

1. **Их инцидент** → проверить `status.anthropic.com` / `status.openai.com`.
2. **Наш RPS превышает квоту** → запросить повышение или поднять `MAX_RETRIES` в [`ai/providers.py`](ai/providers.py).
3. **Кривые ключи** → 401 не попадёт под `rate_limit` / `api_error`, но `AuthenticationError` от SDK тоже считается `api_error` в нашей метрике — проверить `WorkspaceAIConfig.anthropic_key`.

### `PlaneAIAgentLoop` — **критический**

Агент применяет >30 действий за 5 минут на одном воркспейсе. Это очень близко к petле самозапуска (ТЗ 5.7 `test_agent_no_self_trigger`). **Действие сейчас:**

1. **Отключить агента в воркспейсе** через UI (TZ 5.6 → `AgentPage` → toggle Off) или через Django shell:
   ```bash
   docker compose exec api python manage.py shell -c "
   from ai.models import AIAgent
   AIAgent.objects.filter(workspace_id='<id>').update(enabled=False)
   "
   ```
2. Посмотреть последние строки `ai_agent_action_log` — что именно агент делал на одной задаче.
3. Проверить `is_agent_acting()` ключи в Redis (`KEYS ai:agent_acting:*`) — если их много или они «вечные», `agent_acting`-обёртка где-то протекла.

Включать обратно только после фикса.

### `PlaneAIMetricsDown`

Prometheus не может скрейпить наш `/metrics`. Причины:

1. API-процесс упал → `docker logs api --tail 200`.
2. `PLANEAI_METRICS_TOKEN` поменялся в env, но Prometheus читает старый из `credentials_file` → `docker compose restart prometheus`.

## Health-эндпоинт

`GET /api/ai/health/` — публичный (без токена), быстрый, агрегирует:

- `database` — SELECT 1
- `vector_ext` — наличие расширения pgvector
- `broker` — ping Redis / RabbitMQ
- `index_freshness` — coverage худшего воркспейса
- `budget` — наибольший ratio среди воркспейсов

HTTP-код:
- `200 ok` — всё ок
- `200 degraded` — есть некритичные проблемы (нужно реагировать, но LB не пуллит инстанс)
- `503 down` — критичная проблема (`database` или `vector_ext` упали)

LB-конфиг: использовать как readiness probe с порогом 503.

## Как добавить новую метрику

1. Добавить в [`ai/metrics.py`](ai/metrics.py):
   - Counter — через `_Counter(...)` на module level + bump из кода фичи.
   - Gauge — функцию `_xxx_samples()` + строку в `MetricsView.get()` (`_gauge_lines(...)`).
2. (Опционально) добавить алерт в [`alerts.yml`](deploy-local/monitoring/alerts.yml).
3. (Опционально) добавить панель в [`planeai-overview.json`](deploy-local/monitoring/grafana/dashboards/planeai-overview.json).
4. Обновить «Что мониторим» в этом документе.

## Связи

- Закрывает DoD [ТЗ 6.2](tz/sprint-6/02-задача-6.2-мониторинг.md).
- Использует учёт токенов из [ТЗ 1.7](apps/api/plane/) и `index-status` из [ТЗ 1.8](ai/views.py).
- Бюджет-алерт дополняет (НЕ заменяет) hard-cap в [`require_ai_budget`](ai/guards.py).
- При апгрейде до полноценной observability (логи + traces) — связано с будущей ТЗ 6.3.
