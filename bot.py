import sys
import os
import logging
import asyncio
import shutil
import configparser
import argparse  # Соответствует примеру
from pathlib import Path 

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.types import FSInputFile, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
import aiohttp 

# Test CI/CD 3 

# ИМПОРТ НАШИХ КАСТОМНЫХ МОДУЛЕЙ
import converter_engine
from user_manager import UserManager
from utils import extract_text_from_pptx, check_spelling  # НАШ НОВЫЙ ИМПОРТ
from handlers import router

# 1. Определение директории запуска скрипта и имени родительской папки (prod / test)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_NAME = os.path.basename(SCRIPT_DIR)  # Получает имя папки, например 'pptx2png_prod' или 'pptx2png_test'

# 2. Настраиваем парсер аргументов командной строки (в точности как в примере)
parser = argparse.ArgumentParser(description="PPTX2PNG Telegram Bot")
parser.add_argument("--log-dir", type=str, help="Путь к папке логов (по дефолту внутри SHM)")
parser.add_argument("--shm-dir", type=str, help="Путь к временной папке в RAM-диске")
args, unknown = parser.parse_known_args()

# 3. Инициализация конфигураций .ini относительно SCRIPT_DIR
config_path = Path(SCRIPT_DIR) / "config.ini"
settings_path = Path(SCRIPT_DIR) / "settings.ini"

config = configparser.ConfigParser()
settings_config = configparser.ConfigParser()

# Проверка и чтение секретов
if not config_path.exists():
    sys.exit(f"❌ Ошибка: Файл секретов config.ini не найден по пути: {config_path}")
config.read(config_path, encoding='utf-8')

try:
    BOT_TOKEN = config.get("Telegram", "BOT_TOKEN").strip()
    ADMIN_ID = int(config.get("Telegram", "ADMIN_ID").strip())
except Exception as e:
    sys.exit(f"❌ Ошибка в config.ini: {e}")

# Чтение общих настроек (если файла нет, бот продолжит работу на дефолтах)
if settings_path.exists():
    settings_config.read(settings_path, encoding='utf-8')

# 4. ПРИОРИТЕТ ПУТЕЙ ДЛЯ RAM-ДИСКА (SHM) с проверкой на пустую строку (Решение бага Qodo)
if args.shm_dir:
    SHM_DIR = Path(args.shm_dir)
else:
    try:
        BASE_SHM = settings_config.get("Paths", "shm_dir").strip()
        # Если параметр объявлен в settings.ini, но оставлен пустым
        if not BASE_SHM:
            raise configparser.NoOptionError("shm_dir", "Paths")
        SHM_DIR = Path(BASE_SHM) / ENV_NAME
    except (configparser.NoSectionError, configparser.NoOptionError):
        # Гарантированный абсолютный путь по умолчанию в RAM
        SHM_DIR = Path("/dev/shm/pptx2png_tasks") / ENV_NAME

# Гарантируем наличие изолированной рабочей папки в RAM при старте
SHM_DIR.mkdir(parents=True, exist_ok=True)

# 5. ПРИОРИТЕТ ПУТЕЙ ДЛЯ ЛОГОВ (Синхронизировано с переданным аргументом из manage.sh)
if args.log_dir:
    LOG_DIR = args.log_dir
else:
    try:
        BASE_LOG = settings_config.get("Paths", "log_dir").strip()
        # Если параметр объявлен в settings.ini, но оставлен пустым
        if not BASE_LOG:
            raise configparser.NoOptionError("log_dir", "Paths")
        LOG_DIR = BASE_LOG
    except (configparser.NoSectionError, configparser.NoOptionError):
        # Автоматически создаем подпапку 'logs' внутри RAM-каталога текущего окружения
        LOG_DIR = os.path.join(str(SHM_DIR), "logs")

# Гарантируем наличие папки логов окружения в RAM при старте
os.makedirs(LOG_DIR, exist_ok=True)

# Инициализация менеджера пользователей с явной передачей базового пути (Решение бага Qodo с Whitelist)
user_mgr = UserManager(admin_id=ADMIN_ID, base_dir=Path(SCRIPT_DIR))

