"""Atlas CLI 入口：支持 `python -m atlas <子命令>` 形式调用。

当前子命令：
    init       项目初始化（创建 venv + 安装依赖 + 配置 .env）
    dashboard  启动极简 Dashboard（HTTP + WebSocket 实时面板）

用法：
    python -m atlas init
    python -m atlas dashboard           # 默认 HTTP=8765, WS=8766
    python -m atlas dashboard --port 9000
"""
from __future__ import annotations

import sys


def _parse_port(argv: list[str], default: int) -> int:
    """从子命令 argv 解析 --port=<int> 或 --port <int>，不匹配返回 default。"""
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("--port="):
            try:
                return int(a.split("=", 1)[1])
            except ValueError:
                print(f"[dashboard] --port 值不合法: {a!r}, 用默认 {default}")
                return default
        if a == "--port" and i + 1 < len(argv):
            try:
                return int(argv[i + 1])
            except ValueError:
                print(f"[dashboard] --port 值不合法: {argv[i+1]!r}, 用默认 {default}")
                return default
        i += 1
    return default


def _print_usage() -> int:
    print("用法: python -m atlas <子命令> [选项]")
    print()
    print("可用子命令:")
    print("  init       项目初始化（创建 venv + 安装依赖 + 配置 .env）")
    print("  dashboard  启动极简 Dashboard（HTTP + WebSocket 实时面板）")
    print("                --port <int>   HTTP 端口 (默认 8765，WebSocket = port + 1)")
    return 1


def main() -> int:
    if len(sys.argv) < 2:
        return _print_usage()

    command = sys.argv[1]
    sub_argv = sys.argv[2:]

    if command == "init":
        from .cli.init import run

        return run()

    if command == "dashboard":
        from .cli.dashboard import run_server

        port = _parse_port(sub_argv, 8765)
        run_server(http_port=port)
        return 0

    print(f"未知子命令: {command}")
    return _print_usage()


if __name__ == "__main__":
    sys.exit(main())
