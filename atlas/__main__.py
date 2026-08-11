"""Atlas CLI 入口：支持 `python -m atlas <子命令>` 形式调用。

当前子命令：
    init  - 项目初始化（创建 venv + 安装依赖 + 配置 .env）

用法：
    python -m atlas init
"""
from __future__ import annotations

import sys


def _print_usage() -> int:
    print("用法: python -m atlas <子命令>")
    print()
    print("可用子命令:")
    print("  init   项目初始化（创建 venv + 安装依赖 + 配置 .env）")
    return 1


def main() -> int:
    if len(sys.argv) < 2:
        return _print_usage()

    command = sys.argv[1]
    if command == "init":
        from .cli.init import run
        return run()
    print(f"未知子命令: {command}")
    return _print_usage()


if __name__ == "__main__":
    sys.exit(main())
