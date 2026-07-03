/**
 * SSE 客户端示例 — TypeScript / Node.js
 *
 * 用 native EventSource (Node 22+ / Deno / Bun / 浏览器) + 手动断线重连.
 *
 * 用法:
 *   ts-node examples/sse_client.ts --url http://127.0.0.1:8765
 *   tsx examples/sse_client.ts --url http://127.0.0.1:8765 --topics system.heartbeat,system.pressure
 *
 * 或编译:
 *   npx tsc examples/sse_client.ts --target es2022 --module commonjs --outDir dist/
 *   node dist/sse_client.js --url http://127.0.0.1:8765
 *
 * 契约 (T55/T59):
 *   GET /events?topics=foo,bar.baz
 *   Response: text/event-stream
 *     id: <seq>
 *     data: {"topic": "...", "payload": {...}, "ts": ..., "seq": ...}
 *
 *   断线续传: 重连时构造 URL ?since_seq=N 或加 Last-Event-ID header (EventSource
 *   原生支持, 自动重连时携带).
 */

interface SSEEvent {
  topic: string;
  payload: Record<string, unknown>;
  ts: number;
  seq: number;
}

type SSEHandler = (event: SSEEvent, rawId: string) => void;

interface ClientOptions {
  url: string;
  topics: string[];
  resumeSeq?: number;
  onEvent: SSEHandler;
  onError?: (err: Error) => void;
  onConnect?: () => void;
  maxEvents?: number;
}

/**
 * 订阅 SSE 流 — 自动重连 + 退避. 失败时 backoff: 0.5s → 1s → 2s → ... → 10s.
 * 成功连上后 backoff 重置.
 *
 * 设计: 不依赖 EventSource 的原生重连 (它不带 Last-Event-ID),
 * 自己手动 fetch + 解析 SSE 帧 — 这样能精细控制断点游标.
 *
 * 如果想用 native EventSource 简化: 见 examples/event_source_simple.ts (TODO).
 */
export class SSESubscriber {
  private abort: AbortController | null = null;
  private lastId: string | undefined;
  private received = 0;
  private stop = false;

  constructor(private opts: ClientOptions) {}

  async start(): Promise<void> {
    let backoff = 500;
    const maxBackoff = 10_000;

    while (!this.stop) {
      try {
        await this.connect();
        backoff = 500; // 成功一次重置
      } catch (err) {
        if (this.opts.onError) {
          this.opts.onError(err instanceof Error ? err : new Error(String(err)));
        }
      }

      if (this.stop) break;

      const sleepMs = Math.min(backoff, maxBackoff);
      console.error(`[sse_client] reconnecting in ${sleepMs}ms (Last-Event-ID=${this.lastId})`);
      await new Promise((r) => setTimeout(r, sleepMs));
      backoff = Math.min(backoff * 2, maxBackoff);
    }
  }

  shutdown(): void {
    this.stop = true;
    if (this.abort) this.abort.abort();
  }

  private async connect(): Promise<void> {
    const u = new URL("/events", this.opts.url);
    if (this.opts.topics.length > 0) {
      u.searchParams.set("topics", this.opts.topics.join(","));
    }
    if (this.lastId) {
      u.searchParams.set("since_seq", this.lastId);
    }

    this.abort = new AbortController();
    // alias to disambiguate from outer `stop` field
    const shouldStop = (): boolean => this.stop;

    const headers: Record<string, string> = {
      Accept: "text/event-stream",
      "Cache-Control": "no-cache",
    };
    if (this.lastId) {
      headers["Last-Event-ID"] = this.lastId;
    }

    const resp = await fetch(u.toString(), {
      method: "GET",
      headers,
      signal: this.abort.signal,
    });

    if (!resp.ok) {
      throw new Error(`SSE connect failed: ${resp.status} ${resp.statusText}`);
    }
    if (!resp.body) {
      throw new Error("SSE response has no body");
    }

    if (this.opts.onConnect) this.opts.onConnect();
    console.error(`[sse_client] connected: ${u.toString()}`);

    // 解析 SSE 帧 — 帧边界 = 双换行
    const reader = resp.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buf = "";
    let eventId: string | undefined;
    let dataBuf = "";

    try {
      while (!shouldStop()) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });

