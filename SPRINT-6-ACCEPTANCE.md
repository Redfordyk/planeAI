# Sprint 6 — Production acceptance (ТЗ 6.6)

> Финальный приёмочный прогон **на проде** перед открытием доступа команде. Совместный gate **Ильи (PM)** и **Никиты (QA)**. Это первый запуск на РЕАЛЬНЫХ данных — порядок шагов нестрогая рекомендация, **закон**.
>
> ⚠️ **СТРОГАЯ ПОСЛЕДОВАТЕЛЬНОСТЬ:** GDPR → бэкап → бэкафилл → smoke. Любое отклонение от порядка может уронить данные клиента в облако без правового основания, или потерять данные при первой же ошибке бэкафилла.

---

## 0. Кто и когда

- **Время** — выделенное окно 2-3 часа, минимально влияющее на работу команды. Желательно вечер пятницы или раннее утро. Бэкафилл реальных данных может занять до 1.5 часов на воркспейс среднего размера.
- **Состав:** Илья (PM, модерирует, фиксирует решения), Никита (QA, ведёт smoke-проверки), Костя/Вова (FullStack, дежурят на случай быстрой правки).
- **Слаженность:** Slack-канал `#planeai-acceptance` открыт на всё время; в нём фиксируется каждый шаг с таймстампом и результатом. После завершения — выгрузить транскрипт в [`docs/acceptance-2026-XX-XX.md`](docs/).

---

## 1. Pre-flight: GDPR + бэкап

> **Не запускать бэкафилл, пока этот раздел не зелёный.** Бэкафилл реальных задач уходит в облако OpenAI; без DPA это нарушение законов GDPR/152-ФЗ.

### 1.1 DPA закрыт (ТЗ 0.7)

- [ ] **DPA подписан с Anthropic** (zero-retention enabled, дата подписи в [GDPR.md](GDPR.md)).
- [ ] **DPA подписан с OpenAI** (zero-retention enabled, дата подписи в [GDPR.md](GDPR.md)).
- [ ] Установить env var на проде: `PLANEAI_DPA_CLOSED=YYYY-MM-DD` (дата подписи второго DPA — позднейшая из двух).

```bash
# На прод-хосте
echo "PLANEAI_DPA_CLOSED=2026-07-07" >> deploy-local/.env
docker compose -p plane-ce up -d api worker beat-worker
```

### 1.2 Приватные проекты помечены (ТЗ 3.4)

- [ ] Илья прошёл по списку проектов с командой, **зафиксирован список** конфиденциальных проектов (HR-обсуждения, юридические переговоры, финансы).
- [ ] Для каждого такого проекта в админке Plane выставлен флаг `AIProjectSettings.exclude_from_ai=True`:
  ```bash
  docker compose -p plane-ce exec api python manage.py shell <<'PY'
  from ai.models import AIProjectSettings
  from plane.db.models import Project
  for identifier in ("HR-PRIVATE", "LEGAL", "FINANCE"):  # ← подставить реальные
      prj = Project.objects.filter(identifier=identifier).first()
      if prj:
          AIProjectSettings.objects.update_or_create(
              project=prj, defaults={"exclude_from_ai": True},
          )
          print("excluded", prj.identifier)
  PY
  ```
- [ ] **Если приватных проектов нет** — установить signoff env var вместо флага: `PLANEAI_NO_PRIVATE_PROJECTS=<имя-PM>:<дата>`. Это явное «мы посмотрели, их нет» вместо тишины.

### 1.3 Свежий бэкап (ТЗ 6.1)

- [ ] Backup-сайдкар поднят и крутится по cron:
  ```bash
  docker ps | grep planeai-backup    # → Up
  ```
- [ ] **Принудительный pre-acceptance бэкап** — чтобы точка отката была свежее 24 часов:
  ```bash
  docker compose -p plane-ce exec planeai-backup /usr/local/bin/backup_postgres.sh
  docker compose -p plane-ce exec planeai-backup /usr/local/bin/backup_minio.sh
  ls -la /var/lib/docker/volumes/planeai-backups/_data/postgres/ | tail -3
  # → planeai-2026-XX-XX-HHMM.dump  (свежий)
  ```
- [ ] Запустить restore-тест **на отдельной БД** — убедиться, что бэкап действительно восстанавливается:
  ```bash
  docker compose -p plane-ce exec planeai-backup /usr/local/bin/restore_test.sh
  # → OK: restored, row counts match within tolerance
  ```

### 1.4 Автоматическая проверка

Один command-line gate, который сводит всё выше в один отчёт:

