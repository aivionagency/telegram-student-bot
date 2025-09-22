# бот 2.py

import logging
import datetime
import os
import re
import asyncio
import time

from aiohttp import web

from googleapiclient.http import BatchHttpRequest
from concurrent.futures import ProcessPoolExecutor
from googleapiclient.discovery import build
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
    ConversationHandler, CallbackContext, CallbackQueryHandler,
    PicklePersistence
)

import auth_web
import config

# --- ЛОГИРОВАНИЕ ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- СОСТОЯНИЯ ДИАЛОГА ---
# Определяем состояния. HOMEWORK_MANAGEMENT нам больше не нужен как состояние.
(MAIN_MENU, SCHEDULE_MENU,
 GET_HW_TEXT, CHOOSE_HW_SUBJECT, CHOOSE_HW_DATE_OPTION,
 EDIT_HW_CHOOSE_SUBJECT, EDIT_HW_GET_DATE,
 EDIT_HW_GET_NEW_TEXT, CONFIRM_DELETE_SCHEDULE,
 GET_NAME, GET_EMAIL,
 GET_GROUP_HW_TEXT, CHOOSE_GROUP_HW_SUBJECT, CHOOSE_GROUP_HW_DATE_OPTION,
 EDIT_GROUP_HW_CHOOSE_SUBJECT, EDIT_GROUP_HW_GET_DATE, EDIT_GROUP_HW_GET_NEW_TEXT, HOMEWORK_MENU, CHOOSE_HW_TYPE
 ) = range(19)


def get_calendar_service(user_id):
    """Создает сервис для работы с Google Calendar API."""
    creds = auth_web.load_credentials(user_id)
    if not creds:
        return None
    return build('calendar', 'v3', credentials=creds)


# --- Основные команды и меню ---

async def start(update: Update, context: CallbackContext) -> None:
    """ДЛЯ ЛОКАЛЬНОГО ТЕСТА: Пропускает авторизацию и сразу показывает главное меню."""
    # Эта функция теперь просто вызывает главное меню,
    # как будто пользователь уже успешно вошел в систему.
    await main_menu(update, context)


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


