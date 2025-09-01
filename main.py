import re
import json
import time
import httpx
import requests

from pydantic import BaseModel
from urllib.parse import unquote
from fastapi.middleware import Middleware
from fastapi import FastAPI, Request, Depends, HTTPException
from datetime import datetime, timedelta, timezone
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import StreamingResponse, JSONResponse

from database.db import DbConnection
from pydantic_models import LogEntry
from config import ALLOWED_IPS, FILE_PATH, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

# Инициализация подключения к базе данных
db_connect = DbConnection()

URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"


class MTSMessage(BaseModel):
    text: str
    sender: str
    receiver: str


def request_telegram(mes: str, disable_notification: bool = False):
    response = requests.post(URL, data={"chat_id": TELEGRAM_CHAT_ID,
                                        "text": mes,
                                        "parse_mode": "Markdown",
                                        "disable_web_page_preview": True,
                                        "disable_notification": disable_notification})
    for _ in range(3):
        if response.status_code == 200:
            break
        else:
            time.sleep(20)
    else:
        print(f"Неудалось отправить сообщение: {mes}")
        print(f"Ошибка {response.status_code}")


class IPFilterMiddleware(BaseHTTPMiddleware):
    """Мидлвар для фильтрации IP-адресов"""

    def __init__(self, app, allowed_ips: list[str]):
        super().__init__(app)
        self.allowed_ips = allowed_ips

    async def dispatch(self, request: Request, call_next):
        """Получение IP клиента из заголовка или сокета"""

        client_ip = request.headers.get("X-Forwarded-For", request.client.host).split(",")[0].strip()
        print(f"Client IP: {client_ip}")
        # Пока пропускаем всех — логика фильтрации не реализована
        return await call_next(request)


# Инициализация FastAPI-приложения с мидлваром
app = FastAPI(middleware=[Middleware(IPFilterMiddleware, allowed_ips=ALLOWED_IPS)])


def get_db():
    """Зависимость FastAPI — передаёт подключение к БД в хендлеры"""

    return db_connect


@app.get("/call")
async def get_call(virtual_phone_number: str,
                   notification_time: str,
                   contact_phone_number: str,
                   db_conn: DbConnection = Depends(get_db)) -> JSONResponse:
    """Эндпоинт для обработки звонка (без сообщения, код — последние 6 цифр номера)"""
    try:
        # Очистка номера от лишних символов, оставляем только 10 цифр
        virtual_phone_number = re.sub(r'\D', '', virtual_phone_number)[-10:]

        # Преобразование времени уведомления к московскому часовому поясу
        notification_time = datetime.strptime(notification_time, "%Y-%m-%d %H:%M:%S.%f")
        now_time = datetime.now(tz=timezone(timedelta(hours=3))).replace(tzinfo=None)
        hours = round((now_time - notification_time).total_seconds() / 3600)
        notification_time += timedelta(hours=hours)

        # Последние 6 цифр контактного номера используются как "сообщение"
        contact_phone_number = re.sub(r'\D', '', contact_phone_number)
        message = contact_phone_number[-6:]

        # Сохраняем информацию в БД
        db_conn.add_message(
            virtual_phone_number=virtual_phone_number,
            time_response=notification_time,
            message=message
        )
        details = "Сообщение получено"
    except Exception as e:
        details = f"Ошибка сообщения: {str(e)}"
    return JSONResponse(
        status_code=200,
        content={"status": "ok", "details": details},
        headers={"X-Custom-Header": "some-value"}
    )


