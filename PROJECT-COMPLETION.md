# PROJECT-COMPLETION — финальная sign-off planeAI (ТЗ 6.8)

> Единый артефакт «проект завершён». Собирает в один документ закрытие всех 6 спринтов, ссылки на acceptance/release/GDPR-документы, и подписи всей команды. Без подписей здесь — проект формально **не сдан**.
>
> Подписывается **в течение 14 дней после завершения фазы 3** ([RELEASE.md](RELEASE.md)). Не раньше — нужно убедиться, что в боевой эксплуатации нет крит-инцидентов. Не позже — детали забываются.

---

## 0. Сводка

| | |
|---|---|
| Проект | planeAI — ИИ-надстройка над Plane CE |
| Старт | `YYYY-MM-DD` (TZ 0.1) |
| Финальный релиз (фаза 3) | `YYYY-MM-DD` (TZ 6.8) |
| Длительность | `<N>` недель |
| Сдано фичей | Поиск ✅ / Саммари `<✅/⏳>` / Bulk `<✅/⏳>` / Агенты `<✅/⏳>` / Дашборд расхода ✅ |
| Стек на проде | Plane CE 1.3.1 + `plane-backend-ai` + pgvector 0.8.2 + Prometheus + Alertmanager + Grafana + backup-сайдкар |
| Расход на staging-разработку | $`<X>` |
| Прогноз месячного расхода на проде | $`<Y>`/мес |

---

## 1. Что закрыто по спринтам

### Спринт 0 — Подготовка

| TZ | Тема | DoD статус | Артефакт |
|---|---|---|---|
| 0.1 | Развернуть Plane CE | ✅ | [README-deploy.md](README-deploy.md) |
| 0.2 | Верификация схемы Plane | ✅ | [SCHEMA.md](SCHEMA.md), [scripts/verify_schema.py](scripts/verify_schema.py) |
| 0.3 | ASGI vs WSGI решение | ✅ | [STREAMING.md](STREAMING.md) |
| 0.4 | pgvector + CVE | ✅ | [PGVECTOR.md](PGVECTOR.md) |
| 0.5 | Образ `plane-backend-ai` | ✅ | [IMAGE.md](IMAGE.md), [Dockerfile.ai](Dockerfile.ai) |
| 0.6 | ACL-модель | ✅ | [ACL.md](ACL.md), [ai/acl.py](ai/acl.py) |
| 0.7 | DPA / GDPR (каркас) | ✅ | [GDPR.md](GDPR.md) |
| 0.8 | ИИ-проект в Plane | ✅ | tz-папка |
| 0.9 | CI/CD | ✅ | [CICD.md](CICD.md), [.github/workflows/planeai-*.yml](.github/workflows/) |
| 0.10 | Staging | ✅ | [STAGING.md](STAGING.md) |
| 0.11 | Секреты | ✅ | [SECRETS.md](SECRETS.md), [scripts/gen_encryption_key.py](scripts/gen_encryption_key.py) |
| 0.12 | Онбординг | ✅ | [ONBOARDING.md](ONBOARDING.md) |

### Спринт 1 — Ингест + индекс

| TZ | Тема | DoD статус | Артефакт |
|---|---|---|---|
| 1.1 | Модели `ai` | ✅ | [ai/models.py](ai/models.py) |
| 1.2 | Миграция pgvector + HNSW | ✅ | [ai/migrations/](ai/migrations/) |
| 1.3 | LLM-абстракция | ✅ | [ai/providers.py](ai/providers.py) |
| 1.4 | Хуки ингеста | ✅ | [ai/signals.py](ai/signals.py) |
| 1.5 | Чанкинг + reindex | ✅ | [ai/chunking.py](ai/chunking.py), [ai/tasks.py](ai/tasks.py) |
| 1.6 | Backfill | ✅ | [ai/management/commands/backfill_embeddings.py](ai/management/commands/backfill_embeddings.py) |
| 1.7 | Учёт токенов + бюджет | ✅ | [ai/usage.py](ai/usage.py), [ai/guards.py](ai/guards.py) |
| 1.8 | Index-status | ✅ | [ai/views.py:IndexStatusView](ai/views.py) |
| 1.9 | Юнит-тесты | ✅ | [ai/tests/](ai/tests/) |

### Спринт 2 — Поиск + RAG

| TZ | Тема | DoD статус | Артефакт |
|---|---|---|---|
| 2.1-2.8 | RAG retrieval / SSE / UI / safety / acceptance | ✅ | [ai/search.py](ai/search.py), [ai/streaming.py](ai/streaming.py), [SPRINT-2-ACCEPTANCE.md](SPRINT-2-ACCEPTANCE.md) |
| 2.9 | Acceptance | ✅ | [SPRINT-2-ACCEPTANCE.md](SPRINT-2-ACCEPTANCE.md) |

