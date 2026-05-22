# planeAI — навигатор по бэкенду Plane и точки интеграции `ai/` (ТЗ 0.12)

Документ — не учебник по Django. Это **карта реального репозитория** `apps/api` + памятка о dev-цикле + список точек, куда подключается наше приложение `ai/`. Пути и имена проверены против Plane CE v1.3.1 (см. [README-deploy.md](README-deploy.md)).

Если документ устарел после апгрейда Plane — перепрогнать [scripts/verify_schema.py](scripts/verify_schema.py) и пройтись `find apps/api/plane -name urls.py -o -name tasks.py` для свежей карты.

## 1. Карта backend-репозитория

```
apps/api/                              ← корень Django-проекта
├── manage.py                          DJANGO_SETTINGS_MODULE=plane.settings.production
└── plane/
    ├── settings/
    │   ├── common.py                  ← INSTALLED_APPS, DRF, кэш, логи, DB
    │   ├── production.py              ← наследует common, добавляет scout_apm
    │   ├── local.py / test.py         ← dev/тесты
    │   ├── storage.py                 ← S3/MinIO конфиг
    │   ├── redis.py / mongo.py        ← клиенты
    │   └── openapi.py                 ← drf-spectacular
    │
    ├── db/                            ← ВСЕ бизнес-модели Plane (см. SCHEMA.md)
    │   ├── models/
    │   │   ├── issue.py               Issue, IssueComment, IssueLabel, IssueReaction…
    │   │   ├── project.py             Project, ProjectMember, ROLE_CHOICES
    │   │   ├── workspace.py           Workspace, WorkspaceMember
    │   │   ├── page.py                Page, PageVersion, PageLabel
    │   │   ├── cycle.py / module.py / state.py / label.py …
    │   │   ├── user.py                кастомный User + единственный signal в проекте
    │   │   └── base.py                BaseModel (deleted_at, created_at, …)
    │   ├── migrations/                ← `db.NNNN_*` миграции (мы видели до 0010+ при тестах)
    │   └── management/commands/       ← наши verify_schema / verify_acl попадают сюда (docker cp)
    │
    ├── app/                           ← внутренний DRF API (mount /api/)
    │   ├── views/
    │   │   ├── issue/base.py          IssueListEndpoint, IssueDetailEndpoint, BulkDeleteIssuesEndpoint
    │   │   ├── issue/{label,attachment,comment,…}.py
    │   │   ├── workspace/             workspace endpoints
    │   │   ├── project/               project endpoints
    │   │   ├── page/                  page endpoints
    │   │   └── search.py              глобальный поиск (точка интеграции для AI-поиска!)
    │   ├── serializers/
    │   └── urls/                      по одному модулю на ресурс
    │       ├── issue.py / project.py / workspace.py / page.py …
    │       └── __init__.py            ← здесь собирается app.urls.urlpatterns
    │
    ├── api/                           ← внешний публичный API v1 (mount /api/v1/)
    │   ├── views/  serializers/  urls/
    │   └── rate_limit/                токен-бакет для публичного API
    │
    ├── space/                         ← публичные «space» страницы (mount /api/public/)
    │
    ├── authentication/                ← логин, OAuth, MFA (mount /auth/)
    │
    ├── web/                           ← server-side роут (mount /)
    │
    ├── bgtasks/                       ← ВСЕ Celery-задачи Plane
    │   ├── issue_activities_task.py   логгер активности
    │   ├── issue_version_sync.py      версии задач
    │   ├── page_transaction_task.py / page_version_task.py
    │   ├── notification_task.py / email_notification_task.py
    │   ├── file_asset_task.py / storage_metadata_task.py
    │   └── cleanup_task.py / deletion_task.py
    │
    ├── license/                       ← инстанс-уровень, лицензия, scout
    ├── middleware/                    ← наш SessionMiddleware и пр.
    ├── throttles/                     ← rate-limit классы
    ├── utils/
    │   └── permissions/base.py        ← ROLE enum + allow_permission decorator (см. ACL.md)
    │
    ├── celery.py                      ← `app = Celery("plane")`, beat_schedule
    ├── asgi.py                        ← ASGI entry (используется gunicorn + uvicorn worker)
    ├── wsgi.py                        ← WSGI entry (НЕ используется на проде, см. STREAMING.md)
    ├── urls.py                        ← корневой URLConf
    └── seeds/                         ← дамп фикстур
```

