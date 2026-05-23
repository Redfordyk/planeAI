# GDPR-RELEASE — финальный чеклист соответствия (ТЗ 6.7)

> **Юридический gate перед открытием доступа команде.** Каждый из 8 пунктов TZ 6.7 должен быть `✅ DONE` с заполненным доказательством. Любой `✗` / `?` = no-go.
>
> **Ответственный — Илья (PM).** Подписывается лично, не делегируется. Подпись = «я лично проверил, доказательства приложены, могу обосновать решение перед юристом».
>
> Этот файл — runbook + журнал. Заполняется один раз перед релизом, дальше — read-only артефакт. История изменений = git log.

---

## 0. Контекст

| | |
|---|---|
| Дата подписи (planned) | `2026-XX-XX` |
| Релиз-кандидат (commit/tag) | `<git-sha>` |
| Воркспейсы, которым открывается доступ | `<list>` |
| Регион обработки | EU (Франкфурт) |
| Юристы (если консультировались) | `<имя, дата>` |

---

## 1. DPA с OpenAI и Anthropic ✅ / ✗

**Требование (TZ 0.7).** Data Processing Agreement с обоими провайдерами + сохранённое подтверждение.

| Провайдер | DPA подписан | Дата | Подтверждение | Ссылка на документ |
|---|---|---|---|---|
| OpenAI | [ ] да / [ ] нет | `YYYY-MM-DD` | `[email/screenshot]` | `legal/openai-dpa.pdf` |
| Anthropic | [ ] да / [ ] нет | `YYYY-MM-DD` | `[email/screenshot]` | `legal/anthropic-dpa.pdf` |

**Автоматическая проверка (на проде):**

```bash
docker compose -p plane-ce exec api python manage.py gdpr_check --check dpa
# → [✓] dpa  DPA acknowledged closed on 2026-07-07.
```

Env var `PLANEAI_DPA_CLOSED=YYYY-MM-DD` устанавливается **после** подписи обоих DPA (позднейшая из двух дат). Это операционное подтверждение.

**Доказательство:** `[приложить ссылки на скриншоты/копии DPA]`

---

## 2. Zero-retention / отказ от обучения ✅ / ✗

**Требование.** Контент команды не используется для обучения моделей и не хранится дольше необходимого.

| Провайдер | Статус по умолчанию | Что включено дополнительно |
|---|---|---|
| OpenAI | API-данные **не** идут в обучение по дефолту | [ ] Zero data retention запрошен и подтверждён (требует enterprise/business plan, письмо от support). Дата: `YYYY-MM-DD`. |
| Anthropic | API-данные **не** идут в обучение по дефолту | [ ] Письменное подтверждение от sales/support. Дата: `YYYY-MM-DD`. |

**Доказательство:** скриншоты настроек / письма от support — `[приложить]`.

---

## 3. Правовое основание ✅ / ✗

**Требование.** Указано, на каком основании по GDPR Art. 6 обрабатываются данные.

Выбрано (отметить одно):

- [ ] **Art. 6(1)(b)** — necessary for performance of contract (если planeAI как SaaS).
- [ ] **Art. 6(1)(f)** — legitimate interest + DPIA. **Текущее предположение команды.**
- [ ] **Art. 6(1)(a)** — explicit consent (opt-in на уровне воркспейса через `WorkspaceAIConfig.enabled`).

**Решение принято:** Art. 6(1)(f) + opt-in `WorkspaceAIConfig.enabled`. Воркспейсы без `enabled=True` не отправляют ничего в облако (TZ 1.7, TZ 6.5 kill switch).

**DPIA (если 6(1)(f)):** `[ссылка на документ]`

**Юрист подтвердил:** `[имя]`, `YYYY-MM-DD`

---

## 4. Приватные проекты ✅ / ✗

**Требование (TZ 3.4).** Конфиденциальные проекты (HR, юристы, финансы) помечены `AIProjectSettings.exclude_from_ai=True`, их чанки **отсутствуют** в индексе.

**Автоматическая проверка:**

```bash
docker compose -p plane-ce exec api python manage.py gdpr_check \
  --check private_clean
# → [✓] private_clean  all 3 excluded project(s) clean.
```