### Спринт 3 — Саммари / генерация

| TZ | Тема | DoD статус | Артефакт |
|---|---|---|---|
| 3.x | Summarize / draft generation | `<✅/⏳ — отметить по факту>` | `<ссылка>` |
| 3.4 | exclude_from_ai | ✅ | [ai/models.py:AIProjectSettings](ai/models.py), retroactive cleanup в [ai/signals.py](ai/signals.py) (TZ 6.7) |

### Спринт 4 — Bulk

| TZ | Тема | DoD статус | Артефакт |
|---|---|---|---|
| 4.x | Bulk operations + confirmation flow | `<✅/⏳>` | `<ссылка>` |

### Спринт 5 — Агенты

| TZ | Тема | DoD статус | Артефакт |
|---|---|---|---|
| 5.1 | AIAgent provisioning | ✅ | [ai/management/commands/create_ai_agent.py](ai/management/commands/create_ai_agent.py) |
| 5.2 | Worker + white-list + audit | ✅ | [ai/agent_worker.py](ai/agent_worker.py), [ai/tests/test_agent_worker.py](ai/tests/test_agent_worker.py) |
| 5.3 | Triage scenario | ✅ | [ai/triage.py](ai/triage.py) |
| 5.4 | Dedupe scenario | ✅ | [ai/dedupe.py](ai/dedupe.py) |
| 5.5 | Describe scenario | ✅ | [ai/describe.py](ai/describe.py) |
| 5.6 | UI: feed / undo / toggle / badge | ✅ | [ai/agent_views.py](ai/agent_views.py), [apps/web/core/components/ai/agent-*.tsx](apps/web/core/components/ai) |
| 5.7 | Safety suite | ✅ | [ai/tests/test_agent_safety.py](ai/tests/test_agent_safety.py) |
| 5.8 | Acceptance | ✅ | [SPRINT-5-ACCEPTANCE.md](SPRINT-5-ACCEPTANCE.md) |

### Спринт 6 — Прод и выпуск

| TZ | Тема | DoD статус | Артефакт |
|---|---|---|---|
| 6.1 | Бэкап / restore | ✅ | [BACKUP.md](BACKUP.md), [scripts/backup/](scripts/backup/), [deploy-local/docker-compose.backup.yml](deploy-local/docker-compose.backup.yml) |
| 6.2 | Мониторинг + алерты | ✅ | [MONITORING.md](MONITORING.md), [ai/metrics.py](ai/metrics.py), [ai/health.py](ai/health.py), [ai/alerting.py](ai/alerting.py), [deploy-local/monitoring/](deploy-local/monitoring/) |
| 6.3 | Дашборд расхода | ✅ | [BUDGET.md](BUDGET.md), [ai/usage_views.py](ai/usage_views.py), [apps/web/core/components/ai/usage-dashboard.tsx](apps/web/core/components/ai/usage-dashboard.tsx) |
| 6.4 | RUNBOOK | ✅ | [RUNBOOK.md](RUNBOOK.md) |
| 6.5 | План отката | ✅ | [ROLLBACK.md](ROLLBACK.md), [ai/management/commands/disable_ai.py](ai/management/commands/disable_ai.py), [enable_ai.py](ai/management/commands/enable_ai.py) |
| 6.6 | Приёмочный прогон + DPA gate | ✅ | [SPRINT-6-ACCEPTANCE.md](SPRINT-6-ACCEPTANCE.md), [ai/management/commands/acceptance_check.py](ai/management/commands/acceptance_check.py) |
| 6.7 | GDPR чеклист | ✅ | [GDPR-RELEASE.md](GDPR-RELEASE.md), [ai/management/commands/gdpr_check.py](ai/management/commands/gdpr_check.py), [docs/processing-register.md](docs/processing-register.md), [docs/team-notice-ai-processing.md](docs/team-notice-ai-processing.md) |
| 6.8 | Релиз + фидбэк | ✅ | [RELEASE.md](RELEASE.md), [ai/management/commands/rollout_status.py](ai/management/commands/rollout_status.py), [docs/user-guide.md](docs/user-guide.md), [PHASE-2-BACKLOG.md](PHASE-2-BACKLOG.md), [RETROSPECTIVE.md](RETROSPECTIVE.md), **этот файл** |

