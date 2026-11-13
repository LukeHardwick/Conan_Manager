#!/usr/bin/env python3
"""
proxytest.py - ask the proxy to open a tunnel in a few different ways and show what it answers.

Use it when Conan (or check_network.py) gets "407 Proxy Authentication Required" but another tool,
such as curl or a browser, gets through:

    python proxytest.py                          # the proxy saved in the Conan Mirror settings
    python proxytest.py --proxy 10.1.2.3:8080    # a proxy you give, e.g. the one curl used
    python proxytest.py --proxy 10.1.2.3:8080 --target github.com:443

Each request is a plain CONNECT without a login, as Python and Conan send it, as curl sends it,
and in the older HTTP/1.0 form. If the answers differ, the request headers decide whether the
proxy wants a login. If every answer is 407, the proxy wants a login from everything (compare
with the proxy address the other tool used). Standard library only (Python 3.11+).
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
from pathlib import Path
from urllib.parse import urlparse

SETTINGS = Path.home() / ".conan_mirror" / "settings.json"


def saved_proxy() -> str:
    try:
        value = json.loads(SETTINGS.read_text()).get("proxy", "")
    except (OSError, ValueError):
        return ""
    return value.strip() if isinstance(value, str) else ""


def variants(target: str) -> dict[str, str]:
    return {
        "python-like": f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n",
        "curl-like": (f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\nUser-Agent: curl/8.9.1\r\n"
                      "Proxy-Connection: Keep-Alive\r\n\r\n"),
        "http-1.0": f"CONNECT {target} HTTP/1.0\r\n\r\n",
    }


def ask(proxy: tuple[str, int], request: str, timeout: float) -> str:
    """Send one CONNECT request; return the proxy's status line and any login it asks for."""
    with socket.create_connection(proxy, timeout=timeout) as s:
        s.sendall(request.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = s.recv(4096)
            if not chunk:
                break
            resp += chunk
    lines = resp.split(b"\r\n\r\n", 1)[0].decode(errors="replace").split("\r\n")
    schemes = [ln.split(":", 1)[1].strip() for ln in lines[1:]
               if ln.lower().startswith("proxy-authenticate:")]
    return lines[0] + (f"   (asks for: {', '.join(schemes)})" if schemes else "")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--proxy", default="", help="host:port (default: the proxy in the Conan Mirror settings)")
    ap.add_argument("--target", default="center2.conan.io:443", help="host:port to tunnel to")
    ap.add_argument("--timeout", type=float, default=10)
    a = ap.parse_args()

    proxy = a.proxy.strip() or saved_proxy()
    source = "--proxy" if a.proxy.strip() else f"saved settings ({SETTINGS})"
    if not proxy:
        print("No proxy given and none saved in the settings. Use --proxy host:port.")
        return 2
    p = urlparse(proxy if "://" in proxy else f"http://{proxy}")
    if not p.hostname:
        print(f"Not a proxy address: {proxy!r}")
        return 2
    address = (p.hostname, p.port or 8080)
    print(f"proxy  : {address[0]}:{address[1]}   (from {source})")
    print(f"target : {a.target}\n")

    for name, request in variants(a.target).items():
        try:
            answer = ask(address, request, a.timeout)
        except OSError as e:
            answer = f"no answer: {e}"
        print(f"{name:12} -> {answer}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
