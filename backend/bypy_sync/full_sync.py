#!/usr/bin/env python3

import argparse
import fcntl
import json
import logging
import os
import posixpath
import queue
import re
import signal
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.utils import generate_local_sync_full_notification, send_configured_notification


DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "bypy_sync.json"
DEFAULT_CONFIG_TEMPLATE_PATH = PROJECT_ROOT / "config" / "bypy_sync.example.json"
DEFAULT_LOG_DIR = PROJECT_ROOT / "log" / "bypy_sync"
LIST_FORMAT = "$t|$f|$s|$m"
APP_ROOT_PREFIX = "/apps/bypy"
DATE_DIRECTORY_PATTERNS = (
    (
        "day",
        re.compile(r"(?<!\d)(?P<year>20\d{2})[-_.年](?P<month>0[1-9]|1[0-2])[-_.月](?P<day>0[1-9]|[12]\d|3[01])(?:日)?(?!\d)"),
    ),
    (
        "day",
        re.compile(r"(?<!\d)(?P<year>20\d{2})(?P<month>0[1-9]|1[0-2])(?P<day>0[1-9]|[12]\d|3[01])(?!\d)"),
    ),
    (
        "month",
        re.compile(r"(?<!\d)(?P<year>20\d{2})[-_.年](?P<month>0[1-9]|1[0-2])(?:月)?(?!\d)"),
    ),
    (
        "month",
        re.compile(r"(?<!\d)(?P<year>20\d{2})(?P<month>0[1-9]|1[0-2])(?!\d)"),
    ),
)


class BypySyncError(Exception):
    pass


class BypyCommandError(BypySyncError):
    def __init__(self, command: List[str], returncode: int, stdout: str, stderr: str):
        self.command = command
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        message = f"bypy 命令失败，退出码={returncode}: {' '.join(command)}"
        super().__init__(message)


def ensure_default_config(config_path: Path) -> Path:
    target = Path(config_path)
    if target.exists():
        return target

    is_default_path = target.resolve(strict=False) == DEFAULT_CONFIG_PATH.resolve(strict=False)
    if not is_default_path:
        return target

    if not DEFAULT_CONFIG_TEMPLATE_PATH.exists():
        raise BypySyncError(f"配置文件不存在: {target}，且模板文件不存在: {DEFAULT_CONFIG_TEMPLATE_PATH}")

    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(DEFAULT_CONFIG_TEMPLATE_PATH, target)
    return target


@dataclass
class RuntimeSettings:
    binary: str = "bypy"
    config_dir: Optional[str] = None
    retry_times: int = 3
    retry_delay_seconds: float = 5.0
    retry_backoff: float = 2.0
    network_timeout: int = 300
    command_timeout: int = 0
    command_heartbeat_seconds: int = 15
    min_command_interval_seconds: float = 1.0
    processes: int = 1
    verify_download: bool = False
    log_dir: str = str(DEFAULT_LOG_DIR)
    summary_file: str = str(DEFAULT_LOG_DIR / "last_run.json")
    lock_file: str = str(DEFAULT_LOG_DIR / "bypy_full_sync.lock")
    state_file: str = str(DEFAULT_LOG_DIR / "current_state.json")


@dataclass
class SyncTask:
    name: str
    remote_root: str
    local_root: str
    enabled: bool = True
    directory_filters: List[str] = field(default_factory=list)
    sync_mode: str = "all"
    recent_value: int = 0
    overwrite_policy: str = "if_newer"


@dataclass
class TaskSummary:
    name: str
    remote_root: str
    local_root: str
    listed_dirs: int = 0
    downloaded_dirs: int = 0
    downloaded_files: int = 0
    synced_items: List[Dict[str, str]] = field(default_factory=list)
    skipped_existing_dirs: int = 0
    skipped_existing_files: int = 0
    failed_items: List[Dict[str, str]] = field(default_factory=list)
    status: str = "pending"


class StopRequested(BypySyncError):
    pass


class RateLimiter:
    def __init__(self, min_interval_seconds: float):
        self.min_interval_seconds = max(0.0, float(min_interval_seconds))
        self._lock = threading.Lock()
        self._last_called_at = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait_seconds = self.min_interval_seconds - (now - self._last_called_at)
            if wait_seconds > 0:
                time.sleep(wait_seconds)
            self._last_called_at = time.monotonic()