Список помеченных проектов и проверка:

| Project | identifier | `exclude_from_ai` | chunks в индексе |
|---|---|---|---|
| `<project-1>` | `HR-PRIVATE` | ✅ True | 0 |
| `<project-2>` | `LEGAL` | ✅ True | 0 |
| `<project-3>` | `FINANCE` | ✅ True | 0 |

**Если приватных проектов нет** — установить signoff env var:
```bash
PLANEAI_NO_PRIVATE_PROJECTS=<подписант>:<YYYY-MM-DD>
```
Это явное «мы посмотрели, их нет», а не молчаливое предположение.

**Retroactive cleanup (TZ 6.7):** при flip-е `exclude_from_ai=False → True` сигнал `_on_project_settings_saved` ([`ai/signals.py`](ai/signals.py)) автоматически удаляет существующие чанки. Регрессионно зафиксировано в `test_flipping_exclude_from_ai_purges_existing_chunks`.

**Доказательство:** скриншот вывода `gdpr_check --check private_clean`, JSON-режим в commit-сообщение release-pr.

---

## 5. Прозрачность — уведомление команды ✅ / ✗

**Требование.** Каждый член команды знает, что его задачи/комменты обрабатываются ИИ-провайдерами OpenAI / Anthropic.

- [ ] Разослано уведомление команде по шаблону [`docs/team-notice-ai-processing.md`](docs/team-notice-ai-processing.md).
- [ ] Дата рассылки: `YYYY-MM-DD`.
- [ ] Канал: `[email / Slack #all-hands / встреча]`.
- [ ] Получили подтверждение прочтения от: `[имена]` или `[весь список через Slack reaction count]`.
- [ ] Уведомление включает: список фичей, какие данные уходят, к каким провайдерам, как opt-out (`WorkspaceAIConfig.enabled=False` или флаг `exclude_from_ai` на проект), как пользоваться правом на удаление.

**Доказательство:** копия отправленного письма / Slack-пост, ссылка → `[URL]`.

---

## 6. Минимизация ✅ / ✗

**Требование.** Уходит только текст задач/комментариев/страниц. Никаких лишних PII.

| Что отправляется | Источник в Plane | Цель |
|---|---|---|
| `Issue.name` + `Issue.description_stripped` | `db.Issue` | эмбеддинг + RAG |
| `IssueComment.comment_stripped` | `db.IssueComment` | эмбеддинг + RAG |
| `Page.name` + `Page.description_stripped` | `db.Page` | эмбеддинг + RAG |

Что **не уходит:**

- Пароли, токены, секреты — не в полях Issue/Comment/Page по дизайну.
- Файловые аттачменты — out-of-scope (TZ 1.5).
- Email/имена авторов задач — могут оказаться в теле текста, **это известный риск**.
  - Митигация: команда уведомлена не вкладывать PII в тело задач (см. пункт 5).
  - Долгосрочная митигация: PII scrubber pass перед эмбеддингом — `[ ]` TZ для будущего спринта.

**Реестр трансферов:** [GDPR.md](GDPR.md) → "Реестр трансферов".

**Доказательство:** код в [`ai/chunking.py`](ai/chunking.py) и [`ai/search.py`](ai/search.py) — единственные пути, по которым контент уходит в провайдеров. Ревью кода: `[ревьюер, дата]`.

---

## 7. Право на удаление ✅ / ✗

**Требование.** Удаление задачи / комментария / страницы → чанки в `DocumentChunk` удаляются автоматически.

**Реализация:** signal handlers в [`ai/signals.py`](ai/signals.py):
- `_on_issue_saved` / `_on_issue_deleted` — soft-delete (Plane API) + hard-delete.
- `_on_comment_saved` / `_on_comment_deleted` — то же.
- `_on_page_saved` / `_on_page_deleted` — то же, плюс архивирование (`archived_at`).
- TZ 6.7: `_on_project_settings_saved` — flip `exclude_from_ai=True` → purge.

**Автоматическая проверка:**

