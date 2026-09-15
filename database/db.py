import time
import logging

from functools import wraps
from sqlalchemy.orm import Session
from pyodbc import Error as PyodbcError
from datetime import datetime, timedelta
from sqlalchemy.exc import OperationalError
from sqlalchemy import create_engine, func as f, select, or_, and_

from config import DB_URL
from database.models import *
from sms_routing import marketplace_roles

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
        except Exception as e:
            # Откат обязателен: без него транзакция остаётся aborted,
            # и все следующие запросы в этой сессии падают — в том числе add_message
            print(f"get_tg_id: {e}")
            self.session.rollback()
            return None

    @retry_on_exception()
    def get_shops_for_phone(self, phone10: str, marketplace: str) -> list[str]:
        """
        Магазины (name_company) площадки `marketplace`, зарегистрированные на номере.
        phone10 — 10 цифр без ведущей 7 (формат markets.phone). Пустой список — в базе нет.
        """
        try:
            stmt = (select(Market.name_company)
                    .where(Market.phone == phone10, Market.marketplace == marketplace)
                    .distinct())
            return [r.name_company for r in self.session.execute(stmt).all() if r.name_company]
        except Exception as e:
            print(f"get_shops_for_phone: {e}")
            self.session.rollback()
            return []

    @retry_on_exception()
    def get_marketplaces_by_number(self, phone10: str) -> list[str]:
        """
        Список marketplace-номеров живёт в таблице markets (phone -> marketplace).
        Возвращает площадки, на которые зарегистрирован номер (10 цифр без 7).
        Пустой список — номер не marketplace-номер. Добавить номер = строка в markets.
        """
        try:
            stmt = select(Market.marketplace).where(Market.phone == phone10).distinct()
            return [r.marketplace for r in self.session.execute(stmt).all() if r.marketplace]
        except Exception as e:
            print(f"get_marketplaces_by_number: {e}")
            self.session.rollback()
            return []

    @retry_on_exception()
    def get_users_by_marketplace_role(self, marketplace: str) -> list[str]:
        """
        Получатели SMS площадки (только status='works'):
          - employees.role = 'head <мп>' или 'manager <мп>';
          - employees.role = 'admin' с включённым receive_sms.
        Привязки номеров здесь не учитываются: marketplace-номер уходит всем менеджерам площадки.
        'rating' и 'manager' без площадки сюда не попадают никогда.
        Пустой список — ни у кого нет роли (или ошибка БД — в лог).
        """
        roles = marketplace_roles(marketplace)
        if not roles:
            return []

        try:
            stmt = (select(Employee.tg_user_id)
                    .where(Employee.status == "works",
                           or_(Employee.role.in_(roles),
                               and_(Employee.role == "admin", Employee.receive_sms.is_(True))))
                    .distinct())
            return [r.tg_user_id for r in self.session.execute(stmt).all()]
        except Exception as e:
            print(f"get_users_by_marketplace_role: {e}")
            self.session.rollback()
            return []

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
                    PhoneMessage.marketplace.in_(['WB', 'Ozon', 'Yandex', 'МВидео']),
                    PhoneMessage.time_response.is_(None),
                    PhoneMessage.message.is_(None),
                    PhoneMessage.time_request <= time_response + timedelta(seconds=15),
                    PhoneMessage.time_request >= time_response - timedelta(minutes=2)
                ).order_by(PhoneMessage.time_request.asc()).first()
            else:
                # Поиск по конкретному маркетплейсу
                mes = self.session.query(PhoneMessage).filter(
                    PhoneMessage.phone == virtual_phone_number,
                    PhoneMessage.marketplace == marketplace,
                    PhoneMessage.time_response.is_(None),
                    PhoneMessage.message.is_(None),
                    PhoneMessage.time_request <= time_response + timedelta(seconds=15),
                    PhoneMessage.time_request >= time_response - timedelta(minutes=2)
                ).order_by(PhoneMessage.time_request.asc()).first()

            if mes:
                # Обновление найденной записи
                mes.time_response = time_response
                mes.message = message
                self.session.commit()
                print(f"add_message: код {message} записан в заявку id={mes.id} "
                      f"(phone={virtual_phone_number}, mp={marketplace})")
                break

            # Освобождаем соединение на время паузы: иначе оно держится все 30 сек
            # и пул (10+5) выедается — остальные запросы виснут по pool_timeout
            self.session.rollback()
            time.sleep(3)
        else:
            # Заявка не нашлась за 30 сек — код потерян, без лога это незаметно
            print(f"add_message: заявка НЕ найдена - phone={virtual_phone_number}, "
                  f"mp={marketplace}, time_response={time_response}, код={message}")

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

    @retry_on_exception()
    def add_code(self, virtual_phone_number: str, time_response: datetime, code: str) -> None:
        """
        Добавление кода подтверждения SMS в таблицу phone_message.

        Производит поиск по номеру, маркетплейсу и диапазону времени (±2 минуты от time_response),
        и обновляет соответствующую запись.
        """

        code = PhoneCode(phone=virtual_phone_number,
                         time_response=time_response,
                         code=code)
        self.session.add(code)
        self.session.commit()