`INSTALLED_APPS` (`plane/settings/common.py`):

```
django.contrib.{auth, contenttypes, sessions, staticfiles}
plane.{analytics, app, space, bgtasks, db, utils, web, middleware, license, api, authentication}
rest_framework, corsheaders, django_celery_beat
```

В нашей надстройке к этому списку **добавляется `ai`** через settings-шим `plane.settings.production_ai` (см. [IMAGE.md](IMAGE.md)). Своих файлов в `apps/api/` мы НЕ редактируем.

## 2. Где что искать (быстрые рецепты)

| Хочу… | Иду в… |
|---|---|
| Найти модель по имени | `grep -rn "class Issue\b" apps/api/plane/db/models/` или [SCHEMA.md](SCHEMA.md) |
| Найти endpoint по URL `/api/workspaces/<slug>/projects/<id>/issues/` | `plane/app/urls/issue.py` → класс из `plane/app/views/issue/base.py` |
| Понять, как DRF проверяет права | декоратор `@allow_permission([ROLE.ADMIN, ROLE.MEMBER])` из `plane/utils/permissions/base.py` (см. [ACL.md](ACL.md)) |
| Зарегистрировать Celery-задачу | `plane/bgtasks/<name>.py` с `@shared_task`. Celery их подхватит автоматически (autodiscover) |
| Добавить периодическую задачу | расширить `beat_schedule` в `plane/celery.py` — но это патч upstream-файла, поэтому **у нас** регистрируем через `app.conf.beat_schedule.update(...)` в `ai/celery_schedule.py`, вызываемом из `ai/apps.py:ready()` |
| Загрузить файл (аттачмент, AI-артефакт) | `from django.core.files.storage import default_storage` — упирается в S3/MinIO, настройки в `plane/settings/storage.py` |
| Понять, какая Postgres-схема | `apps/api/plane/db/migrations/` — все миграции через app_label `db` (см. [SCHEMA.md](SCHEMA.md)) |
| Найти сигналы | в Plane сигналов почти нет (один `post_save` в `db/models/user.py`). Plane предпочитает явные вызовы Celery-задач из вьюх вместо сигналов. **Это влияет на наш дизайн** (см. §4). |

## 3. Локальный dev-цикл

Полностью описан в [README-deploy.md](README-deploy.md) и [IMAGE.md](IMAGE.md). Шпаргалка:

```powershell
# 1. Собрать наш backend-образ (нужно один раз и при правках Dockerfile.ai / ai/)
docker build -f Dockerfile.ai -t plane-backend-ai:local .

# 2. Поднять стек. ВАЖНО: запускать ИЗ deploy-local/, чтобы compose
#    подхватил docker-compose.override.yml (иначе уйдёт upstream-образ).
cd deploy-local
docker compose -p plane-ce up -d

# 3. Дождаться migrator → Exited 0
docker inspect -f '{{.State.Status}} {{.State.ExitCode}}' plane-ce-migrator-1
```

Прогон одной команды Django:

```powershell
docker compose -p plane-ce exec api python manage.py <command>
# примеры:
docker compose -p plane-ce exec api python manage.py showmigrations ai
docker compose -p plane-ce exec api python manage.py shell
```

Smoke-проверки (живая БД с pgvector):

```powershell
docker cp scripts/verify_schema.py plane-ce-api-1:/code/plane/db/management/commands/verify_schema.py
docker cp scripts/verify_acl.py    plane-ce-api-1:/code/plane/db/management/commands/verify_acl.py
docker compose -p plane-ce exec api python manage.py verify_schema  # карта моделей
docker compose -p plane-ce exec api python manage.py verify_acl     # 13 ACL-ассертов
```