```bash
docker compose -p plane-ce exec api python manage.py gdpr_check \
  --check deleted_clean
# → [✓] deleted_clean  no chunks reference soft-deleted sources.
```

**Регрессионные тесты:**
- `test_soft_delete_issue_purges_chunks` ([`ai/tests/test_gdpr.py`](ai/tests/test_gdpr.py))
- `test_flipping_exclude_from_ai_purges_existing_chunks` (тот же файл)
- `test_setting_exclude_false_does_not_reindex` (защищает обратное: declassify ≠ автоматический reindex)

**Доказательство:** вывод тестов в CI зелёный — `[ссылка на CI run]`.

---

## 8. Реестр обработки ✅ / ✗

**Требование.** Документ с перечислением: какие данные, куда, зачем, как долго (GDPR Art. 30).

- [ ] Реестр заполнен: [`docs/processing-register.md`](docs/processing-register.md).
- [ ] Включает все 4 столбца: data category, recipient, purpose, retention.
- [ ] Дата последнего обновления: `YYYY-MM-DD`.
- [ ] Согласовано с юристом / DPO: `[имя, дата]`.

---

## Сводная автоматическая проверка

Один проход — все техническиe пункты:

```bash
docker compose -p plane-ce exec api python manage.py gdpr_check \
  --workspace <prod-workspace-id>

# ИЛИ для всей инсталляции (multi-workspace):
docker compose -p plane-ce exec api python manage.py gdpr_check
```

Ожидаемый вывод — `ALL GREEN — safe to sign GDPR-RELEASE.md.`

JSON-режим для приклеивания в commit-сообщение релизного PR:

```bash
docker compose -p plane-ce exec api python manage.py gdpr_check --json
# → {"workspace_id": null, "results": [...5 checks...], "go": true}
```

---

## Подпись

> Эта секция заполняется в день релиза. Не подписывать «авансом».

```
DECISION:           [ ] GO    [ ] NO-GO
Date signed:        YYYY-MM-DD HH:MM TZ
Signed by:          Илья Х. (PM)        ___________________________
Witnessed by (QA):  Никита Ф.           ___________________________
Witnessed by (Eng): Костя/Вова          ___________________________

Open risks acknowledged (must NOT block, team aware):
  - PII в теле задачи: команда уведомлена не вкладывать; долгосрочно — scrubber pass.
  - <другое>

Open follow-ups for the next sprint:
  - PII scrubber pass перед эмбеддингом (TZ-TBD)
  - <другое>
```

При `NO-GO` — релиз откладывается. Список открытых пунктов с дедлайнами:

| # | Что не закрыто | Кто чинит | Дедлайн |
|---|---|---|---|
| | | | |

---

## DoD ТЗ 6.7 — статус

- [x] 8 пунктов чеклиста сформулированы и измеримы.
- [x] Технические инварианты автоматизируются через `gdpr_check` (5 checks).
- [x] Право на удаление имеет signal + регрессионные тесты.
- [x] Retroactive privacy flag (TZ 6.7 новое) — signal + тест.
- [x] Шаблон уведомления команды есть ([`docs/team-notice-ai-processing.md`](docs/team-notice-ai-processing.md)).
- [x] Шаблон реестра обработки есть ([`docs/processing-register.md`](docs/processing-register.md)).
- [ ] **Реальные подписи** — закрывается ТОЛЬКО в день релиза.

## Связи

- ТЗ 0.7 — основная DPA-страница: [GDPR.md](GDPR.md).
- ТЗ 3.4 — приватные проекты: [`AIProjectSettings`](ai/models.py).
- ТЗ 1.5 — индексация + delete_chunks: [`ai/tasks.py`](ai/tasks.py), [`ai/signals.py`](ai/signals.py).
- ТЗ 6.6 — приёмочный прогон (предшествует этому): [SPRINT-6-ACCEPTANCE.md](SPRINT-6-ACCEPTANCE.md).
- ТЗ 6.8 — релиз (следующий шаг): [tz/sprint-6/08-задача-6.8-релиз.md](tz/sprint-6/08-задача-6.8-релиз.md).
- Команды: `gdpr_check`, `acceptance_check`.
