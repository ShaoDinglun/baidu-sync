import json
import os
import re
from pathlib import Path
from loguru import logger

PROJECT_ROOT = Path(__file__).resolve().parent.parent

def generate_transfer_notification(tasks_results):
    """生成转存通知内容"""
    try:
        content = []
        
        # 添加成功任务信息
        for task in tasks_results['success']:
            task_name = task.get('name', task['url'])
            save_dir = task.get('save_dir', '')
            transferred_files = tasks_results['transferred_files'].get(task['url'], [])
            
            if transferred_files:  # 只有在有新文件时才添加到通知
                content.append(f"✅《{task_name}》添加追更：")
                
                # 按目录分组文件
                files_by_dir = {}
                for file_path in transferred_files:
                    dir_path = os.path.dirname(file_path)
                    if not dir_path:
                        dir_path = '/'
                    files_by_dir.setdefault(dir_path, []).append(os.path.basename(file_path))
                
                # 对每个目录的文件进行排序和显示
                for dir_path, files in files_by_dir.items():
                    # 构建完整的保存路径
                    full_path = save_dir
                    if dir_path and dir_path != '/':
                        full_path = os.path.join(save_dir, dir_path).replace('\\', '/')
                    content.append(full_path)
                    
                    files.sort()  # 对文件名进行排序
                    for i, file in enumerate(files):
                        is_last = (i == len(files) - 1)
                        prefix = '└── ' if is_last else '├── '
                        
                        # 根据文件类型添加图标
                        if file.lower().endswith(('.mp4', '.mkv', '.avi', '.mov')):
                            icon = '🎞️'
                        elif '.' not in file:
                            icon = '📁'
                        else:
                            icon = '📄'
                            
                        content.append(f"{prefix}{icon}{file}")
                
                content.append("")  # 添加空行分隔任务
        
        # 添加失败任务信息
        for task in tasks_results['failed']:
            task_name = task.get('name', task['url'])
            error_msg = task.get('error', '未知错误')
            if "error_code: 115" in error_msg:
                content.append(f"❌《{task_name}》：分享链接已失效")
            else:
                content.append(f"❌《{task_name}》：{error_msg}")
            content.append("")  # 添加空行分隔任务
        
        return "\n".join(content)
    except Exception as e:
        logger.error(f"生成通知内容失败: {str(e)}")
        return "生成通知内容失败" 


def _render_grouped_local_sync_items(items):
    content = []
    items_by_dir = {}

    for item in items:
        item_path = str(item.get('path') or '').replace('\\', '/').rstrip('/')
        if not item_path:
            continue
        dir_path = os.path.dirname(item_path) or '/'
        items_by_dir.setdefault(dir_path, []).append(item)

    for dir_path, dir_items in items_by_dir.items():
        content.append(dir_path)
        sorted_items = sorted(dir_items, key=lambda entry: str(entry.get('path') or ''))
        for index, item in enumerate(sorted_items):
            item_path = str(item.get('path') or '').replace('\\', '/').rstrip('/')
            item_name = os.path.basename(item_path) or item_path
            is_last = index == len(sorted_items) - 1
            prefix = '└── ' if is_last else '├── '
            kind = item.get('kind')
            if kind == 'directory':
                icon = '📁'
            elif str(item_name).lower().endswith(('.mp4', '.mkv', '.avi', '.mov')):
                icon = '🎞️'
            else:
                icon = '📄'
            content.append(f"{prefix}{icon}{item_name}")
    return content


def generate_local_sync_task_notification(task_results):
    """生成与订阅任务风格一致的本地同步通知内容。"""
    try:
        content = []

        for task in task_results.get('success', []):
            task_name = task.get('name') or task.get('task_id') or '未命名任务'
            synced_items = task_results.get('transferred_files', {}).get(task_name, [])
            if synced_items:
                content.append(f"✅《{task_name}》本地同步更新：")
                content.extend(_render_grouped_local_sync_items(synced_items))
                content.append('')

        for task in task_results.get('failed', []):
            task_name = task.get('name') or task.get('task_id') or '未命名任务'
            error_msg = task.get('error') or '执行失败，请查看日志'
            content.append(f"❌《{task_name}》：{error_msg}")
            content.append('')

        return '\n'.join(content).strip()
    except Exception as e:
        logger.error(f"生成本地同步通知内容失败: {str(e)}")
        return ''


