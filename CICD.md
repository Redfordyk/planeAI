# CI/CD pipeline (ТЗ 0.9)

## Что есть

Два workflow в [`.github/workflows/`](.github/workflows/):

1. **[`planeai-ci.yml`](.github/workflows/planeai-ci.yml)** — на каждый PR / push в `main`/`preview`, который трогает `ai/`, `scripts/`, `deploy-local/`, `Dockerfile.ai` или сами workflow-файлы.
2. **[`planeai-deploy-staging.yml`](.github/workflows/planeai-deploy-staging.yml)** — деплой на staging после успешного CI на `main`, либо ручной `workflow_dispatch` с указанием SHA.

Upstream Plane'овские workflow (`build-branch.yml`, `codeql.yml`, `pull-request-build-lint-api.yml`, ...) живут отдельно и ничего не знают про AI — мы их не модифицируем. Наш CI триггерится только на изменения, относящиеся к AI-слою, чтобы не дублировать сборку backend на каждый чужой коммит.

## Что делает CI (`planeai-ci.yml`)

| Job | Когда | Что |
|---|---|---|
| `lint` | всегда | `ruff check ai/ scripts/` (фиксированная версия `ruff==0.7.4`) |
| `smoke` | всегда | строит `plane-backend-ai`, поднимает полный стек (12 контейнеров + pgvector 0.8.2), ждёт `migrator → Exited 0`, прогоняет в боевой БД `verify_schema` (introspection) + `verify_acl` (13 ассертов прав), проверяет `extversion(vector) >= 0.8.2` |
| `publish` | только `push` в `main`/`preview` и при успехе `lint` + `smoke` | пушит образ в `ghcr.io/<owner>/plane-backend-ai` с двумя тегами: `<short-sha>` (для отката) и `<ref-name>` (`main`/`preview`) |

«Тестов реальным Postgres+pgvector» — да: smoke поднимает `pgvector/pgvector:0.8.2-pg15` как `plane-db`, прогоняет миграции Plane и наши management-команды на живой БД. Полноценный `pytest`-набор для приложения `ai` приедет в ТЗ 1.9 — текущий smoke его не заменяет, но закрывает DoD 0.9.

## Что делает CD (`planeai-deploy-staging.yml`)

- Триггер 1: `workflow_run` от `planeAI CI` на ветке `main` со статусом `success` → автодеплой свежего SHA.
- Триггер 2: `workflow_dispatch` с input'ом `sha` → ручной откат на любой ранее опубликованный SHA.

Действия по SSH на staging-хосте:

1. `docker pull ghcr.io/<owner>/plane-backend-ai:<sha>`.
2. `docker compose ... -f docker-compose.staging.yml up -d migrator` с экспортом `PLANE_AI_IMAGE=<тот же>`. `docker-compose.staging.yml` ссылается на `${PLANE_AI_IMAGE}` — миграции отрабатывают новым образом.
3. Ждём, пока `migrator` `Exited 0`. На фейл — экзит 1.
4. `up -d api worker beat-worker` — рестарт backend-сервисов на новый тег.
5. Smoke: `curl http://localhost/` должен ответить (HTTP-код печатается).

Никаких миграций руками, никаких `docker tag … :latest` — образ всегда называется по SHA.

## Откат (TZ 6.5)

```
gh workflow run planeai-deploy-staging.yml -f sha=<хороший-короткий-sha>
```

Прокатит тот же путь, что и автодеплой, но с произвольно выбранным тегом. Поскольку каждый зелёный коммит на `main` имеет персональный тег в GHCR, откат — это просто запуск workflow с другим SHA.

## Секреты

Все секреты хранятся в **GitHub Settings → Secrets and variables → Actions** (Environment: `staging`). В git их нет и быть не может.

| Секрет | Где используется | Зачем |
|---|---|---|
| `GITHUB_TOKEN` | автоматический, не нужно заводить | пуш в `ghcr.io` под аккаунтом репозитория |
| `STAGING_SSH_HOST` | `planeai-deploy-staging.yml` | хост staging-сервера |
| `STAGING_SSH_USER` | то же | юзер для SSH-деплоя (`deploy`, не root) |
| `STAGING_SSH_KEY` | то же | приватный ключ; публичный лежит в `~/.ssh/authorized_keys` на staging-хосте |
| `STAGING_DEPLOY_DIR` | то же | абсолютный путь на staging, где склонирован репо (содержит `deploy-local/`) |

Пока эти секреты не заведены, **CD workflow упадёт на шаге `Deploy via SSH`** — это намеренно, чтобы было видно: staging ещё не существует (ТЗ 0.10 — отдельный блокер).

## Бейдж в README

В корневом `README.md` добавлен бейдж статуса последней сборки на `main`:

```
[![planeAI CI](https://github.com/Redfordyk/planeAI/actions/workflows/planeai-ci.yml/badge.svg?branch=main)](https://github.com/Redfordyk/planeAI/actions/workflows/planeai-ci.yml)
```

## Локальная отладка (без CI)

Тот же набор шагов воспроизводится один-в-один на машине разработчика:

```powershell
docker build -f Dockerfile.ai -t plane-backend-ai:local .
cd deploy-local
copy .env.example .env  # подставить реальные значения у себя
docker compose -p plane-ce up -d
# дождаться migrator
docker compose -p plane-ce exec api python manage.py verify_schema
docker compose -p plane-ce exec api python manage.py verify_acl
```

Если smoke зелёный локально — `planeai-ci.yml` тоже будет зелёный на runner'е.

## Связи

- Опирается на ТЗ 0.5 (образ `plane-backend-ai`) и ТЗ 0.4 (pgvector 0.8.2 в `plane-db`).
- Использует ТЗ 0.2 (`verify_schema`) и ТЗ 0.6 (`verify_acl`) как «тесты».
- Cd часть зависит от ТЗ 0.10 (поднятый staging) — до тех пор работает только CI.
- Разблокирует ТЗ 6.5 (план отката).
