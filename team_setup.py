#!/usr/bin/env python3
"""
team_setup.py - point a developer's Conan 2 at the team Nexus, reversibly.

    python team_setup.py apply      # configure (backs up everything it touches)
    python team_setup.py status     # show what is applied
    python team_setup.py revert     # put every touched file back exactly as it was

Standard library only (Python 3.11+). Settings come from team_config.json next to
this script (written by the admin's Conan Mirror "Export team config"); if it is
missing, the DEFAULTS below are used.

What `apply` changes in the Conan home (`conan config home`):
  - remotes.json      adds the Nexus remote first, disables ConanCenter
  - global.conf       a marked block: build-from-source fetches sources from Nexus raw
  - compatibility.py  MSVC fallbacks (e.g. 195 may use 194 binaries)
  - profiles/<name>   team profiles shipped in ./profiles next to this script

Before the first change, every one of those files is copied to a backup folder
(%APPDATA%\\ConanTeamSetup on Windows, ~/Library/Application Support/ConanTeamSetup
on macOS, ~/.config/conan-team-setup on Linux). Files that did not exist are recorded
as such and deleted again on revert. Running `apply` twice is safe: the backup
of the ORIGINAL state is kept.
"""
from __future__ import annotations

import argparse
import datetime as dt
import getpass
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULTS = {
    "nexus_remote": "nexus",
    "nexus_url": "https://nexus.example.com/repository/conan-hosted/",
    "raw_url": "https://nexus.example.com/repository/conan-sources/",
    "disable_remotes": ["conancenter"],
    # newer MSVC -> older MSVC whose binaries it may use (one step each)
    "msvc_fallbacks": {"195": "194", "194": "193"},
}

HERE = Path(__file__).resolve().parent
BLOCK_START = "# >>> conan-team-setup (managed) >>>"
BLOCK_END = "# <<< conan-team-setup (managed) <<<"
COMPAT = Path("extensions/plugins/compatibility/compatibility.py")
COMPAT_RE = re.compile(r"msvc_fallback\s*=\s*\{[^}]*\}\.get\(compiler_version\)")


# ------------------------------------------------------------------ helpers
def info(msg: str) -> None:
    print(msg, flush=True)


def die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    f = HERE / "team_config.json"
    if f.exists():
        cfg.update(json.loads(f.read_text(encoding="utf-8")))
    elif "example.com" in cfg["nexus_url"]:
        die(f"{f.name} not found and DEFAULTS still point at example.com - "
            "ask your Conan admin for the team config zip.")
    return cfg


def conan(*args: str, check: bool = True, capture: bool = True) -> str:
    exe = shutil.which("conan")
    if not exe:
        die("'conan' not found on PATH. Install it with:  pip install conan")
    p = subprocess.run([exe, *args], text=True, capture_output=capture)
    if check and p.returncode != 0:
        die(f"conan {' '.join(args)} failed:\n{(p.stderr or '').strip()}")
    return (p.stdout or "").strip() if capture else ""


def state_root() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        return base / "ConanTeamSetup"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "ConanTeamSetup"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "conan-team-setup"


class State:
    """Backups + bookkeeping for one Conan home."""

    def __init__(self, home: Path):
        self.home = home
        key = hashlib.sha1(str(home).lower().encode()).hexdigest()[:10]
        self.dir = state_root() / key
        self.file = self.dir / "state.json"
        self.data = json.loads(self.file.read_text()) if self.file.exists() else {
            "conan_home": str(home), "files": {}, "history": []}

    @property
    def applied(self) -> bool:
        return self.file.exists()

    def save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.file.write_text(json.dumps(self.data, indent=2))

    def backup(self, rel: Path) -> None:
        """Remember the ORIGINAL version of a file before we first touch it."""
        key = rel.as_posix()
        if key in self.data["files"]:
            return
        src = self.home / rel
        if src.exists():
            dst = self.dir / "backup" / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            self.data["files"][key] = "backup"
        else:
            self.data["files"][key] = "absent"
        self.save()  # save immediately so a crash half-way can still be reverted


# ------------------------------------------------------------------ steps
def step_remotes(cfg: dict, st: State) -> None:
    st.backup(Path("remotes.json"))
    name, url = cfg["nexus_remote"], cfg["nexus_url"]
    conan("remote", "add", name, url, "--index", "0", "--force")
    info(f"  remote '{name}' -> {url} (first in list)")
    existing = {r["name"] for r in json.loads(conan("remote", "list", "--format=json") or "[]")}
    for r in cfg.get("disable_remotes", []):
        if r in existing and r != name:
            conan("remote", "disable", r)
            info(f"  remote '{r}' disabled")


