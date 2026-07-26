/**
 * pi extension: MCP ASCII Charts
 *
 * Integrates the @iflow-mcp/mcp-ascii-charts MCP server as pi custom tools.
 * Provides ASCII chart generation (bar, line, scatter, histogram, sparkline)
 * directly in the terminal.
 *
 * Usage:
 *   "create a bar chart of sales: [10, 25, 30, 45, 60] with labels Q1-Q5"
 *   "show me a line chart of monthly revenue"
 *   "list available charts"
 *
 * If the user asks for an unsupported chart type, the agent lists all
 * available chart tools.
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type, type Static } from "typebox";
import { spawn, type ChildProcess } from "node:child_process";
import { createInterface } from "node:readline";

// ─── MCP Protocol Types ──────────────────────────────────────────────────────

interface MCPJsonRpcRequest {
  jsonrpc: "2.0";
  id: number;
  method: string;
  params?: Record<string, unknown>;
}

interface MCPJsonRpcResponse {
  jsonrpc: "2.0";
  id: number;
  result?: Record<string, unknown>;
  error?: { code: number; message: string; data?: unknown };
}

// ─── MCP Client ──────────────────────────────────────────────────────────────

class MCPClient {
  private proc: ChildProcess | null = null;
  private rl: ReturnType<typeof createInterface> | null = null;
  private nextId = 0;
  private pending = new Map<
    number,
    { resolve: (v: MCPJsonRpcResponse) => void; reject: (e: Error) => void }
  >();
  private _ok = false;

  get ok(): boolean {
    return this._ok;
  }

  async start(command: string, args: string[]): Promise<void> {
    this.proc = spawn(command, args, { stdio: ["pipe", "pipe", "pipe"] });

    this.rl = createInterface({
      input: this.proc.stdout!,
      crlfDelay: Infinity,
    });

    // Stderr from MCP server is debug/log info
    this.proc.stderr!.on("data", (d: Buffer) => {
      const m = d.toString().trim();
      if (m && !m.startsWith("[DEBUG]")) console.error(`[mcp-charts] ${m}`);
    });

    this.rl.on("line", (line: string) => {
      try {
        const r: MCPJsonRpcResponse = JSON.parse(line);
        const cb = this.pending.get(r.id);
        if (cb) {
          this.pending.delete(r.id);
          cb.resolve(r);
        }
      } catch { /* malformed line, ignore */ }
    });

    this.proc.on("error", (err) => {
      for (const [, cb] of this.pending) cb.reject(err);
      this.pending.clear();
      this._ok = false;
    });

    this.proc.on("exit", (code) => {
      for (const [, cb] of this.pending)
        cb.reject(new Error(`MCP exited code ${code}`));
      this.pending.clear();
      this._ok = false;
    });

    // Handshake
    await this.send("initialize", {
      protocolVersion: "2024-11-05",
      capabilities: {},
      clientInfo: { name: "pi-mcp-charts", version: "1.0.0" },
    });
    this._ok = true;
  }

  async callTool(
    name: string,
    args: Record<string, unknown>,
  ): Promise<{ content: Array<{ type: string; text: string }>; isError?: boolean }> {
    const r = await this.send("tools/call", { name, arguments: args });
    if (r.error) {
      return { content: [{ type: "text", text: `Error: ${r.error.message}` }], isError: true };
    }
    const res = r.result as { content?: Array<{ type: string; text: string }>; isError?: boolean };
    return { content: res?.content ?? [{ type: "text", text: "No result" }], isError: res?.isError };
  }

  async stop(): Promise<void> {
    this._ok = false;
    if (!this.proc) return;
    this.proc.kill("SIGTERM");
    await new Promise<void>((resolve) => {
      const t = setTimeout(() => {
        this.proc?.kill("SIGKILL");
        resolve();
      }, 3000);
      this.proc!.on("exit", () => {
        clearTimeout(t);
        resolve();
      });
    });
    this.proc = null;
    this.rl = null;
  }

  private send(method: string, params?: Record<string, unknown>): Promise<MCPJsonRpcResponse> {
    const id = ++this.nextId;
    const msg: MCPJsonRpcRequest = { jsonrpc: "2.0", id, method, params };
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`MCP timeout: ${method}`));
      }, 30_000);
      this.pending.set(id, {
        resolve: (v) => { clearTimeout(timer); resolve(v); },
        reject: (e) => { clearTimeout(timer); reject(e); },
      });
      this.proc!.stdin!.write(JSON.stringify(msg) + "\n");
    });
  }
}

