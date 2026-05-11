"""Python client for the Rust execution daemon.

Hot path:
    `ExecutorClient.send(signal)` writes one NDJSON line to the Unix
    socket `EXECUTOR_SIGNAL_SOCKET`. Non-blocking writes, single
    connection reused across calls.

Control plane:
    `ExecutorClient.status()`, `.kill()`, `.flatten()`, `.resume()`
    HTTP-call the daemon at `EXECUTOR_CONTROL_HOST:PORT`.
"""

from __future__ import annotations

import json
import socket
import time
from dataclasses import asdict, dataclass

import httpx

from .config import CONFIG


@dataclass
class Signal:
    id: str
    kind: str                    # "open" | "close" | "rebalance"
    outcome_asset: int
    side: str                    # "Y" | "N"
    px: str
    notional_usd: float
    perp_delta_btc: float
    perp_asset: int
    ts_ms: int
    perp_ref_px: float = 0.0     # BTC perp mid at signal time (required for hedge)
    slippage_bps: int = 50       # IOC slippage budget (1 bp = 0.01%)
    ttl_ms: int = 0


class ExecutorClient:
    def __init__(
        self,
        socket_path: str | None = None,
        control_host: str | None = None,
        control_port: int | None = None,
    ) -> None:
        self.socket_path = socket_path or CONFIG.executor_signal_socket
        self.control_host = control_host or CONFIG.executor_control_host
        self.control_port = control_port or CONFIG.executor_control_port
        self._sock: socket.socket | None = None
        self._http = httpx.Client(timeout=3.0)

    # -- hot path --------------------------------------------------------

    def _ensure_sock(self) -> socket.socket:
        if self._sock is not None:
            return self._sock
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(self.socket_path)
        s.setblocking(True)
        self._sock = s
        return s

    def send(self, sig: Signal) -> None:
        line = (json.dumps(asdict(sig), separators=(",", ":")) + "\n").encode()
        try:
            sock = self._ensure_sock()
            sock.sendall(line)
        except (BrokenPipeError, ConnectionResetError, FileNotFoundError, OSError):
            self._sock = None
            sock = self._ensure_sock()
            sock.sendall(line)

    @staticmethod
    def now_ms() -> int:
        return int(time.time() * 1000)

    # -- control plane ---------------------------------------------------

    def _ctrl_url(self, path: str) -> str:
        return f"http://{self.control_host}:{self.control_port}/{path.lstrip('/')}"

    def status(self) -> dict:
        return self._http.get(self._ctrl_url("status")).json()

    def kill(self) -> dict:
        return self._http.post(self._ctrl_url("kill")).json()

    def resume(self) -> dict:
        return self._http.post(self._ctrl_url("resume")).json()

    def flatten(self) -> dict:
        return self._http.post(self._ctrl_url("flatten")).json()

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None
        self._http.close()
