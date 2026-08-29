import os
import shutil
import logging
from pathlib import Path
from aiogram import Router, F, types, Bot
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardButton, FSInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder

# Импортируем ВСЕ утилиты и конвейер обработки из utils.py
from utils import extract_text_from_pptx, check_spelling, download_file_by_url, core_pipeline


# Инициализируем единый роутер для этого модуля
router = Router()

# ==========================================
# 1. АДМИНСКИЕ ХЕНДЛЕРЫ
# ==========================================
@router.callback_query(F.data.startswith("adm_"))
async def handle_admin_decision(callback: types.CallbackQuery, user_mgr, bot: Bot, ADMIN_ID: int):
    if callback.from_user.id != ADMIN_ID:
        return
    data = callback.data.split("_")
    action, target_id = data[1], int(data[2])

    if action == "allow":
        user_mgr.save_allowed_user(target_id)
        await callback.message.edit_text(f"✅ Доступ для `{target_id}` одобрен.")
        try: await bot.send_message(target_id, "🎉 Доступ одобрен! Нажмите /start.")
        except Exception: pass
    elif action == "deny":
        await callback.message.edit_text(f"❌ Запрос `{target_id}` отклонен.")
        try: await bot.send_message(target_id, "❌ Доступ отклонен.")
        except Exception: pass
    await callback.answer()

# ==========================================
# 2. КОМАНДА СТАРТ
# ==========================================
@router.message(CommandStart())
async def cmd_start(message: types.Message, check_access, get_settings_keyboard):
    if not await check_access(message): return
    await message.reply(
        "👋 Привет! Настройте параметры генерации:", 
        reply_markup=get_settings_keyboard(message.from_user.id))

# ==========================================
# 3. НАСТРОЙКИ (CALLBACK-КНОПКИ)
# ==========================================
@router.callback_query(F.data.startswith("set_q_"))
async def handle_quality_settings(callback: types.CallbackQuery, user_mgr, get_settings_keyboard):
    user_id = callback.from_user.id
    # Извлекаем выбранное качество из callback_data (standard, 2k, 4k)
    new_quality = callback.data.replace("set_q_", "")
    
    # Записываем новое качество в JSON-базу данных через UserManager
    user_mgr.update_user_config(user_id, "quality", new_quality)
    
    # Перерисовываем клавиатуру с актуальными галочками ✅
    try:
        await callback.message.edit_reply_markup(reply_markup=get_settings_keyboard(user_id))
        await callback.answer(f"Quality updated to: {new_quality.upper()}")
    except Exception as e:
        logging.error(f"Error updating quality keyboard: {e}")
        await callback.answer()

@router.callback_query(F.data == "toggle_pdf")
async def handle_toggle_pdf(callback: types.CallbackQuery, user_mgr, get_settings_keyboard):
    user_id = callback.from_user.id
    current_config = user_mgr.get_user_config(user_id)
    
    # Инвертируем текущий флаг отправки PDF (True -> False / False -> True)
    new_pdf_status = not current_config.get("keep_pdf", False)
    
    # Сохраняем измененный статус в JSON
    user_mgr.update_user_config(user_id, "keep_pdf", new_pdf_status)
    
    # Обновляем инлайн-кнопки
    try:
        await callback.message.edit_reply_markup(reply_markup=get_settings_keyboard(user_id))
        status_text = "Да (ZIP + PDF)" if new_pdf_status else "Нет (Только ZIP)"
        await callback.answer(f"PDF output: {status_text}")
    except Exception as e:
        logging.error(f"Error toggling PDF keyboard: {e}")
        await callback.answer()

# --- ОБРАБОТКА ИНТЕРАКТИВНОГО ВЫБОРА ПОЛЬЗОВАТЕЛЯ ---

