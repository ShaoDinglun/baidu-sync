#!/usr/bin/env python3

import argparse
import fcntl
import logging
import os
import posixpath
import sys
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Deque, Dict, Iterable, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.utils import generate_local_sync_incremental_notification, send_configured_notification
from backend.bypy_sync.full_sync import (
    APP_ROOT_PREFIX,
    DEFAULT_CONFIG_PATH,
    BypyRunner,
    BypySyncError,
    StopRequested,
    acquire_lock,
    create_runner_state_callback,
    detect_directory_date,
    directory_matches_recent_window,
    join_remote_path,
    load_config,
    normalize_directory_filters,
    normalize_remote_path,
)


def is_month_dir_name(name: str) -> bool:
    return bool(name and len(name) == 7 and name[4] == "-" and name[:4].isdigit() and name[5:7].isdigit())


def normalize_task_filters(task_filters: Optional[Iterable[str]]) -> List[str]:
    if task_filters is None:
        return []
    normalized: List[str] = []
    for item in task_filters:
        candidate = str(item or "").strip()
        if candidate:
            normalized.append(candidate)
    return normalized


def remote_mtime_to_epoch(remote_mtime: str) -> Optional[int]:
    value = str(remote_mtime or "").strip()
    if not value:
        return None
    if value.isdigit() and len(value) == 10:
        return int(value)

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(value, fmt).timestamp())
        except ValueError:
            continue
    return None


def setup_incremental_logger(log_dir: Path) -> tuple[logging.Logger, Path]:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("bypy_incremental_sync")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False

    log_file = log_dir / f"bypy_incremental_sync_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    return logger, log_file


def determine_incremental_paths(log_dir: Path) -> Dict[str, Path]:
    return {
        "log_dir": log_dir,
        "lock_file": log_dir / "bypy_incremental_sync.lock",
        "state_file": log_dir / "bypy_incremental_state.json",
        "summary_file": log_dir / "bypy_incremental_last_run.txt",
    }


