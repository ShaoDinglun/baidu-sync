#!/usr/bin/env python3
"""根据 systemd 生命周期事件发送服务异常和启动通知。

命令行参数：
    start
        服务启动后调用。若存在上一次异常状态，则发送恢复通知；否则发送启动通知。
    failure SERVICE_RESULT EXIT_CODE EXIT_STATUS
        服务停止后调用。仅当 SERVICE_RESULT 不是 success 时记录异常并发送通知。

环境变量：
    SERVICE_NOTIFY_STATE_FILE
        异常状态文件路径，默认写入项目 log 目录。
    SERVICE_NOTIFY_CONFIG_FILE
        主配置文件路径，默认读取 config/config.json。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import socket
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional, Union


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STATE_FILE = PROJECT_ROOT / "log" / "service_lifecycle_failure.json"
DEFAULT_CONFIG_FILE = PROJECT_ROOT / "config" / "config.json"
StatePath = Union[str, Path]
Notifier = Callable[[str, str], bool]


def _current_time() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def _resolve_state_file(state_file: Optional[StatePath] = None) -> Path:
    configured = state_file or os.getenv("SERVICE_NOTIFY_STATE_FILE") or DEFAULT_STATE_FILE
    return Path(configured)


def _resolve_config_file(config_file: Optional[StatePath] = None) -> Path:
    configured = config_file or os.getenv("SERVICE_NOTIFY_CONFIG_FILE") or DEFAULT_CONFIG_FILE
    return Path(configured)


def _write_failure_state(state_file: Path, payload: dict) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    temporary_file = state_file.with_suffix(state_file.suffix + ".tmp")
    temporary_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary_file.replace(state_file)


def _load_notify_fields(config_file: Path) -> dict:
    if not config_file.exists():
        return {}
    raw = json.loads(config_file.read_text(encoding="utf-8"))
    notify_config = raw.get("notify", {}) or {}
    if not notify_config.get("enabled"):
        return {}
    fields = dict(notify_config.get("direct_fields", {}) or {})
    fields.update(notify_config.get("custom_fields", {}) or {})
    return {key: value for key, value in fields.items() if value not in (None, "")}


def send_configured_notification(title: str, content: str, config_file: Optional[StatePath] = None) -> bool:
    """使用主配置中的飞书/Webhook 字段发送通知。

    输入参数：
        title: 通知标题。
        content: 通知正文。
        config_file: 可选配置文件路径，默认读取 config/config.json。
    输出：
        成功调用已配置通知渠道时返回 True；配置缺失或发送异常时返回 False。
    """
    try:
        fields = _load_notify_fields(_resolve_config_file(config_file))
        if not fields:
            print("服务生命周期通知未发送：通知未启用或配置为空", file=sys.stderr)
            return False

        notify_path = PROJECT_ROOT / "backend" / "notify.py"
        spec = importlib.util.spec_from_file_location("baidu_autosave_notify", notify_path)
        if not spec or not spec.loader:
            raise RuntimeError("无法加载通知模块")
        notify_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(notify_module)
        notify_module.send(title, content, ignore_default_config=True, **fields)
        return True
    except Exception as exc:
        print(f"服务生命周期通知发送失败：{exc}", file=sys.stderr)
        return False


def notify_failure(
    service_result: str,
    exit_code: str,
    exit_status: str,
    *,
    state_file: Optional[StatePath] = None,
    notifier: Notifier = send_configured_notification,
    hostname: Optional[str] = None,
    now: Optional[str] = None,
) -> bool:
    """记录异常停止状态并发送告警；正常停止不会发送。"""
    if str(service_result or "").strip().lower() == "success":
        return False

    failed_at = now or _current_time()
    host = hostname or socket.gethostname()
    payload = {
        "service_result": service_result or "unknown",
        "exit_code": exit_code or "unknown",
        "exit_status": exit_status or "unknown",
        "failed_at": failed_at,
        "hostname": host,
    }
    _write_failure_state(_resolve_state_file(state_file), payload)
    content = "\n".join(
        [
            f"主机: {host}",
            f"服务: baidu-autosave.service",
            f"异常时间: {failed_at}",
            f"systemd 结果: {payload['service_result']}",
            f"退出类型: {payload['exit_code']}",
            f"退出状态: {payload['exit_status']}",
            "处理: systemd 将按自动重启策略尝试恢复服务",
        ]
    )
    return bool(notifier("❌ 百度网盘服务异常", content))


def notify_start(
    *,
    state_file: Optional[StatePath] = None,
    notifier: Notifier = send_configured_notification,
    hostname: Optional[str] = None,
    now: Optional[str] = None,
) -> bool:
    """发送启动或异常恢复通知。"""
    resolved_state_file = _resolve_state_file(state_file)
    started_at = now or _current_time()
    host = hostname or socket.gethostname()

    if resolved_state_file.exists():
        try:
            failure = json.loads(resolved_state_file.read_text(encoding="utf-8"))
        except Exception:
            failure = {}
        title = "✅ 百度网盘服务已恢复"
        content = "\n".join(
            [
                f"主机: {host}",
                "服务: baidu-autosave.service",
                f"异常时间: {failure.get('failed_at', '未知')}",
                f"异常原因: {failure.get('service_result', '未知')}",
                f"恢复时间: {started_at}",
                "状态: 服务已由 systemd 成功拉起",
            ]
        )
        notified = bool(notifier(title, content))
        if notified:
            resolved_state_file.unlink(missing_ok=True)
        return notified

    content = "\n".join(
        [
            f"主机: {host}",
            "服务: baidu-autosave.service",
            f"启动时间: {started_at}",
            "状态: 服务启动成功",
        ]
    )
    return bool(notifier("✅ 百度网盘服务已启动", content))


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="发送 baidu-autosave 服务生命周期通知")
    subparsers = parser.add_subparsers(dest="action", required=True)
    subparsers.add_parser("start", help="发送启动或恢复通知")
    failure_parser = subparsers.add_parser("failure", help="异常停止时发送通知")
    failure_parser.add_argument("service_result")
    failure_parser.add_argument("exit_code")
    failure_parser.add_argument("exit_status")
    args = parser.parse_args(argv)

    if args.action == "start":
        return 0 if notify_start() else 1
    return 0 if notify_failure(args.service_result, args.exit_code, args.exit_status) else 1


if __name__ == "__main__":
    raise SystemExit(main())
