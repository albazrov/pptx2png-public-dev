import os
import shutil
import logging
import secrets
import asyncio
from pathlib import Path
from typing import Optional, Set
from aiogram import Router, F, types, Bot
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardButton, FSInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder

# Импортируем ВСЕ утилиты и конвейер обработки из utils.py
from utils import extract_text_from_pptx, check_spelling, download_file_by_url, core_pipeline

# ==========================================
# ГЛОБАЛЬНЫЙ МЕНЕДЖЕР БЛОКИРОВОК ЗАДАЧ
# ==========================================

class TaskLockManager:
    """
    Менеджер блокировок задач для предотвращения race conditions.
    """
    def __init__(self):
        # Хранит активные блокировки задач
        self._locks: dict[str, asyncio.Lock] = {}
        # Хранит состояния задач (idle, processing, completed, failed)
        self._states: dict[str, str] = {}
        # Хранит активные операции для каждой задачи
        self._active_operations: dict[str, Set[str]] = {}
    
    async def acquire(self, task_id: str, operation: str) -> bool:
        """
        Пытается захватить блокировку задачи для выполнения операции.
        Возвращает True если блокировка получена, иначе False.
        """
        # Создаём блокировку, если её нет
        if task_id not in self._locks:
            self._locks[task_id] = asyncio.Lock()
        
        # Пытаемся захватить блокировку (неблокирующий режим)
        lock = self._locks[task_id]
        acquired = await lock.acquire()
        
        if acquired:
            # Проверяем состояние задачи
            current_state = self._states.get(task_id, "idle")
            if current_state in ("processing", "completed"):
                lock.release()
                return False
            
            # Устанавливаем состояние и регистрируем операцию
            self._states[task_id] = "processing"
            if task_id not in self._active_operations:
                self._active_operations[task_id] = set()
            self._active_operations[task_id].add(operation)
            
            return True
        
        return False
    
    def release(self, task_id: str, operation: str):
        """
        Освобождает блокировку задачи после завершения операции.
        """
        if task_id in self._locks:
            lock = self._locks[task_id]
            
            # Удаляем операцию из активных
            if task_id in self._active_operations:
                self._active_operations[task_id].discard(operation)
                if not self._active_operations[task_id]:
                    # Если операций больше нет, сбрасываем состояние
                    self._states[task_id] = "idle"
            
            # Освобождаем блокировку
            if lock.locked():
                lock.release()
    
    def set_completed(self, task_id: str):
        """Отмечает задачу как завершённую."""
        self._states[task_id] = "completed"
        if task_id in self._active_operations:
            self._active_operations[task_id].clear()
    
    def is_processing(self, task_id: str) -> bool:
        """Проверяет, выполняется ли задача в данный момент."""
        return self._states.get(task_id) == "processing"


# Создаём глобальный экземпляр менеджера блокировок
task_lock_manager = TaskLockManager()


# Инициализируем единый роутер для этого модуля
router = Router()

# ==========================================
# 1. ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ ДЛЯ БЕЗОПАСНОЙ ОБРАБОТКИ ИМЕНИ ФАЙЛА
# ==========================================

def safe_filename(filename: str) -> str:
    """Очищает имя файла от опасных символов и путей."""
    import re
    safe_name = os.path.basename(filename)
    safe_name = re.sub(r'[^\w\s.-]', '', safe_name)
    safe_name = re.sub(r'\s+', ' ', safe_name)
    safe_name = safe_name.strip()
    
    if not safe_name:
        safe_name = f"file_{secrets.token_hex(4)}"
    
    if len(safe_name) > 100:
        name, ext = os.path.splitext(safe_name)
        safe_name = name[:90] + ext
    
    return safe_name


def validate_download_path(task_dir: Path, destination: Path) -> bool:
    """Проверяет, что путь назначения находится внутри task_dir."""
    try:
        resolved_dest = destination.resolve()
        resolved_task = task_dir.resolve()
        return resolved_dest.parent == resolved_task or resolved_dest.parent in resolved_task.parents
    except Exception:
        return False


