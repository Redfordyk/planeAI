# Plane schema — verified mapping (ТЗ 0.2)

Получено из работающего Plane CE 1.3.1 через `verify_schema` management-команду (`scripts/verify_schema.py`, сырой вывод — `scripts/schema_dump.txt`). На этот файл ссылаются все ТЗ спринта 1.

## TL;DR

- **Django app для всех бизнес-моделей: `plane.db` (app_label = `db`).** Допущение подтверждено.
- Все ключевые модели названы так же, как в нашем предположении (`Issue`, `IssueComment`, `Page`, `Project`, `Workspace`, `ProjectMember`, `WorkspaceMember`).
- Главное расхождение с допущением: 🚨 **`Page` НЕ имеет прямого FK на `Project`** — связка идёт через `db.ProjectPage` (M2M-through). Это влияет на индексацию страниц.
- `IssueComment` имеет ещё и связку `description: OneToOneField → db.Description` (коллаборативная система описаний); тексты в `comment_html` / `comment_stripped` / `comment_json`.

## Карта «предположение → реальность»

| Сущность (наше допущение) | Реальная модель | `app_label`.`Model` | `db_table` | Прямой FK на `Workspace` | Прямой FK на `Project` | Контент-поля |
|---|---|---|---|---|---|---|
| Задача | ✅ совпадает | `db.Issue` | `issues` | `workspace` | `project` | `name` (Char), `description_html` (Text), `description_stripped` (Text), `description_binary` (Binary), `description_json` (JSON) |
| Комментарий | ✅ совпадает (`IssueComment`, не `Comment`) | `db.IssueComment` | `issue_comments` | `workspace` | `project` | `comment_html` (Text), `comment_stripped` (Text), `comment_json` (JSON) |
| Страница (вики) | ⚠️ **отличается линковкой** | `db.Page` | `pages` | `workspace` | 🚨 **нет прямого FK** — M2M `projects` + through `db.ProjectPage` | `name` (Text), `description_html` (Text), `description_stripped` (Text), `description_binary` (Binary), `description_json` (JSON) |
| Проект | ✅ совпадает | `db.Project` | `projects` | `workspace` | — | `name` (Char), `description` (Text), `description_text` (JSON), `description_html` 🚩 (**JSON**, не Text) |
| Воркспейс | ✅ совпадает | `db.Workspace` | `workspaces` | (сам) | — | `name` (Char), `slug` (Slug) |
| Членство в проекте | ✅ совпадает | `db.ProjectMember` | `project_members` | `workspace` | `project` | `member` → `db.User`, `role` (PSI), `is_active` (Bool) |
| Членство в воркспейсе | ✅ (бонус — нам тоже понадобится) | `db.WorkspaceMember` | `workspace_members` | `workspace` | — | `member` → `db.User`, `role` (PSI), `is_active` (Bool) |

## Ключевые поля моделей-кандидатов

### `db.Issue` (table `issues`)
- `id: UUIDField` PK
- `workspace: FK → db.Workspace`, `project: FK → db.Project` — оба обязательны
- `parent: FK → db.Issue` (self) — иерархия задач
- `state: FK → db.State`, `type: FK → db.IssueType`
- `name: CharField`, `description_html: TextField`, `description_stripped: TextField`, `description_binary: BinaryField`, `description_json: JSONField`
- `priority: CharField`, `start_date / target_date / completed_at / archived_at`, `sequence_id: IntegerField`, `is_draft: BooleanField`
- `assignees: M2M → db.User`, `labels: M2M → db.Label`
- soft delete: `deleted_at: DateTimeField` (типично для всех моделей Plane через mixin)
- ✅ для RAG: индексировать `name + description_stripped` при `is_draft=False AND deleted_at IS NULL`.

### `db.IssueComment` (table `issue_comments`)
- `workspace: FK`, `project: FK`, `issue: FK → db.Issue`, `actor: FK → db.User`
- `comment_html: TextField`, `comment_stripped: TextField`, `comment_json: JSONField`
- `parent: FK → db.IssueComment` (треды), `access: CharField` (видимость)
- `description: OneToOneField → db.Description` — коллаборативная система (отдельная сущность хранит текущую/версионную версию)
- ✅ для RAG: индексировать `comment_stripped`.

### `db.Page` (table `pages`)
- `workspace: FK → db.Workspace` — **обязателен**
- 🚨 **`project: FK` отсутствует.** Связь со страниц проекта: M2M `projects → db.Project` через `db.ProjectPage` (промежуточная модель)
- `parent: FK → db.Page` (вложенные страницы), `owned_by: FK → db.User`
- `name: TextField`, `description_html: TextField`, `description_stripped: TextField`, `description_binary: BinaryField`, `description_json: JSONField`
- `access: PositiveSmallIntegerField` (приватность), `is_locked: BooleanField`, `is_global: BooleanField`, `archived_at: DateField`
- ⚠️ для RAG: workspace-уровень обязателен; project-фильтр нужно делать через `db.ProjectPage` (см. ниже). Также фильтровать `archived_at IS NULL AND deleted_at IS NULL`.

