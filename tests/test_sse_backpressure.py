"""Mission 23 Task 3B: SSE keep-alive reliability, back-pressure and resource bounds.

``tests/test_event_streaming_api.py`` covers the SSE wire contract, but it drives
the application through ``starlette.testclient.TestClient``, which buffers the
whole response body before returning. That makes it impossible to observe what
happens *while* a stream is open — which is exactly where this task's questions
live: does a keep-alive still arrive when one client's query is slow, does a
paused consumer block anyone else, does a disconnect release the stream, does
shutdown complete with connections active.

This module therefore adds a small deterministic harness instead of relying on
sleeps:

``EventLoopHarness`` runs the real application on one private event loop in a
background thread and drives it over raw ASGI — the same single loop a real
server multiplexes the lifespan and every response onto. A ``_ticker`` task
increments a counter every few milliseconds, so tests can ask "did the loop make
progress while X was happening?" by counting ticks instead of sleeping.

``SSEStream`` is one connection on that loop. Its ``receive`` only reports a
disconnect once the test asks for one, and its ``send`` can be gated closed so a
write provably cannot complete — a slow consumer, reproduced without timing.

``HarnessRepository`` is in-memory event history with one extra knob: a named
investigation whose queries block until the test releases them. ``threading``
events, not sleeps, are the synchronisation points throughout.

Scenarios covered: concurrent clients, keep-alives while idle, slow consumers,
disconnect during a keep-alive, disconnect while waiting for an event, shutdown
with live connections, and delivery resuming correctly after an idle period.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from api.app import create_app
from core.domain.models import IncidentStatus
from core.domain.state import InvestigationState
from core.persistence.outbox import OutboxEvent
from core.persistence.repository import PersistenceRepository


# ---------------------------------------------------------------------------
# Deterministic test doubles
# ---------------------------------------------------------------------------

def _make_state(
    investigation_id: str,
    status: IncidentStatus = IncidentStatus.RUNNING,
) -> InvestigationState:
    state = InvestigationState(incident_id=investigation_id)
    state.status = status
    return state


def _make_event(
    event_id: str,
    investigation_id: str = "INV-001",
    created_at: str = "2026-10-10T18:00:00Z",
    event_type: str = "StageStarted",
) -> OutboxEvent:
    return OutboxEvent(
        event_id=event_id,
        investigation_id=investigation_id,
        event_type=event_type,
        payload={"stage": "logs"},
        status="pending",
        created_at=created_at,
    )


def _sse_ids(text: str) -> List[str]:
    """Event ids in the order they appear on the wire."""
    return [
        line.split(":", 1)[1].strip()
        for line in text.splitlines()
        if line.startswith("id:")
    ]


class HarnessRepository(PersistenceRepository):
    """In-memory event history with a controllable slow query.

    ``arm_slow_query`` makes every ``list_events`` call for one investigation
    block until ``release_slow_query`` is called, and announces itself through
    ``block_started``. That gives tests a hard synchronisation point — "this
    query is provably in flight and has not returned" — so no sleep is needed to
    decide whether other work progressed meanwhile.
    """

    def __init__(
        self,
        states: Optional[Dict[str, InvestigationState]] = None,
        events: Optional[List[OutboxEvent]] = None,
        supports_history: bool = True,
    ) -> None:
        self._states: Dict[str, InvestigationState] = dict(states or {})
        self._events: List[OutboxEvent] = list(events or [])
        self._supports_history = supports_history
        self._data_lock = threading.RLock()
        self._cond = threading.Condition()
        self._list_events_calls = 0

        self.slow_investigation: Optional[str] = None
        self.block_active = threading.Event()
        self.block_started = threading.Event()
        self.block_finished = threading.Event()
        self.block_released = threading.Event()
        self.block_timeout = 10.0

        self.close_calls = 0

    # -- controllable slow query -------------------------------------------

    def arm_slow_query(self, investigation_id: str, block_timeout: float = 10.0) -> None:
        self.slow_investigation = investigation_id
        self.block_timeout = block_timeout
        self.block_started.clear()
        self.block_finished.clear()
        self.block_released.clear()
        self.block_active.set()

    def release_slow_query(self) -> None:
        self.block_released.set()

    # -- observation helpers ------------------------------------------------

    @property
    def list_calls(self) -> int:
        with self._cond:
            return self._list_events_calls

    def wait_for_list_calls(self, count: int, timeout: float = 5.0) -> bool:
        with self._cond:
            return self._cond.wait_for(lambda: self._list_events_calls >= count, timeout)

    # -- PersistenceRepository ---------------------------------------------

    @property
    def supports_event_history(self) -> bool:
        return self._supports_history

    def save(self, state: InvestigationState, events: Optional[List[Any]] = None) -> None:
        with self._data_lock:
            self._states[state.incident_id] = state
            for ev in events or []:
                if isinstance(ev, OutboxEvent):
                    self._events.append(ev)

    def load(self, investigation_id: str) -> Optional[InvestigationState]:
        with self._data_lock:
            return self._states.get(investigation_id)

    def exists(self, investigation_id: str) -> bool:
        with self._data_lock:
            return investigation_id in self._states

    def delete(self, investigation_id: str) -> None:
        with self._data_lock:
            self._states.pop(investigation_id, None)
            self._events = [e for e in self._events if e.investigation_id != investigation_id]

    def list(self) -> List[str]:
        with self._data_lock:
            return sorted(self._states.keys())

    def list_events(
        self,
        investigation_id: str,
        limit: int = 200,
        after_cursor: Optional[Tuple[str, str]] = None,
    ) -> List[OutboxEvent]:
        with self._cond:
            self._list_events_calls += 1
            self._cond.notify_all()

        if self.slow_investigation == investigation_id and self.block_active.is_set():
            self.block_started.set()
            self.block_released.wait(timeout=self.block_timeout)
            self.block_finished.set()

        if not self._supports_history:
            return []

        with self._data_lock:
            matching = [e for e in self._events if e.investigation_id == investigation_id]

        matching.sort(key=lambda e: (str(e.created_at or ""), e.event_id or ""))
        if after_cursor is not None:
            c_ts, c_eid = after_cursor
            matching = [
                e
                for e in matching
                if (str(e.created_at or ""), e.event_id or "") > (c_ts, c_eid)
            ]
        return matching[:limit]

    def add_event(self, event: OutboxEvent) -> None:
        with self._data_lock:
            self._events.append(event)

    def close(self) -> None:
        self.close_calls += 1


# ---------------------------------------------------------------------------
# Raw-ASGI harness
# ---------------------------------------------------------------------------

class EventLoopHarness:
    """Runs the real ASGI app on one private event loop in a background thread.

    Both the lifespan and every stream are driven on this loop, mirroring a real
    server where a single loop serves all clients and the application lifecycle.
    """
    TICK_INTERVAL = 0.005

    def __init__(self, app: Any) -> None:
        self.app = app
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="sse-harness-loop", daemon=True
        )
        self._streams: List["SSEStream"] = []

        # Loop responsiveness instrumentation.
        self._tick_cond = threading.Condition()
        self._tick_count = 0

        # Lifespan plumbing.
        self._lifespan_in: Optional["asyncio.Queue[Dict[str, Any]]"] = None
        self._lifespan_future: Any = None
        self._startup_done = threading.Event()
        self._shutdown_done = threading.Event()
        self._lifespan_failures: List[str] = []

    # -- lifecycle ----------------------------------------------------------

    def start(self, timeout: float = 10.0) -> "EventLoopHarness":
        self._thread.start()
        assert self._ready.wait(timeout=timeout), "harness loop did not start"
        return self

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.create_task(self._ticker())
        self._ready.set()
        self._loop.run_forever()
        # Drain whatever is left so the loop shuts down without orphaned tasks.
        pending = asyncio.all_tasks(self._loop)
        for task in pending:
            task.cancel()
        if pending:
            self._loop.run_until_complete(
                asyncio.gather(*pending, return_exceptions=True)
            )
        self._loop.close()

    async def _ticker(self) -> None:
        """Advance a counter every few milliseconds whenever the loop is free."""
        while True:
            with self._tick_cond:
                self._tick_count += 1
                self._tick_cond.notify_all()
            await asyncio.sleep(self.TICK_INTERVAL)

    def tick_count(self) -> int:
        with self._tick_cond:
            return self._tick_count

    def wait_for_ticks(self, count: int, timeout: float, base: Optional[int] = None) -> bool:
        """True once the loop has completed *count* more ticks than *base*."""
        target = (self.tick_count() if base is None else base) + count
        deadline = time.monotonic() + timeout
        with self._tick_cond:
            while self._tick_count < target:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._tick_cond.wait(remaining)
            return True

    def _call_soon_threadsafe(self, func: Callable[..., None], *args: Any) -> None:
        with contextlib.suppress(RuntimeError):
            self._loop.call_soon_threadsafe(func, *args)

    def submit(self, coro: Any) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    # -- lifespan -----------------------------------------------------------

    def start_lifespan(self, timeout: float = 10.0) -> None:
        queue: "asyncio.Queue[Dict[str, Any]]" = asyncio.Queue()
        self._lifespan_in = queue

        async def receive() -> Dict[str, Any]:
            return await queue.get()

        async def send(message: Dict[str, Any]) -> None:
            kind = message["type"]
            if kind in ("lifespan.startup.complete", "lifespan.startup.failed"):
                if kind.endswith("failed"):
                    self._lifespan_failures.append(str(message.get("message", "")))
                self._startup_done.set()
            elif kind in ("lifespan.shutdown.complete", "lifespan.shutdown.failed"):
                if kind.endswith("failed"):
                    self._lifespan_failures.append(str(message.get("message", "")))
                self._shutdown_done.set()

        scope = {"type": "lifespan", "asgi": {"version": "3.0", "spec_version": "2.3"}}
        self._lifespan_future = self.submit(self.app(scope, receive, send))
        self._push_lifespan({"type": "lifespan.startup"})
        assert self._startup_done.wait(timeout=timeout), "lifespan startup did not complete"
        assert not self._lifespan_failures, f"lifespan startup failed: {self._lifespan_failures}"

    def _push_lifespan(self, message: Dict[str, Any]) -> None:
        queue = self._lifespan_in
        assert queue is not None, "lifespan was never started"
        self._call_soon_threadsafe(queue.put_nowait, message)

    def stop_lifespan(self, timeout: float = 10.0) -> Optional[float]:
        """Request shutdown; returns the elapsed time, or None if it never finished."""
        started = time.monotonic()
        self._push_lifespan({"type": "lifespan.shutdown"})
        if not self._shutdown_done.wait(timeout=timeout):
            return None
        return time.monotonic() - started

    # -- streams ------------------------------------------------------------

    def open_stream(
        self,
        path: str,
        *,
        paused: bool = False,
        headers: Optional[List[Tuple[bytes, bytes]]] = None,
        query_string: bytes = b"",
    ) -> "SSEStream":
        stream = SSEStream(self, path, headers=headers, query_string=query_string)
        self._streams.append(stream)
        return stream.start(paused=paused)

    def disconnect_all(self) -> None:
        for stream in list(self._streams):
            stream.disconnect()

    def close(self, timeout: float = 10.0) -> None:
        self.disconnect_all()
        for stream in list(self._streams):
            stream.wait_finished(timeout=timeout)
        if self._lifespan_future is not None and not self._shutdown_done.is_set():
            self.stop_lifespan(timeout=timeout)
        for stream in list(self._streams):
            stream.cancel()
        self._call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=timeout)


class SSEStream:
    """One SSE connection driven against the app on the harness loop."""

    def __init__(
        self,
        harness: EventLoopHarness,
        path: str,
        *,
        headers: Optional[List[Tuple[bytes, bytes]]] = None,
        query_string: bytes = b"",
    ) -> None:
        self._harness = harness
        self.path = path
        self.errors: List[BaseException] = []

        self._cond = threading.Condition()
        self._body = bytearray()
        self._status: Optional[int] = None
        self._response_headers: Dict[str, str] = {}
        self._started = threading.Event()
        self._completed = threading.Event()

        # Blocked-write evidence: set from inside ``send`` while a write cannot
        # proceed, so a test can prove back-pressure without guessing.
        self.writes_blocked = threading.Event()

        self._request_sent = False
        self._disconnected = False
        self._disconnect = asyncio.Event()
        self._writes_allowed = asyncio.Event()
        self._future: Any = None

        self._scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": query_string,
            "root_path": "",
            "headers": headers or [(b"accept", b"text/event-stream")],
            "server": ("testserver", 80),
            "client": ("testclient", 12345),
        }

    # -- control ------------------------------------------------------------

    def start(self, *, paused: bool = False) -> "SSEStream":
        if not paused:
            self._writes_allowed.set()
        self._future = self._harness.submit(self._drive())
        return self

    async def _drive(self) -> None:
        try:
            await self._harness.app(self._scope, self._receive, self._send)
        except BaseException as exc:  # noqa: BLE001 - surfaced to the test
            self.errors.append(exc)
        finally:
            self._completed.set()

    def disconnect(self) -> None:
        if self._disconnected:
            return
        self._disconnected = True
        self._harness._call_soon_threadsafe(self._disconnect.set)

    def resume_writes(self) -> None:
        self._harness._call_soon_threadsafe(self._writes_allowed.set)

    def cancel(self) -> None:
        if self._future is not None:
            self._future.cancel()

    # -- ASGI callables -----------------------------------------------------

    async def _receive(self) -> Dict[str, Any]:
        if not self._request_sent:
            self._request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        if self._disconnect.is_set():
            return {"type": "http.disconnect"}
        # No message available: park so ``Request.is_disconnected`` treats this
        # as "nothing to report" and the streaming task keeps running.
        await asyncio.Event().wait()
        return {"type": "http.request", "body": b"", "more_body": False}

    async def _send(self, message: Dict[str, Any]) -> None:
        kind = message["type"]
        if kind == "http.response.start":
            with self._cond:
                self._status = message["status"]
                self._response_headers = {
                    k.decode("latin-1").lower(): v.decode("latin-1")
                    for k, v in message.get("headers", [])
                }
                self._cond.notify_all()
            self._started.set()
        elif kind == "http.response.body":
            if not self._writes_allowed.is_set():
                self.writes_blocked.set()
            # A consumer that stops reading applies back-pressure here.
            await self._writes_allowed.wait()
            body = message.get("body", b"")
            if body:
                with self._cond:
                    self._body.extend(body)
                    self._cond.notify_all()
            if not message.get("more_body", False):
                with self._cond:
                    self._cond.notify_all()

    # -- observation --------------------------------------------------------

    def wait_headers(self, timeout: float = 10.0) -> int:
        assert self._started.wait(timeout=timeout), "no response start within timeout"
        with self._cond:
            return int(self._status)

    @property
    def headers(self) -> Dict[str, str]:
        with self._cond:
            return dict(self._response_headers)

    def _raw_text(self) -> str:
        # Callers must already hold ``self._cond``.
        return self._body.decode("utf-8", errors="replace")

    def text(self) -> str:
        with self._cond:
            return self._raw_text()

    def keepalive_count(self) -> int:
        with self._cond:
            return self._raw_text().count(": keep-alive")

    def wait_until(self, predicate: Callable[[str], bool], timeout: float = 5.0) -> bool:
        """Wait until *predicate* accepts the accumulated body text.

        The predicate is evaluated with the condition lock held and receives the
        raw text, so it must not call back into this object's accessors.
        """
        with self._cond:
            return self._cond.wait_for(lambda: predicate(self._raw_text()), timeout)

    def wait_for_more_keepalives(
        self, baseline: int, extra: int = 1, timeout: float = 5.0
    ) -> bool:
        return self.wait_until(
            lambda text: text.count(": keep-alive") >= baseline + extra, timeout=timeout
        )

    def wait_finished(self, timeout: float = 10.0) -> bool:
        return self._completed.wait(timeout=timeout)


@contextlib.contextmanager
def running_app(app: Any, repository: HarnessRepository) -> Iterator[EventLoopHarness]:
    """Run *app* (lifespan included) on a private loop, then tear it down."""
    harness = EventLoopHarness(app).start()
    try:
        harness.start_lifespan()
        yield harness
    finally:
        harness.disconnect_all()
        # Never leave a harness thread parked behind an armed slow query.
        repository.release_slow_query()
        harness.close()


def _streaming_app(repository: HarnessRepository, **state: Any) -> Any:
    app = create_app(repository=repository)
    for name, value in state.items():
        setattr(app.state, name, value)
    return app


# ===========================================================================
# 1. Multiple concurrent clients
# ===========================================================================

def test_concurrent_clients_each_receive_the_full_history_in_order():
    """Several live connections on one investigation all get every event once."""
    events = [
        _make_event(f"evt_{i}", created_at=f"2026-10-10T18:00:0{i}Z")
        for i in range(1, 6)
    ]
    repo = HarnessRepository(states={"INV-001": _make_state("INV-001")}, events=events)
    app = _streaming_app(repo, sse_poll_interval=0.01, sse_keepalive_interval=60.0)

    expected = [f"evt_{i}" for i in range(1, 6)]

    with running_app(app, repo) as harness:
        streams = [
            harness.open_stream("/investigations/INV-001/events") for _ in range(4)
        ]
        for stream in streams:
            assert stream.wait_headers() == 200
            assert "text/event-stream" in stream.headers.get("content-type", "")
            assert stream.wait_until(lambda text: _sse_ids(text) == expected), (
                "a concurrent client did not receive the complete history"
            )

        for stream in streams:
            # Complete, ordered and non-duplicated for every connection.
            assert _sse_ids(stream.text()) == expected

        # Each connection is an independent stream, so none of them is terminal
        # yet: the investigation is still running.
        assert repo.list_calls >= len(streams)

        for stream in streams:
            stream.disconnect()
        for stream in streams:
            assert stream.wait_finished(timeout=5.0)
            assert stream.errors == []


# ===========================================================================
# 2. Keep-alives while no events are available
# ===========================================================================

def test_keepalives_are_emitted_while_no_events_exist():
    """An idle stream stays alive with comment frames and invents no events."""
    repo = HarnessRepository(states={"INV-IDLE": _make_state("INV-IDLE")})
    app = _streaming_app(repo, sse_poll_interval=0.01, sse_keepalive_interval=0.02)

    with running_app(app, repo) as harness:
        stream = harness.open_stream("/investigations/INV-IDLE/events")
        assert stream.wait_headers() == 200

        assert stream.wait_for_more_keepalives(0, extra=3, timeout=5.0), (
            "no keep-alive comments while the stream was idle"
        )
        # Keep-alives must not be mistaken for (or fabricate) events.
        assert _sse_ids(stream.text()) == []
        assert repo.list_calls >= 2, "the stream was not polling while idle"

        stream.disconnect()
        assert stream.wait_finished(timeout=5.0)
        assert stream.errors == []


# ===========================================================================
# 3. Slow repository queries must not stall the shared event loop
# ===========================================================================

def test_slow_repository_call_does_not_starve_the_event_loop():
    """A query blocking in the persistence backend must not freeze the loop.

    The repository call is held open from inside the backend, and the harness
    ticker is the measuring device: if the loop can tick while that call is
    provably in flight, the blocking work is not being done on the loop.
    """
    repo = HarnessRepository(states={"INV-SLOW": _make_state("INV-SLOW")})
    app = _streaming_app(repo, sse_poll_interval=0.01, sse_keepalive_interval=60.0)

    with running_app(app, repo) as harness:
        repo.arm_slow_query("INV-SLOW")
        stream = harness.open_stream("/investigations/INV-SLOW/events")
        assert stream.wait_headers() == 200

        # Deterministic: the query is running and has not returned.
        assert repo.block_started.wait(timeout=5.0)
        assert not repo.block_finished.is_set()

        base = harness.tick_count()
        assert harness.wait_for_ticks(3, timeout=5.0, base=base), (
            "the event loop stopped making progress while a repository call was blocked"
        )
        assert not repo.block_finished.is_set(), (
            "the repository call returned before the loop was observed to progress"
        )


def test_slow_repository_call_does_not_delay_another_clients_keepalive():
    """One investigation's slow query must not postpone an unrelated client."""
    repo = HarnessRepository(
        states={
            "INV-FAST": _make_state("INV-FAST"),
            "INV-SLOW": _make_state("INV-SLOW"),
        }
    )
    app = _streaming_app(repo, sse_poll_interval=0.01, sse_keepalive_interval=0.02)

    with running_app(app, repo) as harness:
        slow = harness.open_stream("/investigations/INV-SLOW/events")
        fast = harness.open_stream("/investigations/INV-FAST/events")
        assert slow.wait_headers() == 200
        assert fast.wait_headers() == 200

        repo.arm_slow_query("INV-SLOW")
        assert repo.block_started.wait(timeout=5.0)
        assert not repo.block_finished.is_set()

        baseline = fast.keepalive_count()
        assert fast.wait_for_more_keepalives(baseline, extra=1, timeout=5.0), (
            "an unrelated client's keep-alive was delayed by another client's slow query"
        )
        assert not repo.block_finished.is_set()


