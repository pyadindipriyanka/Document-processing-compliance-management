from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings

settings = get_settings()
_is_sqlite = settings.DATABASE_URL.startswith("sqlite")
engine = create_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,
    connect_args={"check_same_thread": False, "timeout": 30} if _is_sqlite else {},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


if _is_sqlite:  # make SAVEPOINT / transactions behave on pysqlite (see SQLAlchemy docs, "Serializable isolation")
    @event.listens_for(engine, "connect")
    def _sqlite_connect(dbapi_conn, _):
        dbapi_conn.isolation_level = None

    @event.listens_for(engine, "begin")
    def _sqlite_begin(conn):
        conn.exec_driver_sql("BEGIN")