```bash
docker compose -p plane-ce exec api python manage.py acceptance_check \
  --workspace <prod-workspace-id>
```

**Ожидаемый вывод — `ALL GREEN — safe to run backfill_embeddings.`** Без зелёного результата дальше не идти.

Сабсет (для повторной проверки одного пункта после фикса):
```bash
... acceptance_check --workspace <id> --check dpa,backup
```

JSON-режим (для скрипта-обёртки в CI):
```bash
... acceptance_check --workspace <id> --json
# → {"workspace_id": "...", "results": [...], "go": true}
```

---

## 2. Боевой бэкафилл

### 2.1 Dry-run + cost preview

> **Сначала dry-run.** Перед реальной выкаткой надо знать **сколько это стоит** и **сколько займёт времени**.

```bash
docker compose -p plane-ce exec api python manage.py backfill_embeddings \
  --workspace <prod-workspace-id> \
  --dry-run \
  --verbose
```

Запись в acceptance-документ:

| Метрика | Значение |
|---|---|
| Issues to index | `<n>` |
| Comments to index | `<n>` |
| Pages to index | `<n>` |
| **Total tasks** | `<n>` |
| Excluded projects | `<list>` |
| Rows skipped (excluded) | `<n>` |
| **Estimated tokens** | `<n>` |
| **Estimated OpenAI cost** | $`<X.YY>` |

⚠️ **Если estimated cost > $20** — пауза, согласование с Ильёй (PM). Дефолтный budget на воркспейс 5M токенов ≈ $0.10 OpenAI embed (мелочь) + позже Claude search ≈ $20-30/мес. **Бэкафилл одноразовый**, не должен сжигать месячный бюджет.

### 2.2 Real backfill

После того, как dry-run выглядит OK и PM подтвердил:

```bash
# 1. Засечь время начала
date +"%FT%T%z"  # → запишем в acceptance-документ

# 2. Бэкафилл. Флаг --i-confirm-dpa-closed обязателен (TZ 6.6 gate).
docker compose -p plane-ce exec api python manage.py backfill_embeddings \
  --workspace <prod-workspace-id> \
  --rate 3 \
  --i-confirm-dpa-closed \
  --verbose

# → Enqueued <N> reindex tasks for workspace ... (rate=3/s)
# → current chunks for workspace: 0
```

### 2.3 Мониторинг прогресса

Раз в 30 секунд (можно в `watch` цикле):

```bash
watch -n 30 'curl -fsS -H "Cookie: $COOKIE" \
  http://<host>/api/ai/workspaces/<id>/index-status/ | python -m json.tool'
```

Ожидаемый ход:
- 0-2 мин: `coverage` начинает расти, `indexed` идёт вверх.
- ~1 мин на каждые ~10-15 тасков (3 task/sec rate + время эмбеддинга OpenAI).
- При проблеме — алерт `PlaneAIProviderErrors` (если 429 от OpenAI) или `PlaneAIBackfillStuck` (если worker умер).

Цель: **`ready: true` и `coverage >= 0.95`** на all source types.

Запись в acceptance-документ:

| Метрика | Значение |
|---|---|
| Время старта | `<ISO>` |
| Время завершения (`ready=true`) | `<ISO>` |
| Длительность | `<HH:MM>` |
| Final coverage | `<x.xx>` |
| `total / indexed` (work_item) | `<X / Y>` |
| `total / indexed` (comment) | `<X / Y>` |
| `total / indexed` (page) | `<X / Y>` |
| Алертов за время бэкафилла | `<n>` (`<какие>`) |

### 2.4 Подтверждение приватные проекты НЕ проиндексированы

Это критическая проверка — без неё мы не имеем права раскатывать на команду.

```bash
# Список project_id всех приватных проектов
docker compose -p plane-ce exec api python manage.py shell <<'PY'
from ai.models import AIProjectSettings, DocumentChunk
private_ids = list(
    AIProjectSettings.objects.filter(exclude_from_ai=True)
    .values_list("project_id", flat=True)
)
print("private projects:", private_ids)

# Подсчёт чанков из приватных проектов — должен быть 0
n = DocumentChunk.objects.filter(project_id__in=private_ids).count()
print("chunks from private projects:", n)
assert n == 0, f"LEAK: {n} chunks from private projects were indexed"
print("OK — private projects clean")
PY
```

- [ ] Подтверждено: **0 чанков** из проектов с `exclude_from_ai=True`.

---

## 3. Smoke всех фич на боевых данных

> Каждый кейс — реальная задача из воркспейса. Никаких синтетических примеров. Если кейс не работает — **отметить как red**, остановиться, разобрать вместе с FullStack.

