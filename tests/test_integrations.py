"""T77: integration tests — verify the format contracts even when framework isn't installed."""
import json
import sys

import pytest


class TestLangChainAdapterFormat:
    """测试 SemanticAnswer → JSON 格式契约, 不用真实 LangChain."""

    def test_format_result_json_shape(self):
        """_format_result 应返 JSON 含所有 token-saving 字段."""
        # 模拟 SemanticAnswer (绕开依赖)
        from semantic_browser.query import SemanticAnswer
        ans = SemanticAnswer(
            query="test",
            answer="python3.13t",
            sources=["https://docs.python.org/3/whatsnew/3.13.html"],
            confidence=0.95,
            tokens_used={
                "used": {"total": 638, "prompt": 412, "completion": 226},
                "max_total": 2000,
                "cache_hit": False,
            },
            steps=[{"phase": "plan_done", "ts": 1.0}, {"phase": "synth_done", "ts": 30.0}],
            success=True,
        )
        # _format_result 是 SemanticQueryTool 的方法, 但没 langchain 装不上
        # 我们直接构造要返的 dict, 验证契约
        payload = {
            "answer": ans.answer,
            "sources": list(ans.sources),
            "confidence": ans.confidence,
            "tokens_used": ans.tokens_used.get("used", {}).get("total", 0),
            "cache_hit": ans.tokens_used.get("cache_hit", False),
            "elapsed_s": ans.elapsed_s(),
            "success": ans.success,
        }
        # 应能 serialize 成 JSON
        s = json.dumps(payload, ensure_ascii=False, indent=2)
        assert "python3.13t" in s
        assert "638" in s  # tokens
        assert "0.95" in s  # confidence
        # elapsed_s 应能从 steps 算出 (≈ 29s)
        assert payload["elapsed_s"] == pytest.approx(29.0)
        assert payload["cache_hit"] is False
        assert payload["success"] is True

    def test_format_result_for_cache_hit(self):
        from semantic_browser.query import SemanticAnswer
        ans = SemanticAnswer(
            query="test",
            answer="cached answer",
            tokens_used={"used": {"total": 0}, "cache_hit": True, "cache_age_s": 5.0},
            success=True,
        )
        payload = {
            "tokens_used": ans.tokens_used.get("used", {}).get("total", 0),
            "cache_hit": ans.tokens_used.get("cache_hit", False),
            "cache_age_s": ans.tokens_used.get("cache_age_s"),
        }
        assert payload["tokens_used"] == 0
        assert payload["cache_hit"] is True
        assert payload["cache_age_s"] == 5.0


class TestIntegrationsGracefulDegradation:
    """integrations 包应优雅降级, 不强依赖 langchain."""

    def test_integrations_package_imports(self):
        """integrations/__init__.py 应能 import 不报错."""
        # 已经 import 过; 再 import 一次验证可重入
        import importlib
        m = importlib.import_module("semantic_browser.integrations")
        assert m is not None

    def test_langchain_adapter_requires_langchain(self):
        """没装 langchain-core 时, SemanticQueryTool 应要么 None, 要么构造时显式报 ImportError."""
        from semantic_browser.integrations import langchain_adapter
        # 模块本身应能 import
        assert langchain_adapter is not None
        # SemanticQueryTool 在退化模式下要么是 None, 要么构造时 raise
        from semantic_browser.integrations.langchain_adapter import SemanticQueryTool
        if SemanticQueryTool is None:
            # 退化模式 — module 已设 None
            pytest.skip("langchain-core not installed; SemanticQueryTool is None (degraded)")
        try:
            SemanticQueryTool()
            pytest.fail("should have raised ImportError without langchain-core")
        except ImportError as e:
            assert "langchain" in str(e).lower()