@router.callback_query(F.data.startswith("chk_spell:"))
async def callback_run_speller(callback: types.CallbackQuery, bot: Bot, SHM_DIR: str):
    """ Пользователь выбрал: Проверить орфографию. """
    task_id = callback.data.split(":")[-1]
    task_dir = Path(SHM_DIR) / task_id
    
    # Ищем наш .pptx файл внутри временной папки в RAM
    pptx_path = next(task_dir.glob("*.pptx"), None)
    
    if not pptx_path:
        await callback.message.edit_text("❌ Срок действия сессии истек или файл не найден. Отправьте файл заново.")
        return
        
    await callback.message.edit_text("🔍 Извлекаю текст и отправляю в Яндекс.Спеллер...")
    
    # Извлекаем и проверяем (функции из utils.py)
    from utils import extract_text_from_pptx, check_spelling
    slides_text = extract_text_from_pptx(str(pptx_path))
    spelling_report = await check_spelling(slides_text)
    
    # Кнопка для запуска конвертации после отчета
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="⚙️ Всё равно конвертировать", callback_data=f"chk_conv:{task_id}"))
    
    if spelling_report:
        # Если ошибки есть, выводим отчет и кнопку принудительной конвертации
        await callback.message.edit_text(spelling_report, parse_mode="Markdown", reply_markup=kb.as_markup())
    else:
        # Если ошибок нет
        await callback.message.edit_text("✨ **Яндекс.Спеллер не нашёл опечаток!** Всё чисто.", parse_mode="Markdown", reply_markup=kb.as_markup())
    await callback.answer()


@router.callback_query(F.data.startswith("chk_conv:"))
async def callback_run_conversion(callback: types.CallbackQuery, bot: Bot, SHM_DIR: str, user_mgr):
    """ Пользователь выбрал: Конвертировать. """
    task_id = callback.data.split(":")[-1]
    task_dir = Path(SHM_DIR) / task_id
    user_id = callback.from_user.id
    
    pptx_path = next(task_dir.glob("*.pptx"), None)
    if not pptx_path:
        await callback.message.edit_text("❌ Файл не найден в оперативной памяти. Отправьте заново.")
        return
        
    await callback.message.edit_text("⚙️ Запускаю конвейер LibreOffice...")
    
    from utils import core_pipeline
    try:
        # Вызываем вашу оригинальную чистую функцию конвертации (которую вы прислали)
        expected_zip, final_pdf_path = await core_pipeline(pptx_path, callback.message, user_id)
        
        if expected_zip:
            await callback.message.edit_text("📤 Отправляю готовые файлы...")
            
            # Отправляем ZIP
            await bot.send_document(chat_id=user_id, document=types.FSInputFile(expected_zip))
            
            # Если пользователь просил PDF
            if final_pdf_path and final_pdf_path.exists():
                await bot.send_document(chat_id=user_id, document=types.FSInputFile(final_pdf_path))
                
            await callback.message.delete()  # Удаляем статусное сообщение
        else:
            await callback.message.edit_text("❌ Ошибка генерации файлов движком.")
            
    except Exception as e:
        logging.error(f"Ошибка в callback-конвертации: {e}")
        await callback.message.edit_text("❌ Произошла ошибка при конвертации.")
    finally:
        # Полностью очищаем оперативную память RAM-диска после отправки результатов
        if task_dir.exists():
            shutil.rmtree(task_dir)
    await callback.answer()


# ==========================================
# 4. ФАЙЛЫ И ДОКУМЕНТЫ (СТРОГИЙ ПОРЯДОК СВЕРХУ ВНИЗ)
# ==========================================

@router.message(F.document.file_name.endswith('.pptx'))
async def handle_pptx_document(message: types.Message, bot: Bot, SHM_DIR: str, check_access):
    if not await check_access(message):
        return

    document = message.document
    # Создаем имя папки на основе ID сообщения, чтобы связать сессию с callback_data
    task_id = f"td_{message.message_id}"
    task_dir = Path(SHM_DIR) / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    
    local_file_path = task_dir / document.file_name
    status_msg = await message.reply("⏳ Скачиваю презентацию в память...")
    
    try:
        # Скачиваем файл из Telegram напрямую в RAM-диск
        file_info = await bot.get_file(document.file_id)
        await bot.download_file(file_info.file_path, destination=local_file_path)
        
        # Строим инлайн-клавиатуру инфо-диалога
        kb = InlineKeyboardBuilder()
        # В callback_data зашиваем маркер действия и уникальный ID папки задачи
        kb.row(
            InlineKeyboardButton(text="🔍 Проверить ошибки", callback_data=f"chk_spell:{task_id}"),
            InlineKeyboardButton(text="⚙️ Конвертировать", callback_data=f"chk_conv:{task_id}")
        )
        
        await status_msg.edit_text(
            f"📄 **Файл '{document.file_name}' успешно загружен.**\n\n"
            "Желаете проверить текст слайдов на орфографические ошибки через Яндекс.Спеллер перед рендерингом?",
            parse_mode="Markdown",
            reply_markup=kb.as_markup()
        )
        
    except Exception as e:
        logging.error(f"Ошибка при загрузке файла: {e}")
        await status_msg.edit_text("❌ Произошла ошибка при загрузке файла.")
        if task_dir.exists():
            shutil.rmtree(task_dir)