        // 处理完整帧
        let frameEnd: number;
        while ((frameEnd = buf.indexOf("\n\n")) !== -1) {
          const rawFrame = buf.slice(0, frameEnd);
          buf = buf.slice(frameEnd + 2);

          // 单帧内逐行解析
          eventId = undefined;
          dataBuf = "";
          for (const line of rawFrame.split("\n")) {
            if (line.startsWith(":")) continue; // comment / keepalive
            const colonIdx = line.indexOf(":");
            let field: string;
            let value: string;
            if (colonIdx === -1) {
              field = "data";
              value = line;
            } else {
              field = line.slice(0, colonIdx);
              value = line.slice(colonIdx + 1);
              if (value.startsWith(" ")) value = value.slice(1);
            }
            if (field === "id") eventId = value;
            else if (field === "data") {
              dataBuf += (dataBuf ? "\n" : "") + value;
            }
          }

          if (eventId !== undefined) this.lastId = eventId;
          if (dataBuf) {
            try {
              const evt: SSEEvent = JSON.parse(dataBuf);
              this.opts.onEvent(evt, eventId ?? "");
              this.received++;
              if (this.opts.maxEvents && this.received >= this.opts.maxEvents) {
                this.stop = true;
                break;
              }
            } catch {
              console.error(`[sse_client] malformed JSON in data: ${dataBuf}`);
            }
          }
          if (shouldStop()) break;
        }
      }
    } finally {
      try {
        reader.releaseLock();
      } catch {
        /* ignore */
      }
    }
  }
}

// ── CLI 入口 ────────────────────────────────────────────────

interface CLIArgs {
  url: string;
  topics: string[];
  resumeSeq?: number;
  maxEvents?: number;
}

function parseArgs(argv: string[]): CLIArgs {
  const args: CLIArgs = { url: "http://127.0.0.1:8765", topics: [] };
  for (let i = 2; i < argv.length; i++) {
    const cur = argv[i];
    const next = argv[i + 1];
    if (cur === "--url") args.url = next, i++;
    else if (cur === "--topics") args.topics = next.split(",").map((s) => s.trim()).filter(Boolean), i++;
    else if (cur === "--resume-seq") args.resumeSeq = parseInt(next, 10), i++;
    else if (cur === "--max-events") args.maxEvents = parseInt(next, 10), i++;
  }
  if (args.topics.length === 0) {
    args.topics = ["system.heartbeat", "system.pressure", "daemon.degraded"];
  }
  return args;
}

async function main(): Promise<number> {
  const args = parseArgs(process.argv);

  const sub = new SSESubscriber({
    url: args.url,
    topics: args.topics,
    resumeSeq: args.resumeSeq,
    maxEvents: args.maxEvents,
    onEvent: (evt) => {
      // 标准输出: 一行 JSON per event — 方便 pipe 到 jq
      process.stdout.write(JSON.stringify(evt) + "\n");
    },
    onError: (err) => {
      console.error(`[sse_client] error: ${err.message}`);
    },
    onConnect: () => {
      console.error(`[sse_client] connected to ${args.url}`);
    },
  });

  // 优雅退出 — SIGINT/SIGTERM
  const onSignal = () => {
    console.error("\n[sse_client] SIGINT/SIGTERM, shutting down");
    sub.shutdown();
  };
  process.on("SIGINT", onSignal);
  process.on("SIGTERM", onSignal);

  await sub.start();
  console.error(`[sse_client] done, received events`);
  return 0;
}

// 只在直接调用时跑 (不是 import 时)
if (require.main === module) {
  main().then(
    (code) => process.exit(code),
    (err) => {
      console.error(err);
      process.exit(1);
    }
  );
}