class BypyRunner:
    def __init__(
        self,
        settings: RuntimeSettings,
        logger: logging.Logger,
        stop_event: threading.Event,
        state_callback: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    ):
        self.settings = settings
        self.logger = logger
        self.stop_event = stop_event
        self.state_callback = state_callback
        self.rate_limiter = RateLimiter(settings.min_command_interval_seconds)
        self._current_process: Optional[subprocess.Popen[str]] = None

    def _check_stop_requested(self) -> None:
        if self.stop_event and self.stop_event.is_set():
            raise StopRequested("收到停止信号，任务终止")

    def _base_command(self) -> List[str]:
        command = [self.settings.binary]
        command.extend(["--retry", str(self.settings.retry_times)])
        command.extend(["--timeout", str(self.settings.network_timeout)])
        command.extend(["--processes", str(max(1, self.settings.processes))])
        if self.settings.verify_download:
            command.append("--verify")
        if self.settings.config_dir:
            command.extend(["--config-dir", self.settings.config_dir])
        return command

    def run(self, args: List[str], allow_retry: bool = True) -> subprocess.CompletedProcess[str]:
        attempts = max(1, self.settings.retry_times if allow_retry else 1)
        delay_seconds = max(0.0, self.settings.retry_delay_seconds)
        last_error: Optional[Exception] = None

        for attempt in range(1, attempts + 1):
            self._check_stop_requested()
            self.rate_limiter.wait()
            command = self._base_command() + args
            self.logger.info("执行命令: %s", " ".join(command))
            if self.state_callback:
                self.state_callback("执行命令", {"command": " ".join(command), "argv": command})
            try:
                completed = self._run_streaming_command(command)
            except subprocess.TimeoutExpired as exc:
                last_error = exc
                self.logger.warning("命令超时，第 %s/%s 次: %s", attempt, attempts, " ".join(command))
            except StopRequested:
                self._terminate_process_if_needed()
                raise
            except Exception as exc:
                last_error = exc
                self.logger.warning("命令异常，第 %s/%s 次: %s", attempt, attempts, exc)
            else:
                combined_output = "\n".join(part for part in (completed.stdout.strip(), completed.stderr.strip()) if part)

                if completed.returncode == 0 and not output_contains_error(combined_output):
                    return completed

                last_error = BypyCommandError(command, completed.returncode, completed.stdout, completed.stderr)
                self.logger.warning(
                    "命令失败，第 %s/%s 次，退出码=%s",
                    attempt,
                    attempts,
                    completed.returncode,
                )

            if attempt < attempts:
                self.logger.info("%s 秒后重试", delay_seconds)
                if self.state_callback:
                    self.state_callback(
                        "命令重试等待",
                        {"delay_seconds": delay_seconds, "attempt": attempt, "attempts": attempts},
                    )
                slept = 0.0
                while slept < delay_seconds:
                    self._check_stop_requested()
                    step = min(0.5, delay_seconds - slept)
                    time.sleep(step)
                    slept += step
                delay_seconds *= max(1.0, self.settings.retry_backoff)

        if isinstance(last_error, BypyCommandError):
            raise last_error
        if last_error:
            raise BypySyncError(str(last_error))
        raise BypySyncError("未知 bypy 执行错误")

    def _run_streaming_command(self, command: List[str]) -> subprocess.CompletedProcess[str]:
        stdout_lines: List[str] = []
        stderr_lines: List[str] = []
        message_queue: "queue.Queue[tuple[str, str]]" = queue.Queue()
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._current_process = process

        def reader(pipe: Any, source: str) -> None:
            try:
                while True:
                    line = pipe.readline()
                    if line == "":
                        break
                    message_queue.put((source, line.rstrip()))
            finally:
                pipe.close()

        stdout_thread = threading.Thread(target=reader, args=(process.stdout, "stdout"), daemon=True)
        stderr_thread = threading.Thread(target=reader, args=(process.stderr, "stderr"), daemon=True)
        stdout_thread.start()
        stderr_thread.start()

        started_at = time.monotonic()
        last_heartbeat_at = started_at

        try:
            while True:
                self._check_stop_requested()
                drained_output = False

                while True:
                    try:
                        source, line = message_queue.get_nowait()
                    except queue.Empty:
                        break
                    drained_output = True
                    if not line:
                        continue
                    if source == "stdout":
                        stdout_lines.append(line)
                        self.logger.info("命令输出: %s", line)
                    else:
                        stderr_lines.append(line)
                        self.logger.warning("命令错误输出: %s", line)
                    if self.state_callback:
                        self.state_callback("命令输出", {"source": source, "line": line})

                if process.poll() is not None and not drained_output and message_queue.empty():
                    break

                now = time.monotonic()
                if now - last_heartbeat_at >= max(1, int(self.settings.command_heartbeat_seconds)):
                    elapsed = int(now - started_at)
                    self.logger.info("命令执行中，已运行 %s 秒: %s", elapsed, " ".join(command))
                    if self.state_callback:
                        self.state_callback(
                            "命令执行中",
                            {"command": " ".join(command), "elapsed_seconds": elapsed},
                        )
                    last_heartbeat_at = now

                time.sleep(0.2)
        finally:
            stdout_thread.join(timeout=1)
            stderr_thread.join(timeout=1)
            self._current_process = None

        return subprocess.CompletedProcess(
            args=command,
            returncode=process.returncode,
            stdout="\n".join(stdout_lines),
            stderr="\n".join(stderr_lines),
        )

    def _terminate_process_if_needed(self) -> None:
        process = self._current_process
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=5)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

    def list_dir(self, remote_dir: str) -> List[Dict[str, Any]]:
        completed = self.run(["list", remote_dir, LIST_FORMAT])
        entries: List[Dict[str, Any]] = []
        for line in completed.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("/apps/bypy/") or line.startswith("/"):
                continue
            parts = line.split("|", 3)
            if len(parts) != 4:
                self.logger.warning("无法解析目录项，已跳过: %s", line)
                continue
            entry_type, name, size, mtime = parts
            entries.append(
                {
                    "type": entry_type.strip(),
                    "name": name.strip(),
                    "size": size.strip(),
                    "mtime": mtime.strip(),
                }
            )
        return entries

    def sync_dir(self, remote_dir: str, local_dir: Path) -> None:
        local_dir.parent.mkdir(parents=True, exist_ok=True)
        self.run(["syncdown", remote_dir, str(local_dir), "False"])

    def download_file(self, remote_file: str, local_file: Path) -> None:
        local_file.parent.mkdir(parents=True, exist_ok=True)
        self.run(["downfile", remote_file, str(local_file)])


