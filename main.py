import re
import json
import time
import httpx
import asyncio

from pydantic import BaseModel
from urllib.parse import unquote
from fastapi.middleware import Middleware
from fastapi.concurrency import run_in_threadpool
from datetime import datetime, timedelta, timezone
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi import FastAPI, Request, Depends, HTTPException, BackgroundTasks
from starlette.responses import StreamingResponse, JSONResponse

from database.db import DbConnection
from pydantic_models import LogEntry
from sms_routing import detect_marketplace, detect_shop, resolve_marketplace, MARKETPLACE_ROLES
from database.bootstrap import SessionLocal, SessionLocal2
from config import ALLOWED_IPS, FILE_PATH, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, ADMIN_TG_ID, PROXY, NOVOFON_BOT_TOKEN, \
    NOVOFON_CHAT_ID

MDV2_SPECIALS = r'[_\[\]()~`>#+\|{}]'


class MTSMessage(BaseModel):
    text: str
    sender: str
    receiver: str


# Дедуп: одинаковые сообщения в пределах окна считаем повтором и не обрабатываем повторно
DEDUP_WINDOW = 300  # секунд (5 минут)
_recent_messages: dict[str, float] = {}


def is_duplicate_message(msg: MTSMessage) -> bool:
    """True, если такое же сообщение (номер + отправитель + текст) уже приходило за последние DEDUP_WINDOW секунд."""
    now = time.time()
    key = f"{msg.receiver}|{msg.sender}|{msg.text}"

    # чистим устаревшие ключи, чтобы словарь не разрастался
    for old_key in [k for k, t in _recent_messages.items() if now - t > DEDUP_WINDOW]:
        del _recent_messages[old_key]

    last = _recent_messages.get(key)
    _recent_messages[key] = now  # запоминаем/продлеваем время последнего появления (скользящее окно)
    return last is not None and now - last <= DEDUP_WINDOW


# Novofon-номера (10 цифр), звонки/SMS которых дополнительно дублируются в бота
# (адресно по привязке get_tg_id). Novofon-чат при этом получает копию как обычно.
NOVOFON_TO_BOT = {
    '9240778433', '9333994170', '9952226756', '9843332141',
    '9699992486', '9699997468', '9581110845', '9333994184',
    '9240778126', '9240779171', '9581119477', '9860889534',
}

EXOLVE_TO_BOT = {
    '9862268017', '9393276850','9395154366',
}

# MTS-номера (10 цифр), обслуживающие ProxyBrowser: их WB-коды пишутся
# в admin.phone_message (у остальных номеров — в buyer.phone_code),
# а сами сообщения дополнительно дублируются в общий Novofon-чат.
# Добавить номер = дописать строку сюда, трогать обработчик /mts не нужно.
MTS_PROXYBROWSER = {
    '9393276833', '9681978744', '9820909411', '9667786703', '9862268017',
    '9587143506','9395154366','79393276850',
}

# Номер, на который формально переадресуются звонки Exolve.
# Верификационный звонок маркетплейса сбрасывается раньше, чем дозвонится —
# важен сам факт вызова, номер звонящего мы уже забрали из запроса
EXOLVE_REDIRECT_NUMBER = "79316447568"

# Exolve-номера (10 цифр), звонки которых дополнительно дублируются в бота
# (адресно по привязке get_tg_id). Novofon-чат получает копию как обычно.
# Добавить номер = дописать строку сюда.

# Определение площадки и магазина по тексту SMS вынесено в sms_routing.py
# (detect_marketplace, detect_shop). Правила расширяются там — в MARKETPLACE_RULES.


# Шаблоны кода подтверждения в порядке приоритета: 123456, 123-456, 1234
CODE_PATTERNS = [
    (r'\b\d{6}\b', lambda s: s),
    (r'\b\d{3}-\d{3}\b', lambda s: s.replace('-', '')),
    (r'\b\d{4}\b', lambda s: s),
]


def extract_code(text: str) -> str:
    """Достаёт код подтверждения из текста сообщения. Пустая строка — код не найден."""
    for pattern, transform in CODE_PATTERNS:
        match = re.search(pattern, text or '')
        if match:
            return transform(match.group(0))
    return ''


def escape_mdv2(text: str) -> str:
    return re.sub(MDV2_SPECIALS, lambda m: '\\' + m.group(0), text)


