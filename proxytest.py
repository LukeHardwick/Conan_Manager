#!/usr/bin/env python3
"""
proxytest.py - ask the proxy to open a tunnel in a few different ways and show what it answers.

Use it when Conan (or check_network.py) gets "407 Proxy Authentication Required" but another tool,
such as curl or a browser, gets through:

    python proxytest.py                          # the proxy saved in the Conan Mirror settings
    python proxytest.py --proxy 10.1.2.3:8080    # a proxy you give, e.g. the one curl used
    python proxytest.py --proxy 10.1.2.3:8080 --target github.com:443

Part 1 opens the tunnel (CONNECT) without a login, as Python and Conan do it, as curl does it,
and in the older HTTP/1.0 form. If the answers differ, the request headers decide whether the
proxy wants a login. If every answer is 407, the proxy wants a login from everything (compare
with the proxy address the other tool used).

Part 2 goes one step further, as Conan does: it checks TLS with the CA bundle from the settings
(or --cacert), then sends a GET inside the tunnel. A proxy that intercepts TLS sees that request
and can answer 407 to it even though the tunnel opened. Each GET differs in one thing only (the
User-Agent or the address), so the table shows what the proxy objects to.
Standard library only (Python 3.11+).
"""
from __future__ import annotations

import argparse
import json
import socket
import ssl
import sys
from pathlib import Path
from urllib.parse import urlparse

SETTINGS = Path.home() / ".conan_mirror" / "settings.json"


# The User-Agent each program sends with its requests. Conan's is
# "Conan/<version> (Python <version>) python-requests/<version>".
AGENTS: dict[str, str | None] = {
    "curl": "curl/8.9.1",
    "Python urllib": "Python-urllib/3.12",
    "python-requests": "python-requests/2.32.5",
    "Conan": "Conan/2.34.0 (Python 3.12.0) python-requests/2.32.5",
    "no User-Agent": None,
}
PATHS = ("/v1/ping", "/v2/conans/search?q=zlib")      # Conan's first call, and a search


def saved(key: str) -> str:
    try:
        value = json.loads(SETTINGS.read_text()).get(key, "")
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


def read_head(sock: socket.socket | ssl.SSLSocket) -> str:
    """The status line of an HTTP answer, plus any login the proxy asks for."""
    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = sock.recv(4096)
        if not chunk:
            break
        resp += chunk
    lines = resp.split(b"\r\n\r\n", 1)[0].decode(errors="replace").split("\r\n")
    schemes = [ln.split(":", 1)[1].strip() for ln in lines[1:]
               if ln.lower().startswith("proxy-authenticate:")]
    return lines[0] + (f"   (asks for: {', '.join(schemes)})" if schemes else "")


def ask(proxy: tuple[str, int], request: str, timeout: float) -> str:
    """Send one CONNECT request; return the proxy's answer."""
    with socket.create_connection(proxy, timeout=timeout) as s:
        s.sendall(request.encode())
        return read_head(s)


def get_inside(proxy: tuple[str, int], target: str, path: str, agent: str | None,
               ctx: ssl.SSLContext, timeout: float) -> str:
    """Open the tunnel, check TLS with ctx, send one GET inside it; return the answer."""
    host = target.rsplit(":", 1)[0]
    with socket.create_connection(proxy, timeout=timeout) as raw:
        raw.sendall(variants(target)["python-like"].encode())
        opened = read_head(raw)
        if " 200" not in opened.split("(")[0]:
            return f"tunnel refused: {opened}"
        with ctx.wrap_socket(raw, server_hostname=host) as tls:
            headers = [f"GET {path} HTTP/1.1", f"Host: {host}", "Accept: */*", "Connection: close"]
            if agent is not None:
                headers.append(f"User-Agent: {agent}")
            tls.sendall(("\r\n".join(headers) + "\r\n\r\n").encode())
            return read_head(tls)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--proxy", default="", help="host:port (default: the proxy in the Conan Mirror settings)")
    ap.add_argument("--target", default="center2.conan.io:443", help="host:port to tunnel to")
    ap.add_argument("--cacert", default="", help="CA bundle for part 2 (default: the one in the settings)")
    ap.add_argument("--timeout", type=float, default=10)
    a = ap.parse_args()

    proxy = a.proxy.strip() or saved("proxy")
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
    print("== Part 1: opening the tunnel (CONNECT), no login")

    for name, request in variants(a.target).items():
        try:
            answer = ask(address, request, a.timeout)
        except OSError as e:
            answer = f"no answer: {e}"
        print(f"{name:12} -> {answer}")

    cacert = a.cacert.strip() or saved("cacert_path")
    print("\n== Part 2: a GET inside the tunnel, TLS checked "
          + (f"with the CA bundle {cacert}" if cacert else "with this machine's own certificate store"))
    try:
        ctx = ssl.create_default_context(cafile=cacert or None)
        # Python 3.13+ checks certificates more strictly by default than Conan (requests) does;
        # corporate proxy CAs often fail only that extra check, so match Conan here.
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    except (OSError, ssl.SSLError) as e:
        print(f"Could not load the CA bundle: {e}")
        return 2
    for path in PATHS:
        print(f"\nGET https://{a.target.rsplit(':', 1)[0]}{path}")
        for label, agent in AGENTS.items():
            try:
                answer = get_inside(address, a.target, path, agent, ctx, a.timeout)
            except ssl.SSLCertVerificationError as e:
                answer = (f"TLS not trusted ({e.verify_message}): "
                          + ("the CA bundle lacks the proxy's CA" if cacert else "try --cacert <bundle>"))
            except OSError as e:
                answer = f"no answer: {e}"
            print(f"  {label:16} -> {answer}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
