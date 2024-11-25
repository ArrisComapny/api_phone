import re

from urllib.parse import unquote
from datetime import datetime, timedelta, timezone

from fastapi.middleware import Middleware
from fastapi import FastAPI, Request, Depends
from starlette.responses import StreamingResponse
from starlette.middleware.base import BaseHTTPMiddleware

from config import ALLOWED_IPS, FILE_PATH
from database.db import DbConnection
from pydantic_models import LogEntry

db_connect = DbConnection()


class IPFilterMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, allowed_ips: list[str]):
        super().__init__(app)
        self.allowed_ips = allowed_ips

    async def dispatch(self, request: Request, call_next):
        client_ip = request.headers.get("X-Forwarded-For", request.client.host).split(",")[0].strip()
        print(f"Client IP: {client_ip}")
        # if client_ip not in self.allowed_ips:
        #     print(f"Access forbidden for IP: {client_ip}")
        #     raise HTTPException(status_code=403, detail="Access forbidden")
        return await call_next(request)


app = FastAPI(middleware=[Middleware(IPFilterMiddleware, allowed_ips=ALLOWED_IPS)])


def get_db():
    return db_connect


@app.get("/call")
async def get_call(virtual_phone_number: str,
                   notification_time: str,
                   contact_phone_number: str,
                   db_conn: DbConnection = Depends(get_db)):
    virtual_phone_number = re.sub(r'\D', '', virtual_phone_number)
    virtual_phone_number = virtual_phone_number[-10:]

    notification_time = datetime.strptime(notification_time, "%Y-%m-%d %H:%M:%S.%f")
    now_time = datetime.now(tz=timezone(timedelta(hours=3))).replace(tzinfo=None)
    hours = round((now_time - notification_time).total_seconds() / 3600)
    notification_time += timedelta(hours=hours)

    contact_phone_number = re.sub(r'\D', '', contact_phone_number)
    message = contact_phone_number[-6:]

    db_conn.add_message(virtual_phone_number=virtual_phone_number,
                        time_response=notification_time,
                        message=message,
                        marketplace='Ozon')


@app.get("/sms")
async def get_sms(virtual_phone_number: str,
                  notification_time: str,
                  contact_phone_number: str,
                  message: str,
                  db_conn: DbConnection = Depends(get_db)):
    virtual_phone_number = re.sub(r'\D', '', virtual_phone_number)
    virtual_phone_number = virtual_phone_number[-10:]

    notification_time = datetime.strptime(notification_time, "%Y-%m-%d %H:%M:%S.%f")
    now_time = datetime.now(tz=timezone(timedelta(hours=3))).replace(tzinfo=None)
    hours = round((now_time - notification_time).total_seconds() / 3600)
    notification_time += timedelta(hours=hours)

    message = unquote(message)
    match = re.search(r'\b\d{6}\b', message)

    if match:
        message = match.group(0)

    marketplace = {'Wildberries': 'WB', 'OZON.ru': 'Ozon'}

    db_conn.add_message(virtual_phone_number=virtual_phone_number,
                        time_response=notification_time,
                        message=message,
                        marketplace=marketplace[contact_phone_number])


@app.get("/download_app")
async def get_app():
    try:
        def iterfile():
            with open(FILE_PATH, "rb") as file:
                while chunk := file.read(1024 * 1024):
                    yield chunk

        return StreamingResponse(iterfile(),
                                 media_type="application/zip",
                                 headers={"Content-Disposition": f"attachment; filename=browser-1.0.1.zip"})
    except Exception as e:
        print(f"get_app: {e}")
        return {"error": "File not found"}


@app.post("/log")
async def get_log(entry: LogEntry, db_conn: DbConnection = Depends(get_db)):
    db_conn.add_log(**entry.dict())
    return {"status": "success", "message": "Log saved successfully"}