def normalize_remote_path(path: str) -> str:
    if not path:
        return "/"
    normalized = path.strip().replace("\\", "/")
    if normalized == APP_ROOT_PREFIX:
        normalized = "/"
    elif normalized.startswith(APP_ROOT_PREFIX + "/"):
        normalized = normalized[len(APP_ROOT_PREFIX):]
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    normalized = posixpath.normpath(normalized)
    return normalized if normalized != "." else "/"


def output_contains_error(output: str) -> bool:
    if not output:
        return False

    error_markers = (
        "<E>",
        "Error ",
        "error ",
        "Exception:",
        "request failed",
        "No such file or directory",
    )
    return any(marker in output for marker in error_markers)


def join_remote_path(parent: str, name: str) -> str:
    parent = normalize_remote_path(parent)
    if parent == "/":
        return normalize_remote_path(f"/{name}")
    return normalize_remote_path(f"{parent}/{name}")


def sanitize_name(name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in name.strip())
    return cleaned or "task"


def normalize_directory_filters(raw_filters: Any) -> List[str]:
    if raw_filters is None:
        return []

    if isinstance(raw_filters, str):
        raw_filters = [raw_filters]

    normalized: List[str] = []
    for item in raw_filters:
        if item is None:
            continue

        candidate = str(item).replace("\\", "/").strip().strip("/")
        if not candidate:
            continue

        candidate = posixpath.normpath(candidate)
        if candidate in ("", "."):
            continue
        if candidate == ".." or candidate.startswith("../"):
            continue
        if candidate not in normalized:
            normalized.append(candidate)

    return normalized


