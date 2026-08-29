import os
import shutil
import logging
import secrets  # Добавлен для генерации безопасных ID
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
async def handle_quality_settings(callback: types.CallbackQuery, user_mgr, get_settings_keyboard, check_access_by_user, bot: Bot):
    """Обработчик изменения качества изображений."""
    # Проверяем доступ для пользователя, который нажал кнопку
    if not await check_access_by_user(callback.from_user, bot):
        await callback.answer("❌ Доступ запрещен.", show_alert=True)
        return
    
    user_id = callback.from_user.id
    new_quality = callback.data.replace("set_q_", "")
    
    user_mgr.update_user_config(user_id, "quality", new_quality)
    
    try:
        await callback.message.edit_reply_markup(reply_markup=get_settings_keyboard(user_id))
        await callback.answer(f"Quality updated to: {new_quality.upper()}")
    except Exception as e:
        logging.error(f"Error updating quality keyboard: {e}")
        await callback.answer()


@router.callback_query(F.data == "toggle_pdf")
async def handle_toggle_pdf(callback: types.CallbackQuery, user_mgr, get_settings_keyboard, check_access_by_user, bot: Bot):
    """Обработчик переключения отправки PDF."""
    # Проверяем доступ для пользователя, который нажал кнопку
    if not await check_access_by_user(callback.from_user, bot):
        await callback.answer("❌ Доступ запрещен.", show_alert=True)
        return
    
    user_id = callback.from_user.id
    current_config = user_mgr.get_user_config(user_id)
    new_pdf_status = not current_config.get("keep_pdf", False)
    
    user_mgr.update_user_config(user_id, "keep_pdf", new_pdf_status)
    
    try:
        await callback.message.edit_reply_markup(reply_markup=get_settings_keyboard(user_id))
        status_text = "Да (ZIP + PDF)" if new_pdf_status else "Нет (Только ZIP)"
        await callback.answer(f"PDF output: {status_text}")
    except Exception as e:
        logging.error(f"Error toggling PDF keyboard: {e}")
        await callback.answer()


# ==========================================
# 4. ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ ДЛЯ ГЕНЕРАЦИИ УНИКАЛЬНОГО ID ЗАДАЧИ
# ==========================================

def generate_task_id(chat_id: int, user_id: int, message_id: int) -> str:
    """
    Генерирует уникальный идентификатор задачи на основе chat_id, user_id и message_id.
    Добавляет случайный суффикс для защиты от коллизий.
    
    :param chat_id: ID чата (уникален для каждого чата)
    :param user_id: ID пользователя (для дополнительной защиты)
    :param message_id: ID сообщения (уникален в пределах чата)
    :return: Уникальный строковый идентификатор задачи
    """
    # Используем все три компонента для максимальной уникальности
    # Добавляем 4-символьный случайный суффикс для защиты от коллизий
    suffix = secrets.token_hex(2)  # 4 символа (2 байта в hex)
    return f"task_{chat_id}_{user_id}_{message_id}_{suffix}"


# ==========================================
# 5. ОБРАБОТКА ИНТЕРАКТИВНОГО ВЫБОРА ПОЛЬЗОВАТЕЛЯ
# ==========================================

# --- ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ ДЛЯ ПРОВЕРКИ ВЛАДЕЛЬЦА ЗАДАЧИ ---
async def _validate_task_ownership(callback: types.CallbackQuery, task_id: str, SHM_DIR: str) -> tuple:
    """
    Проверяет, что пользователь является владельцем задачи.
    Возвращает (task_dir, pptx_path) или (None, None) если проверка не пройдена.
    """
    task_dir = Path(SHM_DIR) / task_id
    ownership_file = task_dir / ".owner"
    
    # Проверяем существование папки задачи
    if not task_dir.exists():
        await callback.message.edit_text("❌ Срок действия сессии истек. Отправьте файл заново.")
        await callback.answer()
        return None, None
    
    # Проверяем файл владельца
    if not ownership_file.exists():
        await callback.message.edit_text("❌ Данные задачи повреждены. Отправьте файл заново.")
        await callback.answer()
        return None, None
    
    # Читаем ID владельца (храним в формате "user_id:chat_id" для дополнительной проверки)
    try:
        owner_data = ownership_file.read_text().strip()
        owner_user_id, owner_chat_id = map(int, owner_data.split(":"))
    except (ValueError, IOError):
        await callback.message.edit_text("❌ Ошибка чтения данных задачи. Отправьте файл заново.")
        await callback.answer()
        return None, None
    
    # Проверяем, что текущий пользователь - владелец
    if callback.from_user.id != owner_user_id:
        await callback.message.edit_text("❌ Эта задача принадлежит другому пользователю. Отправьте свой файл.")
        await callback.answer()
        return None, None
    
    # Проверяем, что чат совпадает (дополнительная защита)
    if callback.message.chat.id != owner_chat_id:
        await callback.message.edit_text("❌ Эта задача была создана в другом чате.")
        await callback.answer()
        return None, None
    
    # Ищем PPTX файл
    pptx_path = next(task_dir.glob("*.pptx"), None)
    if not pptx_path:
        await callback.message.edit_text("❌ Файл презентации не найден. Отправьте файл заново.")
        await callback.answer()
        return None, None
    
    return task_dir, pptx_path