class IncrementalStateTracker:
    def __init__(self, state_path: Path):
        self.state_path = state_path
        self._state: Dict[str, Any] = {
            "sync_type": "incremental",
            "status": "idle",
            "pid": None,
            "started_at": None,
            "finished_at": None,
            "updated_at": None,
            "message": "未运行",
            "dry_run": False,
            "tasks": [],
            "config": None,
            "log_file": None,
            "trigger_source": "manual",
            "current_task": None,
            "current_item": None,
            "current_command": None,
            "last_error": None,
            "summary_text": "",
        }

    def _write(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        with temp_path.open("w", encoding="utf-8") as fp:
            import json

            json.dump(self._state, fp, ensure_ascii=False, indent=2)
        os.replace(temp_path, self.state_path)

    def update(self, **changes: Any) -> None:
        self._state.update(changes)
        self._state["updated_at"] = datetime.now().isoformat(timespec="seconds")
        self._write()

    def start_run(
        self,
        *,
        dry_run: bool,
        config: str,
        log_file: Path,
        tasks: List[str],
        trigger_source: str,
    ) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        self.update(
            status="running",
            pid=None,
            started_at=now,
            finished_at=None,
            message="开始执行本地同步任务",
            dry_run=dry_run,
            tasks=tasks,
            config=config,
            log_file=str(log_file.resolve()),
            trigger_source=trigger_source,
            current_task=None,
            current_item=None,
            current_command=None,
            last_error=None,
            summary_text="",
        )

    def finish_run(self, status: str, message: str, *, last_error: Optional[str] = None, summary_text: str = "") -> None:
        self.update(
            status=status,
            pid=None,
            finished_at=datetime.now().isoformat(timespec="seconds"),
            current_task=None,
            current_item=None,
            current_command=None,
            message=message,
            last_error=last_error,
            summary_text=summary_text,
        )


@dataclass
class IncrementalTaskSummary:
    name: str
    remote_root: str
    local_root: str
    listed_dirs: int = 0
    downloaded_dirs: int = 0
    downloaded_files: int = 0
    updated_files: int = 0
    skipped_files: int = 0
    local_only_dirs: int = 0
    local_only_files: int = 0
    failures: int = 0
    status: str = "pending"


def resolve_recent_directory_filters(runner: BypyRunner, task: Any) -> List[str]:
    resolution = resolve_recent_directory_filter_result(runner, task)
    return resolution["filters"]


def _join_relative_directory(parent: str, name: str) -> str:
    return posixpath.join(parent, name) if parent else name


def resolve_recent_directory_filter_result(runner: BypyRunner, task: Any) -> Dict[str, Any]:
    queue: Deque[Tuple[str, str]] = deque([("", task.remote_root)])
    selected: List[str] = []
    seen_remote_paths = set()
    date_hint_found = False

    while queue:
        relative_root, remote_root = queue.popleft()
        normalized_remote_root = normalize_remote_path(remote_root)
        if normalized_remote_root in seen_remote_paths:
            continue
        seen_remote_paths.add(normalized_remote_root)

        entries = runner.list_dir(normalized_remote_root)
        for entry in entries:
            if entry.get("type") != "D":
                continue

            name = str(entry.get("name") or "").strip()
            if not name:
                continue

            relative_path = _join_relative_directory(relative_root, name)
            remote_path = join_remote_path(normalized_remote_root, name)
            detected = detect_directory_date(name)

            if not detected:
                queue.append((relative_path, remote_path))
                continue

            date_hint_found = True
            precision, _detected_date = detected

            if task.sync_mode == "recent_days" and precision == "month":
                if directory_matches_recent_window(name, task.sync_mode, task.recent_value):
                    queue.append((relative_path, remote_path))
                continue

            if directory_matches_recent_window(name, task.sync_mode, task.recent_value):
                selected.append(relative_path)

    return {
        "filters": normalize_directory_filters(selected),
        "date_hint_found": date_hint_found,
    }


class IncrementalSyncExecutor:
    def __init__(self, runner: BypyRunner, logger: logging.Logger, dry_run: bool, show_local_only: bool, stop_event: Any):
        self.runner = runner
        self.logger = logger
        self.dry_run = dry_run
        self.show_local_only = show_local_only
        self.stop_event = stop_event
        self.target_months = self._init_target_months()

    def _check_stop_requested(self) -> None:
        if self.stop_event and self.stop_event.is_set():
            raise StopRequested("收到停止信号，任务终止")

    def _init_target_months(self) -> List[str]:
        current = datetime.now().strftime("%Y-%m")
        previous = (datetime.now().replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
        return [current] if previous == current else [current, previous]

    def _compare_file_action(self, local_path: Path, remote_size: str, remote_mtime: str, overwrite_policy: str) -> str:
        if not local_path.exists():
            return "download"
        if local_path.is_dir():
            return "conflict-dir"

        if overwrite_policy == "never":
            return "skip"

        if overwrite_policy == "always":
            return "update"

        try:
            local_size = local_path.stat().st_size
        except OSError:
            return "download"

        if str(remote_size or "").isdigit() and local_size != int(remote_size):
            return "update"

        remote_epoch = remote_mtime_to_epoch(remote_mtime)
        if remote_epoch is not None:
            try:
                local_epoch = int(local_path.stat().st_mtime)
            except OSError:
                return "download"
            if remote_epoch > local_epoch:
                return "update"

        return "skip"

    def _scan_local_only_items(self, local_dir: Path, remote_names: List[str], summary: IncrementalTaskSummary) -> None:
        if not self.show_local_only or not local_dir.is_dir():
            return

        remote_name_set = set(remote_names)
        for child in local_dir.iterdir():
            if child.name in remote_name_set:
                continue
            if child.is_dir():
                summary.local_only_dirs += 1
                self.logger.info("本地多出目录，仅记录不删除: %s", child)
            else:
                summary.local_only_files += 1
                self.logger.info("本地多出文件，仅记录不删除: %s", child)

    def _sync_directory_via_syncdown(self, remote_dir: str, local_dir: Path, action_label: str, summary: IncrementalTaskSummary) -> bool:
        self.logger.info("%s: %s -> %s", action_label, remote_dir, local_dir)
        if not self.dry_run:
            local_dir.parent.mkdir(parents=True, exist_ok=True)
            self.runner.sync_dir(remote_dir, local_dir)
        summary.downloaded_dirs += 1
        return True

    def _download_remote_file(self, remote_file: str, local_file: Path, action_label: str) -> bool:
        self.logger.info("%s: %s -> %s", action_label, remote_file, local_file)
        if not self.dry_run:
            local_file.parent.mkdir(parents=True, exist_ok=True)
            self.runner.download_file(remote_file, local_file)
        return True

    def _list_output_has_month_dirs(self, entries: List[Dict[str, Any]]) -> bool:
        return any(entry.get("type") == "D" and is_month_dir_name(str(entry.get("name") or "")) for entry in entries)

    def _directory_has_month_children(self, local_dir: Path) -> bool:
        if not local_dir.is_dir():
            return False
        try:
            return any(child.is_dir() and is_month_dir_name(child.name) for child in local_dir.iterdir())
        except OSError:
            return False

    def _sync_remote_file(self, remote_file: str, local_file: Path, remote_size: str, remote_mtime: str, summary: IncrementalTaskSummary, overwrite_policy: str) -> None:
        action = self._compare_file_action(local_file, remote_size, remote_mtime, overwrite_policy)
        try:
            if action == "download":
                self._download_remote_file(remote_file, local_file, "拉取缺失文件")
                summary.downloaded_files += 1
            elif action == "update":
                self._download_remote_file(remote_file, local_file, "更新文件")
                summary.updated_files += 1
            elif action == "conflict-dir":
                summary.failures += 1
                self.logger.error("本地存在同名目录，无法映射远程文件: %s", local_file)
            else:
                summary.skipped_files += 1
        except Exception:
            summary.failures += 1
            self.logger.error("%s失败: %s", "文件更新" if action == "update" else "文件拉取", remote_file)

    def _sync_month_container(self, remote_dir: str, local_dir: Path, entries: List[Dict[str, Any]], summary: IncrementalTaskSummary, overwrite_policy: str) -> None:
        self.logger.info("按近两个月检查目录: %s | months=%s", remote_dir, " ".join(self.target_months))
        remote_names = [str(entry.get("name") or "") for entry in entries if str(entry.get("name") or "")]
        remote_months = {name for name in remote_names if is_month_dir_name(name)}

        for target_month in self.target_months:
            self._check_stop_requested()
            remote_path = join_remote_path(remote_dir, target_month)
            local_path = local_dir / target_month

            if target_month not in remote_months:
                if self.show_local_only and local_path.is_dir():
                    summary.local_only_dirs += 1
                    self.logger.info("本地存在远端缺失的月份目录，仅记录不删除: %s", local_path)
                continue

            if not local_path.is_dir():
                try:
                    self._sync_directory_via_syncdown(remote_path, local_path, "拉取月份目录", summary)
                except Exception:
                    summary.failures += 1
                    self.logger.error("月份目录拉取失败: %s", remote_path)
                continue

            self._sync_remote_dir(remote_path, local_path, summary, overwrite_policy)

        self._scan_local_only_items(local_dir, remote_names, summary)

    def _sync_top_level_directory(self, remote_dir: str, local_dir: Path, summary: IncrementalTaskSummary, overwrite_policy: str) -> None:
        self._check_stop_requested()
        if not local_dir.is_dir():
            try:
                self._sync_directory_via_syncdown(remote_dir, local_dir, "拉取缺失一级目录", summary)
            except Exception:
                summary.failures += 1
                self.logger.error("一级目录拉取失败: %s", remote_dir)
            return

        summary.listed_dirs += 1
        self.logger.info("扫描一级目录: %s", remote_dir)
        try:
            entries = self.runner.list_dir(remote_dir)
        except Exception:
            summary.failures += 1
            self.logger.error("一级目录扫描失败: %s", remote_dir)
            return

        if self._list_output_has_month_dirs(entries) or self._directory_has_month_children(local_dir):
            self._sync_month_container(remote_dir, local_dir, entries, summary, overwrite_policy)
            return

        self.logger.info("按递归比对方式更新一级目录: %s", remote_dir)
        self._sync_remote_dir(remote_dir, local_dir, summary, overwrite_policy)

    def _sync_remote_dir(self, remote_dir: str, local_dir: Path, summary: IncrementalTaskSummary, overwrite_policy: str) -> None:
        self._check_stop_requested()
        remote_dir = normalize_remote_path(remote_dir)
        if not local_dir.is_dir():
            try:
                self._sync_directory_via_syncdown(remote_dir, local_dir, "拉取缺失目录", summary)
            except Exception:
                summary.failures += 1
                self.logger.error("目录拉取失败: %s", remote_dir)
            return

        summary.listed_dirs += 1
        self.logger.info("扫描目录: %s", remote_dir)
        try:
            entries = self.runner.list_dir(remote_dir)
        except Exception:
            summary.failures += 1
            self.logger.error("目录扫描失败: %s", remote_dir)
            return

        remote_names: List[str] = []
        for entry in entries:
            self._check_stop_requested()
            entry_type = str(entry.get("type") or "")
            entry_name = str(entry.get("name") or "").strip()
            if not entry_name:
                continue
            remote_names.append(entry_name)
            remote_path = join_remote_path(remote_dir, entry_name)
            local_path = local_dir / entry_name

            if entry_type == "D":
                if local_path.exists() and not local_path.is_dir():
                    summary.failures += 1
                    self.logger.error("本地存在同名文件，无法映射远程目录: %s", local_path)
                    continue
                if not local_path.is_dir():
                    try:
                        self._sync_directory_via_syncdown(remote_path, local_path, "拉取缺失目录", summary)
                    except Exception:
                        summary.failures += 1
                        self.logger.error("目录拉取失败: %s", remote_path)
                    continue
                self._sync_remote_dir(remote_path, local_path, summary, overwrite_policy)
                continue

            if entry_type == "F":
                self._sync_remote_file(remote_path, local_path, str(entry.get("size") or ""), str(entry.get("mtime") or ""), summary, overwrite_policy)
                continue

            self.logger.warning("未知目录项类型，已跳过: %s", entry)

        self._scan_local_only_items(local_dir, remote_names, summary)

    def sync_task_root(self, task: Any, directory_filters: List[str], summary: IncrementalTaskSummary, overwrite_policy: str) -> None:
        root_local_path = Path(task.local_root).expanduser().resolve()
        if directory_filters:
            for selected_dir in directory_filters:
                self._check_stop_requested()
                remote_path = join_remote_path(task.remote_root, selected_dir)
                local_path = root_local_path / Path(selected_dir)
                self.logger.info(
                    "按目录过滤执行: %s | remote=%s | local=%s",
                    selected_dir,
                    remote_path,
                    local_path,
                )
                nested_task = type("TaskContext", (), {"remote_root": remote_path, "local_root": str(local_path)})
                self.sync_task_root(nested_task, [], summary, overwrite_policy)
            return

        if not root_local_path.is_dir():
            try:
                self._sync_directory_via_syncdown(task.remote_root, root_local_path, "拉取缺失任务根目录", summary)
            except Exception:
                summary.failures += 1
                self.logger.error("任务根目录拉取失败: %s", task.remote_root)
            return

        summary.listed_dirs += 1
        self.logger.info("扫描任务根目录: %s", task.remote_root)
        try:
            entries = self.runner.list_dir(task.remote_root)
        except Exception:
            summary.failures += 1
            self.logger.error("任务根目录扫描失败: %s", task.remote_root)
            return

        if self._list_output_has_month_dirs(entries) or self._directory_has_month_children(root_local_path):
            self._sync_month_container(task.remote_root, root_local_path, entries, summary, overwrite_policy)
            return

        remote_names: List[str] = []
        for entry in entries:
            self._check_stop_requested()
            entry_type = str(entry.get("type") or "")
            entry_name = str(entry.get("name") or "").strip()
            if not entry_name:
                continue

            remote_names.append(entry_name)
            remote_path = join_remote_path(task.remote_root, entry_name)
            local_path = root_local_path / entry_name

            if entry_type == "D":
                self._sync_top_level_directory(remote_path, local_path, summary, overwrite_policy)
            elif entry_type == "F":
                self._sync_remote_file(remote_path, local_path, str(entry.get("size") or ""), str(entry.get("mtime") or ""), summary, overwrite_policy)
            else:
                self.logger.warning("未知根目录项类型，已跳过: %s", entry)

        self._scan_local_only_items(root_local_path, remote_names, summary)


def build_summary_text(config_path: str, log_file: Path, task_summaries: List[IncrementalTaskSummary], finished_at: datetime) -> str:
    total_tasks = len(task_summaries)
    listed_dirs = sum(item.listed_dirs for item in task_summaries)
    downloaded_dirs = sum(item.downloaded_dirs for item in task_summaries)
    downloaded_files = sum(item.downloaded_files for item in task_summaries)
    updated_files = sum(item.updated_files for item in task_summaries)
    skipped_files = sum(item.skipped_files for item in task_summaries)
    local_only_dirs = sum(item.local_only_dirs for item in task_summaries)
    local_only_files = sum(item.local_only_files for item in task_summaries)
    failures = sum(item.failures for item in task_summaries)

    return "\n".join(
        [
            f"执行时间: {finished_at.strftime('%Y-%m-%d %H:%M:%S')}",
            f"配置文件: {config_path}",
            f"日志文件: {log_file}",
            f"任务数: {total_tasks}",
            f"扫描目录数: {listed_dirs}",
            f"拉取目录数: {downloaded_dirs}",
            f"新增文件数: {downloaded_files}",
            f"更新文件数: {updated_files}",
            f"跳过文件数: {skipped_files}",
            f"本地多出目录数: {local_only_dirs}",
            f"本地多出文件数: {local_only_files}",
            f"失败数: {failures}",
        ]
    )


def write_summary(summary_path: Path, summary_text: str) -> None:
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(summary_text + "\n", encoding="utf-8")


def send_incremental_notification(
    *,
    status: str,
    message: str,
    dry_run: bool,
    log_file: Path,
    summary_text: str,
    task_names: List[str],
) -> None:
    if dry_run:
        return
    content = generate_local_sync_incremental_notification(
        status=status,
        message=message,
        task_names=task_names,
        dry_run=dry_run,
        log_file=str(log_file),
        summary_text=summary_text,
    )
    send_configured_notification("百度网盘本地同步", content)


def select_incremental_tasks(tasks: List[Any], task_filters: List[str], include_disabled: bool) -> List[Any]:
    task_filter_set = set(task_filters)
    selected: List[Any] = []
    for task in tasks:
        if task_filter_set and task.name not in task_filter_set:
            continue
        if not task.enabled and not (include_disabled and task_filter_set and task.name in task_filter_set):
            continue
        selected.append(task)
    return selected


def run_incremental_sync(
    *,
    config_path: str = str(DEFAULT_CONFIG_PATH),
    task_filters: Optional[Iterable[str]] = None,
    dry_run: bool = False,
    include_disabled: bool = False,
    show_local_only: bool = True,
    trigger_source: str = "manual",
    stop_event: Any = None,
) -> int:
    normalized_task_filters = normalize_task_filters(task_filters)
    started_at = datetime.now()

    settings, tasks = load_config(Path(config_path))
    paths = determine_incremental_paths(Path(settings.log_dir))
    logger, log_file = setup_incremental_logger(paths["log_dir"])
    state_tracker = IncrementalStateTracker(paths["state_file"])

    try:
        lock_fp = acquire_lock(paths["lock_file"])
    except Exception as exc:
        logger.error("获取运行锁失败: %s", exc)
        state_tracker.finish_run("failed", "获取运行锁失败", last_error=str(exc))
        return 1

    selected_tasks = select_incremental_tasks(tasks, normalized_task_filters, include_disabled)
    if not selected_tasks:
        logger.error("没有可执行任务，请检查 enabled 或 --task 参数")
        lock_fp.close()
        state_tracker.finish_run("failed", "没有可执行任务")
        return 1

    task_names = [task.name for task in selected_tasks]
    state_tracker.start_run(
        dry_run=dry_run,
        config=str(Path(config_path).resolve()),
        log_file=log_file,
        tasks=task_names,
        trigger_source=trigger_source,
    )
    logger.info("增量同步开始，配置文件: %s", Path(config_path).resolve())
    logger.info("dry_run=%s, show_local_only=%s, target_months=%s", dry_run, show_local_only, " ".join(IncrementalSyncExecutor(None, logger, dry_run, show_local_only, stop_event)._init_target_months()))

    runner = BypyRunner(settings, logger, stop_event, state_callback=create_runner_state_callback(state_tracker))
    executor = IncrementalSyncExecutor(runner, logger, dry_run, show_local_only, stop_event)
    task_summaries: List[IncrementalTaskSummary] = []
    final_status = "success"
    final_message = "全部同步任务执行完成"
    last_error: Optional[str] = None

    try:
        for task in selected_tasks:
            if stop_event and stop_event.is_set():
                raise StopRequested("收到停止信号，任务终止")

            directory_filters = normalize_directory_filters(getattr(task, "directory_filters", []))
            recent_resolution = None
            if task.sync_mode in {"recent_days", "recent_months"}:
                recent_resolution = resolve_recent_directory_filter_result(runner, task)
                directory_filters = recent_resolution["filters"]
                if not directory_filters:
                    if recent_resolution["date_hint_found"]:
                        logger.warning(
                            "未识别到符合条件的日期目录，跳过任务: %s | mode=%s | recent_value=%s",
                            task.name,
                            task.sync_mode,
                            task.recent_value,
                        )
                        continue
                    logger.warning(
                        "未识别到任何日期目录，回退为整个目录同步: %s | mode=%s | recent_value=%s",
                        task.name,
                        task.sync_mode,
                        task.recent_value,
                    )

            summary = IncrementalTaskSummary(task.name, task.remote_root, task.local_root)
            task_summaries.append(summary)

            if task.sync_mode == "manual":
                selected_dirs_desc = f"手动选择:{' '.join(directory_filters) if directory_filters else '无'}"
            elif task.sync_mode == "recent_days":
                selected_dirs_desc = f"最近{task.recent_value}天:{' '.join(directory_filters)}"
            elif task.sync_mode == "recent_months":
                selected_dirs_desc = f"最近{task.recent_value}月:{' '.join(directory_filters)}"
            else:
                selected_dirs_desc = "全部"

            overwrite_policy = str(getattr(task, "overwrite_policy", "if_newer") or "if_newer")

            resolved_local_root = str(Path(task.local_root).expanduser().resolve())
            state_tracker.update(
                current_task={
                    "name": task.name,
                    "remote_root": task.remote_root,
                    "local_root": resolved_local_root,
                    "sync_mode": task.sync_mode,
                    "recent_value": task.recent_value,
                    "directory_filters": directory_filters,
                    "overwrite_policy": overwrite_policy,
                },
                message=f"正在执行任务: {task.name}",
            )
            logger.info(
                "开始任务: %s | remote=%s | local=%s | target_months=%s | sync_mode=%s | overwrite_policy=%s | selected_dirs=%s",
                task.name,
                task.remote_root,
                resolved_local_root,
                " ".join(executor.target_months),
                task.sync_mode,
                overwrite_policy,
                selected_dirs_desc,
            )
            executor.sync_task_root(task, directory_filters, summary, overwrite_policy)
            summary.status = "failed" if summary.failures > 0 else "success"
            logger.info(
                "任务结束: %s | listed_dirs=%s | downloaded_dirs=%s | downloaded_files=%s | updated_files=%s | skipped_files=%s | local_only_dirs=%s | local_only_files=%s | failures=%s",
                task.name,
                summary.listed_dirs,
                summary.downloaded_dirs,
                summary.downloaded_files,
                summary.updated_files,
                summary.skipped_files,
                summary.local_only_dirs,
                summary.local_only_files,
                summary.failures,
            )
            if summary.failures > 0:
                final_status = "failed"
                final_message = "同步任务执行完成，但存在失败项"
    except StopRequested as exc:
        final_status = "stopped"
        final_message = "同步任务已停止"
        last_error = str(exc)
        logger.warning("增量同步被停止")
    except Exception as exc:
        final_status = "failed"
        final_message = "同步任务执行失败"
        last_error = str(exc)
        logger.exception("增量同步执行异常")
    finally:
        finished_at = datetime.now()
        summary_text = build_summary_text(str(Path(config_path).resolve()), log_file, task_summaries, finished_at)
        write_summary(paths["summary_file"], summary_text)
        state_tracker.finish_run(final_status, final_message, last_error=last_error, summary_text=summary_text)
        logger.info(
            "全部任务完成 | tasks=%s | listed_dirs=%s | downloaded_dirs=%s | downloaded_files=%s | updated_files=%s | skipped_files=%s | local_only_dirs=%s | local_only_files=%s | failures=%s",
            len(task_summaries),
            sum(item.listed_dirs for item in task_summaries),
            sum(item.downloaded_dirs for item in task_summaries),
            sum(item.downloaded_files for item in task_summaries),
            sum(item.updated_files for item in task_summaries),
            sum(item.skipped_files for item in task_summaries),
            sum(item.local_only_dirs for item in task_summaries),
            sum(item.local_only_files for item in task_summaries),
            sum(item.failures for item in task_summaries),
        )
        logger.info("日志文件: %s", log_file)
        logger.info("执行汇总: %s", paths["summary_file"])
        try:
            send_incremental_notification(
                status=final_status,
                message=final_message,
                dry_run=dry_run,
                log_file=log_file,
                summary_text=summary_text,
                task_names=normalized_task_filters,
            )
        finally:
            try:
                lock_fp.close()
            except Exception:
                pass

    return 0 if final_status == "success" else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="使用 bypy 执行增量同步，仅拉取缺失目录/文件并更新变更文件")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="配置文件路径")
    parser.add_argument("--task", action="append", default=[], help="只执行指定任务名称，可重复传入")
    parser.add_argument("--dry-run", action="store_true", help="只输出计划，不实际下载")
    parser.add_argument("--hide-local-only", action="store_true", help="不统计本地多出项")
    parser.add_argument("--include-disabled", action="store_true", help="当显式指定任务时允许执行已禁用任务")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return run_incremental_sync(
        config_path=args.config,
        task_filters=args.task,
        dry_run=args.dry_run,
        include_disabled=args.include_disabled,
        show_local_only=not args.hide_local_only,
        trigger_source="cli",
    )


if __name__ == "__main__":
    raise SystemExit(main())