# ===========================================================================
# 4. Slow consumers and back-pressure
# ===========================================================================

def test_paused_consumer_does_not_block_others_and_resumes_without_loss():
    """A consumer that stops reading stalls only its own connection."""
    repo = HarnessRepository(
        states={"INV-001": _make_state("INV-001")},
        events=[_make_event("evt_1", created_at="2026-10-10T18:00:01Z")],
    )
    app = _streaming_app(repo, sse_poll_interval=0.01, sse_keepalive_interval=60.0)

    with running_app(app, repo) as harness:
        # This connection's writes can never complete until it is resumed.
        stalled = harness.open_stream("/investigations/INV-001/events", paused=True)
        healthy = harness.open_stream("/investigations/INV-001/events")
        assert stalled.wait_headers() == 200
        assert healthy.wait_headers() == 200

        assert stalled.writes_blocked.wait(timeout=5.0), (
            "the paused consumer never attempted a write"
        )
        # Provably nothing has been written to the stalled connection...
        assert stalled.text() == ""

        # ...while the unrelated client keeps streaming normally.
        assert healthy.wait_until(
            lambda text: _sse_ids(text) == ["evt_1"], timeout=5.0
        )
        repo.add_event(_make_event("evt_2", created_at="2026-10-10T18:00:02Z"))
        assert healthy.wait_until(
            lambda text: _sse_ids(text) == ["evt_1", "evt_2"], timeout=5.0
        )
        assert stalled.text() == "", "a stalled consumer received data it was not reading"

        # Releasing the consumer drains the whole backlog, in canonical order.
        stalled.resume_writes()
        assert stalled.wait_until(
            lambda text: _sse_ids(text) == ["evt_1", "evt_2"], timeout=5.0
        ), "buffered frames were lost when the consumer resumed"
        assert stalled.errors == []


