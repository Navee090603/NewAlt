import os
import re
import time
import smtplib
import traceback
from dataclasses import dataclass
from datetime import datetime, date, time as dtime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Dict, Optional, Any, List
from zoneinfo import ZoneInfo

import pyodbc


IST = ZoneInfo("Asia/Kolkata")


@dataclass(frozen=True)
class StageConfig:
    name: str
    folder: str
    extension: str
    previous_stage: Optional[str]


@dataclass(frozen=True)
class Settings:
    internal_email: str = "Hari.shankaran@caresource.com"
    team1_email: str = "Thamaraipriya.Madhaiyan@caresource.com"
    from_email: str = "Naveen.Thiyagasundaram@caresource.com"
    smtp_host: str = "localhost"
    smtp_port: int = 25

    monitor_start_ist: dtime = dtime(6, 0)
    monitor_end_ist: dtime = dtime(8, 30)
    expected_file_count: int = 2
    poll_seconds: int = 10

    stuck_minutes: int = 5
    sla_window_hours: float = 2.5
    large_file_mb_threshold: int = 700
    large_file_expected_hours: float = 3.0
    large_file_update_minutes: int = 30

    mssql_conn_str: str = os.getenv(
        "ALT_MSSQL_CONN_STR",
        "Driver={ODBC Driver 17 for SQL Server};Server=localhost;Database=ALT_MONITOR;Trusted_Connection=yes;TrustServerCertificate=yes;",
    )

    stages: tuple = (
        StageConfig("STEP1", r"C:\Users\karun\Downloads\ALT\01_VU\Altruista", ".txt", None),
        StageConfig("STEP2", r"C:\Users\karun\Downloads\ALT\Step2", ".txt", "STEP1"),
        StageConfig("STEP3", r"C:\Users\karun\Downloads\ALT\Step_3", ".edi", "STEP2"),
        StageConfig("STEP4", r"C:\Users\karun\Downloads\ALT\Step4", ".edi", "STEP3"),
    )


