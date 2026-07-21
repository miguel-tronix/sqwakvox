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
from collections import Counter
from collections.abc import Callable
from typing import Any

from mcp.server.fastmcp import FastMCP

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
    try:
        result = safe_eval(expression)
        # Format nicely — ints stay ints, floats get reasonable precision
        if isinstance(result, float):
            return f"{result:.10g}"
        return str(result)
    except Exception as exc:
        return f"Error: {exc}"


@mcp.tool(
    name="stats_summary",
    description=(
        "Compute a comprehensive statistical summary for a list of numbers. "
        "Returns count, sum, mean, median, min, max, range, variance (population), "
        "standard deviation (population), and mode(s)."
    ),
)
def stats_summary(numbers: str) -> str:
    """Compute full stats for a comma/space-separated list of numbers.

    Accepts formats like: "1, 2, 3, 4" or "1 2 3 4" or "1,2,3,4"
    """
    try:
        values = _parse_number_list(numbers)
    except ValueError as exc:
        return f"Error: {exc}"

    n = len(values)
    if n == 0:
        return "Error: no numbers provided"

    total = sum(values)
    mean = total / n
    sorted_vals = sorted(values)

    # median
    if n % 2 == 1:
        median = sorted_vals[n // 2]
    else:
        median = (sorted_vals[n // 2 - 1] + sorted_vals[n // 2]) / 2

    # mode
    counts = Counter(values)
    max_count = max(counts.values())
    modes = sorted(k for k, v in counts.items() if v == max_count)
    mode_str = ", ".join(f"{m:.10g}" for m in modes) if len(modes) < len(values) else "none"

    # variance & std dev (population)
    variance = sum((x - mean) ** 2 for x in values) / n
    std_dev = math.sqrt(variance)

    return (
        f"Count: {n}\n"
        f"Sum: {total:.10g}\n"
        f"Mean: {mean:.10g}\n"
        f"Median: {median:.10g}\n"
        f"Min: {sorted_vals[0]:.10g}\n"
        f"Max: {sorted_vals[-1]:.10g}\n"
        f"Range: {sorted_vals[-1] - sorted_vals[0]:.10g}\n"
        f"Variance (population): {variance:.10g}\n"
        f"Std Dev (population): {std_dev:.10g}\n"
        f"Mode(s): {mode_str}"
    )


@mcp.tool(
    name="stats_mean",
    description="Calculate the arithmetic mean (average) of a list of numbers.",
)
def stats_mean(numbers: str) -> str:
    try:
        values = _parse_number_list(numbers)
    except ValueError as exc:
        return f"Error: {exc}"
    if not values:
        return "Error: no numbers provided"
    return f"{sum(values) / len(values):.10g}"


@mcp.tool(
    name="stats_median",
    description="Calculate the median of a list of numbers.",
)
def stats_median(numbers: str) -> str:
    try:
        values = _parse_number_list(numbers)
    except ValueError as exc:
        return f"Error: {exc}"
    if not values:
        return "Error: no numbers provided"
    n = len(values)
    sv = sorted(values)
    if n % 2 == 1:
        return f"{sv[n // 2]:.10g}"
    return f"{(sv[n // 2 - 1] + sv[n // 2]) / 2:.10g}"


@mcp.tool(
    name="stats_stddev",
    description="Calculate the population standard deviation of a list of numbers.",
)
def stats_stddev(numbers: str) -> str:
    try:
        values = _parse_number_list(numbers)
    except ValueError as exc:
        return f"Error: {exc}"
    if not values:
        return "Error: no numbers provided"
    mean = sum(values) / len(values)
    var = sum((x - mean) ** 2 for x in values) / len(values)
    return f"{math.sqrt(var):.10g}"


@mcp.tool(
    name="stats_variance",
    description="Calculate the population variance of a list of numbers.",
)
def stats_variance(numbers: str) -> str:
    try:
        values = _parse_number_list(numbers)
    except ValueError as exc:
        return f"Error: {exc}"
    if not values:
        return "Error: no numbers provided"
    mean = sum(values) / len(values)
    return f"{sum((x - mean) ** 2 for x in values) / len(values):.10g}"


@mcp.tool(
    name="stats_minmax",
    description="Return the minimum and maximum values from a list of numbers.",
)
def stats_minmax(numbers: str) -> str:
    try:
        values = _parse_number_list(numbers)
    except ValueError as exc:
        return f"Error: {exc}"
    if not values:
        return "Error: no numbers provided"
    return f"Min: {min(values):.10g}, Max: {max(values):.10g}"


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
    rate = annual_rate / 100.0
    future_value = principal * (1 + rate / compounds_per_year) ** (compounds_per_year * years)
    total_interest = future_value - principal
    return (
        f"Future Value: {future_value:.2f}\n"
        f"Total Interest Earned: {total_interest:.2f}\n"
        f"Annual Rate: {annual_rate}%\n"
        f"Compounding: {compounds_per_year}x per year for {years} years"
    )


@mcp.tool(
    name="percentage_change",
    description="Calculate the percentage change from old_value to new_value.",
)
def percentage_change(old_value: float, new_value: float) -> str:
    if old_value == 0:
        return "Error: old_value cannot be zero (infinite percentage change)"
    change = ((new_value - old_value) / abs(old_value)) * 100.0
    direction = "increase" if change >= 0 else "decrease"
    return f"{change:.4f}% {direction} (from {old_value:.10g} to {new_value:.10g})"


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
    try:
        flows = _parse_number_list(cash_flows)
    except ValueError as exc:
        return f"Error parsing cash flows: {exc}"
    if not flows:
        return "Error: no cash flows provided"

    rate = discount_rate / 100.0
    npv = sum(cf / (1 + rate) ** t for t, cf in enumerate(flows))
    return f"NPV: {npv:.4f}\nDiscount Rate: {discount_rate}%\nPeriods: {len(flows)}"


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
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.run(transport="sse")
    else:
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
