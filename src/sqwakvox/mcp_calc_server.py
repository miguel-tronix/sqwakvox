"""MCP Server: Calculator & Statistics Tools for Sqwakvox.

Provides safe mathematical evaluation, statistical analysis, and financial
computation tools, all accessible to the agent via MCP stdio transport.

Launch with:
    python -m sqwakvox.mcp_calc_server
"""

from __future__ import annotations

import ast
import math
import operator as op
import time
from collections import Counter
from collections.abc import Callable
from typing import Any

import numpy as np
from fastmcp import FastMCP

from sqwakvox.telemetry import get_telemetry, trace_span

mcp = FastMCP("sqwakvox-calc-stats")

# ---------------------------------------------------------------------------
# Safe expression evaluator (whitelist-based AST walker)
# ---------------------------------------------------------------------------

_SAFE_OPS: dict[type, Callable[..., Any]] = {
    ast.Add: op.add,
    ast.Sub: op.sub,
    ast.Mult: op.mul,
    ast.Div: op.truediv,
    ast.FloorDiv: op.floordiv,
    ast.Mod: op.mod,
    ast.Pow: op.pow,
    ast.LShift: op.lshift,
    ast.RShift: op.rshift,
    ast.USub: op.neg,
    ast.UAdd: op.pos,
    ast.BitAnd: op.and_,
    ast.BitOr: op.or_,
    ast.BitXor: op.xor,
    ast.Invert: op.invert,
}

_SAFE_FUNCTIONS: dict[str, Any] = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sum": sum,
    "pow": pow,
    "sqrt": math.sqrt,
    "log": math.log,
    "log10": math.log10,
    "log2": math.log2,
    "exp": math.exp,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "asin": math.asin,
    "acos": math.acos,
    "atan": math.atan,
    "degrees": math.degrees,
    "radians": math.radians,
    "ceil": math.ceil,
    "floor": math.floor,
    "trunc": math.trunc,
    "pi": math.pi,
    "e": math.e,
}


def _eval_node(node: ast.AST) -> Any:
    """Recursively evaluate a safe AST node."""
    match node:
        case ast.Constant(value):
            if isinstance(value, (int, float)):
                return value
            raise ValueError(f"Unsupported constant type: {type(value)}")
        case ast.BinOp(left=left, op=op_node, right=right):
            op_fn = _SAFE_OPS.get(type(op_node))
            if op_fn is None:
                raise ValueError(f"Unsupported operator: {type(op_node).__name__}")
            return op_fn(_eval_node(left), _eval_node(right))
        case ast.UnaryOp(op=op_node, operand=operand):
            op_fn = _SAFE_OPS.get(type(op_node))
            if op_fn is None:
                raise ValueError(f"Unsupported unary operator: {type(op_node).__name__}")
            return op_fn(_eval_node(operand))
        case ast.Name(id=name):
            val = _SAFE_FUNCTIONS.get(name)
            if val is None:
                raise ValueError(f"Name not allowed: {name}")
            return val
        case ast.Call(func=ast.Name(id=name), args=args):
            fn = _SAFE_FUNCTIONS.get(name)
            if fn is None:
                raise ValueError(f"Function not allowed: {name}")
            evaluated_args = [_eval_node(a) for a in args]
            return fn(*evaluated_args)
        case ast.Expression(body=body):
            return _eval_node(body)
        case _:
            raise ValueError(f"Unsupported expression node: {type(node).__name__}")


def safe_eval(expression: str) -> Any:
    """Safely evaluate a mathematical expression.

    Only whitelisted operators and math functions are permitted.
    """
    if not expression.strip():
        raise ValueError("Empty expression")
    tree = ast.parse(expression.strip(), mode="eval")
    return _eval_node(tree)


def _trace_tool(tool_name: str, fn: Callable[[], str]) -> str:
    tm = get_telemetry()
    start = time.monotonic()
    with trace_span(f"sqwakvox.mcp_tool.{tool_name}", {"tool": tool_name}) as span:
        try:
            result = fn()
            elapsed = time.monotonic() - start
            success = not result.startswith("Error:")
            span.set_attribute("success", success)
            span.set_attribute("duration_sec", elapsed)
            status_str = "success" if success else "error"
            if tm.mcp_tool_counter:
                tm.mcp_tool_counter.add(1, {"tool": tool_name, "status": status_str})
            if tm.mcp_tool_duration:
                tm.mcp_tool_duration.record(elapsed, {"tool": tool_name})
            return result
        except Exception:
            elapsed = time.monotonic() - start
            if tm.mcp_tool_counter:
                tm.mcp_tool_counter.add(1, {"tool": tool_name, "status": "exception"})
            if tm.mcp_tool_duration:
                tm.mcp_tool_duration.record(elapsed, {"tool": tool_name})
            raise


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------


