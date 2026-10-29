"""The HTTP client used by the CLI and preflight."""

import socket
import threading

import pytest

from labtender.client import Hub, HubError


def test_a_refusal_with_a_cut_off_body_keeps_its_status():
    """The status line arrived but the error body did not: still a HubError with the status."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)

    def serve():
        conn, _ = sock.accept()
        with conn:
            conn.settimeout(10)
            seen = b""
            while b"\r\n\r\n" not in seen:
                seen += conn.recv(65536)
            conn.sendall(
                b"HTTP/1.1 403 Forbidden\r\nContent-Type: application/json\r\n"
                b"Content-Length: 100\r\nConnection: close\r\n\r\n{"
            )
            conn.shutdown(socket.SHUT_WR)  # a clean close, 99 bytes short
            while conn.recv(65536):
                pass

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    with pytest.raises(HubError) as e:
        Hub(f"http://127.0.0.1:{sock.getsockname()[1]}", token="x").post("/api/charters/1/approve")
    assert e.value.status == 403 and str(e.value) == "Forbidden"
    t.join(10)
    sock.close()
