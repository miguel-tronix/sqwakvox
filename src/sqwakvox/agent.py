from __future__ import annotations

import os
from collections.abc import Generator
from contextlib import contextmanager

from any_agent import AgentConfig, AgentFramework
from any_agent import AnyAgent as AnyAgentLib


class AnyAgentOrchestrator:
    @staticmethod
    @contextmanager
    def inject_credentials(env_var: str, api_key: str) -> Generator[None, None, None]:
        original_val = os.environ.get(env_var)
        os.environ[env_var] = api_key
        try:
            yield
        finally:
            if original_val is None:
                os.environ.pop(env_var, None)
            else:
                os.environ[env_var] = original_val

    @classmethod
    def execute_query(
        cls,
        model_id: str,
        api_key: str,
        context: str,
        prompt: str,
        env_var: str,
    ) -> str:
        instructions = (
            f"You are a helpful Financial Document Assistant.\n"
            f"Always ground your answers in the document context provided below.\n\n"
            f"--- DOCUMENT CONTEXT ---\n{context}\n------------------------"
        )

        config = AgentConfig(
            model_id=model_id,
            instructions=instructions,
        )

        with cls.inject_credentials(env_var, api_key):
            agent = AnyAgentLib.create(
                framework=AgentFramework("langchain"),
                config=config,
            )
            result: str = agent.run(prompt)
            return result
