---
name: swe-python-mutable-values
description: Use when auditing or writing Python code to avoid bugs with mutable default arguments, modifying collections during iteration, reference aliasing, shallow vs deep copies, and dataclass/Pydantic field definitions.
---

# Managing Mutable Values in Python

Mutable objects (`list`, `dict`, `set`, custom class instances) can lead to subtle bugs including accidental state sharing, mutated external inputs, and iteration runtime errors. Follow these patterns to manage mutability safely.

---

## 1. Mutable Default Arguments in Functions

### The Anti-Pattern
Default parameter expressions are evaluated **once when the function is defined**, not on each call. Any mutations persist across calls:

```python
# BUG: The same list is shared across all function calls!
def append_log(entry: str, log: list[str] = []) -> list[str]:
    log.append(entry)
    return log

print(append_log("first"))   # ['first']
print(append_log("second"))  # ['first', 'second']  <- Unintended state leak!
```

### The Safe Pattern
Default to `None` and instantiate a fresh collection inside the function body:

```python
from typing import Sequence

def append_log(entry: str, log: list[str] | None = None) -> list[str]:
    if log is None:
        log = []
    log.append(entry)
    return log
```

---

## 2. Mutable Defaults in Classes & Dataclasses

### Class-Level Attributes
Declaring mutable collections at the class body level shares that single instance across all instances of the class:

```python
# BUG: Class attribute shared across all instances
class Worker:
    tasks: list[str] = []  # Shared across all Worker instances!

w1 = Worker()
w2 = Worker()
w1.tasks.append("job1")
print(w2.tasks)  # ['job1'] <- Leak!
```

### Dataclasses and `default_factory`
In `@dataclass`, Python prevents mutable defaults by throwing a `ValueError`:

```python
from dataclasses import dataclass, field

# WRONG: Raises ValueError: mutable default for field items is not allowed
# @dataclass
# class Basket:
#     items: list[str] = []

# CORRECT: Use default_factory
@dataclass
class Basket:
    items: list[str] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)
```

---

## 3. Modifying Collections During Iteration

### Lists
Modifying list elements or length while iterating shifts the internal index pointer, causing elements to be skipped:

```python
# BUG: Deleting items shifts index, skipping adjacent elements
numbers = [1, 2, 2, 3, 4]
for n in numbers:
    if n == 2:
        numbers.remove(n)
print(numbers)  # [1, 2, 3, 4] <- One '2' was skipped!

# SAFE: Filter using list comprehension
numbers = [n for n in numbers if n != 2]

# SAFE: Iterate over a snapshot copy if in-place mutation is mandatory
for n in numbers[:]:
    if n == 2:
        numbers.remove(n)
```

### Dictionaries & Sets
Mutating a dictionary or set during iteration raises a `RuntimeError`:

```python
data = {"a": 1, "b": 2, "c": 3}

# WRONG: RuntimeError: dictionary changed size during iteration
# for k, v in data.items():
#     if v % 2 == 0:
#         del data[k]

# SAFE: Iterate over static keys list or use dict comprehension
for k in list(data.keys()):
    if data[k] % 2 == 0:
        del data[k]

# OR recreate:
data = {k: v for k, v in data.items() if v % 2 != 0}
```

---

## 4. Reference Aliasing: Shallow vs. Deep Copying

### The Assignment Trap
Simple assignment (`b = a`) only binds a new name to the same object reference.

### Shallow Copying (`copy.copy`, `.copy()`, `[:]`)
Shallow copying creates a new top-level container, but nested mutable objects inside remain shared references:

```python
import copy

nested = [[1, 2], [3, 4]]
shallow = nested.copy()

shallow[0].append(99)
print(nested)  # [[1, 2, 99], [3, 4]] <- Inner list was mutated!
```

### Deep Copying (`copy.deepcopy`)
Recursively duplicates both outer and all nested mutable structures:

```python
import copy

nested = [[1, 2], [3, 4]]
deep = copy.deepcopy(nested)

deep[0].append(99)
print(nested)  # [[1, 2], [3, 4]] <- Original untouched
```

---

## 5. In-Place Mutation vs. Returning New Objects

Be explicit whether a function mutates its argument in-place or returns a fresh transformed copy:

| Operation | In-Place Mutation (Returns `None`) | Pure Transformation (Returns New Object) |
| :--- | :--- | :--- |
| **List Sorting** | `items.sort()` | `sorted(items)` |
| **List Reversal** | `items.reverse()` | `list(reversed(items))` |
| **Dict Update** | `d1.update(d2)` | `d1 | d2` (Python 3.9+) |

---

## 6. Defensive Immutability

When a data structure should not be modified after initialization, enforce immutability at the type and runtime level:

- Use `tuple` instead of `list` for fixed sequences.
- Use `frozenset` instead of `set` for immutable collections and dictionary keys.
- Use `@dataclass(frozen=True)` for domain entities and configuration records.
- Use `types.MappingProxyType` to expose read-only dictionary views to callers.