# ===========================================================================
# 5. Disconnects release the stream
# ===========================================================================

def test_disconnect_during_an_idle_keepalive_releases_the_stream():
    """Disconnecting between keep-alives stops the polling loop and frees it."""
    repo = HarnessRepository(states={"INV-IDLE": _make_state("INV-IDLE")})
    app = _streaming_app(repo, sse_poll_interval=0.01, sse_keepalive_interval=0.02)

    with running_app(app, repo) as harness:
        stream = harness.open_stream("/investigations/INV-IDLE/events")
        assert stream.wait_headers() == 200
        assert stream.wait_for_more_keepalives(0, extra=2, timeout=5.0)

        assert repo.list_calls >= 2
        stream.disconnect()
        assert stream.wait_finished(timeout=5.0), "the stream was not released"
        assert stream.errors == []

        settled = repo.list_calls
        # Allow one query that was already submitted to the executor to land;
        # anything more would mean the stream kept polling after the disconnect.
        assert not repo.wait_for_list_calls(settled + 2, timeout=0.3), (
            "the disconnected stream kept querying the repository"
        )


def test_disconnect_while_waiting_for_an_event_releases_the_stream():
    """Disconnecting with nothing to deliver must still terminate promptly."""
    repo = HarnessRepository(states={"INV-IDLE": _make_state("INV-IDLE")})
    app = _streaming_app(repo, sse_poll_interval=0.01, sse_keepalive_interval=60.0)

    with running_app(app, repo) as harness:
        stream = harness.open_stream("/investigations/INV-IDLE/events")
        assert stream.wait_headers() == 200

        # Two queries prove the stream is genuinely in its empty-poll waiting
        # state rather than still starting up.
        assert repo.wait_for_list_calls(2, timeout=5.0), "the stream never polled"
        assert _sse_ids(stream.text()) == []

        stream.disconnect()
        assert stream.wait_finished(timeout=5.0), "the stream was not released"
        assert stream.errors == []

        settled = repo.list_calls
        assert not repo.wait_for_list_calls(settled + 2, timeout=0.3), (
            "the disconnected stream kept querying the repository"
        )


