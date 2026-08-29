import sys
import os
import logging
import asyncio
import shutil
import configparser
import argparse
from pathlib import Path 

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.types import FSInputFile, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
import aiohttp 

# ИМПОРТ НАШИХ КАСТОМНЫХ МОДУЛЕЙ
import converter_engine
from user_manager import UserManager
from utils import extract_text_from_pptx, check_spelling
from handlers import router

# 1. Определение директории запуска скрипта и имени родительской папки (prod / test)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_NAME = os.path.basename(SCRIPT_DIR)

# 2. Настраиваем парсер аргументов командной строки
parser = argparse.ArgumentParser(description="PPTX2PNG Telegram Bot")
parser.add_argument("--log-dir", type=str, help="Путь к папке логов (по дефолту внутри SHM)")
parser.add_argument("--shm-dir", type=str, help="Путь к временной папке в RAM-диске")
args, unknown = parser.parse_known_args()

# 3. Инициализация конфигураций .ini относительно SCRIPT_DIR
config_path = Path(SCRIPT_DIR) / "config.ini"
settings_path = Path(SCRIPT_DIR) / "settings.ini"

config = configparser.ConfigParser()
settings_config = configparser.ConfigParser()

if not config_path.exists():
    sys.exit(f"❌ Ошибка: Файл секретов config.ini не найден по пути: {config_path}")
config.read(config_path, encoding='utf-8')

try:
    BOT_TOKEN = config.get("Telegram", "BOT_TOKEN").strip()
    ADMIN_ID = int(config.get("Telegram", "ADMIN_ID").strip())
except Exception as e:
    sys.exit(f"❌ Ошибка в config.ini: {e}")

if settings_path.exists():
    settings_config.read(settings_path, encoding='utf-8')

# 4. ПРИОРИТЕТ ПУТЕЙ ДЛЯ RAM-ДИСКА (SHM)
if args.shm_dir:
    SHM_DIR = Path(args.shm_dir)
else:
    try:
        BASE_SHM = settings_config.get("Paths", "shm_dir").strip()
        if not BASE_SHM:
            raise configparser.NoOptionError("shm_dir", "Paths")
        SHM_DIR = Path(BASE_SHM) / ENV_NAME
    except (configparser.NoSectionError, configparser.NoOptionError):
        SHM_DIR = Path("/dev/shm/pptx2png_tasks") / ENV_NAME

SHM_DIR.mkdir(parents=True, exist_ok=True)

# 5. ПРИОРИТЕТ ПУТЕЙ ДЛЯ ЛОГОВ
if args.log_dir:
    LOG_DIR = args.log_dir
else:
    try:
        BASE_LOG = settings_config.get("Paths", "log_dir").strip()
        if not BASE_LOG:
            raise configparser.NoOptionError("log_dir", "Paths")
        LOG_DIR = BASE_LOG
    except (configparser.NoSectionError, configparser.NoOptionError):
        LOG_DIR = os.path.join(str(SHM_DIR), "logs")

os.makedirs(LOG_DIR, exist_ok=True)

# Инициализация менеджера пользователей
user_mgr = UserManager(admin_id=ADMIN_ID, base_dir=Path(SCRIPT_DIR))

# 6. ГИБКАЯ НАСТРОЙКА ЛОГИРОВАНИЯ
log_formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
root_logger = logging.getLogger()
root_logger.setLevel(logging.DEBUG)

info_handler = logging.FileHandler(os.path.join(LOG_DIR, "bot.log"), encoding='utf-8')
info_handler.setLevel(logging.INFO)
info_handler.setFormatter(log_formatter)
root_logger.addHandler(info_handler)

debug_handler = logging.FileHandler(os.path.join(LOG_DIR, "debug.log"), encoding='utf-8')
debug_handler.setLevel(logging.DEBUG)
debug_handler.setFormatter(log_formatter)
root_logger.addHandler(debug_handler)

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
    
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text=q_std, callback_data="set_q_standard"),
        InlineKeyboardButton(text=q_2k, callback_data="set_q_2k"),
        InlineKeyboardButton(text=q_4k, callback_data="set_q_4k")
    )
    builder.row(InlineKeyboardButton(text=f"Возвращать PDF: {pdf_status}", callback_data="toggle_pdf"))
    return builder.as_markup()


# ==========================================
# ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ ДЛЯ ЭКРАНИРОВАНИЯ ТЕКСТА
# ==========================================

def escape_markdown(text: str) -> str:
    """
    Экранирует специальные символы Markdown V2 в тексте.
    Согласно документации Telegram:
    _ * [ ] ( ) ~ ` > # + - = | { } . !
    """
    special_chars = r'_*[]()~`>#+-=|{}.!'
    for char in special_chars:
        text = text.replace(char, f'\\{char}')
    return text


# ==========================================
# УЛУЧШЕННАЯ ФУНКЦИЯ ПРОВЕРКИ ДОСТУПА
# ==========================================

async def check_access_by_user(user: types.User, bot: Bot) -> bool:
    """
    Проверяет доступ для конкретного пользователя.
    Используется в callback-хендлерах, где доступен callback.from_user.
    
    :param user: Объект пользователя Telegram (callback.from_user)
    :param bot: Экземпляр бота для отправки уведомлений
    :return: True если доступ разрешён, иначе False
    """
    user_id = user.id
    
    # Проверяем, есть ли пользователь в белом списке
    if user_id in user_mgr.load_allowed_users():
        return True
    
    # Если нет — отправляем запрос администратору
    # ✅ Экранируем все поля для безопасного Markdown
    safe_full_name = escape_markdown(user.full_name or "без имени")
    safe_username = escape_markdown(f"@{user.username}") if user.username else "нет юзернейма"
    safe_user_id = str(user_id)  # Цифры не требуют экранирования
    
    admin_kb = InlineKeyboardBuilder()
    admin_kb.row(
        InlineKeyboardButton(text="✅ Разрешить", callback_data=f"adm_allow_{user_id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"adm_deny_{user_id}")
    )
    
    # ✅ Используем HTML вместо Markdown для надёжности (или экранированный Markdown)
    # Вариант 1: HTML (рекомендуется для пользовательских данных)
    try:
        await bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                f"🔔 <b>Запрос доступа!</b>\n\n"
                f"• <b>Имя:</b> <code>{user.full_name or 'без имени'}</code>\n"
                f"• <b>Юзернейм:</b> <code>@{user.username if user.username else 'нет'}</code>\n"
                f"• <b>ID:</b> <code>{user_id}</code>"
            ),
            parse_mode="HTML",  # ✅ HTML безопаснее для пользовательских данных
            reply_markup=admin_kb.as_markup()
        )
        return False
    except Exception as e:
        logging.error(f"Ошибка отправки запроса доступа админу: {e}")
        return False


async def check_access(message: types.Message) -> bool:
    """
    Проверяет доступ для пользователя, отправившего сообщение.
    Используется в обычных message-хендлерах.
    
    :param message: Объект сообщения от пользователя
    :return: True если доступ разрешён, иначе False
    """
    return await check_access_by_user(message.from_user, bot)


# ==========================================================
# ПРОПИСЫВАНИЕ ЗАВИСИМОСТЕЙ:
# ==========================================================
dp.workflow_data.update({
    "SHM_DIR": str(SHM_DIR),
    "user_mgr": user_mgr,
    "check_access": check_access,
    "check_access_by_user": check_access_by_user,
    "get_settings_keyboard": get_settings_keyboard,
    "bot": bot,
    "ADMIN_ID": ADMIN_ID
})

# 3. ПОДКЛЮЧАЕМ НАШ ВНЕШНИЙ РОУТЕР
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
        
