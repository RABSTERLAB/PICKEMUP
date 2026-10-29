from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from .config import settings
from .models import Base

_url_obj = make_url(settings.database_url)
if _url_obj.get_backend_name() == "sqlite":
    db = _url_obj.database or ""
    if db and ":memory:" not in db:
        import pathlib
        _p = pathlib.Path(db)
        if not _p.is_absolute():
            _p = (pathlib.Path.cwd() / _p).resolve()
        _p.parent.mkdir(parents=True, exist_ok=True)
        _url_obj = _url_obj.set(database=str(_p))
_url = _url_obj.render_as_string(hide_password=False)

engine = create_engine(
    _url,
    connect_args={"check_same_thread": False} if _url.startswith("sqlite") else {},
    pool_pre_ping=True,
)

if _url.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _sqlite_on(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


def init_db() -> None:
    Base.metadata.create_all(engine)


def get_session():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()

def session_dep():
    """FastAPI dependency over the session generator (shared by routers)."""
    yield from get_session()
