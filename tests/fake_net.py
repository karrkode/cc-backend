"""CC-189: fake DNS and a fake internet for fetch tests. The shared fetch only reaches public addresses, so tests
give hosts public (or other) answers here, and a "public" connection lands on a local test server."""
import socket

from app.discovery import fetch as web

PUBLIC = "93.184.215.14"


class Net:
    def __init__(self, port: int, answers: dict[str, list[list[str]]] | None = None):
        self.port = port
        self.answers = answers or {}  # host: its DNS answers in turn, the last one repeating; any other host is PUBLIC
        self.lookups: list[str] = []
        self.dialed: list[tuple[str, int]] = []
        self.real_dial = socket.create_connection

    def lookup(self, host: str, port: int) -> list[str]:
        self.lookups.append(host)
        replies = self.answers.get(host, [[PUBLIC]])
        return replies.pop(0) if len(replies) > 1 else replies[0]

    def dial(self, address, timeout=None, source_address=None):
        self.dialed.append(address)
        return self.real_dial(("127.0.0.1", self.port), timeout)


def route(monkeypatch, port: int, answers: dict[str, list[list[str]]] | None = None) -> Net:
    net = Net(port, answers)
    monkeypatch.setattr(web, "_addresses", net.lookup)
    monkeypatch.setattr(socket, "create_connection", net.dial)
    return net