def step_global_conf(cfg: dict, st: State) -> None:
    rel = Path("global.conf")
    st.backup(rel)
    path = st.home / rel
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    text = re.sub(re.escape(BLOCK_START) + r".*?" + re.escape(BLOCK_END) + r"\n?", "", text,
                  flags=re.S).rstrip("\n")
    lines = []
    if cfg.get("raw_url"):
        # list value = Python literal; Conan tries these mirrors for upstream sources
        lines.append(f"core.sources:download_urls = {json.dumps([cfg['raw_url']])}")
    if lines:
        text = (text + "\n\n" if text else "") + "\n".join([BLOCK_START, *lines, BLOCK_END])
    path.write_text(text + "\n", encoding="utf-8")
    info(f"  global.conf: sources from {cfg.get('raw_url') or '(none)'}")


def step_compat(cfg: dict, st: State) -> None:
    fallbacks = cfg.get("msvc_fallbacks") or {}
    path = st.home / COMPAT
    if not fallbacks:
        return
    if not path.exists():
        conan("profile", "list")  # makes Conan generate its default plugins
    if not path.exists():
        info("  compatibility plugin not found - skipped")
        return
    text = path.read_text(encoding="utf-8")
    if not COMPAT_RE.search(text):
        info("  compatibility.py has an unexpected layout - MSVC fallback NOT added "
             "(ask your Conan admin)")
        return
    st.backup(COMPAT)
    new = COMPAT_RE.sub(f"msvc_fallback = {json.dumps(fallbacks)}.get(compiler_version)", text)
    # Without removing this header Conan overwrites the file on upgrade.
    new = re.sub(r"^# This file was generated by Conan.*\n(#.*will destroy your changes\.\n)?",
                 "# Customised by team_setup.py (revert with: python team_setup.py revert)\n",
                 new, count=1, flags=re.M)
    path.write_text(new, encoding="utf-8")
    info(f"  MSVC fallbacks: {', '.join(f'{a}->{b}' for a, b in fallbacks.items())}")


def step_profiles(st: State) -> None:
    src = HERE / "profiles"
    if not src.is_dir():
        return
    for f in sorted(p for p in src.rglob("*") if p.is_file()):
        rel = Path("profiles") / f.relative_to(src)
        st.backup(rel)
        dst = st.home / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, dst)
        info(f"  profile {rel.as_posix()[9:]}")


# ------------------------------------------------------------------ commands
def cmd_apply(a) -> None:
    cfg = load_config()
    home = Path(conan("config", "home"))
    st = State(home)
    info(f"Conan home : {home}")
    info(f"Backups in : {st.dir}" + ("  (already applied - original backup kept)" if st.applied else ""))
    st.save()
    step_remotes(cfg, st)
    step_global_conf(cfg, st)
    step_compat(cfg, st)
    step_profiles(st)
    st.data["history"].append({"applied": dt.datetime.now().isoformat(timespec="seconds"),
                               "nexus_url": cfg["nexus_url"]})
    st.save()
    info("\nDone. Undo any time with:  python team_setup.py revert")

    if a.no_login:
        return
    if not a.yes:
        ans = input(f"\nLog in to '{cfg['nexus_remote']}' now? [Y/n] ").strip().lower()
        if ans not in ("", "y", "yes"):
            return
    user = a.user or input("Nexus username: ").strip() or getpass.getuser()
    # conan prompts for the password itself, so it never appears in the command line
    conan("remote", "login", cfg["nexus_remote"], user, capture=False)


def cmd_revert(a) -> None:
    home = Path(conan("config", "home"))
    st = State(home)
    if not st.applied:
        info(f"Nothing to revert for {home} (no backup in {st.dir}).")
        return
    nexus = load_config().get("nexus_remote", "nexus") if (HERE / "team_config.json").exists() else None
    if nexus:
        conan("remote", "logout", nexus, check=False)
    for key, kind in st.data["files"].items():
        target = home / key
        if kind == "backup":
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(st.dir / "backup" / key, target)
            info(f"  restored {key}")
        elif target.exists():
            target.unlink()
            info(f"  removed  {key}")
    shutil.rmtree(st.dir)
    info("\nReverted - Conan is configured exactly as before.")


def cmd_status(a) -> None:
    home = Path(conan("config", "home"))
    st = State(home)
    info(f"Conan home : {home}")
    if not st.applied:
        info("Status     : NOT applied")
    else:
        last = st.data["history"][-1] if st.data["history"] else {}
        info(f"Status     : applied {last.get('applied', '?')} ({last.get('nexus_url', '?')})")
        info(f"Backups in : {st.dir}")
        for k, v in st.data["files"].items():
            info(f"   {v:7} {k}")
    info("")
    print(conan("remote", "list"))


def main() -> None:
    ap = argparse.ArgumentParser(description="Configure Conan for the team Nexus (reversible).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("apply", help="configure Conan for Nexus")
    p.add_argument("--user", help="Nexus username for login")
    p.add_argument("--no-login", action="store_true", help="skip 'conan remote login'")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask questions")
    sub.add_parser("revert", help="restore the original Conan configuration")
    sub.add_parser("status", help="show current state")
    a = ap.parse_args()
    {"apply": cmd_apply, "revert": cmd_revert, "status": cmd_status}[a.cmd](a)


if __name__ == "__main__":
    main()
