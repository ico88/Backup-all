from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
import os

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./backup_all.db")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    from app import models  # noqa: F401
    Base.metadata.create_all(bind=engine)
    _migrate()


def _migrate():
    """Aggiunge colonne mancanti per aggiornamenti incrementali."""
    with engine.connect() as conn:
        migrations = [
            ("backup_destinations", "smb_share", "VARCHAR(200)"),
            ("servers", "xcp_host_id", "INTEGER REFERENCES xcp_hosts(id)"),
            ("backup_runs", "backup_mode", "VARCHAR(20) DEFAULT 'full'"),
            ("vm_replication_jobs", "target_network", "VARCHAR(255)"),
        ]
        for table, column, col_type in migrations:
            try:
                conn.execute(__import__("sqlalchemy").text(
                    f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"
                ))
                conn.commit()
            except Exception:
                pass  # colonna già esistente