def normalize_sync_mode(raw_mode: Any, raw_filters: Any = None) -> str:
    allowed_modes = {"all", "manual", "recent_days", "recent_months"}
    candidate = str(raw_mode or "").strip().lower()
    if candidate in allowed_modes:
        return candidate
    if normalize_directory_filters(raw_filters):
        return "manual"
    return "all"


def normalize_recent_value(raw_value: Any, sync_mode: str) -> int:
    if sync_mode not in {"recent_days", "recent_months"}:
        return 0
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        value = 0
    return value if value > 0 else 0


def normalize_overwrite_policy(raw_value: Any) -> str:
    candidate = str(raw_value or "").strip().lower()
    if candidate in {"never", "if_newer", "always"}:
        return candidate
    return "if_newer"


def detect_directory_date(name: str) -> Optional[tuple[str, datetime]]:
    for precision, pattern in DATE_DIRECTORY_PATTERNS:
        match = pattern.search(name)
        if not match:
            continue
        try:
            year = int(match.group("year"))
            month = int(match.group("month"))
            day = int(match.groupdict().get("day") or 1)
            return precision, datetime(year, month, day)
        except ValueError:
            continue
    return None


def month_index(value: datetime) -> int:
    return value.year * 12 + value.month


def directory_matches_recent_window(name: str, sync_mode: str, recent_value: int, now: Optional[datetime] = None) -> bool:
    if sync_mode not in {"recent_days", "recent_months"} or recent_value <= 0:
        return False

    detected = detect_directory_date(name)
    if not detected:
        return False

    precision, detected_date = detected
    current_time = now or datetime.now()
    current_month = month_index(current_time)
    detected_month = month_index(detected_date)

    if sync_mode == "recent_months":
        cutoff_month = current_month - (recent_value - 1)
        return cutoff_month <= detected_month <= current_month

    cutoff_date = (current_time - timedelta(days=recent_value - 1)).date()
    if precision == "day":
        return cutoff_date <= detected_date.date() <= current_time.date()

    cutoff_month = month_index(datetime(cutoff_date.year, cutoff_date.month, 1))
    return cutoff_month <= detected_month <= current_month


def resolve_task_directory_filters(runner: BypyRunner, logger: logging.Logger, task: SyncTask) -> List[str]:
    if task.sync_mode == "manual":
        return task.directory_filters
    if task.sync_mode == "all":
        return []

    entries = runner.list_dir(task.remote_root)
    selected_dirs: List[str] = []
    for entry in entries:
        if entry.get("type") != "D":
            continue
        entry_name = str(entry.get("name") or "").strip()
        if not entry_name:
            continue
        if directory_matches_recent_window(entry_name, task.sync_mode, task.recent_value):
            selected_dirs.append(entry_name)

    normalized_dirs = normalize_directory_filters(selected_dirs)
    if normalized_dirs:
        logger.info(
            "任务 %s 自动识别到 %s 个日期目录: %s",
            task.name,
            len(normalized_dirs),
            ", ".join(normalized_dirs),
        )
    else:
        logger.warning(
            "任务 %s 未识别到符合条件的日期目录: mode=%s, recent_value=%s",
            task.name,
            task.sync_mode,
            task.recent_value,
        )
    return normalized_dirs


