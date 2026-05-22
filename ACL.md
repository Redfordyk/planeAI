# ACL — модель прав Plane и наш фильтр (ТЗ 0.6)

Документ описывает, **какие роли Plane даёт чтение/запись/админ** и **как мы их используем в двух функциях** `ai/acl.py`. Ссылается на [SCHEMA.md](SCHEMA.md) (имена моделей и полей).

## Источник правды

- Enum ролей: `plane.utils.permissions.base.ROLE` в Plane v1.3.1:
  - `ADMIN = 20`
  - `MEMBER = 15`
  - `GUEST = 5`
- Хранение: `PositiveSmallIntegerField(choices=ROLE_CHOICES, default=5)` на:
  - `db.ProjectMember.role` (`project_members.role`)
  - `db.WorkspaceMember.role` (`workspace_members.role`)
- Источник: `apps/api/plane/db/models/project.py:21` и `workspace.py:19` (один и тот же набор значений, отдельные модули, но идентичные).

## Матрица ролей (что они дают)

«✅ = разрешено в апстриме Plane по умолчанию, проверено в `allow_permission` декораторах», «❌ = запрещено», «🟡 = частично, см. примечание».

| Действие | GUEST (5) | MEMBER (15) | ADMIN (20) | Заметки |
|---|---|---|---|---|
| Прочитать задачу / комментарий | ✅ | ✅ | ✅ | На `IssueListEndpoint`, `IssueDetailEndpoint` стоит `[ADMIN, MEMBER, GUEST]`. |
| Создать / редактировать задачу | ❌ | ✅ | ✅ | На write-эндпойнтах задач — `[ADMIN, MEMBER]`. **Это и есть граница записи, на которую опирается `filter_ids_by_acl`.** |
| Bulk-update задач (дат и т.п.) | ❌ | ✅ | ✅ | `IssueBulkUpdateDateEndpoint`: `[ADMIN, MEMBER]`. |
| Bulk-delete задач | ❌ | ❌ | ✅ | `BulkDeleteIssuesEndpoint`: только `[ADMIN]`. ИИ-bulk на удаление мы вообще не делаем — DoD ниже. |
| Удалить чужую задачу | ❌ | ❌ | ✅ | `IssueDetailEndpoint.delete`: `[ADMIN]` с эскейп-хатчем для автора (`creator=True`). |
| Управлять метками проекта | ❌ | ❌ | ✅ | `IssueLabelEndpoint` — все мутации `[ADMIN]`. |
| Прикрепить файл, оставить коммент | ✅ | ✅ | ✅ | Аттачменты — `[ADMIN, MEMBER, GUEST]`, удаление аттачмента — `[ADMIN]` + creator. |

### Эскейп-хатч workspace-админа

В декораторе `allow_permission` есть второй проход для проектных эндпойнтов: если у пользователя нет нужной роли в проекте, но он:

- `WorkspaceMember(workspace=W, role=ADMIN, is_active=True)`, **и**
- `ProjectMember(workspace=W, project=P, is_active=True)` любой роли (включая GUEST),

— то его пускают. Видно в `apps/api/plane/utils/permissions/base.py:50-67`.

Мы это поведение **зеркалим** в `filter_ids_by_acl`. Это значит:

- Воркспейс-админ, добавленный гостем в чужой проект, может писать в его задачи через наш bulk.
- Воркспейс-админ **без** какого-либо ProjectMember-row на проект — **нет**. Это намеренно: Plane так же не пустил бы.

## Наш контракт

Файл: [`ai/acl.py`](ai/acl.py). Две функции — единственные авторизованные точки выдачи прав для AI-фич.

### `allowed_projects(user, workspace_id) -> list[UUID]`

Семантика: «проекты, которые `user` может **читать** в воркспейсе `workspace_id`».

Реализация — единственный фильтр:

