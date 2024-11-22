from sqlalchemy.orm import declarative_base
from sqlalchemy import Column, String, MetaData, Integer, Identity, DateTime, UniqueConstraint, Text, ForeignKey

metadata = MetaData()
Base = declarative_base(metadata=metadata)


class User(Base):
    """Модель таблицы connects."""
    __tablename__ = 'users'

    user = Column(String(length=255), primary_key=True, nullable=False)
    password = Column(String(length=255), nullable=False)
    name = Column(String(length=255), default=None, nullable=True)


class PhoneMessage(Base):
    """Модель таблицы phone_message."""
    __tablename__ = 'phone_message'

    id = Column(Integer, Identity(), primary_key=True)
    user = Column(String(length=255), ForeignKey('users.user'), nullable=False)
    phone = Column(String(length=255), nullable=False)
    marketplace = Column(String(length=255), nullable=False)
    time_request = Column(DateTime, nullable=False)
    time_response = Column(DateTime, default=None, nullable=True)
    message = Column(String(length=255), default=None, nullable=True)

    __table_args__ = (
        UniqueConstraint('time_request', name='phone_message_time_request_unique'),
        UniqueConstraint('time_response', name='phone_message_time_response_unique'),
    )


class Log(Base):
    """Модель таблицы log."""
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
