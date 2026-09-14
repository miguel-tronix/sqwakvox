---
name: swe-duplicate-code-smell
description: Use when reviewing, refactoring, or auditing code to detect, evaluate, and eliminate duplicate code (DRY violations), while balancing abstraction overhead and avoiding premature deduplication.
---

# Duplicate Code Smell & Refactoring

Duplicate code occurs when similar or identical logic appears in multiple places within a codebase. It is one of the most common software design smells, increasing maintenance burden and causing divergent bugs when fixes applied to one location are missed in another.

---

## 1. Diagnostics: Identifying Types of Duplication

Before refactoring, classify the nature of the duplication:

- **Exact Duplication (Copy-Paste)**: Identical syntax and logic across multiple blocks or files.
- **Structural Duplication (Semantic)**: The execution flow and algorithm are identical, but variable names, data types, or literal values differ.
- **Incidental / Accidental Duplication**: Two blocks of code look similar today but represent fundamentally different business concepts or change for completely different reasons. **Do not deduplicate incidental duplication.**

---

## 2. Decision Framework: When to Deduplicate

Apply the **Rule of Three** and **AHA (Avoid Hasty Abstractions)**:

| Scenario | Action | Rationale |
| :--- | :--- | :--- |
| **1–2 instances** | **Leave duplicated** (unless exact large block) | Requirements may diverge. "Duplication is far cheaper than the wrong abstraction" (Sandi Metz). |
| **3+ instances** | **Refactor** | The common pattern, invariant flow, and variant inputs are now clear. |
| **Across domain boundaries** | **Keep separate** | Shared helpers across independent modules create tight architectural coupling. |
| **Test suites (DAMP vs. DRY)** | **Allow duplication** | Tests value clarity, isolation, and readability over DRY. Over-abstracted fixtures hide test setup. |

---

## 3. Step-by-Step Refactoring Patterns

### Pattern A: Parameterize Method (Variant Data)

When duplicate blocks differ only in inputs or constants, extract the common logic into a function and pass the differences as parameters.

```python
# Before: Duplicated processing with hardcoded variants
def export_pdf_report(records):
    header = "Report Header"
    payload = format_records(records)
    send_to_s3(f"reports/{today()}.pdf", header + payload)

def export_csv_report(records):
    header = "Report Header"
    payload = format_records(records)
    send_to_s3(f"reports/{today()}.csv", header + payload)

# After: Unified function with parameterized format
def export_report(records, file_format: str) -> None:
    header = "Report Header"
    payload = format_records(records)
    send_to_s3(f"reports/{today()}.{file_format}", header + payload)
```

### Pattern B: Template Method / Higher-Order Functions (Variant Execution)

When the surrounding lifecycle (setup, error handling, cleanup, retry) is duplicated but the core action differs, pass a callable or use a context manager.

```python
# Before: Duplicated retry & telemetry wrapper
def fetch_user_data(user_id: str):
    start = time.perf_counter()
    for attempt in range(3):
        try:
            return api_client.get_user(user_id)
        except TransientError:
            time.sleep(2 ** attempt)
    log_metric("user_fetch_failed", time.perf_counter() - start)

def fetch_order_data(order_id: str):
    start = time.perf_counter()
    for attempt in range(3):
        try:
            return api_client.get_order(order_id)
        except TransientError:
            time.sleep(2 ** attempt)
    log_metric("order_fetch_failed", time.perf_counter() - start)

# After: Reusable execution wrapper
from typing import Callable, TypeVar

T = TypeVar("T")

def execute_with_retry(operation: Callable[[], T], metric_name: str, max_retries: int = 3) -> T | None:
    start = time.perf_counter()
    for attempt in range(max_retries):
        try:
            return operation()
        except TransientError:
            time.sleep(2 ** attempt)
    log_metric(f"{metric_name}_failed", time.perf_counter() - start)
    return None
```

### Pattern C: Dispatch Map (Variant Branching)

Replace repetitive `if/elif/else` or `switch` statements with a dictionary dispatch table.

```python
# Before: Repeated conditional ladder
def handle_action(action_type: str, payload: dict):
    if action_type == "create":
        return create_resource(payload)
    elif action_type == "update":
        return update_resource(payload)
    elif action_type == "delete":
        return delete_resource(payload)
    raise ValueError(f"Unknown action: {action_type}")

# After: Dispatch table
ACTION_HANDLERS = {
    "create": create_resource,
    "update": update_resource,
    "delete": delete_resource,
}

def handle_action(action_type: str, payload: dict):
    handler = ACTION_HANDLERS.get(action_type)
    if not handler:
        raise ValueError(f"Unknown action: {action_type}")
    return handler(payload)
```

---

## 4. Refactoring Verification Checklist

When eliminating duplicate code:

1. **Behavioral Equivalence**: Run existing tests before and after refactoring to ensure zero regressions.
2. **Type Signatures & Return Types**: Ensure the consolidated function retains explicit type annotations (`mypy` / `pyright` clean).
3. **Traceability**: Ensure error messages and logging in the extracted helper preserve context (e.g., include the caller's contextual parameters).
4. **No Premature Generalization**: Do not add speculative flags (`if allow_extra_mode: ...`) to satisfy future hypothetical needs.
