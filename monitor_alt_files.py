import json
import logging
import os
import re
import smtplib
import time
import traceback
from dataclasses import dataclass, asdict
from datetime import datetime, date, time as dtime, timedelta
from email.message import EmailMessage
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Dict, Optional, Tuple, List
from zoneinfo import ZoneInfo


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

    state_file: str = "state/monitor_state.json"
    log_dir: str = "logs"
    event_log_jsonl: str = "logs/events.jsonl"

    stages: Tuple[StageConfig, ...] = (
        StageConfig("STEP1", r"C:\Users\karun\Downloads\ALT\01_VU\Altruista", ".txt", None),
        StageConfig("STEP2", r"C:\Users\karun\Downloads\ALT\Step2", ".txt", "STEP1"),
        StageConfig("STEP3", r"C:\Users\karun\Downloads\ALT\Step_3", ".edi", "STEP2"),
        StageConfig("STEP4", r"C:\Users\karun\Downloads\ALT\Step4", ".edi", "STEP3"),
    )


class JsonStateStore:
    """Lightweight non-SQL persistence for production runtime continuity."""

    def __init__(self, state_file: str):
        self.path = Path(state_file)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.tmp_path = self.path.with_suffix(".tmp")
        self.data = self._load_or_init()

    def _load_or_init(self) -> Dict:
        if not self.path.exists():
            return {
                "files": {},
                "daily": {},
            }
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {
                "files": {},
                "daily": {},
            }

    def save(self) -> None:
        self.tmp_path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        self.tmp_path.replace(self.path)

    @staticmethod
    def _key(stage: str, file_name: str) -> str:
        return f"{stage}::{file_name}"

    def get_file(self, stage: str, file_name: str) -> Optional[Dict]:
        return self.data["files"].get(self._key(stage, file_name))

    def upsert_arrival(
        self,
        stage: str,
        file_name: str,
        logical_name: str,
        file_ext: str,
        file_size_bytes: int,
        arrival_ts_ist: str,
        last_seen_ts_ist: str,
        large_file: bool,
    ) -> None:
        k = self._key(stage, file_name)
        existing = self.data["files"].get(k)
        if existing:
            existing["file_size_bytes"] = file_size_bytes
            existing["last_seen_ts_ist"] = last_seen_ts_ist
            existing["active"] = True
        else:
            self.data["files"][k] = {
                "stage_name": stage,
                "file_name": file_name,
                "logical_name": logical_name,
                "file_ext": file_ext,
                "file_size_bytes": file_size_bytes,
                "arrival_ts_ist": arrival_ts_ist,
                "last_seen_ts_ist": last_seen_ts_ist,
                "moved_ts_ist": None,
                "stuck_alert_sent": False,
                "large_file": bool(large_file),
                "sla_breach_sent": False,
                "last_large_update_ts_ist": None,
                "active": True,
            }

    def mark_seen(self, stage: str, file_name: str, ts_ist: str) -> None:
        row = self.get_file(stage, file_name)
        if row:
            row["last_seen_ts_ist"] = ts_ist
            row["active"] = True

    def mark_moved(self, stage: str, file_name: str, ts_ist: str) -> None:
        row = self.get_file(stage, file_name)
        if row:
            row["moved_ts_ist"] = ts_ist
            row["active"] = False

    def set_stuck_alert_sent(self, stage: str, file_name: str) -> None:
        row = self.get_file(stage, file_name)
        if row:
            row["stuck_alert_sent"] = True

    def set_sla_breach_sent(self, stage: str, file_name: str) -> None:
        row = self.get_file(stage, file_name)
        if row:
            row["sla_breach_sent"] = True

    def update_large_last_ping(self, stage: str, file_name: str, ts_ist: str) -> None:
        row = self.get_file(stage, file_name)
        if row:
            row["last_large_update_ts_ist"] = ts_ist

    def fetch_active(self, stage: str) -> List[Dict]:
        return [r for r in self.data["files"].values() if r["stage_name"] == stage and r["active"]]

    def count_arrivals_between(self, stage: str, start_iso: str, end_iso: str) -> int:
        return sum(
            1
            for r in self.data["files"].values()
            if r["stage_name"] == stage and start_iso <= r["arrival_ts_ist"] <= end_iso
        )

    def previous_stage_has_moved(self, stage: str, logical_name: str) -> bool:
        return any(
            r["stage_name"] == stage and r["logical_name"] == logical_name and r["moved_ts_ist"] is not None
            for r in self.data["files"].values()
        )

    def get_daily_flag(self, day_key: str, flag: str) -> bool:
        return bool(self.data["daily"].get(day_key, {}).get(flag, False))

    def set_daily_flag(self, day_key: str, flag: str) -> None:
        self.data["daily"].setdefault(day_key, {})
        self.data["daily"][day_key][flag] = True