async def main_menu(update: Update, context: CallbackContext):
    """Отображает главное меню."""
    keyboard = [
        [InlineKeyboardButton("Управление расписанием", callback_data="schedule_menu")],
        [InlineKeyboardButton("Управление ДЗ", callback_data="homework_management_menu")],
        [InlineKeyboardButton("Выйти и сменить аккаунт", callback_data="logout")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = "Вы авторизованы. Выберите действие:"
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
    else:
        await update.message.reply_text(text, reply_markup=reply_markup)


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
    # service = get_calendar_service(user_id)
    # if not service:
    #     await query.answer("Ошибка авторизации. Попробуйте /start снова.", show_alert=True)
    #     return ConversationHandler.END

    await query.answer()
    await query.edit_message_text("Начинаю составлять расписание... Это может занять несколько минут.")

    loop = asyncio.get_running_loop()
    # Здесь мы меняем service на user_id
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
    # service = get_calendar_service(user_id)
    # if not service:
    #     await query.answer("Ошибка авторизации.", show_alert=True)
    #     return ConversationHandler.END

    await query.answer()
    await query.edit_message_text("Начинаю удаление... Это может занять время.")

    loop = asyncio.get_running_loop()
    # И здесь мы тоже меняем service на user_id
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

def save_homework_to_event(event: dict, homework_text: str, service, is_group_hw: bool = False):
    """Обновляет описание и заголовок события с ДЗ (СИНХРОННАЯ ФУНКЦИЯ)."""
    description = event.get('description', '')
    summary = event.get('summary', '')

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
        group_hw_part = f"\n{homework_text}" if homework_text else ""
    else:
        personal_hw_part = f"\n{homework_text}" if homework_text else ""

    new_description = teacher_part.strip()
    if group_hw_part.strip():
        new_description += f"\n\n{config.GROUP_HOMEWORK_DESC_TAG}{group_hw_part}"
    if personal_hw_part.strip():
        new_description += f"\n\n{config.PERSONAL_HOMEWORK_DESC_TAG}{personal_hw_part}"
    event['description'] = new_description.strip()

    summary = summary.replace(config.HOMEWORK_TITLE_TAG, "").strip()
    if group_hw_part.strip() or personal_hw_part.strip():
        event['summary'] = f"{summary}{config.HOMEWORK_TITLE_TAG}"
    else:
        event['summary'] = summary

    service.events().update(calendarId='primary', eventId=event['id'], body=event).execute()


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


# ЗАМЕНИТЕ ЭТУ ФУНКЦИЮ
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


# ДОБАВЬТЕ ЭТУ НОВУЮ ФУНКЦИЮ
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


# ЗАМЕНИТЕ ЭТУ ФУНКЦИЮ (бывшая find_next_seminar)
# ЗАМЕНИТЕ find_next_seminar (или find_next_class)
async def find_next_class(update: Update, context: CallbackContext) -> int:
    """Ищет следующее занятие (семинар или лабу) и сохраняет ДЗ."""
    query = update.callback_query
    user_id = update.effective_user.id
    service = get_calendar_service(user_id)
    if not service:
        await query.answer("Ошибка авторизации.", show_alert=True)
        return ConversationHandler.END

    await query.answer()

    class_type = context.user_data.get('hw_type', 'Семинар')
    await query.edit_message_text(f"Ищу ближайшее занятие типа «{class_type}»...")

    subject = context.user_data.get('homework_subject')
    homework_text = context.user_data.get('homework_text')
    class_color_id = config.COLOR_MAP.get(class_type)

    now_utc = datetime.datetime.utcnow()
    tomorrow_utc_date = now_utc.date() + datetime.timedelta(days=1)
    start_of_tomorrow_utc = datetime.datetime.combine(tomorrow_utc_date, datetime.time.min)
    search_start_time = start_of_tomorrow_utc.isoformat() + 'Z'

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
                save_homework_to_event(event, homework_text, service, is_group_hw=False)
                event_date_str = event['start'].get('dateTime', event['start'].get('date'))
                event_date = datetime.datetime.fromisoformat(event_date_str).strftime('%d.%m.%Y')
                await query.edit_message_text(
                    f'Готово! ДЗ для "{subject}" ({class_type.lower()}) записано на {event_date}.',
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("« В главное меню", callback_data="main_menu")]
                    ])
                )
                context.user_data.clear()
                return ConversationHandler.END

        await query.edit_message_text(f"Не нашел предстоящих занятий типа «{class_type}» по предмету '{subject}'.")
        return CHOOSE_HW_DATE_OPTION
    except Exception as e:
        logger.error(f"Error finding next class: {e}")
        await query.edit_message_text(f"Произошла ошибка: {e}")
        context.user_data.clear()
        return ConversationHandler.END


# ЗАМЕНИТЕ get_manual_date_for_hw
async def get_manual_date_for_hw(update: Update, context: CallbackContext, is_editing: bool = False) -> int:
    user_id = update.effective_user.id
    service = get_calendar_service(user_id)
    if not service:
        await update.message.reply_text("Ошибка авторизации.")
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
            # Логика добавления
            homework_text = context.user_data.get('homework_text')
            save_homework_to_event(event_to_process, homework_text, service, is_group_hw=False)
            await update.message.reply_text(
                f'Готово! ДЗ для "{subject}" ({class_type.lower()}) записано на {target_date.strftime("%d.%m.%Y")}.',
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("« В главное меню", callback_data="main_menu")]])
            )
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
    return await get_manual_date_for_hw(update, context, is_editing=True)