def load_config(config_path: Path) -> tuple[RuntimeSettings, List[SyncTask]]:
    config_path = ensure_default_config(config_path)
    if not config_path.exists():
        raise BypySyncError(f"配置文件不存在: {config_path}")

    with config_path.open("r", encoding="utf-8") as fp:
        raw = json.load(fp)

    bypy_config = raw.get("bypy", {})
    settings = RuntimeSettings(
        binary=bypy_config.get("binary", "bypy"),
        config_dir=bypy_config.get("config_dir"),
        retry_times=int(bypy_config.get("retry_times", 3)),
        retry_delay_seconds=float(bypy_config.get("retry_delay_seconds", 5)),
        retry_backoff=float(bypy_config.get("retry_backoff", 2)),
        network_timeout=int(bypy_config.get("network_timeout", 300)),
        command_timeout=int(bypy_config.get("command_timeout", 0)),
        command_heartbeat_seconds=int(bypy_config.get("command_heartbeat_seconds", 15)),
        min_command_interval_seconds=float(bypy_config.get("min_command_interval_seconds", 1.0)),
        processes=int(bypy_config.get("processes", 1)),
        verify_download=bool(bypy_config.get("verify_download", False)),
        log_dir=bypy_config.get("log_dir", str(DEFAULT_LOG_DIR)),
        summary_file=bypy_config.get("summary_file", str(DEFAULT_LOG_DIR / "last_run.json")),
        lock_file=bypy_config.get("lock_file", str(DEFAULT_LOG_DIR / "bypy_full_sync.lock")),
        state_file=bypy_config.get("state_file", str(DEFAULT_LOG_DIR / "current_state.json")),
    )

    tasks: List[SyncTask] = []
    for index, item in enumerate(raw.get("tasks", []), start=1):
        name = str(item.get("name") or f"task-{index}").strip()
        remote_root = normalize_remote_path(str(item.get("remote_root") or "").strip())
        local_root = str(item.get("local_root") or "").strip()
        if not remote_root or not local_root:
            raise BypySyncError(f"第 {index} 个任务缺少 remote_root 或 local_root")
        sync_mode = normalize_sync_mode(item.get("sync_mode"), item.get("directory_filters"))
        tasks.append(
            SyncTask(
                name=name,
                remote_root=remote_root,
                local_root=local_root,
                enabled=bool(item.get("enabled", True)),
                directory_filters=normalize_directory_filters(item.get("directory_filters")) if sync_mode == "manual" else [],
                sync_mode=sync_mode,
                recent_value=normalize_recent_value(item.get("recent_value"), sync_mode),
                overwrite_policy=normalize_overwrite_policy(item.get("overwrite_policy")),
            )
        )

    if not tasks:
        raise BypySyncError("配置文件未定义任何 tasks")

    return settings, tasks


def setup_logger(log_dir: Path) -> tuple[logging.Logger, Path]:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("bypy_full_sync")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False

    log_file = log_dir / f"bypy_full_sync_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    return logger, log_file


def acquire_lock(lock_path: Path) -> Any:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fp = lock_path.open("w", encoding="utf-8")
    try:
        fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        lock_fp.close()
        raise BypySyncError(f"已有同步任务在运行，锁文件: {lock_path}") from exc
    lock_fp.write(str(os.getpid()))
    lock_fp.flush()
    return lock_fp


def append_failure(summary: TaskSummary, remote_path: str, local_path: str, error: Exception) -> None:
    summary.failed_items.append(
        {
            "remote_path": remote_path,
            "local_path": local_path,
            "error": str(error),
        }
    )


