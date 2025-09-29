# database.py

import logging
from pymongo import MongoClient
import config

# --- Настройка логгирования ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Подключение к базе данных ---
try:
    # Создаем клиент для подключения к MongoDB, используя строку из конфига
    client = MongoClient(config.MONGO_DB_CONNECTION_STRING)

    # Выбираем нашу базу данных (можно назвать ее 'student_bot_db')
    db = client.student_bot_db

    # Выбираем коллекцию (это как таблица в обычной БД) для хранения учебников
    textbooks_collection = db.textbooks

    # Проверка соединения с сервером
    client.server_info()
    logger.info("✅ Успешное подключение к MongoDB Atlas.")

except Exception as e:
    logger.error(f"❌ Не удалось подключиться к MongoDB: {e}")
    client = None
    textbooks_collection = None


# --- Функции для работы с коллекцией учебников ---

def add_textbook(subject: str, file_name: str, file_id: str):
    """
    Добавляет информацию о новом учебнике в базу данных.
    """
    if textbooks_collection is None:
        logger.error("Невозможно добавить учебник: отсутствует подключение к БД.")
        return None

    try:
        # Создаем документ (запись) для вставки
        document = {
            "subject": subject,
            "file_name": file_name,
            "file_id": file_id
        }
        # Вставляем документ в коллекцию
        result = textbooks_collection.insert_one(document)
        logger.info(f"Учебник '{file_name}' добавлен в базу с ID: {result.inserted_id}")
        return result
    except Exception as e:
        logger.error(f"Ошибка при добавлении учебника в БД: {e}")
        return None


def get_textbooks_by_subject(subject: str) -> list:
    """
    Находит все учебники по указанному предмету.
    """
    if textbooks_collection is None:
        logger.error("Невозможно найти учебники: отсутствует подключение к БД.")
        return []

    try:
        # Ищем все документы, у которых поле 'subject' соответствует запросу
        # Возвращаем результат в виде списка
        return list(textbooks_collection.find({"subject": subject}))
    except Exception as e:
        logger.error(f"Ошибка при поиске учебников в БД: {e}")
        return []
