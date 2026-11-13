#!/usr/bin/env python3
"""
Conan Mirror - copy packages from ConanCenter into a private Nexus.

Standard library only (Python 3.11+, tkinter). It drives the `conan` (2.x) CLI:

  Search   -> conan search / conan list   (ConanCenter, compared with Nexus)
  Plan     -> conan graph info            (full dependency graph per profile,
                                            each node checked against Nexus)
  Mirror   -> conan install --build=missing   (download or build every node)
              conan list --graph ...          (turn the graph into a package list)
              conan upload --list ... -r nexus (recipes + binaries + backup sources)

Source tarballs go to the Nexus *raw* repo via Conan's "backup sources" feature
(core.sources:upload_url), so your team can build from source without internet.
Conan skips source files that already exist in the raw repo.

Settings are stored in ~/.conan_mirror/settings.json. Passwords are never
written to disk; they live in memory for the session and are passed to Conan
through environment variables.
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

from internal_recipes import Settings
from mirror_core import Conan, ConanError, Mirror, Options, apply_config, load_manifest, \
    load_settings, run_jobs, run_verify

# --------------------------------------------------------------------------- headless
class Headless:
    """Stands in for the GUI when running from a manifest on the command line."""

    def __init__(self, user: str, password: str):
        self.settings: Settings = load_settings()
        self.creds: tuple[str, str] = (user, password)

    @staticmethod
    def log(text: str, tag: str = "") -> None:
        print(text, flush=True)


def run_headless(a: argparse.Namespace) -> int:
    user = a.user or os.environ.get("CONAN_MIRROR_RAW_USER", "")
    password = os.environ.get("CONAN_MIRROR_RAW_PASSWORD", "")
    app = Headless(user, password)
    s = app.settings
    if not s["nexus_url"]:
        print("ERROR: no Nexus URL in settings - run the GUI once and fill in Settings.", file=sys.stderr)
        return 2
    if not a.plan and not user:
        app.creds = (s.get("nexus_user") or input("Nexus username: ").strip(), "")
    if not a.plan and not password:
        app.creds = (app.creds[0], getpass.getpass(f"Nexus password / token for {app.creds[0]}: "))
    conan = Conan(app)
    m = Mirror(conan, s)
    opts = Options(dry_run=a.dry_run, verify=not a.no_verify)
    try:
        jobs = load_manifest(Path(a.manifest).resolve())
        apply_config(conan, s, lambda text, tag="": None)     # idempotent; quiet
        if a.plan:
            todo = 0
            for ref, p in jobs:
                print(f"== Plan {ref} [{p}]", flush=True)
                for r in m.plan(ref, p, opts):
                    print(f"   {r.ref:45} {r.context:6} {r.action}")
                    todo += not r.done
            print(f"\n{todo} package(s) not yet complete in Nexus")
            return 0
        if a.verify_only:
            results = run_verify(m, jobs, opts, app.log)
        else:
            results = run_jobs(m, jobs, opts, app.log, lambda r, rows: None)
    except ConanError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    return 1 if any("FAILED" in r[2] for r in results) else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Conan Mirror. Without --manifest the GUI opens.")
    ap.add_argument("--manifest", metavar="FILE", help="mirror everything listed in this file, no GUI")
    ap.add_argument("--plan", action="store_true", help="with --manifest: only show what would be done")
    ap.add_argument("--dry-run", action="store_true", help="with --manifest: do everything except upload")
    ap.add_argument("--no-verify", action="store_true", help="with --manifest: skip the install-from-Nexus check")
    ap.add_argument("--verify-only", action="store_true", help="with --manifest: only run that check")
    ap.add_argument("--user", default="", help="Nexus username (password: CONAN_MIRROR_RAW_PASSWORD or prompt)")
    a = ap.parse_args()
    if a.manifest:
        return run_headless(a)
    from gui import App     # only now: the command-line mode must work without tkinter
    App().mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
