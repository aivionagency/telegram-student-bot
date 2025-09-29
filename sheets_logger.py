# sheets_logger.py
import gspread
import logging
from datetime import datetime
import config

# Настройка логгирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)

try:
    # Авторизация с помощью JSON-ключа сервисного аккаунта
    gc = gspread.service_account(filename='service_account.json')
    # Открываем нашу таблицу по имени из конфига
    sh = gc.open(config.GOOGLE_SHEET_NAME)
    # Выбираем первый лист в таблице
    worksheet = sh.sheet1
    logger.info(f"✅ Успешное подключение к Google Sheet: {config.GOOGLE_SHEET_NAME}")
except Exception as e:
    worksheet = None
    logger.error(f"❌ Не удалось подключиться к Google Sheets: {e}")


def log_g_sheets(user_id, prompt_tokens, completion_tokens, total_tokens, summary_text,
                 subject, homework_text, pages_str):
    """
    Добавляет полную строку с данными об использовании ИИ в Google Таблицу.
    Не делает ничего, если в конфиге включен DEBUG_MODE.
    """
    # --- НОВАЯ ПРОВЕРКА ---
    # Если бот в режиме отладки, ничего не делаем и выходим из функции
    if config.DEBUG_MODE:
        logger.info("Режим отладки включен. Пропускаю логирование в Google Sheets.")
        return
    # -----------------------

    if worksheet is None:
        logger.warning("Пропускаю логирование в Google Sheets: отсутствует подключение.")
        return

    try:
        current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        # Собираем строку со всеми новыми данными в правильном порядке
        row = [
            user_id,
            current_time,
            prompt_tokens,
            completion_tokens,
            total_tokens,
            subject,
            homework_text,
            pages_str,
            summary_text
        ]
        worksheet.append_row(row)
        logger.info("Полная информация о запросе к AI успешно залогирована в Google Sheets.")
    except Exception as e:
        logger.error(f"Ошибка при записи в Google Sheets: {e}")