# BUDGET — расход токенов и стоимость ИИ (ТЗ 6.3)

> Кто читает: **Илья (PM)** — для еженедельного отчёта по затратам; **Костя/Вова (FullStack)** — когда расход растёт быстрее ожидаемого; **Никита (QA)** — для проверки, что новая фича логирует расход.

## TL;DR

- **Где смотреть.** В UI: страница `Workspace settings → ИИ-расход` (компонент [`UsagePage`](apps/web/core/components/ai/usage-page.tsx)). Через API: `GET /api/ai/workspaces/<id>/usage/stats/?from=...&to=...`. Доступ — только админ воркспейса.
- **Откуда числа.** Из таблицы `ai_usage_log` ([`AIUsageLog`](ai/models.py)). Каждая ИИ-фича вызывает [`record_usage()`](ai/usage.py) с явным `feature=...` — это единственная точка записи, поэтому числа в дашборде = числа в БД = числа, которые видит бюджет-гард ([`ai.guards`](ai/guards.py)) и Prometheus-метрики (ТЗ 6.2).
- **Что считается в бюджет.** `input + output + cache_creation` токены. `cache_read` НЕ считаются — это уже скидка на повторный ввод; считать их повторно = штрафовать за prompt caching.

## Связь с другими подсистемами

```
   feature ──► ai.usage.record_usage ──► AIUsageLog (Postgres)
                                          │
                       ┌──────────────────┼──────────────────────────┐
                       ▼                  ▼                          ▼
              ai.guards (1.7)     ai.metrics (6.2)        ai.usage.compute_usage_stats
              hard-block 429      planeai_workspace_tokens   GET /usage/stats/ (6.3)
                                  + Prometheus alerts        UsageDashboard.tsx
```

Все три consumer-а смотрят на один и тот же лог — расхождения быть не должно.

## Что показывает дашборд

Карточки сверху вниз (см. [`apps/web/core/components/ai/usage-dashboard.tsx`](apps/web/core/components/ai/usage-dashboard.tsx)):

1. **Расход за период.** Общая сумма $, общая сумма токенов, прогресс-бар vs `monthly_token_budget` из [`WorkspaceAIConfig`](ai/models.py). Цвет бара = тот же threshold, что у Prometheus-алертов:
   - зелёный < 80%,
   - жёлтый ≥ 80% → срабатывает `PlaneAIBudgetWarning`,
   - оранжевый ≥ 95% → срабатывает `PlaneAIBudgetCritical`,
   - красный — `exceeded` (бюджет-гард уже отдаёт 429 на каждый ИИ-вызов).
2. **Расход по фичам.** Все 5 фичей перечислены всегда, даже с нулевым расходом — чтобы PM сразу видел "поиск не используется", "агент жжёт половину бюджета", и т.п.
3. **Топ пользователей** (по cost desc, до 10). Если пользователь удалён — строка остаётся с подписью `(удалённый пользователь)`, так суммы сходятся.
4. **Расход по дням.** Каждый день в окне (включая нулевые) — чтобы тренд не выглядел рваным.
5. **Расход по моделям.** `claude-sonnet-4-6`, `claude-haiku-4-5-20251001`, `text-embedding-3-small` — для понимания, не съезжает ли роутер на дорогую модель.

Фильтры периода (preset-ы): "Этот месяц" (дефолт), "Прошлый месяц", "30 дней", "7 дней". Кастомный диапазон — на стороне бэка уже работает (`?from=&to=`), UI-пикер не сделан намеренно (сложность ↑, бенефит на MVP — нет).

## Что фичи должны логировать

| Фича | TZ | `feature=` | Где вызов `record_usage` |
|---|---|---|---|
| Эмбеддинги при индексации | 1.5 | `FEATURE_EMBED` | [`ai/tasks.py`](ai/tasks.py) |
| Семантический поиск + RAG | 2.3 | `FEATURE_INTENT_SEARCH` | [`ai/search.py`](ai/search.py) + [`ai/streaming.py`](ai/streaming.py) (default) |
| Генерация / саммари | 3.2 | `FEATURE_SUMMARIZE` | **не реализовано** (см. ниже) |
| Bulk-операции | 4.2 | `FEATURE_BULK` | **не реализовано** (см. ниже) |
| Агент | 5.2 | `FEATURE_AGENT` | [`ai/agent_worker.py`](ai/agent_worker.py) |