# ===========================================================================
# 6. Application shutdown with active connections
# ===========================================================================

def test_lifespan_shutdown_completes_with_active_and_stalled_connections():
    """Shutdown runs to completion on the shared loop even with clients blocked."""
    repo = HarnessRepository(
        states={
            "INV-LIVE": _make_state("INV-LIVE"),
            "INV-SLOW": _make_state("INV-SLOW"),
        }
    )
    app = _streaming_app(repo, sse_poll_interval=0.01, sse_keepalive_interval=0.02)

    with running_app(app, repo) as harness:
        live = harness.open_stream("/investigations/INV-LIVE/events")
        assert live.wait_headers() == 200
        assert live.wait_for_more_keepalives(0, extra=1, timeout=5.0)

        repo.arm_slow_query("INV-SLOW")
        stalled = harness.open_stream("/investigations/INV-SLOW/events")
        assert stalled.wait_headers() == 200
        assert repo.block_started.wait(timeout=5.0)
        assert not repo.block_finished.is_set()

        elapsed = harness.stop_lifespan(timeout=5.0)
        assert elapsed is not None, (
            "lifespan shutdown never completed with connections active"
        )
        assert elapsed < 5.0
        assert repo.close_calls == 1, "the repository was not released on shutdown"
        assert getattr(app.state, "shutdown_complete", False) is True

        repo.release_slow_query()


