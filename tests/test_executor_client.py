"""Round-trip test: Python ExecutorClient writes NDJSON over a UDS,
listener parses it back into the same Signal."""

from __future__ import annotations

import json
import os
import socket
import threading
import time

import pytest

from src.executor_client import ExecutorClient, Signal


def _start_listener(path: str, sink: list, stop: threading.Event) -> threading.Thread:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    if os.path.exists(path):
        os.remove(path)
    sock.bind(path)
    sock.listen(4)
    sock.settimeout(0.2)

    def loop() -> None:
        while not stop.is_set():
            try:
                conn, _ = sock.accept()
            except TimeoutError:
                continue
            buf = b""
            conn.settimeout(0.2)
            while not stop.is_set():
                try:
                    chunk = conn.recv(4096)
                except TimeoutError:
                    continue
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, _, buf = buf.partition(b"\n")
                    if line.strip():
                        sink.append(json.loads(line))

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t


@pytest.mark.timeout(5)
def test_signal_roundtrip(tmp_path):
    sock_path = str(tmp_path / "exec.sock")
    sink: list = []
    stop = threading.Event()
    listener = _start_listener(sock_path, sink, stop)
    try:
        client = ExecutorClient(socket_path=sock_path, control_port=0)
        sig = Signal(
            id="t1", kind="open", outcome_asset=42, side="Y",
            px="0.55", notional_usd=2500.0, perp_delta_btc=-0.07,
            perp_asset=0, ts_ms=client.now_ms(), ttl_ms=5_000,
        )
        client.send(sig)
        client.send(sig)
        for _ in range(50):
            if len(sink) >= 2:
                break
            time.sleep(0.02)
        assert len(sink) == 2
        assert sink[0]["id"] == "t1"
        assert sink[0]["notional_usd"] == 2500.0
        assert sink[0]["side"] == "Y"
        assert sink[0]["perp_delta_btc"] == -0.07
        client.close()
    finally:
        stop.set()
        listener.join(timeout=1)