После правки `ai/*.py` достаточно `docker compose -p plane-ce restart api worker beat-worker`, **без** пересборки образа — изменения подхватятся только если ai/ замаунчен в контейнер. У нас `ai/` запекается через `COPY ai/ /code/ai/` в `Dockerfile.ai`, поэтому для подхвата правок нужен либо `docker build` + recreate, либо bind-mount в dev-override (можно добавить отдельный `docker-compose.dev.yml`, если станет неудобно). На текущей итерации — пересборка.

Тесты:

- `pytest` для нашего `ai/` появится в ТЗ 1.9.
- На сейчас вместо pytest гоняем `verify_acl` (13 ассертов с реальной БД). Это и есть «тесты» в CI (см. [CICD.md](CICD.md)).

## 4. Точки интеграции `ai/` в Plane

Все точки **подключаются НЕ патчем upstream-файлов**, а через свои модули в `ai/`.

### 4.1 Регистрация приложения

| Что | Где |
|---|---|
| `INSTALLED_APPS += ["ai"]` | [`deploy-local/production_ai.py`](deploy-local/production_ai.py) — settings-шим. Активируется через `DJANGO_SETTINGS_MODULE=plane.settings.production_ai` ([compose override](deploy-local/docker-compose.override.yml)) |
| Сами файлы `ai/` | [`Dockerfile.ai`](Dockerfile.ai) — `COPY ai/ /code/ai/`. `/code` на `sys.path`, импортируется как `import ai` |
| Запекание в одном образе на api/worker/beat-worker/migrator | `plane-backend-ai:local` — иначе сигналы/задачи `ai` не были бы видны воркерам |

### 4.2 Модели и миграции (ТЗ 1.1 + 1.2)

- `ai/models.py` — `DocumentChunk`, `WorkspaceAIConfig`, `AIUsageLog`, `AIProjectSettings`.
- `ai/migrations/0001_initial.py` создаёт таблицы; `ai/migrations/000N_pgvector.py` добавляет `VECTOR(1536)` колонку + HNSW индекс (`CREATE EXTENSION IF NOT EXISTS vector` тоже здесь, RunSQL).
- Миграции применяются обычным `migrator`-контейнером Plane благодаря `INSTALLED_APPS` — отдельный шаг не нужен.

### 4.3 URL-эндпоинты

- Свой URL-модуль: `ai/urls.py` (создаётся в ТЗ 2.x).
- Подключение без правки `plane/urls.py`: в `production_ai.py` переопределяем `ROOT_URLCONF`:
  ```python
  # ai/_root_urls.py
  from plane.urls import urlpatterns as base
  from django.urls import include, path
  urlpatterns = base + [path("api/ai/", include("ai.urls"))]
  ```
  ```python
  # deploy-local/production_ai.py — добавить:
  ROOT_URLCONF = "ai._root_urls"
  ```
- Все наши эндпоинты живут под префиксом `/api/ai/...`.

### 4.4 Celery-задачи

- Свой модуль: `ai/tasks.py` с функциями `@shared_task`.
- Celery их подхватит автоматически (`app.autodiscover_tasks()` обходит `INSTALLED_APPS`).
- Периодические задачи (например, реиндекс чанков по cron) — регистрируем без правки `plane/celery.py`:
  ```python
  # ai/celery_schedule.py
  from celery import current_app
  from celery.schedules import crontab
  def register():
      current_app.conf.beat_schedule.setdefault("ai-reindex-dirty", {
          "task": "ai.tasks.reindex_dirty_chunks",
          "schedule": crontab(minute="*/5"),
      })
  ```
  ```python
  # ai/apps.py
  class AiConfig(AppConfig):
      def ready(self):
          from ai.celery_schedule import register
          register()
  ```

