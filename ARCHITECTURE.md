Паспорт архитектуры проекта PPTX2PNG Bot

# Архитектура проекта PPTX2PNG Bot

## 1. Топология Окружений
* **Production** (`/app/pptx2png/prod`)
  * Приватный репозиторий, ветка `main`.
  * Боевой токен, живые пользователи.
* **Testing** (`/app/pptx2png/test`)
  * Приватный репозиторий, ветка `test`.
  * Тестовый токен, интеграционные проверки.
* **Development** (`dev`)
  * Публичный репозиторий `pptx2png-public-dev`.
  * Открытая песочница без секретов для работы ИИ-агентов.
  * **CI/CD не используется** — ручной перенос через `git merge`.

## 2. Изоляция и Пути в RAM
* Динамический контекст `ENV_NAME` из папки.
* Файлы и логи хранятся в `/dev/shm`.
* SD-карта Raspberry Pi защищена от износа.
* Базовый путь: `/dev/shm/pptx2png_tasks/{ENV_NAME}`.
* Логи разделены на `bot.log` и `debug.log`.

## 3. Секреты и Безопасность
* `.gitignore` жестко блокирует секретные файлы.
* Скрыты `config.ini`, `allowed_users.txt`, `user_settings.json`.
* База пользователей изолирована от `Path.cwd()`.
* Менеджер инициализируется строго от `SCRIPT_DIR`.

## 4. Компоненты Системы
* **`manage.sh`**
  * Управляет фоновыми процессами через `nohup`.
  * Использует Bash-массивы против пробелов.
* **`user_manager.py`**
  * Персистентное хранилище настроек на JSON.
  * Авто-каст ключей `str -> int` для `aiogram`.
* **`handlers.py`**
  * Все хендлеры команд и callback'ов.
  * Блокировка задач через `TaskLockManager`.
  * Проверка владельца задачи через `.owner` файл.
* **`converter_engine.py`**
  * Движок конвертации PPTX → PDF → PNG.
  * Использует LibreOffice и PyMuPDF.
* **`utils.py`**
  * Проверка орфографии через Яндекс.Спеллер.
  * Основной конвейер `core_pipeline`.
  * Скачивание по ссылкам.

## 5. Процесс развертывания

### Production и Testing (автоматический деплой)

Для окружений **Production** и **Testing** используется GitHub Actions Self-Hosted Runner на Raspberry Pi.

**Воркфлоу:** `.github/workflows/deploy.yml`

[Push в main/test] → [GitHub Actions] → [Self-Hosted Runner на Pi]
                          │
┌─────────────────────────┴─────────────────────────┐
▼ (ветка test)                                      ▼ (ветка main)

rsync кода в /test/                                 rsync кода в /prod/
./manage.sh stop
2. ./manage.sh restart
./manage.sh start
3. ./manage.sh status
Проверка статуса
Сбор логов → Артефакты
./manage.sh stop (тест)



**Особенности:**
- Раннер не пушит логи в Git (избегаем конфликтов).
- Логи выгружаются как Артефакты GitHub.
- Добавлена проверка статуса процесса.

### Development (ручной перенос)

Для **Development** (публичный репозиторий) CI/CD **не используется**, так как:
- Публичный репозиторий не содержит секретов (`config.ini` в `.gitignore`).
- Изменения проходят ручное ревью через Qodo.
- Перенос в test выполняется вручную по регламенту (см. раздел 6).

---

## 6. Регламент синхронизации кода между репозиториями

При переносе отлаженных фич из публичной среды разработки (dev) в боевую тестовую среду, используется следующий атомарный цикл команд в терминале Raspberry Pi:

```bash
# 1. Забираем изменения, подтвержденные ИИ-агентом из публичного репо
git checkout dev
git pull pptx2png-public-origin main

# 2. Переносим изменения в приватный тестовый контур
git checkout test
git pull origin test --no-rebase
git merge dev -m "merge: sync stable dev with test environment"

# 3. Пушим в приватную ветку, триггеря Runner на Raspberry Pi
git push origin test

# 4. Возвращаем папку в режим открытой разработки
git checkout dev

После пуша в test автоматически запускается GitHub Actions, который разворачивает изменения на тестовом окружении.

7. Требования для CI/CD (Production и Testing)

Для работы автоматического деплоя необходимы:

Self-Hosted Runner на Raspberry Pi:

# Установка runner
mkdir actions-runner && cd actions-runner
curl -O https://github.com/actions/runner/releases/latest/download/actions-runner-linux-arm64.tar.gz
tar xzf actions-runner-linux-arm64.tar.gz
./config.sh --url https://github.com/albazrov/pptx2png --token <TOKEN>
./svc.sh install
./svc.sh start

Настройка окружений в GitHub:

main → Production (/app/pptx2png/prod)
test → Testing (/app/pptx2png/test)
Файл .github/workflows/deploy.yml — см. пример в репозитории.

8. Структура каталогов

/app/pptx2png/
├── prod/                     # Production
│   ├── bot.py
│   ├── handlers.py
│   ├── ... (все файлы)
│   ├── config.ini            # Боевой токен
│   ├── allowed_users.txt
│   └── user_settings.json
├── test/                     # Testing
│   ├── bot.py
│   ├── handlers.py
│   ├── ... (все файлы)
│   ├── config.ini            # Тестовый токен
│   ├── allowed_users.txt
│   └── user_settings.json
└── dev/                      # Ссылка на test (для разработки)

/dev/shm/pptx2png_tasks/
├── prod/                     # RAM-диск PROD
│   └── logs/
│       ├── bot.log
│       ├── debug.log
│       └── sys_nohup.log
└── test/                     # RAM-диск TEST
    └── logs/
        ├── bot.log
        ├── debug.log
        └── sys_nohup.log

9. Безопасность

Защита от угроз:

Угроза	Защита
Directory Traversal	safe_filename() + validate_download_path()
Доступ к чужим задачам	Файл .owner с user_id:chat_id
Race conditions	TaskLockManager с блокировками
Markdown-инъекции	HTML с экранированием
Утечка памяти	Автоматическая очистка TaskLockManager
Переполнение RAM	Автоматическая очистка через cleanup_expired()
10. Мониторинг и логирование

Логи:

bot.log — INFO и выше (основные события)
debug.log — DEBUG и выше (детальная отладка)
sys_nohup.log — STDOUT процесса nohup
Просмотр логов:

./manage.sh logs        # Основной лог
./manage.sh debug-logs  # Дебаг лог

Статус бота:

./manage.sh status
# 🟢 Бот РАБОТАЕТ (PID: 12345) [prod]

11. Известные ограничения

Dev-окружение не имеет CI/CD — только ручной перенос.
Self-Hosted Runner должен быть всегда запущен на Pi.
LibreOffice должен быть установлен для конвертации.
RAM-диск ограничен объёмом памяти Pi (рекомендуется 4+ ГБ).
Яндекс.Спеллер требует доступа к интернету.



---

## 📝 Что было изменено

### 1. Добавлено пояснение про dev:
```markdown
* **Development** (`dev`)
  * ...
  * **CI/CD не используется** — ручной перенос через `git merge`.


2. Раздел 5 переименован и структурирован:

Production и Testing — автоматический деплой через GitHub Actions.
Development — ручной перенос, CI/CD не используется.
3. Добавлен раздел 7 "Требования для CI/CD":

Настройка Self-Hosted Runner.
Настройка окружений в GitHub.
4. Добавлены разделы:

Структура каталогов
Безопасность (таблица угроз)
Мониторинг и логирование
Известные ограничения
5. Обновлена диаграмма:

Теперь чётко видно, что dev не использует CI/CD.
