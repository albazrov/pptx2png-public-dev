import aiohttp
import logging
import shutil
from pathlib import Path
from aiogram import Bot, types
from pptx import Presentation

# Импортируем ваш существующий движок рендеринга
import converter_engine

async def check_spelling(text_list: list) -> str:
    """
    Асинхронно проверяет список текстов слайдов через Яндекс.Спеллер API.
    
    :param text_list: Список строк с текстом каждого слайда
    :return: Форматированный Markdown-отчет об ошибках или пустая строка, если опечаток нет.
    """
    url = "https://speller.yandex.net/services/spellservice.json/checkText"
    report_lines = []
    
    async with aiohttp.ClientSession() as session:
        for idx, slide_text in enumerate(text_list, start=1):
            if not slide_text.strip():
                continue
                
            payload = {
                "text": slide_text,
                "options": 518  # Игнорировать URL, email и римские цифры
            }
            
            try:
                async with session.post(url, data=payload, timeout=5) as response:
                    if response.status == 200:
                        results = await response.json()
                        if results:
                            report_lines.append(f"📋 **Слайд №{idx}:**")
                            for error in results:
                                word = error.get("word")
                                s_list = error.get("s", [])
                                suggestions = f" ➔ возможно: `{', '.join(s_list)}`" if s_list else " (нет вариантов)"
                                report_lines.append(f"  • Опечатка в `{word}`{suggestions}")
            except Exception as e:
                logging.error(f"Ошибка Яндекс.Спеллера на слайде {idx}: {e}")
                
    if report_lines:
        header = "⚠️ **Внимание! На слайдах обнаружены возможные опечатки:**\n\n"
        return header + "\n".join(report_lines)
    return ""

def extract_text_from_pptx(file_path: str) -> list:
    """
    Извлекает весь текст из блоков презентации, группируя его по слайдам.
    
    :param file_path: Путь к локальному файлу .pptx
    :return: Список строк, где каждый элемент — слитный текст одного слайда
    """
    try:
        prs = Presentation(file_path)
        presentation_text = []
        
        for slide in prs.slides:
            slide_pieces = []
            for shape in slide.shapes:
                if hasattr(shape, "text") and shape.text.strip():
                    slide_pieces.append(shape.text)
            presentation_text.append(" ".join(slide_pieces))
            
        return presentation_text
    except Exception as e:
        logging.error(f"Ошибка извлечения текста из PPTX через python-pptx: {e}")
        return []

async def download_file_by_url(url: str, destination: Path, status_message: types.Message) -> bool:
    """ Скачивает презентацию по HTTP-ссылке напрямую в RAM-диск (SHM). """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=30) as response:
                if response.status != 200:
                    logging.error(f"Ошибка скачивания по ссылке. Статус: {response.status}")
                    return False
                
                with open(destination, "wb") as f:
                    f.write(await response.read())
                return True
    except Exception as e:
        logging.error(f"Исключение при скачивании по URL: {e}")
        return False

async def core_pipeline(downloaded_file_path: Path, status_message: types.Message, user_id: int, user_mgr):
    work_dir = downloaded_file_path.parent
    is_zip = downloaded_file_path.suffix.lower() == '.zip'
    cfg = user_mgr.get_user_config(user_id)

    try:
        if is_zip:
            await status_message.edit_text("📦 Распаковка ZIP...")
            pptx_path = converter_engine.extract_zip_if_needed(downloaded_file_path, work_dir)
            if not pptx_path:
                await status_message.edit_text("❌ Внутри ZIP не найдено .pptx.")
                return None, None
        else:
            pptx_path = downloaded_file_path

        await status_message.edit_text(f"⏳ Конвертация через LibreOffice в RAM...\n(Качество: {cfg['quality'].upper()})")
        
        clean_folder = not cfg["keep_pdf"]
        
        args = converter_engine.FakeArgs(
            quality=cfg["quality"],
            keep_pdf=cfg["keep_pdf"],
            dark_mode=(cfg.get("theme") == "dark"),  # ← Исправить
            zip_mode=True,
            clean=clean_folder,
            output_dir=str(work_dir)
        )
        
        # Вызов движка в фоновом пуле потоков
        await asyncio.to_thread(converter_engine.process_file_local, pptx_path, args)
        
        expected_zip = next(work_dir.glob("*.zip"), None) if not is_zip else [f for f in work_dir.glob("*.zip") if f != downloaded_file_path][0]
        
        final_pdf_path = None
        if cfg["keep_pdf"]:
            png_folder = next((d for d in work_dir.iterdir() if d.is_dir() and d.name.endswith("_output")), None)
            if png_folder:
                pdf_file = next(png_folder.glob("*.pdf"), None)
                if pdf_file:
                    final_pdf_path = work_dir / f"{pptx_path.stem}.pdf"
                    shutil.move(str(pdf_file), str(final_pdf_path))
                shutil.rmtree(png_folder)

        return expected_zip, final_pdf_path
    except Exception as e:
        logging.error(f"Критическая ошибка ядра: {e}")
        return None, None
