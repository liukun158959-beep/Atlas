# cli - 命令行工具

atlas/cli/ 与 atlas/__main__.py 共同实现 Atlas 的命令行工具，提供项目初始化等子命令。

## 文件结构

```
atlas/
├── __main__.py     # CLI 入口：python -m atlas <子命令>
└── cli/
    └── init.py     # atlas init：venv + 依赖安装 + .env 配置
```

## __main__.py - CLI 入口

### 模块职责

支持 python -m atlas <子命令> 形式调用，路由到对应子命令。

### 当前子命令

- init：项目初始化（创建 venv + 安装依赖 + 配置 .env）

### 路由逻辑

```python
def main() -> int:
    if len(sys.argv) < 2:
        return _print_usage()

    command = sys.argv[1]
    if command == "init":
        from .cli.init import run
        return run()
    print(f"未知子命令: {command}")
    return _print_usage()
```

### 设计决策

1. **延迟导入**：init 的实现放在函数内导入，避免无子命令时加载不必要的依赖
2. **简单路由不用框架**：不用 click/typer，标准库 sys.argv 够用
3. **返回退出码**：遵循 Unix 惯例，0 成功非 0 失败

## init.py - 项目初始化

### 模块职责

交互式引导完成项目初始化，一条命令从零到可运行。

### 完整流程

```
1. 检测/创建虚拟环境（.venv）
2. 用 venv 内的 python 执行 pip install -e .
3. 检测 .env 是否已存在，存在则询问是否覆盖
4. 选择 LLM Provider（5 个预设 + 自定义）
5. 输入 API Key（getpass 隐藏输入）
6. 输入模型名（provider 预设给默认值，回车确认）
7. 写入 .env，打印激活提示和验证命令
```

### 路径常量

```python
_PROJECT_ROOT = Path(__file__).resolve().parents[2]  # 项目根目录
_ENV_PATH = _PROJECT_ROOT / ".env"
_VENV_PATH = _PROJECT_ROOT / ".venv"
_PYPROJECT = _PROJECT_ROOT / "pyproject.toml"
```

### 核心函数

#### _venv_python() -> Path

返回 venv 内 python 可执行文件路径（区分 Win/Linux）：

```python
def _venv_python() -> Path:
    if os.name == "nt":
        return _VENV_PATH / "Scripts" / "python.exe"
    return _VENV_PATH / "bin" / "python"
```

#### _activate_hint() -> str

返回当前平台对应的 venv 激活命令：
- Windows：.\.venv\Scripts\Activate.ps1
- Linux/macOS：source .venv/bin/activate

#### _ensure_venv() -> bool

检测 .venv 是否存在，不存在则用当前解释器创建：

```python
def _ensure_venv() -> bool:
    if _venv_python().exists():
        print(f"[1/3] 检测到现有虚拟环境: {_VENV_PATH}")
        return False

    print(f"[1/3] 创建虚拟环境: {_VENV_PATH}")
    result = subprocess.run(
        [sys.executable, "-m", "venv", str(_VENV_PATH)],
        cwd=str(_PROJECT_ROOT),
    )
    if result.returncode != 0:
        print("虚拟环境创建失败，请检查 Python 环境。")
        sys.exit(1)
    ...
```

#### _pip_install() -> None

用 venv 内的 python 执行 pip install -e .：

```python
def _pip_install() -> None:
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
        ...
        sys.exit(1)
```

#### _select_provider() -> tuple[str, str, str]

引导用户选择 provider，返回 (base_url, model, provider_key)。

#### _input_api_key() -> str

使用 getpass 隐藏输入，避免终端历史泄露。

#### _input_model(default_model: str) -> str

模型名带默认值，回车即采用默认。

#### _write_env(base_url: str, model: str, api_key: str) -> None

写入 .env 文件：

```python
content = (
    f"ATLAS_API_KEY={api_key}\n"
    f"ATLAS_BASE_URL={base_url}\n"
    f"ATLAS_MODEL={model}\n"
)
_ENV_PATH.write_text(content, encoding="utf-8")
```

#### _config_env() -> None

交互收集配置并写入 .env，包含覆盖检测。

#### run() -> int

执行 init 命令，串联三步流程：

```python
def run() -> int:
    _ensure_venv()      # 步骤 1：虚拟环境
    _pip_install()      # 步骤 2：安装依赖
    _config_env()       # 步骤 3：配置 .env
    # 打印后续提示
    ...
```

### 设计决策

1. **失败即退出**：venv 创建失败 / pip install 失败都直接 sys.exit(1)，打印手动恢复命令，不静默 fallback
2. **路径用 sys.executable**：用当前解释器创建 venv，避免依赖系统 PATH 里的 python
3. **pip install 用 venv 内 python**：避免污染系统环境，也绕过系统 Python 权限问题
4. **幂等性**：再次运行 init 会检测到 .venv 已存在直接跳过，不会重复创建
5. **getpass 隐藏 API Key**：避免终端历史泄露
6. **provider 预设自动填 base_url**：用户只需填 key，减少填错概率
7. **不自动验证配置**：init 结束打印验证命令提示，把验证责任交给 quickstart，保持 cli 层不依赖 LLM 层
