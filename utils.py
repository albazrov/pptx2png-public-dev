import aiohttp
import logging
from pptx import Presentation  # Требуется: pip install python-pptx

async def check_spelling(text_list: list) -> str:
    """
    Асинхронно проверяет список текстов слайдов через Яндекс.Спеллер API.
    
    :param text_list: Список строк с текстом каждого слайда
    :return: Форматированный Markdown-отчет об ошибках или пустая строка, если опечаток нет.
    """
    url = "https://yandex.net"
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