@mcp.tool(
    name="calculator",
    description=(
        "Safely evaluate a mathematical expression. Supports +, -, *, /, //, "
        "**, %, bitwise ops, and math functions: sqrt, log, log10, log2, exp, "
        "sin, cos, tan, asin, acos, atan, ceil, floor, abs, round, min, max, "
        "sum, pow. Constants: pi, e. Example: 'sqrt(16) + 2 * 3' returns 10.0"
    ),
)
def calculator(expression: str) -> str:
    """Evaluate a mathematical expression safely."""

    def _run() -> str:
        try:
            result = safe_eval(expression)
            if isinstance(result, float):
                return f"{result:.10g}"
            return str(result)
        except Exception as exc:
            return f"Error: {exc}"

    return _trace_tool("calculator", _run)


def _with_numbers(numbers: str, func: Callable[[np.ndarray], str]) -> str:
    """Parse a number list and run *func* on the resulting 1D float array.

    Consolidates parsing, validation, and error handling across statistical tools
    (eliminating duplicate-code smell).
    """
    try:
        values = _parse_number_list(numbers)
    except ValueError as exc:
        return f"Error: {exc}"
    if not values:
        return "Error: no numbers provided"
    return func(np.asarray(values, dtype=float))


@mcp.tool(
    name="stats_summary",
    description=(
        "Compute a comprehensive statistical summary for a list of numbers. "
        "Returns count, sum, mean, median, min, max, range, variance, "
        "standard deviation, mode(s), and quartiles (Q1, Q2, Q3, IQR). "
        "Optional ddof parameter (default 0 for population)."
    ),
)
def stats_summary(numbers: str, ddof: int = 0) -> str:
    """Compute full stats for a comma/space-separated list of numbers."""

    def _run() -> str:
        def _calc(arr: np.ndarray) -> str:
            n = len(arr)
            total = float(np.sum(arr))
            mean = float(np.mean(arr))
            median = float(np.median(arr))

            counts = Counter(arr)
            max_count = max(counts.values())
            modes = sorted(k for k, v in counts.items() if v == max_count)
            mode_str = ", ".join(f"{m:.10g}" for m in modes) if len(modes) < n else "none"

            variance = float(np.var(arr, ddof=ddof))
            std_dev = float(np.std(arr, ddof=ddof))
            vmin = float(np.min(arr))
            vmax = float(np.max(arr))

            q1 = float(np.percentile(arr, 25, method="linear"))
            q2 = float(np.percentile(arr, 50, method="linear"))
            q3 = float(np.percentile(arr, 75, method="linear"))
            iqr = q3 - q1

            return (
                f"Count: {n}\n"
                f"Sum: {total:.10g}\n"
                f"Mean: {mean:.10g}\n"
                f"Median: {median:.10g}\n"
                f"Min: {vmin:.10g}\n"
                f"Max: {vmax:.10g}\n"
                f"Range: {vmax - vmin:.10g}\n"
                f"Variance (ddof={ddof}): {variance:.10g}\n"
                f"Std Dev (ddof={ddof}): {std_dev:.10g}\n"
                f"Mode(s): {mode_str}\n"
                f"Q1: {q1:.10g}\n"
                f"Q2: {q2:.10g}\n"
                f"Q3: {q3:.10g}\n"
                f"IQR: {iqr:.10g}"
            )

        return _with_numbers(numbers, _calc)

    return _trace_tool("stats_summary", _run)


@mcp.tool(
    name="stats_mean",
    description="Calculate the arithmetic mean (average) of a list of numbers.",
)
def stats_mean(numbers: str) -> str:
    def _run() -> str:
        return _with_numbers(numbers, lambda arr: f"{np.mean(arr):.10g}")

    return _trace_tool("stats_mean", _run)


@mcp.tool(
    name="stats_median",
    description="Calculate the median of a list of numbers.",
)
def stats_median(numbers: str) -> str:
    def _run() -> str:
        return _with_numbers(numbers, lambda arr: f"{np.median(arr):.10g}")

    return _trace_tool("stats_median", _run)


