from sqlalchemy.orm import declarative_base
from sqlalchemy import Column, String, MetaData, Integer, Identity, DateTime, UniqueConstraint

metadata = MetaData()
Base = declarative_base(metadata=metadata)


class PhoneMessage(Base):
    """Модель таблицы phone_message."""
    __tablename__ = 'phone_message'

    id = Column(Integer, Identity(), primary_key=True)
    user = Column(String(length=255), nullable=False)
    phone = Column(String(length=255), nullable=False)
    marketplace = Column(String(length=255), nullable=False)
    time_request = Column(DateTime, nullable=False)
    time_response = Column(DateTime, default=None, nullable=True)
    message = Column(String(length=255), default=None, nullable=True)

    __table_args__ = (
        UniqueConstraint('time_request', name='phone_message_time_request_unique'),
        UniqueConstraint('time_response', name='phone_message_time_response_unique'),
    )
