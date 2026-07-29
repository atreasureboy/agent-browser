"""Tests for circuit breaker and metering modules."""
import time
import pytest
from semantic_browser.daemon.circuit_breaker import (
    CircuitBreaker, CircuitBreakerRegistry, CircuitState,
)
from semantic_browser.daemon.metering import (
    UsageEvent, MeteringStore, PriceTable,
)


class TestCircuitBreaker:
    """T122: 熔断器单元测试."""

    def test_initial_state_closed(self):
        cb = CircuitBreaker(name="test")
        assert cb.state == CircuitState.CLOSED
        assert cb.allow_request() is True

    def test_closed_to_open_on_failures(self):
        cb = CircuitBreaker(name="test", failure_threshold=3)
        for _ in range(3):
            cb.record_failure()
        assert cb.state == CircuitState.OPEN
        assert cb.allow_request() is False

    def test_open_to_half_open_after_timeout(self):
        cb = CircuitBreaker(name="test", failure_threshold=2, timeout_s=0.1)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitState.OPEN
        time.sleep(0.15)
        assert cb.allow_request() is True
        assert cb.state == CircuitState.HALF_OPEN

    def test_half_open_to_closed_on_successes(self):
        cb = CircuitBreaker(name="test", failure_threshold=2, timeout_s=0.1, success_threshold=2)
        cb.record_failure()
        cb.record_failure()
        time.sleep(0.15)
        cb.allow_request()  # triggers HALF_OPEN
        cb.record_success()
        cb.record_success()
        assert cb.state == CircuitState.CLOSED

    def test_half_open_to_open_on_failure(self):
        cb = CircuitBreaker(name="test", failure_threshold=2, timeout_s=0.1)
        cb.record_failure()
        cb.record_failure()
        time.sleep(0.15)
        cb.allow_request()  # triggers HALF_OPEN
        cb.record_failure()
        assert cb.state == CircuitState.OPEN
        # Timeout should double
        assert cb.current_timeout_s == 0.2

    def test_to_dict(self):
        cb = CircuitBreaker(name="test-site")
        d = cb.to_dict()
        assert d["name"] == "test-site"
        assert d["state"] == "closed"


class TestCircuitBreakerRegistry:
    """熔断器注册表测试."""

    def test_get_or_create(self):
        reg = CircuitBreakerRegistry()
        cb1 = reg.get_or_create("site-a")
        cb2 = reg.get_or_create("site-a")
        assert cb1 is cb2

    def test_check_unknown_returns_true(self):
        reg = CircuitBreakerRegistry()
        assert reg.check("unknown") is True

    def test_record_success_and_failure(self):
        reg = CircuitBreakerRegistry()
        cb = reg.get_or_create("site-a", failure_threshold=2, timeout_s=0.1, success_threshold=2)
        reg.record_failure("site-a")
        reg.record_failure("site-a")
        assert reg.check("site-a") is False
        # Reset by waiting + success
        cb.opened_at = time.time() - 0.2  # force timeout to expire
        reg.check("site-a")  # triggers HALF_OPEN
        reg.record_success("site-a")
        reg.record_success("site-a")
        assert cb.state == CircuitState.CLOSED

    def test_to_dict(self):
        reg = CircuitBreakerRegistry()
        reg.get_or_create("site-a")
        reg.get_or_create("site-b")
        d = reg.to_dict()
        assert "site-a" in d
        assert "site-b" in d


class TestMeteringStore:
    """T122: 计量存储测试."""

    def test_ingest_and_query(self, tmp_path):
        db_path = tmp_path / "metering.db"
        store = MeteringStore(db_path)
        event = UsageEvent(
            event_id="evt_001",
            kind="llm_usage",
            source="proxy",
            ts=time.time(),
            tenant_id="acme",
            agent_id="agent-1",
            run_id="run-123",
            provider="openai",
            model="gpt-4",
            input_tokens=1000,
            output_tokens=500,
            cost_micro_usd=50000,
        )
        store.ingest(event)
        usage = store.get_usage_by_run("run-123")
        assert "llm_usage" in usage
        assert usage["llm_usage"]["input_tokens"] == 1000
        assert usage["llm_usage"]["output_tokens"] == 500

    def test_ingest_batch(self, tmp_path):
        db_path = tmp_path / "metering.db"
        store = MeteringStore(db_path)
        events = [
            UsageEvent(
                event_id=f"evt_{i}",
                kind="llm_usage",
                source="proxy",
                ts=time.time(),
                run_id="run-batch",
                input_tokens=100,
                output_tokens=50,
            )
            for i in range(5)
        ]
        count = store.ingest_batch(events)
        assert count == 5
        usage = store.get_usage_by_run("run-batch")
        assert usage["llm_usage"]["input_tokens"] == 500

    def test_dedup_by_event_id(self, tmp_path):
        db_path = tmp_path / "metering.db"
        store = MeteringStore(db_path)
        event = UsageEvent(
            event_id="evt_dup",
            kind="llm_usage",
            source="proxy",
            ts=time.time(),
            run_id="run-dup",
            input_tokens=100,
        )
        store.ingest(event)
        store.ingest(event)  # duplicate
        usage = store.get_usage_by_run("run-dup")
        assert usage["llm_usage"]["input_tokens"] == 100  # not 200

    def test_get_usage_by_tenant(self, tmp_path):
        db_path = tmp_path / "metering.db"
        store = MeteringStore(db_path)
        for i in range(3):
            store.ingest(UsageEvent(
                event_id=f"evt_t{i}",
                kind="llm_usage",
                source="proxy",
                ts=time.time(),
                tenant_id="acme",
                input_tokens=100,
            ))
        usage = store.get_usage_by_tenant("acme")
        assert usage["llm_usage"]["input_tokens"] == 300


class TestPriceTable:
    """价格表测试."""

    def test_known_model(self):
        pt = PriceTable()
        cost, estimated = pt.calculate_cost("gpt-4", 1000, 500)
        assert estimated is False
        assert cost > 0

    def test_unknown_model_estimated(self):
        pt = PriceTable()
        cost, estimated = pt.calculate_cost("unknown-model-xyz", 1000, 500)
        assert estimated is True
        assert cost > 0

    def test_custom_prices(self):
        pt = PriceTable(custom_prices={"my-model": {"input": 100, "output": 200}})
        cost, estimated = pt.calculate_cost("my-model", 1000, 500)
        assert estimated is False
        # (1000 * 100 + 500 * 200) / 1000 = 200
        assert cost == 200
