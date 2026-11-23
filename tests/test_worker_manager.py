"""Tests for the managed Celery worker subprocesses."""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from sqwakvox.worker_manager import (
    DISABLE_ENV,
    DOCLING_QUEUE,
    DOCLING_WORKER_CONCURRENCY,
    WorkerManager,
    managed_workers_enabled,
)

_REAL_POPEN = subprocess.Popen


@pytest.fixture
def manager(tmp_path):
    """A WorkerManager writing logs to a temp dir, with no real spawns."""
    mgr = WorkerManager(log_dir=tmp_path)
    yield mgr
    mgr.stop_all()


def _fake_proc() -> MagicMock:
    # NB: reference the real Popen class captured at import time — inside
    # tests ``subprocess.Popen`` is patched out with a Mock, and mocks
    # cannot be used as a spec for other mocks.
    proc = MagicMock(spec=_REAL_POPEN)
    proc.pid = 4242
    proc.poll.return_value = None  # running
    return proc


def test_managed_workers_enabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(DISABLE_ENV, raising=False)
    assert managed_workers_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "False"])
def test_managed_workers_disabled_via_env(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(DISABLE_ENV, value)
    assert managed_workers_enabled() is False


@patch("sqwakvox.worker_manager.subprocess.Popen")
def test_ensure_worker_spawns_subprocess_with_queue(
    mock_popen: MagicMock, manager: WorkerManager
) -> None:
    mock_popen.return_value = _fake_proc()

    spawned = manager.ensure_worker("sqwakvox.doc0")

    assert spawned is True
    argv = mock_popen.call_args.args[0]
    assert "sqwakvox.run_worker" in " ".join(argv)
    assert "--queue" in argv
    assert argv[argv.index("--queue") + 1] == "sqwakvox.doc0"
    # Per-document workers must never run the beat scheduler.
    assert "--no-beat" in argv
    assert manager.queues == ["sqwakvox.doc0"]


@patch("sqwakvox.worker_manager.subprocess.Popen")
def test_ensure_worker_is_idempotent_while_running(
    mock_popen: MagicMock, manager: WorkerManager
) -> None:
    mock_popen.return_value = _fake_proc()

    assert manager.ensure_worker("sqwakvox.doc0") is True
    assert manager.ensure_worker("sqwakvox.doc0") is False
    assert mock_popen.call_count == 1


@patch("sqwakvox.worker_manager.subprocess.Popen")
def test_dead_worker_is_respawned(mock_popen: MagicMock, manager: WorkerManager) -> None:
    proc = _fake_proc()
    mock_popen.return_value = proc

    manager.ensure_worker("sqwakvox.doc0")
    proc.poll.return_value = 1  # died

    assert manager.ensure_worker("sqwakvox.doc0") is True
    assert mock_popen.call_count == 2


@patch("sqwakvox.worker_manager.subprocess.Popen")
def test_spawn_failure_returns_false(mock_popen: MagicMock, manager: WorkerManager) -> None:
    mock_popen.side_effect = OSError("no fork for you")

    assert manager.ensure_worker("sqwakvox.doc0") is False
    assert manager.queues == []


@patch("sqwakvox.worker_manager.subprocess.Popen")
def test_ensure_docling_worker_spawns_shared_queue(
    mock_popen: MagicMock, manager: WorkerManager
) -> None:
    """The Docling ingest worker is a single process on the shared queue."""
    mock_popen.return_value = _fake_proc()

    spawned = manager.ensure_docling_worker()

    assert spawned is True
    argv = mock_popen.call_args.args[0]
    assert "sqwakvox.run_worker" in " ".join(argv)
    assert argv[argv.index("--queue") + 1] == DOCLING_QUEUE
    assert argv[argv.index("--concurrency") + 1] == str(DOCLING_WORKER_CONCURRENCY)
    assert "--no-beat" in argv
    assert manager.queues == [DOCLING_QUEUE]


@patch("sqwakvox.worker_manager.subprocess.Popen")
def test_ensure_docling_worker_is_singleton(mock_popen: MagicMock, manager: WorkerManager) -> None:
    """No matter how many document tabs open, only one Docling worker spawns."""
    mock_popen.return_value = _fake_proc()

    assert manager.ensure_docling_worker() is True
    assert manager.ensure_docling_worker() is False
    assert manager.ensure_docling_worker() is False
    assert mock_popen.call_count == 1


@patch("sqwakvox.worker_manager.subprocess.Popen")
def test_stop_all_terminates_running_workers(
    mock_popen: MagicMock, manager: WorkerManager
) -> None:
    procs = [_fake_proc(), _fake_proc()]
    mock_popen.side_effect = procs

    manager.ensure_worker("sqwakvox.doc0")
    manager.ensure_worker("sqwakvox.doc1")
    manager.stop_all()

    for proc in procs:
        proc.terminate.assert_called_once()
    assert manager.queues == []


@patch("sqwakvox.worker_manager.subprocess.Popen")
def test_stop_all_escapes_to_kill(mock_popen: MagicMock, manager: WorkerManager) -> None:
    proc = _fake_proc()
    proc.wait.side_effect = [subprocess.TimeoutExpired(cmd="w", timeout=5), 0]
    mock_popen.return_value = proc

    manager.ensure_worker("sqwakvox.doc0")
    manager.stop_all(timeout=0)

    proc.kill.assert_called_once()


@patch("sqwakvox.worker_manager.subprocess.Popen")
def test_one_worker_per_document_tab(mock_popen: MagicMock, manager: WorkerManager) -> None:
    """Each document tab gets its own queue and its own worker process."""
    procs = [_fake_proc() for _ in range(3)]
    mock_popen.side_effect = procs

    queues = [f"sqwakvox.doc{i}" for i in range(3)]
    for q in queues:
        manager.ensure_worker(q)

    assert manager.queues == queues
    assert mock_popen.call_count == 3


@patch("sqwakvox.worker_manager.subprocess.Popen")
def test_cloud_run_streams_worker_logs_to_stderr(
    mock_popen: MagicMock, manager: WorkerManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    mock_popen.return_value = _fake_proc()
    monkeypatch.setenv("K_SERVICE", "sqwakvox-web")

    assert manager.ensure_worker("sqwakvox.doc0") is True
    kwargs = mock_popen.call_args.kwargs
    assert kwargs["stdout"] is sys.stderr
    assert kwargs["stderr"] is sys.stderr
