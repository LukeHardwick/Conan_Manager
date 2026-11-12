#!/usr/bin/env python3
"""
check_network.py - connectivity / TLS-interception check for the Conan mirror setup.

Standard library only (Python 3.11+). Run it on the admin machine:

    python check_network.py --proxy 10.1.2.3:8080 --nexus https://nexus.corp.local

If you have already saved settings in the Conan Mirror app, the values are read
from ~/.conan_mirror/settings.json automatically, so plain `python check_network.py`
works too.

What it checks
  1. Nexus is reachable DIRECTLY (no proxy).
  2. ConanCenter is reachable THROUGH the proxy.
  3. Whether the proxy intercepts TLS (re-signs certificates with a corporate CA).
     Conan uses its own CA bundle (certifi), NOT the Windows certificate store,
     so if the proxy intercepts TLS Conan will fail with SSL errors even though
     your browser works. Fix: build a CA bundle (--export-bundle on Windows) and set
         core.net.http:cacert_path = <path to bundle.pem>
     in Conan's global.conf (the Conan Mirror app has a field for this).
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import ssl
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

SETTINGS = Path.home() / ".conan_mirror" / "settings.json"
DEFAULT_CENTER = "https://center2.conan.io"

# Organisations that issue the public certificates you would expect to see for
# ConanCenter / GitHub etc. Anything else is very likely a corporate proxy CA.
PUBLIC_CA_HINTS = (
    "let's encrypt", "isrg", "digicert", "amazon", "google trust", "sectigo",
    "comodo", "globalsign", "microsoft", "cloudflare", "entrust", "godaddy",
    "starfield", "usertrust", "baltimore", "geotrust", "thawte", "zerossl",
)

OK, WARN, FAIL = "[ OK ]", "[WARN]", "[FAIL]"


def say(tag: str, msg: str) -> None:
    print(f"{tag} {msg}", flush=True)


def name_to_str(name) -> str:
    """Turn ssl's ((('commonName', 'x'),), ...) tuples into 'CN=x, O=y'."""
    short = {"commonName": "CN", "organizationName": "O", "countryName": "C",
             "organizationalUnitName": "OU"}
    parts = []
    for rdn in name or ():
        for key, value in rdn:
            parts.append(f"{short.get(key, key)}={value}")
    return ", ".join(parts) or "?"


def looks_public(issuer: str) -> bool:
    low = issuer.lower()
    return any(h in low for h in PUBLIC_CA_HINTS)


def open_tunnel(proxy: str | None, host: str, port: int, timeout: float) -> socket.socket:
    """TCP connection to host:port, through an HTTP CONNECT proxy if given."""
    if not proxy:
        return socket.create_connection((host, port), timeout=timeout)
    p = urlparse(proxy if "://" in proxy else f"http://{proxy}")
    sock = socket.create_connection((p.hostname, p.port or 8080), timeout=timeout)
    req = f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n"
    if p.username:
        token = base64.b64encode(f"{p.username}:{p.password or ''}".encode()).decode()
        req += f"Proxy-Authorization: Basic {token}\r\n"
    sock.sendall((req + "\r\n").encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = sock.recv(4096)
        if not chunk:
            break
        resp += chunk
    status = resp.split(b"\r\n", 1)[0].decode(errors="replace")
    if " 200" not in status:
        sock.close()
        raise ConnectionError(f"proxy refused CONNECT: {status!r}")
    return sock


def decode_der(der: bytes) -> dict:
    """Decode a certificate without third-party libs (uses a CPython helper)."""
    pem = ssl.DER_cert_to_PEM_cert(der)
    with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as f:
        f.write(pem)
        path = f.name
    try:
        return ssl._ssl._test_decode_cert(path)  # type: ignore[attr-defined]
    except Exception:
        return {}
    finally:
        os.unlink(path)


def tls_probe(proxy: str | None, url: str, timeout: float, save_dir: Path) -> bool:
    """Handshake with the system trust store; report issuer; detect interception."""
    u = urlparse(url)
    host, port = u.hostname, u.port or 443
    via = f"via proxy {proxy}" if proxy else "direct"

    # 1) verified handshake using the OS trust store
    ctx = ssl.create_default_context()
    try:
        with open_tunnel(proxy, host, port, timeout) as raw:
            with ctx.wrap_socket(raw, server_hostname=host) as tls:
                cert = tls.getpeercert()
                issuer = name_to_str(cert.get("issuer"))
                say(OK, f"TLS to {host} ({via}) verified by the system trust store")
                print(f"       issuer: {issuer}")
                if looks_public(issuer):
                    say(OK, "issuer is a public CA -> no TLS interception detected")
                else:
                    say(WARN, "issuer does not look like a public CA -> the proxy probably "
                              "intercepts TLS. Your OS trusts it, but Conan (certifi) will not.")
                    print("       Fix: set core.net.http:cacert_path to a bundle containing the "
                          "corporate root CA (on Windows: run this script with --export-bundle).")
                return True
    except ssl.SSLCertVerificationError as e:
        say(WARN, f"TLS to {host} ({via}) not trusted by Python's trust store: {e.verify_message}")
    except Exception as e:  # noqa: BLE001
        say(FAIL, f"could not connect to {host} ({via}): {e}")
        return False

    # 2) verification failed: grab the certificate anyway so you can inspect it
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with open_tunnel(proxy, host, port, timeout) as raw:
            with ctx.wrap_socket(raw, server_hostname=host) as tls:
                der = tls.getpeercert(binary_form=True)
    except Exception as e:  # noqa: BLE001
        say(FAIL, f"unverified handshake also failed: {e}")
        return False
    info = decode_der(der)
    issuer = name_to_str(info.get("issuer")) if info else "?"
    if info:
        print(f"       subject: {name_to_str(info.get('subject'))}")
        print(f"       issuer : {issuer}")
    out = save_dir / f"{host}_leaf.pem"
    out.write_text(ssl.DER_cert_to_PEM_cert(der))
    print(f"       saved presented certificate to {out}")
    if info and looks_public(issuer):
        say(WARN, "the certificate is from a PUBLIC CA, so this is NOT interception - this "
                  "Python install just has no root certificates (macOS python.org builds: run "
                  "'Install Certificates.command'). Conan bundles its own CAs and is unaffected.")
        return True
    say(FAIL, "certificate is NOT from a known public CA -> TLS interception is likely.")
    print("       This usually means TLS interception. Get the corporate ROOT CA "
          "(from IT, or export it from the Windows cert store) and point "
          "core.net.http:cacert_path at a bundle that contains it.")
    return False


def http_probe(proxy: str | None, url: str, timeout: float) -> bool:
    """Plain HTTP(S) GET; any HTTP status (even 401/404) proves the path works."""
    # Reachability only: certificate trust is judged separately by tls_probe().
    insecure = ssl.create_default_context()
    insecure.check_hostname = False
    insecure.verify_mode = ssl.CERT_NONE
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {}),
        urllib.request.HTTPSHandler(context=insecure),
    )
    via = f"via proxy {proxy}" if proxy else "direct"
    try:
        with opener.open(url, timeout=timeout) as r:
            say(OK, f"GET {url} ({via}) -> HTTP {r.status}")
            return True
    except urllib.error.HTTPError as e:
        say(OK, f"GET {url} ({via}) -> HTTP {e.code} (server reachable)")
        return True
    except Exception as e:  # noqa: BLE001
        say(FAIL, f"GET {url} ({via}) failed: {e}")
        return False