async def request_telegram2(mes: str):
    mes2 = escape_mdv2(mes)
    api = f"https://api.telegram.org/bot{NOVOFON_BOT_TOKEN}/sendMessage"

    timeout = httpx.Timeout(10.0, connect=5.0)

    async with httpx.AsyncClient(proxy=PROXY, timeout=timeout) as client:
        for _ in range(1):
            try:
                r = await client.post(api, data={"chat_id": NOVOFON_CHAT_ID,
                                                 "text": mes2,
                                                 "parse_mode": "Markdown",
                                                 "disable_web_page_preview": True})
                if r.status_code == 200:
                    break
            except httpx.RequestError as e:
                print(f"⚠️ Ошибка запроса к Telegram: {e}")
            await asyncio.sleep(3)
        else:
            try:
                r = await client.post(api, data={"chat_id": NOVOFON_CHAT_ID,
                                                 "text": mes,
                                                 "disable_web_page_preview": True})
                if r.status_code != 200:
                    print(f"Telegram 400: {r.text}")
            except httpx.RequestError as e:
                print(f"⚠️ Ошибка запроса к Telegram: {e}")


async def request_telegram(mes: str, db_conn: DbConnection, phone: str = None,
                           recipients: list[str] | None = None):
    """
    Отправка сообщения в бота.
    recipients — готовый список tg_id (из get_sms_recipients, с учётом ролей менеджеров).
    Если recipients не передан — получатели берутся по привязке номера (get_tg_id):
    так работают звонки и другие события без текста, по которому можно определить площадку.
    """
    mes2 = escape_mdv2(mes)

    async def reg(tg_id: str = None):
        timeout = httpx.Timeout(10.0, connect=5.0)

        # Поддержка одного бота (строка) и нескольких (список токенов)
        tokens = TELEGRAM_BOT_TOKEN if isinstance(TELEGRAM_BOT_TOKEN, (list, tuple)) else [TELEGRAM_BOT_TOKEN]

        if tg_id is None:
            tg_id = TELEGRAM_CHAT_ID
        else:
            tg_id = [tg_id]

        for id_tg in tg_id:
            for token in tokens:
                api = f"https://api.telegram.org/bot{token}/sendMessage"
                async with httpx.AsyncClient(proxy=PROXY, timeout=timeout) as client:
                    for _ in range(1):
                        try:
                            r = await client.post(api, data={"chat_id": str(id_tg),
                                                             "text": mes2,
                                                             "parse_mode": "Markdown",
                                                             "disable_web_page_preview": True})
                            if r.status_code == 200:
                                break
                        except httpx.RequestError as e:
                            print(f"⚠️ Ошибка запроса к Telegram: {e}")
                        await asyncio.sleep(3)
                    else:
                        try:
                            r = await client.post(api, data={"chat_id": str(id_tg),
                                                             "text": mes,
                                                             "disable_web_page_preview": True})
                            if r.status_code != 200:
                                print(f"Telegram 400: {r.text}")
                        except httpx.RequestError as e:
                            print(f"⚠️ Ошибка запроса к Telegram: {e}")

    if phone is None:
        phone = mes2.split('\n')[0].split()[-1]

    if phone == '79340060237':
        try:
            await reg('7796462930')
        except:
            pass
        return

    if recipients is not None:
        tg_ids = recipients
    else:
        tg_ids = await run_in_threadpool(db_conn.get_tg_id, phone)

    if tg_ids is None:
        for tg in ADMIN_TG_ID:
            try:
                await reg(tg)
            except:
                pass
    elif not tg_ids:
        await reg()
    else:
        for tg in tg_ids:
            try:
                await reg(tg)
            except:
                pass


