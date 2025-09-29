# бот 2.py
import pickle
import logging
import datetime
import os
import re
import telegram
import asyncio
import time

import io
import base64
import fitz  # PyMuPDF
from PIL import Image
from openai import OpenAI
from sheets_logger import log_g_sheets
from dotenv import load_dotenv
from aiohttp import web
from google.auth.transport.requests import Request
from io import BytesIO
from googleapiclient.http import MediaIoBaseUpload
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
    ConversationHandler, CallbackContext, CallbackQueryHandler
)

import auth_web
import config
from database import get_textbooks_by_subject

load_dotenv()


# --- ЛОГИРОВАНИЕ ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- СОСТОЯНИЯ ДИАЛОГА ---

(
    # Главные меню
    MAIN_MENU, SCHEDULE_MENU, HOMEWORK_MENU, PERSONAL_HW_MENU,

    # Регистрация
    GET_NAME, GET_EMAIL,

    # Управление расписанием
    CONFIRM_DELETE_SCHEDULE,

    # Добавление/Редактирование своего текстового ДЗ
    GET_HW_TEXT, CHOOSE_HW_SUBJECT, CHOOSE_HW_DATE_OPTION,
    EDIT_HW_CHOOSE_SUBJECT, EDIT_HW_GET_DATE,  EDIT_HW_MENU, EDIT_HW_REPLACE_TEXT,

    # Добавление своего файла
    GET_FILE_ONLY, CHOOSE_SUBJECT_FOR_FILE, CHOOSE_DATE_FOR_FILE,

    # Добавление/Редактирование группового ДЗ
    GROUP_HW_MENU,
    GET_GROUP_HW_TEXT, CHOOSE_GROUP_HW_SUBJECT, CHOOSE_GROUP_HW_DATE_OPTION,
    GET_GROUP_FILE_ONLY, CHOOSE_SUBJECT_FOR_GROUP_FILE, CHOOSE_DATE_FOR_GROUP_FILE,
    EDIT_GROUP_HW_CHOOSE_SUBJECT, EDIT_GROUP_HW_GET_DATE, EDIT_GROUP_HW_MENU,
    EDIT_GROUP_HW_REPLACE_TEXT, ADD_TEXTBOOK_CHOOSE_SUBJECT, GET_TEXTBOOK_FILE, CHOOSE_SUMMARY_SUBJECT, CHOOSE_SUMMARY_FILE, GET_PAGE_NUMBERS,
    CONFIRM_SUMMARY_GENERATION
) = range(34)

async def error_handler(update: object, context: CallbackContext) -> None:
    """Логирует ошибки, вызванные апдейтами."""
    logger.error("Exception while handling an update:", exc_info=context.error)




def get_creds():
    """Загружает учетные данные из файла token.pickle."""
    creds = None
    if os.path.exists('.venv/token.pickle'):
        with open('.venv/token.pickle', 'rb') as token:
            creds = pickle.load(token)
    # Если токен истек, обновляем его
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return creds

def get_calendar_service(user_id: int):
    """
    Создает сервис для работы с Google Calendar API для конкретного пользователя.
    """
    if config.DEBUG_MODE:
        creds = auth_web.load_credentials("debug")
    else:
        # В обычном режиме используем токен конкретного пользователя
        creds = auth_web.load_credentials(user_id)

    if not creds:
        logger.warning(f"Не удалось загрузить учетные данные для user_id: {user_id}")
        return None
    try:
        return build('calendar', 'v3', credentials=creds)
    except Exception as e:
        logger.error(f"Ошибка при создании сервиса календаря для user_id {user_id}: {e}")
        return None

def get_drive_service(user_id: int):
    """
    Создает сервис для работы с Google Drive API для конкретного пользователя.
    """
    # Используем новую функцию из auth_web для загрузки учетных данных
    if config.DEBUG_MODE:
        creds = auth_web.load_credentials("debug")
    else:
        creds = auth_web.load_credentials(user_id)
    if not creds:
        logger.warning(f"Не удалось загрузить учетные данные для user_id: {user_id}")
        return None
    try:
        return build('drive', 'v3', credentials=creds)
    except Exception as e:
        logger.error(f"Ошибка при создании сервиса Google Drive для user_id {user_id}: {e}")
        return None


def upload_file_to_drive(user_id: int, file_name: str, file_bytes: bytes) -> dict | None:
    """
    Загружает файл на Google Drive конкретного пользователя и возвращает словарь с информацией о файле.
    """
    # --- ИЗМЕНЕНИЕ: Получаем сервис для конкретного пользователя ---
    drive_service = get_drive_service(user_id)
    if not drive_service:
        logger.error(f"Не удалось создать сервис Google Drive для user_id: {user_id}")
        return None

    try:
        # (остальная логика функции не меняется)
        folder_id = None
        q = "mimeType='application/vnd.google-apps.folder' and name='ДЗ от Телеграм Бота' and trashed=false"
        response = drive_service.files().list(q=q, spaces='drive', fields='files(id, name)').execute()

        if not response.get('files'):
            folder_metadata = {'name': 'ДЗ от Телеграм Бота', 'mimeType': 'application/vnd.google-apps.folder'}
            folder = drive_service.files().create(body=folder_metadata, fields='id').execute()
            folder_id = folder.get('id')
        else:
            folder_id = response.get('files')[0].get('id')

        file_metadata = {'name': file_name, 'parents': [folder_id]}
        media = MediaIoBaseUpload(BytesIO(file_bytes), mimetype='application/octet-stream', resumable=True)
        file = drive_service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id, webViewLink, mimeType'
        ).execute()
        file_id = file.get('id')

        drive_service.permissions().create(fileId=file_id, body={'type': 'anyone', 'role': 'reader'}).execute()

        return {
            'fileUrl': file.get('webViewLink'),
            'title': file_name,
            'mimeType': file.get('mimeType'),
            'fileId': file_id
        }

    except Exception as e:
        logger.error(f"Ошибка при загрузке файла на Google Drive для user_id {user_id}: {e}")
        return None

# --- Основные команды и меню ---

