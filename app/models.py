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
    VMWARE = "vmware"
    XCPNG = "xcpng"


class BackupType(str, enum.Enum):
    VM_SNAPSHOT = "vm_snapshot"
    APP_DATA = "app_data"
    FULL = "full"


class JobStatus(str, enum.Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    DISABLED = "disabled"


class RunStatus(str, enum.Enum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    PARTIAL = "partial"


class User(Base):
    """Utente dell'applicazione"""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    username = Column(String(80), nullable=False, unique=True)
    email = Column(String(255))
    hashed_password = Column(Text, nullable=False)
    is_admin = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    last_login_at = Column(DateTime)


class SystemSettings(Base):
    """Impostazioni di sistema (chiave/valore)"""
    __tablename__ = "system_settings"

    id = Column(Integer, primary_key=True)
    key = Column(String(100), nullable=False, unique=True)
    value = Column(Text)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))


class VMwareHost(Base):
    __tablename__ = "vmware_hosts"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    host = Column(String(255), nullable=False)
    port = Column(Integer, default=443)
    username = Column(String(100), nullable=False)
    password_enc = Column(Text, nullable=False)
    ssl_verify = Column(Boolean, default=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    servers = relationship("Server", back_populates="vmware_host", foreign_keys="[Server.vmware_host_id]")


class XCPHost(Base):
    __tablename__ = "xcp_hosts"
    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    host = Column(String(255), nullable=False)
    port = Column(Integer, default=443)
    username = Column(String(100), nullable=False)
    password_enc = Column(Text, nullable=False)
    ssl_verify = Column(Boolean, default=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    servers = relationship("Server", back_populates="xcp_host", foreign_keys="[Server.xcp_host_id]")


class ReplicationStatus(str, enum.Enum):
    ACTIVE = "active"
    PAUSED = "paused"

class ReplicationSyncStatus(str, enum.Enum):
    NEVER = "never"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"

class FailoverState(str, enum.Enum):
    NORMAL = "normal"        # VM-A primaria, VM-B standby
    FAILOVER = "failover"    # VM-B promossa a primaria

class VMReplicationJob(Base):
    __tablename__ = "vm_replication_jobs"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    description = Column(Text)

    # Sorgente (VM primaria)
    source_host_id = Column(Integer, ForeignKey("vmware_hosts.id"), nullable=False)
    source_vm_name = Column(String(255), nullable=False)

    # Destinazione (VM standby)
    target_host_id = Column(Integer, ForeignKey("vmware_hosts.id"), nullable=False)
    target_vm_name = Column(String(255), nullable=False)
    target_datastore = Column(String(255))  # datastore su ESXi target (opzionale)

    # Schedule
    cron_expression = Column(String(100), nullable=False, default="0 2 * * *")
    status = Column(SAEnum(ReplicationStatus), default=ReplicationStatus.ACTIVE)

    # Auto-failover
    auto_failover = Column(Boolean, default=False)
    heartbeat_interval_sec = Column(Integer, default=60)   # ogni quanto controlla
    heartbeat_max_failures = Column(Integer, default=3)    # quanti fail consecutivi prima di failover
    heartbeat_failures = Column(Integer, default=0)        # contatore corrente
    source_vm_ip = Column(String(45))                      # IP da pingare per heartbeat (opzionale)

    # Stato
    failover_state = Column(SAEnum(FailoverState), default=FailoverState.NORMAL)
    last_sync_at = Column(DateTime)
    last_sync_status = Column(SAEnum(ReplicationSyncStatus), default=ReplicationSyncStatus.NEVER)
    last_sync_duration_sec = Column(Integer)
    last_heartbeat_at = Column(DateTime)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    source_host = relationship("VMwareHost", foreign_keys=[source_host_id])
    target_host = relationship("VMwareHost", foreign_keys=[target_host_id])
    runs = relationship("VMReplicationRun", back_populates="job", order_by="VMReplicationRun.started_at.desc()")


class VMReplicationRun(Base):
    __tablename__ = "vm_replication_runs"

    id = Column(Integer, primary_key=True)
    job_id = Column(Integer, ForeignKey("vm_replication_jobs.id"), nullable=False)
    started_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    finished_at = Column(DateTime)
    status = Column(SAEnum(ReplicationSyncStatus), default=ReplicationSyncStatus.RUNNING)
    triggered_by = Column(String(50), default="scheduler")  # "scheduler", "manual", "heartbeat_failover"
    error_message = Column(Text)
    log_output = Column(Text)

    job = relationship("VMReplicationJob", back_populates="runs")


class Server(Base):
    __tablename__ = "servers"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False, unique=True)
    description = Column(Text)
    server_type = Column(SAEnum(ServerType), nullable=False)
    ip_address = Column(String(45), nullable=False)
    vm_name = Column(String(255))
    vmware_host_id = Column(Integer, ForeignKey("vmware_hosts.id"))
    ssh_port = Column(Integer, default=22)
    winrm_port = Column(Integer, default=5985)
    username = Column(String(100))
    password_enc = Column(Text)
    ssh_key_path = Column(Text)
    app_name = Column(String(100))
    app_data_paths = Column(Text)
    app_db_type = Column(String(50))
    app_db_name = Column(String(100))
    app_db_user = Column(String(100))
    app_db_password_enc = Column(Text)
    xcp_host_id = Column(Integer, ForeignKey("xcp_hosts.id"))
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    vmware_host = relationship("VMwareHost", back_populates="servers", foreign_keys=[vmware_host_id])
    xcp_host = relationship("XCPHost", back_populates="servers", foreign_keys=[xcp_host_id])
    backup_jobs = relationship("BackupJob", back_populates="server")


class BackupDestination(Base):
    __tablename__ = "backup_destinations"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    description = Column(Text)
    dest_type = Column(String(50), nullable=False)
    host = Column(String(255), nullable=False)
    port = Column(Integer)
    username = Column(String(100))
    password_enc = Column(Text)
    base_path = Column(String(500), nullable=False)
    rsync_module = Column(String(100))
    smb_share = Column(String(200))
    max_retention_days = Column(Integer, default=30)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    backup_jobs = relationship("BackupJob", back_populates="destination")


class BackupJob(Base):
    __tablename__ = "backup_jobs"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    description = Column(Text)
    server_id = Column(Integer, ForeignKey("servers.id"), nullable=False)
    destination_id = Column(Integer, ForeignKey("backup_destinations.id"), nullable=False)
    backup_type = Column(SAEnum(BackupType), nullable=False)
    status = Column(SAEnum(JobStatus), default=JobStatus.ACTIVE)
    cron_expression = Column(String(100), nullable=False)
    retention_copies = Column(Integer, default=7)
    compression = Column(Boolean, default=True)
    notify_email = Column(String(255))
    notify_on_success = Column(Boolean, default=False)
    notify_on_failure = Column(Boolean, default=True)
    verify_integrity = Column(Boolean, default=True)
    last_run_at = Column(DateTime)
    last_run_status = Column(SAEnum(RunStatus))
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))

    server = relationship("Server", back_populates="backup_jobs")
    destination = relationship("BackupDestination", back_populates="backup_jobs")
    runs = relationship("BackupRun", back_populates="job",
                        order_by="BackupRun.started_at.desc()")


class BackupRun(Base):
    __tablename__ = "backup_runs"

    id = Column(Integer, primary_key=True)
    job_id = Column(Integer, ForeignKey("backup_jobs.id"), nullable=False)
    status = Column(SAEnum(RunStatus), default=RunStatus.RUNNING)
    started_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    finished_at = Column(DateTime)
    size_bytes = Column(Integer)
    backup_path = Column(Text)
    error_message = Column(Text)
    triggered_by = Column(String(50), default="scheduler")
    checksum_sha256 = Column(String(64))
    integrity_verified = Column(Boolean)

    job = relationship("BackupJob", back_populates="runs")
    logs = relationship("BackupLog", back_populates="run",
                        order_by="BackupLog.timestamp")


class BackupLog(Base):
    __tablename__ = "backup_logs"

    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("backup_runs.id"), nullable=False)
    timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    level = Column(String(10), default="INFO")
    message = Column(Text, nullable=False)

    run = relationship("BackupRun", back_populates="logs")