async def route_sms(header: str, message_text: str, sender: str, phone11: str, db_conn: DbConnection) -> None:
    """
    Маршрутизация входящего SMS. Приоритет строго такой:

        MARKETPLACE  → номер в markets → только сотрудникам с ролью площадки (привязка игнорируется).
                       В общие чаты SMS площадок НЕ уходят никогда: нет получателей — только лог.
        BINDING      → номер привязан к сотруднику → только ему (существующая логика get_tg_id)
        DEFAULT CHATS→ иначе в оба общих чата TELEGRAM_CHAT_ID

    Одно SMS уходит ровно по одной ветке — либо менеджерам, либо владельцу, либо в чаты.
    header — первые строки сообщения («На номер / От»), phone11 — номер в формате 79...
    """
    phone10 = phone11[-10:]
    body = f"\n*Сообщение:*\n{message_text.replace('*', chr(92) + '*')}"

    # 1. MARKETPLACE — номер из markets имеет наивысший приоритет
    marketplaces = await run_in_threadpool(db_conn.get_marketplaces_by_number, phone10)
    if marketplaces:
        marketplace = resolve_marketplace(marketplaces, message_text, sender)
        if marketplace is None:
            # На номере несколько площадок, а по тексту не различить — не угадываем площадку,
            # отправляем менеджерам всех площадок этого номера (в общие чаты — нельзя)
            recipients = []
            for mp in marketplaces:
                for tg in await run_in_threadpool(db_conn.get_users_by_marketplace_role, mp):
                    if tg not in recipients:
                        recipients.append(tg)
            print(f"route_sms: {phone11} обслуживает {marketplaces}, площадку по тексту "
                  f"не различить (от={sender!r}) → менеджерам всех площадок: {recipients or 'НИКОГО'}")
            if recipients:
                await request_telegram(header + f"*Площадка:* ⚠️ не различить среди {', '.join(marketplaces)}\n" + body,
                                       db_conn, phone=phone11, recipients=recipients)
            return

        shops = await run_in_threadpool(db_conn.get_shops_for_phone, phone10, marketplace)
        shop = detect_shop(message_text, shops)
        shop_label = shop or ("не определён" if shops else "нет в базе")

        recipients = await run_in_threadpool(db_conn.get_users_by_marketplace_role, marketplace)
        if not recipients:
            # SMS площадки без единого получателя: в общие чаты не отправляем, только лог
            print(f"route_sms: {marketplace} на {phone11}: НЕТ сотрудников с ролью "
                  f"{MARKETPLACE_ROLES.get(marketplace)} и админов с receive_sms → НЕ отправлено")
            return

        await request_telegram(header + f"*Площадка:* {marketplace} · *Магазин:* {shop_label}\n" + body,
                               db_conn, phone=phone11, recipients=recipients)
        return

    # 2. BINDING / 3. DEFAULT CHATS — существующая логика без изменений:
    # request_telegram сам берёт get_tg_id(phone) → привязанным, а если никого — в TELEGRAM_CHAT_ID
    await request_telegram(header + body, db_conn, phone=phone11, recipients=None)


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


async def get_db():
    session = SessionLocal()
    db = DbConnection(session)
    try:
        yield db
    finally:
        session.close()


async def get_db2():
    session = SessionLocal2()
    db = DbConnection(session)
    try:
        yield db
    finally:
        session.close()


@app.get("/myip")
async def get_ip(request: Request):
    return {"ip": request.client.host}


@app.get("/call")
async def get_call(virtual_phone_number: str,
                   notification_time: str,
                   contact_phone_number: str,
                   db_conn: DbConnection = Depends(get_db)) -> JSONResponse:
    """Эндпоинт для обработки звонка (без сообщения, код — последние 6 цифр номера)"""
    try:
        text = ""

        # Очистка номера от лишних символов, оставляем только 10 цифр
        virtual_phone_number = re.sub(r'\D', '', virtual_phone_number)[-10:]

        # Преобразование времени уведомления к московскому часовому поясу
        notification_time = datetime.strptime(notification_time, "%Y-%m-%d %H:%M:%S.%f")
        now_time = datetime.now(tz=timezone(timedelta(hours=3))).replace(tzinfo=None)
        hours = round((now_time - notification_time).total_seconds() / 3600)
        notification_time += timedelta(hours=hours)

        text += f"В {str(notification_time).split('.')[0]} на ваш номер 7{virtual_phone_number} поступил звонок.\n"
        text += f"Номер с которого поступил вызов: {contact_phone_number}"

        try:
            await request_telegram2(text)
        except:
            pass

        # Дублируем в бота (адресно по привязке) для выбранных Novofon-номеров
        if virtual_phone_number in NOVOFON_TO_BOT:
            try:
                await request_telegram(text, db_conn, phone=f'7{virtual_phone_number}')
            except:
                pass

        # Последние 6 цифр контактного номера используются как "сообщение"
        contact_phone_number = re.sub(r'\D', '', contact_phone_number)
        message = contact_phone_number[-6:]

        # Сохраняем информацию в БД
        await run_in_threadpool(db_conn.add_message,
                                virtual_phone_number=virtual_phone_number,
                                time_response=notification_time,
                                message=message)
        details = "Сообщение получено"
    except Exception as e:
        details = f"Ошибка сообщения: {str(e)}"
    return JSONResponse(
        status_code=200,
        content={"status": "ok", "details": details},
        headers={"X-Custom-Header": "some-value"}
    )