class MultiStageFileMonitor:
    def __init__(self, cfg: Settings):
        self.cfg = cfg
        self.stage_map = {s.name: s for s in cfg.stages}
        for stage in cfg.stages:
            Path(stage.folder).mkdir(parents=True, exist_ok=True)

        Path(cfg.log_dir).mkdir(parents=True, exist_ok=True)
        self.logger = self._build_logger(Path(cfg.log_dir) / "monitor.log")
        self.event_log_path = Path(cfg.event_log_jsonl)
        self.event_log_path.parent.mkdir(parents=True, exist_ok=True)

        self.store = JsonStateStore(cfg.state_file)

    def _build_logger(self, file_path: Path) -> logging.Logger:
        logger = logging.getLogger("alt_monitor")
        logger.setLevel(logging.INFO)
        if not logger.handlers:
            handler = RotatingFileHandler(file_path, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
            fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
            handler.setFormatter(fmt)
            logger.addHandler(handler)
        return logger

    def _now_ist(self) -> datetime:
        return datetime.now(tz=IST)

    def _iso(self, dt: datetime) -> str:
        return dt.isoformat()

    def _from_iso(self, s: str) -> datetime:
        return datetime.fromisoformat(s)

    def _event(self, level: str, event_type: str, details: str, stage_name: Optional[str] = None, file_name: Optional[str] = None) -> None:
        now = self._now_ist()
        line = {
            "ts_ist": self._iso(now),
            "level": level,
            "event_type": event_type,
            "stage_name": stage_name,
            "file_name": file_name,
            "details": details,
        }
        self.logger.log(getattr(logging, level, logging.INFO), f"{event_type} stage={stage_name} file={file_name} details={details}")
        with self.event_log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")

    def _send_email(self, to_email: str, subject: str, body: str) -> bool:
        msg = EmailMessage()
        msg["From"] = self.cfg.from_email
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.set_content(body)
        try:
            with smtplib.SMTP(self.cfg.smtp_host, self.cfg.smtp_port, timeout=20) as smtp:
                smtp.send_message(msg)
            self._event("INFO", "EMAIL_SENT", f"to={to_email} subject={subject}")
            return True
        except Exception as exc:
            self._event("ERROR", "EMAIL_FAILED", f"to={to_email} subject={subject} error={exc}")
            return False

    def _in_monitor_window(self, ts: datetime) -> bool:
        t = ts.timetz().replace(tzinfo=None)
        return self.cfg.monitor_start_ist <= t <= self.cfg.monitor_end_ist

    def _logical_name(self, file_name: str) -> str:
        return Path(file_name).stem

    def _file_matches_stage(self, stage: StageConfig, name: str) -> bool:
        return name.lower().endswith(stage.extension.lower()) and (date.today().strftime("%Y%m%d") in name)

    def _list_stage_files(self, stage: StageConfig) -> Dict[str, Path]:
        files: Dict[str, Path] = {}
        with os.scandir(stage.folder) as it:
            for entry in it:
                if not entry.is_file():
                    continue
                if self._file_matches_stage(stage, entry.name):
                    files[entry.name] = Path(entry.path)
        return files

    def _get_file_creation_time_ist(self, path: Path) -> datetime:
        st = path.stat()
        ts = st.st_ctime if hasattr(st, "st_ctime") else st.st_mtime
        return datetime.fromtimestamp(ts, tz=IST)

    def _send_arrival_email(self, stage: str, file_name: str, size: int, arrival: datetime) -> None:
        body = (
            f"Hi Team,\n\nFile received in {stage}.\n"
            f"File name: {file_name}\nSize: {size / (1024 * 1024):.2f} MB\n"
            f"Arrival time (IST): {arrival.strftime('%Y-%m-%d %H:%M:%S')}\n\nRegards,\nALT Monitor Bot"
        )
        self._send_email(self.cfg.team1_email, f"[ALT][{stage}] File received - {file_name}", body)

    def _send_moved_email(self, row: Dict) -> None:
        stage = row["stage_name"]
        body = (
            f"Hi Team,\n\nFile moved out of {stage}.\n"
            f"File name: {row['file_name']}\n"
            f"Size: {row['file_size_bytes'] / (1024 * 1024):.2f} MB\n"
            f"Arrival time (IST): {self._from_iso(row['arrival_ts_ist']).strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"Moved time (IST): {self._now_ist().strftime('%Y-%m-%d %H:%M:%S')}\n\nRegards,\nALT Monitor Bot"
        )
        self._send_email(self.cfg.team1_email, f"[ALT][{stage}] File moved - {row['file_name']}", body)

    def _send_stuck_alert(self, row: Dict) -> None:
        stage = row["stage_name"]
        body = (
            f"Hi Team,\n\nFile appears stuck in {stage} for more than {self.cfg.stuck_minutes} minutes.\n"
            f"File name: {row['file_name']}\nSize: {row['file_size_bytes'] / (1024 * 1024):.2f} MB\n"
            f"Arrival time (IST): {self._from_iso(row['arrival_ts_ist']).strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"Status: STUCK\n\nRegards,\nALT Monitor Bot"
        )
        if self._send_email(self.cfg.internal_email, f"[ALT][{stage}] Stuck file alert - {row['file_name']}", body):
            self.store.set_stuck_alert_sent(stage, row["file_name"])

    def _send_sla_breach_alert(self, row: Dict, eta: datetime) -> None:
        stage = row["stage_name"]
        body = (
            f"Hi Team,\n\nPotential SLA breach detected for large file in {stage}.\n"
            f"File name: {row['file_name']}\nSize: {row['file_size_bytes'] / (1024 * 1024):.2f} MB\n"
            f"Arrival time (IST): {self._from_iso(row['arrival_ts_ist']).strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"Expected completion (IST): {eta.strftime('%Y-%m-%d %H:%M:%S')}\n\nRegards,\nALT Monitor Bot"
        )
        if self._send_email(self.cfg.internal_email, f"[ALT][{stage}] SLA breach alert - {row['file_name']}", body):
            self.store.set_sla_breach_sent(stage, row["file_name"])

    def _send_large_progress_update(self, row: Dict, eta: datetime, completed: bool = False) -> None:
        now = self._now_ist()
        stage = row["stage_name"]
        if completed:
            status = f"Large file processing completed at {now.strftime('%Y-%m-%d %H:%M:%S')} IST."
        else:
            remaining = max(0, int((eta - now).total_seconds() // 60))
            status = f"Large file still processing; approx {remaining} minutes remaining to ETA."

        body = (
            f"Hi Team,\n\nStage: {stage}\nFile name: {row['file_name']}\n"
            f"Size: {row['file_size_bytes'] / (1024 * 1024):.2f} MB\n"
            f"Arrival time (IST): {self._from_iso(row['arrival_ts_ist']).strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"ETA (IST): {eta.strftime('%Y-%m-%d %H:%M:%S')}\nStatus: {status}\n\nRegards,\nALT Monitor Bot"
        )
        subject = f"[ALT][{stage}] Large file {'completed' if completed else 'progress'} - {row['file_name']}"
        if self._send_email(self.cfg.internal_email, subject, body):
            self.store.update_large_last_ping(stage, row["file_name"], self._iso(now))

    def _process_stage_arrival(self, stage: StageConfig, name: str, path: Path, now: datetime) -> None:
        row = self.store.get_file(stage.name, name)
        size = path.stat().st_size
        logical = self._logical_name(name)

        if row is not None:
            self.store.mark_seen(stage.name, name, self._iso(now))
            return

        if stage.previous_stage and not self.store.previous_stage_has_moved(stage.previous_stage, logical):
            self._event("INFO", "ARRIVAL_BLOCKED", f"previous stage not completed for logical={logical}", stage.name, name)
            return

        arrival = self._get_file_creation_time_ist(path)
        if stage.name == "STEP1" and not self._in_monitor_window(arrival):
            self._event("INFO", "ARRIVAL_IGNORE", "creation time outside STEP1 monitoring window", stage.name, name)
            return

        large = size >= (self.cfg.large_file_mb_threshold * 1024 * 1024)
        self.store.upsert_arrival(
            stage=stage.name,
            file_name=name,
            logical_name=logical,
            file_ext=stage.extension,
            file_size_bytes=size,
            arrival_ts_ist=self._iso(arrival),
            last_seen_ts_ist=self._iso(now),
            large_file=large,
        )
        self._send_arrival_email(stage.name, name, size, arrival)
        self._event("INFO", "ARRIVED", f"size={size}", stage.name, name)

    def _backfill_stage(self, stage: StageConfig) -> None:
        now = self._now_ist()
        for name, path in self._list_stage_files(stage).items():
            if self.store.get_file(stage.name, name):
                continue
            self._process_stage_arrival(stage, name, path, now)

    def _handle_stage_files(self, stage: StageConfig) -> None:
        now = self._now_ist()
        current = self._list_stage_files(stage)

        for name, path in current.items():
            self._process_stage_arrival(stage, name, path, now)

        for row in self.store.fetch_active(stage.name):
            if row["file_name"] not in current:
                self._send_moved_email(row)
                self.store.mark_moved(stage.name, row["file_name"], self._iso(now))
                if row["large_file"]:
                    eta = self._from_iso(row["arrival_ts_ist"]) + timedelta(hours=self.cfg.large_file_expected_hours)
                    self._send_large_progress_update(row, eta, completed=True)
                self._event("INFO", "MOVED", "file moved out", stage.name, row["file_name"])

    def _handle_window_expectation_step1(self) -> None:
        now = self._now_ist()
        if now.timetz().replace(tzinfo=None) < self.cfg.monitor_end_ist:
            return

        day_key = now.date().isoformat()
        flag = "step1_missing_alert_sent"
        if self.store.get_daily_flag(day_key, flag):
            return

        start = datetime.combine(now.date(), self.cfg.monitor_start_ist, tzinfo=IST)
        end = datetime.combine(now.date(), self.cfg.monitor_end_ist, tzinfo=IST)
        count = self.store.count_arrivals_between("STEP1", self._iso(start), self._iso(end))
        if count < self.cfg.expected_file_count:
            body = (
                f"Hi Team,\n\nExpected {self.cfg.expected_file_count} STEP1 files between "
                f"{self.cfg.monitor_start_ist.strftime('%H:%M')} and {self.cfg.monitor_end_ist.strftime('%H:%M')} IST.\n"
                f"Received count: {count}.\nMissing count: {self.cfg.expected_file_count - count}.\n\nRegards,\nALT Monitor Bot"
            )
            if self._send_email(self.cfg.internal_email, "[ALT][STEP1] Missing file alert", body):
                self.store.set_daily_flag(day_key, flag)

    def _handle_stuck_and_sla(self, stage: StageConfig) -> None:
        now = self._now_ist()
        for row in self.store.fetch_active(stage.name):
            arrival = self._from_iso(row["arrival_ts_ist"])
            age = now - arrival

            if row["large_file"]:
                eta = arrival + timedelta(hours=self.cfg.large_file_expected_hours)
                standard_deadline = arrival + timedelta(hours=self.cfg.sla_window_hours)
                if now >= standard_deadline and not row["sla_breach_sent"]:
                    self._send_sla_breach_alert(row, eta)

                last_update = self._from_iso(row["last_large_update_ts_ist"]) if row["last_large_update_ts_ist"] else None
                if last_update is None or now - last_update >= timedelta(minutes=self.cfg.large_file_update_minutes):
                    self._send_large_progress_update(row, eta)
                continue

            if age >= timedelta(minutes=self.cfg.stuck_minutes) and not row["stuck_alert_sent"]:
                self._send_stuck_alert(row)

    def run_forever(self) -> None:
        self._event("INFO", "START", "Starting ALT multi-stage monitor (non-SQL mode)")
        for stage in self.cfg.stages:
            self._backfill_stage(stage)
        self.store.save()

        while True:
            try:
                for stage in self.cfg.stages:
                    self._handle_stage_files(stage)
                    self._handle_stuck_and_sla(stage)
                self._handle_window_expectation_step1()
                self.store.save()
            except Exception as exc:
                self._event("ERROR", "LOOP_EXCEPTION", f"{exc}\n{traceback.format_exc()}")
                try:
                    self.store.save()
                except Exception:
                    pass
            time.sleep(self.cfg.poll_seconds)


def main() -> None:
    monitor = MultiStageFileMonitor(Settings())
    monitor.run_forever()


if __name__ == "__main__":
    main()