async def edit_hw_get_new_text(update: Update, context: CallbackContext) -> int:
    user_id = update.effective_user.id
    service = get_calendar_service(user_id)
    if not service:
        await update.message.reply_text("Ошибка авторизации.")
        return ConversationHandler.END

    new_homework_text = update.message.text
    event_to_edit = context.user_data.get('event_to_edit')
    subject = context.user_data.get('homework_subject')
    save_homework_to_event(event_to_edit, new_homework_text, service, is_group_hw=False)
    await update.message.reply_text(
        f"ДЗ для '{subject}' успешно обновлено!",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« В главное меню", callback_data="main_menu")]])
    )
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
    """Выбирает предмет и запрашивает дату для редактирования группового ДЗ."""
    query = update.callback_query
    await query.answer()
    subject_index = int(query.data.split('_')[-1])
    selected_item = context.user_data['subjects_list'][subject_index]

    # Распознаем специальную кнопку и устанавливаем тип
    if selected_item == "Лабораторная: Теоретические основы информатики":
        context.user_data['group_homework_subject'] = "Теоретические основы информатики"
        context.user_data['hw_type'] = "Лабораторные работы"
    else:
        context.user_data['group_homework_subject'] = selected_item
        context.user_data['hw_type'] = "Семинар"

    await query.edit_message_text(
        f"Для предмета '{context.user_data['group_homework_subject']}' введите дату (в формате ДД.ММ), на которую хотите отредактировать ДЗ."
    )
    return EDIT_GROUP_HW_GET_DATE


async def edit_group_hw_get_date(update: Update, context: CallbackContext) -> int:
    """
    Обрабатывает введенную дату, находит нужное занятие (семинар или лабу)
    и показывает меню редактирования группового ДЗ.
    """
    user_id = update.effective_user.id
    service = get_calendar_service(user_id)
    if not service:
        await update.message.reply_text("Ошибка авторизации.")
        return ConversationHandler.END

    try:
        day, month = map(int, update.message.text.split('.'))
        target_date = datetime.date(datetime.date.today().year, month, day)
        # Сохраняем дату в контекст, чтобы использовать ее на следующих шагах
        context.user_data['target_date'] = target_date
    except (ValueError, IndexError):
        await update.message.reply_text("Неверный формат. Введите дату как ДД.ММ")
        return EDIT_GROUP_HW_GET_DATE

    subject = context.user_data.get('group_homework_subject')

    # --- КЛЮЧЕВОЕ ИСПРАВЛЕНИЕ ---
    # Получаем тип занятия из контекста, а не используем "Семинар" по умолчанию
    class_type = context.user_data.get('hw_type', 'Семинар')
    class_color_id = config.COLOR_MAP.get(class_type)
    # --- КОНЕЦ ИСПРАВЛЕНИЯ ---

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
            # Ищем по правильному цвету (семинар или лабораторная)
            if event_subject == subject and event.get('colorId') == class_color_id:
                found_event = event
                break

        if not found_event:
            await update.message.reply_text(
                f"На {target_date.strftime('%d.%m.%Y')} не найдено занятий типа «{class_type}» по предмету '{subject}'.")
            return EDIT_GROUP_HW_GET_DATE

        description = found_event.get('description', '')
        # Используем универсальную функцию для извлечения ДЗ
        hw_part = extract_homework_part(description, config.GROUP_HOMEWORK_DESC_TAG)
        if not hw_part:
            hw_part = "Групповое ДЗ пока не было записано."

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
    class_type = context.user_data.get('hw_type', 'Семинар') # Получаем тип
    target_date = context.user_data.get('target_date') # Получаем дату

    # Передаем class_type и target_date, но пустой текст для удаления
    updated_count, _ = await asyncio.to_thread(
        find_and_update_or_delete_group_hw_blocking, subject, "", class_type, target_date
    )

    await query.edit_message_text(
        f"Групповое ДЗ для '{subject}' ({class_type.lower()}) удалено у {updated_count} пользователей.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« В главное меню", callback_data="main_menu")]])
    )
    context.user_data.clear()
    return ConversationHandler.END


