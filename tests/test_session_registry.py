"""Tests for the Sqwakvox session registry (session_registry.py)."""

from __future__ import annotations

from unittest.mock import patch

from sqwakvox import session_registry


class TestSessionRegistry:
    def test_publish_and_get_active_session(self) -> None:
        session_registry.clear_session()
        ok = session_registry.publish_active_document(
            doc_name="report_q3.pdf",
            source="/docs/report_q3.pdf",
            domain_id="financial",
            queue="sqwakvox.doc0",
            doc_context="Revenue was $10M.",
            data_store={"Revenue": "$10M"},
            table_count=3,
        )
        assert ok is True

        active = session_registry.get_active_session()
        assert active is not None
        assert active["active_document_name"] == "report_q3.pdf"
        assert active["domain_id"] == "financial"
        assert active["queue"] == "sqwakvox.doc0"
        assert active["table_count"] == 3

    def test_get_document_payload(self) -> None:
        session_registry.publish_active_document(
            doc_name="report_q3.pdf",
            source="/docs/report_q3.pdf",
            domain_id="financial",
            queue="sqwakvox.doc0",
            doc_context="Revenue was $10M.",
            data_store={"Revenue": "$10M"},
            table_count=3,
        )
        payload = session_registry.get_document_payload("report_q3.pdf")
        assert payload is not None
        assert payload["doc_context"] == "Revenue was $10M."
        assert payload["data_store"] == {"Revenue": "$10M"}

        # Default to active if doc_name not provided
        default_payload = session_registry.get_document_payload()
        assert default_payload is not None
        assert default_payload["active_document_name"] == "report_q3.pdf"

    def test_list_active_documents(self) -> None:
        session_registry.clear_session()
        session_registry.publish_active_document(
            doc_name="doc_a.pdf",
            source="/docs/doc_a.pdf",
            domain_id="financial",
            queue="sqwakvox.doc0",
            doc_context="A",
        )
        session_registry.publish_active_document(
            doc_name="doc_b.epub",
            source="/docs/doc_b.epub",
            domain_id="swe",
            queue="sqwakvox.doc1",
            doc_context="B",
        )
        docs = session_registry.list_active_documents()
        doc_names = [d["file_name"] for d in docs]
        assert "doc_a.pdf" in doc_names
        assert "doc_b.epub" in doc_names

    def test_clear_session(self) -> None:
        session_registry.publish_active_document(
            doc_name="clear_me.pdf",
            source="/docs/clear_me.pdf",
            domain_id="financial",
            queue="sqwakvox.doc0",
            doc_context="C",
        )
        assert session_registry.get_active_session() is not None

        session_registry.clear_session()
        assert session_registry.get_active_session() is None
        assert session_registry.list_active_documents() == []

    def test_graceful_degradation_on_redis_error(self) -> None:
        with patch("sqwakvox.session_registry.get_redis_client") as mock_client:
            mock_client.side_effect = ConnectionError("Redis is down")
            assert session_registry.publish_active_document("d", "s", "dom", "q", "ctx") is False
            assert session_registry.get_active_session() is None
            assert session_registry.get_document_payload() is None
            assert session_registry.list_active_documents() == []
            assert session_registry.clear_session() is False
            assert session_registry.is_redis_available() is False