def build_incremental_local_sync_results(log_file, fallback_task_names=None):
    """从增量同步日志提取任务级明细，用于生成通知。"""
    fallback_task_names = fallback_task_names or []
    results = {
        'success': [],
        'failed': [],
        'transferred_files': {},
    }

    path = Path(log_file) if log_file else None
    if not path or not path.exists():
        for task_name in fallback_task_names:
            results['failed'].append({'name': task_name, 'error': '未找到同步日志'})
        return results

    start_pattern = re.compile(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \| INFO \| 开始任务: (?P<name>[^|]+?) \|')
    end_pattern = re.compile(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \| INFO \| 任务结束: (?P<name>[^|]+?) \| .*?failures=(?P<failures>\d+)')
    action_pattern = re.compile(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \| INFO \| (?P<action>拉取缺失目录|拉取缺失文件|更新文件|拉取月份目录|拉取缺失一级目录|拉取缺失任务根目录): .*? -> (?P<local>.+)$')
    error_pattern = re.compile(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \| ERROR \| (?P<message>.+)$')

    task_map = {}
    current_task_name = None

    for raw_line in path.read_text(encoding='utf-8', errors='ignore').splitlines():
        line = raw_line.strip()
        if not line:
            continue

        match = start_pattern.match(line)
        if match:
            current_task_name = match.group('name').strip()
            task_map.setdefault(current_task_name, {'name': current_task_name, 'items': [], 'errors': [], 'failures': 0})
            continue

        match = action_pattern.match(line)
        if match and current_task_name:
            action = match.group('action')
            local_path = match.group('local').strip()
            kind = 'directory' if '目录' in action else 'file'
            task_map.setdefault(current_task_name, {'name': current_task_name, 'items': [], 'errors': [], 'failures': 0})
            task_map[current_task_name]['items'].append({'path': local_path, 'kind': kind})
            continue

        match = error_pattern.match(line)
        if match and current_task_name:
            task_map.setdefault(current_task_name, {'name': current_task_name, 'items': [], 'errors': [], 'failures': 0})
            task_map[current_task_name]['errors'].append(match.group('message').strip())
            continue

        match = end_pattern.match(line)
        if match:
            task_name = match.group('name').strip()
            failures = int(match.group('failures'))
            task_map.setdefault(task_name, {'name': task_name, 'items': [], 'errors': [], 'failures': 0})
            task_map[task_name]['failures'] = failures
            current_task_name = None

    if not task_map and fallback_task_names:
        for task_name in fallback_task_names:
            task_map[task_name] = {'name': task_name, 'items': [], 'errors': [], 'failures': 0}

    for task_name, task_data in task_map.items():
        if task_data['items']:
            results['success'].append({'name': task_name})
            results['transferred_files'][task_name] = task_data['items']

        if task_data['failures'] > 0 or task_data['errors']:
            error_msg = task_data['errors'][-1] if task_data['errors'] else '执行中存在失败，请查看日志'
            results['failed'].append({'name': task_name, 'error': error_msg})

    return results


def build_full_local_sync_results(payload):
    """从全量同步 summary payload 提取任务级明细，用于生成通知。"""
    results = {
        'success': [],
        'failed': [],
        'transferred_files': {},
    }

    for task in payload.get('tasks', []) or []:
        task_name = task.get('name') or '未命名任务'
        synced_items = task.get('synced_items') or []
        failed_items = task.get('failed_items') or []

        if synced_items:
            results['success'].append({'name': task_name})
            results['transferred_files'][task_name] = synced_items

        if failed_items or task.get('status') in {'failed', 'stopped'}:
            error_msg = failed_items[-1].get('error') if failed_items else '执行失败，请查看日志'
            results['failed'].append({'name': task_name, 'error': error_msg})

    return results


def _default_app_config_path():
    return PROJECT_ROOT / 'config' / 'config.json'


def load_notify_kwargs(config_path=None):
    """从主配置文件读取通知配置，并转换为 notify.send 可用的字段。"""
    try:
        config_file = Path(config_path) if config_path else _default_app_config_path()
        if not config_file.exists():
            return {}

        raw = json.loads(config_file.read_text(encoding='utf-8'))
        notify_config = raw.get('notify', {}) or {}
        if not notify_config.get('enabled'):
            return {}

        kwargs = {}
        if 'direct_fields' in notify_config:
            kwargs.update(notify_config.get('direct_fields', {}) or {})
        elif 'channels' in notify_config:
            channels = notify_config.get('channels', {}) or {}
            pushplus = channels.get('pushplus') or {}
            if pushplus.get('token'):
                kwargs['PUSH_PLUS_TOKEN'] = pushplus.get('token')
            if pushplus.get('topic'):
                kwargs['PUSH_PLUS_USER'] = pushplus.get('topic')

        if 'custom_fields' in notify_config:
            kwargs.update(notify_config.get('custom_fields', {}) or {})

        return {key: value for key, value in kwargs.items() if value not in (None, '')}
    except Exception as e:
        logger.error(f"加载通知配置失败: {str(e)}")
        return {}


def send_configured_notification(title, content, config_path=None):
    """使用主配置文件中的通知设置发送消息。"""
    try:
        if not str(content or '').strip():
            return False

        notify_kwargs = load_notify_kwargs(config_path=config_path)
        if not notify_kwargs:
            return False

        from backend.notify import send as notify_send

        notify_send(title, content, **notify_kwargs)
        return True
    except Exception as e:
        logger.error(f"发送通知失败: {str(e)}")
        return False


def generate_local_sync_incremental_notification(status, message, task_names=None, dry_run=False, started_at=None, finished_at=None, log_file='', summary_text=''):
    """生成增量本地同步通知内容。"""
    task_results = build_incremental_local_sync_results(log_file, fallback_task_names=task_names)
    detailed_content = generate_local_sync_task_notification(task_results)
    if detailed_content:
        return detailed_content

    status_map = {
        'success': '执行成功',
        'failed': '执行失败',
        'stopped': '已停止',
        'partial-success': '部分成功',
    }
    lines = [f"状态: {status_map.get(status, status or '未知状态')}"]

    if task_names:
        lines.append(f"任务: {'、'.join(task_names)}")
    else:
        lines.append('任务: 全部启用任务')

    lines.append(f"模式: {'Dry Run' if dry_run else '正式执行'}")

    if started_at:
        lines.append(f"开始时间: {started_at}")
    if finished_at:
        lines.append(f"结束时间: {finished_at}")
    if message:
        lines.append(f"结果: {message}")
    if log_file:
        lines.append(f"日志文件: {log_file}")
    if summary_text:
        lines.append('')
        lines.append(summary_text.strip())

    return '\n'.join(line for line in lines if line is not None)


def generate_local_sync_full_notification(status, message, payload):
    """生成全量本地同步通知内容。"""
    task_results = build_full_local_sync_results(payload)
    detailed_content = generate_local_sync_task_notification(task_results)
    if detailed_content:
        return detailed_content

    tasks = payload.get('tasks', []) or []
    status_map = {
        'success': '执行成功',
        'failed': '执行失败',
        'stopped': '已停止',
        'partial-success': '部分成功',
    }

    downloaded_dirs = sum(int(task.get('downloaded_dirs') or 0) for task in tasks)
    downloaded_files = sum(int(task.get('downloaded_files') or 0) for task in tasks)
    failed_items = sum(len(task.get('failed_items') or []) for task in tasks)
    success_tasks = sum(1 for task in tasks if task.get('status') in {'success', 'partial-success'})

    lines = [
        f"状态: {status_map.get(status, status or '未知状态')}",
        f"任务数: {len(tasks)}",
        f"成功任务数: {success_tasks}",
        f"拉取目录数: {downloaded_dirs}",
        f"新增文件数: {downloaded_files}",
        f"失败项数: {failed_items}",
        f"模式: {'Dry Run' if payload.get('dry_run') else '正式执行'}",
    ]

    if payload.get('started_at'):
        lines.append(f"开始时间: {payload.get('started_at')}")
    if payload.get('finished_at'):
        lines.append(f"结束时间: {payload.get('finished_at')}")
    if payload.get('duration_seconds') is not None:
        lines.append(f"耗时: {payload.get('duration_seconds')} 秒")
    if message:
        lines.append(f"结果: {message}")
    if payload.get('log_file'):
        lines.append(f"日志文件: {payload.get('log_file')}")

    task_lines = []
    for task in tasks:
        task_lines.append(
            f"- {task.get('name')}: status={task.get('status')}, dirs={task.get('downloaded_dirs')}, files={task.get('downloaded_files')}, failures={len(task.get('failed_items') or [])}"
        )

    if task_lines:
        lines.append('')
        lines.append('任务详情:')
        lines.extend(task_lines)

    return '\n'.join(lines)