@mcp.tool(
    name="stats_stddev",
    description=(
        "Calculate the standard deviation of a list of numbers. "
        "Default is population std (ddof=0). Pass ddof=1 for sample std."
    ),
)
def stats_stddev(numbers: str, ddof: int = 0) -> str:
    def _run() -> str:
        return _with_numbers(numbers, lambda arr: f"{np.std(arr, ddof=ddof):.10g}")

    return _trace_tool("stats_stddev", _run)


@mcp.tool(
    name="stats_variance",
    description=(
        "Calculate the variance of a list of numbers. "
        "Default is population variance (ddof=0). Pass ddof=1 for sample variance."
    ),
)
def stats_variance(numbers: str, ddof: int = 0) -> str:
    def _run() -> str:
        return _with_numbers(numbers, lambda arr: f"{np.var(arr, ddof=ddof):.10g}")

    return _trace_tool("stats_variance", _run)


@mcp.tool(
    name="stats_minmax",
    description="Return the minimum and maximum values from a list of numbers.",
)
def stats_minmax(numbers: str) -> str:
    def _run() -> str:
        return _with_numbers(
            numbers,
            lambda arr: f"Min: {np.min(arr):.10g}, Max: {np.max(arr):.10g}",
        )

    return _trace_tool("stats_minmax", _run)


@mcp.tool(
    name="stats_percentile",
    description=(
        "Calculate the q-th percentile of a list of numbers. "
        "Parameter q is a float between 0 and 100."
    ),
)
def stats_percentile(numbers: str, q: float = 50) -> str:
    def _run() -> str:
        if not (0 <= q <= 100):
            return "Error: q must be between 0 and 100"
        return _with_numbers(numbers, lambda arr: f"Percentile({q}): {np.percentile(arr, q):.10g}")

    return _trace_tool("stats_percentile", _run)


@mcp.tool(
    name="stats_quartiles",
    description=(
        "Compute Q1, Q2 (median), Q3, and the Interquartile Range (IQR) for a list of numbers."
    ),
)
def stats_quartiles(numbers: str) -> str:
    def _run() -> str:
        def _calc(arr: np.ndarray) -> str:
            q1 = float(np.percentile(arr, 25))
            q2 = float(np.percentile(arr, 50))
            q3 = float(np.percentile(arr, 75))
            iqr = q3 - q1
            return (
                f"Q1 (25th): {q1:.10g}\n"
                f"Q2 (50th): {q2:.10g}\n"
                f"Q3 (75th): {q3:.10g}\n"
                f"IQR: {iqr:.10g}"
            )

        return _with_numbers(numbers, _calc)

    return _trace_tool("stats_quartiles", _run)


@mcp.tool(
    name="stats_2d",
    description=(
        "Compute statistics for a 2D numeric matrix given as rows separated by "
        "newlines and columns by commas/spaces. Aggregates across axis=0 "
        "(per-column, down each row) or axis=1 (per-row, across each column). "
        "Returns mean, median, min, max, std, var for the chosen axis. "
        "Optional ddof parameter (default 0 for population)."
    ),
)
def stats_2d(matrix: str, axis: int = 0, ddof: int = 0) -> str:
    """Compute per-column or per-row statistics for a 2D numeric matrix."""

    def _run() -> str:
        try:
            arr = _parse_matrix(matrix)
        except ValueError as exc:
            return f"Error: {exc}"

        if arr.size == 0:
            return "Error: no numbers provided"

        if axis not in (0, 1):
            return "Error: axis must be 0 (per-column) or 1 (per-row)"

        mean = np.mean(arr, axis=axis)
        median = np.median(arr, axis=axis)
        vmin = np.min(arr, axis=axis)
        vmax = np.max(arr, axis=axis)
        std = np.std(arr, axis=axis, ddof=ddof)
        var = np.var(arr, axis=axis, ddof=ddof)

        label = "Column" if axis == 0 else "Row"
        lines = [f"{label}-wise statistics (axis={axis}, ddof={ddof}):"]
        for i in range(mean.shape[0]):
            lines.append(
                f"{label} {i}: "
                f"mean={mean[i]:.10g}, "
                f"median={median[i]:.10g}, "
                f"min={vmin[i]:.10g}, "
                f"max={vmax[i]:.10g}, "
                f"std={std[i]:.10g}, "
                f"var={var[i]:.10g}"
            )
        return "\n".join(lines)

    return _trace_tool("stats_2d", _run)


