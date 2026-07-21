"""T112 audit fix: shared JSON parsing helpers + dedup fenced-JSON regex.

之前 `r\"```(?:json)?\\\\s*(.*?)```\"` 在 4 个文件重复编译 (snapshot/vision.py
classifier/llm_enhanced.py llm/service.py 2 处). 提取成单点, 改解析
行为时只改这里.
"""
from __future__ import annotations

import json
import re
from typing import Any


# 之前在多处 re.compile 同一个 regex. 模块级单点, lazy 编译.
_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def strip_fenced_json(content: str) -> str:
    """如果 content 包在 ```` ```json ... ``` ```` 里, 提取内部内容.

    否则原样返回. 跟 service.py :181 + :254 两条路径一致.
    """
    if "```" not in content:
        return content
    m = _FENCED_JSON_RE.search(content)
    return m.group(1).strip() if m else content


def loads_json_strip_fence(content: str) -> Any:
    """strip_fenced_json + json.loads. 异常透传 (json.JSONDecodeError)."""
    return json.loads(strip_fenced_json(content))