def save_call_code(virtual_phone_number: str, time_response: datetime, message: str) -> None:
    """
    Записывает код звонка в phone_message.
    Выполняется в фоне со своей сессией: сессия из Depends закрывается
    раньше, чем отработают фоновые задачи.
    """
    session = SessionLocal()
    try:
        DbConnection(session).add_message(virtual_phone_number=virtual_phone_number,
                                          time_response=time_response,
                                          message=message)
    except Exception as e:
        print(f"save_call_code: {e}")
    finally:
        session.close()


async def notify_call_to_bot(text: str, phone: str) -> None:
    """
    Адресная отправка звонка в бота (по привязке номера к сотруднику).
    Своя сессия — по той же причине, что и в save_call_code.
    """
    session = SessionLocal()
    try:
        await request_telegram(text, DbConnection(session), phone=phone)
    except Exception as e:
        print(f"notify_call_to_bot: {e}")
    finally:
        session.close()


@app.post("/exolve/call")
async def get_exolve_call(request: Request, background_tasks: BackgroundTasks) -> JSONResponse:
    """
    Динамическая переадресация Exolve (метод getControlCallFollowMe).
    Код авторизации — последние 6 цифр номера звонящего.

    Ответ отдаём сразу: пока сервер думает, звонок висит на линии,
    поэтому запись в базу и уведомление уходят в фон.
    """
    body = {}
    try:
        body = await request.json()
    except Exception as e:
        print(f"exolve/call: не удалось разобрать тело запроса: {e}")

    params = (body or {}).get("params") or {}

    # Номер звонящего (сторона A) и наш виртуальный номер (сторона B)
    number_a = re.sub(r'\D', '', str(params.get("numberA", "")))
    sip_id = re.sub(r'\D', '', str(params.get("sip_id", "")))[-10:]
    call_sid = params.get("call_sid", "")

    notification_time = datetime.now(tz=timezone(timedelta(hours=3))).replace(tzinfo=None)

    print(f"exolve/call: sip_id={sip_id} numberA={number_a} call_sid={call_sid}")

    if number_a and sip_id:
        # Кодом являются последние 6 цифр номера, с которого поступил вызов
        message = number_a[-6:]

        text = (f"В {notification_time.strftime('%Y-%m-%d %H:%M:%S')} "
                f"на ваш номер 7{sip_id} поступил звонок.\n"
                f"Номер с которого поступил вызов: {number_a}")

        background_tasks.add_task(save_call_code, sip_id, notification_time, message)
        background_tasks.add_task(request_telegram2, text)

        # Дублируем в бота (адресно по привязке) для выбранных Exolve-номеров
        if sip_id in EXOLVE_TO_BOT:
            background_tasks.add_task(notify_call_to_bot, text, f'7{sip_id}')

    # Ответ в формате JSON-RPC — без него Exolve не смаршрутизирует вызов
    return JSONResponse(
        status_code=200,
        content={
            "id": (body or {}).get("id", 1),
            "jsonrpc": "2.0",
            "sip_id": params.get("sip_id", ""),
            "result": {
                "redirect_type": 1,
                "followme_struct": [1, [{
                    "I_FOLLOW_ORDER": 1,
                    "ACTIVE": True,
                    "NAME": "stub",
                    "REDIRECT_NUMBER": EXOLVE_REDIRECT_NUMBER,
                    "PERIOD": "always",
                    "PERIOD_DESCRIPTION": "always",
                    "TIMEOUT": 30,
                }]],
            },
        }
    )