Дашборд работает корректно для всех пяти фичей — он использует `feature` из таблицы. Когда 3.2 и 4.2 будут реализованы, **обязательно** пробросить корректный `feature=` в `claude_sse(...)` или прямой вызов `record_usage(...)`. Без этого расход уходит в `intent_search` (дефолт `claude_sse`) и админ не видит, что съело бюджет.

Контракт регрессионно зафиксирован в [`ai/tests/test_usage_stats.py::test_by_feature_pads_all_five_features`](ai/tests/test_usage_stats.py) — он падает, если в `ALL_FEATURES` ([`ai/usage.py`](ai/usage.py)) добавили новый код, но забыли расширить дашборд.

## API

```
GET /api/ai/workspaces/<workspace_id>/usage/stats/
    ?from=2026-05-01T00:00:00Z      (optional, ISO-8601)
    &to=2026-05-31T23:59:59Z        (optional, ISO-8601)
    &top_users=10                    (default 10, cap 50)
```

Ответ:

```json
{
  "period": {"start": "...", "end": "..."},
  "totals": {
    "calls": 1234,
    "input_tokens": 50000,
    "output_tokens": 12000,
    "cache_read_tokens": 30000,
    "cache_creation_tokens": 5000,
    "billable_tokens": 67000,
    "cost_usd": "1.234567"
  },
  "by_feature": [
    {"feature": "intent_search", "calls": 800, "billable_tokens": 40000, "cost_usd": "0.80"},
    ... все 5 фичей, нули включительно
  ],
  "by_model":   [...],
  "by_user":    [...],  // top N
  "by_day":     [...],  // каждый день в окне
  "budget": {
    "tokens_used": 67000,
    "tokens_budget": 5000000,
    "ratio": 0.0134,
    "exceeded": false,
    "level": "ok"        // ok|warning|critical|exceeded|unset
  }
}
```

Ошибки:
- `400` — `from`/`to` без пары, неверный ISO-8601, или окно > 366 дней.
- `403` — caller не админ воркспейса.

## Когда дашборд "врёт"

Все известные источники расхождения:

1. **Новая фича без `record_usage`.** Самая частая причина. Проверка: на стенде вызвать новую фичу один раз, через минуту обновить дашборд — счётчик `calls` должен вырасти. Если нет — фича не логируется.
2. **Кэш-read.** Если PM ждёт, что в `billable_tokens` войдут все токены, разочарование: cache_read исключены (см. TL;DR). Они показаны отдельно в `totals.cache_read_tokens` — это статья экономии, не расхода.
3. **Удалённые пользователи.** Их трафик попадает в строку `user_id=null` ("удалённый пользователь"). Если пропустить — суммы не сойдутся. Дашборд рендерит эту строку явно.
4. **Долгий стрим.** `record_usage` для SSE-поиска (`claude_sse`) вызывается в самом конце потока. Если поток был оборван clientside — токены НЕ запишутся. Это редкий случай (несколько вызовов в день максимум), но достаточный, чтобы заметить расхождение с провайдерским billing dashboard на 1–2%.
5. **Цена выкатывается.** Цены закреплены в [`ai/pricing.py`](ai/pricing.py). Если Anthropic/OpenAI повысили — пока не обновили словарь, $-цифры отстают. Обновлять синхронно с реальной выкаткой.

## Бюджет: куда крутить ручки

- `monthly_token_budget` на `WorkspaceAIConfig` — мягкий лимит, при превышении бюджет-гард отдаёт 429 (см. ТЗ 1.7).
- Дефолт 5 млн токенов/месяц — это ≈ $20–30 для Sonnet или ≈ 1 ГБ контента для эмбеддингов. Поднимать только если PM подтвердил расход.
- Менять через Django shell:
  ```bash
  docker compose exec api python manage.py shell -c "
  from ai.models import WorkspaceAIConfig
  WorkspaceAIConfig.objects.filter(workspace_id='<id>').update(monthly_token_budget=20_000_000)
  "
  ```
  или через админку Django.

## Связи

- TZ 1.7 → запись `AIUsageLog` + `record_usage`.
- TZ 6.2 → Prometheus metrics + алерты на тех же числах (`planeai_workspace_tokens`, `PlaneAIBudgetWarning/Critical`).
- TZ 6.3 (этот документ) → UI и read-API.
- При апгрейде до экспорта в CSV / xlsx — фронт может вызвать тот же endpoint и сериализовать локально; на бэке менять нечего.
