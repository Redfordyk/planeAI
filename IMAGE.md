# Образ backend с приложением `ai` (ТЗ 0.5)

## Решение

Берём **правильный путь**: собираем кастомный образ `plane-backend-ai:<tag>` поверх официального `makeplane/plane-backend`. Bind-mount как у прототипа не используется — он не воспроизводим и сломается на CI/CD (ТЗ 0.9).

Регистрация `ai` в `INSTALLED_APPS` — через **отдельный модуль настроек `plane.settings.production_ai`**, который наследует `plane.settings.production` и добавляет `"ai"` в конец списка. Активируется через переменную окружения `DJANGO_SETTINGS_MODULE=plane.settings.production_ai`. Это самый чистый из трёх рассмотренных способов — мы не патчим upstream-файлы Plane in place, а просто кладём свой сосед в `plane/settings/`.

Рассмотренные альтернативы и почему отвергнуты:

| Подход | Минус |
|---|---|
| `sed`-патч `plane/settings/common.py` в Dockerfile | при апгрейде Plane патч ломается; мерж конфликты в CI |
| `sitecustomize.py` + рантайм-инжект | хрупко, плохо отлаживается, скрывает источник правды |
| Bind-mount `ai/` + ручное добавление в `INSTALLED_APPS` через `local_settings` | у Plane нет `local_settings`-хука в `production.py`; пришлось бы патчить = тот же вариант 1 |

## Что лежит в репозитории

- [`Dockerfile.ai`](Dockerfile.ai) — `FROM makeplane/plane-backend:v1.3.1`, копирует `ai/` в `/code/ai/`, копирует settings-шим в `/code/plane/settings/production_ai.py`, ставит python-зависимости (`pgvector`, `openai`, `anthropic`, `tiktoken`, `django-encrypted-model-fields`).
- [`ai/__init__.py`](ai/__init__.py), [`ai/apps.py`](ai/apps.py), [`ai/models.py`](ai/models.py), [`ai/migrations/__init__.py`](ai/migrations/__init__.py) — минимальный скелет Django-app. Модели появятся в спринте 1.
- [`deploy-local/production_ai.py`](deploy-local/production_ai.py) — settings-шим, два значимых строчки:
  ```python
  from .production import *
  INSTALLED_APPS = list(INSTALLED_APPS) + ["ai"]
  ```
- [`deploy-local/docker-compose.override.yml`](deploy-local/docker-compose.override.yml) — заменяет образ у `api`, `worker`, `beat-worker` **и `migrator`** на `plane-backend-ai:local`, прокидывает `DJANGO_SETTINGS_MODULE=plane.settings.production_ai`. **Все четыре** backend-сервиса используют один образ — иначе фоновые задачи/сигналы `ai` не были бы видны воркерам и миграции бы выполнились без таблиц `ai`.

## Закреплённые версии

| Что | Версия | Где зафиксировано |
|---|---|---|
| База `makeplane/plane-backend` | `v1.3.1` | `Dockerfile.ai` |
| `pgvector` (PyPI, не Postgres-расширение) | `0.3.6` | `Dockerfile.ai` |
| `openai` | `1.54.4` | `Dockerfile.ai` |
| `anthropic` | `0.39.0` | `Dockerfile.ai` |
| `tiktoken` | `0.8.0` | `Dockerfile.ai` |
| `django-encrypted-model-fields` | `0.6.5` | `Dockerfile.ai` |

Не используем `:latest` нигде — апгрейд только осознанно с пересборкой и smoke-чеком.

## Как собрать и запустить

```powershell
# 1. Сборка образа
cd E:\Dev\planeAI
docker build -f Dockerfile.ai -t plane-backend-ai:local .

# 2. Подъём стека (compose автоматически подтянет override.yml из той же папки)
cd E:\Dev\planeAI\deploy-local
docker compose -p plane-ce up -d

# 3. Дождаться migrator
docker inspect -f '{{.State.Status}} {{.State.ExitCode}}' plane-ce-migrator-1
# → exited 0
```

## Smoke-проверка (DoD)

```powershell
$c = "E:\Dev\planeAI\deploy-local\docker-compose.yml"

# Django видит приложение `ai`
docker compose -p plane-ce -f $c exec -T api python manage.py showmigrations ai
# → ai
#    (no migrations)     ← ожидаемо, миграций ещё нет

# INSTALLED_APPS реально содержит 'ai' и используется наш settings-модуль
docker compose -p plane-ce -f $c exec -T api python -c \
  "import django; django.setup(); from django.conf import settings; print('ai' in settings.INSTALLED_APPS, settings.SETTINGS_MODULE)"
# → True plane.settings.production_ai

# Plane по-прежнему работает (регресс)
(Invoke-WebRequest -Uri http://localhost:8088 -UseBasicParsing).StatusCode
# → 200
```

Все три проверки прошли на момент 2026-05-22.

## Когда обновлять базовый образ Plane

Bump делается осознанно. Шаги:

1. Поставить новый тег в `Dockerfile.ai` (например, `v1.4.0`).
2. `docker pull makeplane/plane-backend:v1.4.0` — убедиться, что подтягивается.
3. `docker build -f Dockerfile.ai -t plane-backend-ai:local .` — пересобрать.
4. `docker compose -p plane-ce up -d` — поднять.
5. Прогнать smoke-проверки выше.
6. Если `SCHEMA.md` потерял актуальность — перезапустить `scripts/verify_schema.py` и обновить.
7. Закоммитить с пометкой о версии Plane.

## Связи

- Опирается на ТЗ 0.1 (известна версия Plane, известен `/code` workdir).
- Опирается на ТЗ 0.2 (известно, что app для моделей — `db`, не пересекается с нашим `ai`).
- Разблокирует ТЗ 1.1 (миграции / модели приложения `ai`) и 0.9 (CI/CD строит этот же образ).