def find_and_update_or_delete_group_hw_blocking(subject: str, homework_text: str, class_type: str, target_date: datetime.date = None) -> \
        tuple[int, list]:
    """
    Блокирующая функция для обновления/удаления ДЗ для всей группы.
    Теперь принимает class_type для поиска нужного типа занятия.
    """
    updated_count = 0
    failed_users = []

    try:
        user_ids = [int(f.split('_')[1].split('.')[0]) for f in os.listdir(auth_web.TOKEN_DIR) if
                    f.startswith('token_')]
    except (FileNotFoundError, IndexError):
        return 0, []

    # Получаем нужный цвет по типу занятия
    class_color_id = config.COLOR_MAP.get(class_type)

    for user_id in user_ids:
        try:
            service = get_calendar_service(user_id)
            if not service:
                failed_users.append(str(user_id))
                continue

            if target_date:
                time_min = datetime.datetime.combine(target_date, datetime.time.min).isoformat() + 'Z'
                time_max = datetime.datetime.combine(target_date, datetime.time.max).isoformat() + 'Z'
                order_by = None
            else:
                now_utc = datetime.datetime.utcnow()
                tomorrow_utc_date = now_utc.date() + datetime.timedelta(days=1)
                start_of_tomorrow_utc = datetime.datetime.combine(tomorrow_utc_date, datetime.time.min)
                time_min = start_of_tomorrow_utc.isoformat() + 'Z'
                time_max = None
                order_by = 'startTime'

            events = service.events().list(
                calendarId='primary', timeMin=time_min, timeMax=time_max,
                singleEvents=True, orderBy=order_by, maxResults=250
            ).execute().get('items', [])

            found = False
            for event in events:
                event_summary = event.get('summary', '')
                match = re.search(r'^(.*?)\s\(', event_summary)
                event_subject = match.group(1).strip() if match else ''

                # Ищем по цвету, соответствующему class_type
                if event_subject == subject and event.get('colorId') == class_color_id:
                    save_homework_to_event(event, homework_text, service, is_group_hw=True)
                    found = True
                    break

            if found:
                updated_count += 1
            else:
                logger.warning(f"No class of type '{class_type}' found for user {user_id} for subject {subject} on date {target_date}")

        except Exception as e:
            logger.error(f"Failed to update homework for user {user_id}: {e}")
            failed_users.append(str(user_id))

    return updated_count, failed_users