# ---------------------------------------------------------------------------
# Financial tools
# ---------------------------------------------------------------------------


@mcp.tool(
    name="compound_interest",
    description=(
        "Calculate compound interest / future value. "
        "Parameters: principal, annual_rate (as percentage, e.g. 5 for 5%), "
        "years, compounds_per_year (default 12 for monthly). "
        "Returns the future value after compounding."
    ),
)
def compound_interest(
    principal: float,
    annual_rate: float,
    years: float,
    compounds_per_year: int = 12,
) -> str:
    def _run() -> str:
        rate = annual_rate / 100.0
        fv = principal * (1 + rate / compounds_per_year) ** (compounds_per_year * years)
        total_interest = fv - principal
        return (
            f"Future Value: {fv:.2f}\n"
            f"Total Interest Earned: {total_interest:.2f}\n"
            f"Annual Rate: {annual_rate}%\n"
            f"Compounding: {compounds_per_year}x per year for {years} years"
        )

    return _trace_tool("compound_interest", _run)


@mcp.tool(
    name="percentage_change",
    description="Calculate the percentage change from old_value to new_value.",
)
def percentage_change(old_value: float, new_value: float) -> str:
    def _run() -> str:
        if old_value == 0:
            return "Error: old_value cannot be zero (infinite percentage change)"
        change = ((new_value - old_value) / abs(old_value)) * 100.0
        direction = "increase" if change >= 0 else "decrease"
        return f"{change:.4f}% {direction} (from {old_value:.10g} to {new_value:.10g})"

    return _trace_tool("percentage_change", _run)


@mcp.tool(
    name="net_present_value",
    description=(
        "Calculate the Net Present Value (NPV) of a series of cash flows. "
        "Parameters: discount_rate (as percentage, e.g. 8 for 8%), "
        "cash_flows (comma/space-separated list, where the first value is "
        "the initial investment at t=0, typically negative)."
    ),
)
def net_present_value(discount_rate: float, cash_flows: str) -> str:
    def _run() -> str:
        try:
            flows = _parse_number_list(cash_flows)
        except ValueError as exc:
            return f"Error parsing cash flows: {exc}"
        if not flows:
            return "Error: no cash flows provided"

        rate = discount_rate / 100.0
        npv = sum(cf / (1 + rate) ** t for t, cf in enumerate(flows))
        return f"NPV: {npv:.4f}\nDiscount Rate: {discount_rate}%\nPeriods: {len(flows)}"

    return _trace_tool("net_present_value", _run)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_number_list(raw: str) -> list[float]:
    """Parse a comma/space/newline-separated string of numbers into a float list."""
    import re

    parts = re.split(r"[,\s]+", raw.strip())
    result: list[float] = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        try:
            result.append(float(p))
        except ValueError:
            raise ValueError(f"Not a valid number: '{p}'") from None
    return result


def _parse_matrix(raw: str) -> np.ndarray:
    """Parse a newline-separated matrix (comma/space columns) into a 2D array."""
    rows: list[list[float]] = []
    for line in raw.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(_parse_number_list(line))
        except ValueError:
            raise
    if not rows:
        return np.empty((0, 0), dtype=float)
    width = len(rows[0])
    for r in rows:
        if len(r) != width:
            raise ValueError("All matrix rows must have the same number of columns")
    return np.asarray(rows, dtype=float)


# ---------------------------------------------------------------------------
# Entry-point
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the MCP server.

    Defaults to stdio transport, which works with the stdio MCP config in
    ``mcp_servers.json``. Pass ``--sse`` (or ``--http``) to run as a long-lived
    HTTP server instead — this avoids the async→sync stdio threading issues
    documented in ``mcp_fixes.md`` (Priority 1). When using SSE/HTTP, point the
    client config at ``MCPSse`` / ``MCPStreamableHttp`` with the matching host/port.
    """
    import argparse

    parser = argparse.ArgumentParser(description="Sqwakvox calc-stats MCP server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "http"],
        default="stdio",
        help="Transport to use (default: stdio)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Host for sse/http transport")
    parser.add_argument("--port", type=int, default=8000, help="Port for sse/http transport")
    args = parser.parse_args()

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    elif args.transport == "sse":
        mcp.run(transport="sse", host=args.host, port=args.port)
    else:
        mcp.run(transport="streamable-http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