# ==========================================
# 2. АДМИНСКИЕ ХЕНДЛЕРЫ
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
# 3. КОМАНДА СТАРТ
# ==========================================
@router.message(CommandStart())
async def cmd_start(message: types.Message, check_access, get_settings_keyboard):
    if not await check_access(message): return
    await message.reply(
        "👋 Привет! Настройте параметры генерации:", 
        reply_markup=get_settings_keyboard(message.from_user.id))


# ==========================================
# 4. НАСТРОЙКИ (CALLBACK-КНОПКИ)
# ==========================================
@router.callback_query(F.data.startswith("set_q_"))
async def handle_quality_settings(callback: types.CallbackQuery, user_mgr, get_settings_keyboard, check_access_by_user, bot: Bot):
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
# 5. ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ ДЛЯ ГЕНЕРАЦИИ УНИКАЛЬНОГО ID ЗАДАЧИ
# ==========================================

def generate_task_id(chat_id: int, user_id: int, message_id: int) -> str:
    suffix = secrets.token_hex(2)
    return f"task_{chat_id}_{user_id}_{message_id}_{suffix}"


# ==========================================
# 6. ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ ДЛЯ ОБНОВЛЕНИЯ КНОПОК
# ==========================================

def disable_task_buttons(task_id: str) -> InlineKeyboardBuilder:
    """Создаёт клавиатуру с отключёнными кнопками для задачи в процессе обработки."""
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(
            text="⏳ Обработка... (Пожалуйста, подождите)",
            callback_data=f"disabled_{task_id}"
        )
    )
    return kb


# ==========================================
# 7. ОБРАБОТКА ИНТЕРАКТИВНОГО ВЫБОРА ПОЛЬЗОВАТЕЛЯ
# ==========================================