// ─── Chart Tools Metadata ────────────────────────────────────────────────────
// All available chart types from @iflow-mcp/mcp-ascii-charts

const CHART_TOOLS = [
  {
    name: "create_line_chart",
    label: "Line Chart",
    description: "Generate ASCII line charts for temporal data visualization. Best for showing trends over time.",
    parameters: Type.Object({
      data: Type.Array(Type.Number(), {
        description: "Array of numeric values to plot on the y-axis",
      }),
      labels: Type.Optional(
        Type.Array(Type.String(), {
          description: "Optional labels for x-axis (must match data length)",
        }),
      ),
      title: Type.Optional(
        Type.String({ description: "Optional chart title displayed above the chart" }),
      ),
      width: Type.Optional(
        Type.Number({
          description: "Chart width in characters (10-200, default: 60)",
          minimum: 10,
          maximum: 200,
        }),
      ),
      height: Type.Optional(
        Type.Number({
          description: "Chart height in characters (5-50, default: 15)",
          minimum: 5,
          maximum: 50,
        }),
      ),
      color: Type.Optional(
        Type.String({
          description: "ANSI color name (red, green, blue, yellow, cyan, magenta, white)",
        }),
      ),
    }),
  },
  {
    name: "create_bar_chart",
    label: "Bar Chart",
    description: "Create horizontal or vertical ASCII bar charts for comparing categories.",
    parameters: Type.Object({
      data: Type.Array(Type.Number(), {
        description: "Array of numeric values for each bar",
      }),
      labels: Type.Optional(
        Type.Array(Type.String(), {
          description: "Optional labels for each bar",
        }),
      ),
      title: Type.Optional(
        Type.String({ description: "Optional chart title" }),
      ),
      width: Type.Optional(
        Type.Number({
          description: "Chart width (10-200, default: 60)",
          minimum: 10,
          maximum: 200,
        }),
      ),
      height: Type.Optional(
        Type.Number({
          description: "Chart height (5-50, default: 15)",
          minimum: 5,
          maximum: 50,
        }),
      ),
      color: Type.Optional(
        Type.String({ description: "ANSI color name" }),
      ),
      orientation: Type.Optional(
        Type.Unsafe<"horizontal" | "vertical">({
          type: "string",
          enum: ["horizontal", "vertical"],
          description: "Bar orientation (default: horizontal)",
        }),
      ),
    }),
  },
  {
    name: "create_scatter_plot",
    label: "Scatter Plot",
    description: "Generate ASCII scatter plots for correlation analysis between variables.",
    parameters: Type.Object({
      data: Type.Array(Type.Number(), {
        description: "Array of y-values to plot (x-values will be indices)",
      }),
      labels: Type.Optional(
        Type.Array(Type.String(), {
          description: "Optional point labels",
        }),
      ),
      title: Type.Optional(
        Type.String({ description: "Optional chart title" }),
      ),
      width: Type.Optional(
        Type.Number({
          description: "Chart width (10-200, default: 60)",
          minimum: 10,
          maximum: 200,
        }),
      ),
      height: Type.Optional(
        Type.Number({
          description: "Chart height (5-50, default: 15)",
          minimum: 5,
          maximum: 50,
        }),
      ),
      color: Type.Optional(
        Type.String({ description: "ANSI color name" }),
      ),
    }),
  },
  {
    name: "create_histogram",
    label: "Histogram",
    description: "Create ASCII histograms showing frequency distribution of data.",
    parameters: Type.Object({
      data: Type.Array(Type.Number(), {
        description: "Array of numeric values for distribution analysis",
      }),
      title: Type.Optional(
        Type.String({ description: "Optional chart title" }),
      ),
      width: Type.Optional(
        Type.Number({
          description: "Chart width (10-200, default: 60)",
          minimum: 10,
          maximum: 200,
        }),
      ),
      height: Type.Optional(
        Type.Number({
          description: "Chart height (5-50, default: 15)",
          minimum: 5,
          maximum: 50,
        }),
      ),
      color: Type.Optional(
        Type.String({ description: "ANSI color name" }),
      ),
      bins: Type.Optional(
        Type.Number({
          description: "Number of histogram bins (3-50, default: 10)",
          minimum: 3,
          maximum: 50,
        }),
      ),
    }),
  },
  {
    name: "create_sparkline",
    label: "Sparkline",
    description: "Generate compact ASCII sparklines for inline mini-charts in dashboards.",
    parameters: Type.Object({
      data: Type.Array(Type.Number(), {
        description: "Array of numeric values to plot as a compact sparkline",
      }),
      title: Type.Optional(
        Type.String({ description: "Optional sparkline title" }),
      ),
      width: Type.Optional(
        Type.Number({
          description: "Sparkline width in characters (10-100, default: 40)",
          minimum: 10,
          maximum: 100,
        }),
      ),
      color: Type.Optional(
        Type.String({ description: "ANSI color name" }),
      ),
    }),
  },
] as const;

