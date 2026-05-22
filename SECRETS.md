# Секреты и ротация (ТЗ 0.11)

## Что считается секретом в planeAI

| Секрет | Источник | Куда попадает | Чем грозит компрометация |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | проектный ключ под DPA (Anthropic) | `.env` хоста → env api/worker/beat-worker | произвольные расходы на чужой запрос; читаемые ответы из логов их аккаунта |
| `OPENAI_API_KEY` | то же (OpenAI) | то же | то же |
| `FIELD_ENCRYPTION_KEY` | генерация через `scripts/gen_encryption_key.py` | `.env` хоста; **необходим** для расшифровки `WorkspaceAIConfig.*` | без ключа все зашифрованные строки в БД нечитаемы. **Без ключа = потеря данных всех воркспейсовых AI-настроек.** |
| `SECRET_KEY` (Django) | `openssl rand -hex 32` или эквивалент | `.env` Plane | подделка сессий / CSRF-токенов |
| `LIVE_SERVER_SECRET_KEY` | то же | `.env` Plane | подделка WebSocket-токенов |
| `POSTGRES_PASSWORD`, `RABBITMQ_PASSWORD` | сильный пароль | `.env` Plane | доступ к БД / очереди |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | для встроенного MinIO или внешнего S3 | `.env` Plane | доступ к загруженным файлам |
| `STAGING_SSH_KEY` | приватный SSH-ключ | GitHub Secrets (Env: staging) | контроль над staging-сервером |
| `GITHUB_TOKEN` | автоматический | GitHub Actions runtime | scoped push в наш GHCR |

## Где хранится

| Окружение | Хранилище | Права |
|---|---|---|
| Разработка локально | `deploy-local/.env` (gitignored), Windows ACL / `chmod 600` | только разработчик |
| CI | GitHub Secrets (Repo Settings → Secrets and variables → Actions) | только `Maintainer+` могут добавлять/редактировать |
| Staging | `/home/deploy/planeAI/deploy-local/.env`, `chmod 600`, владелец `deploy` | юзер `deploy`; root не использовать для рантайма |
| Прод (будущее) | **Vault или SOPS** (см. ниже roadmap), плюс `.env` со ссылками на пути в Vault | dev-ops + on-call |

### Roadmap по хранилищам