async def _validate_task_ownership(callback: types.CallbackQuery, task_id: str, SHM_DIR: str) -> tuple:
    """Проверяет, что пользователь является владельцем задачи."""
    task_dir = Path(SHM_DIR) / task_id
    ownership_file = task_dir / ".owner"
    
    if not task_dir.exists():
        await callback.answer("❌ Срок действия сессии истек.", show_alert=True)
        return None, None
    
    if not ownership_file.exists():
        await callback.answer("❌ Данные задачи повреждены.", show_alert=True)
        return None, None
    
    try:
        owner_data = ownership_file.read_text().strip()
        owner_user_id, owner_chat_id = map(int, owner_data.split(":"))
    except (ValueError, IOError):
        await callback.answer("❌ Ошибка чтения данных задачи.", show_alert=True)
        return None, None
    
    if callback.from_user.id != owner_user_id:
        await callback.answer(
            "❌ Эта задача принадлежит другому пользователю. Отправьте свой файл.",
            show_alert=True
        )
        return None, None
    
    if callback.message.chat.id != owner_chat_id:
        await callback.answer(
            "❌ Эта задача была создана в другом чате.",
            show_alert=True
        )
        return None, None
    
    pptx_path = next(task_dir.glob("*.pptx"), None)
    if not pptx_path:
        await callback.answer("❌ Файл презентации не найден.", show_alert=True)
        return None, None
    
    return task_dir, pptx_path

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
    
    # ✅ Инициализируем переменные ДО try, чтобы они были доступны в finally
    task_dir = None
    task_id_for_cleanup = task_id  # Сохраняем для очистки блокировки
    
    # ✅ Пытаемся захватить блокировку
    if not await task_lock_manager.acquire(task_id, "conversion"):
        await callback.answer(
            "⏳ Задача уже обрабатывается. Пожалуйста, подождите.",
            show_alert=True
        )
        return
    
    try:
        # 3. Проверяем владельца задачи
        task_dir, pptx_path = await _validate_task_ownership(callback, task_id, SHM_DIR)
        if not task_dir or not pptx_path:
            return
        
        user_id = callback.from_user.id
        chat_id = callback.message.chat.id
        
        # Отключаем кнопки, чтобы предотвратить повторные клики
        disabled_kb = disable_task_buttons(task_id)
        await callback.message.edit_reply_markup(reply_markup=disabled_kb.as_markup())
        
        await callback.message.edit_text("⚙️ Запускаю конвейер LibreOffice...")
        
        # Отмечаем задачу как завершённую перед удалением
        task_lock_manager.set_completed(task_id)
        
        expected_zip, final_pdf_path = await core_pipeline(pptx_path, callback.message, user_id, user_mgr)
        
        if expected_zip and expected_zip.exists():
            await callback.message.edit_text("📤 Отправляю готовые файлы...")
            
            await bot.send_document(
                chat_id=chat_id,
                document=FSInputFile(expected_zip),
                caption=f"📦 ZIP с картинками готов! ({callback.from_user.full_name})"
            )
            
            if final_pdf_path and final_pdf_path.exists():
                await bot.send_document(
                    chat_id=chat_id,
                    document=FSInputFile(final_pdf_path),
                    caption="📄 PDF готов!"
                )
            
            await callback.message.delete()
        else:
            await callback.message.edit_text("❌ Ошибка генерации файлов движком.")
            
    except Exception as e:
        logging.error(f"Ошибка в callback_run_conversion: {e}", exc_info=True)
        await callback.message.edit_text("❌ Произошла ошибка при конвертации.")
    finally:
        # ✅ Безопасная очистка: проверяем, что task_dir был присвоен
        if task_dir is not None and task_dir.exists():
            shutil.rmtree(task_dir)
        
        # ✅ Всегда освобождаем блокировку
        task_lock_manager.release(task_id_for_cleanup, "conversion")
    
    await callback.answer()

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
    
    # Инициализируем переменные ДО try
    task_dir = None
    task_id_for_cleanup = task_id
    
    # Пытаемся захватить блокировку
    if not await task_lock_manager.acquire(task_id, "spelling"):
        await callback.answer(
            "⏳ Задача уже обрабатывается. Пожалуйста, подождите.",
            show_alert=True
        )
        return
    
    try:
        # 3. Проверяем владельца задачи
        task_dir, pptx_path = await _validate_task_ownership(callback, task_id, SHM_DIR)
        if not task_dir or not pptx_path:
            return
        
        # Отключаем кнопки, чтобы предотвратить повторные клики
        disabled_kb = disable_task_buttons(task_id)
        await callback.message.edit_reply_markup(reply_markup=disabled_kb.as_markup())
        
        await callback.message.edit_text("🔍 Извлекаю текст и отправляю в Яндекс.Спеллер...")
        
        # ✅ ВЫНОСИМ СИНХРОННУЮ ОПЕРАЦИЮ В ПОТОК
        # Это предотвращает блокировку event loop при парсинге больших презентаций
        from utils import extract_text_from_pptx, check_spelling
        
        # ✅ Запускаем extract_text_from_pptx в отдельном потоке
        # Это позволяет event loop продолжать обработку других запросов
        extract_success, slides_text = await asyncio.to_thread(
            extract_text_from_pptx, 
            str(pptx_path)
        )
        
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
            # Восстанавливаем кнопки (кроме удалённых задач)
            if task_dir is not None and task_dir.exists():
                kb = InlineKeyboardBuilder()
                kb.row(InlineKeyboardButton(
                    text="⚙️ Конвертировать",
                    callback_data=f"chk_conv:{task_id}"
                ))
                await callback.message.edit_reply_markup(reply_markup=kb.as_markup())
            await callback.answer()
            return
        
        # ✅ check_spelling уже асинхронная, её не нужно оборачивать
        check_success, spelling_result = await check_spelling(slides_text)
        
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(
            text="⚙️ Всё равно конвертировать",
            callback_data=f"chk_conv:{task_id}"
        ))
        
        if not check_success:
            await callback.message.edit_text(
                f"{spelling_result}\n\n"
                "Вы можете продолжить конвертацию без проверки орфографии:",
                parse_mode="Markdown",
                reply_markup=kb.as_markup()
            )
        else:
            await callback.message.edit_text(
                spelling_result,
                parse_mode="Markdown",
                reply_markup=kb.as_markup()
            )
        
        await callback.answer()
        
    except Exception as e:
        logging.error(f"Ошибка в callback_run_speller: {e}", exc_info=True)
        await callback.answer("❌ Произошла ошибка при проверке.", show_alert=True)
    finally:
        # Безопасная очистка
        if task_dir is not None and task_dir.exists():
            shutil.rmtree(task_dir)
        
        # Всегда освобождаем блокировку
        task_lock_manager.release(task_id_for_cleanup, "spelling")

