from typing import Optional
from datetime import datetime
from pydantic import BaseModel


class LogEntry(BaseModel):
    """
    Модель данных для записи логов в систему.
    Используется в API `/log` для передачи информации о действиях пользователей и системных событиях.
    """

    timestamp: datetime  # Время события по серверу (обязательное поле)
    timestamp_user: Optional[datetime] = None  # Время события на стороне пользователя. Необязательное поле.
    action: str  # Тип действия (например: INFO, ERROR, WARNING и т.д.)
    user: Optional[str] = None  # Имя пользователя, инициировавшего событие (если есть)
    ip_address: str  # IP-адрес клиента
    city: str  # Город клиента, определённый по IP
    country: str  # Страна клиента, определённая по IP
    proxy: Optional[str] = None  # Используемый прокси (если применимо)
    description: Optional[str] = None  # Дополнительное описание события