# 6. ГИБКАЯ НАСТРОЙКА ЛОГИРОВАНИЯ (Обычный лог, Дебаг лог в SHM + вывод в Консоль)
log_formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
root_logger = logging.getLogger()
root_logger.setLevel(logging.DEBUG)  # Общий уровень перехвата (отлавливаем всё)

# А. Основной файл логов окружения (Только INFO и выше) в RAM
info_handler = logging.FileHandler(os.path.join(LOG_DIR, "bot.log"), encoding='utf-8')
info_handler.setLevel(logging.INFO)
info_handler.setFormatter(log_formatter)
root_logger.addHandler(info_handler)

# Б. Детальный дебаг файл окружения (DEBUG и выше) в RAM
debug_handler = logging.FileHandler(os.path.join(LOG_DIR, "debug.log"), encoding='utf-8')
debug_handler.setLevel(logging.DEBUG)
debug_handler.setFormatter(log_formatter)
root_logger.addHandler(debug_handler)

# В. Вывод в консоль/терминал (Только INFO и выше) для Docker/Systemd
stdout_handler = logging.StreamHandler(sys.stdout)
stdout_handler.setLevel(logging.INFO)
stdout_handler.setFormatter(log_formatter)
root_logger.addHandler(stdout_handler)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


def get_settings_keyboard(user_id):
    cfg = user_mgr.get_user_config(user_id)
    q_std = "✅ Standard" if cfg["quality"] == "standard" else "Standard"
    q_2k  = "✅ 2K" if cfg["quality"] == "2k" else "2K"
    q_4k  = "✅ 4K" if cfg["quality"] == "4k" else "4K"
    pdf_status = "✅ Да (ZIP + PDF)" if cfg["keep_pdf"] else "❌ Нет (Только ZIP)"
    theme_original = "✅ Оригинал" if cfg.get("theme") == "original" else "Оригинал"
    theme_dark = "✅ Тёмная 🌙" if cfg.get("theme") == "dark" else "Тёмная 🌙"
    
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text=q_std, callback_data="set_q_standard"),
        InlineKeyboardButton(text=q_2k, callback_data="set_q_2k"),
        InlineKeyboardButton(text=q_4k, callback_data="set_q_4k")
    )
    builder.row(InlineKeyboardButton(text=f"Возвращать PDF: {pdf_status}", callback_data="toggle_pdf"))
    return builder.as_markup()

async def check_access(message: types.Message) -> bool:
    if message.from_user.id in user_mgr.load_allowed_users():
        return True

    username = f"@{message.from_user.username}" if message.from_user.username else "нет юзернейма"
    admin_kb = InlineKeyboardBuilder()
    admin_kb.row(
        InlineKeyboardButton(text="✅ Разрешить", callback_data=f"adm_allow_{message.from_user.id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"adm_deny_{message.from_user.id}")
    )
    try:
        await bot.send_message(
            chat_id=ADMIN_ID,
            text=f"🔔 **Запрос доступа!**\n\n• {message.from_user.full_name}\n• {username}\n• ID: `{message.from_user.id}`",
            parse_mode="Markdown", reply_markup=admin_kb.as_markup()
        )
        await message.reply("🔒 Доступ ограничен. Администратору отправлен запрос.")
    except Exception:
        await message.reply("🔒 Доступ ограничен. Ошибка уведомления админа.")
    return False

# ==========================================================
# ПРОПИСЫВАНИЕ ЗАВИСИМОСТЕЙ:
# ==========================================================
dp.workflow_data.update({
    "SHM_DIR": str(SHM_DIR),
    "user_mgr": user_mgr,
    "check_access": check_access,
    "get_settings_keyboard": get_settings_keyboard,
    "bot": bot,  # Передаем сам объект бота для отправки файлов и работы админки
    "ADMIN_ID": ADMIN_ID
})

# 3. ПОДКЛЮЧАЕМ НАШ ВНЕШНИЙ РОУТЕР (строго после workflow_data.update!)
dp.include_router(router)

async def main():
    """Основная функция запуска Telegram-бота."""
    logging.info("Модульный бот запущен на /dev/shm...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try: 
        asyncio.run(main())
    except KeyboardInterrupt: 
        logging.info("Бот остановлен.")


