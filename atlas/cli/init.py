"""atlas init 子命令：交互式引导完成项目初始化。

完整流程：
    1. 检测/创建虚拟环境（.venv）
    2. 用 venv 内的 python 执行 pip install -e .
    3. 检测 .env 是否已存在，存在则询问是否覆盖
    4. 选择 LLM Provider（5 个预设 + 自定义）
    5. 输入 API Key（getpass 隐藏输入）
    6. 输入模型名（provider 预设给默认值，回车确认）
    7. 写入 .env，打印激活提示和验证命令
"""
from __future__ import annotations

import getpass
import os
import subprocess
import sys
from pathlib import Path

from ..providers import PROVIDERS, list_providers


# 项目根目录（atlas/cli/init.py 向上三级）
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ENV_PATH = _PROJECT_ROOT / ".env"
_VENV_PATH = _PROJECT_ROOT / ".venv"
_PYPROJECT = _PROJECT_ROOT / "pyproject.toml"


def _venv_python() -> Path:
    """返回 venv 内 python 可执行文件路径（区分 Win/Linux）。"""
    if os.name == "nt":
        return _VENV_PATH / "Scripts" / "python.exe"
    return _VENV_PATH / "bin" / "python"


def _activate_hint() -> str:
    """返回当前平台对应的 venv 激活命令。"""
    if os.name == "nt":
        return r".\.venv\Scripts\Activate.ps1"
    return "source .venv/bin/activate"


def _ensure_venv() -> bool:
    """检测 .venv 是否存在，不存在则创建。返回是否新建。"""
    # 已存在且包含 python 可执行文件，视为可用
    if _venv_python().exists():
        print(f"[1/3] 检测到现有虚拟环境: {_VENV_PATH}")
        return False

    print(f"[1/3] 创建虚拟环境: {_VENV_PATH}")
    # 用当前解释器创建 venv
    result = subprocess.run(
        [sys.executable, "-m", "venv", str(_VENV_PATH)],
        cwd=str(_PROJECT_ROOT),
    )
    if result.returncode != 0:
        print("虚拟环境创建失败，请检查 Python 环境。")
        sys.exit(1)
    if not _venv_python().exists():
        print(f"虚拟环境创建异常，未找到: {_venv_python()}")
        sys.exit(1)
    print("虚拟环境创建完成。")
    return True


def _pip_install() -> None:
    """用 venv 内的 python 执行 pip install -e .，把 atlas 装进 venv。"""
    if not _PYPROJECT.exists():
        print(f"[2/3] 未找到 {_PYPROJECT}，跳过依赖安装。")
        return

    print(f"[2/3] 安装 atlas 到虚拟环境（pip install -e .）...")
    python = _venv_python()
    result = subprocess.run(
        [str(python), "-m", "pip", "install", "-e", ".", "--quiet"],
        cwd=str(_PROJECT_ROOT),
    )
    if result.returncode != 0:
        print("依赖安装失败，请手动执行：")
        print(f"  {_activate_hint()}")
        print("  pip install -e .")
        sys.exit(1)
    print("依赖安装完成。")


def _select_provider() -> tuple[str, str, str]:
    """引导用户选择 provider，返回 (base_url, model, api_key)。"""
    print("\n选择 LLM Provider:")
    providers = list_providers()
    for idx, (_, display_name) in enumerate(providers, start=1):
        print(f"  [{idx}] {display_name}")
    print(f"  [{len(providers) + 1}] 自定义")

    while True:
        choice = input("\n请输入序号: ").strip()
        if not choice:
            continue
        try:
            num = int(choice)
        except ValueError:
            print("无效输入，请输入数字序号")
            continue

        if 1 <= num <= len(providers):
            key = providers[num - 1][0]
            info = PROVIDERS[key]
            print(f"已选择: {info['name']}")
            return info["base_url"], info["default_model"], key
        if num == len(providers) + 1:
            # 自定义：让用户手动填 base_url 和 model
            base_url = input("输入 base_url: ").strip()
            model = input("输入模型名: ").strip()
            return base_url, model, "custom"
        print("序号超出范围")


def _input_api_key() -> str:
    """使用 getpass 隐藏输入，避免终端历史泄露。"""
    while True:
        key = getpass.getpass("输入 API Key (输入隐藏): ").strip()
        if key:
            return key
        print("API Key 不能为空")


def _input_model(default_model: str) -> str:
    """模型名带默认值，回车即采用默认。"""
    while True:
        model = input(f"输入模型名 (回车采用默认 {default_model}): ").strip()
        if not model:
            return default_model
        return model


def _write_env(base_url: str, model: str, api_key: str) -> None:
    """写入 .env 文件。"""
    content = (
        f"ATLAS_API_KEY={api_key}\n"
        f"ATLAS_BASE_URL={base_url}\n"
        f"ATLAS_MODEL={model}\n"
    )
    _ENV_PATH.write_text(content, encoding="utf-8")


def _config_env() -> None:
    """交互收集配置并写入 .env。"""
    # 检测已有 .env
    if _ENV_PATH.exists():
        print(f"\n检测到已存在配置文件: {_ENV_PATH}")
        overwrite = input("是否覆盖? (y/n): ").strip().lower()
        if overwrite != "y":
            print("已跳过 .env 配置。")
            return

    base_url, default_model, _ = _select_provider()
    api_key = _input_api_key()
    model = _input_model(default_model)

    _write_env(base_url, model, api_key)
    print(f"\n配置已写入: {_ENV_PATH}")
    print(f"  base_url = {base_url}")
    print(f"  model    = {model}")


def run() -> int:
    """执行 init 命令，返回退出码。"""
    print("=" * 50)
    print("Atlas 项目初始化")
    print("=" * 50)

    # 步骤 1：虚拟环境
    _ensure_venv()

    # 步骤 2：安装依赖
    _pip_install()

    # 步骤 3：配置 .env
    print("\n[3/3] 配置 .env")
    _config_env()

    # 后续提示
    print("\n" + "=" * 50)
    print("初始化完成！")
    print("=" * 50)
    print("\n下一步：")
    print(f"  1. 激活虚拟环境: {_activate_hint()}")
    print("  2. 验证配置: python examples/quickstart.py")
    return 0


# 供 __main__.py 路由调用
main = run
