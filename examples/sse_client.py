"""
sse_client.py — Subscribe to Transparent Browser daemon /events SSE stream.

Reference Python client for the daemon's SSE endpoint. Demonstrates:
  - Topic filter (?topics=system.*,daemon.degraded)
  - Last-Event-ID resume after reconnect (W3C SSE spec)
  - Keepalive comment handling (": keepalive\n\n")
  - Graceful SIGINT shutdown

用法:
    # 启 daemon: python -m semantic_browser.daemon.server --port 8765
    # 跑示例: python examples/sse_client.py --url http://127.0.0.1:8765
    # 过滤 topic: python examples/sse_client.py --topics system.heartbeat,system.pressure
    # 断线续传: python examples/sse_client.py --resume-seq 42
    # 自定义 handler: import sse_client; sub = MyHandler(...); sub.run()

依赖: httpx (sync streaming client, blocking 主线程). 适合 CLI 工具;
长连的 daemon-internal 消费者参考 EventSubscriber 类的 on_event() override
方式做.

契约 (T55/T59/T65.3):
    GET /events?topics=foo,bar.baz
    Headers:
      Last-Event-ID: <seq>     # 断线续传 — 跳到 seq 之后
    Response: text/event-stream
      id: <seq>
      data: {"topic": "...", "payload": {...}, "ts": ..., "seq": ...}

      : keepalive              # 注释行 (空数据) — server 60s 没事件时发

topics 支持:
    "system.*"                # 通配符
    "system.pressure"         # 精确
    "*"                       # 全部 (默认)
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from typing import Optional

import httpx


def parse_sse_stream(response: httpx.Response):
    """解析 text/event-stream 帧 — yield (event_id, data_dict).

    SSE 协议 (W3C):
      - 帧以双换行 \\n\\n 分隔
      - 每行格式: "field: value\\n"
      - 字段: id (event id), data (event payload), event (event type), retry
      - 注释行: ": ..." (服务器保活, 客户端忽略)
    """
    event_id: Optional[str] = None
    data_buf: list[str] = []

    for raw_line in response.iter_lines():
        # httpx iter_lines 保留末尾 \\n — strip 后判定
        line = raw_line.rstrip("\n").rstrip("\r")

        if line == "":
            # 帧边界 — flush
            if data_buf:
                data_str = "\n".join(data_buf)
                try:
                    data_obj = json.loads(data_str)
                except json.JSONDecodeError:
                    # 多行 data 或非 JSON — 当字符串返
                    data_obj = {"raw": data_str}
                yield event_id, data_obj
            event_id = None
            data_buf = []
            continue

        if line.startswith(":"):
            # 注释 (keepalive / comment) — 忽略
            continue

        if ":" in line:
            field, _, value = line.partition(":")
            # SSE spec: 单空格前缀可选 strip
            if value.startswith(" "):
                value = value[1:]
        else:
            # 无冒号 — 当 data (e.g. server ping)
            field, value = "data", line

        if field == "id":
            event_id = value
        elif field == "data":
            data_buf.append(value)
        # 其他字段 (event, retry) — 当前 daemon 不发, 忽略


def run(url: str, topics: list[str], resume_seq: Optional[int],
        max_events: Optional[int], timeout_s: float,
        on_event: Optional[callable] = None) -> int:
    """Subscribe to /events with auto-reconnect + Last-Event-ID resume.

    on_event: 可选回调, signature (seq: str, payload: dict) -> None.
              默认是 print(json.dumps(payload)) 到 stdout.
              子类化或传 callable 都可以覆盖 — 比 print 灵活.
    """
    base = url.rstrip("/")
    sse_url = f"{base}/events"
    params: dict[str, str] = {}
    if topics:
        params["topics"] = ",".join(topics)
    if resume_seq is not None and resume_seq > 0:
        params["since_seq"] = str(resume_seq)

    headers: dict[str, str] = {}
    if resume_seq is not None and resume_seq > 0:
        # W3C SSE 标准的断点续传 header
        headers["Last-Event-ID"] = str(resume_seq)

    handler = on_event or _default_print_event

    print(f"[sse_client] connecting to {sse_url} topics={topics} resume_seq={resume_seq}",
          file=sys.stderr, flush=True)

    # 退避参数 — 重连间隔
    backoff = 0.5
    max_backoff = 10.0

    events_received = 0
    last_event_id: Optional[str] = None
    stop = False

    def on_signal(_sig, _frm):
        nonlocal stop
        stop = True
        print("\n[sse_client] SIGINT/SIGTERM received, exiting cleanly", file=sys.stderr,
              flush=True)
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    while not stop:
        try:
            with httpx.Client(timeout=httpx.Timeout(timeout_s)) as client:
                with client.stream("GET", sse_url, params=params,
                                   headers=headers) as resp:
                    resp.raise_for_status()
                    print(f"[sse_client] connected, status={resp.status_code}",
                          file=sys.stderr, flush=True)
                    backoff = 0.5  # 连上就重置 backoff

                    for event_id, data in parse_sse_stream(resp):
                        if stop:
                            break
                        if event_id is not None:
                            last_event_id = event_id
                            # 更新续传游标 — 下次断线重连从这里开始
                            headers["Last-Event-ID"] = event_id
                        try:
                            handler(event_id, data)
                        except Exception as e:
                            # handler 抛错不打断 SSE 流 — log 继续
                            print(f"[sse_client] handler error: {type(e).__name__}: {e}",
                                  file=sys.stderr, flush=True)
                        events_received += 1
                        if max_events is not None and events_received >= max_events:
                            stop = True
                            break

        except httpx.RemoteProtocolError as e:
            # server 主动断 (idle timeout / shutdown)
            print(f"[sse_client] server closed connection: {e}", file=sys.stderr,
                  flush=True)
        except httpx.ConnectError as e:
            # 连接失败 — daemon 可能没启
            print(f"[sse_client] connect failed: {e}", file=sys.stderr, flush=True)
        except httpx.HTTPError as e:
            print(f"[sse_client] HTTP error: {e}", file=sys.stderr, flush=True)
        except Exception as e:
            print(f"[sse_client] unexpected error: {type(e).__name__}: {e}",
                  file=sys.stderr, flush=True)

        if stop:
            break

        # 重连前 sleep (退避, 防 busy loop)
        print(f"[sse_client] reconnecting in {backoff:.1f}s (Last-Event-ID={last_event_id})",
              file=sys.stderr, flush=True)
        for _ in range(int(backoff * 10)):
            if stop:
                break
            time.sleep(0.1)
        backoff = min(backoff * 2, max_backoff)

    print(f"[sse_client] done, received {events_received} events", file=sys.stderr,
          flush=True)
    return 0


def _default_print_event(seq: Optional[str], payload: dict) -> None:
    """默认 handler — 把每条事件 JSON 写到 stdout."""
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Subscribe to daemon /events SSE stream")
    parser.add_argument("--url", default="http://127.0.0.1:8765",
                        help="daemon base URL (default: http://127.0.0.1:8765)")
    parser.add_argument("--topics", default="system.heartbeat,system.pressure,daemon.degraded",
                        help="comma-separated topic patterns (default: heartbeat + pressure + degraded)")
    parser.add_argument("--resume-seq", type=int, default=None,
                        help="resume from seq (uses Last-Event-ID header per W3C SSE)")
    parser.add_argument("--max-events", type=int, default=None,
                        help="exit after receiving N events (test convenience)")
    parser.add_argument("--timeout", type=float, default=300.0,
                        help="HTTP timeout per request in seconds (default: 300)")
    args = parser.parse_args(argv)

    topics = [t.strip() for t in args.topics.split(",") if t.strip()]
    return run(args.url, topics, args.resume_seq, args.max_events, args.timeout)


if __name__ == "__main__":
    sys.exit(main())