@router.callback_query(F.data.startswith("chk_spell:"))
async def callback_run_speller(callback: types.CallbackQuery, bot: Bot, SHM_DIR: str, check_access_by_user):
    """
    Пользователь выбрал: Проверить орфографию.
    """
    # 1. Проверяем доступ для пользователя, который нажал кнопку
    if not await check_access_by_user(callback.from_user, bot):
        await callback.answer("❌ Доступ запрещен.", show_alert=True)
        return
    
    # 2. Извлекаем ID задачи
    task_id = callback.data.split(":")[-1]
    
    # 3. Проверяем владельца задачи
    task_dir, pptx_path = await _validate_task_ownership(callback, task_id, SHM_DIR)
    if not task_dir or not pptx_path:
        return
    
    await callback.message.edit_text("🔍 Извлекаю текст и отправляю в Яндекс.Спеллер...")
    
    # 4. Извлекаем текст с проверкой успешности
    from utils import extract_text_from_pptx, check_spelling
    extract_success, slides_text = extract_text_from_pptx(str(pptx_path))
    
    # 5. Если извлечение не удалось
    if not extract_success:
        await callback.message.edit_text(
            "❌ **Не удалось извлечь текст из презентации.**\n\n"
            "Возможные причины:\n"
            "• Файл повреждён\n"
            "• Неподдерживаемый формат PPTX\n"
            "• Срок действия сессии истек\n\n"
            "Вы можете продолжить конвертацию без проверки орфографии:",
            parse_mode="Markdown"
        )
        # Показываем кнопку конвертации
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="⚙️ Конвертировать", callback_data=f"chk_conv:{task_id}"))
        await callback.message.edit_reply_markup(reply_markup=kb.as_markup())
        await callback.answer()
        return
    
    # 6. Проверяем орфографию (получаем статус и результат)
    check_success, spelling_result = await check_spelling(slides_text)
    
    # 7. Кнопка для запуска конвертации после отчёта
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="⚙️ Всё равно конвертировать", callback_data=f"chk_conv:{task_id}"))
    
    # 8. Обработка результатов проверки
    if not check_success:
        # Проверка не удалась (сетевая ошибка, таймаут и т.д.)
        await callback.message.edit_text(
            f"{spelling_result}\n\n"
            "Вы можете продолжить конвертацию без проверки орфографии:",
            parse_mode="Markdown",
            reply_markup=kb.as_markup()
        )
    else:
        # Проверка выполнена успешно
        await callback.message.edit_text(
            spelling_result,
            parse_mode="Markdown",
            reply_markup=kb.as_markup()
        )
    
    await callback.answer()


