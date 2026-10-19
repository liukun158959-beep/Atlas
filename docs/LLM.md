# 模型调用契约

`atlas.llm.ChatModel` 是 Agent / Compact 使用的最小边界：`chat(messages, tools=None, on_delta=None)`，返回 `message`、`usage`、`duration_ms`。`FakeLLM` 和已有三参数模型替身继续适用。`LLM` 提供 OpenAI 兼容实现，SDK 负责 HTTP/SSE 解码，Atlas 只负责请求配置、响应聚合和完成校验；不依赖产品权限、任务进程或 UI。

## 完成与失败

`import atlas.llm` 不加载 Agent 执行栈；原有 `from atlas import Agent, LLM, ...` 公共出口按需加载，保持名称与调用方式兼容。

- 流式和非流式仅接受第 0 个 choice 的 `stop` / `tool_calls` 为正常完成。缺少 `finish_reason`、仅 `[DONE]`、`length`、`content_filter` 和未知原因均抛 `IncompleteResponseError`。
- 异常的 `finish_reason` 与 `partial` 可用于调试。`partial` 仍为 `{message, usage, duration_ms}`，其中的工具仅为片段，不得执行。已经发送给回调的文字不能等同于成功结果。
- usage 保留服务端返回的明细；流读取完成原因之后的 usage 尾帧。耗时使用单调时钟。
- SDK / HTTP / 回调异常原样抛出。内部创建的 SDK 客户端禁用自动重试；注入客户端的重试设置由调用方负责，产品应设 `max_retries=0`。不在流失败后重发请求。

## 每次调用的配置

```python
from atlas.llm import ChatCallbacks, ChatOptions

result = llm.chat(messages, tools, options=ChatOptions(
    stream=True,
    sampling={"temperature": .3, "top_p": .8, "reasoning_effort": "low"},
    extra_body={"thinking": {"clear_thinking": False}, "tool_stream": True},
    preserve_reasoning=True,
    should_cancel=cancel_event.is_set,
), callbacks=ChatCallbacks(
    on_delta=show_text,
    on_reasoning=record_reasoning,
    on_progress=heartbeat,
    on_tool_delta=show_tool_snapshot,
))
```

`on_tool_delta(index, name, arguments)` 提供累计快照，工具增量同时触发 `on_progress()`。`on_reasoning` 与正文分离；只有 `preserve_reasoning=True` 才在返回 message 中保留 `reasoning_content`，其展示、存储和下一轮回传由产品决定。Atlas 不按模型名选择策略。

显式 options / callbacks 仅作用于本次调用，并分别替换实例默认值。未传 callbacks 时仍使用旧的 `on_delta` 参数及实例 `on_reasoning` / `on_progress` / `on_tool_delta`。未传 options 时仍使用实例 `sampling`。无正文回调默认非流式；工具专用调用可显式 `stream=True`。新集成优先使用每次调用配置，避免共享可变状态。

取消在发请求前、流片段交付前后、返回前检查，抛 `ChatCancelledError` 并关闭当前响应。它是协作式取消，无法中断尚未返回的阻塞读取；产品仍需 HTTP 超时及任务进程终止机制。

## 客户端所有权

`LLM(client=sdk_client)` 借用客户端，退出 context / `close()` 不关闭外部客户端；内部创建的客户端会关闭。`bind_client(client, model=..., owned=True)` 在替换时关闭旧的自有客户端，并声明新客户端所有权。代理、TLS、凭据和超时由产品构建客户端时配置；无需直接改写 `llm.client` 或通过 SDK `with_options` 偷换共享传输。

## AI 离线调试与契约回归

```powershell
python examples/llm_contract_demo.py --mock
python examples/llm_contract_demo.py --mock --incomplete
python -m pytest tests/test_llm_contract.py tests/test_llm_options.py tests/test_llm_stream.py -q
```

示例使用真实 SDK 与 `httpx.MockTransport`，不会访问模型服务。正常输出含正文、usage、思考回调；`--incomplete` 输出错误原因与 partial，退出码为 2。排查不完整结果时先看 finish_reason、已交付片段和工具参数；网络异常检查产品网络配置，不打印密钥。

`atlas.testing.llm_contract.StreamContract` 是可复用 unittest 行为套件。产品适配器继承它并提供 `make_model(transport)`、`call_model(...)`，即可执行相同的完成、错误、回调、取消、资源释放和所有权验证。desk_companion 的协作适配器直接运行该套件。
