# Deploying Sqwakvox with AWS Bedrock

Sqwakvox is a **Textual TUI** (terminal UI) app — it runs in your terminal, not as a web service. Deploying it "with AWS Bedrock" means routing the LLM calls through Bedrock rather than directly to OpenAI/Anthropic/etc.

Below are the requirements and steps.

---

## 1. AWS Account & IAM Setup

You need an AWS account with **Bedrock access** and an IAM user/role that can invoke models.

### Minimum IAM policy

```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "bedrock:InvokeModel",
                "bedrock:InvokeModelWithResponseStream"
            ],
            "Resource": "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-3-5-sonnet-20241022-v2:0"
        }
    ]
}
```

You can broaden the `Resource` to `*` during development, but restrict it per-model in production.

### Credentials

Provide credentials via one of:

- **Environment variables** (easiest for local use):

  ```bash
  export AWS_ACCESS_KEY_ID=AKIA...
  export AWS_SECRET_ACCESS_KEY=...
  export AWS_DEFAULT_REGION=us-east-1
  ```

- **AWS CLI config** (`~/.aws/credentials`):

  ```ini
  [default]
  aws_access_key_id = AKIA...
  aws_secret_access_key = ...
  region = us-east-1
  ```

- **IAM role** (EC2, ECS, or CloudShell) — no keys needed, the SDK picks up the role automatically.

### Model access

In the **AWS Bedrock console** → **Model access**, request access to the models you want (e.g. Claude 3.5 Sonnet, Claude 3 Haiku, Llama 3, Mistral). This is a one-time approval (usually instant for Anthropic models).

---

## 2. Code Changes

The current `ModelProvider.MAP` in `src/sqwakvox/models.py` only knows about direct API providers. You need to add Bedrock model entries.

### Add to `models.py`

```python
class ModelProvider:
    MAP: ClassVar[dict[str, dict[str, str]]] = {
        # ... existing entries ...
        "bedrock:anthropic.claude-3-5-sonnet-20241022-v2:0": {
            "env_var": "AWS_ACCESS_KEY_ID",
            "friendly_name": "Bedrock Claude 3.5 Sonnet",
        },
        "bedrock:anthropic.claude-3-5-haiku-20241022-v1:0": {
            "env_var": "AWS_ACCESS_KEY_ID",
            "friendly_name": "Bedrock Claude 3.5 Haiku",
        },
        "bedrock:meta.llama3-70b-instruct-v1:0": {
            "env_var": "AWS_ACCESS_KEY_ID",
            "friendly_name": "Bedrock Llama 3 70B",
        },
        "bedrock:mistral.mistral-large-2402-v1:0": {
            "env_var": "AWS_ACCESS_KEY_ID",
            "friendly_name": "Bedrock Mistral Large",
        },
    }
```

> The `env_var` field is used for credential injection in `AnyAgentOrchestrator` — Bedrock needs `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` rather than a single API key. See step 3 below.

### (Recommended) Split into `access_key` and `secret_key`

The simplest approach for local dev: rely on `~/.aws/credentials` or env vars already set. If you want the sidebar UI to work for Bedrock too, modify the `execute_query` path so that when the model starts with `bedrock:`, it injects both keys.

A minimal patch in `agent.py`:

```python
if model_id.startswith("bedrock:"):
    # rely on existing env vars or ~/.aws/credentials
    pass
else:
    with cls.inject_credentials(env_var, api_key):
        ...
```

For a full TUI-driven approach, add two input fields (access key + secret key) when a Bedrock model is selected.

---

## 3. Dependencies

Add the LangChain AWS integration:

```bash
pip install langchain-aws
```

Or add to `pyproject.toml`:

```toml
dependencies = [
    # ... existing ...
    "langchain-aws>=0.1.0",
]
```

This package provides `ChatBedrock` which `any-agent` / LangChain uses under the hood when the model ID starts with `bedrock:`.

---

## 4. Running Locally

With credentials configured and `langchain-aws` installed:

```bash
AWS_DEFAULT_REGION=us-east-1 sqwakvox
```

Then in the TUI sidebar:
- Select e.g. **Bedrock Claude 3.5 Sonnet**
- Enter your **AWS Access Key ID** in the API key field (or leave blank if using `~/.aws/credentials`)

> **Note:** The current `api_key` field is single-value. For Bedrock you need both access key and secret key. The simplest workaround is to set `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` as environment variables before launch and skip the sidebar API key entirely for Bedrock models.

---

## 5. "Deploying" the TUI on AWS

Since this is a terminal UI, there is no traditional "deploy to ECS/Fargate" unless you pair it with a web wrapper. Options:

### Option A — EC2 instance (SSH + Tmux)

```bash
# Launch an EC2 instance (Amazon Linux 2023, t3.medium+)
ssh -L 5901:localhost:5901 ec2-user@<ip>

# Install Python, clone repo, install deps
sudo dnf install python3.12 git
git clone https://github.com/your-org/sqwakvox
cd sqwakvox
pip install -e . langchain-aws

# Run inside tmux so it persists
tmux new -s sqwakvox
sqwakvox
```

### Option B — AWS CloudShell

CloudShell includes AWS credentials automatically. You can run the TUI if your terminal emulator supports it:

```bash
git clone https://github.com/your-org/sqwakvox
cd sqwakvox
pip install -e . langchain-aws --user
python -m sqwakvox
```

### Option C — Web wrapper (not implemented)

If you want a *deployed web service*, Sqwakvox would need a web framework wrapper (FastAPI/Flask) to expose the chat endpoint over HTTP. The TUI layer (`textual`) would be replaced with a web frontend or an API-only mode. This is a significant refactor outside the current scope.

---

## 6. Cost Considerations

- Bedrock pricing is per-token (similar to direct API pricing).
- Claude 3.5 Sonnet via Bedrock costs **$3.00/M input tokens, $15.00/M output tokens** (us-east-1).
- Use Claude 3 Haiku for cheaper inference: **$0.25/M input, $1.25/M output**.
- No additional data-transfer costs if the app runs on EC2 in the same region as the Bedrock endpoint.

---

## Summary

| Requirement | Detail |
|-------------|--------|
| AWS account | With Bedrock model access enabled |
| IAM permissions | `bedrock:InvokeModel` on the target models |
| Credentials | `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY` + `AWS_DEFAULT_REGION` |
| Python package | `langchain-aws` (`pip install langchain-aws`) |
| Code change | Add Bedrock models to `ModelProvider.MAP` in `models.py` |
| UI note | Bedrock needs two credential fields; the simplest path is pre-set env vars |

The most practical "deployment" today is **EC2 + tmux** with your AWS credentials configured via IAM role, giving you a persistent terminal session running the TUI backed by Bedrock models.