@app.get("/sms")
async def get_sms(virtual_phone_number: str,
                  notification_time: str,
                  contact_phone_number: str,
                  message: str,
                  db_conn: DbConnection = Depends(get_db)) -> JSONResponse:
    """Эндпоинт для обработки СМС с кодом"""
    try:
        text = ""

        # Очистка номера от лишних символов, оставляем только 10 цифр
        virtual_phone_number = re.sub(r'\D', '', virtual_phone_number)[-10:]

        # Преобразование времени уведомления к московскому часовому поясу
        notification_time = datetime.strptime(notification_time, "%Y-%m-%d %H:%M:%S.%f")
        now_time = datetime.now(tz=timezone(timedelta(hours=3))).replace(tzinfo=None)
        hours = round((now_time - notification_time).total_seconds() / 3600)
        notification_time += timedelta(hours=hours)

        text += f"В {str(notification_time).split('.')[0]} "
        text += f"на ваш номер 7{virtual_phone_number} пришло сообщение от {contact_phone_number}.\n"
        text += f"Текст сообщения:\n"

        # Декодирование URL-сообщения
        message = unquote(message)
        text += f"{message}"

        try:
            await request_telegram2(text)
        except:
            pass

        # Дублируем в бота для выбранных Novofon-номеров по тем же правилам, что /mts:
        # MARKETPLACE → BINDING → DEFAULT CHATS (см. route_sms)
        if virtual_phone_number in NOVOFON_TO_BOT:
            try:
                await route_sms(f"*На номер:* 7{virtual_phone_number}\n*От:* {contact_phone_number}\n",
                                message, contact_phone_number, f'7{virtual_phone_number}', db_conn)
            except Exception as e:
                print(f"/sms → бот: {e}")

        patterns = [
            (r'\b\d{6}\b', lambda s: s),
            (r'\b\d{3}-\d{3}\b', lambda s: s.replace('-', '')),
            (r'\b\d{4}\b', lambda s: s),
        ]

        for pattern, transform in patterns:
            match = re.search(pattern, message)
            if match:
                message = transform(match.group(0))
                break

        # Сопоставление названия платформы с кодом.
        # Отправитель не из списка -> KeyError, поэтому берём через .get с проверкой
        marketplace = {'Wildberries': 'WB', 'OZON.ru': 'Ozon', 'Yandex': 'Yandex', 'M.Video': 'МВидео'}
        mkt = marketplace.get(contact_phone_number)

        print(f"/sms: от={contact_phone_number!r} на={virtual_phone_number} "
              f"площадка={mkt} код={message} время={notification_time}")

        if mkt is None:
            details = f"Неизвестный отправитель: {contact_phone_number}"
            print(f"/sms: {details}")
            return JSONResponse(
                status_code=200,
                content={"status": "ok", "details": details},
                headers={"X-Custom-Header": "some-value"}
            )

        # Сохраняем информацию в БД
        await run_in_threadpool(db_conn.add_message,
                                virtual_phone_number=virtual_phone_number,
                                time_response=notification_time,
                                message=message,
                                marketplace=mkt)
        details = "Сообщение получено"
    except Exception as e:
        details = f"Ошибка сообщения: {str(e)}"
        print(f"/sms: {details}")
    return JSONResponse(
        status_code=200,
        content={"status": "ok", "details": details},
        headers={"X-Custom-Header": "some-value"}
    )


@app.get("/download_app")
async def get_app(db_conn: DbConnection = Depends(get_db)):
    """Эндпоинт для скачивания zip-файла приложения браузера"""

    try:
        version = await run_in_threadpool(db_conn.get_version)

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

    await run_in_threadpool(db_conn.add_log, **entry.dict())
    return {"status": "success", "message": "Log saved successfully"}