### 4.5 Сигналы / ингест из Plane (ТЗ 1.4)

Plane сигналов почти не использует, **но Django сигналы — стандартный механизм** и они работают. План:

- `ai/signals.py` подписывается на `post_save`/`post_delete` от `db.Issue`, `db.IssueComment`, `db.Page`.
- Подключается из `ai/apps.py:ready()`:
  ```python
  from django.db.models.signals import post_save
  from django.apps import apps
  Issue = apps.get_model("db", "Issue")
  post_save.connect(on_issue_saved, sender=Issue, dispatch_uid="ai.on_issue_saved")
  ```
- Обработчик не делает работу в синхронном пути — а enqueue Celery-задачу `ai.tasks.index_object(model_label, instance_id)`. Это важно: вьюхи Plane не должны замедляться из-за нашего ингеста.
- **Альтернатива** (если сигналы окажутся ненадёжны на каких-то путях, например `bulk_update`): встроиться в Plane'овские Celery-задачи `bgtasks/issue_activities_task.py` через doc-патч в Dockerfile или периодический реиндекс. Решение принимаем в ТЗ 1.4 по факту.

### 4.6 ACL для AI-запросов

- Единственная авторизованная точка прав — `ai/acl.py` (см. [ACL.md](ACL.md)). Все retrieval / bulk идут через неё.
- НИКОГДА не реализуем «свой» ACL по моделям Plane в обход `allowed_projects` / `filter_ids_by_acl`.

### 4.7 Файлы / артефакты

- Берём `default_storage` Django — он уже настроен на MinIO/S3 у Plane.
- Свой подкаталог: `ai/<workspace_uuid>/<project_uuid>/<artifact>.json` (точную схему ключей зафиксируем в ТЗ 5.x).

### 4.8 Логирование и метрики

- Использовать стандартный `logging` Plane: `logger = logging.getLogger("plane.ai")` — упадёт в общий console handler.
- В коде НИКОГДА `logger.*(api_key)` (см. [SECRETS.md](SECRETS.md)).
- Метрики кол-ва токенов / стоимости — пишем в `ai.AIUsageLog` (ТЗ 1.4), не в логи.

## 5. Чек-лист «куда писать новую фичу AI»

Сценарий: «добавить новый AI-эндпоинт `POST /api/ai/summarize`».

1. View → `ai/views/summarize.py`.
2. URL → `ai/urls.py` (`path("summarize/", SummarizeView.as_view())`).
3. Если нужны фоновые расчёты → `ai/tasks.py` (`@shared_task def summarize_issue(issue_id): ...`).
4. Если фича требует контекста по правам → импорт `from ai.acl import allowed_projects, filter_ids_by_acl`.
5. Если зовёт LLM → через единую `ai/llm/client.py` (появится в ТЗ 1.3) с учётом токенов в `AIUsageLog`.
6. Если меняет данные Plane → запись только через DRF-сериалайзеры Plane (не minуть ACL и аудит).

## 6. DoD ТЗ 0.12

- [x] Карта settings / моделей / вьюх / URL / Celery / сигналов / файлов составлена с реальными путями `apps/api/plane/...`.
- [x] Точки подключения `ai` явно перечислены: INSTALLED_APPS (settings shim), URL (ROOT_URLCONF override), Celery (autodiscover + beat update), сигналы (`apps.ready`), хранилище (`default_storage`).
- [x] Локальный dev-цикл описан: build → up из `deploy-local/` → ждать migrator → exec команды.
- [ ] Синк с командой 26.05 — отдельным мероприятием, после ревью этого документа.
- [x] Закоммичено.

## Связи

- Опирается на [SCHEMA.md](SCHEMA.md), [ACL.md](ACL.md), [IMAGE.md](IMAGE.md), [README-deploy.md](README-deploy.md), [STREAMING.md](STREAMING.md), [CICD.md](CICD.md).
- Разблокирует **весь спринт 1**: команда знает, в какие точки писать `ai`.