def export_bundle(path: Path) -> None:
    """Windows only: dump the machine's ROOT + CA stores into one PEM bundle."""
    if not hasattr(ssl, "enum_certificates"):
        say(WARN, "--export-bundle only works on Windows (it reads the Windows cert store).")
        return
    seen, pems = set(), []
    for store in ("ROOT", "CA"):
        for der, enc, _trust in ssl.enum_certificates(store):  # type: ignore[attr-defined]
            if enc == "x509_asn" and der not in seen:
                seen.add(der)
                pems.append(ssl.DER_cert_to_PEM_cert(der))
    path.write_text("".join(pems))
    say(OK, f"wrote {len(pems)} certificates to {path}")
    print(f"       In Conan global.conf:  core.net.http:cacert_path = {path}")


def main() -> int:
    saved = {}
    if SETTINGS.exists():
        try:
            saved = json.loads(SETTINGS.read_text())
        except ValueError:
            pass

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--proxy", default=saved.get("proxy", ""), help="e.g. 10.1.2.3:8080")
    ap.add_argument("--nexus", default=saved.get("nexus_url", ""), help="Nexus conan repo URL")
    ap.add_argument("--raw", default=saved.get("raw_url", ""), help="Nexus raw repo URL")
    ap.add_argument("--center", default=saved.get("center_url") or DEFAULT_CENTER)
    ap.add_argument("--timeout", type=float, default=15)
    ap.add_argument("--export-bundle", metavar="FILE", help="(Windows) write OS CA store to a PEM bundle")
    a = ap.parse_args()

    proxy = a.proxy.strip() or None
    if proxy and "://" not in proxy:
        proxy = f"http://{proxy}"
    save_dir = Path.home() / ".conan_mirror"
    save_dir.mkdir(exist_ok=True)

    print(f"Python {sys.version.split()[0]} on {sys.platform}")
    print(f"proxy={proxy or '(none)'}  nexus={a.nexus or '(not set)'}  center={a.center}\n")

    results = []
    print("== Nexus (direct, no proxy) ==")
    for url in filter(None, {a.nexus, a.raw}):
        results.append(http_probe(None, url, a.timeout))
        if url.startswith("https"):
            results.append(tls_probe(None, url, a.timeout, save_dir))
    if not a.nexus:
        say(WARN, "no Nexus URL given (--nexus) - skipped")

    print("\n== ConanCenter (through proxy) ==")
    if not proxy:
        say(WARN, "no proxy given (--proxy) - testing direct access instead")
    results.append(http_probe(proxy, a.center.rstrip("/") + "/v1/ping", a.timeout))
    results.append(tls_probe(proxy, a.center, a.timeout, save_dir))

    print("\n== GitHub (recipes download most sources from here) ==")
    results.append(tls_probe(proxy, "https://github.com", a.timeout, save_dir))

    if a.export_bundle:
        print("\n== CA bundle export ==")
        export_bundle(Path(a.export_bundle).resolve())

    print("\nSummary:", "all checks passed" if all(results) else "some checks FAILED (see above)")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
