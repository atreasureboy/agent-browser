# 框架集成（LangChain / AutoGen / Aider）

三个适配器把 SemanticQuery 暴露为各 agent 框架可调用的工具。
框架本身是 optional 依赖，不装也能用核心功能。

## 发现入口

```bash
curl -s localhost:8765/v1/integrations
```

返回 machine-readable 目录（entry point / installed / 参数 schema）。

## LangChain

```bash
pip install langchain-core
```

```python
from semantic_browser.integrations.langchain_adapter import SemanticQueryTool
tool = SemanticQueryTool(budget=2000, max_pages=1)
tool.run(query="Python 3.13 free-threading")
```

## AutoGen

```bash
pip install pyautogen
```

```python
from semantic_browser.integrations.autogen_adapter import semantic_query_fn
# register_function(semantic_query_fn, ...) 或放进 function_map
```

## Aider

无额外依赖。`semantic_query_tool(query, start_url, budget, max_pages)`
是普通函数，Aider 通过 signature + docstring 发现。

```python
from semantic_browser.integrations.aider_adapter import semantic_query_tool
```
