from flask import Flask, request, jsonify, render_template, send_from_directory, session, redirect, url_for, Response, stream_with_context
from backend.storage import BaiduStorage
from backend.scheduler import TaskScheduler, convert_cron_weekday
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import json
from loguru import logger
import sys
import os
import atexit
import re
import subprocess
import posixpath
from functools import wraps
import signal
from backend.utils import generate_transfer_notification
from backend.notify import send as notify_send
from datetime import datetime
from flask_cors import CORS
import time
import socket
import threading
import queue
import pytz
from collections import deque
from uuid import uuid4

from gevent.pywsgi import WSGIServer
from backend.bypy_sync.full_sync import run_full_sync
from backend.bypy_sync.incremental_sync import run_incremental_sync

# GitHub 仓库信息
GITHUB_REPO = 'kokojacket/baidu-autosave'
# Docker Hub 信息
DOCKER_HUB_RSS = 'https://rsshub.rssforever.com/dockerhub/tag/kokojacket/baidu-autosave'
# 备用 Docker Hub RSS 源
DOCKER_HUB_RSS_ALT = 'https://rss.kuaisouxia.com/dockerhub/tag/kokojacket/baidu-autosave'
# 1ms.run API 源
MS_RUN_API = 'https://1ms.run/api/v1/registry/get_tags'

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BYPY_SYNC_LOG_DIR = os.path.join(ROOT_DIR, 'log', 'bypy_sync')
BYPY_SYNC_CONFIG_PATH = os.path.join(ROOT_DIR, 'config', 'bypy_sync.json')
INCREMENTAL_SYNC_PID_FILE = os.path.join(BYPY_SYNC_LOG_DIR, 'bypy_incremental_manager.pid')
INCREMENTAL_SYNC_STATE_FILE = os.path.join(BYPY_SYNC_LOG_DIR, 'bypy_incremental_state.json')
INCREMENTAL_SYNC_SUMMARY_FILE = os.path.join(BYPY_SYNC_LOG_DIR, 'bypy_incremental_last_run.txt')
FULL_SYNC_MANAGER_PID_FILE = os.path.join(BYPY_SYNC_LOG_DIR, 'manager.pid')
FULL_SYNC_STATE_FILE = os.path.join(BYPY_SYNC_LOG_DIR, 'current_state.json')

# 创建日志目录
os.makedirs(os.path.join(ROOT_DIR, 'log'), exist_ok=True)
os.makedirs(BYPY_SYNC_LOG_DIR, exist_ok=True)

# 配置日志
logger.remove()  # 移除默认的控制台输出

# 定义统一的日志级别
log_level = "DEBUG"  # 使用DEBUG级别，可以看到所有日志

# 过滤敏感信息
def filter_sensitive_info(record):
    """过滤敏感信息，如BDUSS和cookies"""
    message = record["message"]
    # 替换BDUSS
    message = re.sub(r"BDUSS['\"]?\s*:\s*['\"]?([^'\"]+)['\"]?", "BDUSS: [已隐藏]", message)
    # 替换cookies
    message = re.sub(r"cookies['\"]?\s*:\s*['\"]?([^'\"]+)['\"]?", "cookies: [已隐藏]", message)
    record["message"] = message
    return True

# 过滤轮询请求日志
def filter_polling_requests(record):
    """过滤高频轮询请求的访问日志。"""
    message = record["message"]

    # 检查是否是HTTP请求日志（WSGI服务器的访问日志）
    if "GET /api/tasks/status HTTP" in message:
        return False  # 不显示这些日志

    return True  # 显示其他所有日志

# 应用过滤器到所有日志处理器
logger.configure(patcher=filter_sensitive_info)

# 添加彩色的控制台输出（带轮询过滤）
logger.add(sys.stdout, 
          level=log_level, 
          format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
          filter=filter_polling_requests)  # 添加轮询过滤器

app = Flask(__name__)
app.secret_key = 'your-secret-key-here'  # 用于session加密
CORS(app)

# 全局变量声明
storage = None
scheduler = None
local_sync_scheduler = None
web_execution_lock = threading.Lock()


class NativeSyncRuntime:
    def __init__(self, name):
        self.name = name
        self._lock = threading.Lock()
        self._thread = None
        self._stop_event = None

    def is_running(self):
        with self._lock:
            return bool(self._thread and self._thread.is_alive())

    def start(self, target, **kwargs):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return False

            stop_event = threading.Event()

            def runner():
                try:
                    target(stop_event=stop_event, **kwargs)
                except Exception:
                    logger.exception(f"{self.name} 原生任务执行异常")
                finally:
                    with self._lock:
                        self._thread = None
                        self._stop_event = None

            self._stop_event = stop_event
            self._thread = threading.Thread(target=runner, name=f"native-sync-{self.name}", daemon=True)
            self._thread.start()
            return True

    def stop(self, wait_seconds=8.0):
        with self._lock:
            thread = self._thread
            stop_event = self._stop_event

        if not thread or not thread.is_alive() or not stop_event:
            return False

        stop_event.set()
        thread.join(timeout=max(0.0, float(wait_seconds)))
        return not thread.is_alive()


incremental_sync_runtime = NativeSyncRuntime('incremental')
full_sync_runtime = NativeSyncRuntime('full')


def _get_execution_lock():
    if scheduler and hasattr(scheduler, '_execution_lock'):
        return scheduler._execution_lock
    return web_execution_lock


def _ensure_task_runtime_state():
    """初始化任务日志与 SSE 运行时状态。"""
    if not hasattr(app, 'task_logs'):
        app.task_logs = {}
    if not hasattr(app, 'task_stream_subscribers'):
        app.task_stream_subscribers = {}
    if not hasattr(app, 'task_order_to_uid'):
        app.task_order_to_uid = {}
    if not hasattr(app, 'task_cancel_flags'):
        app.task_cancel_flags = {}
    if not hasattr(app, '_log_cleanup_counter'):
        app._log_cleanup_counter = 0


def _set_task_cancel_flag(task_order, cancelled=True):
    _ensure_task_runtime_state()
    if task_order is not None:
        app.task_cancel_flags[task_order] = cancelled


def _clear_task_cancel_flag(task_order):
    _ensure_task_runtime_state()
    if task_order in app.task_cancel_flags:
        del app.task_cancel_flags[task_order]


def _is_task_cancelled(task_order):
    _ensure_task_runtime_state()
    return bool(app.task_cancel_flags.get(task_order))


def _remember_task_stream(task_uid, task_order):
    _ensure_task_runtime_state()
    if task_uid and task_order:
        app.task_order_to_uid[task_order] = task_uid


def _get_task_uid(task_order, task_uid=None):
    _ensure_task_runtime_state()
    return task_uid or app.task_order_to_uid.get(task_order)


def _enqueue_task_event(event_queue, event_name, payload):
    try:
        event_queue.put_nowait((event_name, payload))
    except queue.Full:
        try:
            event_queue.get_nowait()
        except queue.Empty:
            pass
        try:
            event_queue.put_nowait((event_name, payload))
        except queue.Full:
            pass


def _publish_task_event(task_uid, event_name, payload):
    if not task_uid:
        return

    _ensure_task_runtime_state()
    subscribers = list(app.task_stream_subscribers.get(task_uid, []))
    for event_queue in subscribers:
        _enqueue_task_event(event_queue, event_name, payload)


def _register_task_stream(task_uid, event_queue):
    if not task_uid:
        return

    _ensure_task_runtime_state()
    app.task_stream_subscribers.setdefault(task_uid, []).append(event_queue)


def _unregister_task_stream(task_uid, event_queue):
    if not task_uid or not hasattr(app, 'task_stream_subscribers'):
        return

    subscribers = app.task_stream_subscribers.get(task_uid, [])
    if event_queue in subscribers:
        subscribers.remove(event_queue)
    if not subscribers and task_uid in app.task_stream_subscribers:
        del app.task_stream_subscribers[task_uid]


def _format_sse(event_name, payload):
    data = json.dumps(payload, ensure_ascii=False)
    return f"event: {event_name}\ndata: {data}\n\n"


def _build_task_stream_snapshot(task):
    _ensure_task_runtime_state()
    task_order = task.get('order') if task else None
    logs = list(app.task_logs.get(task_order, [])) if task_order else []
    return {
        'task': task,
        'logs': logs,
    }


def _publish_task_status(task_order, task_uid=None):
    if not storage:
        return None

    stable_uid = _get_task_uid(task_order, task_uid)
    task = storage.resolve_task(stable_uid, order=task_order)
    if task and stable_uid:
        _publish_task_event(stable_uid, 'status', task)
    return task


def _append_task_log(task_order, message, level='INFO', task_uid=None):
    stable_uid = _get_task_uid(task_order, task_uid)
    _ensure_task_runtime_state()
    log_entry = {
        'timestamp': datetime.now().strftime('%H:%M:%S'),
        'level': level,
        'message': message,
        'task_order': task_order
    }
    app.task_logs.setdefault(task_order, []).append(log_entry)
    if stable_uid:
        _publish_task_event(stable_uid, 'log', log_entry)
    return log_entry


def _serialize_subscription_tasks(tasks):
    next_run_times = scheduler.get_next_run_times() if scheduler and hasattr(scheduler, 'get_next_run_times') else {}
    serialized_tasks = []

    for task in tasks:
        serialized_task = dict(task)
        serialized_task['next_run_at'] = next_run_times.get(task.get('order'))
        serialized_tasks.append(serialized_task)

    return serialized_tasks


def _publish_task_completed(task_order, task_uid=None):
    task = _publish_task_status(task_order, task_uid)
    stable_uid = _get_task_uid(task_order, task_uid)
    if task and stable_uid:
        _publish_task_event(stable_uid, 'completed', {'task': task})
    return task


def _load_json_file(path, default=None):
    if not os.path.exists(path):
        return default if default is not None else {}

    try:
        with open(path, 'r', encoding='utf-8') as fp:
            return json.load(fp)
    except Exception as e:
        logger.warning(f"读取 JSON 文件失败: {path}, 错误: {str(e)}")
        return default if default is not None else {}


def _write_json_file(path, data):
    temp_path = f"{path}.tmp"
    with open(temp_path, 'w', encoding='utf-8') as fp:
        json.dump(data, fp, ensure_ascii=False, indent=2)
    os.replace(temp_path, path)


def _read_text_file(path, default=''):
    if not os.path.exists(path):
        return default

    try:
        with open(path, 'r', encoding='utf-8') as fp:
            return fp.read()
    except Exception as e:
        logger.warning(f"读取文本文件失败: {path}, 错误: {str(e)}")
        return default


def _read_pid_file(path):
    if not os.path.exists(path):
        return None

    try:
        with open(path, 'r', encoding='utf-8') as fp:
            raw = fp.read().strip()
        return int(raw) if raw else None
    except Exception:
        return None


def _remove_file_if_exists(path):
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


def _is_process_running(pid):
    if not pid:
        return False

    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _kill_process_group(pid):
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except Exception:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return


def _tail_log_file(path, lines=120, max_chars=20000):
    if not path or not os.path.exists(path):
        return ''

    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as fp:
            content = ''.join(deque(fp, maxlen=max(1, lines)))
        return content[-max_chars:]
    except Exception as e:
        logger.warning(f"读取日志尾部失败: {path}, 错误: {str(e)}")
        return ''


def _find_latest_log(prefix):
    try:
        candidates = [
            os.path.join(BYPY_SYNC_LOG_DIR, name)
            for name in os.listdir(BYPY_SYNC_LOG_DIR)
            if name.startswith(prefix) and name.endswith('.log')
        ]
    except OSError:
        return ''

    if not candidates:
        return ''

    candidates.sort(key=lambda path: os.path.getmtime(path), reverse=True)
    return candidates[0]