class MSSQLStateStore:
    def __init__(self, conn_str: str):
        self.conn = pyodbc.connect(conn_str, autocommit=False)
        self.conn.timeout = 30
        self._setup_schema()

    def _setup_schema(self) -> None:
        ddl = """
IF OBJECT_ID('dbo.stage_file_state', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.stage_file_state (
        stage_name NVARCHAR(20) NOT NULL,
        file_name NVARCHAR(260) NOT NULL,
        logical_name NVARCHAR(260) NOT NULL,
        file_ext NVARCHAR(10) NOT NULL,
        file_size_bytes BIGINT NOT NULL,
        arrival_ts_ist DATETIME2 NOT NULL,
        last_seen_ts_ist DATETIME2 NOT NULL,
        moved_ts_ist DATETIME2 NULL,
        stuck_alert_sent BIT NOT NULL DEFAULT 0,
        large_file BIT NOT NULL DEFAULT 0,
        sla_breach_sent BIT NOT NULL DEFAULT 0,
        last_large_update_ts_ist DATETIME2 NULL,
        active BIT NOT NULL DEFAULT 1,
        PRIMARY KEY(stage_name, file_name)
    );

    CREATE INDEX IX_stage_file_state_stage_active ON dbo.stage_file_state(stage_name, active);
    CREATE INDEX IX_stage_file_state_stage_arrival ON dbo.stage_file_state(stage_name, arrival_ts_ist);
    CREATE INDEX IX_stage_file_state_stage_logical_moved ON dbo.stage_file_state(stage_name, logical_name, moved_ts_ist);
END;

IF OBJECT_ID('dbo.event_log', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.event_log (
        id BIGINT IDENTITY(1,1) PRIMARY KEY,
        event_ts_ist DATETIME2 NOT NULL,
        level NVARCHAR(20) NOT NULL,
        event_type NVARCHAR(100) NOT NULL,
        stage_name NVARCHAR(20) NULL,
        file_name NVARCHAR(260) NULL,
        details NVARCHAR(MAX) NULL
    );
END;
"""
        cur = self.conn.cursor()
        cur.execute(ddl)
        self.conn.commit()

    def log(self, event_ts: datetime, level: str, event_type: str, details: str, stage_name: Optional[str] = None, file_name: Optional[str] = None) -> None:
        self.conn.execute(
            "INSERT INTO dbo.event_log(event_ts_ist, level, event_type, stage_name, file_name, details) VALUES (?,?,?,?,?,?)",
            (event_ts.replace(tzinfo=None), level, event_type, stage_name, file_name, details),
        )
        self.conn.commit()

    def upsert_arrival(self, stage: str, file_name: str, logical_name: str, ext: str, file_size_bytes: int, arrival: datetime, now: datetime, large_file: bool) -> None:
        sql = """
MERGE dbo.stage_file_state AS target
USING (SELECT ? AS stage_name, ? AS file_name) AS src
ON target.stage_name = src.stage_name AND target.file_name = src.file_name
WHEN MATCHED THEN
    UPDATE SET
        file_size_bytes = ?,
        last_seen_ts_ist = ?,
        active = 1
WHEN NOT MATCHED THEN
    INSERT (stage_name, file_name, logical_name, file_ext, file_size_bytes, arrival_ts_ist, last_seen_ts_ist, large_file, active)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1);
"""
        self.conn.execute(
            sql,
            (
                stage,
                file_name,
                file_size_bytes,
                now.replace(tzinfo=None),
                stage,
                file_name,
                logical_name,
                ext,
                file_size_bytes,
                arrival.replace(tzinfo=None),
                now.replace(tzinfo=None),
                int(large_file),
            ),
        )
        self.conn.commit()

    def mark_seen(self, stage: str, file_name: str, now: datetime) -> None:
        self.conn.execute(
            "UPDATE dbo.stage_file_state SET last_seen_ts_ist=?, active=1 WHERE stage_name=? AND file_name=?",
            (now.replace(tzinfo=None), stage, file_name),
        )
        self.conn.commit()

    def mark_moved(self, stage: str, file_name: str, now: datetime) -> None:
        self.conn.execute(
            "UPDATE dbo.stage_file_state SET moved_ts_ist=?, active=0 WHERE stage_name=? AND file_name=?",
            (now.replace(tzinfo=None), stage, file_name),
        )
        self.conn.commit()

    def fetch_active(self, stage: str):
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM dbo.stage_file_state WHERE stage_name=? AND active=1", (stage,))
        return cur.fetchall()

    def fetch_by_name(self, stage: str, file_name: str):
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM dbo.stage_file_state WHERE stage_name=? AND file_name=?", (stage, file_name))
        return cur.fetchone()

    def set_stuck_alert_sent(self, stage: str, file_name: str) -> None:
        self.conn.execute(
            "UPDATE dbo.stage_file_state SET stuck_alert_sent=1 WHERE stage_name=? AND file_name=?",
            (stage, file_name),
        )
        self.conn.commit()

    def set_sla_breach_sent(self, stage: str, file_name: str) -> None:
        self.conn.execute(
            "UPDATE dbo.stage_file_state SET sla_breach_sent=1 WHERE stage_name=? AND file_name=?",
            (stage, file_name),
        )
        self.conn.commit()

    def update_large_last_ping(self, stage: str, file_name: str, now: datetime) -> None:
        self.conn.execute(
            "UPDATE dbo.stage_file_state SET last_large_update_ts_ist=? WHERE stage_name=? AND file_name=?",
            (now.replace(tzinfo=None), stage, file_name),
        )
        self.conn.commit()

    def count_arrivals_between(self, stage: str, start: datetime, end: datetime) -> int:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM dbo.stage_file_state WHERE stage_name=? AND arrival_ts_ist BETWEEN ? AND ?",
            (stage, start.replace(tzinfo=None), end.replace(tzinfo=None)),
        )
        return int(cur.fetchone()[0])

    def previous_stage_has_moved(self, stage: str, logical_name: str) -> bool:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT TOP 1 1 FROM dbo.stage_file_state WHERE stage_name=? AND logical_name=? AND moved_ts_ist IS NOT NULL",
            (stage, logical_name),
        )
        return cur.fetchone() is not None


