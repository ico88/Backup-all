from sqlalchemy import (
    Column, Integer, String, DateTime, Boolean, Text, ForeignKey, Enum as SAEnum
)
from sqlalchemy.orm import relationship
from datetime import datetime, timezone
import enum
from app.database import Base


class ServerType(str, enum.Enum):
    WINDOWS = "windows"
    LINUX = "linux"


class BackupType(str, enum.Enum):
    VM_SNAPSHOT = "vm_snapshot"       # ESXi snapshot + export OVF
    APP_DATA = "app_data"             # Solo dati applicativi
    FULL = "full"                     # VM + dati applicativi


class JobStatus(str, enum.Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    DISABLED = "disabled"


class RunStatus(str, enum.Enum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    PARTIAL = "partial"


class VMwareHost(Base):
    """Configurazione host ESXi"""
    __tablename__ = "vmware_hosts"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    host = Column(String(255), nullable=False)
    port = Column(Integer, default=443)
    username = Column(String(100), nullable=False)
    password_enc = Column(Text, nullable=False)  # cifrato
    ssl_verify = Column(Boolean, default=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    servers = relationship("Server", back_populates="vmware_host")


class Server(Base):
    """VM/server da cui fare backup"""
    __tablename__ = "servers"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False, unique=True)
    description = Column(Text)
    server_type = Column(SAEnum(ServerType), nullable=False)
    ip_address = Column(String(45), nullable=False)
    vm_name = Column(String(255))           # nome VM su ESXi
    vmware_host_id = Column(Integer, ForeignKey("vmware_hosts.id"))
    ssh_port = Column(Integer, default=22)  # Linux
    winrm_port = Column(Integer, default=5985)  # Windows
    username = Column(String(100))
    password_enc = Column(Text)
    ssh_key_path = Column(Text)             # percorso chiave privata SSH
    app_name = Column(String(100))          # es. "gamma", "abulafia"
    app_data_paths = Column(Text)           # JSON array di percorsi
    app_db_type = Column(String(50))        # "mssql", "postgresql", "mysql", ecc.
    app_db_name = Column(String(100))
    app_db_user = Column(String(100))
    app_db_password_enc = Column(Text)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    vmware_host = relationship("VMwareHost", back_populates="servers")
    backup_jobs = relationship("BackupJob", back_populates="server")


class BackupDestination(Base):
    """Destinazione backup (QNAP o altro)"""
    __tablename__ = "backup_destinations"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    description = Column(Text)
    dest_type = Column(String(50), nullable=False)  # "rsync", "smb", "qnap_api"
    host = Column(String(255), nullable=False)
    port = Column(Integer)
    username = Column(String(100))
    password_enc = Column(Text)
    base_path = Column(String(500), nullable=False)  # percorso radice sul QNAP
    rsync_module = Column(String(100))               # modulo rsync se usato
    max_retention_days = Column(Integer, default=30)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    backup_jobs = relationship("BackupJob", back_populates="destination")


class BackupJob(Base):
    """Job di backup: collega server, destinazione, tipo e schedule"""
    __tablename__ = "backup_jobs"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    description = Column(Text)
    server_id = Column(Integer, ForeignKey("servers.id"), nullable=False)
    destination_id = Column(Integer, ForeignKey("backup_destinations.id"), nullable=False)
    backup_type = Column(SAEnum(BackupType), nullable=False)
    status = Column(SAEnum(JobStatus), default=JobStatus.ACTIVE)
    cron_expression = Column(String(100), nullable=False)  # es. "0 2 * * *"
    retention_copies = Column(Integer, default=7)
    compression = Column(Boolean, default=True)
    notify_email = Column(String(255))
    last_run_at = Column(DateTime)
    last_run_status = Column(SAEnum(RunStatus))
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    server = relationship("Server", back_populates="backup_jobs")
    destination = relationship("BackupDestination", back_populates="backup_jobs")
    runs = relationship("BackupRun", back_populates="job", order_by="BackupRun.started_at.desc()")


class BackupRun(Base):
    """Singola esecuzione di un job"""
    __tablename__ = "backup_runs"

    id = Column(Integer, primary_key=True)
    job_id = Column(Integer, ForeignKey("backup_jobs.id"), nullable=False)
    status = Column(SAEnum(RunStatus), default=RunStatus.RUNNING)
    started_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    finished_at = Column(DateTime)
    size_bytes = Column(Integer)           # dimensione backup
    backup_path = Column(Text)             # percorso finale sul QNAP
    error_message = Column(Text)
    triggered_by = Column(String(50), default="scheduler")  # "scheduler" o "manual"

    job = relationship("BackupJob", back_populates="runs")
    logs = relationship("BackupLog", back_populates="run", order_by="BackupLog.timestamp")


class BackupLog(Base):
    """Log dettagliato di ogni run"""
    __tablename__ = "backup_logs"

    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("backup_runs.id"), nullable=False)
    timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    level = Column(String(10), default="INFO")  # INFO, WARNING, ERROR
    message = Column(Text, nullable=False)

    run = relationship("BackupRun", back_populates="logs")
