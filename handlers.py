import os
import shutil
import logging
from pathlib import Path
from aiogram import Router, F, types, Bot
from aiogram.types import InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

# Импортируем утилиты проверки и базы данных
from utils import extract_text_from_pptx, check_spelling

# Создаем изолированный роутер для документов
router = Router()

@router.message(F.document.file_name.endswith('.pptx'))
async def handle_pptx_document(message: types.Message, bot: Bot, SHM_DIR: str, user_mgr, check_access):
    """
    Хендлер перехватывает файлы .pptx, извлекает текст, отправляет в Яндекс.Спеллер 
    и затем передает на конвертацию.
    """
    # 1. Проверяем доступ пользователя через переданную функцию
    if not await check_access(message):
        return

    document = message.document
    
    # Создаем изолированную временную подпапку для задачи внутри RAM-диска (SHM)
    task_dir = Path(SHM_DIR) / f"task_{message.message_id}"
    task_dir.mkdir(parents=True, exist_ok=True)
    
    local_file_path = task_dir / document.file_name
    status_msg = await message.reply("⏳ Скачиваю презентацию...")
    
    try:
        # 2. Скачиваем файл напрямую в RAM-диск
        file_info = await bot.get_file(document.file_id)
        await bot.download_file(file_info.file_path, destination=local_file_path)
        
        # 3. ЛИНГВИСТИЧЕСКИЙ АНАЛИЗ (Вызов из utils.py)
        await status_msg.edit_text("🔍 Извлекаю текст и проверяю орфографию через Яндекс.Спеллер...")
        
        # Парсим строки слайдов через python-pptx
        slides_text = extract_text_from_pptx(str(local_file_path))
        
        # Отправляем асинхронный JSON-запрос в API Яндекса
        spelling_report = await check_spelling(slides_text)
        
        # Если найдены опечатки — выводим отчет отдельным сообщением
        if spelling_report:
            await message.reply(spelling_report, parse_mode="Markdown")
        
        # 4. ДВИЖОК КОНВЕРТАЦИИ
        await status_msg.edit_text("⚙️ Запускаю конвертацию слайдов в PNG...")
        
        # Здесь будет вызываться ваш converter_engine
        # converter_engine.process(...)
        
        await status_msg.delete()
        
    except Exception as e:
        logging.error(f"Ошибка при обработке файла в роутере: {e}")
        await status_msg.edit_text("❌ Произошла ошибка при обработке файла.")
    finally:
        # Полностью очищаем оперативную память после завершения сессии
        if task_dir.exists():
            shutil.rmtree(task_dir)
