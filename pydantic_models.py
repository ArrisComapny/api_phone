from pydantic import BaseModel
from datetime import datetime
from typing import Optional


class LogEntry(BaseModel):
    timestamp: datetime
    timestamp_user: Optional[datetime] = None
    action: str
    user: Optional[str] = None
    ip_address: str
    city: str
    country: str
    proxy: Optional[str] = None
    description: Optional[str] = None