@router.callback_query(F.data.startswith("chk_conv:"))
async def callback_run_conversion(callback: types.CallbackQuery, bot: Bot, SHM_DIR: str, user_mgr, check_access_by_user):
    """
    Пользователь выбрал: Конвертировать.
    """
    # 1. Проверяем доступ для пользователя, который нажал кнопку
    if not await check_access_by_user(callback.from_user, bot):
        await callback.answer("❌ Доступ запрещен.", show_alert=True)
        return
    
    # 2. Извлекаем ID задачи
    task_id = callback.data.split(":")[-1]
    
    # 3. Проверяем владельца задачи
    task_dir, pptx_path = await _validate_task_ownership(callback, task_id, SHM_DIR)
    if not task_dir or not pptx_path:
        return
    
    user_id = callback.from_user.id
    chat_id = callback.message.chat.id  # ✅ Сохраняем ID чата, где была создана задача
    
    await callback.message.edit_text("⚙️ Запускаю конвейер LibreOffice...")
    
    try:
        # Вызываем конвейер конвертации
        expected_zip, final_pdf_path = await core_pipeline(pptx_path, callback.message, user_id, user_mgr)
        
        if expected_zip and expected_zip.exists():
            await callback.message.edit_text("📤 Отправляю готовые файлы...")
            
            # ✅ Отправляем файлы в ТОТ ЖЕ ЧАТ, где была создана задача
            # Это может быть как личный чат, так и группа
            await bot.send_document(
                chat_id=chat_id,  # ✅ Используем chat_id из callback
                document=FSInputFile(expected_zip),
                caption="📦 ZIP с картинками готов!"
            )
            
            # Если пользователь просил PDF
            if final_pdf_path and final_pdf_path.exists():
                await bot.send_document(
                    chat_id=chat_id,  # ✅ Используем chat_id из callback
                    document=FSInputFile(final_pdf_path),
                    caption="📄 PDF готов!"
                )
                
            # Удаляем статусное сообщение после отправки
            await callback.message.delete()
        else:
            await callback.message.edit_text("❌ Ошибка генерации файлов движком.")
            
    except Exception as e:
        logging.error(f"Ошибка в callback-конвертации: {e}", exc_info=True)
        await callback.message.edit_text("❌ Произошла ошибка при конвертации.")
    finally:
        if task_dir.exists():
            shutil.rmtree(task_dir)
    await callback.answer()


# ==========================================
# 6. ФАЙЛЫ И ДОКУМЕНТЫ
# ==========================================

@router.message(F.document.file_name.endswith('.pptx'))
async def handle_pptx_document(message: types.Message, bot: Bot, SHM_DIR: str, check_access):
    if not await check_access(message):
        return

    document = message.document
    user_id = message.from_user.id
    chat_id = message.chat.id
    
    # ✅ Используем уникальный идентификатор задачи
    task_id = generate_task_id(chat_id, user_id, message.message_id)
    task_dir = Path(SHM_DIR) / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    
    # Сохраняем ID владельца задачи (user_id:chat_id)
    ownership_file = task_dir / ".owner"
    ownership_file.write_text(f"{user_id}:{chat_id}")
    
    local_file_path = task_dir / document.file_name
    status_msg = await message.reply("⏳ Скачиваю презентацию в память...")
    
    try:
        file_info = await bot.get_file(document.file_id)
        await bot.download_file(file_info.file_path, destination=local_file_path)
        
        kb = InlineKeyboardBuilder()
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


@router.message(F.document)
async def handle_docs(message: types.Message, bot: Bot, SHM_DIR: str, check_access, user_mgr, get_settings_keyboard):
    if not await check_access(message): 
        return
    
    file_name = message.document.file_name
    file_ext = Path(file_name).suffix.lower()
    
    if file_ext not in ['.pptx', '.ppt', '.zip']:
        await message.reply("❌ Неверный формат. Отправьте PPTX или ZIP с презентацией.")
        return

    user_id = message.from_user.id
    chat_id = message.chat.id
    
    # ✅ Используем уникальный идентификатор задачи
    task_id = generate_task_id(chat_id, user_id, message.message_id)
    task_dir = Path(SHM_DIR) / task_id
    task_dir.mkdir(exist_ok=True)
    
    # Сохраняем владельца задачи (user_id:chat_id)
    ownership_file = task_dir / ".owner"
    ownership_file.write_text(f"{user_id}:{chat_id}")
    
    status_message = await message.reply("📥 Загрузка файла в RAM...")
    
    try:
        download_path = task_dir / file_name
        await bot.download(file=message.document.file_id, destination=str(download_path))
        
        if not download_path.exists() or download_path.stat().st_size == 0:
            await status_message.edit_text("❌ Ошибка загрузки файла. Попробуйте еще раз.")
            return
        
        output_zip, output_pdf = await core_pipeline(download_path, status_message, user_id, user_mgr)
        
        if output_zip and output_zip.exists():
            await status_message.edit_text("📤 Отправка результатов...")
            
            await message.reply_document(
                document=FSInputFile(path=output_zip), 
                caption="📦 ZIP с картинками готов!"
            )
            
            if output_pdf and output_pdf.exists():
                await message.reply_document(
                    document=FSInputFile(path=output_pdf), 
                    caption="📄 PDF готов!"
                )
            
            await status_message.delete()
            
            await message.answer(
                "⚙️ **Настройки для следующей презентации:**", 
                reply_markup=get_settings_keyboard(user_id)
            )
        else:
            await status_message.edit_text("❌ Ошибка сборки файлов. Попробуйте еще раз.")
            
    except Exception as e:
        error_msg = str(e).lower()
        if "file is too big" in error_msg or "bad request" in error_msg:
            await status_message.edit_text(
                "❌ Ошибка: Файл превышает лимит Telegram (20 МБ).\n"
                "Используйте отправку ссылкой Google Drive."
            )
        else:
            await status_message.edit_text(f"❌ Ошибка: {e}")
            logging.error(f"Ошибка в handle_docs: {e}", exc_info=True)
    finally:
        if task_dir.exists():
            shutil.rmtree(task_dir)


