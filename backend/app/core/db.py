"""Base de datos: engine, sesion y Base declarativa.

Un esquema PostgreSQL por modulo. No se definen aqui tablas de negocio.
"""

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import get_settings


class Base(DeclarativeBase):
    """Base declarativa compartida por los modelos ORM de cada modulo."""


_settings = get_settings()

engine = create_engine(_settings.database_url, pool_pre_ping=True, future=True)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    class_=Session,
    expire_on_commit=False,
)


def get_db() -> Generator[Session, None, None]:
    """Dependencia FastAPI que entrega una sesion por peticion."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