class MultiStageFileMonitor:
    def __init__(self, cfg: Settings):
        self.cfg = cfg
        self.store = MSSQLStateStore(cfg.mssql_conn_str)
        self.stage_map = {s.name: s for s in cfg.stages}
        for stage in cfg.stages:
            Path(stage.folder).mkdir(parents=True, exist_ok=True)
        self._window_alert_sent_for: Optional[str] = None

    def _now_ist(self) -> datetime:
        return datetime.now(tz=IST)

    def _row_get(self, row: Any, key: str):
        return getattr(row, key)

    def _log(self, level: str, event_type: str, details: str, stage_name: Optional[str] = None, file_name: Optional[str] = None) -> None:
        now = self._now_ist()
        print(f"[{now.isoformat()}] {level} {event_type} stage={stage_name or '-'} file={file_name or '-'} {details}")
        self.store.log(now, level, event_type, details, stage_name, file_name)

    def _send_email(self, to_email: str, subject: str, body: str) -> bool:
        msg = EmailMessage()
        msg["From"] = self.cfg.from_email
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.set_content(body)
        try:
            with smtplib.SMTP(self.cfg.smtp_host, self.cfg.smtp_port, timeout=20) as smtp:
                smtp.send_message(msg)
            self._log("INFO", "EMAIL_SENT", f"To={to_email} Subject={subject}")
            return True
        except Exception as exc:
            self._log("ERROR", "EMAIL_FAILED", f"To={to_email} Subject={subject} Error={exc}")
            return False

    def _in_monitor_window(self, ts: datetime) -> bool:
        t = ts.timetz().replace(tzinfo=None)
        return self.cfg.monitor_start_ist <= t <= self.cfg.monitor_end_ist

    def _logical_name(self, file_name: str) -> str:
        return Path(file_name).stem

    def _file_matches_stage(self, stage: StageConfig, name: str) -> bool:
        if not name.lower().endswith(stage.extension.lower()):
            return False
        today_tag = date.today().strftime("%Y%m%d")
        return re.search(today_tag, name) is not None

    def _list_stage_files(self, stage: StageConfig) -> Dict[str, Path]:
        folder = Path(stage.folder)
        files: Dict[str, Path] = {}
        for p in folder.iterdir():
            if p.is_file() and self._file_matches_stage(stage, p.name):
                files[p.name] = p
        return files

    def _get_file_creation_time_ist(self, path: Path) -> datetime:
        try:
            ts = path.stat().st_ctime
        except OSError:
            ts = path.stat().st_mtime
        return datetime.fromtimestamp(ts, tz=IST)

    def _send_arrival_email(self, stage: str, file_name: str, size: int, arrival: datetime) -> None:
        body = (
            f"Hi Team,\n\n"
            f"File received in {stage}.\n"
            f"File name: {file_name}\n"
            f"Size: {size / (1024 * 1024):.2f} MB\n"
            f"Arrival time (IST): {arrival.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            f"Regards,\nALT Monitor Bot"
        )
        self._send_email(self.cfg.team1_email, f"[ALT][{stage}] File received - {file_name}", body)

    def _send_moved_email(self, stage: str, row: Any) -> None:
        body = (
            f"Hi Team,\n\n"
            f"File moved out of {stage}.\n"
            f"File name: {self._row_get(row, 'file_name')}\n"
            f"Size: {self._row_get(row, 'file_size_bytes') / (1024 * 1024):.2f} MB\n"
            f"Arrival time (IST): {self._row_get(row, 'arrival_ts_ist').strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"Moved time (IST): {self._now_ist().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            f"Regards,\nALT Monitor Bot"
        )
        self._send_email(self.cfg.team1_email, f"[ALT][{stage}] File moved - {self._row_get(row, 'file_name')}", body)

    def _send_stuck_alert(self, stage: str, row: Any) -> None:
        body = (
            f"Hi Team,\n\n"
            f"File appears stuck in {stage} for more than {self.cfg.stuck_minutes} minutes.\n"
            f"File name: {self._row_get(row, 'file_name')}\n"
            f"Size: {self._row_get(row, 'file_size_bytes') / (1024 * 1024):.2f} MB\n"
            f"Arrival time (IST): {self._row_get(row, 'arrival_ts_ist').strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"Status: STUCK\n\n"
            f"Regards,\nALT Monitor Bot"
        )
        if self._send_email(self.cfg.internal_email, f"[ALT][{stage}] Stuck file alert - {self._row_get(row, 'file_name')}", body):
            self.store.set_stuck_alert_sent(stage, self._row_get(row, "file_name"))

    def _send_sla_breach_alert(self, stage: str, row: Any, eta: datetime) -> None:
        body = (
            f"Hi Team,\n\n"
            f"Potential SLA breach detected for large file in {stage}.\n"
            f"File name: {self._row_get(row, 'file_name')}\n"
            f"Size: {self._row_get(row, 'file_size_bytes') / (1024 * 1024):.2f} MB\n"
            f"Arrival time (IST): {self._row_get(row, 'arrival_ts_ist').strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"Expected completion (IST): {eta.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            f"Regards,\nALT Monitor Bot"
        )
        if self._send_email(self.cfg.internal_email, f"[ALT][{stage}] SLA breach alert - {self._row_get(row, 'file_name')}", body):
            self.store.set_sla_breach_sent(stage, self._row_get(row, "file_name"))

    def _send_large_progress_update(self, stage: str, row: Any, eta: datetime, completed: bool = False) -> None:
        now = self._now_ist()
        if completed:
            status = f"Large file processing completed at {now.strftime('%Y-%m-%d %H:%M:%S')} IST."
        else:
            remaining_minutes = max(0, int((eta - now).total_seconds() // 60))
            status = f"Large file still processing; approx {remaining_minutes} minutes remaining to ETA."

        body = (
            f"Hi Team,\n\n"
            f"Stage: {stage}\n"
            f"File name: {self._row_get(row, 'file_name')}\n"
            f"Size: {self._row_get(row, 'file_size_bytes') / (1024 * 1024):.2f} MB\n"
            f"Arrival time (IST): {self._row_get(row, 'arrival_ts_ist').strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"ETA (IST): {eta.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"Status: {status}\n\n"
            f"Regards,\nALT Monitor Bot"
        )
        subject = f"[ALT][{stage}] Large file {'completed' if completed else 'progress'} - {self._row_get(row, 'file_name')}"
        if self._send_email(self.cfg.internal_email, subject, body):
            self.store.update_large_last_ping(stage, self._row_get(row, "file_name"), now)

    def _process_stage_arrival(self, stage: StageConfig, name: str, path: Path, now: datetime) -> None:
        row = self.store.fetch_by_name(stage.name, name)
        size = path.stat().st_size
        logical = self._logical_name(name)

        if row is not None:
            self.store.mark_seen(stage.name, name, now)
            return

        if stage.previous_stage:
            if not self.store.previous_stage_has_moved(stage.previous_stage, logical):
                self._log("INFO", "ARRIVAL_BLOCKED", f"Previous stage {stage.previous_stage} not completed for {logical}", stage.name, name)
                return

        arrival = self._get_file_creation_time_ist(path)
        if stage.name == "STEP1" and not self._in_monitor_window(arrival):
            self._log("INFO", "ARRIVAL_IGNORE", "Creation time outside STEP1 monitoring window", stage.name, name)
            return

        large = size >= (self.cfg.large_file_mb_threshold * 1024 * 1024)
        self.store.upsert_arrival(stage.name, name, logical, stage.extension, size, arrival, now, large)
        self._send_arrival_email(stage.name, name, size, arrival)
        self._log("INFO", "ARRIVED", f"size={size}", stage.name, name)

    def _backfill_stage(self, stage: StageConfig) -> None:
        now = self._now_ist()
        for name, path in self._list_stage_files(stage).items():
            if self.store.fetch_by_name(stage.name, name):
                continue
            self._process_stage_arrival(stage, name, path, now)

    def _handle_stage_files(self, stage: StageConfig) -> None:
        now = self._now_ist()
        current = self._list_stage_files(stage)

        for name, path in current.items():
            self._process_stage_arrival(stage, name, path, now)

        for row in self.store.fetch_active(stage.name):
            name = self._row_get(row, "file_name")
            if name not in current:
                self._send_moved_email(stage.name, row)
                self.store.mark_moved(stage.name, name, now)

                if self._row_get(row, "large_file"):
                    arrival = self._row_get(row, "arrival_ts_ist").replace(tzinfo=IST)
                    eta = arrival + timedelta(hours=self.cfg.large_file_expected_hours)
                    self._send_large_progress_update(stage.name, row, eta, completed=True)

                self._log("INFO", "MOVED", "File moved out", stage.name, name)

    def _handle_window_expectation_step1(self) -> None:
        now = self._now_ist()
        if now.timetz().replace(tzinfo=None) < self.cfg.monitor_end_ist:
            return

        day_key = now.date().isoformat()
        if self._window_alert_sent_for == day_key:
            return

        start = datetime.combine(now.date(), self.cfg.monitor_start_ist, tzinfo=IST)
        end = datetime.combine(now.date(), self.cfg.monitor_end_ist, tzinfo=IST)
        count = self.store.count_arrivals_between("STEP1", start, end)
        if count < self.cfg.expected_file_count:
            body = (
                f"Hi Team,\n\n"
                f"Expected {self.cfg.expected_file_count} STEP1 files between "
                f"{self.cfg.monitor_start_ist.strftime('%H:%M')} and {self.cfg.monitor_end_ist.strftime('%H:%M')} IST.\n"
                f"Received count: {count}.\n"
                f"Missing count: {self.cfg.expected_file_count - count}.\n\n"
                f"Regards,\nALT Monitor Bot"
            )
            if self._send_email(self.cfg.internal_email, "[ALT][STEP1] Missing file alert", body):
                self._window_alert_sent_for = day_key

    def _handle_stuck_and_sla(self, stage: StageConfig) -> None:
        now = self._now_ist()
        for row in self.store.fetch_active(stage.name):
            arrival = self._row_get(row, "arrival_ts_ist").replace(tzinfo=IST)
            age = now - arrival

            if self._row_get(row, "large_file"):
                eta = arrival + timedelta(hours=self.cfg.large_file_expected_hours)
                if now >= arrival + timedelta(hours=self.cfg.sla_window_hours) and not self._row_get(row, "sla_breach_sent"):
                    self._send_sla_breach_alert(stage.name, row, eta)

                last_update = self._row_get(row, "last_large_update_ts_ist")
                if last_update is None or now - last_update.replace(tzinfo=IST) >= timedelta(minutes=self.cfg.large_file_update_minutes):
                    self._send_large_progress_update(stage.name, row, eta)
                continue

            if age >= timedelta(minutes=self.cfg.stuck_minutes) and not self._row_get(row, "stuck_alert_sent"):
                self._send_stuck_alert(stage.name, row)

    def run_forever(self) -> None:
        self._log("INFO", "START", "Starting ALT multi-stage monitor (MSSQL mode)")
        for stage in self.cfg.stages:
            self._backfill_stage(stage)

        while True:
            try:
                for stage in self.cfg.stages:
                    self._handle_stage_files(stage)
                    self._handle_stuck_and_sla(stage)
                self._handle_window_expectation_step1()
            except Exception as exc:
                self._log("ERROR", "LOOP_EXCEPTION", f"{exc}\n{traceback.format_exc()}")
            time.sleep(self.cfg.poll_seconds)


def main() -> None:
    monitor = MultiStageFileMonitor(Settings())
    monitor.run_forever()


if __name__ == "__main__":
    main()