# ==========================================
# 7. ТЕКСТОВЫЕ СООБЩЕНИЯ И ССЫЛКИ
# ==========================================

@router.message(F.text.contains("http://") | F.text.contains("https://"))
async def handle_links(message: types.Message, bot: Bot, SHM_DIR: str, check_access, user_mgr, get_settings_keyboard):
    if not await check_access(message):
        return
    
    import converter_engine
    
    direct_url = converter_engine.convert_to_direct_download(message.text)
    
    user_id = message.from_user.id
    chat_id = message.chat.id
    
    # ✅ Используем уникальный идентификатор задачи
    task_id = generate_task_id(chat_id, user_id, message.message_id)
    task_dir = Path(SHM_DIR) / task_id
    task_dir.mkdir(exist_ok=True)
    
    # Сохраняем владельца задачи (user_id:chat_id)
    ownership_file = task_dir / ".owner"
    ownership_file.write_text(f"{user_id}:{chat_id}")
    
    status_message = await message.reply("🌐 Скачивание ссылки в RAM...")
    
    try:
        download_path = task_dir / "downloaded_presentation.pptx"
        if await download_file_by_url(direct_url, download_path, status_message):
            output_zip, output_pdf = await core_pipeline(download_path, status_message, user_id, user_mgr)
            if output_zip and output_zip.exists():
                await status_message.edit_text("📤 Отправка результатов...")
                await message.reply_document(
                    document=FSInputFile(path=output_zip), 
                    caption="📦 ZIP готов!"
                )
                if output_pdf and output_pdf.exists():
                    await message.reply_document(
                        document=FSInputFile(path=output_pdf), 
                        caption="📄 PDF готов!"
                    )
                await status_message.delete()
                
                await message.answer(
                    "⚙️ **Настройки для следующей презентации:**", 
                    reply_markup=get_settings_keyboard(user_id)
                )
            else:
                await status_message.edit_text("❌ Ошибка конвертации по ссылке.")
        else:
            await status_message.edit_text("❌ Не удалось скачать файл по ссылке. Проверьте доступность.")
    except Exception as e:
        await status_message.edit_text(f"❌ Ошибка ссылки: {e}")
        logging.error(f"Ошибка в handle_links: {e}", exc_info=True)
    finally:
        if task_dir.exists():
            shutil.rmtree(task_dir)


@router.message(F.text & ~F.text.contains("http://") & ~F.text.contains("https://"))
async def handle_any_text(message: types.Message, check_access, get_settings_keyboard):
    """Если пользователь пишет любой обычный текст (не ссылку), бот выводит актуальные настройки."""
    if not await check_access(message): 
        return
        
    user_id = message.from_user.id
    await message.reply(
        "⚙️ **Параметры генерации слайдов:**\n\n"
        "Настройте качество картинок и режим сохранения PDF перед отправкой презентации.", 
        reply_markup=get_settings_keyboard(user_id)
    )