> Любая `⏳` в столбце DoD статус означает, что задача переехала в [PHASE-2-BACKLOG.md](PHASE-2-BACKLOG.md). Это не блокер sign-off-а проекта — потому что фичи добавляются итеративно, а первоначальные **гарантии** (безопасность, GDPR, мониторинг, бэкап, откат) — закрыты на 100%.

---

## 2. Gate-документы — каждый подписан

| Gate | Документ | Подпись | Дата |
|---|---|---|---|
| Acceptance спринт 2 (поиск) | [SPRINT-2-ACCEPTANCE.md](SPRINT-2-ACCEPTANCE.md) | Илья / Никита | `YYYY-MM-DD` |
| Acceptance спринт 5 (агент) | [SPRINT-5-ACCEPTANCE.md](SPRINT-5-ACCEPTANCE.md) | Илья / Никита | `YYYY-MM-DD` |
| Acceptance спринт 6 (прод) | [SPRINT-6-ACCEPTANCE.md](SPRINT-6-ACCEPTANCE.md) | Илья / Никита / Костя | `YYYY-MM-DD` |
| GDPR release | [GDPR-RELEASE.md](GDPR-RELEASE.md) | Илья (PM, юр. ответственный) | `YYYY-MM-DD` |
| Release rollout | [RELEASE.md](RELEASE.md) — фаза 3 завершена | Илья | `YYYY-MM-DD` |
| Retrospective | [RETROSPECTIVE.md](RETROSPECTIVE.md) | вся команда | `YYYY-MM-DD` |

Любая ячейка пустая = `NOT COMPLETE`. **Не подписывать этот документ, пока все gate'ы выше не подписаны.**

---

## 3. Финальная статистика

> Заполняется после 14 дней пост-релиз наблюдения.

| Метрика | Значение |
|---|---|
| Воркспейсов с включённым ИИ | `<N>` |
| Активных пользователей за неделю | `<N>` |
| Поисковых запросов за неделю | `<N>` |
| Действий агента за неделю (applied/rejected) | `<X>/<Y>` |
| Средняя стоимость на пользователя в день | $`<X>` |
| Срабатываний алертов мониторинга | `<N>` (`<какие>`) |
| Инцидентов с эскалацией | `<N>` (`<краткое описание>`) |
| Откатов через kill switch | `<N>` (`<когда и почему>`) |
| Сданных фидбэк-отзывов | `<N>` |
| Позитивных / нейтральных / негативных в фидбэке | `<X> / <Y> / <Z>` |

---

## 4. Подпись

> **Этот блок заполняется в день, когда все вышеуказанные пункты закрыты, и не раньше.**

```
DECISION:    [ ] PROJECT COMPLETE — sign off all gates
             [ ] HOLD — outstanding blockers below

Date signed: YYYY-MM-DD

PM:          Илья Х.            ___________________________
QA:          Никита Ф.          ___________________________
FullStack:   Костя              ___________________________
FullStack:   Вова               ___________________________
Frontend:    Эдик               ___________________________
```

Каждая подпись = «я лично проверил мои разделы, согласен с разрешением проекта как завершённого, перехожу на phase 2 backlog».

### Если HOLD

Список открытых блокеров с дедлайнами:

| # | Что не закрыто | Кто чинит | Дедлайн | Затрагиваемый gate |
|---|---|---|---|---|
| | | | | |

После закрытия всех — назначить новую дату подписи, повторить ревью.

---

## 5. Что дальше

После подписи:

1. **PHASE 2.** Бэклог в [PHASE-2-BACKLOG.md](PHASE-2-BACKLOG.md) разбирается на спринт-7 → ... — обычный итеративный режим разработки.
2. **Эксплуатация.** Per [RUNBOOK.md](RUNBOOK.md). Дежурство on-call в `#planeai-feedback`. Ежемесячный отчёт PM руководству.
3. **Регулярная безопасность.** Раз в квартал — повторный прогон `gdpr_check` + сверка с обновлёнными политиками DPA. Бамп pgvector / Plane image по CVE-feed.
4. **Этот файл — read-only.** История изменений только через PR с явной правкой и обновлённой подписью.

---

## DoD ТЗ 6.8 — статус

- [x] Все 6 спринтов отражены в табличном виде с ссылками на артефакты.
- [x] Каждый gate-документ имеет место для подписи.
- [x] Финальная статистика — шаблон, ждёт реальных цифр.
- [x] Sign-off секция для каждого члена команды.
- [x] Описано, что происходит после подписи (phase 2 + регулярная эксплуатация).
- [ ] **Реальные подписи** — закрывается в день sign-off-а.

## Связи (полный индекс)

См. [RUNBOOK.md → раздел 13 «Индекс документов»](RUNBOOK.md#13-индекс-документов).
