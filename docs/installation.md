# 安装

## 方式 A：PyPI（推荐）

```bash
pip install agent-site-intelligence
python -m playwright install chromium   # 浏览器内核
```

Linux 上如缺系统库：

```bash
python -m playwright install-deps chromium
```

## 方式 B：源码开发安装

```bash
git clone https://github.com/atreasureboy/agent-browser.git
cd agent-browser
pip install -e ".[dev]"
python -m playwright install chromium
```

`[dev]` 额外包含 pytest / pytest-asyncio / pytest-timeout / pytest-xdist /
PyYAML / ruff。

## 验证

```bash
sb browse https://example.com        # Python API/CLI 冒烟
tb-daemon --port 8765                # daemon 冒烟 (Ctrl-C 退出)
curl -s localhost:8765/health
```

## 可选依赖

| 集成 | 安装 |
|------|------|
| LangChain 适配器 | `pip install langchain-core` |
| AutoGen 适配器 | `pip install pyautogen` |
| Aider 适配器 | 无额外依赖 |

## 开发

```bash
make install          # editable + dev 依赖
make test             # 全量测试
make test-unit        # 快速离线子集
make lint             # ruff
```