class SyncStateTracker:
    def __init__(self, state_path: Path, pid: Optional[int] = None):
        self.state_path = state_path
        self._lock = threading.Lock()
        self._state: Dict[str, Any] = {
            "status": "idle",
            "pid": pid,
            "started_at": None,
            "updated_at": None,
            "finished_at": None,
            "dry_run": False,
            "config": None,
            "log_file": None,
            "task_count": 0,
            "completed_task_count": 0,
            "current_task": None,
            "current_item": None,
            "current_command": None,
            "message": "未运行",
            "last_error": None,
        }

    def _write(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        with temp_path.open("w", encoding="utf-8") as fp:
            json.dump(self._state, fp, ensure_ascii=False, indent=2)
        os.replace(temp_path, self.state_path)

    def update(self, **changes: Any) -> None:
        with self._lock:
            self._state.update(changes)
            self._state["updated_at"] = datetime.now().isoformat(timespec="seconds")
            self._write()

    def start_run(self, *, dry_run: bool, config: str, log_file: Path, task_count: int) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        self.update(
            status="running",
            started_at=now,
            finished_at=None,
            dry_run=dry_run,
            config=config,
            log_file=str(log_file.resolve()),
            task_count=task_count,
            completed_task_count=0,
            current_task=None,
            current_item=None,
            current_command=None,
            message="开始执行同步任务",
            last_error=None,
        )

    def finish_task(self, completed_task_count: int) -> None:
        self.update(completed_task_count=completed_task_count, current_item=None, current_command=None)

    def finish_run(self, status: str, message: str, last_error: Optional[str] = None) -> None:
        self.update(
            status=status,
            finished_at=datetime.now().isoformat(timespec="seconds"),
            current_task=None,
            current_item=None,
            current_command=None,
            message=message,
            last_error=last_error,
        )


def check_stop_requested(stop_event: threading.Event) -> None:
    if stop_event.is_set():
        raise StopRequested("收到停止信号，任务终止")


def create_runner_state_callback(state_tracker: SyncStateTracker) -> Callable[[str, Dict[str, Any]], None]:
    def callback(message: str, details: Dict[str, Any]) -> None:
        current_command = details.copy()
        current_command["message_type"] = message
        state_tracker.update(message=message, current_command=current_command)

    return callback


def sync_missing_items(
    runner: BypyRunner,
    logger: logging.Logger,
    task: SyncTask,
    summary: TaskSummary,
    dry_run: bool,
    state_tracker: SyncStateTracker,
    stop_event: threading.Event,
) -> None:
    root_local_path = Path(task.local_root).expanduser().resolve()
    selected_directory_filters = resolve_task_directory_filters(runner, logger, task)
    state_tracker.update(
        current_task={
            "name": task.name,
            "remote_root": task.remote_root,
            "local_root": str(root_local_path),
            "status": "running",
            "sync_mode": task.sync_mode,
            "recent_value": task.recent_value,
            "directory_filters": selected_directory_filters,
        },
        current_item=None,
        message=f"正在执行任务: {task.name}",
    )

    if not root_local_path.exists() and not selected_directory_filters:
        logger.info("本地根目录不存在，整目录拉取: %s -> %s", task.remote_root, root_local_path)
        state_tracker.update(
            current_item={
                "type": "directory",
                "action": "sync_dir",
                "remote_path": task.remote_root,
                "local_path": str(root_local_path),
            },
            message=f"整目录拉取: {task.remote_root}",
        )
        if not dry_run:
            check_stop_requested(stop_event)
            runner.sync_dir(task.remote_root, root_local_path)
        summary.downloaded_dirs += 1
        summary.synced_items.append({"path": str(root_local_path), "kind": "directory"})
        return

    def walk(remote_dir: str, local_dir: Path) -> None:
        check_stop_requested(stop_event)
        summary.listed_dirs += 1
        state_tracker.update(
            current_item={
                "type": "directory",
                "action": "list",
                "remote_path": remote_dir,
                "local_path": str(local_dir),
            },
            message=f"扫描目录: {remote_dir}",
        )
        entries = runner.list_dir(remote_dir)
        for entry in entries:
            check_stop_requested(stop_event)
            entry_name = entry["name"]
            remote_path = join_remote_path(remote_dir, entry_name)
            local_path = local_dir / entry_name
            entry_type = entry["type"]

            try:
                if entry_type == "D":
                    if local_path.exists() and not local_path.is_dir():
                        raise BypySyncError("本地存在同名文件，无法映射远程目录")

                    if not local_path.exists():
                        logger.info("发现缺失目录，准备拉取: %s -> %s", remote_path, local_path)
                        state_tracker.update(
                            current_item={
                                "type": "directory",
                                "action": "sync_dir",
                                "remote_path": remote_path,
                                "local_path": str(local_path),
                            },
                            message=f"拉取缺失目录: {remote_path}",
                        )
                        if not dry_run:
                            runner.sync_dir(remote_path, local_path)
                        summary.downloaded_dirs += 1
                        summary.synced_items.append({"path": str(local_path), "kind": "directory"})
                        continue

                    summary.skipped_existing_dirs += 1
                    walk(remote_path, local_path)
                    continue

                if entry_type == "F":
                    if local_path.exists() and local_path.is_dir():
                        raise BypySyncError("本地存在同名目录，无法映射远程文件")

                    if not local_path.exists():
                        logger.info("发现缺失文件，准备拉取: %s -> %s", remote_path, local_path)
                        state_tracker.update(
                            current_item={
                                "type": "file",
                                "action": "download_file",
                                "remote_path": remote_path,
                                "local_path": str(local_path),
                            },
                            message=f"拉取缺失文件: {remote_path}",
                        )
                        if not dry_run:
                            runner.download_file(remote_path, local_path)
                        summary.downloaded_files += 1
                        summary.synced_items.append({"path": str(local_path), "kind": "file"})
                        continue

                    summary.skipped_existing_files += 1
                    continue

                logger.warning("未知目录项类型，已跳过: %s", entry)
            except Exception as exc:
                logger.error("处理失败: remote=%s local=%s error=%s", remote_path, local_path, exc)
                append_failure(summary, remote_path, str(local_path), exc)

    if selected_directory_filters:
        for relative_dir in selected_directory_filters:
            remote_target_path = join_remote_path(task.remote_root, relative_dir)
            local_target_path = root_local_path / Path(relative_dir)
            logger.info("按目录过滤执行: %s -> %s", remote_target_path, local_target_path)
            state_tracker.update(
                current_item={
                    "type": "directory-filter",
                    "action": "sync_selected_dir",
                    "remote_path": remote_target_path,
                    "local_path": str(local_target_path),
                },
                message=f"按目录执行: {relative_dir}",
            )

            if not local_target_path.exists():
                if not dry_run:
                    runner.sync_dir(remote_target_path, local_target_path)
                summary.downloaded_dirs += 1
                continue

            walk(remote_target_path, local_target_path)
        return

    walk(task.remote_root, root_local_path)


def build_summary_payload(
    started_at: datetime,
    finished_at: datetime,
    args: argparse.Namespace,
    log_file: Path,
    task_summaries: List[TaskSummary],
) -> Dict[str, Any]:
    return {
        "started_at": started_at.isoformat(timespec="seconds"),
        "finished_at": finished_at.isoformat(timespec="seconds"),
        "duration_seconds": round((finished_at - started_at).total_seconds(), 2),
        "dry_run": args.dry_run,
        "config": str(Path(args.config).resolve()),
        "log_file": str(log_file.resolve()),
        "tasks": [
            {
                "name": item.name,
                "remote_root": item.remote_root,
                "local_root": item.local_root,
                "status": item.status,
                "listed_dirs": item.listed_dirs,
                "downloaded_dirs": item.downloaded_dirs,
                "downloaded_files": item.downloaded_files,
                "synced_items": item.synced_items,
                "skipped_existing_dirs": item.skipped_existing_dirs,
                "skipped_existing_files": item.skipped_existing_files,
                "failed_items": item.failed_items,
            }
            for item in task_summaries
        ],
    }


def write_summary_files(settings: RuntimeSettings, payload: Dict[str, Any], started_at: datetime) -> None:
    summary_path = Path(settings.summary_file)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    history_path = summary_path.parent / f"bypy_full_sync_{started_at.strftime('%Y%m%d_%H%M%S')}.json"

    for target in (summary_path, history_path):
        with target.open("w", encoding="utf-8") as fp:
            json.dump(payload, fp, ensure_ascii=False, indent=2)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="使用 bypy 递归检查远程目录，只补拉本地缺失目录和文件")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="配置文件路径")
    parser.add_argument("--task", action="append", default=[], help="只执行指定任务名称，可重复传入")
    parser.add_argument("--dry-run", action="store_true", help="只输出计划，不实际下载")
    return parser.parse_args()


