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

