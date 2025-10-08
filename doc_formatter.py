import io
from docx import Document
from docx.shared import Mm, Pt, Cm
from docx.enum.text import WD_ALIGN_PARAGRAPH


def format_docx(file_bytes: bytes) -> bytes:
    """
    Применяет заданные правила форматирования к документу .docx.
    Принимает байты исходного файла и возвращает байты отформатированного.
    """
    # Загружаем документ из байтов в память
    source_stream = io.BytesIO(file_bytes)
    document = Document(source_stream)

    # --- 1. Настройка полей страницы ---
    # В документе может быть несколько секций, применяем ко всем
    for section in document.sections:
        section.left_margin = Mm(30)
        section.right_margin = Mm(10)
        section.top_margin = Mm(20)
        section.bottom_margin = Mm(20)

    # --- 2. Форматирование абзацев и текста ---
    for paragraph in document.paragraphs:
        # Настройка форматирования абзаца
        p_format = paragraph.paragraph_format
        p_format.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        p_format.first_line_indent = Cm(1.25)
        p_format.line_spacing = 1.5

        # Настройка форматирования шрифта для каждого участка текста в абзаце
        for run in paragraph.runs:
            font = run.font
            font.name = 'Times New Roman'
            font.size = Pt(14)

    # --- 3. Сохранение результата в память ---
    target_stream = io.BytesIO()
    document.save(target_stream)
    # Возвращаем байты готового файла
    return target_stream.getvalue()