@app.post("/mts")
async def get_mts(request: Request,
                  db_conn: DbConnection = Depends(get_db),
                  db_conn2: DbConnection = Depends(get_db2)) -> JSONResponse:
    """Эндпоинт для получения смс на виртуальные номера MTS"""
    try:
        body = {}
        raw = "Пустое сообщение"
        msg = None

        notification_time = datetime.now(tz=timezone(timedelta(hours=3))).replace(tzinfo=None)

        if request.headers.get("content-type", "").startswith("application/json"):

            try:
                body = await request.json()
                msg = MTSMessage(**{k: body[k] for k in ["text", "sender", "receiver"]})
            except:
                body = {}

        # 2) multipart/form-data (формы) — работает при установленном python-multipart
        if not body:
            try:
                form = await request.form()
                body = {k: (v.filename if hasattr(v, "filename") else str(v)) for k, v in form.items()}
                print(f"form: {body}")
            except:
                body = {}

        # 3) query как запасной вариант
        if not body:
            body = dict(request.query_params)
            print(f"dict: {body}")

        if not body:
            try:
                raw = (await request.body()).decode("utf-8", "ignore")
                data = json.loads(raw)
                msg = MTSMessage(**{k: data[k] for k in ["text", "sender", "receiver"]})
            except:
                body = {}

        if msg:
            if is_duplicate_message(msg):
                print(f"Дубль в пределах {DEDUP_WINDOW}s — пропуск: {msg.sender} {msg.receiver}")
                return JSONResponse(status_code=200, content={"status": "ok", "duplicate": True})

            try:
                # Площадка по тексту — нужна ниже для записи кода в phone_message
                marketplace = detect_marketplace(message_text=msg.text, sender=msg.sender)

                # Доставка: MARKETPLACE → BINDING → DEFAULT CHATS (см. route_sms).
                # Отделена от записи кода: сбой Telegram не должен мешать сохранению кода
                try:
                    await route_sms(f"*На номер:* {msg.receiver}\n*От:* {msg.sender}\n",
                                    msg.text, msg.sender, msg.receiver, db_conn)
                except Exception as e:
                    print(f"route_sms: {e}")

                print(msg.sender, msg.receiver, msg.text)

                # Дублируем сообщения этих номеров в общий Novofon-чат
                if msg.receiver[1:] in MTS_PROXYBROWSER:
                    try:
                        await request_telegram2(f"На номер: {msg.receiver}\n"
                                                f"От: {msg.sender}\n\n"
                                                f"Сообщение:\n{msg.text}")
                    except:
                        pass

                if msg.receiver[1:] in MTS_PROXYBROWSER:
                    if msg.sender == 'Wildberries':
                        code = ""
                        phone = msg.receiver[1:]
                        match = re.search(r'\b\d{6}\b', msg.text)
                        if match:
                            code = match.group(0)
                        else:
                            match = re.search(r'\b\d{3}-\d{3}\b', msg.text)
                            if match:
                                code = match.group(0).replace('-', '')
                        if code:
                            await run_in_threadpool(db_conn.add_message,
                                                    virtual_phone_number=phone,
                                                    time_response=notification_time,
                                                    message=code,
                                                    marketplace='WB')
                elif msg.sender == 'Wildberries':
                    code = ""
                    phone = msg.receiver[1:]
                    match = re.search(r'\b\d{6}\b', msg.text)
                    if match:
                        code = match.group(0)
                    else:
                        match = re.search(r'\b\d{3}-\d{3}\b', msg.text)
                        if match:
                            code = match.group(0).replace('-', '')
                    if code:
                        db_conn2.add_code(virtual_phone_number=phone, time_response=notification_time, code=code)

                # Коды остальных площадок (Ozon, Yandex, МВидео) — в phone_message.
                # Отдельный if, а не ветка цепочки выше: номер может быть и в списке, и вне его
                if msg.sender != 'Wildberries' and marketplace:
                    code = extract_code(msg.text)
                    print(f"{marketplace}: код {code or 'не найден'} на номер {msg.receiver}")
                    if code:
                        await run_in_threadpool(db_conn.add_message,
                                                virtual_phone_number=msg.receiver[1:],
                                                time_response=notification_time,
                                                message=code,
                                                marketplace=marketplace)

                return JSONResponse(status_code=200, content={"status": "ok"})
            except Exception as e:
                print(f'{str(e)}')

        tokens = TELEGRAM_BOT_TOKEN if isinstance(TELEGRAM_BOT_TOKEN, (list, tuple)) else [TELEGRAM_BOT_TOKEN]
        # str() от списка чатов давал '[-100..., -100...]' — Telegram отвечал
        # "chat not found", и нераспознанные сообщения молча терялись
        chat_ids = TELEGRAM_CHAT_ID if isinstance(TELEGRAM_CHAT_ID, (list, tuple)) else [TELEGRAM_CHAT_ID]
        async with httpx.AsyncClient(proxy=PROXY, timeout=httpx.Timeout(10.0, connect=5.0)) as client:
            for token in tokens:
                api = f"https://api.telegram.org/bot{token}/sendMessage"
                for chat_id in chat_ids:
                    try:
                        r = await client.post(api, data={"chat_id": str(chat_id), "text": body or raw})
                        if r.status_code != 200:
                            print(f"fallback telegram {r.status_code}: {r.text}")
                    except httpx.RequestError as e:
                        print(f"fallback telegram: {e}")

        return JSONResponse(status_code=200, content={"status": "ok"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "details": str(e)})