def _extract_local_sync_task_log_segment(path, task_name, lines=200, max_chars=30000):
    if not path or not os.path.exists(path) or not task_name:
        return ''

    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as fp:
            file_lines = fp.readlines()
    except Exception as e:
        logger.warning(f"读取本地同步任务日志失败: {path}, 错误: {str(e)}")
        return ''

    start_marker = f'开始任务: {task_name} |'
    end_marker = f'任务结束: {task_name} |'
    start_index = -1

    for index in range(len(file_lines) - 1, -1, -1):
        if start_marker in file_lines[index]:
            start_index = index
            break

    if start_index < 0:
        return ''

    end_index = len(file_lines)
    for index in range(start_index + 1, len(file_lines)):
        line = file_lines[index]
        if end_marker in line:
            end_index = index + 1
            break
        if '开始任务: ' in line and start_marker not in line:
            end_index = index
            break

    segment_lines = file_lines[start_index:end_index]
    if len(segment_lines) > lines:
        head_count = min(20, max(1, lines // 5))
        tail_count = max(20, lines - head_count - 1)
        segment_lines = segment_lines[:head_count] + ['... 省略中间日志 ...\n'] + segment_lines[-tail_count:]

    content = ''.join(segment_lines)
    return content[-max_chars:]


def _find_local_sync_task_log(task_name, lines=200):
    state = _load_json_file(INCREMENTAL_SYNC_STATE_FILE, default={})
    active_tasks = _normalize_task_filters(state.get('tasks'))
    active_log_file = state.get('log_file') if task_name in active_tasks else ''

    if active_log_file and os.path.exists(active_log_file):
        segment = _extract_local_sync_task_log_segment(active_log_file, task_name, lines=lines)
        if segment:
            return active_log_file, segment

    full_state = _load_json_file(FULL_SYNC_STATE_FILE, default={})
    current_full_task = full_state.get('current_task') or {}
    active_full_log_file = full_state.get('log_file') if current_full_task.get('name') == task_name else ''

    if active_full_log_file and os.path.exists(active_full_log_file):
        segment = _extract_local_sync_task_log_segment(active_full_log_file, task_name, lines=lines)
        if segment:
            return active_full_log_file, segment

    try:
        candidates = [
            os.path.join(BYPY_SYNC_LOG_DIR, name)
            for name in os.listdir(BYPY_SYNC_LOG_DIR)
            if name.endswith('.log') and (
                name.startswith('bypy_incremental_') or name.startswith('bypy_full_sync_')
            )
        ]
    except OSError:
        return '', ''

    candidates.sort(key=lambda item: os.path.getmtime(item), reverse=True)
    for path in candidates[:30]:
        segment = _extract_local_sync_task_log_segment(path, task_name, lines=lines)
        if segment:
            return path, segment

    return '', ''


def _format_local_sync_recent_message(downloaded_dirs=0, downloaded_files=0, updated_files=0):
    parts = []
    if downloaded_dirs > 0:
        parts.append(f'新增目录 {downloaded_dirs} 个')
    if downloaded_files > 0:
        parts.append(f'新增文件 {downloaded_files} 个')
    if updated_files > 0:
        parts.append(f'更新文件 {updated_files} 个')
    return '，'.join(parts) if parts else '没有新增或更新'


def _parse_local_sync_log_timestamp(value):
    timestamp = str(value or '').strip()
    if not timestamp:
        return None

    for fmt in ('%Y-%m-%d %H:%M:%S,%f', '%Y-%m-%d %H:%M:%S'):
        try:
            return datetime.strptime(timestamp, fmt).isoformat()
        except ValueError:
            continue
    return None


def _extract_local_sync_task_recent_status(task_name, log_file='', log_text=''):
    if not task_name:
        return {
            'recent_run_status': None,
            'recent_run_message': '',
            'recent_run_at': None,
        }

    resolved_log_file = log_file
    resolved_log_text = log_text
    if not resolved_log_text:
        resolved_log_file, resolved_log_text = _find_local_sync_task_log(task_name, lines=500)

    if not resolved_log_text:
        return {
            'recent_run_status': None,
            'recent_run_message': '',
            'recent_run_at': None,
        }

    timestamp_expr = r'(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:,\d{3})?)'
    end_pattern = re.compile(
        rf'^{timestamp_expr} \| INFO \| 任务结束: {re.escape(task_name)} \| (?P<details>.+)$'
    )
    error_pattern = re.compile(rf'^{timestamp_expr} \| ERROR \| (?P<message>.+)$')
    timestamp_pattern = re.compile(rf'^{timestamp_expr}(?: \||$)')

    error_messages = []
    latest_timestamp = None

    for raw_line in resolved_log_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        timestamp_match = timestamp_pattern.match(line)
        if timestamp_match:
            latest_timestamp = timestamp_match.group('timestamp')

        error_match = error_pattern.match(line)
        if error_match:
            error_messages.append(error_match.group('message').strip())

        end_match = end_pattern.match(line)
        if end_match:
            details = end_match.group('details')

            def _extract_metric(name, default=0):
                metric_match = re.search(rf'\b{name}=(\d+)\b', details)
                return int(metric_match.group(1)) if metric_match else default

            status_match = re.search(r'\bstatus=([a-z-]+)\b', details)
            explicit_status = status_match.group(1) if status_match else ''
            downloaded_dirs = _extract_metric('downloaded_dirs')
            downloaded_files = _extract_metric('downloaded_files')
            updated_files = _extract_metric('updated_files')
            failures = _extract_metric('failures')
            finished_at = _parse_local_sync_log_timestamp(end_match.group('timestamp'))

            if failures > 0 or explicit_status in {'failed', 'partial-success'}:
                return {
                    'recent_run_status': 'failed',
                    'recent_run_message': error_messages[-1] if error_messages else f'执行失败，失败数 {failures}',
                    'recent_run_at': finished_at,
                    'recent_log_file': resolved_log_file,
                }

            status = 'success' if (downloaded_dirs > 0 or downloaded_files > 0 or updated_files > 0) else 'skipped'
            return {
                'recent_run_status': status,
                'recent_run_message': _format_local_sync_recent_message(downloaded_dirs, downloaded_files, updated_files),
                'recent_run_at': finished_at,
                'recent_log_file': resolved_log_file,
            }

    if error_messages:
        parsed_at = _parse_local_sync_log_timestamp(latest_timestamp)
        return {
            'recent_run_status': 'failed',
            'recent_run_message': error_messages[-1],
            'recent_run_at': parsed_at,
            'recent_log_file': resolved_log_file,
        }

    parsed_at = _parse_local_sync_log_timestamp(latest_timestamp)

    return {
        'recent_run_status': 'unknown',
        'recent_run_message': '最近一次执行结果待确认',
        'recent_run_at': parsed_at,
        'recent_log_file': resolved_log_file,
    }


def _normalize_task_filters(task_filters):
    if task_filters is None:
        return []

    if isinstance(task_filters, str):
        stripped = task_filters.strip()
        return [stripped] if stripped else []

    normalized = []
    for item in task_filters:
        if item is None:
            continue
        stripped = str(item).strip()
        if stripped:
            normalized.append(stripped)
    return normalized


def _normalize_directory_filters(directory_filters):
    if directory_filters is None:
        return []

    if isinstance(directory_filters, str):
        directory_filters = [directory_filters]

    normalized = []
    for item in directory_filters:
        if item is None:
            continue

        candidate = str(item).replace('\\', '/').strip().strip('/')
        if not candidate:
            continue

        candidate = posixpath.normpath(candidate)
        if candidate in ('', '.'):
            continue
        if candidate == '..' or candidate.startswith('../'):
            continue
        if candidate not in normalized:
            normalized.append(candidate)

    return normalized


def _normalize_local_sync_mode(sync_mode, directory_filters=None):
    allowed_modes = {'all', 'manual', 'recent_days', 'recent_months'}
    candidate = str(sync_mode or '').strip().lower()
    if candidate in allowed_modes:
        return candidate

    if _normalize_directory_filters(directory_filters):
        return 'manual'
    return 'all'


def _normalize_local_sync_recent_value(recent_value, sync_mode):
    if sync_mode not in {'recent_days', 'recent_months'}:
        return 0

    try:
        value = int(recent_value)
    except (TypeError, ValueError):
        value = 0

    return value if value > 0 else 0


def _normalize_local_sync_overwrite_policy(overwrite_policy):
    candidate = str(overwrite_policy or '').strip().lower()
    if candidate in {'never', 'if_newer', 'always'}:
        return candidate
    return 'if_newer'


def _normalize_local_sync_cron(cron_value):
    return ' '.join(str(cron_value or '').strip().split())


def _load_bypy_sync_config():
    config_data = _load_json_file(BYPY_SYNC_CONFIG_PATH, default={})
    if not isinstance(config_data, dict):
        config_data = {}

    config_data.setdefault('bypy', {})
    config_data.setdefault('tasks', [])
    return config_data


def _normalize_bypy_sync_task(item, index=0):
    raw_name = str(item.get('name') or f'本地同步任务{index or 1}').strip()
    raw_remote_root = str(item.get('remote_root') or '').replace('\\', '/').strip()
    raw_local_root = str(item.get('local_root') or '').strip()
    remote_root = posixpath.normpath('/' + raw_remote_root.lstrip('/')) if raw_remote_root else ''
    if remote_root == '/.':
        remote_root = '/'

    sync_mode = _normalize_local_sync_mode(item.get('sync_mode'), item.get('directory_filters'))
    directory_filters = _normalize_directory_filters(item.get('directory_filters')) if sync_mode == 'manual' else []
    recent_value = _normalize_local_sync_recent_value(item.get('recent_value'), sync_mode)
    cron = _normalize_local_sync_cron(item.get('cron'))

    return {
        'task_id': str(item.get('task_id') or '').strip() or uuid4().hex,
        'name': raw_name,
        'enabled': bool(item.get('enabled', True)),
        'auto_run_enabled': bool(item.get('auto_run_enabled', False)),
        'cron': cron,
        'remote_root': remote_root,
        'local_root': raw_local_root,
        'directory_filters': directory_filters,
        'sync_mode': sync_mode,
        'recent_value': recent_value,
        'overwrite_policy': _normalize_local_sync_overwrite_policy(item.get('overwrite_policy')),
    }


def _load_bypy_sync_tasks(include_disabled=True):
    config_data = _load_bypy_sync_config()
    normalized_tasks = []
    changed = False

    for index, item in enumerate(config_data.get('tasks', []), start=1):
        normalized_task = _normalize_bypy_sync_task(item, index=index)
        normalized_tasks.append(normalized_task)
        if normalized_task != item:
            changed = True

    if changed:
        config_data['tasks'] = normalized_tasks
        _write_json_file(BYPY_SYNC_CONFIG_PATH, config_data)

    if include_disabled:
        return normalized_tasks

    return [item for item in normalized_tasks if item.get('enabled', True)]


def _load_bypy_sync_task_names():
    return [item['name'] for item in _load_bypy_sync_tasks(include_disabled=False) if item.get('name')]


def _find_bypy_sync_task(task_id=None, task_name=None):
    for task in _load_bypy_sync_tasks(include_disabled=True):
        if task_id and task.get('task_id') == task_id:
            return task
        if task_name and task.get('name') == task_name:
            return task
    return None


def _save_bypy_sync_tasks(tasks):
    config_data = _load_bypy_sync_config()
    config_data['tasks'] = [_normalize_bypy_sync_task(item, index=index) for index, item in enumerate(tasks, start=1)]
    _write_json_file(BYPY_SYNC_CONFIG_PATH, config_data)
    return config_data['tasks']


class LocalSyncTaskScheduler:
    def __init__(self):
        self.timezone = pytz.timezone('Asia/Shanghai')
        self.scheduler = BackgroundScheduler(
            timezone=self.timezone,
            executors={'default': ThreadPoolExecutor(max_workers=1)},
            job_defaults={
                'coalesce': True,
                'max_instances': 1,
                'misfire_grace_time': 3600,
            },
        )
        self.started = False

    def _job_id(self, task_id):
        return f'local_sync_task_{task_id}'

    def start(self):
        if not self.started:
            self.scheduler.start()
            self.started = True
        self.sync_jobs()

    def stop(self):
        if self.started:
            self.scheduler.shutdown(wait=False)
            self.started = False

    def sync_jobs(self):
        desired_job_ids = set()

        for task in _load_bypy_sync_tasks(include_disabled=True):
            cron_value = _normalize_local_sync_cron(task.get('cron'))
            if not task.get('enabled') or not task.get('auto_run_enabled') or not cron_value:
                continue

            job_id = self._job_id(task['task_id'])
            desired_job_ids.add(job_id)

            try:
                trigger = CronTrigger.from_crontab(convert_cron_weekday(cron_value), timezone=self.timezone)
                self.scheduler.add_job(
                    self._execute_task,
                    trigger=trigger,
                    args=[task['task_id']],
                    id=job_id,
                    replace_existing=True,
                )
            except Exception as e:
                logger.error(f"本地同步任务 cron 无法生效: {task.get('name', task['task_id'])} -> {cron_value}, 错误: {str(e)}")

        for job in self.scheduler.get_jobs():
            if job.id.startswith('local_sync_task_') and job.id not in desired_job_ids:
                self.scheduler.remove_job(job.id)

    def _execute_task(self, task_id):
        task = _find_bypy_sync_task(task_id=task_id)
        if not task:
            logger.warning(f"定时执行本地同步任务失败，任务不存在: {task_id}")
            return
        if not task.get('enabled') or not task.get('auto_run_enabled'):
            logger.info(f"跳过未启用的本地同步自动任务: {task.get('name', task_id)}")
            return

        success, message, _status = _run_local_sync_task(
            task_id=task_id,
            dry_run=False,
            include_disabled=False,
            background=False,
            trigger_source='schedule',
        )
        if success:
            logger.info(f"本地同步定时任务执行完成: {task.get('name', task_id)}")
        else:
            logger.warning(f"本地同步定时任务执行失败: {task.get('name', task_id)} | {message}")

    def get_next_run_times(self):
        next_runs = {}
        if not self.started:
            return next_runs

        for job in self.scheduler.get_jobs():
            if not job.id.startswith('local_sync_task_'):
                continue
            task_id = job.id.replace('local_sync_task_', '', 1)
            next_runs[task_id] = job.next_run_time.astimezone(self.timezone).isoformat() if job.next_run_time else None
        return next_runs


def _serialize_local_sync_tasks(tasks):
    next_run_times = local_sync_scheduler.get_next_run_times() if local_sync_scheduler else {}
    incremental_status = _build_incremental_sync_status()
    full_status = _build_full_sync_status()
    incremental_running_tasks = set(_normalize_task_filters(incremental_status.get('tasks')))
    full_running_tasks = set(_normalize_task_filters(full_status.get('tasks')))
    serialized_tasks = []
    for task in tasks:
        serialized_task = dict(task)
        serialized_task['next_run_at'] = next_run_times.get(task.get('task_id'))
        if task.get('name') in incremental_running_tasks and incremental_status.get('running', False):
            serialized_task['recent_run_status'] = 'running'
            serialized_task['recent_run_message'] = '任务正在执行中'
            serialized_task['recent_run_at'] = incremental_status.get('started_at')
        elif task.get('name') in full_running_tasks and full_status.get('running', False):
            serialized_task['recent_run_status'] = 'running'
            serialized_task['recent_run_message'] = '任务正在执行中'
            serialized_task['recent_run_at'] = full_status.get('started_at')
        else:
            serialized_task.update(_extract_local_sync_task_recent_status(task.get('name', '')))
        serialized_tasks.append(serialized_task)
    return serialized_tasks


def _build_bypy_command(config_data, retry_times=None):
    bypy_config = config_data.get('bypy', {})
    command = [str(bypy_config.get('binary') or 'bypy')]
    command.extend(['--retry', str(retry_times if retry_times is not None else int(bypy_config.get('retry_times', 3)))])
    command.extend(['--timeout', str(int(bypy_config.get('network_timeout', 300)))])
    command.extend(['--processes', str(max(1, int(bypy_config.get('processes', 1))))])
    if bypy_config.get('verify_download', False):
        command.append('--verify')

    config_dir = str(bypy_config.get('config_dir') or '').strip()
    if config_dir:
        command.extend(['--config-dir', config_dir])

    return command


def _list_bypy_sync_directories(remote_root):
    normalized_remote_root = posixpath.normpath('/' + str(remote_root or '').replace('\\', '/').strip().lstrip('/'))
    if normalized_remote_root == '/.':
        normalized_remote_root = '/'

    if not normalized_remote_root:
        raise ValueError('远程目录不能为空')

    config_data = _load_bypy_sync_config()
    command = _build_bypy_command(config_data, retry_times=1)
    command.extend(['list', normalized_remote_root, '$t|$f|$s|$m'])

    completed = subprocess.run(
        command,
        cwd=ROOT_DIR,
        capture_output=True,
        text=True,
        timeout=60,
    )

    output = '\n'.join(part for part in [completed.stdout.strip(), completed.stderr.strip()] if part).strip()
    if completed.returncode != 0:
        raise RuntimeError(output or '列出远程目录失败')

    directories = []
    for raw_line in completed.stdout.splitlines():
        line = raw_line.strip()
        if not line or line.startswith('/apps/bypy/') or line.startswith('/'):
            continue

        parts = line.split('|', 3)
        if len(parts) != 4:
            continue

        entry_type, entry_name, _entry_size, _entry_mtime = parts
        if entry_type.strip() != 'D':
            continue

        relative_path = entry_name.strip().replace('\\', '/').strip('/').strip()
        if not relative_path:
            continue

        directories.append({
            'path': relative_path,
            'label': relative_path,
        })

    directories.sort(key=lambda item: item['path'])
    return directories


def _set_local_sync_in_process_running(running):
    setattr(app, 'local_sync_in_process_running', bool(running))


def _is_external_state_process_running(pid):
    return bool(pid) and pid != os.getpid() and _is_process_running(pid)


def _execute_incremental_sync_native(dry_run=False, task_filters=None, include_disabled=False, trigger_source='manual', stop_event=None):
    _set_local_sync_in_process_running(True)
    try:
        return run_incremental_sync(
            config_path=BYPY_SYNC_CONFIG_PATH,
            task_filters=task_filters,
            dry_run=dry_run,
            include_disabled=include_disabled,
            trigger_source=trigger_source,
            stop_event=stop_event,
        )
    finally:
        _set_local_sync_in_process_running(False)


def _execute_full_sync_native(dry_run=False, task_filters=None, stop_event=None):
    return run_full_sync(
        config_path=BYPY_SYNC_CONFIG_PATH,
        task_filters=_normalize_task_filters(task_filters),
        dry_run=dry_run,
        stop_event=stop_event,
        install_signal_handlers=False,
        state_pid=None,
    )


def _write_incremental_sync_state(status, message, dry_run=False, tasks=None, log_file='', pid=None, started_at=None, finished_at=None, trigger_source='manual'):
    state = {
        'sync_type': 'incremental',
        'status': status,
        'pid': pid,
        'started_at': started_at,
        'finished_at': finished_at,
        'message': message,
        'dry_run': bool(dry_run),
        'tasks': _normalize_task_filters(tasks),
        'config': BYPY_SYNC_CONFIG_PATH,
        'log_file': log_file,
        'trigger_source': trigger_source,
    }
    _write_json_file(INCREMENTAL_SYNC_STATE_FILE, state)


def _run_incremental_sync_command(dry_run=False, task_filters=None, include_disabled=False, background=True, trigger_source='manual'):
    normalized_tasks = _normalize_task_filters(task_filters)

    current_status = _build_incremental_sync_status()
    if current_status.get('running'):
        return False, '增量同步已在运行中', current_status

    if background:
        started = incremental_sync_runtime.start(
            _execute_incremental_sync_native,
            dry_run=dry_run,
            task_filters=normalized_tasks,
            include_disabled=include_disabled,
            trigger_source=trigger_source,
        )
        if not started:
            return False, '增量同步已在运行中', _build_incremental_sync_status()
        _remove_file_if_exists(INCREMENTAL_SYNC_PID_FILE)
        return True, '增量同步已启动', _build_incremental_sync_status()

    exit_code = _execute_incremental_sync_native(
        dry_run=dry_run,
        task_filters=normalized_tasks,
        include_disabled=include_disabled,
        trigger_source=trigger_source,
        stop_event=threading.Event(),
    )
    status = _build_incremental_sync_status()
    success = exit_code == 0 and status.get('status') not in {'failed', 'stopped'}
    message = status.get('message') or ('本地同步任务执行完成' if success else '本地同步任务执行失败')
    if not success:
        log_tail = _tail_log_file(status.get('log_file'), lines=30).strip()
        return False, log_tail or message, status
    return True, message, status


def _build_incremental_sync_status():
    state = _load_json_file(INCREMENTAL_SYNC_STATE_FILE, default={})
    pid = _read_pid_file(INCREMENTAL_SYNC_PID_FILE)
    external_running = _is_external_state_process_running(pid)
    native_running = incremental_sync_runtime.is_running() or bool(getattr(app, 'local_sync_in_process_running', False))
    running = external_running or native_running

    if pid and not external_running:
        _remove_file_if_exists(INCREMENTAL_SYNC_PID_FILE)

    log_file = state.get('log_file')
    if not log_file or not os.path.exists(log_file):
        log_file = _find_latest_log('bypy_incremental_')

    status = state.get('status') or ('running' if running else 'idle')
    message = state.get('message') or ('增量同步运行中' if running else '增量同步未运行')
    if native_running and status != 'running':
        status = 'running'
        message = '增量同步运行中'
    elif not running and status == 'running':
        status = 'idle'
        message = '增量同步未运行'

    return {
        'sync_type': 'incremental',
        'running': running,
        'status': status,
        'pid': pid if external_running else None,
        'log_file': log_file or '',
        'started_at': state.get('started_at'),
        'finished_at': state.get('finished_at'),
        'message': message,
        'dry_run': bool(state.get('dry_run', False)),
        'tasks': state.get('tasks', []),
        'summary_text': _read_text_file(INCREMENTAL_SYNC_SUMMARY_FILE, default='').strip() or state.get('summary_text', ''),
    }


def _build_full_sync_status():
    state = _load_json_file(FULL_SYNC_STATE_FILE, default={})
    pid = state.get('pid')
    external_running = _is_external_state_process_running(pid)
    native_running = full_sync_runtime.is_running()
    running = external_running or native_running

    log_file = state.get('log_file')
    if not log_file or not os.path.exists(log_file):
        log_file = _find_latest_log('bypy_full_sync_')

    status = state.get('status') or ('running' if running else 'idle')
    message = state.get('message') or ('全量补缺同步运行中' if running else '全量补缺同步未运行')
    if native_running and status != 'running':
        status = 'running'
        message = '全量补缺同步运行中'
    elif not running and status == 'running':
        status = 'idle'
        message = '全量补缺同步未运行'

    current_task = state.get('current_task') or {}
    return {
        'sync_type': 'full',
        'running': running,
        'status': status,
        'pid': pid if external_running else None,
        'log_file': log_file or '',
        'started_at': state.get('started_at'),
        'finished_at': state.get('finished_at'),
        'message': message,
        'dry_run': bool(state.get('dry_run', False)),
        'tasks': current_task.get('name') and [current_task.get('name')] or [],
        'summary_text': '',
    }


def _spawn_background_process(command, log_file, env=None):
    with open(log_file, 'a', encoding='utf-8') as fp:
        process = subprocess.Popen(
            command,
            cwd=ROOT_DIR,
            env=env or os.environ.copy(),
            stdin=subprocess.DEVNULL,
            stdout=fp,
            stderr=subprocess.STDOUT,
            preexec_fn=os.setsid,
        )
    return process.pid


def _start_incremental_sync(dry_run=False, task_filters=None):
    return _run_incremental_sync_command(
        dry_run=dry_run,
        task_filters=task_filters,
        include_disabled=False,
        background=True,
        trigger_source='manual',
    )


def _run_local_sync_task(task_id, dry_run=False, include_disabled=True, background=True, trigger_source='manual'):
    task = _find_bypy_sync_task(task_id=task_id)
    if not task:
        return False, '未找到对应的本地同步任务', _build_incremental_sync_status()

    return _run_incremental_sync_command(
        dry_run=dry_run,
        task_filters=[task.get('name')],
        include_disabled=include_disabled,
        background=background,
        trigger_source=trigger_source,
    )


def _stop_incremental_sync():
    if incremental_sync_runtime.is_running():
        stopped = incremental_sync_runtime.stop()
        message = '增量同步已停止' if stopped else '已发送增量同步停止请求，等待任务结束'
        state = _load_json_file(INCREMENTAL_SYNC_STATE_FILE, default={})
        state.update({
            'status': 'stopped' if stopped else state.get('status', 'running'),
            'pid': None,
            'message': message,
            'finished_at': datetime.now().isoformat(timespec='seconds') if stopped else state.get('finished_at'),
        })
        _write_json_file(INCREMENTAL_SYNC_STATE_FILE, state)
        return True, message, _build_incremental_sync_status()

    pid = _read_pid_file(INCREMENTAL_SYNC_PID_FILE)
    if not pid or not _is_external_state_process_running(pid):
        _remove_file_if_exists(INCREMENTAL_SYNC_PID_FILE)
        state = _load_json_file(INCREMENTAL_SYNC_STATE_FILE, default={})
        state.update({
            'status': 'idle',
            'pid': None,
            'message': '增量同步未运行',
            'finished_at': datetime.now().isoformat(timespec='seconds'),
        })
        _write_json_file(INCREMENTAL_SYNC_STATE_FILE, state)
        return False, '增量同步未运行', _build_incremental_sync_status()

    _kill_process_group(pid)
    for _ in range(20):
        if not _is_process_running(pid):
            break
        time.sleep(0.3)

    _remove_file_if_exists(INCREMENTAL_SYNC_PID_FILE)
    state = _load_json_file(INCREMENTAL_SYNC_STATE_FILE, default={})
    state.update({
        'status': 'stopped',
        'pid': None,
        'message': '增量同步已停止',
        'finished_at': datetime.now().isoformat(timespec='seconds'),
    })
    _write_json_file(INCREMENTAL_SYNC_STATE_FILE, state)
    return True, '增量同步已停止', _build_incremental_sync_status()


def _run_full_sync_manager(action, dry_run=False, task_filters=None):
    normalized_tasks = _normalize_task_filters(task_filters)
    if action == 'start':
        if _build_full_sync_status().get('running'):
            return False, '全量补缺同步已在运行中'
        started = full_sync_runtime.start(
            _execute_full_sync_native,
            dry_run=dry_run,
            task_filters=normalized_tasks,
        )
        return started, '全量补缺同步已启动' if started else '全量补缺同步已在运行中'

    if action == 'stop':
        if full_sync_runtime.is_running():
            stopped = full_sync_runtime.stop()
            state = _load_json_file(FULL_SYNC_STATE_FILE, default={})
            state.update({
                'status': 'stopped' if stopped else state.get('status', 'running'),
                'message': '全量补缺同步已停止' if stopped else '已发送全量补缺同步停止请求，等待任务结束',
                'finished_at': datetime.now().isoformat(timespec='seconds') if stopped else state.get('finished_at'),
            })
            _write_json_file(FULL_SYNC_STATE_FILE, state)
            return True, state.get('message')

        pid = _load_json_file(FULL_SYNC_STATE_FILE, default={}).get('pid')
        if _is_external_state_process_running(pid):
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError as exc:
                return False, f'全量补缺同步停止失败: {str(exc)}'
            return True, '已发送全量补缺同步停止请求'

        return False, '全量补缺同步未运行'

    return False, '不支持的操作'


def get_server_port():
    """从环境变量读取服务端口，默认保持 5000 兼容。"""
    raw_port = os.environ.get('WEB_APP_PORT') or os.environ.get('PORT') or '5000'
    try:
        return int(raw_port)
    except ValueError:
        logger.warning(f"无效的服务端口配置: {raw_port}，回退到 5000")
        return 5000


# 登录装饰器
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'username' not in session:
            return redirect(url_for('login'))
            
        # 检查会话是否过期
        auth_config = storage.config.get('auth', {})
        session_timeout = auth_config.get('session_timeout', 3600)
        if time.time() - session.get('login_time', 0) > session_timeout:
            session.clear()
            return redirect(url_for('login'))
            
        # 更新最后活动时间
        session['login_time'] = time.time()
        return f(*args, **kwargs)
    return decorated_function


def init_app():
    """初始化应用"""
    global storage, scheduler, local_sync_scheduler
    try:
        logger.info("开始初始化应用...")
        # 初始化存储
        logger.info("正在初始化存储...")
        storage = BaiduStorage()
        
        # 使用已创建的 storage 实例初始化调度器
        try:
            logger.info("正在初始化调度器...")
            scheduler = TaskScheduler(storage)
            scheduler.start()
            logger.info("调度器初始化成功")
        except Exception as e:
            logger.error(f"初始化调度器失败: {str(e)}")
            scheduler = None

        try:
            logger.info("正在初始化本地同步调度器...")
            local_sync_scheduler = LocalSyncTaskScheduler()
            local_sync_scheduler.start()
            _set_local_sync_in_process_running(False)
            logger.info("本地同步调度器初始化成功")
        except Exception as e:
            logger.error(f"初始化本地同步调度器失败: {str(e)}")
            local_sync_scheduler = None
        
        if not storage.is_valid():
            logger.warning("存储初始化成功，但未登录或未配置用户")
            
        logger.info("应用初始化完成")
        return True, None
        
    except Exception as e:
        error_msg = f"应用初始化失败: {str(e)}"
        logger.error(error_msg)
        return False, error_msg

def cleanup():
    """清理资源"""
    global scheduler, local_sync_scheduler
    if scheduler:
        try:
            if hasattr(scheduler, 'is_running') and scheduler.is_running:
                scheduler.stop()
            scheduler = None
            logger.info("调度器已停止")
        except Exception as e:
            logger.error(f"停止调度器失败: {str(e)}")
    if local_sync_scheduler:
        try:
            local_sync_scheduler.stop()
            local_sync_scheduler = None
            logger.info("本地同步调度器已停止")
        except Exception as e:
            logger.error(f"停止本地同步调度器失败: {str(e)}")

def handle_api_error(f):
    """API错误处理装饰器"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except Exception as e:
            error_msg = f"{f.__name__} 失败: {str(e)}"
            logger.error(error_msg)
            return jsonify({'success': False, 'message': error_msg})
    return decorated_function

@app.route('/login', methods=['GET', 'POST'])
def login():
    """登录处理"""
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        if not storage:
            # 对于POST请求，返回JSON响应（新前端使用API）
            return jsonify({'success': False, 'message': '系统未初始化'}), 400
            
        # 验证用户名和密码
        auth_config = storage.config.get('auth', {})
        if (username == auth_config.get('users') and 
            password == auth_config.get('password')):
            session['username'] = username
            session['login_time'] = time.time()
            
            # 返回JSON响应给新前端
            return jsonify({'success': True, 'message': '登录成功'})
        else:
            return jsonify({'success': False, 'message': '用户名或密码错误'}), 401
            
    # GET请求返回SPA的index.html，让Vue Router处理登录页面
    return send_from_directory('static', 'index.html')

@app.route('/logout')
def logout():
    """登出处理"""
    session.clear()
    # 返回JSON响应给新前端，而不是重定向
    return jsonify({'success': True, 'message': '登出成功'})

@app.route('/')
@login_required
def index():
    """首页 - 返回新前端SPA"""
    return send_from_directory('static', 'index.html')

@app.route('/api/tasks', methods=['GET'])
@login_required
@handle_api_error
def get_tasks():
    """获取所有任务"""
    if not storage:
        return jsonify({'success': False, 'message': '存储未初始化'})
    tasks = storage.list_tasks()
    # 按 order 排序，没有 order 的排在最后
    tasks.sort(key=lambda x: x.get('order', float('inf')))
    return jsonify({'success': True, 'tasks': _serialize_subscription_tasks(tasks)})

@app.route('/api/tasks/<int:task_id>/status', methods=['GET'])
@login_required
@handle_api_error
def get_task_status(task_id):
    """获取单个任务状态"""
    if not storage:
        return jsonify({'success': False, 'message': '存储未初始化'})

    task_uid = request.args.get('task_uid')
    task_order = request.args.get('task_order')
    if task_order is not None:
        try:
            task_order = int(task_order)
        except (TypeError, ValueError):
            task_order = None

    if task_uid or task_order is not None:
        task = storage.resolve_task(task_uid, order=task_order)
        if task:
            return jsonify({'success': True, 'status': _serialize_subscription_tasks([task])[0]})

    tasks = storage.list_tasks()
    # 按 order 排序，确保 task_id 对应正确的任务
    tasks.sort(key=lambda x: x.get('order', float('inf')))
    if 0 <= task_id < len(tasks):
        return jsonify({'success': True, 'status': _serialize_subscription_tasks([tasks[task_id]])[0]})
    return jsonify({'success': False, 'message': '任务不存在'})

@app.route('/api/tasks/running', methods=['GET'])
@login_required
@handle_api_error
def get_running_tasks():
    """获取正在运行的任务"""
    if not storage:
        return jsonify({'success': False, 'message': '存储未初始化'})
    tasks = storage.list_tasks()
    # 按 order 排序后再筛选运行中的任务
    tasks.sort(key=lambda x: x.get('order', float('inf')))
    running_tasks = [task for task in tasks if task.get('status') == 'running']
    return jsonify({'success': True, 'tasks': _serialize_subscription_tasks(running_tasks)})

@app.route('/api/task/add', methods=['POST'])
@login_required
@handle_api_error
def add_task():
    """添加任务"""
    data = request.get_json()
    url = data.get('url', '').strip()
    save_dir = data.get('save_dir', '').strip()
    pwd = data.get('pwd', '').strip()
    name = data.get('name', '').strip()
    sync_mode = str(data.get('sync_mode', 'incremental')).strip().lower()
    sync_scope_type = str(data.get('sync_scope_type', 'recent_months')).strip().lower()
    recent_months = data.get('recent_months', 2)
    scope_start_month = str(data.get('scope_start_month', '')).strip()
    scope_end_month = str(data.get('scope_end_month', '')).strip()
    overwrite_policy = str(data.get('overwrite_policy', 'window_only')).strip().lower()
    date_dir_mode = str(data.get('date_dir_mode', 'auto')).strip().lower()
    date_dir_patterns = data.get('date_dir_patterns', [])
    cron = data.get('cron', '').strip()
    category = data.get('category', '').strip()
    regex_pattern = data.get('regex_pattern', '').strip()
    regex_replace = data.get('regex_replace', '').strip()
    
    if not url or not save_dir:
        return jsonify({'success': False, 'message': '分享链接和保存目录不能为空'})
    
    # 移除URL中的hash部分
    if '#' in url:
        url = url.split('#')[0]
    
    # 处理第二种格式: https://pan.baidu.com/share/init?surl=xxx&pwd=xxx
    if '/share/init?' in url and 'surl=' in url:
        import urllib.parse
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)
        
        # 提取surl和pwd参数
        surl = params.get('surl', [''])[0]
        if not pwd and 'pwd' in params:
            pwd = params.get('pwd', [''])[0]
        
        # 转换为第一种格式
        if surl:
            url = f"https://pan.baidu.com/s/{surl}"
    
    # 处理第一种格式中的密码部分
    if '?pwd=' in url:
        url, pwd = url.split('?pwd=')
        pwd = pwd.strip()
        
    try:
        # 添加任务，backend.storage 内部会处理调度器更新
        if storage.add_task(
            url,
            save_dir,
            pwd,
            name,
            cron,
            category,
            regex_pattern,
            regex_replace,
            False,
            False,
            sync_mode=sync_mode,
            sync_scope_type=sync_scope_type,
            recent_months=recent_months,
            scope_start_month=scope_start_month,
            scope_end_month=scope_end_month,
            overwrite_policy=overwrite_policy,
            date_dir_mode=date_dir_mode,
            date_dir_patterns=date_dir_patterns,
        ):
            
            return jsonify({'success': True, 'message': '添加任务成功'})
            
    except Exception as e:
        logger.error(f"添加任务失败: {str(e)}")
        return jsonify({'success': False, 'message': f'添加任务失败: {str(e)}'})

@app.route('/api/task/update', methods=['POST'])
@login_required
@handle_api_error
def update_task():
    """更新任务"""
    data = request.get_json()
    try:
        task_id = int(data.get('task_id', -1))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'message': '无效的任务ID'})
    
    if not storage:
        return jsonify({'success': False, 'message': '存储未初始化'})
        
    tasks = storage.list_tasks()
    if not tasks:
        return jsonify({'success': False, 'message': '任务列表为空'})
        
    # 按order排序
    tasks.sort(key=lambda x: x.get('order', float('inf')))
    
    # 查找对应order的任务
    task_order = task_id + 1  # task_id 是从0开始的索引，而 order 是从1开始的
    task = None
    for t in tasks:
        if t.get('order') == task_order:
            task = t
            break
            
    if not task:
        return jsonify({'success': False, 'message': f'未找到任务(order={task_order})'})
    
    # 获取新的URL和密码
    new_url = data.get('url', '').strip()
    new_pwd = data.get('pwd', '').strip()
    
    # 如果URL中包含密码部分，提取出来
    if '?pwd=' in new_url:
        new_url, new_pwd = new_url.split('?pwd=')
        new_url = new_url.strip()
        new_pwd = new_pwd.strip()
    
    # 创建更新数据对象，保持原有状态
    update_data = {
        'url': new_url,
        'save_dir': data.get('save_dir', '').strip(),
        'pwd': new_pwd,  # 使用处理后的新密码
        'name': data.get('name', '').strip(),
        'sync_mode': str(data.get('sync_mode', 'incremental')).strip().lower(),
        'sync_scope_type': str(data.get('sync_scope_type', 'recent_months')).strip().lower(),
        'recent_months': data.get('recent_months', 2),
        'scope_start_month': str(data.get('scope_start_month', '')).strip(),
        'scope_end_month': str(data.get('scope_end_month', '')).strip(),
        'overwrite_policy': str(data.get('overwrite_policy', 'window_only')).strip().lower(),
        'date_dir_mode': str(data.get('date_dir_mode', 'auto')).strip().lower(),
        'date_dir_patterns': data.get('date_dir_patterns', []),
        'cron': data.get('cron', '').strip(),
        'category': data.get('category', '').strip(),
        'regex_pattern': data.get('regex_pattern', '').strip(),
        'regex_replace': data.get('regex_replace', '').strip(),
        'order': task_order,  # 保持原有的order
        'status': task.get('status', 'normal'),  # 保持原有的状态
        'message': task.get('message', ''),  # 保持原有的消息
        'last_update': int(time.time())  # 添加更新时间戳
    }
    
    # 验证必填字段
    if not update_data['url']:
        return jsonify({'success': False, 'message': '分享链接不能为空'})
    if not update_data['save_dir']:
        return jsonify({'success': False, 'message': '保存目录不能为空'})
        
    try:
        # 更新任务
        success = storage.update_task_by_order(task_order, update_data)
        if not success:
            return jsonify({'success': False, 'message': '更新任务失败'})
        
        
        return jsonify({
            'success': True, 
            'message': '更新任务成功',
            'task': update_data
        })
    except Exception as e:
        logger.error(f"更新任务失败: {str(e)}")
        return jsonify({'success': False, 'message': f'更新任务失败: {str(e)}'})

@app.route('/api/share/info', methods=['POST'])
@login_required
@handle_api_error
def get_share_info():
    """获取分享链接信息"""
    data = request.get_json()
    url = data.get('url', '').strip()
    pwd = data.get('pwd', '').strip()
    
    if not url:
        return jsonify({'success': False, 'message': '分享链接不能为空'})
    
    try:
        # 移除URL中的hash部分
        url = url.split('#')[0]
        
        # 处理第二种格式: https://pan.baidu.com/share/init?surl=xxx&pwd=xxx
        if '/share/init?' in url and 'surl=' in url:
            import urllib.parse
            parsed = urllib.parse.urlparse(url)
            params = urllib.parse.parse_qs(parsed.query)
            
            # 提取surl和pwd参数
            surl = params.get('surl', [''])[0]
            if not pwd and 'pwd' in params:
                pwd = params.get('pwd', [''])[0]
            
            # 转换为第一种格式
            if surl:
                url = f"https://pan.baidu.com/s/{surl}"
        
        # 处理第一种格式中的密码部分
        if '?pwd=' in url:
            url, extracted_pwd = url.split('?pwd=')
            pwd = extracted_pwd.strip()
        
        # 获取分享文件信息
        result = storage.get_share_folder_name(url, pwd)
        
        if result['success']:
            return jsonify({
                'success': True,
                'folder_name': result['folder_name'],
                'message': '获取文件夹名称成功'
            })
        else:
            return jsonify({
                'success': False,
                'message': result.get('error', '获取分享信息失败')
            })
            
    except Exception as e:
        logger.error(f"获取分享信息失败: {str(e)}")
        return jsonify({'success': False, 'message': f'获取分享信息失败: {str(e)}'})

@app.route('/api/task/delete', methods=['POST'])
@login_required
@handle_api_error
def delete_task():
    """删除任务"""
    data = request.get_json()
    task_id = data.get('task_id')
    
    if not storage:
        return jsonify({'success': False, 'message': '存储未初始化'})
    tasks = storage.list_tasks()
    # 按 order 排序，确保 task_id 对应正确的任务
    tasks.sort(key=lambda x: x.get('order', float('inf')))
    if 0 <= task_id < len(tasks):
        task = tasks[task_id]
        task_order = task.get('order', task_id + 1)
        if storage.remove_task_by_order(task_order):
            # 删除成功后重新整理剩余任务的顺序
            storage._update_task_orders()
            return jsonify({'success': True, 'message': '删除任务成功'})
    return jsonify({'success': False, 'message': '任务不存在'})


@app.route('/api/task/move', methods=['POST'])
@login_required
@handle_api_error
def move_task():
    """移动任务位置"""
    data = request.get_json()
    task_id = data.get('task_id')
    new_index = data.get('new_index')
    
    if task_id is None or new_index is None:
        return jsonify({'success': False, 'message': '缺少必要参数'})
    
    if not storage:
        return jsonify({'success': False, 'message': '存储未初始化'})
    
    try:
        tasks = storage.list_tasks()
        # 按 order 排序
        tasks.sort(key=lambda x: x.get('order', float('inf')))
        
        if not (0 <= task_id < len(tasks)) or not (0 <= new_index < len(tasks)):
            return jsonify({'success': False, 'message': '任务ID或位置无效'})
        
        # 移动任务
        task = tasks.pop(task_id)
        tasks.insert(new_index, task)
        
        # 更新所有任务的order
        for i, task in enumerate(tasks):
            task['order'] = i + 1
            storage.update_task(i, task)
        
        return jsonify({'success': True, 'message': '任务位置已更新'})
        
    except Exception as e:
        logger.error(f"移动任务失败: {str(e)}")
        return jsonify({'success': False, 'message': f'移动任务失败: {str(e)}'})


def _resolve_request_task(task_ref=None, task_id=None, tasks=None):
    """按 task_uid/order/url 解析请求中的任务，兼容旧的 task_id。"""
    if not storage:
        return None

    task = storage.resolve_task(task_ref)
    if task is not None:
        return task

    if task_id is None:
        return None

    current_tasks = list(tasks if tasks is not None else storage.list_tasks())
    current_tasks.sort(key=lambda x: x.get('order', float('inf')))
    if 0 <= task_id < len(current_tasks):
        fallback_task = current_tasks[task_id]
        return storage.resolve_task(fallback_task) or fallback_task

    return None


@app.route('/api/task/execute', methods=['POST'])
@login_required
@handle_api_error
def execute_task():
    """执行指定的任务"""
    data = request.get_json() or {}
    task_uid = data.get('task_uid')

    # 获取并验证task_id（兼容旧前端只传索引）
    task_id = data.get('task_id')
    if task_uid is None:
        try:
            task_id = int(task_id)
        except (TypeError, ValueError):
            return jsonify({'success': False, 'message': '无效的任务ID'})
    elif task_id is not None:
        try:
            task_id = int(task_id)
        except (TypeError, ValueError):
            task_id = None

    if not storage:
        return jsonify({'success': False, 'message': '存储未初始化'})

    # 获取任务列表
    tasks = storage.list_tasks()
    if not tasks:
        return jsonify({'success': False, 'message': '任务列表为空'})

    # 按稳定标识优先解析，避免任务重排后串线
    task = _resolve_request_task(task_ref=task_uid, task_id=task_id, tasks=tasks)
    if not task:
        if task_uid:
            return jsonify({'success': False, 'message': f'未找到任务(task_uid={task_uid})'})
        return jsonify({'success': False, 'message': f'任务索引超出范围(task_id={task_id})'})

    task_order = task.get('order')
    task_uid = task.get('task_uid')
    task_url = task.get('url')

    if not task_order:
        return jsonify({'success': False, 'message': f'任务order不存在(task_uid={task_uid or task_id})'})

    task_name = task.get('name') or f'任务{task_order}'

    _ensure_task_runtime_state()
    _remember_task_stream(task_uid, task_order)

    execution_lock = _get_execution_lock()
    if not execution_lock.acquire(blocking=False):
        busy_message = '当前已有任务正在执行，请稍后再试'
        return jsonify({'success': False, 'message': busy_message})

    _clear_task_cancel_flag(task_order)

    # 先抢到执行锁，再切换任务到运行中，避免前端刚点启动就进入伪运行态。
    storage.update_task_status_by_order(task_order, 'running', '正在执行任务')
    _publish_task_status(task_order, task_uid)

    # 清理旧的任务日志，避免显示历史日志
    app.task_logs[task_order] = []
    _append_task_log(task_order, f'开始执行任务: {task_name}', task_uid=task_uid)
    
    # 立即返回响应，然后异步执行任务
    def execute_task_async():
        """异步执行任务"""
        try:
            _append_task_log(task_order, '任务线程已启动，正在准备执行...', task_uid=task_uid)
            
            # 重新获取最新的任务数据，优先使用稳定标识，避免任务重排后串任务
            latest_task = storage.resolve_task(task_uid, order=task_order, url=task_url)

            if not latest_task:
                logger.error(f'任务已不存在(order={task_order})')
                storage.update_task_status_by_order(task_order, 'error', '任务已不存在')
                _publish_task_status(task_order, task_uid)
                _append_task_log(task_order, '任务已不存在，执行失败', level='ERROR', task_uid=task_uid)
                _publish_task_completed(task_order, task_uid)
                return

            # 使用最新任务数据
            current_task = latest_task

            _append_task_log(
                task_order,
                f'开始处理任务: {current_task.get("name", "未命名任务")}',
                task_uid=task_uid
            )

            def progress_callback(status, message):
                """实时记录任务执行进度"""
                level = status.upper() if status in ['error', 'info', 'warning'] else 'INFO'
                _append_task_log(task_order, message, level=level, task_uid=task_uid)

                # 同时更新任务状态消息
                if status != 'error':
                    storage.update_task_status_by_order(task_order, 'running', message)
                    _publish_task_status(task_order, task_uid)

                # 记录到系统日志
                if status == 'error':
                    logger.error(f"[任务{task_order}] {message}")
                else:
                    logger.info(f"[任务{task_order}] {message}")

            result = storage.transfer_share(
                current_task['url'],
                current_task.get('pwd'),
                None,
                current_task.get('save_dir'),
                progress_callback,
                current_task,
                cancel_callback=lambda: _is_task_cancelled(task_order)
            )

            if result.get('cancelled'):
                storage.update_task_status_by_order(task_order, 'cancelled', '任务已取消')
                _publish_task_status(task_order, task_uid)
                _append_task_log(task_order, '任务已取消', level='WARNING', task_uid=task_uid)
                _publish_task_completed(task_order, task_uid)
            elif result.get('success'):
                transferred_files = result.get('transferred_files', [])
                if transferred_files:
                    task_results = {
                        'success': [current_task],
                        'failed': [],
                        'transferred_files': {current_task['url']: transferred_files}
                    }
                    
                    try:
                        # 发送转存成功通知
                        notify_send('百度自动追更', generate_transfer_notification(task_results))
                    except Exception as e:
                        logger.error(f"发送转存成功通知失败: {str(e)}")
                    
                    storage.update_task_status_by_order(
                        task_order,
                        'normal',
                        '转存成功',
                        transferred_files=transferred_files
                    )
                    _publish_task_status(task_order, task_uid)
                    _append_task_log(task_order, '任务执行完成', task_uid=task_uid)
                    _publish_task_completed(task_order, task_uid)
                else:
                    storage.update_task_status_by_order(task_order, 'normal', '没有新文件需要转存')
                    _publish_task_status(task_order, task_uid)
                    _append_task_log(task_order, '没有新文件需要转存', task_uid=task_uid)
                    _publish_task_completed(task_order, task_uid)
            else:
                error_msg = result.get('error', '转存失败')
                storage.update_task_status_by_order(task_order, 'error', error_msg)
                _publish_task_status(task_order, task_uid)
                _append_task_log(task_order, f'任务执行失败: {error_msg}', level='ERROR', task_uid=task_uid)
                _publish_task_completed(task_order, task_uid)

        except Exception as e:
            error_msg = str(e)
            # 使用存储模块的错误解析功能
            parsed_error = storage._parse_share_error(error_msg) if storage else error_msg
            
            is_share_forbidden = "error_code: 115" in error_msg
            
            if is_share_forbidden:
                try:
                    storage.remove_task_by_order(task_order)
                    storage._update_task_orders()
                except Exception as del_err:
                    pass  # 删除失效任务失败，继续执行
            
            storage.update_task_status_by_order(task_order, 'error', parsed_error)
            _publish_task_status(task_order, task_uid)
            _append_task_log(task_order, f'任务执行异常: {parsed_error}', level='ERROR', task_uid=task_uid)
            _publish_task_completed(task_order, task_uid)
        finally:
            _clear_task_cancel_flag(task_order)
            execution_lock.release()

    # 启动异步任务并立即返回
    thread = threading.Thread(target=execute_task_async)
    thread.daemon = True  # 设置为守护线程
    thread.start()
    
    # 立即返回响应，表示任务已开始执行
    return jsonify({
        'success': True,
        'message': '任务已开始执行',
        'task_uid': task_uid,
        'task_order': task_order,
        'stream_url': f'/api/task/stream/{task_uid}' if task_uid else None
    })


@app.route('/api/task/cancel', methods=['GET', 'POST'])
@login_required
@handle_api_error
def cancel_task():
    """取消正在执行的任务。"""
    logger.info(f"取消接口收到请求: method={request.method}, content_type={request.content_type}")

    data = request.get_json(silent=True) or {}
    if request.method == 'GET':
        data = {**request.args.to_dict(), **data}
    elif not data and request.form:
        data = request.form.to_dict()

    task_uid = data.get('task_uid')
    task_order = data.get('task_order')
    task_id = data.get('task_id')

    if task_order is not None:
        try:
            task_order = int(task_order)
        except (TypeError, ValueError):
            task_order = None

    if task_id is not None:
        try:
            task_id = int(task_id)
        except (TypeError, ValueError):
            task_id = None

    if not storage:
        return jsonify({'success': False, 'message': '存储未初始化'})

    task = storage.resolve_task(task_uid, order=task_order)
    if not task and task_id is not None:
        tasks = storage.list_tasks()
        task = _resolve_request_task(task_ref=task_uid, task_id=task_id, tasks=tasks)
    if not task:
        return jsonify({'success': False, 'message': '未找到任务'})

    task_order = task.get('order')
    task_uid = task.get('task_uid')
    if not task_order:
        return jsonify({'success': False, 'message': '任务order不存在'})

    if task.get('status') != 'running' and not _is_task_cancelled(task_order):
        return jsonify({'success': False, 'message': '当前任务未在执行'})

    logger.info(f"收到取消请求: order={task_order}, task_uid={task_uid}")
    _set_task_cancel_flag(task_order, True)
    storage.update_task_status_by_order(task_order, 'running', '正在取消...')
    _publish_task_status(task_order, task_uid)
    _append_task_log(task_order, '收到取消请求，正在停止当前任务...', level='WARNING', task_uid=task_uid)
    return jsonify({'success': True, 'message': '已发送取消请求'})

@app.route('/api/users', methods=['GET'])
@login_required
@handle_api_error
def get_users():
    """获取所有用户"""
    if not storage:
        return jsonify({'success': False, 'message': '存储未初始化'})
    
    users = storage.list_users()
    current_username = storage.config.get('baidu', {}).get('current_user')
    
    # 标记当前用户
    for user in users:
        user['is_current'] = user.get('username') == current_username
    
    return jsonify({
        'success': True, 
        'users': users,
        'current_user': current_username
    })

@app.route('/api/user/add', methods=['POST'])
@login_required
@handle_api_error
def add_user():
    """添加用户"""
    data = request.get_json()
    username = data.get('username', '').strip()
    cookies = data.get('cookies', '').strip()
    
    if not username or not cookies:
        return jsonify({'success': False, 'message': '用户名和cookies不能为空'})
        
    if storage.add_user_from_cookies(cookies, username):
        init_app()
        return jsonify({'success': True, 'message': '添加用户成功'})
    return jsonify({'success': False, 'message': '添加用户失败'})

@app.route('/api/user/switch', methods=['POST'])
@login_required
@handle_api_error
def switch_user():
    """切换用户"""
    data = request.get_json()
    username = data.get('username')
    
    if not username:
        return jsonify({'success': False, 'message': '用户名不能为空'})
        
    try:
        if storage.switch_user(username):
            # 获取完整的用户信息
            user = storage.get_user(username)
            if not user:
                return jsonify({'success': False, 'message': f'用户 {username} 不存在'})
            
            # 重新初始化应用
            init_app()
            
            # 切换用户后立即获取用户配额信息
            try:
                if storage and hasattr(storage, 'get_user_info'):
                    user_info = storage.get_user_info()
                    if user_info and 'quota' in user_info:
                        quota = user_info['quota']
                        total_gb = round(quota.get('total', 0) / (1024**3), 2)
                        used_gb = round(quota.get('used', 0) / (1024**3), 2)
                        logger.info(f"已切换到用户: {username}，网盘总空间: {total_gb}GB, 已使用: {used_gb}GB")
                        
                        # 将配额信息添加到用户数据中
                        user['quota'] = {
                            'total': quota.get('total', 0),
                            'used': quota.get('used', 0),
                            'total_gb': total_gb,
                            'used_gb': used_gb,
                            'percent': round(quota.get('used', 0) / quota.get('total', 1) * 100, 2) if quota.get('total', 0) > 0 else 0
                        }
            except Exception as e:
                logger.error(f"切换用户后获取配额信息失败: {str(e)}")
            
            # 返回更新后的状态
            return jsonify({
                'success': True, 
                'message': '切换用户成功',
                'current_user': user,
                'login_status': storage.is_valid()
            })
        return jsonify({'success': False, 'message': '切换用户失败'})
    except Exception as e:
        logger.error(f"切换用户失败: {str(e)}")
        return jsonify({'success': False, 'message': f'切换用户失败: {str(e)}'})

@app.route('/api/user/delete', methods=['POST'])
@login_required
@handle_api_error
def delete_user():
    """删除用户"""
    data = request.get_json()
    username = data.get('username')
    
    if not username:
        return jsonify({'success': False, 'message': '用户名不能为空'})
        
    current_user = storage.config['baidu'].get('current_user')
    if current_user == username:
        return jsonify({'success': False, 'message': '不能删除当前使用的用户'})
        
    if storage.remove_user(username):
        return jsonify({'success': True, 'message': '删除用户成功'})
    return jsonify({'success': False, 'message': '删除用户失败'})

@app.route('/api/user/update', methods=['POST'])
@login_required
@handle_api_error
def update_user():
    """更新用户信息"""
    data = request.get_json()
    original_username = data.get('original_username', '').strip()
    username = data.get('username', '').strip()
    cookies = data.get('cookies', '').strip()
    
    if not original_username or not username or not cookies:
        return jsonify({'success': False, 'message': '原始用户名、新用户名和cookies不能为空'})
    
    # 如果是重命名用户
    if original_username != username:
        # 检查新用户名是否已存在
        if username in storage.config['baidu']['users']:
            return jsonify({'success': False, 'message': f'用户名 {username} 已存在'})
        
        # 获取原用户信息
        user_info = storage.get_user(original_username)
        if not user_info:
            return jsonify({'success': False, 'message': f'用户 {original_username} 不存在'})
        
        # 检查cookies是否发生变化
        cookies_changed = user_info.get('cookies', '') != cookies
        
        # 如果仅重命名，无需验证cookies
        if not cookies_changed:
            # 复制用户信息到新用户名
            storage.config['baidu']['users'][username] = storage.config['baidu']['users'][original_username].copy()
            
            # 如果是当前用户，更新当前用户名
            if storage.config['baidu']['current_user'] == original_username:
                storage.config['baidu']['current_user'] = username
            
            # 删除原用户
            storage.remove_user(original_username)
            
            # 保存配置
            storage._save_config()
            
            return jsonify({'success': True, 'message': '用户更新成功'})
        else:
            # 创建新用户
            if storage.add_user_from_cookies(cookies, username):
                # 如果是当前用户，更新当前用户名
                if storage.config['baidu']['current_user'] == original_username:
                    storage.switch_user(username)
                
                # 删除原用户
                storage.remove_user(original_username)
                
                return jsonify({'success': True, 'message': '用户更新成功'})
            else:
                return jsonify({'success': False, 'message': '用户更新失败，cookies可能无效'})
    else:
        # 仅更新cookies
        if storage.update_user(username, cookies):
            init_app()
            return jsonify({'success': True, 'message': '用户更新成功'})
        return jsonify({'success': False, 'message': '用户更新失败，cookies可能无效'})

@app.route('/api/user/<username>/cookies', methods=['GET'])
@login_required
@handle_api_error
def get_user_cookies(username):
    """获取用户cookies"""
    user_info = storage.get_user(username)
    if not user_info:
        return jsonify({'success': False, 'message': f'用户 {username} 不存在'})
    
    return jsonify({'success': True, 'cookies': user_info.get('cookies', '')})

@app.route('/api/user/quota', methods=['GET'])
@login_required
@handle_api_error
def get_user_quota():
    """获取当前用户的网盘配额信息"""
    if not storage:
        return jsonify({'success': False, 'message': '存储未初始化'})
        
    try:
        # 获取用户信息，包括配额
        user_info = storage.get_user_info()
        if not user_info or 'quota' not in user_info:
            return jsonify({'success': False, 'message': '无法获取用户配额信息'})
            
        # 提取配额信息
        quota = user_info['quota']
        total = quota.get('total', 0)
        used = quota.get('used', 0)
        
        # 转换为GB并保留2位小数
        total_gb = round(total / (1024**3), 2)
        used_gb = round(used / (1024**3), 2)
        
        return jsonify({
            'success': True, 
            'quota': {
                'total': total,
                'used': used,
                'total_gb': total_gb,
                'used_gb': used_gb,
                'percent': round(used / total * 100, 2) if total > 0 else 0
            }
        })
    except Exception as e:
        logger.error(f"获取用户配额失败: {str(e)}")
        return jsonify({'success': False, 'message': f'获取用户配额失败: {str(e)}'})

@app.route('/api/config', methods=['GET'])
@login_required
@handle_api_error
def get_config():
    """获取配置"""
    if not storage:
        return jsonify({'success': False, 'message': '存储未初始化'})
    
    # 获取当前用户的完整信息
    current_user = None
    current_username = storage.config.get('baidu', {}).get('current_user')
    if current_username:
        current_user = storage.get_user(current_username)
    
    auth_config = dict(storage.config.get('auth', {}))
    auth_config.pop('password', None)

    config = {
        'cron': storage.config.get('cron', {}),
        'notify': storage.config.get('notify', {}),
        'scheduler': storage.config.get('scheduler', {}),
        'quota_alert': storage.config.get('quota_alert', {}),
        'share': storage.config.get('share', {}),
        'file_operations': storage.config.get('file_operations', {}),
        'auth': auth_config,
        'baidu': {
            'current_user': current_user  # 返回完整的用户信息
        }
    }
    return jsonify({'success': True, 'config': config})

def format_webhook_body(webhook_body):
    """格式化WEBHOOK_BODY字段，将简化格式转换为标准多行格式"""
    if not webhook_body or isinstance(webhook_body, dict):
        return webhook_body
    
    # 检测是否是简化格式（如：title: "$title"content: "$content"source: "我的项目"）
    import re
    simple_format = re.match(r'title:\s*"([^"]*)"content:\s*"([^"]*)"source:\s*"([^"]*)"', webhook_body)
    
    if simple_format:
        # 转换为标准多行格式
        title = simple_format.group(1)
        content = simple_format.group(2)
        source = simple_format.group(3)
        return f'title: "{title}"\ncontent: "{content}"\nsource: "{source}"'
    
    # 如果不是简化格式，直接返回原始值
    return webhook_body

@app.route('/api/config/update', methods=['POST'])
@login_required
@handle_api_error
def update_config():
    """更新配置"""
    if not storage:
        return jsonify({'success': False, 'message': '存储未初始化'})
        
    data = request.get_json()
    
    # 处理通知配置：完全替换notify配置，清除旧字段
    if 'notify' in data:
        # 格式化WEBHOOK_BODY字段
        if 'direct_fields' in data['notify'] and 'WEBHOOK_BODY' in data['notify']['direct_fields']:
            data['notify']['direct_fields']['WEBHOOK_BODY'] = format_webhook_body(data['notify']['direct_fields']['WEBHOOK_BODY'])
        
        # 完全替换整个notify对象，而不是合并
        # 这样可以清除旧的字段（push_plus_token、webhook_url等）
        storage.config['notify'] = {
            'enabled': data['notify'].get('enabled', False),
            'notification_delay': data['notify'].get('notification_delay', 30),
            'direct_fields': data['notify'].get('direct_fields', {})
        }
        
        # 从data中移除notify，避免后续update重复处理
        del data['notify']
    
    # 处理认证配置：保留现有密码，避免前端读取或误覆盖
    if 'auth' in data:
        current_auth = dict(storage.config.get('auth', {}))
        incoming_auth = data.get('auth') or {}
        merged_auth = {
            'users': incoming_auth.get('users', current_auth.get('users', '')),
            'session_timeout': incoming_auth.get('session_timeout', current_auth.get('session_timeout', 3600))
        }

        new_password = incoming_auth.get('password')
        if isinstance(new_password, str) and new_password.strip():
            merged_auth['password'] = new_password
        elif 'password' in current_auth:
            merged_auth['password'] = current_auth['password']

        storage.config['auth'] = merged_auth
        del data['auth']

    # 更新其他配置
    storage.config.update(data)
    storage._save_config()
    
    # 处理调度器配置更新
    if scheduler and ('cron' in data or 'scheduler' in data):
        try:
            was_running = scheduler.is_running
            if was_running:
                scheduler.stop()
                logger.info('调度器已停止')
            
            # 重新初始化调度器
            scheduler._init_scheduler()
            
            # 如果之前在运行，或者配置中指定了自动启动，则启动调度器
            should_start = was_running or data.get('cron', {}).get('auto_install', True)
            if should_start and not scheduler.is_running:
                scheduler.start()
                logger.info('调度器已重新启动')
            
            logger.info('调度器配置已更新')
        except Exception as e:
            logger.error(f'更新调度器配置失败: {str(e)}')
            return jsonify({
                'success': False,
                'message': f'配置已保存，但更新调度器失败: {str(e)}'
            })
    
    # 处理通知配置更新
    if scheduler and 'notify' in data:
        try:
            # 重新初始化通知配置
            scheduler._init_notify()
            logger.info('通知配置已更新')
        except Exception as e:
            logger.error(f'更新通知配置失败: {str(e)}')
            return jsonify({
                'success': False,
                'message': f'配置已保存，但更新通知配置失败: {str(e)}'
            })
    
    return jsonify({'success': True, 'message': '更新配置成功'})

@app.route('/api/notify/test', methods=['POST'])
@login_required
@handle_api_error
def test_notify():
    """测试通知功能"""
    if not storage or not storage.config.get('notify', {}).get('enabled'):
        return jsonify({'success': False, 'message': '通知功能未启用'})
        
    try:
        # 确保通知配置正确加载
        notify_config = storage.config.get('notify', {})
        if notify_config and notify_config.get('enabled'):
            # 重新应用通知配置
            from backend.notify import push_config, send as notify_send
            
            # 应用直接字段配置
            if 'direct_fields' in notify_config:
                for key, value in notify_config.get('direct_fields', {}).items():
                    push_config[key] = value
            # 兼容旧版配置
            elif 'channels' in notify_config and 'pushplus' in notify_config['channels']:
                pushplus = notify_config['channels']['pushplus']
                if 'token' in pushplus:
                    push_config['PUSH_PLUS_TOKEN'] = pushplus['token']
                if 'topic' in pushplus:
                    push_config['PUSH_PLUS_USER'] = pushplus['topic']
            
            # 应用自定义字段
            if 'custom_fields' in notify_config:
                for key, value in notify_config.get('custom_fields', {}).items():
                    push_config[key] = value
            
            # 使用时间戳确保每次内容不同，避免重复内容限制
            import time
            timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
            
            # 使用notify_send发送通知
            notify_send('百度网盘自动追更', f'这是一条测试通知,如果你收到了这条消息,说明通知配置正确! 测试时间: {timestamp}')
            
            return jsonify({'success': True, 'message': '测试通知已发送'})
        else:
            return jsonify({'success': False, 'message': '通知功能未启用'})
    except Exception as e:
        logger.error(f"发送测试通知失败: {str(e)}")
        return jsonify({'success': False, 'message': f'发送测试通知失败: {str(e)}'})

@app.route('/api/tasks/execute-all', methods=['POST'])
@login_required
@handle_api_error
def execute_all_tasks():
    """批量执行任务"""
    if not storage:
        return jsonify({'success': False, 'message': '存储未初始化'})
        
    data = request.get_json() or {}
    task_ids = data.get('task_ids', [])
    task_uids = data.get('task_uids', [])

    if not task_ids and not task_uids:
        return jsonify({'success': False, 'message': '没有指定要执行的任务'})

    # 获取并按order排序的任务列表
    tasks = storage.list_tasks()
    if not tasks:
        return jsonify({'success': False, 'message': '任务列表为空'})

    tasks = sorted(tasks, key=lambda x: x.get('order', float('inf')))

    selected_tasks = []
    seen_uids = set()

    for task_uid in task_uids:
        task = _resolve_request_task(task_ref=task_uid, tasks=tasks)
        if task and task.get('task_uid') not in seen_uids:
            selected_tasks.append(task)
            if task.get('task_uid'):
                seen_uids.add(task.get('task_uid'))

    for raw_task_id in task_ids:
        try:
            task_id = int(raw_task_id)
        except (TypeError, ValueError):
            continue

        task = _resolve_request_task(task_id=task_id, tasks=tasks)
        if not task:
            continue

        task_uid = task.get('task_uid')
        if task_uid and task_uid in seen_uids:
            continue

        selected_tasks.append(task)
        if task_uid:
            seen_uids.add(task_uid)

    if not selected_tasks:
        return jsonify({'success': False, 'message': '未找到指定的任务'})

    execution_lock = _get_execution_lock()
    if not execution_lock.acquire(blocking=False):
        return jsonify({'success': False, 'message': '当前已有任务正在执行，请稍后再试'})

    def execute_batch_async():
        results = {
            'success': [],
            'skipped': [],
            'failed': [],
            'cancelled': [],
            'transferred_files': {}
        }

        try:
            for selected_task in selected_tasks:
                task = storage.resolve_task(selected_task)
                if not task:
                    task = storage.resolve_task(
                        task_uid=selected_task.get('task_uid'),
                        order=selected_task.get('order'),
                        url=selected_task.get('url')
                    )

                if not task:
                    logger.warning(f"批量执行时任务已不存在: {selected_task}")
                    results['failed'].append(selected_task)
                    continue

                task_order = task.get('order')
                if not task_order:
                    continue

                _clear_task_cancel_flag(task_order)
                storage.update_task_status_by_order(task_order, 'running', '批量执行中')

                try:
                    result = storage.transfer_share(
                        task['url'],
                        task.get('pwd'),
                        None,
                        task.get('save_dir'),
                        None,
                        task,
                        cancel_callback=lambda current_order=task_order: _is_task_cancelled(current_order)
                    )

                    if result.get('cancelled'):
                        results['cancelled'].append(task)
                        storage.update_task_status_by_order(task_order, 'cancelled', '任务已取消')
                    elif result.get('success'):
                        if result.get('skipped'):
                            results['skipped'].append(task)
                            storage.update_task_status_by_order(task_order, 'skipped', '没有新文件需要转存')
                        else:
                            transferred_files = result.get('transferred_files', [])
                            if transferred_files:
                                results['success'].append(task)
                                results['transferred_files'][task['url']] = transferred_files
                                storage.update_task_status_by_order(
                                    task_order,
                                    'success',
                                    '转存成功',
                                    transferred_files=transferred_files
                                )
                            else:
                                results['skipped'].append(task)
                                storage.update_task_status_by_order(task_order, 'skipped', '没有新文件需要转存')
                    else:
                        error_msg = result.get('error', '转存失败')
                        results['failed'].append(task)
                        storage.update_task_status_by_order(task_order, 'failed', error_msg)

                except Exception as e:
                    error_msg = str(e)
                    if "error_code: 115" in error_msg:
                        error_msg = "该分享链接已失效（文件禁止分享）"
                        try:
                            storage.remove_task_by_order(task_order)
                        except Exception as del_err:
                            logger.error(f"删除失效任务失败: {str(del_err)}")
                    results['failed'].append(task)
                    storage.update_task_status_by_order(task_order, 'failed', error_msg)
                finally:
                    _clear_task_cancel_flag(task_order)

            if results['success'] or results['failed']:
                try:
                    notification_content = generate_transfer_notification(results)
                    notify_send("百度网盘自动追更", notification_content)
                except Exception as e:
                    logger.error(f"发送通知失败: {str(e)}")

            logger.info(
                f"批量执行完成，成功: {len(results['success'])}，跳过: {len(results['skipped'])}，"
                f"取消: {len(results['cancelled'])}，失败: {len(results['failed'])}"
            )
        finally:
            execution_lock.release()

    thread = threading.Thread(target=execute_batch_async, daemon=True)
    thread.start()

    return jsonify({
        'success': True,
        'message': f'批量任务已开始执行，共 {len(selected_tasks)} 个任务'
    })

@app.route('/api/categories', methods=['GET'])
@login_required
@handle_api_error
def get_categories():
    """获取所有任务分类"""
    if not storage:
        return jsonify({'success': False, 'message': '存储未初始化'})
    categories = storage.get_task_categories()
    return jsonify({'success': True, 'categories': categories})

@app.route('/api/tasks/category/<category>', methods=['GET'])
@login_required
@handle_api_error
def get_tasks_by_category(category):
    """获取指定分类的任务"""
    if not storage:
        return jsonify({'success': False, 'message': '存储未初始化'})
        
    if category == 'uncategorized':
        tasks = storage.get_tasks_by_category(None)
    else:
        tasks = storage.get_tasks_by_category(category)
    
    # 按 order 排序
    tasks.sort(key=lambda x: x.get('order', float('inf')))
    return jsonify({'success': True, 'tasks': tasks})

@app.route('/api/notify/fields', methods=['POST'])
@login_required
@handle_api_error
def add_notify_field():
    """添加自定义通知字段"""
    if not storage:
        return jsonify({'success': False, 'message': '存储未初始化'})
        
    data = request.get_json()
    field_name = data.get('name', '').strip()
    field_value = data.get('value', '').strip()
    
    if not field_name:
        return jsonify({'success': False, 'message': '字段名称不能为空'})
    
    # 自动格式化WEBHOOK_BODY字段
    if field_name == 'WEBHOOK_BODY':
        field_value = format_webhook_body(field_value)
        
    notify_config = storage.config.get('notify', {})
    if 'custom_fields' not in notify_config:
        notify_config['custom_fields'] = {}
        
    notify_config['custom_fields'][field_name] = field_value
    storage.config['notify'] = notify_config
    storage._save_config()
    
    return jsonify({'success': True, 'message': '添加通知字段成功'})

@app.route('/api/notify/fields', methods=['DELETE'])
@login_required
@handle_api_error
def delete_notify_field():
    """删除通知字段"""
    if not storage:
        return jsonify({'success': False, 'message': '存储未初始化'})
        
    data = request.get_json()
    field_name = data.get('name', '').strip()
    
    if not field_name:
        return jsonify({'success': False, 'message': '字段名称不能为空'})
        
    notify_config = storage.config.get('notify', {})
    
    # 检查字段在哪个配置中
    field_deleted = False
    
    # 1. 检查direct_fields
    if 'direct_fields' in notify_config and field_name in notify_config['direct_fields']:
        del notify_config['direct_fields'][field_name]
        field_deleted = True
    
    # 2. 检查custom_fields (兼容旧版本)
    if not field_deleted and 'custom_fields' in notify_config and field_name in notify_config['custom_fields']:
        del notify_config['custom_fields'][field_name]
        field_deleted = True
    
    if not field_deleted:
        return jsonify({'success': False, 'message': f'未找到字段: {field_name}'})
    
    storage.config['notify'] = notify_config
    storage._save_config()
    
    # 重新初始化通知配置
    if scheduler:
        scheduler._init_notify()
    
    return jsonify({'success': True, 'message': f'字段 {field_name} 已删除'})

@app.route('/api/task/reorder', methods=['POST'])
@login_required
@handle_api_error
def reorder_task():
    """重新排序任务"""
    if not storage:
        return jsonify({'success': False, 'message': '存储未初始化'})
        
    data = request.get_json()
    task_id = data.get('task_id')
    new_order = data.get('new_order')
    
    if task_id is None or new_order is None:
        return jsonify({'success': False, 'message': '任务ID和新顺序不能为空'})
    
    # 将task_id转换为order
    task_order = task_id + 1
        
    if storage.reorder_task(task_order, new_order):
        return jsonify({'success': True, 'message': '任务重排序成功'})
    return jsonify({'success': False, 'message': '任务重排序失败'})


@app.route('/api/local-sync/status', methods=['GET'])
@login_required
@handle_api_error
def get_local_sync_status():
    """获取本地同步任务的状态信息。"""
    return jsonify({
        'success': True,
        'tasks': _load_bypy_sync_task_names(),
        'incremental': _build_incremental_sync_status(),
        'full': _build_full_sync_status(),
    })


@app.route('/api/local-sync/tasks', methods=['GET'])
@login_required
@handle_api_error
def get_local_sync_tasks():
    """获取本地同步任务配置。"""
    return jsonify({
        'success': True,
        'tasks': _serialize_local_sync_tasks(_load_bypy_sync_tasks(include_disabled=True)),
    })


@app.route('/api/local-sync/tasks/save', methods=['POST'])
@login_required
@handle_api_error
def save_local_sync_task():
    """新增或更新本地同步任务配置。"""
    data = request.get_json(silent=True) or {}
    task_id = str(data.get('task_id') or '').strip()
    name = str(data.get('name') or '').strip()
    remote_root = str(data.get('remote_root') or '').replace('\\', '/').strip()
    local_root = str(data.get('local_root') or '').strip()
    enabled = bool(data.get('enabled', True))
    auto_run_enabled = bool(data.get('auto_run_enabled', False))
    cron = _normalize_local_sync_cron(data.get('cron'))
    sync_mode = _normalize_local_sync_mode(data.get('sync_mode'), data.get('directory_filters'))
    directory_filters = _normalize_directory_filters(data.get('directory_filters')) if sync_mode == 'manual' else []
    recent_value = _normalize_local_sync_recent_value(data.get('recent_value'), sync_mode)
    overwrite_policy = _normalize_local_sync_overwrite_policy(data.get('overwrite_policy'))

    if not name:
        return jsonify({'success': False, 'message': '任务名称不能为空'})
    if not remote_root:
        return jsonify({'success': False, 'message': '远程根目录不能为空'})
    if not local_root:
        return jsonify({'success': False, 'message': '本地根目录不能为空'})
    if sync_mode == 'manual' and not directory_filters:
        return jsonify({'success': False, 'message': '手动选择子目录时，至少选择一个子目录'})
    if sync_mode in {'recent_days', 'recent_months'} and recent_value <= 0:
        return jsonify({'success': False, 'message': '最近天数或月数必须大于 0'})
    if auto_run_enabled and not cron:
        return jsonify({'success': False, 'message': '启用自动运行时，crontab 表达式不能为空'})
    if cron:
        try:
            CronTrigger.from_crontab(convert_cron_weekday(cron), timezone=pytz.timezone('Asia/Shanghai'))
        except Exception as e:
            return jsonify({'success': False, 'message': f'crontab 表达式无效: {str(e)}'})

    tasks = _load_bypy_sync_tasks(include_disabled=True)
    duplicate = next((item for item in tasks if item.get('name') == name and item.get('task_id') != task_id), None)
    if duplicate:
        return jsonify({'success': False, 'message': '任务名称已存在，请使用不同名称'})

    payload = {
        'task_id': task_id or uuid4().hex,
        'name': name,
        'enabled': enabled,
        'auto_run_enabled': auto_run_enabled,
        'cron': cron,
        'remote_root': remote_root,
        'local_root': local_root,
        'directory_filters': directory_filters,
        'sync_mode': sync_mode,
        'recent_value': recent_value,
        'overwrite_policy': overwrite_policy,
    }

    updated = False
    for index, item in enumerate(tasks):
        if item.get('task_id') == payload['task_id']:
            tasks[index] = payload
            updated = True
            break

    if not updated:
        tasks.append(payload)

    saved_tasks = _save_bypy_sync_tasks(tasks)
    if local_sync_scheduler:
        local_sync_scheduler.sync_jobs()
    serialized_tasks = _serialize_local_sync_tasks(saved_tasks)
    saved_task = next((item for item in serialized_tasks if item.get('task_id') == payload['task_id']), payload)
    return jsonify({
        'success': True,
        'message': '本地同步任务已保存',
        'task': saved_task,
        'tasks': serialized_tasks,
    })


@app.route('/api/local-sync/tasks/delete', methods=['POST'])
@login_required
@handle_api_error
def delete_local_sync_task():
    """删除本地同步任务配置。"""
    data = request.get_json(silent=True) or {}
    task_id = str(data.get('task_id') or '').strip()
    if not task_id:
        return jsonify({'success': False, 'message': 'task_id 不能为空'})

    tasks = _load_bypy_sync_tasks(include_disabled=True)
    filtered_tasks = [item for item in tasks if item.get('task_id') != task_id]
    if len(filtered_tasks) == len(tasks):
        return jsonify({'success': False, 'message': '未找到对应的本地同步任务'})

    saved_tasks = _save_bypy_sync_tasks(filtered_tasks)
    if local_sync_scheduler:
        local_sync_scheduler.sync_jobs()
    return jsonify({
        'success': True,
        'message': '本地同步任务已删除',
        'tasks': _serialize_local_sync_tasks(saved_tasks),
    })


@app.route('/api/local-sync/tasks/run', methods=['POST'])
@login_required
@handle_api_error
def run_local_sync_task():
    """手动执行单个本地同步任务。"""
    data = request.get_json(silent=True) or {}
    task_id = str(data.get('task_id') or '').strip()
    dry_run = bool(data.get('dry_run', False))

    if not task_id:
        return jsonify({'success': False, 'message': 'task_id 不能为空'})

    success, message, status = _run_local_sync_task(
        task_id=task_id,
        dry_run=dry_run,
        include_disabled=True,
        background=True,
        trigger_source='manual',
    )
    return jsonify({
        'success': success,
        'message': message,
        'status': status,
    })


@app.route('/api/local-sync/directories', methods=['GET'])
@login_required
@handle_api_error
def get_local_sync_directories():
    """列出指定本地同步任务可选的远程一级目录。"""
    task_id = str(request.args.get('task_id') or '').strip()
    remote_root = str(request.args.get('remote_root') or '').strip()

    task = _find_bypy_sync_task(task_id=task_id) if task_id else None
    if task and not remote_root:
        remote_root = task.get('remote_root') or ''

    if not remote_root:
        return jsonify({'success': False, 'message': 'remote_root 不能为空'})

    directories = _list_bypy_sync_directories(remote_root)
    return jsonify({
        'success': True,
        'remote_root': remote_root,
        'directories': directories,
    })


@app.route('/api/local-sync/start', methods=['POST'])
@login_required
@handle_api_error
def start_local_sync():
    """从平台内启动本地同步任务。"""
    data = request.get_json(silent=True) or {}
    sync_type = str(data.get('sync_type') or 'incremental').strip()
    dry_run = bool(data.get('dry_run', False))
    task_filters = _normalize_task_filters(data.get('tasks'))

    if sync_type == 'incremental':
        success, message, status = _start_incremental_sync(dry_run=dry_run, task_filters=task_filters)
        return jsonify({
            'success': success,
            'message': message,
            'status': status,
        })

    if sync_type == 'full':
        success, output = _run_full_sync_manager('start', dry_run=dry_run, task_filters=task_filters)
        return jsonify({
            'success': success,
            'message': output or ('全量补缺同步已启动' if success else '全量补缺同步启动失败'),
            'status': _build_full_sync_status(),
        })

    return jsonify({'success': False, 'message': '不支持的同步类型'})


@app.route('/api/local-sync/stop', methods=['POST'])
@login_required
@handle_api_error
def stop_local_sync():
    """停止平台内启动的本地同步任务。"""
    data = request.get_json(silent=True) or {}
    sync_type = str(data.get('sync_type') or 'incremental').strip()

    if sync_type == 'incremental':
        success, message, status = _stop_incremental_sync()
        return jsonify({
            'success': success,
            'message': message,
            'status': status,
        })

    if sync_type == 'full':
        success, output = _run_full_sync_manager('stop')
        return jsonify({
            'success': success,
            'message': output or ('全量补缺同步已停止' if success else '全量补缺同步停止失败'),
            'status': _build_full_sync_status(),
        })

    return jsonify({'success': False, 'message': '不支持的同步类型'})


@app.route('/api/local-sync/logs', methods=['GET'])
@login_required
@handle_api_error
def get_local_sync_logs():
    """读取本地同步任务最近日志。"""
    sync_type = str(request.args.get('sync_type') or 'incremental').strip()
    lines = request.args.get('lines', default=120, type=int)
    lines = max(20, min(lines, 300))

    if sync_type == 'incremental':
        status = _build_incremental_sync_status()
    elif sync_type == 'full':
        status = _build_full_sync_status()
    else:
        return jsonify({'success': False, 'message': '不支持的同步类型'})

    log_text = _tail_log_file(status.get('log_file'), lines=lines)
    return jsonify({
        'success': True,
        'sync_type': sync_type,
        'log_file': status.get('log_file', ''),
        'logs': log_text,
    })


@app.route('/api/local-sync/tasks/logs', methods=['GET'])
@login_required
@handle_api_error
def get_local_sync_task_logs():
    """读取指定本地同步任务最近一次执行日志。"""
    task_id = str(request.args.get('task_id') or '').strip()
    lines = request.args.get('lines', default=200, type=int)
    lines = max(20, min(lines, 500))

    if not task_id:
        return jsonify({'success': False, 'message': 'task_id 不能为空'})

    task = _find_bypy_sync_task(task_id=task_id)
    if not task:
        return jsonify({'success': False, 'message': '未找到对应的本地同步任务'})

    log_file, log_text = _find_local_sync_task_log(task.get('name', ''), lines=lines)
    running_status = _build_incremental_sync_status()
    is_running = task.get('name') in _normalize_task_filters(running_status.get('tasks')) and running_status.get('running', False)

    return jsonify({
        'success': True,
        'task_id': task_id,
        'task_name': task.get('name', ''),
        'running': is_running,
        'log_file': log_file,
        'logs': log_text,
        'message': '' if log_text else '暂未找到该任务的执行日志',
    })


# 静态文件路由
@app.route('/static/<path:path>')
def send_static(path):
    return send_from_directory('static', path)

# 前端资源路由 - 直接从static目录提供
@app.route('/assets/<path:path>')
def send_assets(path):
    return send_from_directory('static/assets', path)

@app.route('/favicon/<path:path>')
def send_favicon(path):
    return send_from_directory('static/favicon', path)

# SPA路由支持 - 捕获所有前端路由
@app.route('/<path:path>')
@login_required
def spa_routes(path):
    """SPA前端路由支持 - 将所有未匹配的路由返回index.html"""
    # 排除API路由、登录登出路由、静态资源等
    if path.startswith(('api/', 'login', 'logout', 'static/', 'assets/', 'favicon/')):
        return jsonify({'success': False, 'message': '接口不存在'}), 404
    
    # 返回SPA的index.html，让Vue Router处理路由
    return send_from_directory('static', 'index.html')

# 错误处理
@app.errorhandler(404)
def not_found(error):
    return jsonify({'success': False, 'message': '接口不存在'}), 404

@app.errorhandler(405)
def method_not_allowed(error):
    return jsonify({'success': False, 'message': '请求方法不允许'}), 405

@app.errorhandler(500)
def internal_error(error):
    return jsonify({'success': False, 'message': '服务器内部错误'}), 500

def signal_handler(signum, frame):
    """处理退出信号"""
    logger.info("接收到退出信号，正在清理...")
    try:
        cleanup()
    except Exception as e:
        logger.error(f"清理过程出错: {str(e)}")
    finally:
        logger.info("清理完成，正在退出...")
        sys.exit(0)

@app.route('/api/scheduler/reload', methods=['POST'])
@login_required
def reload_scheduler():
    """重新加载调度器，使配置更改生效"""
    try:
        was_running = scheduler.is_running
        if was_running:
            scheduler.stop()
            logger.info('调度器已停止')
        
        # 重新初始化调度器
        scheduler._init_scheduler()
        
        # 如果之前在运行，则重新启动
        if was_running and not scheduler.is_running:
            scheduler.start()
            logger.info('调度器已重新启动')
        
        logger.info('调度器已重新加载')
        return jsonify({
            'success': True,
            'message': '调度器已重新加载'
        })
    except Exception as e:
        logger.error(f'重新加载调度器失败: {str(e)}')
        return jsonify({
            'success': False,
            'message': f'重新加载调度器失败: {str(e)}'
        }), 500

@app.route('/api/tasks/batch-delete', methods=['POST'])
@login_required
@handle_api_error
def batch_delete_tasks():
    """批量删除任务"""
    data = request.get_json()
    task_ids = data.get('task_ids', [])
    
    if not storage:
        return jsonify({'success': False, 'message': '存储未初始化'})
        
    if not task_ids:
        return jsonify({'success': False, 'message': '没有指定要删除的任务'})
    
    try:
        # 将task_ids转换为orders（task_id + 1）
        task_orders = [task_id + 1 for task_id in task_ids]
        
        # 批量删除任务
        deleted_count = storage.remove_tasks(task_orders)
        
        if deleted_count > 0:
            return jsonify({
                'success': True,
                'message': f'成功删除{deleted_count}个任务'
            })
        else:
            return jsonify({
                'success': False,
                'message': '没有任务被删除'
            })
            
    except Exception as e:
        error_msg = str(e)
        logger.error(f"批量删除任务失败: {error_msg}")
        return jsonify({
            'success': False,
            'message': f'批量删除任务失败: {error_msg}'
        })

@app.route('/api/auth/login', methods=['POST'])
@handle_api_error
def api_login():
    """API登录接口"""
    username = request.json.get('username') if request.is_json else request.form.get('username')
    password = request.json.get('password') if request.is_json else request.form.get('password')
    
    if not storage:
        return jsonify({'success': False, 'message': '系统未初始化'}), 400
        
    # 验证用户名和密码
    auth_config = storage.config.get('auth', {})
    if (username == auth_config.get('users') and 
        password == auth_config.get('password')):
        session['username'] = username
        session['login_time'] = time.time()
        
        return jsonify({
            'success': True, 
            'message': '登录成功',
            'username': username
        })
    else:
        return jsonify({'success': False, 'message': '用户名或密码错误'}), 401

@app.route('/api/auth/logout', methods=['POST'])
@handle_api_error
def api_logout():
    """API登出接口"""
    session.clear()
    return jsonify({'success': True, 'message': '登出成功'})

@app.route('/api/auth/check', methods=['GET'])
@handle_api_error
def api_check_auth():
    """检查认证状态"""
    if 'username' not in session:
        return jsonify({'success': False, 'message': '未登录'}), 401
        
    # 检查会话是否过期
    auth_config = storage.config.get('auth', {}) if storage else {}
    session_timeout = auth_config.get('session_timeout', 3600)
    if time.time() - session.get('login_time', 0) > session_timeout:
        session.clear()
        return jsonify({'success': False, 'message': '会话已过期'}), 401
        
    return jsonify({
        'success': True, 
        'message': '认证有效',
        'username': session['username']
    })

@app.route('/api/auth/update', methods=['POST'])
@login_required
@handle_api_error
def update_auth():
    """更新登录凭据"""
    if not storage:
        return jsonify({'success': False, 'message': '存储未初始化'})
        
    data = request.get_json()
    new_username = data.get('username', '').strip()
    new_password = data.get('password', '').strip()
    old_password = data.get('old_password', '').strip()
    
    if not new_username or not new_password or not old_password:
        return jsonify({'success': False, 'message': '用户名、新密码和旧密码都不能为空'})
    
    # 验证旧密码
    auth_config = storage.config.get('auth', {})
    if old_password != auth_config.get('password'):
        return jsonify({'success': False, 'message': '旧密码错误'})
    
    # 更新配置
    auth_config['users'] = new_username
    auth_config['password'] = new_password
    storage.config['auth'] = auth_config
    storage._save_config()
    
    return jsonify({'success': True, 'message': '登录凭据更新成功'})

@app.route('/api/version/check', methods=['GET'])
@handle_api_error
def check_version():
    """检查最新版本"""
    try:
        import feedparser
        import requests
        from requests.exceptions import RequestException
        import re
        
        # 获取查询参数，确定使用哪个源检查更新
        source = request.args.get('source', 'github')
        
        if source == 'dockerhub':
            # 使用 Docker Hub RSS 检查更新
            feed_url = DOCKER_HUB_RSS
        elif source == 'dockerhub_alt':
            # 使用备用 Docker Hub RSS 源
            feed_url = DOCKER_HUB_RSS_ALT
        elif source in ['msrun', '1ms']:
            # 使用 1ms.run API 获取版本信息
            try:
                params = {
                    "repositories": "kokojacket/baidu-autosave",
                    "page": 1,
                    "page_size": 10,
                    "search": ""
                }
                response = requests.get(MS_RUN_API, params=params, timeout=5)
                response.raise_for_status()
                data = response.json()
                
                if data.get('code') == 0 and data.get('data', {}).get('list'):
                    # 查找最新的正式版本（格式为vX.Y.Z）
                    version_tags = []
                    latest_tag = None
                    
                    # 首先找到latest标签
                    for tag_info in data['data']['list']:
                        if tag_info['tag_name'] == 'latest':
                            latest_tag = tag_info
                            break
                    
                    if latest_tag:
                        # 找到与latest标签具有相同digest的版本标签
                        latest_digest = latest_tag.get('digest')
                        for tag_info in data['data']['list']:
                            if re.match(r'^v\d+\.\d+\.\d+$', tag_info['tag_name']) and tag_info.get('digest') == latest_digest:
                                version_tags.append(tag_info)
                    
                    # 如果没有找到与latest相同digest的版本标签，则收集所有版本标签
                    if not version_tags:
                        for tag_info in data['data']['list']:
                            if re.match(r'^v\d+\.\d+\.\d+$', tag_info['tag_name']):
                                version_tags.append(tag_info)
                    
                    if version_tags:
                        # 按更新时间排序，选择最新的
                        version_tags.sort(key=lambda x: x.get('tag_last_pushed', ''), reverse=True)
                        latest_version = version_tags[0]['tag_name']
                        published = version_tags[0].get('tag_last_pushed')
                        link = f"https://hub.docker.com/layers/kokojacket/baidu-autosave/{latest_version}/images/{version_tags[0].get('digest', '').split(':')[-1]}"
                        
                        logger.info(f"从1ms.run API获取到最新版本: {latest_version}")
                        return jsonify({
                            'success': True,
                            'version': latest_version,
                            'published': published,
                            'link': link,
                            'source': '1ms'
                        })
                
                # 如果没有找到有效的版本信息，返回错误
                logger.warning("1ms.run API未返回有效的版本信息")
                return jsonify({
                    'success': False,
                    'message': '1ms.run API未返回有效的版本信息',
                    'source': '1ms'
                })
                
            except Exception as e:
                logger.warning(f"从1ms.run API获取版本信息失败: {str(e)}")
                return jsonify({
                    'success': False,
                    'message': f'从1ms.run API获取版本信息失败: {str(e)}',
                    'source': '1ms'
                })
        else:
            # 默认使用 GitHub releases feed
            feed_url = f'https://github.com/{GITHUB_REPO}/releases.atom'
        
        # 如果是使用RSS源，则执行以下代码
        if source in ['github', 'dockerhub', 'dockerhub_alt']:
            try:
                # 设置超时，避免长时间等待
                response = requests.get(feed_url, timeout=5)
                response.raise_for_status()  # 如果响应状态码不是200，抛出异常
            except RequestException as e:
                logger.warning(f"获取{source}版本信息失败: {str(e)}")
                return jsonify({
                    'success': False,
                    'message': f'无法获取{source}版本信息: {str(e)}',
                    'source': source
                })
                
            # 解析 feed
            feed = feedparser.parse(response.content)
            if not feed.entries:
                logger.warning(f"{source}未找到版本信息")
                return jsonify({
                    'success': False,
                    'message': f'{source}未找到版本信息',
                    'source': source
                })
                
            # 获取最新版本信息
            if source in ['dockerhub', 'dockerhub_alt']:
                # 首先查找latest标签的条目
                latest_entry = None
                latest_guid = None
                version_entry = None
                
                for entry in feed.entries:
                    if ':latest' in entry.title:
                        latest_entry = entry
                        # 提取镜像ID（guid的@后面部分）
                        guid_match = re.search(r'@([a-f0-9]+)$', entry.guid)
                        if guid_match:
                            latest_guid = guid_match.group(1)
                        break
                
                if not latest_entry:
                    logger.warning("Docker Hub中未找到latest标签")
                    # 如果没有找到latest标签，使用第一个条目
                    latest_entry = feed.entries[0]
                
                # 如果找到了latest的guid，查找对应的版本号条目
                if latest_guid:
                    for entry in feed.entries:
                        # 检查是否是版本号标签（如v1.0.8）并且与latest有相同的guid
                        if re.search(r':v\d+\.\d+\.\d+', entry.title) and latest_guid in entry.guid:
                            version_entry = entry
                            break
                
                # 如果找到了版本号条目，使用它；否则使用latest条目
                entry_to_use = version_entry if version_entry else latest_entry
                
                # 提取版本号
                title = entry_to_use.title
                version_match = re.search(r':(?:v?\d+\.\d+\.\d+|latest)', title)
                latest_version = version_match.group(0)[1:] if version_match else title
                
                # 添加发布日期
                pub_date = entry_to_use.pubDate if hasattr(entry_to_use, 'pubDate') else entry_to_use.published
                
                logger.info(f"从Docker Hub ({source})获取到最新版本: {latest_version}")
                return jsonify({
                    'success': True,
                    'version': latest_version,
                    'published': pub_date,
                    'link': entry_to_use.link,
                    'source': source
                })
            else:
                # GitHub 格式
                title = feed.entries[0].title
                
                # 从标题中提取版本号，支持多种格式：
                # 1. "Release v1.0.8" -> "v1.0.8"
                # 2. "v1.0.8" -> "v1.0.8"
                # 3. "1.0.8" -> "1.0.8"
                version_match = re.search(r'(?:Release\s+)?(v?\d+\.\d+\.\d+)', title)
                if version_match:
                    latest_version = version_match.group(1)
                    # 确保版本号以v开头
                    if not latest_version.startswith('v'):
                        latest_version = 'v' + latest_version
                else:
                    # 如果无法提取版本号，使用原始标题
                    latest_version = title
                
                pub_date = feed.entries[0].published if hasattr(feed.entries[0], 'published') else None
                link = feed.entries[0].link if hasattr(feed.entries[0], 'link') else f"https://github.com/{GITHUB_REPO}/releases/latest"
                
                logger.info(f"从GitHub获取到最新版本: {title} -> 提取版本号: {latest_version}")
                return jsonify({
                    'success': True,
                    'version': latest_version,
                    'published': pub_date,
                    'link': link,
                    'source': 'github'
                })
    except Exception as e:
        logger.error(f"检查版本失败: {str(e)}")
        return jsonify({
            'success': False,
            'message': f'检查版本失败: {str(e)}',
            'source': source if 'source' in locals() else 'unknown'
        })

# 添加轮询API端点
@app.route('/api/tasks/status', methods=['GET'])
@login_required
@handle_api_error
def get_tasks_status():
    """获取所有任务的状态（用于轮询）"""
    if not storage:
        return jsonify({'success': False, 'message': '存储未初始化'})
    tasks = storage.list_tasks()
    # 按 order 排序，没有 order 的排在最后
    tasks.sort(key=lambda x: x.get('order', float('inf')))
    return jsonify({'success': True, 'tasks': _serialize_subscription_tasks(tasks)})

def cleanup_old_task_logs():
    """清理超过1小时的任务日志，释放内存"""
    if not hasattr(app, 'task_logs'):
        return
    
    current_time = datetime.now()
    tasks_to_remove = []
    
    # 获取所有任务状态
    try:
        if storage:
            tasks = storage.list_tasks()
            for task_order in list(app.task_logs.keys()):
                # 查找对应的任务
                task_found = False
                for task in tasks:
                    if task.get('order') == task_order:
                        task_found = True
                        # 如果任务不是运行状态，且日志有内容，检查最后一条日志的时间
                        if task.get('status') != 'running' and app.task_logs[task_order]:
                            last_log = app.task_logs[task_order][-1]
                            if 'timestamp' in last_log:
                                try:
                                    # 解析时间戳（HH:MM:SS格式）
                                    last_time_str = last_log['timestamp']
                                    last_time = datetime.strptime(f"{current_time.strftime('%Y-%m-%d')} {last_time_str}", '%Y-%m-%d %H:%M:%S')
                                    
                                    # 如果是昨天的日志，需要调整日期
                                    if last_time > current_time:
                                        last_time = last_time.replace(day=last_time.day - 1)
                                    
                                    # 如果超过1小时，标记为删除
                                    if (current_time - last_time).total_seconds() > 3600:
                                        tasks_to_remove.append(task_order)
                                except:
                                    # 时间解析失败，保留日志
                                    pass
                        break
                
                # 如果任务不存在，也清理其日志
                if not task_found:
                    tasks_to_remove.append(task_order)
        
        # 删除标记的日志
        for task_order in tasks_to_remove:
            if task_order in app.task_logs:
                del app.task_logs[task_order]
                logger.debug(f"已清理任务{task_order}的历史日志")
                
    except Exception as e:
        logger.error(f"清理任务日志失败: {str(e)}")

@app.route('/api/task/log/<int:task_id>', methods=['GET'])
@login_required
@handle_api_error
def get_task_log(task_id):
    """获取指定任务的执行日志（用于轮询）"""
    try:
        # 根据task_id找到真实的任务order
        if not storage:
            return jsonify({'success': False, 'message': '存储未初始化'})

        task_uid = request.args.get('task_uid')
        task_order = request.args.get('task_order')
        if task_order is not None:
            try:
                task_order = int(task_order)
            except (TypeError, ValueError):
                task_order = None

        tasks = storage.list_tasks()
        tasks.sort(key=lambda x: x.get('order', float('inf')))
        resolved_task = None
        if task_uid or task_order is not None:
            resolved_task = storage.resolve_task(task_uid, order=task_order)

        if resolved_task is not None:
            task_order = resolved_task.get('order')
        else:
            if not tasks or task_id >= len(tasks):
                return jsonify({'success': True, 'logs': []})

            # 获取真实的task order
            task_order = tasks[task_id].get('order')

        # 定期清理旧日志（每100次请求清理一次）
        _ensure_task_runtime_state()
        app._log_cleanup_counter += 1
        if app._log_cleanup_counter >= 100:
            cleanup_old_task_logs()
            app._log_cleanup_counter = 0

        # 从全局变量中获取任务日志
        if hasattr(app, 'task_logs') and task_order in app.task_logs:
            logs = app.task_logs[task_order]
            return jsonify({'success': True, 'logs': logs})
        else:
            # 如果没有找到任务日志，返回空列表
            return jsonify({'success': True, 'logs': []})
    except Exception as e:
        logger.error(f"获取任务日志失败: {str(e)}")
        return jsonify({'success': False, 'message': f'获取任务日志失败: {str(e)}'})


@app.route('/api/task/stream/<task_ref>', methods=['GET'])
@login_required
def stream_task(task_ref):
    """通过 SSE 实时推送任务状态与日志。"""
    if not storage:
        return jsonify({'success': False, 'message': '存储未初始化'}), 503

    task = storage.resolve_task(task_ref)
    if not task:
        return jsonify({'success': False, 'message': '任务不存在'}), 404

    task_uid = task.get('task_uid') or task_ref
    task_order = task.get('order')
    task_url = task.get('url')
    _remember_task_stream(task_uid, task_order)

    def event_stream():
        event_queue = queue.Queue(maxsize=200)
        _register_task_stream(task_uid, event_queue)

        try:
            yield _format_sse('connected', {
                'task_uid': task_uid,
                'task_order': task_order,
                'timestamp': datetime.now().strftime('%H:%M:%S')
            })
            yield _format_sse('snapshot', _build_task_stream_snapshot(task))

            while True:
                try:
                    event_name, payload = event_queue.get(timeout=15)
                except queue.Empty:
                    latest_task = storage.resolve_task(task_uid, order=task_order, url=task_url)
                    if latest_task is None:
                        yield _format_sse('completed', {
                            'task': {
                                'order': task_order,
                                'task_uid': task_uid,
                                'status': 'error',
                                'message': '任务已不存在'
                            }
                        })
                        break
                    yield _format_sse('heartbeat', {
                        'timestamp': datetime.now().strftime('%H:%M:%S')
                    })
                    continue

                yield _format_sse(event_name, payload)
                if event_name == 'completed':
                    break
        finally:
            _unregister_task_stream(task_uid, event_queue)

    response = Response(stream_with_context(event_stream()), mimetype='text/event-stream')
    response.headers['Cache-Control'] = 'no-cache'
    response.headers['X-Accel-Buffering'] = 'no'
    response.headers['Connection'] = 'keep-alive'
    return response

@app.route('/api/task/share', methods=['POST'])
@login_required
@handle_api_error
def share_task():
    """生成任务的分享链接"""
    if not storage:
        return jsonify({'success': False, 'message': '存储未初始化'})
    
    data = request.get_json()
    task_id = data.get('task_id')
    custom_password = data.get('password')  # 可选的自定义密码
    custom_period = data.get('period')      # 可选的自定义有效期
    
    if task_id is None:
        return jsonify({'success': False, 'message': '任务ID不能为空'})
    
    # 获取任务信息
    tasks = storage.list_tasks()
    tasks.sort(key=lambda x: x.get('order', float('inf')))
    
    if 0 <= task_id < len(tasks):
        task = tasks[task_id]
        save_dir = task.get('save_dir')
        
        if not save_dir:
            return jsonify({'success': False, 'message': '任务保存目录为空'})
        
        # 获取分享配置
        share_config = storage.config.get('share', {})
        password = custom_password if custom_password is not None else share_config.get('default_password', '1234')
        # 支持0作为永久有效期
        period_days = custom_period if custom_period is not None else share_config.get('default_period_days', 7)
        
        try:
            # 调用BaiduPCS-Py的share命令
            # 注意：share_file函数内部会检查并创建目录
            share_result = storage.share_file(save_dir, password, period_days)
            
            if share_result.get('success'):
                share_info = share_result.get('share_info', {})
                
                # 更新任务的分享信息
                task_order = task.get('order', task_id + 1)
                storage.update_task_share_info(task_order, share_info)
                
                return jsonify({
                    'success': True, 
                    'message': '分享链接生成成功',
                    'share_info': share_info
                })
            else:
                return jsonify({
                    'success': False, 
                    'message': share_result.get('error', '分享链接生成失败')
                })
                
        except Exception as e:
            logger.error(f"生成分享链接失败: {str(e)}")
            return jsonify({'success': False, 'message': f'生成分享链接失败: {str(e)}'})
    else:
        return jsonify({'success': False, 'message': '任务不存在'})

@app.route('/api/config/share', methods=['POST'])
@login_required
@handle_api_error
def update_share_config():
    """更新分享配置"""
    if not storage:
        return jsonify({'success': False, 'message': '存储未初始化'})
        
    data = request.get_json()
    share_config = {
        'default_password': data.get('default_password', '1234'),
        'default_period_days': data.get('default_period_days', 7)
    }
    
    # 更新配置
    storage.config['share'] = share_config
    storage._save_config()
    
    return jsonify({'success': True, 'message': '分享配置已更新'})

if __name__ == '__main__':
    try:
        # 启动时初始化应用
        init_success, init_error = init_app()
        if not init_success:
            logger.error(f"应用初始化失败: {init_error}")
            if init_error:
                logger.warning("将继续启动 Web 界面，但部分功能可能不可用")
        
        # 注册信号处理器
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
        
        # 启动HTTP服务器
        server_port = get_server_port()
        logger.info(f"使用标准WSGI服务器，监听端口: {server_port}")
        http_server = WSGIServer(('0.0.0.0', server_port), app, log=None)  # 禁用访问日志

        print(f'Server started at http://0.0.0.0:{server_port}')
        http_server.serve_forever()
    except KeyboardInterrupt:
        logger.info("接收到 Ctrl+C，正在退出...")
        signal_handler(signal.SIGINT, None)
    except Exception as e:
        logger.error(f"应用运行出错: {str(e)}")
        try:
            signal_handler(signal.SIGTERM, None)
        except:
            sys.exit(1) 