const CHART_NAMES = CHART_TOOLS.map((t) => t.name);
const CHART_LABELS: Record<string, string> = {};
const CHART_DESCS: Record<string, string> = {};
for (const t of CHART_TOOLS) {
  CHART_LABELS[t.name] = t.label;
  CHART_DESCS[t.name] = t.description;
}

// ─── Extension Entry Point ────────────────────────────────────────────────────

export default function (pi: ExtensionAPI) {
  const client = new MCPClient();
  let started = false;

  // ─── Helper: list all available charts ──────────────────────────────────────

  function formatChartList(): string {
    const lines = CHART_TOOLS.map(
      (t) => `  - \`${t.name}\` — ${t.label}: ${t.description}`,
    );
    return ["Chart not available. Here are the charts we can produce:", ...lines].join("\n");
  }

  // ─── Session Start: launch MCP server and register tools ───────────────────

  pi.on("session_start", async (_event, ctx) => {
    if (started) return; // already registered in this session

    try {
      await client.start("mcp-ascii-charts", []);

      // Register each known chart type as a pi tool
      for (const t of CHART_TOOLS) {
        pi.registerTool({
          name: t.name,
          label: t.label,
          description: t.description,
          promptSnippet: `Generate large, easy-to-read ASCII ${t.label.toLowerCase()} charts from data arrays — always use width=80, height=25`,
          promptGuidelines: [
            `Use \`${t.name}\` when the user asks for a ${t.label.toLowerCase()} visualization.`,
            `If the user requests ANY chart type not in ${JSON.stringify(CHART_NAMES)}, do NOT call any chart tool. Instead, respond with the list produced by formatChartList().`,
            `IMPORTANT: Always pass width=80 and height=25 in the tool arguments for full-size readability (height=30 for bar charts with many bars). Only use smaller values if the user explicitly requests a compact chart. The defaults are too small.`,
            `IMPORTANT: After calling the chart tool, ALWAYS show the full chart output in your response. Do not just describe the trend — display the chart itself inside a code block.`,
          ],
          parameters: t.parameters,

          async execute(
            _toolCallId,
            params,
            _signal,
            _onUpdate,
          ) {
            if (!client.ok) {
              return {
                content: [{ type: "text" as const, text: "Chart server is not ready. Please try again." }],
                isError: true,
              };
            }

            try {
              const result = await client.callTool(t.name, params as Record<string, unknown>);
              return result;
            } catch (err) {
              return {
                content: [
                  {
                    type: "text" as const,
                    text: `Chart failed: ${err instanceof Error ? err.message : String(err)}`,
                  },
                ],
                isError: true,
              };
            }
          },
        });
      }

      started = true;
      ctx.ui.notify(
        `MCP ASCII Charts loaded: ${CHART_TOOLS.map((t) => t.label).join(", ")}`,
        "info",
      );
    } catch (err) {
      ctx.ui.notify(
        `Failed to load MCP Charts: ${err instanceof Error ? err.message : String(err)}`,
        "error",
      );
    }
  });

  // ─── Session Shutdown ──────────────────────────────────────────────────────

  pi.on("session_shutdown", async () => {
    await client.stop();
    started = false;
  });
}
