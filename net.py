from __future__ import annotations

import socket

import aiohttp


def ipv4_connector(**kwargs) -> aiohttp.TCPConnector:
    """Force IPv4 — Timeweb VPS has broken IPv6 outbound to many .ru APIs."""
    kwargs.setdefault("family", socket.AF_INET)
    return aiohttp.TCPConnector(**kwargs)