# ===========================================================================
# 7. Delivery after an idle period
# ===========================================================================

def test_delivery_resumes_correctly_after_an_idle_period():
    """Events published after a quiet stretch arrive in order, in wire format."""
    repo = HarnessRepository(states={"INV-LATE": _make_state("INV-LATE")})
    app = _streaming_app(repo, sse_poll_interval=0.01, sse_keepalive_interval=0.02)

    with running_app(app, repo) as harness:
        stream = harness.open_stream("/investigations/INV-LATE/events")
        assert stream.wait_headers() == 200

        # Establish that the connection really was idle first: nothing to send,
        # two keep-alives already delivered.
        assert stream.wait_for_more_keepalives(0, extra=2, timeout=5.0)
        assert _sse_ids(stream.text()) == []

        repo.add_event(
            _make_event("evt_a", "INV-LATE", created_at="2026-10-10T18:00:01Z")
        )
        assert stream.wait_until(lambda text: _sse_ids(text) == ["evt_a"], timeout=5.0)

        repo.add_event(
            _make_event("evt_b", "INV-LATE", created_at="2026-10-10T18:00:02Z")
        )
        assert stream.wait_until(
            lambda text: _sse_ids(text) == ["evt_a", "evt_b"], timeout=5.0
        )

        # The wire format is unchanged: exactly one id/event/data frame per event.
        text = stream.text()
        assert text.count("event: StageStarted") == 2
        assert "id: evt_a" in text and "id: evt_b" in text
        assert text.count("\n\n") >= 2

        # And the connection goes idle again without being closed.
        baseline = stream.keepalive_count()
        assert stream.wait_for_more_keepalives(baseline, extra=1, timeout=5.0)

        stream.disconnect()
        assert stream.wait_finished(timeout=5.0)
        assert stream.errors == []
