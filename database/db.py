import time
import logging

from functools import wraps
from datetime import datetime, timedelta

from sqlalchemy.orm import Session
from sqlalchemy import create_engine, func as f
from pyodbc import Error as PyodbcError
from sqlalchemy.exc import OperationalError

from config import DB_URL
from database.models import *

logger = logging.getLogger(__name__)


def retry_on_exception(retries=3, delay=10):
    def decorator(func):
        @wraps(func)
        def wrapper(self, *args, **kwargs):
            attempt = 0
            while attempt < retries:
                try:
                    result = func(self, *args, **kwargs)
                    return result
                except (OperationalError, PyodbcError) as e:
                    attempt += 1
                    logger.debug(f"Error occurred: {e}. Retrying {attempt}/{retries} after {delay} seconds...")
                    time.sleep(delay)
                    if hasattr(self, 'session'):
                        self.session.rollback()
                except Exception as e:
                    logger.error(f"An unexpected error occurred: {e}. Rolling back...")
                    if hasattr(self, 'session'):
                        self.session.rollback()
                    raise e
            raise RuntimeError("Max retries exceeded. Operation failed.")

        return wrapper

    return decorator


class DbConnection:
    def __init__(self, echo: bool = False) -> None:
        self.engine = create_engine(url=DB_URL,
                                    echo=echo,
                                    pool_size=10,
                                    max_overflow=5,
                                    pool_timeout=30,
                                    pool_recycle=1800,
                                    pool_pre_ping=True,
                                    connect_args={"keepalives": 1,
                                                  "keepalives_idle": 180,
                                                  "keepalives_interval": 60,
                                                  "keepalives_count": 20,
                                                  "connect_timeout": 10})
        self.session = Session(self.engine)

    @retry_on_exception()
    def add_message(self, virtual_phone_number: str, time_response: datetime, message: str,
                    marketplace: str = None) -> None:
        if marketplace:
            mes = self.session.query(PhoneMessage).filter(
                PhoneMessage.phone == virtual_phone_number,
                PhoneMessage.marketplace == marketplace,
                PhoneMessage.time_response.is_(None),
                PhoneMessage.message.is_(None),
                PhoneMessage.time_request < time_response,
                PhoneMessage.time_request >= time_response - timedelta(minutes=2)
            ).order_by(PhoneMessage.time_request.asc()).first()

            if mes:
                mes.time_response = time_response
                mes.message = message
                self.session.commit()

    @retry_on_exception()
    def add_log(self, timestamp: datetime, timestamp_user: datetime, action: str, user: str, ip_address: str,
                city: str, country: str, proxy: str, description: str) -> None:
        user_bd = self.session.query(User).filter(f.lower(User.user) == user.lower()).first()
        log = Log(timestamp=timestamp,
                  timestamp_user=timestamp_user,
                  action=action,
                  user=user_bd.user,
                  ip_address=ip_address,
                  city=city,
                  country=country,
                  proxy=proxy,
                  description=description)

        self.session.add(log)
        self.session.commit()
