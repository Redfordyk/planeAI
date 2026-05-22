# Staging-контур (ТЗ 0.10)

> ⚠️ **Этот документ — runbook для развёртывания staging**, который пока физически не существует. Сервер провизионит человек (Вова), и затем заполняются поля в `[квадратных скобках]`. Технический скелет — здесь.

## Назначение

Изолированная копия прода, на которой тестируются **разрушительные вещи**:

- bulk-операции AI на массовом наборе задач,
- ИИ-агенты с автономной записью,
- миграции БД на нетривиальном объёме,
- регрессы Plane на наших backend-патчах перед прод-выкатом.

На staging **никогда** не заливаются реальные данные команды Plane или клиентов — только синтетика. Поэтому утечка в staging ≠ инцидент.

## Архитектура и отличия от прода

Идентичность с прод-окружением — на уровне образов и конфигурации, кроме явно перечисленного ниже.

| Аспект | Прод | Staging |
|---|---|---|
| `docker-compose.yml` | `deploy-local/docker-compose.yml` (общий) | то же |
| Override | `deploy-local/docker-compose.prod.yml` _(появится в спринте 6)_ | `deploy-local/docker-compose.staging.yml` ✅ есть |
| Backend-образ | `ghcr.io/<owner>/plane-backend-ai:<release-sha>` | `ghcr.io/<owner>/plane-backend-ai:<latest-main-sha>` (автодеплой из CI) |
| `plane-db` | `pgvector/pgvector:0.8.2-pg15` | то же |
| Внешний HTTP | `80/443`, домен `[plane.example.com]` | `80/443`, домен `[staging.plane.example.com]` |
| API-ключи AI | прод-проектные ключи под DPA | **отдельные** staging-ключи с месячным лимитом `[$XX]` (см. [GDPR.md](GDPR.md)) |
| Бюджет токенов | `WorkspaceAIConfig.token_budget_monthly` (прод-значение) | заведомо низкий, чтобы упереться при разрушительных тестах |
| Данные | реальные | **только синтетика** (см. ниже) |
| Бэкапы | ежедневные `pg_dump`, ретеншн 30д | необязательно — данные синтетические |
| Доступ | команда Plane + клиенты | вся команда planeAI |

## Что нужно сделать (закрывает Вова)

### 1. Провижининг сервера

- [ ] Заказать VM в той же геозоне, что прод (EU/Франкфурт), параметры **>=** прода: `4 vCPU / 8 GB / 40 GB SSD`. Если можно меньше — `2 vCPU / 4 GB` ок, но bulk-тесты могут стать узким горлом.
- [ ] DNS: завести `staging.[домен]` → IP сервера.
- [ ] Открытые порты: `22` только с офис/VPN-IP, `80` и `443` публично.
- [ ] Установить Docker + Docker Compose свежей версии (см. [README-deploy.md](README-deploy.md) — те же команды, что для прода).
- [ ] Завести unix-юзера `deploy`, положить его публичный SSH-ключ (соответствующий приватному `STAGING_SSH_KEY` в GitHub Secrets).

### 2. Клонирование и конфиг

```bash
sudo -iu deploy
git clone https://github.com/Redfordyk/planeAI.git
cd planeAI/deploy-local
cp .env.example .env
chmod 600 .env
# Заполнить .env:
#   APP_DOMAIN=staging.[домен]
#   LISTEN_HTTP_PORT=80
#   LISTEN_HTTPS_PORT=443
#   SECRET_KEY=<openssl rand -hex 32>
#   LIVE_SERVER_SECRET_KEY=<openssl rand -hex 32>
#   FIELD_ENCRYPTION_KEY=<python scripts/gen_encryption_key.py>
#   ANTHROPIC_API_KEY=<staging-only ключ из DPA>
#   OPENAI_API_KEY=<staging-only ключ из DPA>
#   POSTGRES_PASSWORD=<сильный>
#   RABBITMQ_PASSWORD=<сильный>
#   AWS_ACCESS_KEY_ID=<сильный для встроенного MinIO>
#   AWS_SECRET_ACCESS_KEY=<сильный>
#   CORS_ALLOWED_ORIGINS=https://staging.[домен]
#   WEB_URL=https://staging.[домен]
#   CERT_EMAIL=<ops@домен>   # включает HTTPS через Let's Encrypt
```

### 3. Первый запуск (через staging-override, БЕЗ dev-override)

```bash
docker login ghcr.io -u <gh-user>   # или token-based
docker compose -p plane-staging \
  -f docker-compose.yml \
  -f docker-compose.staging.yml \
  up -d
# Дождаться migrator → Exited (0)
docker inspect -f '{{.State.Status}} {{.State.ExitCode}}' plane-staging-migrator-1
```

Важно: использовать `-f docker-compose.staging.yml`, а **не** `docker-compose.override.yml` (он dev-only).

### 4. Подключить как цель CI

В GitHub Settings → Secrets and variables → Actions → Environment `staging` завести 4 секрета (см. [CICD.md](CICD.md) → раздел «Секреты»). После этого `workflow_run` от `planeAI CI` будет триггерить автодеплой.

### 5. Синтетический датасет

- [ ] Скрипт `scripts/seed_staging.py` (ТЗ ещё не написано — заведём по факту). Должен через Plane API создать: 1 воркспейс, 5–10 проектов, по 50–500 задач в каждом, набор комментариев, страниц. Содержимое — Lorem ipsum + предметная лексика (issue tracking), без PII.
- [ ] Записать в `.env` отдельные тест-пользователей с известными ролями (admin/member/guest) для UI-проверок.

### 6. Сеть и изоляция

- [ ] Firewall: `plane-staging-plane-db-1` не должен быть доступен по 5432 извне.
- [ ] Compose-сеть `plane-staging_default` изолирована от прод-сети (`plane-prod_default`) — это автоматически, если контейнеры на разных хостах.
- [ ] В `.env` staging НЕТ доступа к прод-БД или прод-MinIO (нет соответствующих хостов / кредов).
- [ ] Регулярная проверка `grep -r prod` в `.env` — если что-то с продовым именем попало, очистить.

## Проверка готовности (DoD)

- [ ] Сервер поднят, доступен команде по `https://staging.[домен]`.
- [ ] `docker compose -p plane-staging ps` — 12 сервисов up, migrator `Exited 0`.
- [ ] `curl -fsS https://staging.[домен]/` → 200.
- [ ] CI секреты заведены, `gh workflow run planeai-deploy-staging.yml -f sha=<любой-зелёный-SHA>` отрабатывает успешно.
- [ ] Тест: на staging создан проект, в нём `>=10` синтетических задач.
- [ ] `verify_acl` прогнан на staging-инстансе — все 13 ассертов зелёные.
- [ ] Этот файл дополнен реальными значениями `[заполнить]`.

## Что нельзя делать на staging

- Заливать реальные данные команды или клиентов (GDPR-инцидент).
- Использовать прод-API-ключи Anthropic/OpenAI.
- Открывать SSH в публичный интернет (только VPN/whitelisted IP).
- Снимать ограничения месячного бюджета на ключах (тогда упрёмся в шапку расходов и не заметим).

## Связи

- Опирается на ТЗ 0.5 (образ), ТЗ 0.4 (pgvector), ТЗ 0.7 (отдельные DPA-ключи), ТЗ 0.11 (`FIELD_ENCRYPTION_KEY`).
- Используется в связке с ТЗ 0.9 (CD-таргет).
- Разблокирует ТЗ 4.6 и 5.8 (приёмки на staging).
