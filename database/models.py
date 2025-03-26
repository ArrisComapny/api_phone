from sqlalchemy.orm import declarative_base
from sqlalchemy import Column, String, MetaData, Integer, Identity, DateTime, Text, ForeignKey

metadata = MetaData()
Base = declarative_base(metadata=metadata)


class User(Base):
    """
    Таблица users — список пользователей системы.

    Поля:
    - user: логин
    - password: пароль
    - name: имя пользователя (опционально)
    - group: принадлежность к группе (определяет доступные компании)
    """
    __tablename__ = 'users'

    user = Column(String(length=255), primary_key=True, nullable=False)
    password = Column(String(length=255), nullable=False)
    name = Column(String(length=255), default=None, nullable=True)
    group = Column(String(length=255), ForeignKey('group_table.group', onupdate="CASCADE"), nullable=False)


class PhoneMessage(Base):
    """
    Таблица phone_message — отслеживание запросов авторизации по телефону или email.

    Поля:
    - user: пользователь, инициировавший авторизацию
    - phone: номер телефона
    - marketplace: маркетплейс
    - time_request: время запроса кода
    - time_response: время получения ответа
    - message: сам код (если получен)

    Используется для синхронизации кода подтверждения с автоматизацией входа.
    """
    __tablename__ = 'phone_message'

    id = Column(Integer, Identity(), primary_key=True)
    user = Column(String(length=255), ForeignKey('users.user', onupdate="CASCADE"), nullable=False)
    phone = Column(String(length=255), ForeignKey('connects.phone', onupdate="CASCADE"), nullable=False)
    marketplace = Column(String(length=255), ForeignKey('marketplaces.marketplace', onupdate="CASCADE"), nullable=False)
    time_request = Column(DateTime, nullable=False)
    time_response = Column(DateTime, default=None, nullable=True)
    message = Column(String(length=255), default=None, nullable=True)


class Log(Base):
    """
    Таблица log — журнал действий пользователей и системы.

    Поля:
    - id: уникальный идентификатор лога
    - timestamp: серверное время события
    - timestamp_user: локальное время пользователя (если передано)
    - action: тип действия (INFO, ERROR, WARNING и т.д.)
    - user: логин пользователя, инициировавшего действие
    - ip_address: IP-адрес клиента
    - city: определённый по IP город
    - country: определённая по IP страна
    - proxy: использованный прокси (если есть)
    - description: текстовое описание события или ошибки
    """
    __tablename__ = 'log'

    id = Column(Integer, Identity(), primary_key=True)
    timestamp = Column(DateTime, nullable=False)
    timestamp_user = Column(DateTime, default=None, nullable=True)
    action = Column(String(length=255), nullable=False)
    user = Column(String(length=255), ForeignKey('users.user'), default=None, nullable=True)
    ip_address = Column(DateTime, nullable=False)
    city = Column(String(length=255), nullable=False)
    country = Column(String(length=255), nullable=False)
    proxy = Column(String(length=255), nullable=True)
    description = Column(Text, nullable=False)


class Version(Base):
    """
    Таблица version — хранит актуальную версию и ссылку на обновление.

    Поля:
    - version: версия приложения. Пример: 1.0.4
    - url: ссылка на ZIP-обновление http://<host>:<port>/download_app
    """
    __tablename__ = 'version'

    version = Column(String(length=255), primary_key=True, nullable=False)
    url = Column(String(length=1000), primary_key=True, nullable=False)