### 3.1 Семантический поиск (TZ 2.x)

- [ ] **«Найти осмысленный ответ».** Запросить: «что у нас открыто по платежам?» (или любой реальный домен команды). Ответ должен:
  - вернуть SSE-стрим со `sources` в первом frame;
  - сослаться на 3-5 реальных задач по теме;
  - не сослаться на задачи из приватных проектов;
  - закончиться `done` frame с `usage.cost_usd > 0`.
- [ ] **«Поиск с конкретным фильтром».** «покажи задачи Васи за последний спринт» — sources должны содержать задачи Васи, не других людей.
- [ ] **«Поиск без результатов».** «что у нас по теме которой нет» — ответ корректно говорит «не нашлось», не галлюцинирует.
- [ ] **Время до первого `delta`** — < 3 сек. Если дольше — буферизация (см. RUNBOOK Troubleshooting → SSE).

### 3.2 Саммари / генерация (если TZ 3.2 реализован)

- [ ] Открыть длинную дискуссию в Plane (20+ комментариев). Запросить саммари. Должно:
  - сократить до 5-7 буллетов;
  - сохранить ключевые решения и кто-что предложил;
  - не выдумать факты.

Если TZ 3.2 ещё не в проде — отметить «N/A, not yet released» и перейти к 3.3.

### 3.3 Bulk-операции (если TZ 4.x реализован)

- [ ] Команда «закрыть все задачи с меткой `done-but-open`» через bulk-промпт. UI должен:
  - **показать список задач до выполнения** (подтверждение);
  - выполнить только после явного `Confirm`;
  - откат через UI (если поддерживается).

Если TZ 4.x ещё не в проде — отметить «N/A», перейти к 3.4.

### 3.4 Агент (если go получен в TZ 5.8)

> **На ограниченном наборе.** Включить агент на 1 проект с 2-3 тестовыми задачами. После 1 часа — проверить лог действий. Только потом расширять.

- [ ] Создать тестовый Issue: «опишите задачу подробнее». Через 30 сек должен появиться комментарий-черновик от агента.
- [ ] Создать дубль существующей задачи — агент должен поставить метку `🤖 возможно дубль` и комментарий со ссылкой на оригинал.
- [ ] Проверить UI `/settings/ai-agent`: видны последние действия, статусы, кнопка `Отменить` для `set_labels`.
- [ ] **Включить алерт `PlaneAIAgentLoop` сценарий** — попытаться спровоцировать петлю. Алерт должен сработать за 2 мин.

Если TZ 5.x не активирован (go=no в TZ 5.8) — отметить «agent disabled by 5.8 decision», перейти к 3.5.

### 3.5 Дашборд расхода (TZ 6.3)

- [ ] Открыть `/<slug>/settings/ai-usage`. Должно показывать:
  - реальный расход за месяц (после бэкафилла = embed + первые поисковые вызовы);
  - прогресс-бар цвета согласно threshold (вероятно зелёный — первый день);
  - разбивку по фичам (embed > 0, остальные растут от smoke-тестов выше);
  - топ-1: Никита (или кто гонял smoke);
  - тренд по дням — два-три дня, последний день = сегодня.
- [ ] Цифры **совпадают** с прямым запросом БД:
  ```bash
  docker compose -p plane-ce exec -e PGPASSWORD=$POSTGRES_PASSWORD plane-db \
    psql -U plane -d plane -c "
      SELECT feature, COUNT(*), SUM(cost_usd)::numeric(10,4)
      FROM ai_usage_log
      WHERE workspace_id = '<prod-workspace-id>'
      AND created_at >= date_trunc('month', now())
      GROUP BY feature;"
  ```

### 3.6 Алерты (TZ 6.2)

- [ ] Открыть Alertmanager UI (`http://<host>:9093`). Список правил: **все 7** активны, ни одно не firing на нормальном состоянии.
- [ ] Подсмотреть Prometheus (`http://<host>:9090/targets`): scrape job `planeai` — `up=1`.
- [ ] Открыть Grafana (`http://<host>:3000`), dashboard `planeAI — overview`. На каждой панели реальные данные.
- [ ] **Искусственный smoke алерта**: поднять `monthly_token_budget` до 100, выполнить 3 поиска — `tokens_used / budget > 0.8` через минуту — должен прийти `PlaneAIBudgetWarning` в Slack. После проверки вернуть `monthly_token_budget` обратно (5M).

### 3.7 Health endpoint

- [ ] `curl http://<host>/api/ai/health/ | jq .` — статус `ok`, все checks `ok`.