def run_full_sync(
    *,
    config_path: str = str(DEFAULT_CONFIG_PATH),
    task_filters: Optional[List[str]] = None,
    dry_run: bool = False,
    stop_event: Optional[threading.Event] = None,
    install_signal_handlers: bool = False,
    state_pid: Optional[int] = None,
) -> int:
    started_at = datetime.now()
    runtime_stop_event = stop_event or threading.Event()

    def handle_stop_signal(signum: int, _frame: Any) -> None:
        runtime_stop_event.set()

    if install_signal_handlers:
        signal.signal(signal.SIGTERM, handle_stop_signal)
        signal.signal(signal.SIGINT, handle_stop_signal)

    try:
        settings, tasks = load_config(Path(config_path))
    except Exception as exc:
        print(f"加载配置失败: {exc}", file=sys.stderr)
        return 1

    logger, log_file = setup_logger(Path(settings.log_dir))
    state_tracker = SyncStateTracker(Path(settings.state_file), pid=state_pid)

    try:
        lock_fp = acquire_lock(Path(settings.lock_file))
    except Exception as exc:
        logger.error("获取运行锁失败: %s", exc)
        state_tracker.finish_run("failed", "获取运行锁失败", str(exc))
        return 1

    task_filter = {str(name).strip() for name in (task_filters or []) if str(name).strip()}
    selected_tasks = [task for task in tasks if task.enabled and (not task_filter or task.name in task_filter)]

    if not selected_tasks:
        logger.error("没有可执行任务，请检查 enabled 或 --task 参数")
        lock_fp.close()
        state_tracker.finish_run("failed", "没有可执行任务")
        return 1

    logger.info("本次共执行 %s 个任务，dry_run=%s", len(selected_tasks), dry_run)
    state_tracker.start_run(
        dry_run=dry_run,
        config=str(Path(config_path).resolve()),
        log_file=log_file,
        task_count=len(selected_tasks),
    )
    runner = BypyRunner(
        settings,
        logger,
        runtime_stop_event,
        state_callback=create_runner_state_callback(state_tracker),
    )
    task_summaries: List[TaskSummary] = []

    exit_code = 0
    try:
        for index, task in enumerate(selected_tasks, start=1):
            check_stop_requested(runtime_stop_event)
            summary = TaskSummary(name=task.name, remote_root=task.remote_root, local_root=task.local_root)
            task_summaries.append(summary)
            logger.info("开始任务: %s | remote=%s | local=%s", task.name, task.remote_root, task.local_root)
            try:
                sync_missing_items(runner, logger, task, summary, dry_run, state_tracker, runtime_stop_event)
            except StopRequested as exc:
                summary.status = "stopped"
                append_failure(summary, task.remote_root, task.local_root, exc)
                logger.warning("任务被停止: %s", task.name)
                exit_code = 1
                state_tracker.finish_task(index - 1)
                break
            except Exception as exc:
                summary.status = "failed"
                append_failure(summary, task.remote_root, task.local_root, exc)
                logger.exception("任务失败: %s", task.name)
                exit_code = 1
            else:
                summary.status = "success" if not summary.failed_items else "partial-success"
            state_tracker.finish_task(index)
            logger.info(
                "任务结束: %s | status=%s | listed_dirs=%s | downloaded_dirs=%s | downloaded_files=%s | failures=%s",
                task.name,
                summary.status,
                summary.listed_dirs,
                summary.downloaded_dirs,
                summary.downloaded_files,
                len(summary.failed_items),
            )
            if summary.failed_items:
                exit_code = 1
    finally:
        finished_at = datetime.now()
        payload = build_summary_payload(
            started_at,
            finished_at,
            argparse.Namespace(config=config_path, dry_run=dry_run),
            log_file,
            task_summaries,
        )
        write_summary_files(settings, payload, started_at)
        final_status = "failed"
        final_message = "同步任务执行完成，但存在失败项"
        if runtime_stop_event.is_set():
            final_status = "stopped"
            final_message = "同步任务已停止"
            state_tracker.finish_run(final_status, final_message, "收到停止信号")
        elif exit_code == 0:
            final_status = "success"
            final_message = "全部同步任务执行完成"
            state_tracker.finish_run(final_status, final_message)
        else:
            state_tracker.finish_run(final_status, final_message)

        if not dry_run:
            notification_content = generate_local_sync_full_notification(final_status, final_message, payload)
            if not send_configured_notification("百度网盘本地同步", notification_content):
                logger.debug("本地同步通知未发送，可能未启用通知配置")
        try:
            lock_fp.close()
        except Exception:
            pass

    logger.info("全部任务完成，退出码=%s", exit_code)
    logger.info("日志文件: %s", log_file)
    logger.info("执行汇总: %s", Path(settings.summary_file).resolve())
    return exit_code


def main() -> int:
    args = parse_args()
    return run_full_sync(
        config_path=args.config,
        task_filters=args.task,
        dry_run=args.dry_run,
        install_signal_handlers=True,
        state_pid=os.getpid(),
    )


if __name__ == "__main__":
    sys.exit(main())