async def start(update: Update, context: CallbackContext) -> None:
    """Отправляет приветственное сообщение с кнопками 'Зарегистрироваться' и 'Войти'."""
    # Проверяем, есть ли у пользователя уже учетные данные
    user_id = update.effective_user.id

    if config.DEBUG_MODE:
        # В режиме отладки считаем, что пользователь всегда авторизован
        await update.message.reply_text("🤖 Бот в режиме отладки. Авторизация пропущена.")
        await main_menu(update, context)
        return

    creds = auth_web.load_credentials(user_id)

    if creds and not creds.expired:
        # Если пользователь уже вошел, сразу показываем главное меню
        await update.message.reply_text("Вы уже авторизованы.")
        await main_menu(update, context)
    else:
        # Если нет, предлагаем зарегистрироваться или войти
        keyboard = [
            [InlineKeyboardButton("Зарегистрироваться", callback_data="register")],
            [InlineKeyboardButton("Войти в аккаунт", callback_data="login")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        text = "Привет! Я помогу тебе с записью дз. Для работы нам нужен будет гугл календарь."
        await update.message.reply_text(text, reply_markup=reply_markup)

async def start_over_fallback(update: Update, context: CallbackContext) -> int:
    """Принудительно завершает текущий диалог и показывает стартовое меню."""
    # await start(update, context)
    return ConversationHandler.END


async def quick_login(update: Update, context: CallbackContext) -> int:
    """Удаляет старые учетные данные, отправляет ссылку для входа и завершает любой диалог."""
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    auth_web.delete_credentials(user_id)

    flow = auth_web.get_google_auth_flow()
    state = str(user_id)
    authorization_url, _ = flow.authorization_url(access_type='offline', prompt='consent', state=state)

    keyboard = [[InlineKeyboardButton("Авторизоваться", url=authorization_url)]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    text = "Пожалуйста, нажмите на кнопку, чтобы предоставить доступ к вашему Google Календарю:"
    await query.edit_message_text(text, reply_markup=reply_markup)

    # Завершаем любой диалог, в котором мог находиться пользователь
    return ConversationHandler.END

async def main_menu(update: Update, context: CallbackContext, force_new_message: bool = False):
    """Отображает главное меню."""
    keyboard = [
        [InlineKeyboardButton("Управление расписанием", callback_data="schedule_menu")],
        [InlineKeyboardButton("Управление ДЗ", callback_data="homework_management_menu")],
        [InlineKeyboardButton("🤖 Провести анализ ДЗ", callback_data="summary_start")],
        [InlineKeyboardButton("Выйти и сменить аккаунт", callback_data="logout")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = "Вы авторизованы. Выберите действие:"
    if update.callback_query and not force_new_message:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
    else:
        # В остальных случаях отправляем новое сообщение
        await context.bot.send_message(chat_id=update.effective_chat.id, text=text, reply_markup=reply_markup)


async def back_to_main_menu(update: Update, context: CallbackContext) -> int:
    """Возвращает в главное меню и завершает диалог."""
    await main_menu(update, context)
    return ConversationHandler.END


async def login(update: Update, context: CallbackContext):
    """Генерирует ссылку для авторизации Google в виде кнопки."""
    flow = auth_web.get_google_auth_flow()
    state = str(update.effective_user.id)
    authorization_url, _ = flow.authorization_url(access_type='offline', prompt='consent', state=state)

    keyboard = [[InlineKeyboardButton("Авторизоваться", url=authorization_url)]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    text = "Вы не авторизованы. Пожалуйста, нажмите на кнопку, чтобы предоставить доступ к вашему Google Календарю:"
    await update.message.reply_text(text, reply_markup=reply_markup)


# --- Логика регистрации и смены аккаунта ---
async def register_start(update: Update, context: CallbackContext) -> int:
    """Начинает процесс регистрации для нового пользователя."""
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    auth_web.delete_credentials(user_id)

    await query.edit_message_text(
        text="Отлично! Давайте начнем.\n\nПожалуйста, напишите ваше Имя и Фамилию."
    )
    return GET_NAME


async def logout_handler(update: Update, context: CallbackContext) -> int:
    """Обрабатывает выход из системы и начинает процесс новой регистрации."""
    query = update.callback_query
    await query.answer("Вы вышли из аккаунта.")

    user_id = update.effective_user.id
    auth_web.delete_credentials(user_id)

    await query.edit_message_text(
        text="Для новой авторизации, пожалуйста, напишите ваше Имя и Фамилию."
    )
    return GET_NAME


async def get_name(update: Update, context: CallbackContext) -> int:
    """Сохраняет имя и запрашивает email."""
    context.user_data['name'] = update.message.text
    await update.message.reply_text("Отлично! Теперь введите ваш Google email.")
    return GET_EMAIL


async def get_email_and_register(update: Update, context: CallbackContext) -> int:
    """Сохраняет email, уведомляет админа и дает ссылку на авторизацию в виде кнопки."""
    context.user_data['email'] = update.message.text
    user_name = context.user_data.get('name')
    user_email = context.user_data.get('email')

    try:
        message_to_admin = (
            f"Запрос на регистрацию в боте!\n\n"
            f"Имя: {user_name}\n"
            f"Email: {user_email}\n\n"
            f"Пожалуйста, добавь этот email в список тестовых пользователей в Google Cloud."
        )
        await context.bot.send_message(chat_id=config.DEVELOPER_TELEGRAM_ID, text=message_to_admin)
    except Exception as e:
        logger.error(f"Не удалось отправить уведомление администратору: {e}")

    flow = auth_web.get_google_auth_flow()
    state = str(update.effective_user.id)
    authorization_url, _ = flow.authorization_url(access_type='offline', prompt='consent', state=state)

    keyboard = [[InlineKeyboardButton("Авторизоваться", url=authorization_url)]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    text = (
        "Спасибо! Ваши данные отправлены администратору.\n\n"
        "**Дождитесь его одобрения**, и только после этого нажмите на кнопку для авторизации:"
    )
    await update.message.reply_text(text, reply_markup=reply_markup)
    context.user_data.clear()
    return ConversationHandler.END

# --- Логика управления расписанием (создание) ---

def create_semester_schedule_blocking(user_id) -> int:
    """
    Блокирующая функция для создания расписания с использованием batch-запросов и задержки.
    """
    service = get_calendar_service(user_id)
    if not service:
        logger.error(f"Не удалось создать сервис календаря для user_id {user_id} в фоновом потоке.")
        return 0

    events_created_count = 0

    def batch_callback(request_id, response, exception):
        """Callback-функция для подсчета успешно созданных событий."""
        nonlocal events_created_count
        if exception is None:
            events_created_count += 1
        else:
            logger.error(f"Ошибка при создании события в batch-запросе: {exception}")

    batch = service.new_batch_http_request(callback=batch_callback)
    requests_in_batch = 0

    today = datetime.date.today()
    start_date = today - datetime.timedelta(days=today.weekday())
    end_date = datetime.date(2025, 12, 31)
    day_map = {'Понедельник': 0, 'Вторник': 1, 'Среда': 2, 'Четверг': 3, 'Пятница': 4}
    day_names_rus = list(day_map.keys())

    current_date = start_date
    while current_date <= end_date:
        weekday_index = current_date.weekday()
        if weekday_index >= len(day_names_rus):
            current_date += datetime.timedelta(days=1)
            continue
        day_name = day_names_rus[weekday_index]
        if day_name in config.SCHEDULE_DATA:
            semester_start_ref_date = datetime.date(current_date.year, 9,
                                                    1) if current_date.month >= 9 else datetime.date(
                current_date.year - 1, 9, 1)
            semester_start_monday = semester_start_ref_date - datetime.timedelta(days=semester_start_ref_date.weekday())
            current_date_monday = current_date - datetime.timedelta(days=current_date.weekday())
            weeks_diff = (current_date_monday - semester_start_monday).days // 7
            week_type = "Нечетная неделя" if weeks_diff % 2 == 0 else "Четная неделя"

            if week_type in config.SCHEDULE_DATA[day_name]:
                for lesson in config.SCHEDULE_DATA[day_name][week_type]:
                    time_parts = [t.strip() for t in lesson["time"].split('–')]
                    start_h, start_m = map(int, time_parts[0].split(':'))
                    end_h, end_m = map(int, time_parts[1].split(':'))
                    start_datetime = datetime.datetime.combine(current_date, datetime.time(start_h, start_m))
                    end_datetime = datetime.datetime.combine(current_date, datetime.time(end_h, end_m))
                    event = {
                        'summary': f'{lesson["subject"]} ({lesson["room"]})',
                        'description': f'Преподаватель: {lesson["teacher"]}',
                        'start': {'dateTime': start_datetime.isoformat(), 'timeZone': 'Europe/Moscow'},
                        'end': {'dateTime': end_datetime.isoformat(), 'timeZone': 'Europe/Moscow'},
                        'colorId': config.COLOR_MAP.get(lesson["type"], "9"),
                    }
                    batch.add(service.events().insert(calendarId='primary', body=event))
                    requests_in_batch += 1

                    if requests_in_batch >= 50:
                        try:
                            batch.execute()
                        except Exception as e:
                            logger.error(f"Ошибка при выполнении batch-запроса на создание: {e}")

                        batch = service.new_batch_http_request(callback=batch_callback)
                        requests_in_batch = 0

        current_date += datetime.timedelta(days=1)

    if requests_in_batch > 0:
        try:
            batch.execute()
        except Exception as e:
            logger.error(f"Ошибка при выполнении финального batch-запроса на создание: {e}")

    return events_created_count


async def schedule_menu(update: Update, context: CallbackContext) -> int:
    """Меню управления расписанием."""
    query = update.callback_query
    await query.answer()
    keyboard = [
        [InlineKeyboardButton("Составить расписание", callback_data="create_schedule")],
        [InlineKeyboardButton("Удалить расписание", callback_data="delete_schedule")],
        [InlineKeyboardButton("« Назад в главное меню", callback_data="main_menu")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text('Что вы хотите сделать с расписанием?', reply_markup=reply_markup)
    return SCHEDULE_MENU


async def create_schedule_handler(update: Update, context: CallbackContext) -> int:
    """Обработчик для запуска создания расписания."""
    query = update.callback_query
    user_id = update.effective_user.id

    # --- ИЗМЕНЕНИЕ: Проверяем, авторизован ли пользователь ---
    service = get_calendar_service(user_id)
    if not service:
        await query.answer("Ошибка авторизации. Попробуйте войти снова через главное меню.", show_alert=True)
        return ConversationHandler.END

    await query.answer()
    await query.edit_message_text("Начинаю составлять расписание... Это может занять несколько минут.")

    loop = asyncio.get_running_loop()
    events_created = await loop.run_in_executor(
        None, create_semester_schedule_blocking, user_id
    )
    color_legend = (
        "Расписание создано!\n"
        f"Всего создано мероприятий: {events_created}\n\n"
        "🎨 Цветовая схема:\n"
        "🟦 Лекции\n"
        "🟥 Семинары\n"
        "🟩 Лабораторные работы"
    )
    keyboard = [[InlineKeyboardButton("« В меню расписания", callback_data="schedule_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.message.reply_text(color_legend, reply_markup=reply_markup)
    return SCHEDULE_MENU

# --- Логика управления расписанием (удаление) ---

def delete_schedule_blocking(user_id) -> int:
    """
    Блокирующая функция для удаления расписания с использованием batch-запросов и задержки.
    """
    service = get_calendar_service(user_id)
    if not service:
        logger.error(f"Не удалось создать сервис календаря для user_id {user_id} в фоновом потоке.")
        return 0

    bot_subjects = {lesson['subject'] for day in config.SCHEDULE_DATA.values() for week in day.values() for lesson in
                    week}
    end_date = datetime.date(2025, 12, 31)
    today = datetime.date.today()
    start_date = today - datetime.timedelta(days=today.weekday())
    time_min = datetime.datetime.combine(start_date, datetime.time.min).isoformat() + 'Z'
    time_max = datetime.datetime.combine(end_date, datetime.time.max).isoformat() + 'Z'

    events_to_delete = []
    page_token = None
    while True:
        try:
            events_result = service.events().list(
                calendarId='primary', timeMin=time_min, timeMax=time_max,
                singleEvents=True, pageToken=page_token, maxResults=2500
            ).execute()
            events = events_result.get('items', [])
            for event in events:
                summary = event.get('summary', '')
                match = re.search(r'^(.*)\s\(', summary)
                if match:
                    subject_name = match.group(1).strip().replace(config.HOMEWORK_TITLE_TAG, "").strip()
                    if subject_name in bot_subjects:
                        events_to_delete.append(event['id'])
            page_token = events_result.get('nextPageToken')
            if not page_token:
                break
        except Exception as e:
            logger.error(f"Ошибка при получении списка событий для удаления: {e}")
            break

    if not events_to_delete:
        return 0

    deleted_count = 0

    def batch_callback(request_id, response, exception):
        nonlocal deleted_count
        if exception is None:
            deleted_count += 1
        else:
            logger.error(f"Ошибка при удалении события в batch-запросе: {exception}")

    batch = service.new_batch_http_request(callback=batch_callback)
    requests_in_batch = 0

    for event_id in events_to_delete:
        batch.add(service.events().delete(calendarId='primary', eventId=event_id))
        requests_in_batch += 1
        if requests_in_batch >= 50:
            try:
                batch.execute()
            except Exception as e:
                logger.error(f"Ошибка при выполнении batch-запроса на удаление: {e}")

            batch = service.new_batch_http_request(callback=batch_callback)
            requests_in_batch = 0

    if requests_in_batch > 0:
        try:
            batch.execute()
        except Exception as e:
            logger.error(f"Ошибка при выполнении финального batch-запроса на удаление: {e}")

    return deleted_count


async def delete_schedule_confirm(update: Update, context: CallbackContext) -> int:
    """Запрашивает подтверждение на удаление."""
    query = update.callback_query
    await query.answer()
    keyboard = [
        [InlineKeyboardButton("Да, удалить", callback_data="confirm_delete")],
        [InlineKeyboardButton("Нет, отмена", callback_data="schedule_menu")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        "Вы уверены, что хотите удалить всё расписание, созданное ботом?",
        reply_markup=reply_markup
    )
    return CONFIRM_DELETE_SCHEDULE


async def run_schedule_deletion(update: Update, context: CallbackContext) -> int:
    """Запускает удаление расписания."""
    query = update.callback_query
    user_id = update.effective_user.id

    # --- ИЗМЕНЕНИЕ: Проверяем, авторизован ли пользователь ---
    service = get_calendar_service(user_id)
    if not service:
        await query.answer("Ошибка авторизации.", show_alert=True)
        return ConversationHandler.END

    await query.answer()
    await query.edit_message_text("Начинаю удаление... Это может занять время.")

    loop = asyncio.get_running_loop()
    deleted_count = await loop.run_in_executor(
        None, delete_schedule_blocking, user_id
    )
    await query.edit_message_text(
        f"Удаление завершено. Удалено мероприятий: {deleted_count}",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("« В меню расписания", callback_data="schedule_menu")]])
    )
    return SCHEDULE_MENU


# --- Вспомогательные функции для ДЗ ---
async def add_textbook_start(update: Update, context: CallbackContext) -> int:
    """Начинает диалог добавления учебника. Запрашивает предмет."""
    query = update.callback_query
    await query.answer()

    # Берем список предметов из конфига
    subjects = sorted(list(set(l['subject'] for d in config.SCHEDULE_DATA.values() for w in d.values() for l in w)))
    context.user_data['subjects_list'] = subjects

    # Создаем кнопки для каждого предмета
    buttons = [[InlineKeyboardButton(name, callback_data=f"textbook_subj_{i}")] for i, name in enumerate(subjects)]

    await query.edit_message_text(
        "📚 Выберите предмет, для которого хотите добавить учебник:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

    # Переходим в состояние ожидания выбора предмета
    return ADD_TEXTBOOK_CHOOSE_SUBJECT


async def add_textbook_choose_subject(update: Update, context: CallbackContext) -> int:
    """Обрабатывает выбор предмета и запрашивает файл."""
    query = update.callback_query
    await query.answer()

    # Получаем название предмета по индексу из кнопки
    subject_index = int(query.data.split('_')[-1])
    selected_subject = context.user_data['subjects_list'][subject_index]

    # Сохраняем выбранный предмет для следующего шага
    context.user_data['selected_subject'] = selected_subject

    await query.edit_message_text(
        f"Отлично. Теперь отправьте PDF-файл учебника для предмета '{selected_subject}'."
    )

    # Переходим в состояние ожидания файла
    return GET_TEXTBOOK_FILE


async def add_textbook_get_file(update: Update, context: CallbackContext) -> int:
    """Получает файл, загружает его на диск, сохраняет в БД и завершает диалог."""
    message = update.message
    user_id = update.effective_user.id

    # Сообщаем, что начали обработку
    await message.reply_text("Подождите, загружаю файл и сохраняю информацию...")

    doc = message.document
    file_name = doc.file_name

    # Скачиваем файл в память
    file = await doc.get_file()
    file_bytes = await file.download_as_bytearray()

    # 1. Загружаем на Google Drive, используя новую функцию
    upload_result = await asyncio.to_thread(
        upload_textbook_to_shared_drive, user_id, file_name, bytes(file_bytes)
    )

    if not upload_result:
        await message.reply_text("❌ Произошла ошибка при загрузке файла на Google Drive. Попробуйте снова.")
        return ConversationHandler.END

    # 2. Сохраняем информацию в MongoDB
    subject = context.user_data.get('selected_subject')
    file_id = upload_result.get('fileId')

    # Импортируем нашу функцию из database.py
    from database import add_textbook

    add_textbook(subject=subject, file_name=file_name, file_id=file_id)

    await message.reply_text(
        f"✅ Учебник '{file_name}' успешно добавлен в базу по предмету '{subject}'."
    )

    # Завершаем диалог
    context.user_data.clear()
    return ConversationHandler.END




def save_homework_to_event(event: dict, homework_text, service : str = "", is_group_hw: bool = False,
                           attachment_data: dict = None):
    """Обновляет описание, заголовок и ВЛОЖЕНИЯ события с ДЗ."""
    description = event.get('description', '')
    summary = event.get('summary', '')
    full_homework_text = homework_text.strip()

    teacher_part, group_hw_part, personal_hw_part = "", "", ""

    if config.GROUP_HOMEWORK_DESC_TAG in description:
        main_part, group_hw_part = description.split(config.GROUP_HOMEWORK_DESC_TAG, 1)
    else:
        main_part = description
    if config.PERSONAL_HOMEWORK_DESC_TAG in main_part:
        teacher_part, personal_hw_part = main_part.split(config.PERSONAL_HOMEWORK_DESC_TAG, 1)
    elif config.PERSONAL_HOMEWORK_DESC_TAG in group_hw_part:
        group_hw_part, personal_hw_part = group_hw_part.split(config.PERSONAL_HOMEWORK_DESC_TAG, 1)
    else:
        teacher_part = main_part

    if is_group_hw:
        group_hw_part = f"\n{full_homework_text}" if full_homework_text else ""
    else:
        personal_hw_part = f"\n{full_homework_text}" if full_homework_text else ""

    new_description = teacher_part.strip()
    if group_hw_part.strip():
        new_description += f"\n\n{config.GROUP_HOMEWORK_DESC_TAG}{group_hw_part}"
    if personal_hw_part.strip():
        new_description += f"\n\n{config.PERSONAL_HOMEWORK_DESC_TAG}{personal_hw_part}"
    event['description'] = new_description.strip()

    if attachment_data:
        new_attachment = {
            "fileUrl": attachment_data['fileUrl'],
            "title": attachment_data['title'],
            "mimeType": attachment_data['mimeType'],
            "fileId": attachment_data['fileId'],
        }
        if 'attachments' in event:
            existing_ids = {att.get('fileId') for att in event['attachments']}
            if new_attachment['fileId'] not in existing_ids:
                event['attachments'].append(new_attachment)
        else:
            event['attachments'] = [new_attachment]

    summary = summary.replace(config.HOMEWORK_TITLE_TAG, "").strip()
    if group_hw_part.strip() or personal_hw_part.strip() or event.get('attachments'):
        event['summary'] = f"{summary}{config.HOMEWORK_TITLE_TAG}"
    else:
        event['summary'] = summary

    service.events().update(
        calendarId='primary',
        eventId=event['id'],
        body=event,
        supportsAttachments=True
    ).execute()

def extract_homework_part(description: str, target_tag: str) -> str:
    """
    Находит и извлекает текст ДЗ для указанного тега.
    Работает как для личного, так и для группового ДЗ.
    """
    if target_tag not in description:
        return ""

    # Отделяем текст, идущий после нашего тега
    try:
        after_target_tag = description.split(target_tag, 1)[1]
    except IndexError:
        return ""

    # Ищем другие теги (личный/групповой), которые могут идти после нашего
    other_tags = [config.GROUP_HOMEWORK_DESC_TAG, config.PERSONAL_HOMEWORK_DESC_TAG]

    # Находим позицию самого раннего из "других" тегов
    next_tag_position = -1
    for tag in other_tags:
        if tag == target_tag:
            continue

        position = after_target_tag.find(tag)
        if position != -1:
            if next_tag_position == -1 or position < next_tag_position:
                next_tag_position = position

    # Если другой тег найден, обрезаем текст по нему
    if next_tag_position != -1:
        return after_target_tag[:next_tag_position].strip()
    else:
        # Если других тегов нет, берем все до конца
        return after_target_tag.strip()


# --- Логика добавления и редактирования ДЗ ---

async def homework_menu(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Введите текст домашнего задания:")
    return GET_HW_TEXT


async def get_hw_text(update: Update, context: CallbackContext) -> int:
    context.user_data['homework_text'] = update.message.text
    subjects = sorted(list(set(l['subject'] for d in config.SCHEDULE_DATA.values() for w in d.values() for l in w)))

    # --- НОВАЯ ЛОГИКА: Добавляем отдельный пункт для лабораторной ---
    if "Теоретические основы информатики" in subjects:
        subjects.append("Лабораторная: Теоретические основы информатики")
        subjects.sort()  # Сортируем снова, чтобы список был в алфавитном порядке
    # --- КОНЕЦ НОВОЙ ЛОГИКИ ---

    context.user_data['subjects_list'] = subjects
    buttons = [[InlineKeyboardButton(name, callback_data=f"hw_subj_{i}")] for i, name in enumerate(subjects)]
    await update.message.reply_text("Выберите предмет:", reply_markup=InlineKeyboardMarkup(buttons))
    return CHOOSE_HW_SUBJECT



async def choose_hw_subject(update: Update, context: CallbackContext) -> int:
    """Сохраняет предмет и тип занятия в зависимости от нажатой кнопки."""
    query = update.callback_query
    await query.answer()
    subject_index = int(query.data.split('_')[-1])
    selected_item = context.user_data['subjects_list'][subject_index]

    # --- НОВАЯ ЛОГИКА: Распознаем специальную кнопку ---
    if selected_item == "Лабораторная: Теоретические основы информатики":
        # Если выбрана лабораторная, устанавливаем правильные параметры
        context.user_data['homework_subject'] = "Теоретические основы информатики"
        context.user_data['hw_type'] = "Лабораторные работы"
        button_text = "На следующую лабораторку"
    else:
        # Для всех остальных предметов оставляем старое поведение
        context.user_data['homework_subject'] = selected_item
        context.user_data['hw_type'] = "Семинар"
        button_text = "На следующий семинар"
    # --- КОНЕЦ НОВОЙ ЛОГИКИ ---

    keyboard = [[InlineKeyboardButton(button_text, callback_data="find_next_class")]]
    await query.edit_message_text(
        "Введите дату (ДД.ММ) или выберите опцию:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return CHOOSE_HW_DATE_OPTION


async def choose_hw_type(update: Update, context: CallbackContext) -> int:
    """Сохраняет тип занятия (семинар/лаба) и запрашивает дату."""
    query = update.callback_query
    await query.answer()

    hw_type_data = query.data.split('_')[-1]

    if hw_type_data == "seminar":
        context.user_data['hw_type'] = "Семинар"
        button_text = "На следующий семинар"
    else:
        context.user_data['hw_type'] = "Лабораторные работы"
        button_text = "На следующую лабораторку"

    keyboard = [[InlineKeyboardButton(button_text, callback_data="find_next_class")]]
    await query.edit_message_text(
        "Введите дату (ДД.ММ) или выберите опцию:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return CHOOSE_HW_DATE_OPTION



async def find_next_class(update: Update, context: CallbackContext) -> int:
    """Ищет следующее занятие (семинар или лабу) и сохраняет ДЗ."""
    query = update.callback_query
    user_id = update.effective_user.id  # <-- ДОБАВЛЕНО

    # --- ИЗМЕНЕНИЕ: Получаем сервис для конкретного пользователя ---
    service = get_calendar_service(user_id)
    if not service:
        await query.answer("Ошибка авторизации. Попробуйте войти снова.", show_alert=True)
        return ConversationHandler.END

    await query.answer()

    class_type = context.user_data.get('hw_type', 'Семинар')
    subject_to_find = context.user_data.get('homework_subject')
    homework_text = context.user_data.get('homework_text')
    color_id_to_find = config.COLOR_MAP.get(class_type)

    await query.edit_message_text(f"Ищу ближайшее занятие «{class_type}» по предмету '{subject_to_find}'...")

    search_start_time = datetime.datetime.now(datetime.UTC).isoformat()

    try:
        events_result = service.events().list(
            calendarId='primary', timeMin=search_start_time, singleEvents=True,
            orderBy='startTime', maxResults=250
        ).execute()
        events = events_result.get('items', [])

        for event in events:
            event_summary = event.get('summary', '')
            event_color_id = event.get('colorId')
            match = re.search(r'^(.*?)\s*\(', event_summary)
            event_subject = match.group(1).strip() if match else event_summary.strip()

            if event_subject == subject_to_find and event_color_id == color_id_to_find:
                save_homework_to_event(event=event, service=service, homework_text=homework_text)
                event_date_str = event['start'].get('dateTime', event['start'].get('date'))
                event_date = datetime.datetime.fromisoformat(event_date_str).strftime('%d.%m.%Y')
                await query.edit_message_text(
                    f'Готово! ДЗ для "{subject_to_find}" ({class_type.lower()}) записано на {event_date}.'
                )
                await main_menu(update, context, force_new_message=True)
                context.user_data.clear()
                return ConversationHandler.END

        await query.edit_message_text(
            f"Не нашел предстоящих занятий типа «{class_type}» по предмету '{subject_to_find}'.")
        return CHOOSE_HW_DATE_OPTION

    except Exception as e:
        logger.error(f"Ошибка при поиске занятия: {e}", exc_info=True)
        await query.edit_message_text(f"Произошла критическая ошибка: {e}")
        context.user_data.clear()
        return ConversationHandler.END


async def get_manual_date_for_hw(update: Update, context: CallbackContext, is_editing: bool = False) -> int:
    user_id = update.effective_user.id  # <-- Добавляем эту строку для получения ID
    service = get_calendar_service(user_id)
    if not service:
        await update.message.reply_text("Ошибка авторизации. Убедитесь, что файл token.pickle существует.")
        context.user_data.clear()
        return ConversationHandler.END

    try:
        day, month = map(int, update.message.text.split('.'))
        target_date = datetime.date(datetime.date.today().year, month, day)
    except (ValueError, IndexError):
        await update.message.reply_text("Неверный формат. Введите дату как ДД.ММ")
        return EDIT_HW_GET_DATE if is_editing else CHOOSE_HW_DATE_OPTION

    subject = context.user_data.get('homework_subject')
    class_type = context.user_data.get('hw_type', 'Семинар')
    class_color_id = config.COLOR_MAP.get(class_type)

    time_min = datetime.datetime.combine(target_date, datetime.time.min).isoformat() + 'Z'
    time_max = datetime.datetime.combine(target_date, datetime.time.max).isoformat() + 'Z'

    try:
        all_events_today = service.events().list(calendarId='primary', timeMin=time_min, timeMax=time_max,
                                                 singleEvents=True).execute().get('items', [])

        matching_classes = []
        for event in all_events_today:
            event_summary = event.get('summary', '')
            match = re.search(r'^(.*?)\s\(', event_summary)
            event_subject = match.group(1).strip() if match else ''
            if event_subject == subject and event.get('colorId') == class_color_id:
                matching_classes.append(event)

        if not matching_classes:
            await update.message.reply_text(
                f"На {target_date.strftime('%d.%m.%Y')} не найдено занятий типа «{class_type}» по предмету '{subject}'.")
            return EDIT_HW_GET_DATE if is_editing else CHOOSE_HW_DATE_OPTION

        event_to_process = matching_classes[0]

        if is_editing:
            # Логика редактирования остается прежней
            context.user_data['event_to_edit'] = event_to_process
            description = event_to_process.get('description', '')
            hw_part = extract_homework_part(description, config.PERSONAL_HOMEWORK_DESC_TAG)
            if not hw_part: hw_part = "ДЗ пока не было записано."
            keyboard = [[InlineKeyboardButton("Удалить это ДЗ", callback_data="delete_personal_hw")],
                        [InlineKeyboardButton("Оставить как есть", callback_data="main_menu")]]
            await update.message.reply_text(
                f"Текущее ДЗ:\n\n{hw_part}\n\nВведите новый текст ДЗ или используйте кнопки:",
                reply_markup=InlineKeyboardMarkup(keyboard))
            return EDIT_HW_GET_NEW_TEXT
        else:
            homework_text = context.user_data.get('homework_text')
            save_homework_to_event(event=event_to_process, service=service, homework_text=homework_text)

            # Сначала отправляем подтверждение
            await update.message.reply_text(
                f'Готово! ДЗ для "{subject}" ({class_type.lower()}) записано на {target_date.strftime("%d.%m.%Y")}.'
            )
            # А затем вызываем главное меню
            await main_menu(update, context, force_new_message=True)

            context.user_data.clear()
            return ConversationHandler.END

    except Exception as e:
        logger.error(f"Error with manual date for HW: {e}")
        await update.message.reply_text(f"Произошла ошибка: {e}")
        context.user_data.clear()
        return ConversationHandler.END

async def edit_group_hw_get_date(update: Update, context: CallbackContext) -> int:
    """Обрабатывает введенную дату, находит семинар и показывает меню редактирования группового ДЗ."""
    user_id = update.effective_user.id
    service = get_calendar_service(user_id)
    if not service:
        await update.message.reply_text("Ошибка авторизации.")
        return ConversationHandler.END

    try:
        day, month = map(int, update.message.text.split('.'))
        target_date = datetime.date(datetime.date.today().year, month, day)
    except (ValueError, IndexError):
        await update.message.reply_text("Неверный формат. Введите дату как ДД.ММ")
        return EDIT_GROUP_HW_GET_DATE

    subject = context.user_data.get('group_homework_subject')
    seminar_color_id = config.COLOR_MAP.get("Семинар")
    time_min = datetime.datetime.combine(target_date, datetime.time.min).isoformat() + 'Z'
    time_max = datetime.datetime.combine(target_date, datetime.time.max).isoformat() + 'Z'

    try:
        events = service.events().list(calendarId='primary', timeMin=time_min, timeMax=time_max,
                                       singleEvents=True).execute().get('items', [])
        found_event = None
        for event in events:
            event_summary = event.get('summary', '')
            match = re.search(r'^(.*?)\s\(', event_summary)
            event_subject = match.group(1).strip() if match else ''
            if event_subject == subject and event.get('colorId') == seminar_color_id:
                found_event = event
                break

        if not found_event:
            await update.message.reply_text(
                f"На {target_date.strftime('%d.%m.%Y')} не найдено семинаров по предмету '{subject}'.")
            return EDIT_GROUP_HW_GET_DATE

        description = found_event.get('description', '')

        # <<< ВОЗВРАЩАЕМ СТАРУЮ ЛОГИКУ ЧТЕНИЯ ДЗ >>>
        hw_part = "Групповое ДЗ пока не было записано."
        if config.GROUP_HOMEWORK_DESC_TAG in description:
            hw_part = \
                description.split(config.GROUP_HOMEWORK_DESC_TAG, 1)[1].strip().split(config.PERSONAL_HOMEWORK_DESC_TAG,
                                                                                      1)[
                    0].strip()

        keyboard = [
            [InlineKeyboardButton("Удалить групповое ДЗ", callback_data="delete_group_hw")],
            [InlineKeyboardButton("Оставить как есть", callback_data="main_menu")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            f"Текущее групповое ДЗ:\n\n{hw_part}\n\nВведите новый текст или используйте кнопки:",
            reply_markup=reply_markup
        )
        return EDIT_GROUP_HW_GET_NEW_TEXT

    except Exception as e:
        logger.error(f"Error in edit_group_hw_get_date: {e}")
        await update.message.reply_text(f"Произошла ошибка: {e}")
        return ConversationHandler.END


async def edit_hw_start(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    subjects = sorted(list(set(l['subject'] for d in config.SCHEDULE_DATA.values() for w in d.values() for l in w)))

    # --- НОВАЯ ЛОГИКА: Добавляем отдельный пункт для лабораторной ---
    if "Теоретические основы информатики" in subjects:
        subjects.append("Лабораторная: Теоретические основы информатики")
        subjects.sort()
    # --- КОНЕЦ НОВОЙ ЛОГИКИ ---

    context.user_data['subjects_list'] = subjects
    buttons = [[InlineKeyboardButton(name, callback_data=f"edit_hw_subj_{i}")] for i, name in enumerate(subjects)]
    await query.edit_message_text("Выберите предмет для редактирования ДЗ:", reply_markup=InlineKeyboardMarkup(buttons))
    return EDIT_HW_CHOOSE_SUBJECT


async def edit_hw_choose_subject(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    subject_index = int(query.data.split('_')[-1])
    selected_item = context.user_data['subjects_list'][subject_index]

    # --- НОВАЯ ЛОГИКА: Распознаем специальную кнопку ---
    if selected_item == "Лабораторная: Теоретические основы информатики":
        # Если выбрана лабораторная, устанавливаем правильные параметры
        context.user_data['homework_subject'] = "Теоретические основы информатики"
        context.user_data['hw_type'] = "Лабораторные работы"
    else:
        # Для всех остальных предметов оставляем старое поведение
        context.user_data['homework_subject'] = selected_item
        context.user_data['hw_type'] = "Семинар"
    # --- КОНЕЦ НОВОЙ ЛОГИКИ ---

    await query.edit_message_text("Введите дату, для которой хотите отредактировать ДЗ (в формате ДД.ММ):")
    return EDIT_HW_GET_DATE


async def edit_hw_get_date(update: Update, context: CallbackContext) -> int:
    """
    Находит событие по дате, показывает текущее ДЗ и меню действий.
    """
    user_id = update.effective_user.id
    service = get_calendar_service(user_id)
    if not service:
        await update.message.reply_text("Ошибка авторизации. Попробуйте войти снова.")
        return ConversationHandler.END

    try:
        day, month = map(int, update.message.text.split('.'))
        target_date = datetime.date(datetime.date.today().year, month, day)
    except (ValueError, IndexError):
        await update.message.reply_text("Неверный формат. Введите дату как ДД.ММ")
        return EDIT_HW_GET_DATE

    subject = context.user_data.get('homework_subject')
    class_type = context.user_data.get('hw_type', 'Семинар')
    class_color_id = config.COLOR_MAP.get(class_type)
    time_min = datetime.datetime.combine(target_date, datetime.time.min).isoformat() + 'Z'
    time_max = datetime.datetime.combine(target_date, datetime.time.max).isoformat() + 'Z'

    try:
        events = service.events().list(calendarId='primary', timeMin=time_min, timeMax=time_max,
                                       singleEvents=True).execute().get('items', [])
        found_event = None
        for event in events:
            event_summary = event.get('summary', '')
            match = re.search(r'^(.*?)\s\(', event_summary)
            event_subject = match.group(1).strip() if match else ''
            if event_subject == subject and event.get('colorId') == class_color_id:
                found_event = event
                break

        if not found_event:
            await update.message.reply_text(
                f"На {target_date.strftime('%d.%m.%Y')} не найдено занятий типа «{class_type}» по предмету '{subject}'.")
            return EDIT_HW_GET_DATE

        context.user_data['event_to_edit_id'] = found_event['id']
        description = found_event.get('description', '')
        hw_text = extract_homework_part(description, config.PERSONAL_HOMEWORK_DESC_TAG)
        attachments = found_event.get('attachments', [])

        # --- ИЗМЕНЕНИЕ ФОРМАТИРОВАНИЯ ---
        message_lines = [f"📝 **Редактирование ДЗ на {target_date.strftime('%d.%m.%Y')}**"]
        message_lines.append("\n**Текст:**")
        if hw_text:
            message_lines.append(f"```\n{hw_text}\n```")  # Используем многострочный блок
        else:
            message_lines.append("_Текст ДЗ отсутствует_")
        # --- КОНЕЦ ИЗМЕНЕНИЯ ---

        message_lines.append("\n**Файл:**")
        if attachments:
            message_lines.append(f"📎 `{attachments[0]['title']}`")
        else:
            message_lines.append("_Файл не прикреплен_")

        message_text = "\n".join(message_lines)
        keyboard = [
            [
                InlineKeyboardButton("🗑️ Удалить текст", callback_data="edit_delete_text"),
                InlineKeyboardButton("🗑️ Удалить файл", callback_data="edit_delete_file")
            ],
            [InlineKeyboardButton("🔄 Заменить текст ДЗ", callback_data="edit_replace_text")],
            [InlineKeyboardButton("✅ Оставить как есть", callback_data="main_menu")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(message_text, reply_markup=reply_markup, parse_mode='Markdown')
        return EDIT_HW_MENU

    except Exception as e:
        logger.error(f"Error in edit_hw_get_date: {e}")
        await update.message.reply_text(f"Произошла ошибка: {e}")
        return ConversationHandler.END


async def edit_delete_text(update: Update, context: CallbackContext) -> int:
    """Удаляет только текст личного ДЗ из события."""
    query = update.callback_query
    user_id = update.effective_user.id  # <-- ДОБАВЛЕНО
    await query.answer()

    # --- ИЗМЕНЕНИЕ: Получаем сервис для конкретного пользователя ---
    service = get_calendar_service(user_id)
    event_id = context.user_data.get('event_to_edit_id')

    if not service or not event_id:
        await query.edit_message_text("Произошла ошибка, сессия истекла. Попробуйте снова.")
        return ConversationHandler.END

    event = service.events().get(calendarId='primary', eventId=event_id).execute()
    save_homework_to_event(event, service=service, homework_text="")
    await query.edit_message_text("✅ Текст домашнего задания удален.")
    await main_menu(update, context, force_new_message=True)
    context.user_data.clear()
    return ConversationHandler.END


async def edit_delete_file(update: Update, context: CallbackContext) -> int:
    """Удаляет вложение из события и с Google Drive."""
    query = update.callback_query
    await query.answer()

    # --- ИСПРАВЛЕНИЕ ЗДЕСЬ ---
    user_id = update.effective_user.id
    service = get_calendar_service(user_id)
    drive_service = get_drive_service(user_id)
    # --- КОНЕЦ ИСПРАВЛЕНИЯ ---

    event_id = context.user_data.get('event_to_edit_id')

    if not all([service, drive_service, event_id]):
        await query.edit_message_text("Произошла ошибка, сессия истекла. Попробуйте снова.")
        return ConversationHandler.END

    event = service.events().get(calendarId='primary', eventId=event_id).execute()

    if not event.get('attachments'):
        await query.edit_message_text("❌ У этого ДЗ нет прикрепленного файла.")
        await main_menu(update, context, force_new_message=True)
        context.user_data.clear()
        return ConversationHandler.END

    file_to_delete = event['attachments'][0]
    file_id = file_to_delete['fileId']

    event['attachments'] = []
    save_homework_to_event(event, service=service, homework_text=extract_homework_part(event.get('description', ''),
                                                                                       config.PERSONAL_HOMEWORK_DESC_TAG))
    try:
        drive_service.files().delete(fileId=file_id).execute()
        await query.edit_message_text(f"✅ Файл `{file_to_delete['title']}` удален.", parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Не удалось удалить файл {file_id} с Google Drive: {e}")
        await query.edit_message_text(f"✅ Вложение из календаря удалено, но не удалось удалить файл с Google Drive.")

    await main_menu(update, context, force_new_message=True)
    context.user_data.clear()
    return ConversationHandler.END
async def edit_replace_text_start(update: Update, context: CallbackContext) -> int:
    """Просит пользователя ввести новый текст ДЗ."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Пожалуйста, отправьте новый текст домашнего задания.")
    return EDIT_HW_REPLACE_TEXT



async def edit_hw_get_new_text(update: Update, context: CallbackContext) -> int:
    """Получает новый текст ДЗ и обновляет событие."""
    user_id = update.effective_user.id  # <-- ДОБАВЛЕНО

    # --- ИЗМЕНЕНИЕ: Получаем сервис для конкретного пользователя ---
    service = get_calendar_service(user_id)
    event_id = context.user_data.get('event_to_edit_id')

    if not service or not event_id:
        await update.message.reply_text("Произошла ошибка, сессия истекла.")
        return ConversationHandler.END

    new_text = update.message.text
    event = service.events().get(calendarId='primary', eventId=event_id).execute()
    save_homework_to_event(
        event,
        service=service,
        homework_text=new_text,
        attachment_data=event.get('attachments', [None])[0]
    )
    await update.message.reply_text("✅ Текст домашнего задания обновлен.")
    await main_menu(update, context, force_new_message=True)
    context.user_data.clear()
    return ConversationHandler.END


async def delete_personal_hw(update: Update, context: CallbackContext) -> int:
    """Удаляет личное ДЗ из события."""
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    service = get_calendar_service(user_id)
    if not service or 'event_to_edit' not in context.user_data:
        await query.edit_message_text("Произошла ошибка или сессия истекла. Попробуйте снова.")
        return ConversationHandler.END

    event_to_edit = context.user_data.get('event_to_edit')
    subject = context.user_data.get('homework_subject')

    save_homework_to_event(event_to_edit, "", service, is_group_hw=False)

    await query.edit_message_text(
        f"Личное ДЗ для '{subject}' успешно удалено!",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« В главное меню", callback_data="main_menu")]])
    )
    context.user_data.clear()
    return ConversationHandler.END


# --- Логика группового ДЗ (Admin only) ---

async def group_homework_menu(update: Update, context: CallbackContext) -> int:
    """Отображает меню управления групповым ДЗ."""
    query = update.callback_query
    await query.answer()

    keyboard = [
        [InlineKeyboardButton("✍️ Записать групповое ДЗ", callback_data="group_hw_add_text_start")],
        [InlineKeyboardButton("📎 Добавить файл для группы", callback_data="group_hw_add_file_start")],
        [InlineKeyboardButton("✏️ Редактировать групповое ДЗ", callback_data="group_hw_edit_start")],
        # --- ДОБАВЛЯЕМ НОВУЮ КНОПКУ ---
        [InlineKeyboardButton("📚 Добавить учебник", callback_data="add_textbook_start")],
        # ---------------------------------
        [InlineKeyboardButton("« Назад", callback_data="homework_management_menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        text="Действия с групповым домашним заданием:",
        reply_markup=reply_markup
    )
    return GROUP_HW_MENU

async def edit_group_hw_start(update: Update, context: CallbackContext) -> int:
    """Начинает процесс редактирования группового ДЗ."""
    query = update.callback_query
    await query.answer()
    subjects = sorted(list(set(l['subject'] for d in config.SCHEDULE_DATA.values() for w in d.values() for l in w)))

    # Добавляем кнопку для лабораторной
    if "Теоретические основы информатики" in subjects:
        subjects.append("Лабораторная: Теоретические основы информатики")
        subjects.sort()

    context.user_data['subjects_list'] = subjects
    buttons = [[InlineKeyboardButton(name, callback_data=f"edit_group_hw_subj_{i}")] for i, name in enumerate(subjects)]
    await query.edit_message_text("Выберите предмет для редактирования группового ДЗ:",
                                  reply_markup=InlineKeyboardMarkup(buttons))
    return EDIT_GROUP_HW_CHOOSE_SUBJECT


async def edit_group_hw_choose_subject(update: Update, context: CallbackContext) -> int:
    """
    Обрабатывает выбор предмета для редактирования группового ДЗ и запрашивает дату.
    """
    query = update.callback_query
    await query.answer()

    subject_index = int(query.data.split('_')[-1])
    selected_item = context.user_data['subjects_list'][subject_index]

    # Определяем, семинар это или лабораторная, и сохраняем в контекст
    if selected_item == "Лабораторная: Теоретические основы информатики":
        context.user_data['homework_subject'] = "Теоретические основы информатики"
        context.user_data['hw_type'] = "Лабораторные работы"
    else:
        context.user_data['homework_subject'] = selected_item
        context.user_data['hw_type'] = "Семинар"

    await query.edit_message_text("Введите дату (в формате ДД.ММ), на которую хотите отредактировать групповое ДЗ:")

    # Переходим в состояние ожидания даты для редактирования
    return EDIT_GROUP_HW_GET_DATE



async def edit_group_hw_get_new_text(update: Update, context: CallbackContext) -> int:
    """Обновляет групповое ДЗ для всех пользователей."""
    await update.message.reply_text("Начинаю обновление ДЗ для группы. Это может занять время...")
    subject = context.user_data.get('group_homework_subject')
    new_homework_text = update.message.text
    class_type = context.user_data.get('hw_type', 'Семинар') # Получаем тип
    target_date = context.user_data.get('target_date') # Получаем дату из предыдущего шага

    # Передаем class_type и target_date
    updated_count, _ = await asyncio.to_thread(
        find_and_update_or_delete_group_hw_blocking, subject, new_homework_text, class_type, target_date
    )

    await update.message.reply_text(
        f"Групповое ДЗ для '{subject}' ({class_type.lower()}) обновлено у {updated_count} пользователей.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« В главное меню", callback_data="main_menu")]])
    )
    context.user_data.clear()
    return ConversationHandler.END


async def delete_group_hw(update: Update, context: CallbackContext) -> int:
    """Удаляет групповое ДЗ у всех пользователей."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Начинаю удаление ДЗ для группы. Это может занять время...")
    subject = context.user_data.get('group_homework_subject')
    class_type = context.user_data.get('hw_type', 'Семинар')  # Получаем тип
    target_date = context.user_data.get('target_date')  # Получаем дату

    # Передаем class_type и target_date, но пустой текст для удаления
    updated_count, _ = await asyncio.to_thread(
        find_and_update_or_delete_group_hw_blocking, subject, "", class_type, target_date
    )

    await query.edit_message_text(
        f"Групповое ДЗ для '{subject}' ({class_type.lower()}) удалено у {updated_count} пользователей."
    )

    # ИЗМЕНЕНИЕ: Добавляем вызов главного меню
    await main_menu(update, context, force_new_message=True)

    context.user_data.clear()
    return ConversationHandler.END
#-------
# --- Ветка: Добавление ТЕКСТА для группы ---
async def group_hw_add_text_start(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Введите текст ДЗ для всей группы:")
    return GET_GROUP_HW_TEXT


async def get_manual_date_for_group_hw_text(update: Update, context: CallbackContext) -> int:
    """Обрабатывает ручной ввод даты и обновляет текст ДЗ для группы."""
    try:
        day, month = map(int, update.message.text.split('.'))
        target_date = datetime.date(datetime.date.today().year, month, day)
    except (ValueError, IndexError):
        await update.message.reply_text("Неверный формат. Введите дату как ДД.ММ")
        return CHOOSE_GROUP_HW_DATE_OPTION

    subject = context.user_data.get('group_homework_subject')
    class_type = context.user_data.get('hw_type', 'Семинар')
    homework_text = context.user_data.get('group_homework_text')

    await update.message.reply_text(f"Начинаю обновление текста ДЗ для группы...")

    updated_count, _ = await asyncio.to_thread(
        update_group_homework_blocking, subject, class_type, target_date, new_text=homework_text
    )

    await update.message.reply_text(
        f"✅ Текст ДЗ для '{subject}' на {target_date.strftime('%d.%m.%Y')} обновлен у {updated_count} пользователей."
    )

    # ИЗМЕНЕНИЕ: Вызываем главное меню после подтверждения
    await main_menu(update, context, force_new_message=True)

    context.user_data.clear()
    return ConversationHandler.END

# --- Ветка: Добавление ФАЙЛА для группы ---
async def group_hw_add_file_start(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()

    # --- ИЗМЕНЕНИЕ: Устанавливаем флаг для групповой загрузки ---
    context.user_data['is_group_file'] = True

    await query.edit_message_text("Отправьте файл, который нужно прикрепить для всей группы.")
    return GET_GROUP_FILE_ONLY



async def get_manual_date_for_group_hw_file(update: Update, context: CallbackContext) -> int:
    """Обрабатывает ручной ввод даты и добавляет файл для группы."""
    try:
        day, month = map(int, update.message.text.split('.'))
        target_date = datetime.date(datetime.date.today().year, month, day)
    except (ValueError, IndexError):
        await update.message.reply_text("Неверный формат. Введите дату как ДД.ММ")
        return CHOOSE_DATE_FOR_GROUP_FILE

    subject = context.user_data.get('homework_subject')
    class_type = context.user_data.get('hw_type', 'Семинар')
    file_bytes = context.user_data.get('file_bytes')
    file_name = context.user_data.get('file_name')

    await update.message.reply_text(f"Загружаю файл на ваш диск и обновляю ДЗ для группы...")

    attachment_info = await asyncio.to_thread(upload_file_to_drive, file_name, file_bytes)
    if not attachment_info:
        await update.message.reply_text("Не удалось загрузить файл на Google Drive.")
        context.user_data.clear()
        return ConversationHandler.END

    updated_count, _ = await asyncio.to_thread(
        update_group_homework_blocking, subject, class_type, target_date, new_attachment=attachment_info
    )

    await update.message.reply_text(
        f"✅ Файл для '{subject}' на {target_date.strftime('%d.%m.%Y')} прикреплен у {updated_count} пользователей."
    )

    # ИЗМЕНЕНИЕ: Вызываем главное меню после подтверждения
    await main_menu(update, context, force_new_message=True)

    context.user_data.clear()
    return ConversationHandler.END

# --- Ветка: Редактирование группового ДЗ ---
async def group_hw_edit_start(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    subjects = sorted(list(set(l['subject'] for d in config.SCHEDULE_DATA.values() for w in d.values() for l in w)))
    if "Теоретические основы информатики" in subjects:
        subjects.append("Лабораторная: Теоретические основы информатики")
        subjects.sort()
    context.user_data['subjects_list'] = subjects
    buttons = [[InlineKeyboardButton(name, callback_data=f"edit_group_hw_subj_{i}")] for i, name in enumerate(subjects)]
    await query.edit_message_text("Выберите предмет для редактирования группового ДЗ:",
                                  reply_markup=InlineKeyboardMarkup(buttons))
    return EDIT_GROUP_HW_CHOOSE_SUBJECT


async def edit_group_hw_get_date(update: Update, context: CallbackContext) -> int:
    """Показывает меню редактирования группового ДЗ, используя календарь админа как образец."""
    user_id = update.effective_user.id
    service = get_calendar_service(user_id)
    if not service:
        await update.message.reply_text("Ошибка авторизации админа.")
        return ConversationHandler.END

    try:
        day, month = map(int, update.message.text.split('.'))
        target_date = datetime.date(datetime.date.today().year, month, day)
        context.user_data['target_date'] = target_date
    except (ValueError, IndexError):
        await update.message.reply_text("Неверный формат. Введите дату как ДД.ММ")
        return EDIT_GROUP_HW_GET_DATE

    subject = context.user_data.get('homework_subject')
    class_type = context.user_data.get('hw_type', 'Семинар')
    class_color_id = config.COLOR_MAP.get(class_type)
    time_min = datetime.datetime.combine(target_date, datetime.time.min).isoformat() + 'Z'
    time_max = datetime.datetime.combine(target_date, datetime.time.max).isoformat() + 'Z'

    events = service.events().list(calendarId='primary', timeMin=time_min, timeMax=time_max,
                                   singleEvents=True).execute().get('items', [])
    found_event = None
    for event in events:
        event_summary = event.get('summary', '')
        match = re.search(r'^(.*?)\s\(', event_summary)
        event_subject = match.group(1).strip() if match else ''
        if event_subject == subject and event.get('colorId') == class_color_id:
            found_event = event
            break

    if not found_event:
        await update.message.reply_text("Не нашел такого занятия в вашем календаре, чтобы использовать как образец.")
        return EDIT_GROUP_HW_GET_DATE

    description = found_event.get('description', '')
    hw_text = extract_homework_part(description, config.GROUP_HOMEWORK_DESC_TAG)
    attachments = found_event.get('attachments', [])

    # --- ИЗМЕНЕНИЕ ФОРМАТИРОВАНИЯ ---
    message_lines = ["**Текущее групповое ДЗ:**"]
    if hw_text:
        message_lines.append(f"```\n{hw_text}\n```")  # Используем многострочный блок
    else:
        message_lines.append("_Текст отсутствует_")
    # --- КОНЕЦ ИЗМЕНЕНИЯ ---

    if attachments:
        message_lines.append(f"📎 Файл: `{attachments[0]['title']}`")
    else:
        message_lines.append("_Файл отсутствует_")

    keyboard = [
        [InlineKeyboardButton("🗑️ Удалить текст", callback_data="edit_group_delete_text")],
        [InlineKeyboardButton("🗑️ Удалить файл", callback_data="edit_group_delete_file")],
        [InlineKeyboardButton("🔄 Заменить текст", callback_data="edit_group_replace_text")],
        [InlineKeyboardButton("✅ Оставить как есть", callback_data="main_menu")]
    ]
    await update.message.reply_text("\n".join(message_lines), reply_markup=InlineKeyboardMarkup(keyboard),
                                    parse_mode='Markdown')
    return EDIT_GROUP_HW_MENU

async def edit_group_delete_text(update: Update, context: CallbackContext) -> int:
    """Удаляет текст группового ДЗ у всех пользователей."""
    query = update.callback_query
    await query.answer()

    subject = context.user_data.get('homework_subject')
    class_type = context.user_data.get('hw_type', 'Семинар')
    target_date = context.user_data.get('target_date')

    await query.edit_message_text("Начинаю удаление текста ДЗ для группы...")
    updated_count, _ = await asyncio.to_thread(
        update_group_homework_blocking, subject, class_type, target_date, delete_text=True
    )
    await query.edit_message_text(f"✅ Текст группового ДЗ удален у {updated_count} пользователей.")

    # ИЗМЕНЕНИЕ: Добавляем вызов главного меню
    await main_menu(update, context, force_new_message=True)

    context.user_data.clear()
    return ConversationHandler.END


async def edit_group_delete_file(update: Update, context: CallbackContext) -> int:
    """Удаляет файл группового ДЗ у всех и с диска админа."""
    query = update.callback_query
    await query.answer()

    # --- ИСПРАВЛЕНИЕ ЗДЕСЬ ---
    user_id = update.effective_user.id
    service = get_calendar_service(user_id)
    drive_service = get_drive_service(user_id)
    # --- КОНЕЦ ИСПРАВЛЕНИЯ ---

    subject = context.user_data.get('homework_subject')
    class_type = context.user_data.get('hw_type', 'Семинар')
    target_date = context.user_data.get('target_date')

    time_min = datetime.datetime.combine(target_date, datetime.time.min).isoformat() + 'Z'
    time_max = datetime.datetime.combine(target_date, datetime.time.max).isoformat() + 'Z'
    class_color_id = config.COLOR_MAP.get(class_type)

    events = service.events().list(calendarId='primary', timeMin=time_min, timeMax=time_max,
                                   singleEvents=True).execute().get('items', [])
    found_event = None
    for event in events:
        event_summary = event.get('summary', '')
        match = re.search(r'^(.*?)\s\(', event_summary)
        event_subject = match.group(1).strip() if match else ''
        if event_subject == subject and event.get('colorId') == class_color_id:
            found_event = event
            break

    if found_event and found_event.get('attachments'):
        try:
            file_to_delete = found_event['attachments'][0]
            file_id = file_to_delete.get('fileId')
            drive_service.files().delete(fileId=file_id).execute()
            logger.info(f"Файл {file_id} успешно удален с диска админа.")
        except Exception as e:
            logger.warning(f"Не удалось удалить файл с Google Drive админа (возможно, он уже удален): {e}")

    updated_count, _ = await asyncio.to_thread(
        update_group_homework_blocking, subject, class_type, target_date, delete_attachment=True
    )

    await query.edit_message_text(f"✅ Файл группового ДЗ удален у {updated_count} пользователей.")
    await main_menu(update, context, force_new_message=True)
    context.user_data.clear()
    return ConversationHandler.END

async def edit_group_replace_text_start(update: Update, context: CallbackContext) -> int:
    """Просит админа ввести новый текст группового ДЗ."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Введите новый текст для группового ДЗ.")
    return EDIT_GROUP_HW_REPLACE_TEXT


async def edit_group_get_new_text(update: Update, context: CallbackContext) -> int:
    """Получает новый текст и обновляет его для всей группы."""
    new_text = update.message.text
    subject = context.user_data.get('homework_subject')
    class_type = context.user_data.get('hw_type', 'Семинар')
    target_date = context.user_data.get('target_date')

    await update.message.reply_text("Обновляю текст для группы...")
    updated_count, _ = await asyncio.to_thread(
        update_group_homework_blocking, subject, class_type, target_date, new_text=new_text
    )
    await update.message.reply_text(f"✅ Текст группового ДЗ обновлен у {updated_count} пользователей.")

    # ИЗМЕНЕНИЕ: Добавляем вызов главного меню
    await main_menu(update, context, force_new_message=True)

    context.user_data.clear()
    return ConversationHandler.END


def update_group_homework_blocking(subject: str, class_type: str, target_date: datetime.date = None, *,
                                   new_text: str = None, delete_text: bool = False,
                                   new_attachment: dict = None, delete_attachment: bool = False) -> tuple[int, list]:
    """
    Блокирующая функция для обновления ДЗ для ВСЕХ зарегистрированных пользователей.
    """
    updated_count = 0
    failed_users = []

    # --- НОВАЯ ЛОГИКА: Ищем всех пользователей ---
    try:
        # Сканируем папку с токенами, чтобы найти ID всех пользователей
        user_ids = [
            int(f.split('_')[1].split('.')[0])
            for f in os.listdir(auth_web.TOKEN_DIR)
            if f.startswith('token_') and f.endswith('.json')
        ]
        if not user_ids:
            logger.warning("Не найдено ни одного файла с токеном в папке tokens.")
            return 0, []
    except (FileNotFoundError, IndexError):
        logger.error(f"Папка с токенами {auth_web.TOKEN_DIR} не найдена.")
        return 0, []

    class_color_id = config.COLOR_MAP.get(class_type)

    # --- НОВАЯ ЛОГИКА: Запускаем цикл по всем пользователям ---
    for user_id in user_ids:
        try:
            service = get_calendar_service(user_id)
            if not service:
                failed_users.append(str(user_id))
                continue

            # Определяем время поиска
            time_min, time_max, order_by = None, None, None
            if target_date:
                time_min = datetime.datetime.combine(target_date, datetime.time.min).isoformat() + 'Z'
                time_max = datetime.datetime.combine(target_date, datetime.time.max).isoformat() + 'Z'
            else:
                # Ищем начиная с текущего момента
                time_min = datetime.datetime.now(datetime.UTC).isoformat()
                order_by = 'startTime'

            # Ищем событие в календаре текущего пользователя
            events = service.events().list(
                calendarId='primary', timeMin=time_min, timeMax=time_max,
                singleEvents=True, orderBy=order_by, maxResults=250
            ).execute().get('items', [])

            found_event = None
            for event in events:
                event_summary = event.get('summary', '')
                match = re.search(r'^(.*?)\s\(', event_summary)
                event_subject = match.group(1).strip() if match else ''
                if event_subject == subject and event.get('colorId') == class_color_id:
                    found_event = event
                    break

            if found_event:
                # Определяем, каким будет новый текст
                final_text = "" if delete_text else new_text if new_text is not None else extract_homework_part(
                    found_event.get('description', ''), config.GROUP_HOMEWORK_DESC_TAG)

                # Определяем, каким будет новое вложение
                final_attachment = None if delete_attachment else new_attachment if new_attachment is not None else (
                    found_event.get('attachments', [None])[0])

                save_homework_to_event(
                    event=found_event, service=service, homework_text=final_text,
                    attachment_data=final_attachment, is_group_hw=True
                )
                updated_count += 1
            else:
                logger.warning(f"Событие '{subject}' для user_id {user_id} на дату '{target_date}' не найдено.")

        except Exception as e:
            logger.error(f"Критическая ошибка при обновлении ДЗ для user_id {user_id}: {e}")
            failed_users.append(str(user_id))

    return updated_count, failed_users

async def group_homework_start(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Введите текст ДЗ для всей группы:")
    # Устанавливаем флаг, что это групповое ДЗ
    context.user_data['is_group_hw'] = True
    return GET_GROUP_HW_TEXT


async def find_next_class_for_group_hw_text(update: Update, context: CallbackContext) -> int:
    """Находит следующее занятие и обновляет текст ДЗ для группы."""
    query = update.callback_query
    await query.answer()

    subject = context.user_data.get('group_homework_subject')
    class_type = context.user_data.get('hw_type', 'Семинар')
    homework_text = context.user_data.get('group_homework_text')

    await query.edit_message_text(f"Начинаю поиск занятия и обновление текста ДЗ для группы...")

    updated_count, _ = await asyncio.to_thread(
        update_group_homework_blocking, subject, class_type, target_date=None, new_text=homework_text
    )

    await query.edit_message_text(
        f"✅ Текст ДЗ для '{subject}' для следующего занятия обновлен у {updated_count} пользователей."
    )

    # ИЗМЕНЕНИЕ: Вызываем главное меню после подтверждения
    await main_menu(update, context, force_new_message=True)

    context.user_data.clear()
    return ConversationHandler.END


async def find_next_class_for_group_hw_file(update: Update, context: CallbackContext) -> int:
    """Находит следующее занятие и добавляет файл для группы."""
    query = update.callback_query
    await query.answer()

    subject = context.user_data.get('homework_subject')

    # --- ИСПРАВЛЕНИЕ ЗДЕСЬ ---
    # Было: context.user_a
    # Стало: context.user_data
    class_type = context.user_data.get('hw_type', 'Семинар')

    file_bytes = context.user_data.get('file_bytes')
    file_name = context.user_data.get('file_name')

    # --- ИЗМЕНЕНИЕ: Получаем ID админа для загрузки файла на его диск ---
    user_id = update.effective_user.id

    await query.edit_message_text(f"Загружаю файл и ищу следующее занятие для группы...")

    # --- ИЗМЕНЕНИЕ: Передаем ID админа в функцию загрузки ---
    attachment_info = await asyncio.to_thread(upload_file_to_drive, user_id, file_name, file_bytes)
    if not attachment_info:
        await query.edit_message_text("Не удалось загрузить файл на Google Drive.")
        context.user_data.clear()
        return ConversationHandler.END

    updated_count, _ = await asyncio.to_thread(
        update_group_homework_blocking, subject, class_type, target_date=None, new_attachment=attachment_info
    )

    await query.edit_message_text(
        f"✅ Файл для '{subject}' для следующего занятия прикреплен у {updated_count} пользователей."
    )

    await main_menu(update, context, force_new_message=True)

    context.user_data.clear()
    return ConversationHandler.END

async def get_group_hw_text(update: Update, context: CallbackContext) -> int:
    context.user_data['group_homework_text'] = update.message.text
    subjects = sorted(list(set(l['subject'] for d in config.SCHEDULE_DATA.values() for w in d.values() for l in w)))

    # Добавляем кнопку для лабораторной
    if "Теоретические основы информатики" in subjects:
        subjects.append("Лабораторная: Теоретические основы информатики")
        subjects.sort()

    context.user_data['subjects_list'] = subjects
    buttons = [[InlineKeyboardButton(name, callback_data=f"group_hw_subj_{i}")] for i, name in enumerate(subjects)]
    await update.message.reply_text("Выберите предмет:", reply_markup=InlineKeyboardMarkup(buttons))
    return CHOOSE_GROUP_HW_SUBJECT


async def choose_group_hw_subject(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    subject_index = int(query.data.split('_')[-1])
    selected_item = context.user_data['subjects_list'][subject_index]

    # Распознаем специальную кнопку и устанавливаем тип
    if selected_item == "Лабораторная: Теоретические основы информатики":
        context.user_data['group_homework_subject'] = "Теоретические основы информатики"
        context.user_data['hw_type'] = "Лабораторные работы"
        button_text = "На следующую лабораторку"
    else:
        context.user_data['group_homework_subject'] = selected_item
        context.user_data['hw_type'] = "Семинар"
        button_text = "На следующий семинар"

    keyboard = [[InlineKeyboardButton(button_text, callback_data="find_next_class_group_text")]]
    await query.edit_message_text(
        "Куда записать ДЗ для группы? Введите дату (в формате ДД.ММ) или выберите опцию:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return CHOOSE_GROUP_HW_DATE_OPTION


async def find_next_seminar_for_group(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()

    class_type = context.user_data.get('hw_type', 'Семинар')
    await query.edit_message_text(f"Начинаю запись ДЗ для группы на занятие «{class_type}». Это может занять время...")

    subject = context.user_data.get('group_homework_subject')
    homework_text = context.user_data.get('group_homework_text')

    # Передаем class_type в блокирующую функцию
    updated_count, failed_users = await asyncio.to_thread(
        find_and_update_or_delete_group_hw_blocking, subject, homework_text, class_type
    )

    result_text = f"Запись ДЗ для группы завершена.\n\n✅ Успешно обновлено у {updated_count} пользователей."
    if failed_users:
        result_text += f"\n❌ Не удалось обновить для {len(failed_users)} пользователей."

    await query.edit_message_text(
        result_text,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« В главное меню", callback_data="main_menu")]])
    )
    context.user_data.clear()
    return ConversationHandler.END


async def get_manual_date_for_group_hw(update: Update, context: CallbackContext) -> int:
    """Обрабатывает ручной ввод даты для записи группового ДЗ."""
    class_type = context.user_data.get('hw_type', 'Семинар')
    await update.message.reply_text(f"Проверяю наличие занятия «{class_type}»... Это может занять время.")

    subject = context.user_data.get('group_homework_subject')
    homework_text = context.user_data.get('group_homework_text')

    try:
        day, month = map(int, update.message.text.split('.'))
        target_date = datetime.date(datetime.date.today().year, month, day)
    except (ValueError, IndexError):
        await update.message.reply_text("Неверный формат. Введите дату как ДД.ММ")
        return CHOOSE_GROUP_HW_DATE_OPTION

    # Передаем class_type и target_date в блокирующую функцию
    updated_count, _ = await asyncio.to_thread(
        find_and_update_or_delete_group_hw_blocking, subject, homework_text, class_type, target_date
    )

    if updated_count > 0:
        await update.message.reply_text(
            f"Групповое ДЗ для '{subject}' ({class_type.lower()}) на {target_date.strftime('%d.%m.%Y')} записано для {updated_count} пользователей.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« В главное меню", callback_data="main_menu")]])
        )
    else:
        await update.message.reply_text(
            f"Не удалось найти занятия типа «{class_type}» по предмету '{subject}' на {target_date.strftime('%d.%m.%Y')} ни у одного пользователя."
        )
        return CHOOSE_GROUP_HW_DATE_OPTION

    context.user_data.clear()
    return ConversationHandler.END


async def send_main_menu_on_auth_success(context: CallbackContext):
    """Отправляет главное меню после успешной авторизации через веб."""
    user_id = context.job.data

    if context.user_data:
        context.user_data.clear()

    keyboard = [
        [InlineKeyboardButton("Управление расписанием", callback_data="schedule_menu")],
        [InlineKeyboardButton("Управление ДЗ", callback_data="homework_management_menu")],
        [InlineKeyboardButton("Выйти и сменить аккаунт", callback_data="logout")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await context.bot.send_message(
        chat_id=user_id,
        text="Авторизация прошла успешно! Теперь вы можете управлять своим расписанием.",
        reply_markup=reply_markup
    )


async def http_auth_callback(request):
    """Обрабатывает входящий HTTP-запрос об успешной авторизации."""
    try:
        data = await request.json()
        user_id = data.get('user_id')
        if not user_id:
            return web.Response(status=400, text="user_id is required")

        logging.info(f"Получен HTTP-коллбэк об успешной авторизации для user_id: {user_id}")

        app_instance = request.app['bot_app']
        app_instance.job_queue.run_once(send_main_menu_on_auth_success, 0, data=user_id)

        return web.Response(status=200, text="OK")
    except Exception as e:
        logging.error(f"Ошибка в http_auth_callback: {e}")
        return web.Response(status=500, text="Internal Server Error")



async def test_button_press(update: Update, context: CallbackContext) -> None:
    """Тестовый обработчик для проверки нажатия кнопки."""
    print("!!!!!!!!!!!!!!!!! КНОПКА 'Записать ДЗ для группы' БЫЛА НАЖАТА !!!!!!!!!!!!!!!!!!")
    query = update.callback_query
    await query.answer("Тестовый обработчик сработал!", show_alert=True)


async def start_work_handler(update: Update, context: CallbackContext) -> None:
    """Показывает главное меню после нажатия на кнопку 'Начать работу'."""
    query = update.callback_query
    await query.answer()

    keyboard = [
        [InlineKeyboardButton("Управление расписанием", callback_data="schedule_menu")],
        [InlineKeyboardButton("Управление ДЗ", callback_data="homework_management_menu")],
        [InlineKeyboardButton("Выйти и сменить аккаунт", callback_data="logout")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = "Вы авторизованы. Выберите действие:"
    await query.edit_message_text(text, reply_markup=reply_markup)




async def homework_management_menu_dispatcher(update: Update, context: CallbackContext) -> int:
    """Отображает главное меню ДЗ с выбором 'Свое' или 'Групповое'."""
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id

    keyboard = [[InlineKeyboardButton("Мое ДЗ", callback_data="personal_hw_menu")]]
    # Если пользователь админ, добавляем кнопку для группового ДЗ
    if user_id in config.ADMIN_IDS or config.DEBUG_MODE:
        keyboard.append([InlineKeyboardButton("Групповое ДЗ", callback_data="group_hw_menu")])

    keyboard.append([InlineKeyboardButton("« Назад в главное меню", callback_data="main_menu")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text("Выберите тип домашнего задания:", reply_markup=reply_markup)

    return HOMEWORK_MENU



async def personal_homework_menu(update: Update, context: CallbackContext) -> int:
    """Отображает меню управления личным ДЗ."""
    query = update.callback_query
    await query.answer()

    keyboard = [
        [InlineKeyboardButton("✍️ Записать ДЗ", callback_data="homework_add_start")],
        [InlineKeyboardButton("📎 Добавить файл", callback_data="add_file_start")],
        [InlineKeyboardButton("✏️ Редактировать ДЗ", callback_data="homework_edit_start")],
        [InlineKeyboardButton("« Назад", callback_data="homework_management_menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        text="Действия с вашим личным домашним заданием:",
        reply_markup=reply_markup
    )
    return PERSONAL_HW_MENU


# --- НОВЫЕ ФУНКЦИИ ДЛЯ ПОТОКА "ДОБАВИТЬ ФАЙЛ" ---

async def add_file_start(update: Update, context: CallbackContext) -> int:
    """Начинает процесс добавления файла. Просит пользователя отправить файл."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "Пожалуйста, отправьте файл (документ или фото), который хотите прикрепить к занятию."
    )
    return GET_FILE_ONLY


async def get_file_only(update: Update, context: CallbackContext) -> int:
    """Получает файл, сохраняет его в памяти и запрашивает предмет."""
    message = update.message
    file_to_upload = None
    file_name = "файл от пользователя"

    if message.document:
        file_to_upload = await message.document.get_file()
        file_name = message.document.file_name
    elif message.photo:
        file_to_upload = await message.photo[-1].get_file()
        file_name = f"photo_{file_to_upload.file_unique_id}.jpg"

    if not file_to_upload:
        await message.reply_text("Не удалось обработать файл. Попробуйте еще раз.")
        # Возвращаемся в правильное состояние в зависимости от флага
        return GET_GROUP_FILE_ONLY if context.user_data.get('is_group_file') else GET_FILE_ONLY

    file_bytes = await file_to_upload.download_as_bytearray()
    context.user_data['file_bytes'] = bytes(file_bytes)
    context.user_data['file_name'] = file_name
    await message.reply_text("Файл получен!")

    subjects = sorted(list(set(l['subject'] for d in config.SCHEDULE_DATA.values() for w in d.values() for l in w)))
    if "Теоретические основы информатики" in subjects:
        subjects.append("Лабораторная: Теоретические основы информатики")
        subjects.sort()
    context.user_data['subjects_list'] = subjects

    # --- ИЗМЕНЕНИЕ: Создаем разные кнопки для личного и группового ДЗ ---
    is_group = context.user_data.get('is_group_file', False)
    callback_prefix = "group_file_subj_" if is_group else "file_subj_"
    next_state = CHOOSE_SUBJECT_FOR_GROUP_FILE if is_group else CHOOSE_SUBJECT_FOR_FILE

    buttons = [[InlineKeyboardButton(name, callback_data=f"{callback_prefix}{i}")] for i, name in enumerate(subjects)]
    await update.message.reply_text("К какому предмету прикрепить файл?", reply_markup=InlineKeyboardMarkup(buttons))

    # Сбрасываем флаг после использования
    if is_group:
        context.user_data.pop('is_group_file')

    return next_state


async def choose_subject_for_file(update: Update, context: CallbackContext) -> int:
    """Сохраняет предмет для файла и запрашивает дату."""
    query = update.callback_query
    await query.answer()
    subject_index = int(query.data.split('_')[-1])
    selected_item = context.user_data['subjects_list'][subject_index]

    if selected_item == "Лабораторная: Теоретические основы информатики":
        context.user_data['homework_subject'] = "Теоретические основы информатики"
        context.user_data['hw_type'] = "Лабораторные работы"
        button_text = "На следующую лабораторку"
    else:
        context.user_data['homework_subject'] = selected_item
        context.user_data['hw_type'] = "Семинар"
        button_text = "На следующий семинар"

    keyboard = [[InlineKeyboardButton(button_text, callback_data="find_next_class_for_file")]]
    await query.edit_message_text(
        "Введите дату (ДД.ММ) или выберите опцию:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return CHOOSE_DATE_FOR_FILE

#--------

def upload_textbook_to_shared_drive(user_id: int, file_name: str, file_bytes: bytes) -> dict | None:
    """
    Загружает файл учебника в ОБЩУЮ папку на Google Drive и возвращает информацию о файле.
    """
    drive_service = get_drive_service(user_id)
    if not drive_service:
        logger.error(f"Не удалось создать сервис Google Drive для user_id: {user_id}")
        return None

    try:
        # ID общей папки для учебников берется из конфига
        folder_id = config.TEXTBOOKS_DRIVE_FOLDER_ID

        print(f"--- DEBUG: Пытаюсь загрузить в папку с ID: '{folder_id}' ---")

        file_metadata = {'name': file_name, 'parents': [folder_id]}
        media = MediaIoBaseUpload(BytesIO(file_bytes), mimetype='application/pdf', resumable=True)

        file = drive_service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id, name'  # Запрашиваем только ID и имя
        ).execute()

        file_id = file.get('id')

        # Важно: Даем права на чтение всем, у кого есть ссылка
        drive_service.permissions().create(fileId=file_id, body={'type': 'anyone', 'role': 'reader'}).execute()

        logger.info(f"Учебник '{file_name}' успешно загружен на Google Drive с ID: {file_id}")
        return {'fileId': file_id, 'fileName': file.get('name')}

    except Exception as e:
        logger.error(f"Ошибка при загрузке учебника на Google Drive: {e}")
        return None

async def add_textbook_wrong_file(update: Update, context: CallbackContext) -> None:
    """Сообщает пользователю, что он отправил не тот тип файла."""
    await update.message.reply_text(
        "❌ Это не PDF-файл. Пожалуйста, отправьте учебник именно в формате PDF."
    )
# -------------------------
async def choose_subject_for_group_file(update: Update, context: CallbackContext) -> int:
    """Сохраняет предмет для группового файла и запрашивает дату."""
    query = update.callback_query
    await query.answer()
    subject_index = int(query.data.split('_')[-1])
    selected_item = context.user_data['subjects_list'][subject_index]

    if selected_item == "Лабораторная: Теоретические основы информатики":
        context.user_data['homework_subject'] = "Теоретические основы информатики"
        context.user_data['hw_type'] = "Лабораторные работы"
        button_text = "На следующую лабораторку"
    else:
        context.user_data['homework_subject'] = selected_item
        context.user_data['hw_type'] = "Семинар"
        button_text = "На следующий семинар"

    # --- ИЗМЕНЕНИЕ: Указываем правильные callback_data и состояние для ГРУППЫ ---
    keyboard = [[InlineKeyboardButton(button_text, callback_data="find_next_class_for_group_file")]]
    await query.edit_message_text(
        "Введите дату (ДД.ММ) или выберите опцию:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return CHOOSE_DATE_FOR_GROUP_FILE

async def save_file_to_event_logic(update: Update, context: CallbackContext, event: dict, user_id: int) -> int:
    """Общая логика загрузки файла и сохранения его в событие для конкретного пользователя."""
    # --- ИЗМЕНЕНИЕ: Получаем сервисы для конкретного пользователя ---
    service = get_calendar_service(user_id)
    drive_service = get_drive_service(user_id) # drive_service здесь больше не создается, но проверка не помешает

    file_bytes = context.user_data.get('file_bytes')
    file_name = context.user_data.get('file_name')
    subject = context.user_data.get('homework_subject')

    if not all([service, drive_service, file_bytes, file_name, subject, event]):
        await update.effective_message.reply_text("Произошла внутренняя ошибка, не хватает данных. Попробуйте снова.")
        context.user_data.clear()
        return ConversationHandler.END

    message_to_edit = None
    if update.callback_query:
        message_to_edit = await update.callback_query.edit_message_text(f"Загружаю файл '{file_name}' на ваш Google Drive...")
    else:
        message_to_edit = await update.message.reply_text(f"Загружаю файл '{file_name}' на ваш Google Drive...")

    # --- ИЗМЕНЕНИЕ: Передаем user_id в функцию загрузки ---
    attachment_info = await asyncio.to_thread(
        upload_file_to_drive, user_id, file_name, file_bytes
    )

    if not attachment_info:
        await message_to_edit.edit_text(
            "Не удалось загрузить файл на Google Drive. Проверьте, включен ли Drive API в Google Console."
        )
        context.user_data.clear()
        return ConversationHandler.END

    existing_description = event.get('description', '')
    existing_hw_text = extract_homework_part(existing_description, config.PERSONAL_HOMEWORK_DESC_TAG)

    save_homework_to_event(
        event=event,
        service=service,
        homework_text=existing_hw_text,
        attachment_data=attachment_info
    )

    event_date_str = event['start'].get('dateTime', event['start'].get('date'))
    event_date = datetime.datetime.fromisoformat(event_date_str).strftime('%d.%m.%Y')
    await message_to_edit.edit_text(
        f'Готово! Файл для "{subject}" прикреплен к занятию на {event_date}.'
    )
    await main_menu(update, context, force_new_message=True)
    context.user_data.clear()
    return ConversationHandler.END

async def find_next_class_for_file(update: Update, context: CallbackContext) -> int:
    """Ищет следующее занятие для прикрепления файла."""
    query = update.callback_query
    user_id = update.effective_user.id # <-- ДОБАВЛЕНО

    # --- ИЗМЕНЕНИЕ: Получаем сервис для конкретного пользователя ---
    service = get_calendar_service(user_id)
    if not service:
        await query.answer("Ошибка авторизации.", show_alert=True)
        return ConversationHandler.END

    await query.answer()

    class_type = context.user_data.get('hw_type', 'Семинар')
    subject = context.user_data.get('homework_subject')
    class_color_id = config.COLOR_MAP.get(class_type)

    await query.edit_message_text(f"Ищу ближайшее занятие «{class_type}» по предмету '{subject}'...")

    search_start_time = datetime.datetime.now(datetime.UTC).isoformat()

    try:
        events_result = service.events().list(
            calendarId='primary', timeMin=search_start_time, singleEvents=True,
            orderBy='startTime', maxResults=250
        ).execute()
        events = events_result.get('items', [])
        for event in events:
            event_summary = event.get('summary', '')
            match = re.search(r'^(.*?)\s\(', event_summary)
            event_subject = match.group(1).strip() if match else ''
            if event_subject == subject and event.get('colorId') == class_color_id:
                # --- ИЗМЕНЕНИЕ: Передаем user_id в логику сохранения ---
                return await save_file_to_event_logic(update, context, event, user_id)

        await query.edit_message_text(f"Не нашел предстоящих занятий типа «{class_type}» по предмету '{subject}'.")
        return CHOOSE_DATE_FOR_FILE
    except Exception as e:
        logger.error(f"Error finding next class for file: {e}", exc_info=True)
        await query.edit_message_text(f"Произошла ошибка: {e}")
        context.user_data.clear()
        return ConversationHandler.END


async def get_manual_date_for_file(update: Update, context: CallbackContext) -> int:
    """Обрабатывает ручной ввод даты и запускает логику сохранения файла."""
    user_id = update.effective_user.id # <-- ДОБАВЛЕНО

    # --- ИЗМЕНЕНИЕ: Получаем сервис для конкретного пользователя ---
    service = get_calendar_service(user_id)
    if not service:
        await update.message.reply_text("Ошибка авторизации.")
        return ConversationHandler.END

    try:
        day, month = map(int, update.message.text.split('.'))
        target_date = datetime.date(datetime.date.today().year, month, day)
    except (ValueError, IndexError):
        await update.message.reply_text("Неверный формат. Введите дату как ДД.ММ")
        return CHOOSE_DATE_FOR_FILE

    subject = context.user_data.get('homework_subject')
    class_type = context.user_data.get('hw_type', 'Семинар')
    class_color_id = config.COLOR_MAP.get(class_type)
    time_min = datetime.datetime.combine(target_date, datetime.time.min).isoformat() + 'Z'
    time_max = datetime.datetime.combine(target_date, datetime.time.max).isoformat() + 'Z'

    try:
        events = service.events().list(calendarId='primary', timeMin=time_min, timeMax=time_max,
                                       singleEvents=True).execute().get('items', [])
        found_event = None
        for event in events:
            event_summary = event.get('summary', '')
            match = re.search(r'^(.*?)\s\(', event_summary)
            event_subject = match.group(1).strip() if match else ''
            if event_subject == subject and event.get('colorId') == class_color_id:
                found_event = event
                break

        if not found_event:
            await update.message.reply_text(
                f"На {target_date.strftime('%d.%m.%Y')} не найдено занятий типа «{class_type}» по предмету '{subject}'.")
            return CHOOSE_DATE_FOR_FILE

        # --- ИЗМЕНЕНИЕ: Передаем user_id в логику сохранения ---
        return await save_file_to_event_logic(update, context, found_event, user_id)

    except Exception as e:
        logger.error(f"Error with manual date for file: {e}", exc_info=True)
        await update.message.reply_text(f"Произошла ошибка: {e}")
        return ConversationHandler.END


# --- ЛОГИКА СОЗДАНИЯ КОНСПЕКТА (STUDENT) ---

async def summary_start(update: Update, context: CallbackContext) -> int:
    """Начинает диалог создания конспекта. Запрашивает предмет."""
    query = update.callback_query
    await query.answer()

    # Берем список предметов из конфига
    subjects = sorted(list(set(l['subject'] for d in config.SCHEDULE_DATA.values() for w in d.values() for l in w)))
    context.user_data['subjects_list'] = subjects

    # Создаем кнопки для каждого предмета
    buttons = [[InlineKeyboardButton(name, callback_data=f"summary_subj_{i}")] for i, name in enumerate(subjects)]

    buttons.append([InlineKeyboardButton("⏪ В главное меню", callback_data="main_menu")])

    await query.edit_message_text(
        "🤖 По какому предмету готовим конспект? Выберите из списка:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

    # Переходим в состояние ожидания выбора предмета
    return CHOOSE_SUMMARY_SUBJECT

def find_next_homework_event(user_id: int, subject: str) -> dict | None:
    """Ищет в календаре следующее событие с ДЗ по предмету."""
    service = get_calendar_service(user_id)
    if not service:
        return None

    now = datetime.datetime.now(datetime.UTC).isoformat()
    try:
        events_result = service.events().list(
            calendarId='primary', timeMin=now, maxResults=100,
            singleEvents=True, orderBy='startTime'
        ).execute()
        events = events_result.get('items', [])

        for event in events:
            summary = event.get('summary', '')
            # Ищем по названию предмета и по тегу !!!ДЗ!!!
            if subject in summary and config.HOMEWORK_TITLE_TAG in summary:
                return event
        return None
    except Exception as e:
        logger.error(f"Ошибка при поиске ДЗ в календаре: {e}")
        return None

async def summary_choose_subject(update: Update, context: CallbackContext) -> int:
    """
    Главная функция: ищет ДЗ в календаре, затем ищет учебники в БД
    и предлагает студенту выбор.
    """
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id

    subject_index = int(query.data.split('_')[-1])
    subject = context.user_data['subjects_list'][subject_index]
    context.user_data['selected_subject'] = subject

    await query.edit_message_text(f"Ищу ближайшее ДЗ по предмету '{subject}'...")

    # --- 1. Ищем ближайшее ДЗ в Google Календаре ---
    homework_event = await asyncio.to_thread(find_next_homework_event, user_id, subject)

    # --- НОВЫЙ БЛОК: ИЗВЛЕКАЕМ ТЕКСТ ДЗ ---
    homework_text = ""
    if homework_event and homework_event.get('description'):
        description = homework_event.get('description', '')
        # Извлекаем и общее, и личное ДЗ
        group_hw = extract_homework_part(description, config.GROUP_HOMEWORK_DESC_TAG)
        personal_hw = extract_homework_part(description, config.PERSONAL_HOMEWORK_DESC_TAG)

        if group_hw:
            homework_text += f"Общее ДЗ: {group_hw}\n"
        if personal_hw:
            homework_text += f"Личное ДЗ: {personal_hw}\n"

    # Сохраняем найденный текст ДЗ для будущих шагов
    context.user_data['homework_text'] = homework_text.strip()
    # --- КОНЕЦ НОВОГО БЛОКА ---

    # --- 2. Сценарий А: У ДЗ есть прикрепленный файл ---
    if homework_event and homework_event.get('attachments'):
        attachment = homework_event['attachments'][0]
        context.user_data['calendar_attachment'] = attachment

        keyboard = [
            [InlineKeyboardButton("✅ Да, используем его", callback_data="use_calendar_file")],
            [InlineKeyboardButton("📚 Выбрать другой учебник", callback_data="pick_another_book")],
            [InlineKeyboardButton("⏪ В главное меню", callback_data="main_menu")]
        ]
        await query.edit_message_text(
            f"К этому ДЗ уже прикреплен файл: 📎 `{attachment['title']}`\n\nДелаем конспект по нему?",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
        return CHOOSE_SUMMARY_FILE

    # --- 3. Сценарий Б: Файла нет, ищем учебники в нашей базе данных ---
    textbooks = get_textbooks_by_subject(subject)

    if not textbooks:
        await query.edit_message_text(f"Учебники по предмету '{subject}' еще не добавлены в базу.")
        context.user_data.clear()
        return ConversationHandler.END

    context.user_data['db_textbooks'] = textbooks
    buttons = [
        [InlineKeyboardButton(f"📖 {book['file_name']}", callback_data=f"use_db_book_{i}")]
        for i, book in enumerate(textbooks)

    ]
    buttons.append([InlineKeyboardButton("⏪ В главное меню", callback_data="main_menu")])
    await query.edit_message_text(
        "К ДЗ не прикреплен файл. Выберите учебник из базы:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )
    return CHOOSE_SUMMARY_FILE


async def summary_pick_another_book(update: Update, context: CallbackContext) -> int:
    """Показывает список учебников из базы, если пользователь отказался от файла из календаря."""
    query = update.callback_query
    await query.answer()

    subject = context.user_data.get('selected_subject')
    textbooks = get_textbooks_by_subject(subject)

    if not textbooks:
        await query.edit_message_text(f"Учебники по предмету '{subject}' еще не добавлены в базу.")
        context.user_data.clear()
        return ConversationHandler.END

    context.user_data['db_textbooks'] = textbooks
    buttons = [
        [InlineKeyboardButton(f"📖 {book['file_name']}", callback_data=f"use_db_book_{i}")]
        for i, book in enumerate(textbooks)
    ]
    buttons.append([InlineKeyboardButton("⏪ В главное меню", callback_data="main_menu")])
    await query.edit_message_text(
        "Выберите учебник из базы:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )
    return CHOOSE_SUMMARY_FILE


async def summary_file_chosen(update: Update, context: CallbackContext) -> int:
    """
    Обрабатывает выбор файла (из календаря или из базы) и запрашивает номера страниц.
    """
    query = update.callback_query
    await query.answer()

    context.user_data['chosen_file_callback'] = query.data

    # --- НОВЫЙ БЛОК: ФОРМИРУЕМ СООБЩЕНИЕ С ТЕКСТОМ ДЗ ---
    homework_text = context.user_data.get('homework_text')

    message_text = "Отлично. Теперь введите номера страниц для анализа (например: 45, 48-51)."

    if homework_text:
        # Если ДЗ было найдено, добавляем его в сообщение
        message_text = (
            f"Текущее задание:\n"
            f"```\n{homework_text}\n```\n\n"
            f"Теперь введите номера страниц для анализа (например: 45, 48-51)."
        )

    await query.edit_message_text(
        text=message_text,
        parse_mode='Markdown'  # Добавляем parse_mode для отображения ```
    )
    # --- КОНЕЦ НОВОГО БЛОКА ---

    return GET_PAGE_NUMBERS


async def summary_get_pages(update: Update, context: CallbackContext) -> int:
    """Получает и парсит номера страниц, запрашивает финальное подтверждение."""
    page_input = update.message.text

    # Пытаемся обработать введенные страницы
    try:
        pages_to_process = []
        # Разделяем ввод по запятым (например, "45, 48-51")
        parts = [part.strip() for part in page_input.split(',')]
        for part in parts:
            if '-' in part:
                # Если это диапазон (например, "48-51")
                start, end = map(int, part.split('-'))
                if start > end:
                    raise ValueError("Начальная страница диапазона больше конечной.")
                pages_to_process.extend(range(start, end + 1))
            else:
                # Если это одно число
                pages_to_process.append(int(part))

        # Убираем дубликаты и сортируем
        pages_to_process = sorted(list(set(pages_to_process)))

        if not pages_to_process:
            raise ValueError("Не указаны страницы.")

    except ValueError as e:
        await update.message.reply_text(
            f"❌ Неверный формат. Пожалуйста, введите номера страниц правильно (например: 45, 48-51).\nОшибка: {e}"
        )
        return GET_PAGE_NUMBERS  # Остаемся в том же состоянии

    # Сохраняем страницы и их строковое представление для вывода
    context.user_data['pages_to_process'] = pages_to_process
    context.user_data['pages_str'] = ", ".join(map(str, pages_to_process))

    keyboard = [
        [InlineKeyboardButton("✅ Начать", callback_data="confirm_summary_yes")],
        [InlineKeyboardButton("❌ Отмена", callback_data="main_menu")]
    ]

    await update.message.reply_text(
        f"Готовлю конспект по страницам: {context.user_data['pages_str']}.\n\nНачать?",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    return CONFIRM_SUMMARY_GENERATION


def download_file_from_drive(user_id: int, file_id: str) -> bytes | None:
    """Скачивает файл с Google Drive по его ID и возвращает в виде байтов."""
    try:
        drive_service = get_drive_service(user_id)
        if not drive_service:
            return None

        request = drive_service.files().get_media(fileId=file_id)
        file_bytes = request.execute()
        return file_bytes
    except Exception as e:
        logger.error(f"Ошибка при скачивании файла {file_id} с Google Drive: {e}")
        return None


def generate_summary_placeholder(pdf_bytes: bytes, pages: list, subject: str) -> str:
    """
    ИМИТАЦИЯ работы ИИ.
    В будущем здесь будет реальная обработка и вызов GPT.
    """
    logger.info(f"Начата имитация генерации конспекта по предмету '{subject}' для страниц: {pages}")

    # Имитируем бурную деятельность
    time.sleep(5)

    summary_text = (
        f"🤖 *Конспект по предмету «{subject}»*\n\n"
        f"Обработаны страницы: {', '.join(map(str, pages))}.\n\n"
        "Здесь будет краткая выжимка основного теоретического материала "
        "со структурой, ключевыми терминами и основными тезисами. "
        "Реальная генерация с помощью GPT-4o будет добавлена на следующем этапе."
    )
    logger.info("Имитация генерации конспекта завершена.")
    return summary_text


def split_message(text: str, chunk_size: int = 4000) -> list[str]:
    """Делит длинный текст на части, не превышающие chunk_size."""
    if len(text) <= chunk_size:
        return [text]

    chunks = []
    # Умное разделение по абзацам, строкам и словам, чтобы не рвать слова
    while len(text) > chunk_size:
        split_pos = text.rfind('\n\n', 0, chunk_size)
        if split_pos == -1:
            split_pos = text.rfind('\n', 0, chunk_size)
        if split_pos == -1:
            split_pos = text.rfind(' ', 0, chunk_size)
        if split_pos == -1:
            split_pos = chunk_size

        chunks.append(text[:split_pos])
        text = text[split_pos:].lstrip()

    chunks.append(text)
    return chunks

async def summary_generate(update: Update, context: CallbackContext) -> int:
    """Финальный шаг: запускает скачивание, обработку и отправку результата."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Начинаю обработку... Это может занять минуту. ⏳")

    user_id = update.effective_user.id
    user_data = context.user_data
    file_id_to_download = None

    # --- 1. Определяем, какой файл был выбран ---
    callback_data = user_data.get('chosen_file_callback')

    if callback_data == 'use_calendar_file':
        attachment = user_data.get('calendar_attachment')
        file_id_to_download = attachment.get('fileId')

    elif callback_data and callback_data.startswith('use_db_book_'):
        book_index = int(callback_data.split('_')[-1])
        textbook = user_data['db_textbooks'][book_index]
        file_id_to_download = textbook.get('file_id')

    if not file_id_to_download:
        await query.edit_message_text("❌ Не удалось определить файл для обработки. Попробуйте снова.")
        return ConversationHandler.END

    # --- 2. Скачиваем файл с Google Drive ---
    pdf_bytes = await asyncio.to_thread(download_file_from_drive, user_id, file_id_to_download)

    if not pdf_bytes:
        await query.edit_message_text("❌ Ошибка при скачивании файла с Google Drive.")
        return ConversationHandler.END

    # --- 3. Вызываем 'ИИ' для генерации конспекта (пока имитация) ---
    pages = user_data.get('pages_to_process')
    subject = user_data.get('selected_subject')
    homework_text = user_data.get('homework_text', '')
    # Достаем строковое представление страниц, которое мы сохранили ранее
    pages_str = user_data.get('pages_str', '')

    summary = await asyncio.to_thread(
        generate_summary_from_pdf, pdf_bytes, pages, subject, homework_text, user_id, pages_str
    )

    # --- 4. Отправляем результат с умной обработкой ошибок ---
    summary_chunks = split_message(summary)

    # Редактируем исходное сообщение, чтобы пользователь знал, что все готово
    await query.edit_message_text("✅ Конспект готов! Отправляю его...")

    # Отправляем все части по очереди
    for i, chunk in enumerate(summary_chunks):
        # Формируем клавиатуру только для последнего сообщения
        reply_markup = None
        if i == len(summary_chunks) - 1:
            keyboard = [[InlineKeyboardButton("⏪ В главное меню", callback_data="main_menu")]]
            reply_markup = InlineKeyboardMarkup(keyboard)

        try:
            # Попытка №1: Отправить с Markdown разметкой
            await context.bot.send_message(
                chat_id=user_id, text=chunk, reply_markup=reply_markup, parse_mode='Markdown'
            )
        except telegram.error.BadRequest as e:
            if 'Can\'t parse entities' in str(e):
                # Попытка №2: Если Markdown сломан, отправить как простой текст
                logger.warning("Ошибка парсинга Markdown. Отправляю как обычный текст.")
                await context.bot.send_message(
                    chat_id=user_id, text=chunk, reply_markup=reply_markup
                )
            else:
                # Если ошибка другая (не связана с разметкой), сообщаем о ней
                logger.error(f"Не удалось отправить часть конспекта: {e}")
                await context.bot.send_message(chat_id=user_id, text=f"Произошла ошибка при отправке части конспекта.")

    user_data.clear()
    return ConversationHandler.END


def generate_summary_from_pdf(pdf_bytes: bytes, pages: list, subject: str, homework_text: str, user_id: int, pages_str: str) -> str:
    """
    Извлекает страницы из PDF, конвертирует их в изображения и отправляет в GPT-4o
    для создания конспекта.
    """
    try:
        # --- 1. Инициализация клиента OpenAI ---
        client = OpenAI(api_key=config.OPENAI_API_KEY)

        # --- 2. Извлечение и обработка страниц из PDF ---
        pdf_document = fitz.open(stream=pdf_bytes, filetype="pdf")
        base64_images = []

        for page_num in pages:
            # Номера страниц от пользователя 1-основанные, в PyMuPDF 0-основанные
            page = pdf_document.load_page(page_num - 1)

            # Конвертируем страницу в изображение
            pix = page.get_pixmap(dpi=150)  # DPI можно регулировать для качества/скорости
            img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)

            # Оптимизируем и кодируем в base64
            buffer = io.BytesIO()
            img.save(buffer, format="JPEG", quality=75)  # Сжимаем для экономии
            base64_image = base64.b64encode(buffer.getvalue()).decode('utf-8')
            base64_images.append(base64_image)

        if not base64_images:
            return "Не удалось извлечь страницы из файла."

        # --- 3. Формирование промпта и запроса к GPT-4o ---
        hw_prompt_part = ""
        if homework_text:
            hw_prompt_part = (
                f"Особое внимание удели аспектам, связанным с домашним заданием:\n"
                f"'''\n{homework_text}\n'''\n\n"
            )
        prompt_text = (
            f"Ты — AI-ассистент для студента МГТУ им. Баумана. Твоя цель — понять на какую тему задания и составить маленький"
            f"структурированный и понятный конспект из теории по материалам, которые я тебе предоставил. Этот конспект должен помочь для решения заданий из материалов. "
            f"Тема: {subject}. \n\n"
            f"Задачи:\n"
            f"1. Не решай задания, а помоги понять что требуется сделать\n"
            f"2. Составить маленький конспект, для того что бы пользователь мог вспомнить теорию для заданий.\n"
            "Правила составления конспекта:\n"
            "1. Пиши только по-русски.\n"
            "2. ВАЖНО: Итоговый текст должен быть не длиннее 3000 символов, чтобы он поместился в одно сообщение Telegram.\n"
            "3. Не пиши ничего после конспекта. От тебя нужен только конспект.\n"
            "4. **Используй Telegram Markdown для форматирования:**\n"
            "   - **Заголовки** разделов выделяй жирным шрифтом (например, *Основные понятия*).\n"
            "   - *Ключевые термины* или важные мысли выделяй курсивом.\n"
            "   - ```Определения или формулы``` можно выделять моноширинным шрифтом (обратными кавычками).\n"
            "5. Активно используй списки для лучшей читаемости.\n"
            "6. Излагай только самую суть, без воды и лишних вступлений.\n"
            "7. В конце определи одну самую сложную или важную концепцию из предоставленного материала, которая заслуживает более глубокого изучения. Создай для нее готовый промпт, что бы пользователь мог его скопировать и самостоятельно вбить в GPT. Сверху укажи '**Готовый запрос в GPT:**'. В конце промпта и вначале пиши обратные кавычки(```). Например:(``` Объясни подробно Passive voice ```).\n\n"
            
            "Проанализируй изображения страниц и составь конспект по этим правилам."
        )

        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt_text},
                    # Добавляем все изображения страниц
                    *[
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img}"}}
                        for img in base64_images
                    ]
                ]
            }
        ]

        response = client.chat.completions.create(
            model="gpt-5-mini",
            messages=messages,
            max_completion_tokens=6000  # Ограничиваем длину ответа
        )

        # --- ДОБАВЛЯЕМ ЛОГИРОВАНИЕ ТОКЕНОВ ---
        if response.choices and response.choices[0].message.content:
            # Сначала получаем текст конспекта
            summary = response.choices[0].message.content.strip()

            #
            # ===> ВОТ МЕСТО ДЛЯ КОДА, О КОТОРОМ ТЫ СПРАШИВАЛ <===
            #
            # Затем, если есть данные об использовании токенов, логируем их
            if response.usage:
                logger.info(
                    f"Токены использованы: "
                    f"Входные (промпт + изображения): {response.usage.prompt_tokens}, "
                    f"Выходные (ответ): {response.usage.completion_tokens}, "
                    f"Всего: {response.usage.total_tokens}"
                )
                log_g_sheets(
                    user_id=user_id,
                    prompt_tokens=response.usage.prompt_tokens,
                    completion_tokens=response.usage.completion_tokens,
                    total_tokens=response.usage.total_tokens,
                    summary_text=summary,
                    subject=subject,
                    homework_text=homework_text,
                    pages_str=pages_str
                )

            # Возвращаем готовый и очищенный конспект
            return summary
        else:
            # Если ответа нет, возвращаем сообщение об ошибке
            logger.warning(
                f"Ответ от OpenAI не содержит текста. Finish reason: {response.choices[0].finish_reason if response.choices else 'N/A'}")
            return "Не удалось получить конспект от AI. Ответ от нейросети был пустым."

    except Exception as e:
        logger.error(f"Критическая ошибка при генерации конспекта: {e}")
        error_text = f"❌ Произошла ошибка при обращении к AI:\n\n```\n{e}\n```"
        return error_text




#Напоминание админам о записи дз




async def send_homework_reminder(context: CallbackContext):
    """Отправляет напоминание о записи ДЗ всем админам."""
    job_context = context.job.data
    subject = job_context['subject']

    keyboard = [
        # Мы добавим специальный префикс, чтобы потом поймать эту кнопку
        [InlineKeyboardButton("✍️ Записать ДЗ", callback_data=f"reminder_add_hw_{subject}")],
        [InlineKeyboardButton("❌ Не записывать", callback_data="reminder_ignore")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    message_text = f"🔔 Напоминание: через 5 минут закончится семинар.\nНе забудь записать ДЗ по предмету «{subject}»."

    # Отправляем сообщение каждому админу
    for admin_id in config.ADMIN_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_id, text=message_text, reply_markup=reply_markup
            )
        except Exception as e:
            logger.error(f"Не удалось отправить напоминание админу {admin_id}: {e}")


async def check_seminars_and_schedule_reminders(context: CallbackContext):
    """
    Проверяет календари всех пользователей на наличие ближайших семинаров
    и планирует отправку напоминаний для админов.
    """
    bot_data = context.bot_data
    if 'scheduled_reminders' not in bot_data:
        bot_data['scheduled_reminders'] = set()

    # Ищем всех пользователей по файлам токенов
    try:
        user_ids = [
            int(f.split('_')[1].split('.')[0])
            for f in os.listdir(auth_web.TOKEN_DIR) if f.startswith('token_')
        ]
    except (FileNotFoundError, IndexError):
        user_ids = []

    now = datetime.datetime.now(datetime.timezone.utc)
    # Ищем семинары в ближайшие 30 минут
    time_max = now + datetime.timedelta(minutes=30)

    unique_seminars = set()

    for user_id in user_ids:
        service = get_calendar_service(user_id)
        if not service:
            continue

        try:
            events = service.events().list(
                calendarId='primary', timeMin=now.isoformat(), timeMax=time_max.isoformat(),
                singleEvents=True, orderBy='startTime'
            ).execute().get('items', [])

            for event in events:
                # Ищем только семинары
                if event.get('colorId') == config.COLOR_MAP.get("Семинар"):
                    end_time_str = event['end'].get('dateTime')
                    event_id = event['id']

                    if not end_time_str:
                        continue

                    end_time = datetime.datetime.fromisoformat(end_time_str)
                    # Ключ для уникальности: ID события + дата (на случай, если это серия)
                    seminar_key = f"{event_id}_{end_time.date()}"
                    unique_seminars.add((seminar_key, end_time, event.get('summary')))

        except Exception as e:
            logger.warning(f"Ошибка при получении событий для user_id {user_id}: {e}")

    # Планируем напоминания для уникальных семинаров
    for key, end_time, summary in unique_seminars:
        reminder_time = end_time - datetime.timedelta(minutes=5)

        # Планируем, только если время еще не прошло и напоминание не было запланировано ранее
        if reminder_time > now and key not in bot_data['scheduled_reminders']:
            # Извлекаем чистое название предмета
            match = re.search(r'^(.*?)\s\(', summary)
            subject = match.group(1).strip() if match else summary.strip()

            context.job_queue.run_once(
                send_homework_reminder,
                reminder_time,
                data={'subject': subject},
                name=key
            )
            bot_data['scheduled_reminders'].add(key)
            logger.info(f"Запланировано напоминание по предмету '{subject}' на {reminder_time.strftime('%H:%M:%S')}")


async def reminder_ignore(update: Update, context: CallbackContext):
    """Обрабатывает нажатие кнопки 'Не записывать', удаляя сообщение."""
    query = update.callback_query
    await query.answer()
    await query.delete_message()


async def reminder_add_hw_start(update: Update, context: CallbackContext) -> int:
    """
    Начинает диалог добавления ДЗ из напоминания, пропуская шаг выбора предмета.
    """
    query = update.callback_query
    await query.answer()

    # Извлекаем предмет из данных кнопки (например, из 'reminder_add_hw_Матанализ')
    subject = query.data.replace("reminder_add_hw_", "")

    # Сразу сохраняем предмет в состояние диалога
    context.user_data['group_homework_subject'] = subject
    # Устанавливаем тип 'Семинар', т.к. напоминания только для них
    context.user_data['hw_type'] = "Семинар"

    keyboard = [[InlineKeyboardButton("На следующий семинар", callback_data="find_next_class_group_text")]]
    await query.edit_message_text(
        text=f"✍️ **Запись ДЗ по предмету «{subject}»**\n\n"
             "Введите дату (в формате ДД.ММ) или выберите опцию:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )

    # Сразу переходим ко второму шагу диалога - ожиданию даты
    return CHOOSE_GROUP_HW_DATE_OPTION

# --- Главная функция и запуск ---

async def main() -> None:
    """Основная функция для запуска бота."""
    # Стало:
    # persistence = PicklePersistence(filepath="bot_persistence") # Закомментировали эту строку
    bot_token = os.getenv("BOT_TOKEN")
    if not bot_token:
        logging.error("Токен бота не найден! Убедитесь, что он задан в .env файле.")
        return
    application = Application.builder().token(bot_token).build()

    # --- НОВЫЙ БЛОК: ЗАПУСКАЕМ ПЛАНИРОВЩИК ---
    job_queue = application.job_queue
    # Запускаем проверку каждые 15 минут (900 секунд)
    job_queue.run_repeating(check_seminars_and_schedule_reminders, interval=900, first=10)
    # ---------------------------------------------

    auth_web.run_oauth_server()

    # --- ОПРЕДЕЛЕНИЕ ОБРАБОТчикОВ ДИАЛОГОВ ---

    main_menu_fallback = CallbackQueryHandler(back_to_main_menu, pattern='^main_menu$')

    registration_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(logout_handler, pattern='^logout$'),
            CallbackQueryHandler(register_start, pattern='^register$')
        ],
        states={
            GET_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_name)],
            GET_EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_email_and_register)],
        },
        fallbacks=[CommandHandler('start', start_over_fallback), main_menu_fallback],
        name="registration_conversation",

    )

    schedule_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(schedule_menu, pattern='^schedule_menu$')],
        states={
            SCHEDULE_MENU: [
                CallbackQueryHandler(schedule_menu, pattern='^schedule_menu$'),
                CallbackQueryHandler(create_schedule_handler, pattern='^create_schedule$'),
                CallbackQueryHandler(delete_schedule_confirm, pattern='^delete_schedule$'),
            ],
            CONFIRM_DELETE_SCHEDULE: [
                CallbackQueryHandler(run_schedule_deletion, pattern='^confirm_delete$'),
                CallbackQueryHandler(schedule_menu, pattern='^schedule_menu$'),
            ],
        },
        fallbacks=[CommandHandler('start', start_over_fallback), main_menu_fallback],
        name="schedule_conversation",

    )

    # --- ЕДИНЫЙ И ПРАВИЛЬНЫЙ ОБРАБОТЧИК ДЛЯ ВСЕХ ДЗ ---
    homework_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(homework_management_menu_dispatcher, pattern='^homework_management_menu$'),
            CallbackQueryHandler(reminder_add_hw_start, pattern=r'^reminder_add_hw_')
        ],
        states={
            # Уровень 1: Выбор типа ДЗ
            HOMEWORK_MENU: [
                CallbackQueryHandler(personal_homework_menu, pattern='^personal_hw_menu$'),
                CallbackQueryHandler(group_homework_menu, pattern='^group_hw_menu$'),
            ],


            PERSONAL_HW_MENU: [
                CallbackQueryHandler(homework_menu, pattern='^homework_add_start$'),
                CallbackQueryHandler(edit_hw_start, pattern='^homework_edit_start$'),
                CallbackQueryHandler(add_file_start, pattern='^add_file_start$'),
                CallbackQueryHandler(homework_management_menu_dispatcher, pattern='^homework_management_menu$'),
            ],
            GET_HW_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_hw_text)],
            CHOOSE_HW_SUBJECT: [CallbackQueryHandler(choose_hw_subject, pattern=r'^hw_subj_')],
            CHOOSE_HW_DATE_OPTION: [
                CallbackQueryHandler(find_next_class, pattern='^find_next_class$'),
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_manual_date_for_hw)
            ],
            GET_FILE_ONLY: [MessageHandler(filters.Document.ALL | filters.PHOTO, get_file_only)],
            CHOOSE_SUBJECT_FOR_FILE: [CallbackQueryHandler(choose_subject_for_file, pattern=r'^file_subj_')],
            CHOOSE_DATE_FOR_FILE: [
                CallbackQueryHandler(find_next_class_for_file, pattern='^find_next_class_for_file$'),
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_manual_date_for_file),
            ],
            EDIT_HW_CHOOSE_SUBJECT: [CallbackQueryHandler(edit_hw_choose_subject, pattern=r'^edit_hw_subj_')],
            EDIT_HW_GET_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_hw_get_date)],
            EDIT_HW_MENU: [
                CallbackQueryHandler(edit_delete_text, pattern='^edit_delete_text$'),
                CallbackQueryHandler(edit_delete_file, pattern='^edit_delete_file$'),
                CallbackQueryHandler(edit_replace_text_start, pattern='^edit_replace_text$'),
            ],
            EDIT_HW_REPLACE_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_hw_get_new_text)],

            # --- НОВЫЕ ВЕТКИ ГРУППОВОГО ДЗ ---
            GROUP_HW_MENU: [
                CallbackQueryHandler(group_hw_add_text_start, pattern='^group_hw_add_text_start$'),
                CallbackQueryHandler(group_hw_add_file_start, pattern='^group_hw_add_file_start$'),
                CallbackQueryHandler(group_hw_edit_start, pattern='^group_hw_edit_start$'),
                CallbackQueryHandler(homework_management_menu_dispatcher, pattern='^homework_management_menu$'),
            ],
            # Добавление текста
            GET_GROUP_HW_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_group_hw_text)],
            CHOOSE_GROUP_HW_SUBJECT: [CallbackQueryHandler(choose_group_hw_subject, pattern=r'^group_hw_subj_')],
            CHOOSE_GROUP_HW_DATE_OPTION: [
                CallbackQueryHandler(find_next_class_for_group_hw_text, pattern='^find_next_class_group_text$'),
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_manual_date_for_group_hw_text),
            ],
            CHOOSE_SUBJECT_FOR_GROUP_FILE: [
                CallbackQueryHandler(choose_subject_for_group_file, pattern=r'^group_file_subj_')
            ],
            # Добавление файла
            GET_GROUP_FILE_ONLY: [MessageHandler(filters.Document.ALL | filters.PHOTO, get_file_only)],
            CHOOSE_DATE_FOR_GROUP_FILE: [

                CallbackQueryHandler(find_next_class_for_group_hw_file, pattern='^find_next_class_for_group_file$'),
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_manual_date_for_group_hw_file),
            ],
            # Редактирование
            EDIT_GROUP_HW_CHOOSE_SUBJECT: [CallbackQueryHandler(edit_group_hw_choose_subject, pattern=r'^edit_group_hw_subj_')],
            EDIT_GROUP_HW_GET_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_group_hw_get_date)],
            EDIT_GROUP_HW_MENU: [
                CallbackQueryHandler(edit_group_delete_text, pattern='^edit_group_delete_text$'),
                CallbackQueryHandler(edit_group_delete_file, pattern='^edit_group_delete_file$'),
                CallbackQueryHandler(edit_group_replace_text_start, pattern='^edit_group_replace_text$'),
            ],
            EDIT_GROUP_HW_REPLACE_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_group_get_new_text)],
        },
        fallbacks=[CommandHandler('start', start_over_fallback), main_menu_fallback],
        name="homework_conversation",
    )
    textbook_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(add_textbook_start, pattern='^add_textbook_start$')
        ],
        states={
            ADD_TEXTBOOK_CHOOSE_SUBJECT: [
                CallbackQueryHandler(add_textbook_choose_subject, pattern=r'^textbook_subj_')
            ],
            GET_TEXTBOOK_FILE: [
                # Принимаем только документы с типом PDF
                MessageHandler(filters.Document.PDF, add_textbook_get_file)
            ],
        },
        fallbacks=[CommandHandler('start', start_over_fallback), main_menu_fallback],
        name="textbook_conversation",
    )
    summary_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(summary_start, pattern='^summary_start$')
        ],
        states={
            CHOOSE_SUMMARY_SUBJECT: [
                CallbackQueryHandler(summary_choose_subject, pattern=r'^summary_subj_')
            ],
            # ДОБАВЛЯЕМ НОВОЕ СОСТОЯНИЕ И ЕГО ОБРАБОТЧИКИ
            CHOOSE_SUMMARY_FILE: [
                # Обработчик для кнопки "Использовать файл из календаря"
                CallbackQueryHandler(summary_file_chosen, pattern='^use_calendar_file$'),
                # Обработчик для кнопки "Выбрать другой"
                CallbackQueryHandler(summary_pick_another_book, pattern='^pick_another_book$'),
                # Обработчик для кнопок с учебниками из базы (например, use_db_book_0)
                CallbackQueryHandler(summary_file_chosen, pattern=r'^use_db_book_')
            ],
            GET_PAGE_NUMBERS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, summary_get_pages)
            ],
            CONFIRM_SUMMARY_GENERATION: [
                CallbackQueryHandler(summary_generate, pattern='^confirm_summary_yes$')
            ]
        },
        fallbacks=[CommandHandler('start', start_over_fallback), main_menu_fallback],
        name="summary_conversation",
    )

    # --- ДОБАВЛЕНИЕ ВСЕХ ОБРАБОТЧИКОВ В ПРИЛОЖЕНИЕ ---

    # Группа 0: Обработчики диалогов (проверяются в первую очередь)
    application.add_handler(registration_handler)
    application.add_handler(schedule_handler)
    application.add_handler(homework_handler)

    # Группа 1: Простые обработчики (проверяются во вторую очередь)
    application.add_handler(CommandHandler('start', start), group=1)
    application.add_handler(CallbackQueryHandler(quick_login, pattern='^login$'), group=1)
    application.add_handler(CallbackQueryHandler(start_work_handler, pattern='^start_work_after_auth$'), group=1)
    application.add_handler(
        CallbackQueryHandler(homework_management_menu_dispatcher, pattern='^homework_management_menu$'), group=1)
    application.add_handler(CallbackQueryHandler(back_to_main_menu, pattern='^main_menu$'), group=1)
    application.add_handler(textbook_handler)
    application.add_error_handler(error_handler)
    application.add_handler(summary_handler)
    application.add_handler(CallbackQueryHandler(reminder_ignore, pattern='^reminder_ignore$'), group=1)

    # --- Настройка и запуск веб-сервера ---
    internal_app = web.Application()
    internal_app['bot_app'] = application
    internal_app.router.add_post('/auth_success', http_auth_callback)
    runner = web.AppRunner(internal_app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 8081)

    # --- Запуск ---
    try:
        await application.initialize()
        await site.start()
        logging.info("Внутренний веб-сервер для коллбэков запущен на порту 8081")
        logging.info("Бот запущен...")
        await application.start()
        await application.updater.start_polling()
        while True:
            await asyncio.sleep(3600)
    finally:
        await application.updater.stop()
        await application.stop()
        await runner.cleanup()
        logging.info("Бот и веб-серверы остановлены.")


if __name__ == '__main__':
    asyncio.run(main())