- **Сейчас (прототип):** `.env` файл с `chmod 600`, gitignored. Достаточно для команды из 5 и одного staging-сервера.
- **На MVP-prod:** SOPS (https://github.com/getsops/sops) — шифруем `.env.prod` GPG-ключом, который доступен только on-call'ам. SOPS-файл коммитим, GPG-ключ — нет.
- **Долгосрочно:** HashiCorp Vault или 1Password Secrets Automation, если команда вырастет или появятся регуляторные требования.

Сейчас Vault overkill, SOPS — следующий разумный шаг при выходе в реальный прод. Решение зафиксировать перед ТЗ 6.x.

## Генерация секретов

### `FIELD_ENCRYPTION_KEY`

```powershell
python scripts\gen_encryption_key.py
```

Вывод — одна строка (44 байта urlsafe-base64). Скопировать в `.env` и в резервное хранилище (1Password / GPG-шифрованный текстовик у тимлида). **Никогда** не коммитить, не вставлять в чат, не отправлять по почте без шифрования.

### `SECRET_KEY` / `LIVE_SERVER_SECRET_KEY` (Django)

```bash
python -c "import secrets; print(secrets.token_urlsafe(50))"
# или
openssl rand -hex 32
```

### `POSTGRES_PASSWORD`, `RABBITMQ_PASSWORD`, AWS-ключи

Любой стойкий генератор: `openssl rand -base64 30` / 1Password / `pwgen 32 1`.

## Аудит — секретов нет в git и в логах

### git history

```bash
# Текущий tree
git ls-files | xargs -d '\n' grep -lE 'sk-ant-|sk-proj-|sk-[A-Za-z0-9]{20,}' 2>/dev/null
# Вся история
git log -p | grep -nE 'sk-ant-|sk-proj-|sk-[A-Za-z0-9]{20,}' || echo "clean"
```

Выполнено на 2026-05-22 — найден один матч: плейсхолдер `"sk-asddassdfasdefqsdfasd23das3dasdcasd"` в `apps/admin/.../ai/form.tsx` (поле подсказки в UI Plane upstream, не настоящий ключ). Реальных утечек нет.

### Логи

- `GUNICORN_WORKERS=1`, формат лога — стандартный access-log без тела запроса. Тело запросов в access-log Plane **не** попадает.
- В нашем `ai/` коде запрещено `print(api_key)` или `logger.info(f"key={key}")`. Делать только `logger.info("LLM call: model=%s tokens=%d", model, tokens)`.
- Если эксепшен из anthropic/openai SDK содержит ключ в текстовике — **обернуть в `except` и пробрасывать без `.args`**.

```bash
# Регулярная проверка лог-агрегата (когда появится)
grep -rE 'sk-ant-|sk-[A-Za-z0-9]{30,}' /var/log/plane/ && echo "LEAK" || echo "clean"
```

### URL-параметры

Категорически нет — все вызовы провайдеров идут через SDK (`anthropic.AsyncAnthropic(api_key=...)`, `openai.OpenAI(api_key=...)`), ключ передаётся в `Authorization`-header, не в query string. Подтверждено документацией обоих SDK.

## Ротация

Принцип: **если ключ когда-либо появился в git-истории, чате, тикете или скриншоте — он скомпрометирован, ротировать обязательно.** История git вечная, форсированный `--force-push` не удаляет копии у клонировавших.

### Anthropic / OpenAI

1. У провайдера выпустить новый ключ (Anthropic: console.anthropic.com → API Keys; OpenAI: platform.openai.com → API keys).
2. Старый ключ оставить активным временно — для бесшовности.
3. На каждом хосте обновить `.env`: `ANTHROPIC_API_KEY=<новый>`.
4. Перезапустить backend-сервисы (без пересборки образа):
   ```bash
   docker compose -p plane-staging up -d api worker beat-worker
   ```
   `migrator` перезапускать **не** нужно — он коротко-живущий.
5. Smoke: один тестовый запрос (Claude generate / OpenAI embedding) — должен пройти.
6. У провайдера **отозвать** старый ключ. **Только теперь** ротация завершена.
7. Записать в журнал инцидентов: дата, кто, причина (плановая / компрометация).

В коде нет hardcoded ключей — никакого деплоя не требуется, только `.env` + рестарт.

### `FIELD_ENCRYPTION_KEY`

Сложнее, потому что данные в БД зашифрованы старым ключом.

1. Сгенерировать новый ключ.
2. Перевести `django-encrypted-model-fields` в режим переключения ключей: переменная `FIELD_ENCRYPTION_KEYS` (список через запятую) — `новый,старый`. Библиотека пишет новым, читает любым из списка.
3. Прогнать management-команду re-encrypt (в ТЗ 1.3 заложим — пройтись по `WorkspaceAIConfig`, сохранить каждую запись заново → перешифруется новым ключом).
4. Убрать старый ключ из `FIELD_ENCRYPTION_KEYS`, оставить только новый.
5. Перезапустить.

### Django `SECRET_KEY`

Влечёт инвалидацию всех сессий пользователей и подписанных токенов. На staging — спокойно. На проде — анонс юзерам, потом замена в `.env` + рестарт.

### `POSTGRES_PASSWORD` / `RABBITMQ_PASSWORD`

1. Сменить в БД/MQ через их CLI (`ALTER USER plane WITH PASSWORD '...'` / `rabbitmqctl change_password ...`).
2. Обновить `.env` на всех хостах (backend нужно знать новый пароль).
3. Перезапустить api/worker/beat-worker (DB-pool пересоберётся).

## DoD (закрывается)

- [x] Ни одного реального секрета в git (проверено grep'ом на регулярки `sk-ant-|sk-proj-|sk-{20+}`; один false-positive — UI-плейсхолдер).
- [x] `.env` в `.gitignore`; добавлены `secrets/`, `*.pem`, `*.key` (см. [.gitignore](.gitignore)).
- [x] Есть `deploy-local/.env.example` с полями `ANTHROPIC_API_KEY=CHANGE_ME` / `OPENAI_API_KEY=CHANGE_ME` / `FIELD_ENCRYPTION_KEY=CHANGE_ME`.
- [x] Скрипт генерации `FIELD_ENCRYPTION_KEY`: [`scripts/gen_encryption_key.py`](scripts/gen_encryption_key.py).
- [x] Процедура ротации задокументирована (этот файл).
- [ ] Реальные значения для staging/прод — генерирует и кладёт Костя/Вова при провизионинге (вне репо).
- [ ] В коде проверить отсутствие `print(...key)` / `logger.*(...key)` — будет сделано ревью при первом мердже в `ai/` с реальными вызовами LLM (ТЗ 1.3).

## Связи

- Опирается на ТЗ 0.5 (в образе уже стоит `django-encrypted-model-fields==0.6.5`).
- Опирается на ТЗ 0.7 (DPA-ключи приходят оттуда).
- Разблокирует ТЗ 1.3 (LLM-абстракция использует ключи из `WorkspaceAIConfig`).
