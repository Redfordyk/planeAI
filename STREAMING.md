# STREAMING — выбор реализации SSE (ТЗ 0.3)

## Вывод: **ASGI**. В задаче 2.3 использовать async-вариант.

## Что наблюдалось внутри `api`-контейнера (Plane CE 1.3.1)

`ps aux` показывает:

```
1 root  {gunicorn} /usr/local/bin/python3.12 /usr/local/bin/gunicorn -w 1 \
        -k uvicorn.workers.UvicornWorker plane.asgi:application \
        --bind 0.0.0.0:8000 --max-requests 1200 --max-requests-jitter 1000
59 root {gunicorn} ... (worker process)
```

Хвост `bin/docker-entrypoint-api.sh`:

```
exec gunicorn -w "$GUNICORN_WORKERS" -k uvicorn.workers.UvicornWorker \
     plane.asgi:application --bind 0.0.0.0:"${PORT:-8000}" \
     --max-requests 1200 --max-requests-jitter 1000 --access-logfile -
```

Точка входа — `plane.asgi:application` (ProtocolTypeRouter из `channels`). Воркер-класс — `uvicorn.workers.UvicornWorker`, то есть процесс крутит реальный asyncio-цикл, а не WSGI-thread.

## Что это значит для задачи 2.3 (SSE стриминг Claude)

Используем **async-вариант**:

- Хэндлер — `async def`, возвращает `StreamingHttpResponse(async_iter(), content_type="text/event-stream")`.
- LLM-вызов — `anthropic.AsyncAnthropic(...).messages.stream(...)`, итерируем `async for event in stream`.
- **Любой ORM-вызов внутри async-кода — только через `asgiref.sync.sync_to_async`** (или `await Model.objects.aget(...)` для нативных async-методов Django ≥ 4.1). Иначе при первом обращении к БД из event-loop поднимется `SynchronousOnlyOperation`.
- Не закрывать соединение БД явно — Django сам управляет. Но настройка `CONN_MAX_AGE` критична (см. ниже).

Sync-генератор поверх `gunicorn/uvicorn` тоже технически работает, но он блокирует event-loop воркера на всё время стрима. С `-w 1` (как в дефолтном `.env` Plane) это означает, что один открытый SSE «съедает» весь api-контейнер. Async-вариант параллелит десятки одновременных стримов в одном процессе.

## Требование: `CONN_MAX_AGE=0` для async-вьюх (или отдельный DB-алиас)

В async-режиме Django держит подключение к Postgres на уровне per-thread/per-task. При `CONN_MAX_AGE>0` соединение может оказаться шарингованным между корутинами — это race на курсорах и редкие, нестабильные баги.

Безопасные варианты:

1. **Глобально `CONN_MAX_AGE=0`** (по запросу новый коннект). Просто, но даёт latency-overhead.
2. **Отдельный DB-alias для AI-вьюх** с `CONN_MAX_AGE=0`, синхронные части Plane оставить на дефолте. Чище и быстрее. Делается через `DATABASES["ai_async"] = {..., "CONN_MAX_AGE": 0}` + `using="ai_async"` в наших `aget()`/`afilter()`.

**Решение:** в спринте 2 (когда докатим до 2.3) — вариант 2. До того дополнительная настройка не требуется, потому что ORM-вызовов из async-контекста у нас ещё нет.

## Ещё пара мелочей, которые упростят 2.3

- В `Caddyfile` Plane стоит дефолтная буферизация ответов; для SSE на проде проверить, что прокси отдаёт `Content-Type: text/event-stream` потоково без буферизации. Если упрётся в буфер — `flush_threshold` / отключить буфер на этом маршруте.
- `nginx`/proxy могут добавлять `X-Accel-Buffering: no` — на `gunicorn -k uvicorn` это не нужно, но иметь в виду.
- `--max-requests 1200` (worker recycle) безопасен для долгих SSE, потому что recycle случится после graceful окончания текущих запросов.

## Связи

- Базируется на ТЗ 0.1 (рабочий Plane).
- Разблокирует ТЗ 2.3 (SSE-стриминг Claude).
