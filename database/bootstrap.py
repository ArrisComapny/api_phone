from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from config import DB_URL

# Один engine на всё приложение
engine = create_engine(
    url=DB_URL,
    echo=False,
    pool_size=10,
    max_overflow=5,
    pool_timeout=30,
    pool_recycle=600,
    pool_pre_ping=True,
    connect_args={
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 5,
        "connect_timeout": 10,
    },
)

# Фабрика сессий
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
