# get_token.py
import os.path
import pickle
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow

# Указываем права доступа (календарь и диск)
SCOPES = ['https://www.googleapis.com/auth/calendar', 'https://www.googleapis.com/auth/drive.file']


def main():
    """
    Запускает процесс аутентификации и сохраняет токен доступа в файл token.pickle.
    """
    creds = None
    # Файл token.pickle хранит токен доступа пользователя.
    # Он создается автоматически при первом запуске.
    if os.path.exists('token.pickle'):
        with open('token.pickle', 'rb') as token:
            creds = pickle.load(token)

    # Если нет валидных учетных данных, запускаем процесс входа.
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            # Используем ваш файл client_secret.json
            flow = InstalledAppFlow.from_client_secrets_file(
                'client_secret2.json', SCOPES)
            creds = flow.run_local_server(port=0)

        # Сохраняем учетные данные для следующих запусков
        with open('token.pickle', 'wb') as token:
            pickle.dump(creds, token)
            print("Токен успешно сохранен в файл token.pickle!")

    print("Файл token.pickle уже существует и действителен. Можно запускать основного бота.")


if __name__ == '__main__':
    main()