# Б. ШИРОКИЙ ФИЛЬТР: Все остальные типы файлов (PDF, Картинки, Архивы)

@router.message(F.document)
async def handle_docs(message: types.Message, check_access):
    if not await check_access(message): return
    file_name = message.document.file_name
    if Path(file_name).suffix.lower() not in ['.pptx', '.ppt', '.zip']:
        await message.reply("❌ Неверный формат.")
        return

    user_id = message.from_user.id
    task_dir = SHM_DIR / f"task_{user_id}_{message.message_id}"
    task_dir.mkdir(exist_ok=True)
    status_message = await message.reply("📥 Загрузка файла в RAM...")
    try:
        download_path = task_dir / file_name
        await bot.download(file=message.document.file_id, destination=str(download_path))
        output_zip, output_pdf = await core_pipeline(download_path, status_message, user_id)
        
        if output_zip:
            await status_message.edit_text("📤 Отправка результатов...")
            await message.reply_document(document=FSInputFile(path=output_zip), caption="📦 ZIP с картинками готов!")
            if output_pdf:
                await message.reply_document(document=FSInputFile(path=output_pdf), caption="📄 PDF готов!")
            await status_message.delete()
            
            # # АВТОМАТИЧЕСКИЙ ВЫВОД КНОПОК ПОСЛЕ ОТПРАВКИ
            await message.answer("⚙️ **Настройки для следующей презентации:**", reply_markup=get_settings_keyboard(user_id))
        else:
            await status_message.edit_text("❌ Ошибка сборки файлов.")
    except Exception as e:
        if "file is too big" in str(e).lower() or "bad request" in str(e).lower():
            await status_message.edit_text("❌ Ошибка: Файл превышает лимит Telegram (20 МБ).\nИспользуйте отправку ссылкой Google Drive.")
        else:
            await status_message.edit_text(f"❌ Ошибка: {e}")
    finally:
        if task_dir.exists(): shutil.rmtree(task_dir)

# ==========================================
# 5. ТЕКСТОВЫЕ СООБЩЕНИЯ И ССЫЛКИ
# ==========================================
@router.message(F.text.contains("http://") | F.text.contains("https://"))
async def handle_links(message: types.Message, check_access):
    if not await check_access(message): return
    direct_url = converter_engine.convert_to_direct_download(message.text)
    
    user_id = message.from_user.id
    task_dir = SHM_DIR / f"task_{user_id}_{message.message_id}"
    task_dir.mkdir(exist_ok=True)
    status_message = await message.reply("🌐 Скачивание ссылки в RAM...")
    try:
        download_path = task_dir / "downloaded_presentation.pptx"
        if await download_file_by_url(direct_url, download_path, status_message):
            output_zip, output_pdf = await core_pipeline(download_path, status_message, user_id)
            if output_zip:
                await status_message.edit_text("📤 Отправка результатов...")
                await message.reply_document(document=FSInputFile(path=output_zip), caption="📦 ZIP готов!")
                if output_pdf:
                    await message.reply_document(document=FSInputFile(path=output_pdf), caption="📄 PDF готов!")
                await status_message.delete()
                
                # # АВТОМАТИЧЕСКИЙ ВЫВОД КНОПОК ПОСЛЕ ОТПРАВКИ
                await message.answer("⚙️ **Настройки для следующей презентации:**", reply_markup=get_settings_keyboard(user_id))
            else:
                await status_message.edit_text("❌ Ошибка конвертации по ссылке.")
    except Exception as e:
        await status_message.edit_text(f"❌ Ошибка ссылки: {e}")
    finally:
        if task_dir.exists(): shutil.rmtree(task_dir)

@router.message(F.text & ~F.text.contains("http://") & ~F.text.contains("https://"))
async def handle_any_text(message: types.Message, check_access):
    """Если пользователь пишет любой обычный текст (не ссылку), бот выводит актуальные настройки."""
    if not await check_access(message): 
        return
        
    user_id = message.from_user.id
    await message.reply(
        "⚙️ **Параметры генерации слайдов:**\n\n"
        "Настройте качество картинок и режим сохранения PDF перед отправкой презентации.", 
        reply_markup=get_settings_keyboard(user_id)
    )




