# tools - 工具层

atlas/tools/ 模块实现统一的工具定义、注册调度，并提供内置工具集合。

## 文件结构

```
atlas/tools/
├── base.py     # Tool / Toolkit：工具定义与注册调度
└── builtin.py  # 内置工具：calculator / get_current_time
```

## base.py - 工具基座

### 模块职责

提供统一的工具定义（Tool）与工具集合（Toolkit），负责注册、导出 OpenAI 协议、按名执行。

### 核心类

#### Tool

单个工具的定义：

```python
@dataclass
class Tool:
    name: str
    description: str
    parameters: dict  # JSON Schema 描述参数
    func: Callable[[dict], str]  # 接收参数字典，返回字符串结果
```

#### Toolkit

工具集合，负责注册、导出 OpenAI 协议、按名执行。

```python
class Toolkit:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
```

### 核心方法

#### register(func, name, description, parameters) -> Tool

注册一个工具。**重名时直接抛 ValueError**，不静默覆盖（消除命名冲突的模糊性）。

#### get(name: str) -> Tool

按名获取工具，未注册抛 KeyError。

#### to_openai_tools() -> list[dict]

转换为 OpenAI function calling 协议：

```python
[
    {
        "type": "function",
        "function": {
            "name": t.name,
            "description": t.description,
            "parameters": t.parameters,
        },
    }
    for t in self._tools.values()
]
```

#### execute(name: str, arguments: str | dict) -> str

按名称执行工具。arguments 支持 JSON 字符串或字典：

```python
tool = self.get(name)
if isinstance(arguments, str):
    arguments = json.loads(arguments)
return tool.func(arguments)
```

### 设计决策

1. **重名直接报错**：不静默覆盖，消除命名冲突的模糊性
2. **arguments 支持字符串和字典**：OpenAI function calling 返回的是 JSON 字符串，直接传入无需预处理
3. **返回字符串**：工具结果统一为字符串，便于回填 memory

## builtin.py - 内置工具

### 模块职责

提供两个内置工具：calculator（安全算术计算器）和 get_current_time（获取当前时间）。

### calculator

安全算术计算器，支持加减乘除、幂、取模、整除。

**安全设计：** 用 AST 白名单求值，从源头拒绝危险表达式，而不是 try/except 兜底。

```python
_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}

def _safe_eval(node: ast.AST):
    """递归求值 AST 节点，遇到非白名单节点直接抛错。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp):
        op = _OPERATORS.get(type(node.op))
        if op is None:
            raise ValueError(f"不支持的运算符: {type(node.op).__name__}")
        return op(_safe_eval(node.left), _safe_eval(node.right))
    ...
```

**参数（JSON Schema）：**

```json
{
    "type": "object",
    "properties": {
        "expression": {
            "type": "string",
            "description": "数学表达式，例如 (1+2)*3"
        }
    },
    "required": ["expression"]
}
```

### get_current_time

返回当前时间，可指定时区（默认本地）。

```python
def get_current_time(args: dict) -> str:
    tz = args.get("timezone")
    if tz:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo(tz))
    else:
        now = datetime.now()
    return now.strftime("%Y-%m-%d %H:%M:%S")
```

**参数（JSON Schema）：**

```json
{
    "type": "object",
    "properties": {
        "timezone": {
            "type": "string",
            "description": "时区名称，例如 Asia/Shanghai，可选"
        }
    },
    "required": []
}
```

### BUILTIN_TOOLS

工具规格定义，供 Toolkit.register(**spec) 批量注册：

```python
BUILTIN_TOOLS = [
    {
        "func": calculator,
        "name": "calculator",
        "description": "执行算术计算，支持加减乘除、幂、取模、整除。输入数学表达式字符串。",
        "parameters": {...},
    },
    {
        "func": get_current_time,
        "name": "get_current_time",
        "description": "获取当前时间。",
        "parameters": {...},
    },
]
```

### 设计决策

1. **calculator 用 AST 而非 eval**：从源头消除代码注入风险，符合"消除 fallback 触发场景"原则
2. **白名单运算符**：只允许数值和算术运算符，函数调用、属性访问一律拒绝
3. **时区用 zoneinfo**：Python 3.9+ 标准库，不引入 pytz 等第三方依赖
4. **BUILTIN_TOOLS 用 dict 列表**：方便 Toolkit.register(**spec) 批量注册，扩展时追加一项即可