```python
ProjectMember.objects.filter(
    member=user,
    workspace_id=workspace_id,
    is_active=True,
    deleted_at__isnull=True,
).values_list("project_id", flat=True)
```

Любая активная `ProjectMember`-запись = доступ на чтение (даже GUEST). Soft-delete и `is_active=False` исключаются. Для анонимного / `None` — пустой список.

Использовать в **retrieval** (ТЗ 2.1): на каждый поиск отрезать chunks, чьи `project_id` не в этом списке. На уровне воркспейса фильтр уже наложен индексами `DocumentChunk.workspace_id`.

### `filter_ids_by_acl(work_item_ids, user) -> list[str]`

Семантика: «из списка `work_item_ids` оставить те, к которым у `user` есть право **записи**».

Реализация — два запроса:

1. **Прямой путь:** `Issue ∈ ids` где у user есть `ProjectMember(role IN (MEMBER, ADMIN), is_active, deleted_at IS NULL)` на проекте задачи.
2. **Эскейп-хатч:** для остатка из `ids` — пускаем те, чей workspace в списке воркспейсов, где user `WorkspaceMember(role=ADMIN, is_active, deleted_at IS NULL)`, **и** на проекте задачи есть **любая** активная `ProjectMember`-запись user (любая роль).

`UNION` двух результатов — финальный список. Анонимный/`None` user → `[]`. Пустой input → `[]`.

Не используем для удаления задач (см. матрицу) — для удаления нужен ADMIN-only путь, который мы не реализуем (ИИ-bulk на удаление в роадмапе нет).

## Smoke-проверка

Скрипт: [`scripts/verify_acl.py`](scripts/verify_acl.py) — management-команда, создаёт под транзакцией Workspace + Project + 5 пользователей с разными ролями + Issue, прогоняет 13 ассертов, откатывает транзакцию.

Запуск:

```powershell
docker cp ai\acl.py plane-ce-api-1:/code/ai/acl.py
docker cp scripts\verify_acl.py plane-ce-api-1:/code/plane/db/management/commands/verify_acl.py
docker compose -p plane-ce -f deploy-local\docker-compose.yml exec -T api python manage.py verify_acl
```

Покрытые кейсы (все зелёные на 2026-05-22):

- `allowed_projects` для админа / мембера / гостя / постороннего / анонимного.
- `filter_ids_by_acl` для админа / мембера / гостя / постороннего / анонимного.
- **Эскейп-хатч**: workspace-admin без проектной роли выше guest, но с активным PM-rows → разрешено.
- Деактивация (`is_active=False`) у бывшего мембера → запись отозвана.
- Пустой input → пустой output.

Эти кейсы — заготовка под полноценные тесты в `1.9` (юнит) и `2.7` (e2e на ретриве).

## Что мы НЕ покрываем (на след. итерации)

- **Проектная видимость `Project.network`** (private/public). Когда `network=public`, теоретически чтение должно идти и нечленам воркспейса. Сейчас игнорируем — для AI-индексации публичных проектов в нашем воркспейсе нет. Закроем в ТЗ 3.4 вместе с `exclude_from_ai`.
- **Page ACL.** `db.Page.access` (PSI) и `Page` без FK на Project (связь через `db.ProjectPage`, см. SCHEMA.md). Отдельная функция `allowed_pages(user, workspace_id)` появится в спринте 1, когда подключим страницы к RAG.
- **`ProjectPublicMember`.** Read-only анонимный публичный доступ к опубликованным проектам — в AI-флоу не используем.
- **IssueComment-creator hatch.** `creator=True` в `allow_permission` позволяет автору редактировать свой комментарий даже без роли. ИИ комментарии не редактирует (только читает / создаёт от своего лица), так что не зеркалим.

## Связи

- Опирается на [SCHEMA.md](SCHEMA.md) (имена моделей подтверждены).
- Разблокирует ТЗ 2.1 (ACL-фильтр на retrieval) и ТЗ 4.3 (ACL в bulk-операциях).
