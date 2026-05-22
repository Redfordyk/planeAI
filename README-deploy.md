# Plane CE — развёртывание (ТЗ 0.1)

**Статус:** локальный dry-run на Docker Desktop (Windows 11, 32 ГБ RAM, 16 vCPU). Прод-сервер (Ubuntu 24.04, 4 vCPU / 8 ГБ / 40 ГБ) — отдельная итерация, использовать те же файлы из `deploy-local/`.

## Зафиксированные параметры (Plane CE 1.3.1)

| Параметр | Значение |
|---|---|
| Версия Plane | `1.3.1` (тег `stable` на момент 22.05.2026) |
| Django-app основной схемы | **`db`** (подтверждено по миграциям `db.0001…`) — закрывает часть допущений ТЗ 0.2 |
| Compose project name | `plane-ce` |
| Docker network | `plane-ce_default` (bridge) |
| Рабочая директория `api` | `/code` (внутри — `manage.py`, `plane/`, `requirements/`, `bin/`, `templates/`) |
| Внешний HTTP порт | `8088` (на проде поменять на `80`) |
| Внешний HTTPS порт | `8443` (на проде `443`) |
| Внутренний `SITE_ADDRESS` Caddy | `:80` (не менять — это порт внутри контейнера) |

## Контейнеры (13 сервисов, имена в compose project `plane-ce`)

| Сервис | Контейнер | Образ | Назначение |
|---|---|---|---|
| api | `plane-ce-api-1` | `makeplane/plane-backend:stable` | Django + Gunicorn (REST API) |
| worker | `plane-ce-worker-1` | `makeplane/plane-backend:stable` | Celery worker |
| beat-worker | `plane-ce-beat-worker-1` | `makeplane/plane-backend:stable` | Celery beat (периодические задачи) |
| migrator | `plane-ce-migrator-1` | `makeplane/plane-backend:stable` | Прогоняет миграции и `Exited (0)` |
| web | `plane-ce-web-1` | `makeplane/plane-frontend:stable` | Next.js основной UI |
| space | `plane-ce-space-1` | `makeplane/plane-space:stable` | Публичные view |
| admin | `plane-ce-admin-1` | `makeplane/plane-admin:stable` | Админка инстанса |
| live | `plane-ce-live-1` | `makeplane/plane-live:stable` | WebSocket / live updates |
| plane-db | `plane-ce-plane-db-1` | `postgres:15.7-alpine` | PostgreSQL (для AI поверх — нужен pgvector, ставить в спринте 0.x) |
| plane-redis | `plane-ce-plane-redis-1` | `valkey/valkey:7.2.11-alpine` | Кэш / брокер |
| plane-mq | `plane-ce-plane-mq-1` | `rabbitmq:3.13.6-management-alpine` | Celery broker |
| plane-minio | `plane-ce-plane-minio-1` | `minio/minio:latest` | S3-совместимое хранилище |
| proxy | `plane-ce-proxy-1` | `makeplane/plane-proxy:stable` | Caddy reverse proxy |

## Файлы развёртывания

- `deploy-local/docker-compose.yml` — копия `deployments/cli/community/docker-compose.yml` (без модификаций)
- `deploy-local/.env` — копия `deployments/cli/community/variables.env` со следующими изменениями:
  - `LISTEN_HTTP_PORT=8088` (был `80`)
  - `LISTEN_HTTPS_PORT=8443` (был `443`)
  - `WEB_URL=http://${APP_DOMAIN}:8088`
  - `CORS_ALLOWED_ORIGINS=http://${APP_DOMAIN}:8088`
  - `SITE_ADDRESS=:80` — НЕ менять, это внутренний порт Caddy

## Команды запуска

```powershell
cd E:\Dev\planeAI\deploy-local
docker compose -p plane-ce pull
docker compose -p plane-ce up -d
# дождаться, пока migrator завершится:
docker inspect -f '{{.State.Status}} {{.State.ExitCode}}' plane-ce-migrator-1
# UI:
start http://localhost:8088
```

## Команды проверки

```powershell
docker compose -p plane-ce ps                            # все Up, migrator Exited (0)
docker compose -p plane-ce logs api --tail 50            # нет фатальных ошибок
docker compose -p plane-ce exec api pwd                  # /code
docker network ls | findstr plane                        # plane-ce_default
curl.exe -sS -o NUL -w "HTTP %{http_code}`n" http://localhost:8088/
```

## Результат прогона (22.05.2026)

- Все 12 сервисных контейнеров `Up`, `web/admin/space` — `healthy`.
- `migrator` отработал и вышел с кодом `0`.
- UI отвечает HTTP 200 на `http://localhost:8088/`.
- OOM не наблюдалось (хост — 32 ГБ; для прод-сервера 8 ГБ дополнительно проверять `dmesg | grep -i oom`).

## DoD — статус

- [x] Все контейнеры `running`/`healthy`, ни одного в `Restarting`
- [x] Веб-интерфейс открывается (создание админа/воркспейса — следующий ручной шаг через UI на http://localhost:8088)
- [x] RAM достаточно, OOM не зафиксировано (хост = 32 ГБ; проверка `dmesg` применима только к Linux-проду)
- [x] Зафиксированы: версия Plane, имена контейнеров, имя сети, рабочая директория `api`
- [x] README-deploy.md закоммичен

## Что осталось руками (вне DoD ТЗ 0.1)

1. Открыть http://localhost:8088, создать первого админа и тестовый воркспейс.
2. На прод-сервере: повторить с `LISTEN_HTTP_PORT=80`, `LISTEN_HTTPS_PORT=443`, `APP_DOMAIN=<реальный>`, `CERT_EMAIL=<реальный>`.
3. Перед спринтом 1 — установить pgvector в `plane-db` (ТЗ ниже по дорожной карте).