### `db.ProjectPage` (промежуточная для Page ↔ Project)
В дампе видна как `project_pages` (FK на обе стороны). Если нужно «страницы конкретного проекта»: `Page.objects.filter(projects=project)` или через `ProjectPage`. Для разграничения по проектам в индексации хранить **множество project_id** на чанке, а не один.

### `db.Project` (table `projects`)
- `workspace: FK → db.Workspace`
- `name: CharField`, `description: TextField`, `description_text: JSONField`, `description_html: 🚩 JSONField` (исторически не Text!)
- `identifier: CharField` (короткий префикс задач), `archived_at: DateTimeField`
- `default_assignee / project_lead: FK → db.User`
- `network: PositiveSmallIntegerField` (видимость: приватный/публичный)
- ✅ для `AIProjectSettings.exclude_from_ai` — хранить в нашей таблице `ai.AIProjectSettings(project_id, exclude_from_ai)`, не править `db.Project`.

### `db.Workspace` (table `workspaces`)
- `id: UUIDField`, `name: CharField`, `slug: SlugField`, `owner: FK → db.User`
- НЕ имеет FK на себя (root сущность).

### `db.ProjectMember` (table `project_members`)
- `workspace: FK`, `project: FK`, `member: FK → db.User`
- `role: PositiveSmallIntegerField` (роль внутри проекта — числовой код)
- `is_active: BooleanField` — фильтровать перед использованием в ACL
- ✅ ACL для AI: «доступ к проекту = `ProjectMember.objects.filter(member=user, project=p, is_active=True).exists()`». Уточнить значения `role` в коде Plane при реализации ACL-слоя.

### `db.WorkspaceMember` (table `workspace_members`)
- `workspace: FK`, `member: FK → db.User`, `role: PSI`, `is_active: BooleanField`
- ✅ верхний уровень изоляции — если пользователь не `WorkspaceMember(is_active=True)`, никаких чанков воркспейса он не видит.

## Дополнительные модели, про которые стоит знать

| Модель | `db_table` | Зачем знать |
|---|---|---|
| `db.DraftIssue` | `draft_issues` | Черновики задач. Не индексировать в RAG (нет смысла) |
| `db.IntakeIssue` | `intake_issues` | Входящие/триажные. Решить отдельно: индексировать или нет |
| `db.IssueVersion`, `db.IssueDescriptionVersion`, `db.PageVersion` | `*_versions` | История описаний/страниц. Для RAG берём только текущее значение, не версии |
| `db.Description`, `db.DescriptionVersion` | `descriptions`, `description_versions` | Новая коллаборативная подсистема, на которую ссылается `IssueComment.description`. Уточнить при реальной выборке текста |
| `db.User` | (в `db`) | Plane хранит пользователей в `plane.db`, не в стандартном `auth.User`. **Важно при FK / signals.** |

## Что это меняет для спринта 1

1. ✅ **Имена моделей хардкодить можно**, ссылаться через строки `"db.Issue"`, `"db.IssueComment"`, `"db.Page"`, `"db.Project"`, `"db.Workspace"`, `"db.ProjectMember"`, `"db.WorkspaceMember"`.
2. 🚨 **`DocumentChunk` для страниц должен поддерживать множественную привязку к проектам** (или `project_id NULL` + отдельная таблица связи), потому что одна `Page` может относиться к нескольким проектам через `ProjectPage`. Самый простой вариант: на чанке хранить `workspace_id` + `array project_ids` (или отдельный M2M `chunk_projects`). Прямая колонка `project_id NOT NULL` сломается на страницах.
3. ⚠️ **`Project.description_html` — это JSONField**, не TextField. При сборе текста проекта брать `description` (TextField) или `description_text` (JSONField) и не полагаться на имя `*_html`.
4. ✅ **Soft delete** во всех моделях через `deleted_at`. RAG-индексер должен фильтровать `deleted_at__isnull=True` и (для задач) `is_draft=False, archived_at__isnull=True`.
5. ✅ **ACL для AI-ретривала**:
   - workspace gate: `WorkspaceMember(user, workspace, is_active=True)`
   - project gate: `ProjectMember(user, project, is_active=True)`
   - page (без проекта): доступ по workspace + проверка `access` поля Page
6. ✅ **`Project.network`** определяет приватность проекта — учитывать наряду с нашим `AIProjectSettings.exclude_from_ai`.

## Источник данных

- Команда: [scripts/verify_schema.py](scripts/verify_schema.py)
- Сырой дамп: [scripts/schema_dump.txt](scripts/schema_dump.txt)
- Plane CE версия: `1.3.1` (см. [README-deploy.md](README-deploy.md))
- Дата получения: 2026-05-22

Перезапустить можно так:

```powershell
docker cp scripts\verify_schema.py plane-ce-api-1:/code/plane/db/management/commands/verify_schema.py
$out = docker compose -p plane-ce -f deploy-local\docker-compose.yml exec -T api python manage.py verify_schema
[IO.File]::WriteAllText("scripts\schema_dump.txt", ($out -join "`n"), [System.Text.UTF8Encoding]::new($false))
```