# ==========================================
# 8. ФАЙЛЫ И ДОКУМЕНТЫ
# ==========================================

@router.message(F.document.file_name.endswith('.pptx'))
async def handle_pptx_document(message: types.Message, bot: Bot, SHM_DIR: str, check_access):
    if not await check_access(message):
        return

    document = message.document
    user_id = message.from_user.id
    chat_id = message.chat.id
    
    safe_file_name = safe_filename(document.file_name)
    if not safe_file_name.lower().endswith(('.pptx', '.ppt')):
        await message.reply("❌ Неверный формат файла. Отправьте PPTX или PPT.")
        return
    
    task_id = generate_task_id(chat_id, user_id, message.message_id)
    task_dir = Path(SHM_DIR) / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    
    ownership_file = task_dir / ".owner"
    ownership_file.write_text(f"{user_id}:{chat_id}")
    
    local_file_path = task_dir / safe_file_name
    if not validate_download_path(task_dir, local_file_path):
        await message.reply("❌ Ошибка безопасности: недопустимое имя файла.")
        logging.warning(f"Security: Path traversal attempt from user {user_id}: {document.file_name}")
        return
    
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
            f"📄 **Файл '{safe_file_name}' успешно загружен.**\n\n"
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
    
    safe_file_name = safe_filename(message.document.file_name)
    file_ext = Path(safe_file_name).suffix.lower()
    if file_ext not in ['.pptx', '.ppt', '.zip']:
        await message.reply("❌ Неверный формат. Отправьте PPTX, PPT или ZIP с презентацией.")
        return

    user_id = message.from_user.id
    chat_id = message.chat.id
    
    task_id = generate_task_id(chat_id, user_id, message.message_id)
    task_dir = Path(SHM_DIR) / task_id
    task_dir.mkdir(exist_ok=True)
    
    ownership_file = task_dir / ".owner"
    ownership_file.write_text(f"{user_id}:{chat_id}")
    
    download_path = task_dir / safe_file_name
    if not validate_download_path(task_dir, download_path):
        await message.reply("❌ Ошибка безопасности: недопустимое имя файла.")
        logging.warning(f"Security: Path traversal attempt from user {user_id}: {message.document.file_name}")
        return
    
    status_message = await message.reply("📥 Загрузка файла в RAM...")
    
    try:
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
# 9. ТЕКСТОВЫЕ СООБЩЕНИЯ И ССЫЛКИ
# ==========================================

@router.message(F.text.contains("http://") | F.text.contains("https://"))
async def handle_links(message: types.Message, bot: Bot, SHM_DIR: str, check_access, user_mgr, get_settings_keyboard):
    if not await check_access(message):
        return
    
    import converter_engine
    
    direct_url = converter_engine.convert_to_direct_download(message.text)
    
    user_id = message.from_user.id
    chat_id = message.chat.id
    
    task_id = generate_task_id(chat_id, user_id, message.message_id)
    task_dir = Path(SHM_DIR) / task_id
    task_dir.mkdir(exist_ok=True)
    
    ownership_file = task_dir / ".owner"
    ownership_file.write_text(f"{user_id}:{chat_id}")
    
    safe_file_name = "downloaded_presentation.pptx"
    download_path = task_dir / safe_file_name
    
    status_message = await message.reply("🌐 Скачивание ссылки в RAM...")
    
    try:
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
    if not await check_access(message): 
        return
        
    user_id = message.from_user.id
    await message.reply(
        "⚙️ **Параметры генерации слайдов:**\n\n"
        "Настройте качество картинок и режим сохранения PDF перед отправкой презентации.", 
        reply_markup=get_settings_keyboard(user_id)
    )