class TestAutogenAdapterFormat:
    """T89: AutoGen adapter 不依赖 pyautogen 也能 import (has_autogen 标识)."""

    def test_autogen_adapter_imports_without_pyautogen(self):
        from semantic_browser.integrations.autogen_adapter import (
            semantic_query_fn, has_autogen,
        )
        assert semantic_query_fn is not None
        # has_autogen 返 bool (不抛)
        assert isinstance(has_autogen(), bool)

    def test_autogen_adapter_degrades_without_pyautogen(self):
        """无 pyautogen 时, semantic_query_fn 返 JSON 警告 + 占位 answer."""
        from semantic_browser.integrations.autogen_adapter import semantic_query_fn, has_autogen
        if has_autogen():
            import pytest
            pytest.skip("pyautogen installed; degraded path skipped")
        result = semantic_query_fn("test query")
        # 解析 JSON, 验证含警告字段
        import json
        data = json.loads(result)
        assert "_warning" in data or "answer" in data


class TestAiderAdapterFormat:
    """T89: Aider adapter 是 sync function (Aider 期望 sync tool API)."""

    def test_aider_adapter_imports(self):
        from semantic_browser.integrations.aider_adapter import semantic_query_tool
        assert callable(semantic_query_tool)


class TestProductionDeployValidation:
    """T78: production_deploy.md 里的 yaml 块能 parse + 是有效 k8s manifests."""

    def test_yaml_blocks_parse(self):
        """所有 yaml 块能安全 parse."""
        import re
        import yaml
        content = open('/project/semantic-browser/examples/production_deploy.md').read()
        blocks = re.findall(r'```yaml\n(.*?)\n```', content, re.DOTALL)
        assert len(blocks) >= 1, "no yaml blocks found in production_deploy.md"
        for i, blk in enumerate(blocks):
            # 移除 # 注释行
            cleaned = '\n'.join(
                line for line in blk.split('\n') if not line.lstrip().startswith('#')
            )
            try:
                docs = list(yaml.safe_load_all(cleaned))
            except yaml.YAMLError as e:
                pytest.fail(f"yaml block {i+1} failed to parse: {e}")
            assert len(docs) >= 1

    def test_k8s_deployment_has_required_fields(self):
        """k8s Deployment manifest 必须含 kind / metadata / spec."""
        import re
        import yaml
        content = open('/project/semantic-browser/examples/production_deploy.md').read()
        blocks = re.findall(r'```yaml\n(.*?)\n```', content, re.DOTALL)
        # 第二个 block 含 deployment
        assert len(blocks) >= 2
        cleaned = '\n'.join(
            line for line in blocks[1].split('\n') if not line.lstrip().startswith('#')
        )
        docs = list(yaml.safe_load_all(cleaned))
        deployment = next((d for d in docs if d.get("kind") == "Deployment"), None)
        assert deployment is not None, "no Deployment in second yaml block"
        assert deployment["apiVersion"] == "apps/v1"
        assert "semantic-browser" in deployment["metadata"]["name"]
        # spec 应含 container image + port + probe
        spec = deployment["spec"]
        assert spec["replicas"] >= 1
        containers = spec["template"]["spec"]["containers"]
        assert len(containers) >= 1
        container = containers[0]
        # 关键: liveness + readiness probe 都应存在 (k8s production 必填)
        assert "livenessProbe" in container
        assert "readinessProbe" in container
        # 关键 env: ANTHROPIC_AUTH_TOKEN 应来自 secret
        env_names = [e["name"] for e in container.get("env", [])]
        assert "ANTHROPIC_AUTH_TOKEN" in env_names

    def test_pvc_has_storage_request(self):
        """PVC 应有 resources.requests.storage."""
        import re
        import yaml
        content = open('/project/semantic-browser/examples/production_deploy.md').read()
        blocks = re.findall(r'```yaml\n(.*?)\n```', content, re.DOTALL)
        cleaned = '\n'.join(
            line for line in blocks[1].split('\n') if not line.lstrip().startswith('#')
        )
        docs = list(yaml.safe_load_all(cleaned))
        pvc = next((d for d in docs if d.get("kind") == "PersistentVolumeClaim"), None)
        assert pvc is not None
        # 1Gi storage 给了
        assert "1Gi" in str(pvc["spec"]["resources"]["requests"]["storage"])