async def group_homework_start(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Введите текст ДЗ для всей группы:")
    # Устанавливаем флаг, что это групповое ДЗ
    context.user_data['is_group_hw'] = True
    return GET_GROUP_HW_TEXT


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

    keyboard = [[InlineKeyboardButton(button_text, callback_data="find_next_seminar_group")]]
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


# ЭТУ ФУНКЦИЮ НУЖНО ДОБАВИТЬ В КОД
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


# ЗАМЕНИТЕ СТАРУЮ ВЕРСИЮ ЭТОЙ ФУНКЦИИ НА НОВУЮ

async def homework_management_menu_dispatcher(update: Update, context: CallbackContext) -> int:
    """Отображает меню ДЗ, выводит диагностику и переводит диалог в состояние ожидания."""
    query = update.callback_query
    user_id = update.effective_user.id
    await query.answer()

    keyboard = []
    # Сравнение с int() оставляем как самую надежную практику
    if user_id in config.ADMIN_IDS:
        keyboard = [
            [InlineKeyboardButton("Записать свое дз", callback_data="homework_add_start")],
            [InlineKeyboardButton("Редактировать своё ДЗ", callback_data="homework_edit_start")],
            [InlineKeyboardButton("Записать ДЗ для группы", callback_data="homework_add_group_start")],
            [InlineKeyboardButton("Редактировать ДЗ для группы", callback_data="homework_edit_group_start")],
        ]
    else:
        keyboard = [
            [InlineKeyboardButton("Записать ДЗ", callback_data="homework_add_start")],
            [InlineKeyboardButton("Редактировать ДЗ", callback_data="homework_edit_start")],
        ]
    keyboard.append([InlineKeyboardButton("« Назад в главное меню", callback_data="main_menu")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text("Выберите действие с домашним заданием:", reply_markup=reply_markup)

    # ЭТО КЛЮЧЕВОЕ ИСПРАВЛЕНИЕ: Мы возвращаем следующее состояние для диалога
    return HOMEWORK_MENU


# --- Главная функция и запуск ---

async def main() -> None:
    """Основная функция для запуска бота."""
    # Стало:
    # persistence = PicklePersistence(filepath="bot_persistence") # Закомментировали эту строку
    application = Application.builder().token(config.BOT_TOKEN).build()  # Убрали persistence
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
            # Диалог по-прежнему начинается с кнопки "Управление ДЗ"
            CallbackQueryHandler(homework_management_menu_dispatcher, pattern='^homework_management_menu$')
        ],
        states={
            # Первый шаг - показать меню.
            HOMEWORK_MENU: [
                CallbackQueryHandler(homework_menu, pattern='^homework_add_start$'),
                CallbackQueryHandler(edit_hw_start, pattern='^homework_edit_start$'),
                CallbackQueryHandler(group_homework_start, pattern='^homework_add_group_start$'),
                CallbackQueryHandler(edit_group_hw_start, pattern='^homework_edit_group_start$'),
            ],

            # Состояния для личного ДЗ (добавление)
            GET_HW_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_hw_text)],
            CHOOSE_HW_SUBJECT: [CallbackQueryHandler(choose_hw_subject, pattern=r'^hw_subj_')],

            # --- ВОТ НОВОЕ СОСТОЯНИЕ ДЛЯ ВЫБОРА ТИПА ЗАНЯТИЯ ---
            CHOOSE_HW_TYPE: [CallbackQueryHandler(choose_hw_type, pattern=r'^hw_type_')],

            CHOOSE_HW_DATE_OPTION: [
                CallbackQueryHandler(find_next_class, pattern='^find_next_class$'),
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_manual_date_for_hw)
            ],

            # Состояния для личного ДЗ (редактирование)
            EDIT_HW_CHOOSE_SUBJECT: [CallbackQueryHandler(edit_hw_choose_subject, pattern=r'^edit_hw_subj_')],
            EDIT_HW_GET_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_hw_get_date)],
            EDIT_HW_GET_NEW_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_hw_get_new_text),
                CallbackQueryHandler(delete_personal_hw, pattern='^delete_personal_hw$'),
            ],

            # Состояния для группового ДЗ (остаются без изменений, но включены в общий handler)
            GET_GROUP_HW_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_group_hw_text)],
            CHOOSE_GROUP_HW_SUBJECT: [CallbackQueryHandler(choose_group_hw_subject, pattern=r'^group_hw_subj_')],
            CHOOSE_GROUP_HW_DATE_OPTION: [
                CallbackQueryHandler(find_next_seminar_for_group, pattern='^find_next_seminar_group$'),
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_manual_date_for_group_hw),
            ],
            EDIT_GROUP_HW_CHOOSE_SUBJECT: [
                CallbackQueryHandler(edit_group_hw_choose_subject, pattern=r'^edit_group_hw_subj_')],
            EDIT_GROUP_HW_GET_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_group_hw_get_date)],
            EDIT_GROUP_HW_GET_NEW_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_group_hw_get_new_text),
                CallbackQueryHandler(delete_group_hw, pattern='^delete_group_hw$'),
            ],
        },
        fallbacks=[CommandHandler('start', start_over_fallback), main_menu_fallback],
        name="homework_conversation",
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