@app.get("/sms")
async def get_sms(virtual_phone_number: str,
                  notification_time: str,
                  contact_phone_number: str,
                  message: str,
                  db_conn: DbConnection = Depends(get_db)) -> JSONResponse:
    """Эндпоинт для обработки СМС с кодом"""
    try:
        # Очистка номера от лишних символов, оставляем только 10 цифр
        virtual_phone_number = re.sub(r'\D', '', virtual_phone_number)[-10:]

        # Преобразование времени уведомления к московскому часовому поясу
        notification_time = datetime.strptime(notification_time, "%Y-%m-%d %H:%M:%S.%f")
        now_time = datetime.now(tz=timezone(timedelta(hours=3))).replace(tzinfo=None)
        hours = round((now_time - notification_time).total_seconds() / 3600)
        notification_time += timedelta(hours=hours)

        # Декодирование URL-сообщения
        message = unquote(message)

        # Извлечение кода из текста (ищем 6 цифр или формат XXX-XXX)
        match = re.search(r'\b\d{6}\b', message)
        if match:
            message = match.group(0)
        else:
            match = re.search(r'\b\d{3}-\d{3}\b', message)
            if match:
                message = match.group(0).replace('-', '')

        # Сопоставление названия платформы с кодом
        marketplace = {'Wildberries': 'WB', 'OZON.ru': 'Ozon', 'Yandex': 'Yandex'}

        # Сохраняем информацию в БД
        db_conn.add_message(
            virtual_phone_number=virtual_phone_number,
            time_response=notification_time,
            message=message,
            marketplace=marketplace[contact_phone_number]
        )
        details = "Сообщение получено"
    except Exception as e:
        details = f"Ошибка сообщения: {str(e)}"
    return JSONResponse(
        status_code=200,
        content={"status": "ok", "details": details},
        headers={"X-Custom-Header": "some-value"}
    )


@app.get("/download_app")
async def get_app(db_conn: DbConnection = Depends(get_db)):
    """Эндпоинт для скачивания zip-файла приложения браузера"""

    try:
        version = db_conn.get_version()

        # Итеративная передача файла по частям
        def iterfile():
            with open(FILE_PATH + f"browser-{version}.zip", "rb") as file:
                while chunk := file.read(1024 * 1024):  # 1 MB
                    yield chunk

        return StreamingResponse(
            iterfile(),
            media_type="application/zip",
            headers={"Content-Disposition": f"attachment; filename=browser-{version}.zip"}
        )
    except Exception as e:
        print(f"get_app: {e}")
        return {"error": "File not found"}


@app.post("/log")
async def get_log(entry: LogEntry, db_conn: DbConnection = Depends(get_db)) -> dict:
    """Эндпоинт для логирования событий из клиента"""

    db_conn.add_log(**entry.dict())
    return {"status": "success", "message": "Log saved successfully"}


@app.post("/mts")
async def get_mts(request: Request) -> JSONResponse:
    """Эндпоинт для получения смс на виртуальные номера MTS"""
    try:
        body = {}
        raw = "Пустое сообщение"

        if request.headers.get("content-type", "").startswith("application/json"):

            try:
                body = await request.json()
                print(f'body: {body}')
            except:
                body = {}

        # 2) multipart/form-data (формы) — работает при установленном python-multipart
        if not body:
            try:
                form = await request.form()
                body = {k: (v.filename if hasattr(v, "filename") else str(v)) for k, v in form.items()}
                print(f'form: {body}')
            except:
                body = {}

        # 3) query как запасной вариант
        if not body:
            body = dict(request.query_params)
            print(f'query: {body}')

        if not body:
            raw = (await request.body()).decode("utf-8", "ignore")
            print(f'raw: {raw}')

        api = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": str(TELEGRAM_CHAT_ID), "text": body or raw}
        async with httpx.AsyncClient() as client:
            await client.post(api, data=payload)

        return JSONResponse(status_code=200, content={"status": "ok"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "details": str(e)})


# @app.post("/mts")
# async def get_mts(msg: MTSMessage) -> JSONResponse:
#     try:
#         request_telegram(f"*На номер:* {msg.receiver}\n"
#                          f"*От:* {msg.sender}\n\n"
#                          f"*Сообщение:*\n"
#                          f"{msg.text}")
#
#         return JSONResponse(status_code=200, content={"status": "ok"})
#     except Exception as e:
#         request_telegram(f'Ошибка: {str(e)}')
#         return JSONResponse(status_code=500, content={"status": "error", "details": str(e)})