---

## 4. Стоимость бэкафилла (фиксируется в отчёте)

Заполнить после завершения, скопировать в commit-сообщение акceptance:

```
бэкафилл — итоги

воркспейс:        <slug> (<uuid>)
дата:             2026-XX-XX
длительность:     <HH:MM>

индексировано:
  work_items:     <X> / <Y total in ws>
  comments:       <X> / <Y total>
  pages:          <X> / <Y total>
исключено:        <Z> rows (private projects: <list>)

стоимость:
  estimated (dry-run):  $<A.AAAA>
  actual (AIUsageLog):  $<B.BBBB>
  delta:                <X>%

по факту scrape AIUsageLog:
  embedding tokens:     <N>
  embedding cost USD:   $<B.BBBB>
  model:                text-embedding-3-small
```

```bash
# SQL для actual cost из AIUsageLog
docker compose -p plane-ce exec -e PGPASSWORD=$POSTGRES_PASSWORD plane-db \
  psql -U plane -d plane -c "
    SELECT feature, model,
           SUM(input_tokens) AS tokens,
           SUM(cost_usd)::numeric(10,4) AS usd
    FROM ai_usage_log
    WHERE workspace_id = '<id>'
      AND feature = 'embed'
      AND created_at >= '<start-ISO>'
    GROUP BY feature, model;"
```

⚠️ Если actual > estimated × 2 — расследовать. Возможные причины: чанки больше ожидаемого, рестарт воркера → повторные embed-вызовы (но reindex_source идемпотентен по content_hash, так что это маловероятно).

---

## 5. Отчёт о готовности к релизу

После прохождения всех разделов 1-4 — единственный go/no-go параграф в acceptance-документе:

```
GO / NO-GO RELEASE DECISION
==========================
Acceptance date:  2026-XX-XX
PM (Илья):        ✓ GO   /  ✗ NO-GO  (комментарий)
QA (Никита):      ✓ GO   /  ✗ NO-GO  (комментарий)
FullStack:        ✓ GO   /  ✗ NO-GO  (комментарий — если они правили
                                       что-то в процессе acceptance)

Открытые риски (must NOT block release но команда должна знать):
  - ...
  - ...

Открытые баги для пост-релиза:
  - ...
```

**Решение GO от любых двух из трёх — релиз.** NO-GO от любого одного — стоп, разобрать причину, повторить acceptance.

---

## DoD ТЗ 6.6 — статус

- [x] DPA проверка автоматизирована (`acceptance_check --check dpa`).
- [x] Exclude-from-ai проверка автоматизирована (`acceptance_check --check private`).
- [x] Свежесть бэкапа автоматизирована (`acceptance_check --check backup`).
- [x] DPA gate в `backfill_embeddings` (`--i-confirm-dpa-closed` обязателен).
- [x] Cost estimate в dry-run (`--dry-run` показывает $).
- [x] Verification приватных проектов задокументировано (раздел 2.4).
- [x] Smoke-чеклист всех фич (раздел 3).
- [x] Шаблон отчёта о стоимости (раздел 4).
- [x] Go/no-go параграф (раздел 5).
- [ ] **Заполнен реальный отчёт** — закрывается ТОЛЬКО после проведения acceptance на проде. Этот файл — runbook, не запись.

## Связи

- ТЗ 0.7 GDPR → [GDPR.md](GDPR.md)
- ТЗ 1.6 backfill → [`ai/management/commands/backfill_embeddings.py`](ai/management/commands/backfill_embeddings.py)
- ТЗ 1.8 index-status → [`ai/views.py:IndexStatusView`](ai/views.py)
- ТЗ 3.4 exclude_from_ai → [`AIProjectSettings`](ai/models.py)
- ТЗ 5.8 agent go/no-go → [SPRINT-5-ACCEPTANCE.md](SPRINT-5-ACCEPTANCE.md)
- ТЗ 6.1 backup → [BACKUP.md](BACKUP.md)
- ТЗ 6.2 monitoring → [MONITORING.md](MONITORING.md)
- ТЗ 6.3 budget dashboard → [BUDGET.md](BUDGET.md)
- ТЗ 6.4 runbook → [RUNBOOK.md](RUNBOOK.md)
- ТЗ 6.5 rollback (если что-то пойдёт не так) → [ROLLBACK.md](ROLLBACK.md)
- ТЗ 6.7 релиз (следующий шаг, после зелёной acceptance) → [tz/sprint-6/08-задача-6.8-релиз.md](tz/sprint-6/08-задача-6.8-релиз.md)
