import time
import logging

from functools import wraps
from sqlalchemy.orm import Session
from pyodbc import Error as PyodbcError
from datetime import datetime, timedelta
from sqlalchemy.exc import OperationalError
from sqlalchemy import create_engine, func as f, select

from config import DB_URL
from database.models import *

logger = logging.getLogger(__name__)


def retry_on_exception(retries=3, delay=10):
    """
    Декоратор для повторной попытки выполнения метода при ошибках подключения к БД.

    Повторяет вызов до `retries` раз с задержкой `delay` секунд.
    Откатывает сессию при каждой неудачной попытке.
    """
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
    """
    Класс для работы с базой данных через SQLAlchemy.
    Управляет соединением, сессией и предоставляет методы для операций.
    """

    def __init__(self, session: Session):
        self.session = session

    @retry_on_exception()
    def get_version(self) -> str:
        """Получение текущей версии приложения из таблицы `version`"""

        version = self.session.query(Version).first()
        return version.version

    @retry_on_exception()
    def get_tg_id(self, phone: str) -> list[str] | None:
        """Получение списка id Telegram для отправки сообщений из таблицы `employee_mtsnumbers`"""

        tg_ids = []

        try:
            stmt = (select(EmployeeNumber.employee_id)
                    .join(Employee, Employee.tg_user_id == EmployeeNumber.employee_id)
                    .where(EmployeeNumber.phone == phone, Employee.status == "works").distinct())

            result = self.session.execute(stmt).all()

            if result:
                tg_ids = [e.employee_id for e in result]
            return tg_ids
        except:
            return None

    @retry_on_exception()
    def add_message(self, virtual_phone_number: str, time_response: datetime, message: str,
                    marketplace: str = None) -> None:
        """
        Добавление кода подтверждения SMS в таблицу phone_message.

        Производит поиск по номеру, маркетплейсу и диапазону времени (±2 минуты от time_response),
        и обновляет соответствующую запись.
        """

        for _ in range(10):
            if marketplace is None:
                # Поиск по нескольким маркетплейсам, если не указан явно
                mes = self.session.query(PhoneMessage).filter(
                    PhoneMessage.phone == virtual_phone_number,
                    PhoneMessage.marketplace.in_(['Ozon', 'Yandex']),
                    PhoneMessage.time_response.is_(None),
                    PhoneMessage.message.is_(None),
                    PhoneMessage.time_request <= time_response + timedelta(seconds=5),
                    PhoneMessage.time_request >= time_response - timedelta(minutes=2)
                ).order_by(PhoneMessage.time_request.asc()).first()
            else:
                # Поиск по конкретному маркетплейсу
                mes = self.session.query(PhoneMessage).filter(
                    PhoneMessage.phone == virtual_phone_number,
                    PhoneMessage.marketplace == marketplace,
                    PhoneMessage.time_response.is_(None),
                    PhoneMessage.message.is_(None),
                    PhoneMessage.time_request <= time_response + timedelta(seconds=5),
                    PhoneMessage.time_request >= time_response - timedelta(minutes=2)
                ).order_by(PhoneMessage.time_request.asc()).first()

            if mes:
                # Обновление найденной записи
                mes.time_response = time_response
                mes.message = message
                self.session.commit()
                break
            time.sleep(3)

    @retry_on_exception()
    def add_log(self,
                timestamp: datetime,
                timestamp_user: datetime,
                action: str,
                user: str,
                ip_address: str,
                city: str,
                country: str,
                proxy: str,
                description: str) -> None:
        """
        Добавление записи в лог действий (`log`).

        Проверяет, существует ли пользователь (если передан),
        и записывает лог с данными по IP, локации, действию и описанием.
        """
        user_name = None
        if user:
            # Приведение логина к регистронезависимому виду
            user_bd = self.session.query(User).filter(f.lower(User.user) == user.lower()).first()
            if user_bd:
                user_name = user_bd.user

        log = Log(
            timestamp=timestamp,
            timestamp_user=timestamp_user,
            action=action,
            user=user_name,
            ip_address=ip_address,
            city=city,
            country=country,
            proxy=proxy,
            description=description or ''
        )

        self.session.add(log)
        self.session.commit()
