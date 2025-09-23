# auth_web.py

import threading
import os
import logging
from flask import Flask, request
import google_auth_oauthlib.flow
import requests
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
import config # <--- Импортируем конфиг

# --- Настройки ---
# ИСПРАВЛЕНО: Убран неверный путь ../
CLIENT_SECRETS_FILE = 'client_secret_local.json'
SCOPES = ['https://www.googleapis.com/auth/calendar']
TOKEN_DIR = '.venv/tokens'  # Папка для хранения учетных данных пользователей

app = Flask(__name__)
# Отключаем стандартное логирование Flask, чтобы не засорять консоль
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)


def get_google_auth_flow():
    """Создает и возвращает объект Flow для аутентификации Google."""
    # Теперь redirect_uri берется из файла config
    return google_auth_oauthlib.flow.Flow.from_client_secrets_file(
        CLIENT_SECRETS_FILE, scopes=SCOPES, redirect_uri=config.REDIRECT_URI
    )


def save_credentials(user_id, credentials):
    """Сохраняет учетные данные пользователя в файл."""
    if not os.path.exists(TOKEN_DIR):
        os.makedirs(TOKEN_DIR)
    token_path = os.path.join(TOKEN_DIR, f'token_{user_id}.json')
    with open(token_path, 'w') as token_file:
        token_file.write(credentials.to_json())


def load_credentials(user_id):
    """Загружает учетные данные пользователя из файла."""
    token_path = os.path.join(TOKEN_DIR, f'token_{user_id}.json')
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
        # Если токен истек и есть refresh_token, обновляем его
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                save_credentials(user_id, creds) # Пересохраняем обновленный токен
            except Exception as e:
                logging.error(f"Не удалось обновить токен для user_id {user_id}: {e}")
                return None
        return creds
    return None


def delete_credentials(user_id):
    """Удаляет файл с учетными данными пользователя."""
    token_path = os.path.join(TOKEN_DIR, f'token_{user_id}.json')
    if os.path.exists(token_path):
        os.remove(token_path)


@app.route('/oauth2callback')
def oauth2callback():
    """Обрабатывает коллбэк от Google после успешной авторизации."""
    state = request.args.get('state')
    if not state:
        return "Ошибка: отсутствует параметр state.", 400

    user_id = int(state)
    flow = get_google_auth_flow()

    try:
        flow.fetch_token(authorization_response=request.url)
        credentials = flow.credentials
        save_credentials(user_id, credentials)

        # Отправляем уведомление нашему основному боту
        try:
            # Используем переменную из config.py
            requests.post(config.BOT_CALLBACK_URL, json={'user_id': str(user_id)}, timeout=5)
            return "Авторизация прошла успешно! Можете закрыть эту вкладку и вернуться в Telegram."
        except requests.exceptions.RequestException as e:
            # Используем переменную из config.py для более информативного лога
            logging.error(f"Не удалось уведомить бота по адресу {config.BOT_CALLBACK_URL}: {e}")
            return f"Не удалось уведомить бота. Проверьте, что основной бот запущен. Ошибка: {e}", 500

    except Exception as e:
        logging.error(f"Ошибка при получении токена от Google: {e}")
        return f"Произошла ошибка авторизации: {e}", 500


def run_flask():
    """Запускает веб-сервер Flask."""
    # Используем хост и порт из config.py
    app.run(host=config.OAUTH_SERVER_HOST, port=config.OAUTH_SERVER_PORT)


def run_oauth_server():
    """Запускает веб-сервер в отдельном потоке, чтобы не блокировать бота."""
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    # Используем переменную из config.py для логгирования
    logging.info(f"Веб-сервер для OAuth запущен на порту {config.OAUTH_SERVER_PORT}")