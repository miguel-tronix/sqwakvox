from __future__ import annotations

import logging
import os
import threading
from collections.abc import Generator
from contextlib import contextmanager

from any_agent import AgentConfig, AgentFramework
from any_agent import AnyAgent as AnyAgentLib

logger = logging.getLogger(__name__)


class AnyAgentOrchestrator:
    _lock = threading.Lock()

    @staticmethod
    @contextmanager
    def inject_credentials(env_var: str, api_key: str) -> Generator[None, None, None]:
        """Temporarily inject api key into environment securely using a process-wide lock.

        Ensures thread-safe environment variable injection during concurrent query executions.
        """
        with AnyAgentOrchestrator._lock:
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

        logger.info("Starting any-agent execution — model: %s", model_id)
        with cls.inject_credentials(env_var, api_key):
            agent = AnyAgentLib.create(
                agent_framework=AgentFramework("langchain"),
                agent_config=config,
            )
            trace = agent.run(prompt)
            response = str(trace.final_output)
            logger.info("Agent execution complete — response length: %d chars", len(response))
            return response
