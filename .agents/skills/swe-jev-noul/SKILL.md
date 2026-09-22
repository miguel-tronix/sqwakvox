---
name: swe-jev-noul
description: Use when working with the TypeSafe Noul primitive — a binary yes/no classifier that returns a confidence float (0.0–1.0) in Jev/System One responses, or when defining Noul questions in the Python SDK, API calls, or JSON schemas.
---

# Noul — Binary Classifier Primitive

**Noul** (short for "no/oul") is a TypeSafe question primitive that performs binary classification. Unlike `Choice` (multi-class) or `Score` (continuous), a Noul returns a single float between 0.0 and 1.0 representing the model's confidence that the answer is "yes."

It lives in the `typesafe_sdk` module alongside `Choice` and `Score`.

---

## 1. Defining a Noul Question

### In JSON / API Schema

```json
{
  "urgency": {
    "type": "noul",
    "instructions": "Does this message express urgency?"
  }
}
```

### In the Python SDK

```python
from typesafe_sdk import Noul, TypeSafeClient

client = TypeSafeClient()

response = client.system_one(
    state="Hi, I've been trying to connect my Stripe account for 3 days.",
    questions={
        "is_urgent": Noul(
            instructions="Does this message convey urgency or time-sensitivity?"
        ),
    },
)

print(response.answers["is_urgent"].noul)  # e.g. 1.0
```

### In cURL / POST Request

```bash
curl -X POST https://api.typesafe.ai/v1/systemone \
  -H "Authorization: Bearer $TYPESAFE_API_KEY" \
  -H "Content-Type: application/json" \
  -d @- << 'EOF'
{
  "state": "Hi, I've been trying to connect my Stripe account for 3 days and the integration keeps failing. I'm losing sales. Please help ASAP.",
  "model": "jev-latest",
  "questions": {
    "urgency": {
      "type": "noul",
      "instructions": "Does this message express urgency?"
    }
  }
}
EOF
```

---

## 2. Response Shape

A Noul question returns this structure in the `answers` dict:

```json
{
  "is_urgent": {
    "type": "noul",
    "noul": 1.0
  }
}
```

| Field | Type | Meaning |
| :--- | :--- | :--- |
| `type` | string | Always `"noul"` |
| `noul` | float | Confidence score: `0.0` = no, `1.0` = yes |

---

## 3. Noul vs. Score vs. Choice

| Primitive | Output | Use case |
| :--- | :--- | :--- |
| **Noul** | float (0.0–1.0) | Binary yes/no classification |
| **Score** | float (with legend) | Continuous scoring with categorical labels |
| **Choice** | string (selected key) | Multi-class selection from a criteria dict |

Use **Noul** when you only need a yes/no answer with confidence. Use **Score** when you need a label from a legend with a numeric value. Use **Choice** when selecting from named categories.

---

## 4. Common Patterns

### Urgency Detection
```python
Noul(instructions="Does this message convey urgency or time-sensitivity?")
```

### Intent Gating
```python
Noul(instructions="Is the user asking for a refund?")
```

### Combined with other primitives in `system_one`:
```python
from typesafe_sdk import Choice, Noul, Score, TypeSafeClient

client = TypeSafeClient()

response = client.system_one(
    state=ticket,
    questions={
        "department": Choice(
            instructions="Which team should handle this?",
            criteria={"billing": "Payment issues", "technical": "Bugs"},
        ),
        "frustration": Score(
            instructions="How frustrated is the customer?",
            criteria=["Calm", "Frustrated", "Very angry"],
        ),
        "is_urgent": Noul(
            instructions="Does this message express urgency?"
        ),
    },
)
```

---

## 5. Quick Reference

| Detail | Value |
| :--- | :--- |
| **Module** | `typesafe_sdk` |
| **Class** | `Noul` |
| **Constructor param** | `instructions` (str) |
| **Response attribute** | `.noul` (float) |
| **API type string** | `"noul"` |
| **Model default** | `jev-latest` |
| **Min SDK Python** | 3.10 |
| **Install** | `pip install typesafe-sdk` or `uv add typesafe-sdk` |
