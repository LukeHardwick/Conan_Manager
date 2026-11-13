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
import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from tkinter.scrolledtext import ScrolledText

import internal_recipes as ir
import team_config as tc

APP_NAME = "Conan Mirror"
HERE = Path(__file__).resolve().parent
MANIFEST_FILE = HERE / "mirror.json"
CONFIG_DIR = Path.home() / ".conan_mirror"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
WORK_DIR = CONFIG_DIR / "work"
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
BLOCK_START = "# >>> conan-mirror (managed) >>>"
BLOCK_END = "# <<< conan-mirror (managed) <<<"

DEFAULTS = {
    "conan_exe": "conan",
    "center_remote": "conancenter",
    "center_url": "https://center2.conan.io",
    "nexus_remote": "nexus",
    "nexus_url": "",          # e.g. https://nexus.corp/repository/conan-hosted/
    "raw_url": "",            # e.g. https://nexus.corp/repository/conan-sources/
    "proxy": "",              # e.g. 10.1.2.3:8080  (used for ConanCenter only)
    "cacert_path": "",        # optional CA bundle if the proxy intercepts TLS
    "nexus_user": "",
    "internal_remote": "internal",   # local-recipes-index remote for ./recipes
    "build_profile": "",             # Release profile for build tools; empty = same as host
    "conan_home": str(ir.DEFAULT_CONAN_HOME),   # the mirror's own Conan home; empty = your normal one
    "msvc_fallbacks": dict(tc.DEFAULT_FALLBACKS),  # newer MSVC -> older MSVC whose binaries it may use
    "dark_mode": False,              # GUI colours; takes effect when the GUI starts
}

# Text colours for log lines and tree rows (cmd/ok/err/warn), per theme.
# Every Tk widget sharing this dict means one apply_theme() call recolours everything.
LIGHT_TAGS = {"cmd": "#2060c0", "ok": "#208020", "err": "#c02020", "warn": "#b07000"}
DARK_TAGS = {"cmd": "#6ea8ff", "ok": "#5cc16a", "err": "#ff6b6b", "warn": "#e0a040"}
TAGS = dict(LIGHT_TAGS)

DARK = {
    "bg": "#202124",        # windows, frames, tabs
    "field": "#2b2c2f",     # entries, text boxes, lists
    "raised": "#34363a",    # buttons, selected tab, headings
    "hover": "#3e4045",
    "border": "#4a4c50",
    "fg": "#e6e6e6",
    "fg_dim": "#8a8a8a",    # disabled text
    "select": "#2f5d9e",    # selection highlight
}


def apply_theme(root: tk.Misc, dark: bool) -> None:
    """Pick the ttk theme and colours. Call before any widget is created: plain Tk widgets
    (Text, Listbox, dialogs) take their colours from the option database at creation time."""
    style = ttk.Style(root)
    if not dark:
        TAGS.update(LIGHT_TAGS)
        if sys.platform == "win32":
            style.theme_use("vista")
        return

    TAGS.update(DARK_TAGS)
    c = DARK
    style.theme_use("clam")  # the built-in theme that honours custom colours on every platform
    style.configure(".", background=c["bg"], foreground=c["fg"], fieldbackground=c["field"],
                    bordercolor=c["border"], darkcolor=c["bg"], lightcolor=c["raised"],
                    troughcolor=c["field"], selectbackground=c["select"], selectforeground="#ffffff",
                    insertcolor=c["fg"], arrowcolor=c["fg"], focuscolor=c["border"])
    style.map(".", foreground=[("disabled", c["fg_dim"])],
              background=[("disabled", c["bg"])])
    style.configure("TButton", background=c["raised"])
    style.map("TButton", background=[("disabled", c["bg"]), ("pressed", c["border"]),
                                     ("active", c["hover"])])
    for w in ("TCheckbutton", "TRadiobutton"):
        style.configure(w, indicatorbackground=c["field"], indicatorforeground=c["fg"])
        style.map(w, background=[("active", c["bg"])],
                  indicatorbackground=[("pressed", c["raised"]), ("selected", c["select"])])
    style.configure("TNotebook", background=c["bg"], bordercolor=c["border"])
    style.configure("TNotebook.Tab", background=c["bg"], padding=(10, 3))
    style.map("TNotebook.Tab", background=[("selected", c["raised"]), ("active", c["hover"])])
    style.configure("Treeview", background=c["field"], fieldbackground=c["field"], foreground=c["fg"])
    style.map("Treeview", background=[("selected", c["select"])], foreground=[("selected", "#ffffff")])
    style.configure("Treeview.Heading", background=c["raised"], foreground=c["fg"])
    style.map("Treeview.Heading", background=[("active", c["hover"])])
    style.map("TCombobox", fieldbackground=[("readonly", c["field"]), ("disabled", c["bg"])],
              foreground=[("readonly", c["fg"]), ("disabled", c["fg_dim"])],
              selectbackground=[("readonly", c["field"])], selectforeground=[("readonly", c["fg"])])
    style.map("TEntry", fieldbackground=[("readonly", c["bg"]), ("disabled", c["bg"])])
    style.configure("TScrollbar", background=c["raised"], arrowcolor=c["fg"])
    style.map("TScrollbar", background=[("active", c["hover"])])
    style.configure("TProgressbar", background=c["select"])
    style.configure("TLabelframe", background=c["bg"], bordercolor=c["border"])
    style.configure("TLabelframe.Label", background=c["bg"], foreground=c["fg"])

    # plain Tk widgets: Text, Listbox, the combobox drop-down, Toplevels and simpledialog boxes
    root.configure(background=c["bg"])
    for pattern, value in (
            ("*Background", c["bg"]), ("*Foreground", c["fg"]),
            ("*Text.Background", c["field"]), ("*Listbox.Background", c["field"]),
            ("*Entry.Background", c["field"]), ("*Button.Background", c["raised"]),
            ("*activeBackground", c["hover"]), ("*activeForeground", c["fg"]),
            ("*selectBackground", c["select"]), ("*selectForeground", "#ffffff"),
            ("*insertBackground", c["fg"]), ("*highlightBackground", c["bg"]),
            ("*highlightColor", c["border"]), ("*disabledForeground", c["fg_dim"]),
            ("*TCombobox*Listbox.background", c["field"]),
            ("*TCombobox*Listbox.foreground", c["fg"])):
        root.option_add(pattern, value)


# --------------------------------------------------------------------------- helpers
class Cancelled(Exception):
    pass


class ConanError(Exception):
    pass


def load_settings() -> dict:
    s = dict(DEFAULTS)
    try:
        s.update(json.loads(SETTINGS_FILE.read_text()))
    except (OSError, ValueError):
        pass
    return s


def save_settings(s: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(s, indent=2))


def env_suffix(remote: str) -> str:
    """Conan's env-var naming: CONAN_PASSWORD_<REMOTE> with '-' -> '_'."""
    return remote.replace("-", "_").upper()


def slash(url: str) -> str:
    url = url.strip()
    return url if not url or url.endswith("/") else url + "/"


def natural_key(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def strip_ts(ref: str) -> str:
    """'zlib/1.3#abc%1700000000.0' -> 'zlib/1.3#abc'"""
    return ref.split("%", 1)[0]


def fmt_cmd(cmd: list[str]) -> str:
    return subprocess.list2cmdline(cmd) if os.name == "nt" else shlex.join(cmd)


def open_folder(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


# --------------------------------------------------------------------------- conan runner
class Conan:
    """Runs conan commands synchronously (call from a worker thread)."""

    def __init__(self, app: "App"):
        self.app = app
        self.proc: subprocess.Popen | None = None
        self.cancelled = False
        self._home: Path | None = None

    def env(self) -> dict:
        return ir.conan_env(self.app.settings, *self.app.creds)

    def run(self, args: list[str], check: bool = True, quiet: bool = False,
            env: dict | None = None) -> str:
        if self.cancelled:
            raise Cancelled()
        exe = shutil.which(self.app.settings["conan_exe"] or "conan") or self.app.settings["conan_exe"]
        cmd = [exe, *args]
        if not quiet:
            self.app.log("$ " + fmt_cmd(["conan", *args]), "cmd")
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        kw = {"creationflags": 0x08000000} if os.name == "nt" else {}  # CREATE_NO_WINDOW
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                                 errors="replace", env=env or self.env(), cwd=WORK_DIR, **kw)
        except OSError as e:
            raise ConanError(f"Could not run '{exe}': {e}. Set the Conan executable in Settings.")
        self.proc = p

        tail: list[str] = []

        def pump():
            assert p.stderr
            for line in p.stderr:
                line = ANSI.sub("", line.rstrip("\n"))
                tail.append(line)
                del tail[:-5]
                if not quiet:
                    self.app.log(line)

        t = threading.Thread(target=pump, daemon=True)
        t.start()
        out = p.stdout.read() if p.stdout else ""
        p.wait()
        t.join()
        self.proc = None
        if self.cancelled:
            raise Cancelled()
        if check and p.returncode != 0:
            last = next((ln for ln in reversed(tail) if ln.strip()), "")
            raise ConanError(f"'conan {' '.join(args[:2])}' failed (exit {p.returncode}): {last}")
        return out

    def json(self, args: list[str], quiet: bool = False):
        out = self.run([*args, "--format=json"], quiet=quiet)
        try:
            return json.loads(out) if out.strip() else {}
        except json.JSONDecodeError:
            raise ConanError(f"Unexpected (non-JSON) output from 'conan {args[0]}'")

    def home(self) -> Path:
        if self._home is None:
            self._home = Path(self.run(["config", "home"], quiet=True).strip())
        return self._home

    def cancel(self) -> None:
        self.cancelled = True
        p = self.proc
        if p and p.poll() is None:
            if os.name == "nt":  # kill the whole tree (cmake / msbuild children)
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)],
                               capture_output=True, creationflags=0x08000000)
            else:
                p.terminate()

    # ---- remote queries -------------------------------------------------
    def remote_section(self, data: dict, remote: str) -> dict:
        r = data.get(remote, {})
        if isinstance(r, dict) and "error" in r:
            msg = str(r["error"])
            if "404" in msg or "not found" in msg.lower():
                return {}
            raise ConanError(f"{remote}: {msg}")
        return r

    def list_remote(self, pattern: str, remote: str, quiet: bool = True) -> dict:
        return self.remote_section(self.json(["list", pattern, "-r", remote], quiet=quiet), remote)

    def search(self, pattern: str, remote: str) -> list[str]:
        return list(self.remote_section(self.json(["search", pattern, "-r", remote]), remote))


# --------------------------------------------------------------------------- mirror logic
def source_remotes(conan: Conan, s: dict) -> list[str]:
    internal = s.get("internal_remote", "")
    enabled = {r["name"] for r in conan.json(["remote", "list"], quiet=True) if r.get("enabled", True)}
    return ([internal] if internal and internal in enabled else []) + [s["center_remote"]]


def conanfile_path(ref: str) -> Path | None:
    """The conanfile a mirror entry points at, or None if the entry is a name/version reference."""
    p = Path(ref)
    if not p.is_absolute():
        return None
    if p.is_file():
        return p
    for name in ("conanfile.py", "conanfile.txt"):
        if (p / name).is_file():
            return p / name
    return None


def split_refs(raw: str) -> list[str]:
    """One entry per line. A line is a conanfile path (may contain spaces) or references."""
    out: list[str] = []
    for line in raw.splitlines():
        line = line.strip().strip('"')
        if not line:
            continue
        if conanfile_path(line):
            out.append(line)
        else:
            out += [r for r in re.split(r"[\s,]+", line) if r]
    return out


def load_manifest(path: Path) -> list[tuple[str, str]]:
    """mirror.json -> [(reference or conanfile path, profile), ...].

    {"profiles": ["p1", "p2"],
     "packages": ["fmt/11.0.2", "../app/conanfile.py", {"ref": "zlib/1.3.1", "profiles": ["p1"]}]}
    Relative conanfile paths are relative to the manifest.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ConanError(f"Could not read manifest {path}: {e}")
    default_profiles = data.get("profiles") or []
    jobs: list[tuple[str, str]] = []
    for entry in data.get("packages") or []:
        ref = entry if isinstance(entry, str) else entry.get("ref", "")
        profiles = default_profiles if isinstance(entry, str) else entry.get("profiles") or default_profiles
        if not ref:
            raise ConanError(f"Manifest {path.name}: a package entry has no 'ref'")
        if not profiles:
            raise ConanError(f"Manifest {path.name}: no profiles for {ref}")
        if re.search(r"conanfile\.(py|txt)$", ref) or ref.startswith("."):
            ref = str((path.parent / ref).resolve())
        jobs += [(ref, prof) for prof in profiles]
    if not jobs:
        raise ConanError(f"Manifest {path.name} lists no packages")
    return jobs


def save_manifest(path: Path, refs: list[str], profiles: list[str]) -> None:
    packages = []
    for r in refs:
        cf = conanfile_path(r)
        if cf:      # keep paths inside the repo's parent tree relative, so the file is portable
            try:
                r = Path(os.path.relpath(cf, path.parent)).as_posix()
                r = r if r.startswith(".") else "./" + r
            except ValueError:      # different drive on Windows
                r = str(cf)
        packages.append(r)
    path.write_text(json.dumps({"profiles": profiles, "packages": packages}, indent=2) + "\n",
                    encoding="utf-8")


@dataclass
class Options:
    build_missing: bool = True
    fetch_sources: bool = True
    skip_existing: bool = True
    dry_run: bool = False
    verify: bool = True          # afterwards, install from Nexus alone in a clean Conan home
    verify_sources: bool = True  # ... and check every source archive can be fetched from Nexus


@dataclass
class PlanRow:
    profile: str
    ref: str             # name/version#rrev
    context: str
    package_id: str
    binary: str          # conan's status: Download / Build / Missing / Cache / Skip / Invalid
    nexus_recipe: str    # yes / no / other rev
    nexus_binary: bool

    @property
    def done(self) -> bool:
        return self.nexus_recipe == "yes" and (self.nexus_binary or self.binary == "Skip")

    @property
    def action(self) -> str:
        if self.done:
            return "already in Nexus"
        return {
            "Download": "download + upload",
            "Cache": "upload (in local cache)",
            "Update": "download newer + upload",
            "Build": "BUILD from source + upload",
            "Missing": "MISSING - enable 'build missing'",
            "Invalid": "INVALID for this profile",
            "Skip": "recipe only",
        }.get(self.binary, self.binary)


class Mirror:
    def __init__(self, conan: Conan, settings: dict):
        self.c = conan
        self.s = settings
        self._nexus_cache: dict[str, tuple[str, set[str]]] = {}
        self._sources: list[str] | None = None
        self._verify_env: dict | None = None

    def sources(self) -> list[str]:
        """Remotes packages are taken from: our own recipes (if registered), then ConanCenter."""
        if self._sources is None:
            self._sources = source_remotes(self.c, self.s)
        return self._sources

    @staticmethod
    def _target(ref: str) -> list[str]:
        """A reference is installed with --requires; a project's conanfile by its path, so the
        project's own options, overrides and conan.lock (if next to it) shape the graph."""
        cf = conanfile_path(ref)
        return [str(cf)] if cf else ["--requires", ref]

    @staticmethod
    def _safe(ref: str, profile: str) -> str:
        cf = conanfile_path(ref)
        label = f"{cf.parent.name}_{cf.name}" if cf else ref
        return re.sub(r"[^A-Za-z0-9._-]", "_", f"{label}_{profile}")

    def _out(self, ref: str, profile: str) -> list[str]:
        # keep generated files out of the project folder when installing a conanfile
        if conanfile_path(ref):
            return ["--output-folder", str(WORK_DIR / f"out_{self._safe(ref, profile)}")]
        return []

    def _common(self, ref: str, profile: str, opts: Options) -> list[str]:
        # Build tools (cmake, ninja, ...) use one fixed Release profile when set, so a Debug
        # host profile does not ask for Debug tools that have to be built from source.
        a = [*self._target(ref), "-pr:h", profile, "-pr:b", self.s.get("build_profile") or profile]
        for r in self.sources():
            a += ["-r", r]
        # --update: use the newest recipe revision, not a stale copy in the local cache
        # (essential for ./recipes, where an edited recipe is otherwise ignored)
        a += ["--update", "-c", "tools.graph:skip_binaries=False"]
        if opts.build_missing:
            a.append("--build=missing")
        return a

    def nexus_state(self, ref_rev: str) -> tuple[str, set[str]]:
        if ref_rev in self._nexus_cache:
            return self._nexus_cache[ref_rev]
        nexus = self.s["nexus_remote"]
        name_ver, _, rrev = ref_rev.partition("#")
        found = self.c.list_remote(f"{ref_rev}:*", nexus)
        if found:
            revs = found.get(name_ver, {}).get("revisions", {})
            pkgs = set((revs.get(rrev) or {}).get("packages", {}) or {})
            state = ("yes", pkgs)
        else:
            other = self.c.list_remote(f"{name_ver}#*", nexus)
            state = ("other rev" if other else "no", set())
        self._nexus_cache[ref_rev] = state
        return state

    def plan(self, ref: str, profile: str, opts: Options) -> list[PlanRow]:
        g = self.c.json(["graph", "info", *self._common(ref, profile, opts)])
        rows = []
        for nid, n in g.get("graph", {}).get("nodes", {}).items():
            r = n.get("ref") or ""
            if nid == "0" or "/" not in r:
                continue
            ref_rev = strip_ts(r)
            pkg = n.get("package_id") or ""
            rec, pkgs = self.nexus_state(ref_rev)
            rows.append(PlanRow(profile, ref_rev, n.get("context", ""), pkg,
                                n.get("binary") or "?", rec, pkg in pkgs))
        return rows

    def mirror(self, ref: str, profile: str, opts: Options, log, on_plan) -> str:
        if opts.skip_existing:
            rows = self.plan(ref, profile, opts)
            on_plan(rows)
            if rows and all(r.done for r in rows):
                log(f"== {ref} [{profile}]: everything already in Nexus - skipped", "ok")
                return "skipped"
        safe = self._safe(ref, profile)
        graph_file = WORK_DIR / f"graph_{safe}.json"
        list_file = WORK_DIR / f"pkglist_{safe}.json"

        args = ["install", *self._common(ref, profile, opts), *self._out(ref, profile)]
        if opts.fetch_sources:   # make sure sources land in the backup cache
            args += ["-c", "tools.build:download_source=True"]
        graph_file.write_text(self.c.run([*args, "--format=json"]), encoding="utf-8")

        # NB: --graph-binaries is an "append" option: one value per flag, not comma-separated.
        pkglist = self.c.run(["list", f"--graph={graph_file}", "--graph-recipes=*",
                              "--graph-binaries=download", "--graph-binaries=build",
                              "--graph-binaries=cache", "--graph-binaries=update",
                              "--format=json"])
        list_file.write_text(pkglist, encoding="utf-8")

        up = ["upload", f"--list={list_file}", "-r", self.s["nexus_remote"], "-c"]
        if opts.dry_run:
            up.append("--dry-run")
        self.c.run(up)
        self._nexus_cache.clear()
        log(f"== {ref} [{profile}]: {'dry run complete' if opts.dry_run else 'uploaded'}", "ok")
        return "uploaded"

    # ---- verify: can a developer install this from Nexus alone? -----------
    def _prepare_verify(self, log) -> dict:
        """A throwaway Conan home built from the team config: only Nexus, no proxy, empty cache."""
        if self._verify_env is None:
            home = WORK_DIR / "verify_home"
            mirror_home = self.c.home()
            profiles = [p.relative_to(mirror_home / "profiles").as_posix()
                        for p in sorted((mirror_home / "profiles").rglob("*")) if p.is_file()]
            try:
                tc.build(home, self.s, profiles, mirror_home, log=log)
            except (tc.TeamConfigError, OSError) as e:
                raise ConanError(f"Could not prepare the verify home: {e}")
            env = ir.conan_env({**self.s, "proxy": "", "conan_home": str(home)}, *self.c.app.creds)
            for k in ("CONAN_MIRROR_RAW_USER", "CONAN_MIRROR_RAW_PASSWORD"):
                env.pop(k, None)    # developers have no raw-repo credentials
            self._verify_env = env
            log(f"Verify home: {home} (team config, Nexus only, empty cache)")
        return self._verify_env

    def verify(self, ref: str, profile: str, opts: Options, log) -> None:
        """Install from Nexus only. Raises ConanError if a binary or a source is missing."""
        env = self._prepare_verify(log)
        args = ["install", *self._target(ref), "-pr:h", profile,
                "-pr:b", self.s.get("build_profile") or profile,
                "-r", self.s["nexus_remote"], *self._out(ref, profile)]
        if opts.verify_sources and self.s.get("raw_url"):
            # fetch every source archive too, as --build=missing on a developer machine would
            args += ["-c", "tools.build:download_source=True"]
        try:
            self.c.run(args, env=env)
        except ConanError as e:
            if "source backup server" in str(e):
                raise ConanError(
                    f"{e}\n   The raw repo refused a download without a login. Developers have no "
                    "raw-repo login, so --build=missing would fail for them the same way. Allow "
                    "anonymous read on the raw repo, or untick 'Verify sources too' if the team "
                    "never builds from source.")
            raise
        log(f"== {ref} [{profile}]: verified from Nexus", "ok")


def run_jobs(m: Mirror, jobs: list[tuple[str, str]], opts: Options, log, on_plan) -> list[tuple[str, str, str]]:
    """Mirror (and verify) each (ref, profile). Returns [(ref, profile, result)]; never raises ConanError."""
    results = []
    for ref, p in jobs:
        log(f"== Mirror {ref} [{p}]", "cmd")
        try:
            res = m.mirror(ref, p, opts, log, lambda rows, r=ref: on_plan(r, rows))
        except ConanError as e:
            log(f"{ref} [{p}] FAILED: {e}", "err")
            results.append((ref, p, "FAILED"))
            continue
        if opts.verify and not opts.dry_run and res == "uploaded":
            try:
                m.verify(ref, p, opts, log)
                res = "uploaded + verified"
            except ConanError as e:
                log(f"{ref} [{p}] uploaded, but installing it from Nexus alone FAILED: {e}", "err")
                res = "VERIFY FAILED"
        results.append((ref, p, res))
    log("== Summary", "cmd")
    for ref, p, res in results:
        log(f"   {res:19} {ref} [{p}]", "err" if "FAILED" in res else "ok")
    return results


def run_verify(m: Mirror, jobs: list[tuple[str, str]], opts: Options, log) -> list[tuple[str, str, str]]:
    results = []
    for ref, p in jobs:
        log(f"== Verify {ref} [{p}]", "cmd")
        try:
            m.verify(ref, p, opts, log)
            results.append((ref, p, "verified"))
        except ConanError as e:
            log(f"{ref} [{p}] cannot be installed from Nexus alone: {e}", "err")
            results.append((ref, p, "VERIFY FAILED"))
    log("== Summary", "cmd")
    for ref, p, res in results:
        log(f"   {res:14} {ref} [{p}]", "err" if "FAILED" in res else "ok")
    return results


# --------------------------------------------------------------------------- conan home setup
def apply_config(conan: Conan, s: dict, log) -> None:
    """Set up the mirror's Conan home: remotes, global.conf, source credentials, MSVC fallbacks."""
    remotes = {r["name"]: r for r in conan.json(["remote", "list"], quiet=True)}
    for name, url in ((s["center_remote"], s["center_url"]), (s["nexus_remote"], s["nexus_url"])):
        if not url:
            continue
        if name not in remotes:
            conan.run(["remote", "add", name, url])
        elif remotes[name]["url"].rstrip("/") != url.rstrip("/"):
            conan.run(["remote", "update", name, "--url", url])
        else:
            log(f"Remote '{name}' already set to {url}")
        if name in remotes and not remotes[name].get("enabled", True):
            conan.run(["remote", "enable", name])

    if s["internal_remote"] and ir.list_packages():
        ir.ensure_remote(lambda a: conan.run(a, quiet=True), s["internal_remote"], log=log)

    home = conan.home()
    lines = []
    if s["raw_url"]:
        # list values are Python literals; plain string values must NOT be quoted
        lines += [f"core.sources:download_urls = {json.dumps(['origin', s['raw_url']])}",
                  f"core.sources:upload_url = {s['raw_url']}"]
    if s["cacert_path"]:
        lines.append(f"core.net.http:cacert_path = {s['cacert_path']}")
    write_managed_block(home / "global.conf", lines, log)

    creds = home / "source_credentials.json"
    if s["raw_url"]:
        if creds.exists() and "CONAN_MIRROR_RAW" not in creds.read_text(encoding="utf-8"):
            backup = creds.with_suffix(".json.bak")
            shutil.copy2(creds, backup)
            log(f"Backed up existing {creds.name} to {backup.name}")
        creds.write_text(
            '{% set u = os.getenv("CONAN_MIRROR_RAW_USER", "") %}'
            '{% set p = os.getenv("CONAN_MIRROR_RAW_PASSWORD", "") %}\n'
            + json.dumps({"credentials": [{"url": s["raw_url"], "user": "@@U@@", "password": "@@P@@"}]},
                         indent=2).replace('"@@U@@"', "{{ u|tojson }}").replace('"@@P@@"', "{{ p|tojson }}")
            + "\n", encoding="utf-8")
        log(f"Wrote {creds}", "ok")

    # the same MSVC fallbacks the team config ships, so Plan judges binaries as developers will
    compat = tc.patched_compat(home, s.get("msvc_fallbacks", tc.DEFAULT_FALLBACKS))
    if compat is not None:
        (home / tc.COMPAT).write_text(compat, encoding="utf-8")
        log("Compatibility plugin: MSVC fallbacks "
                 + ", ".join(f"{a}->{b}" for a, b in s.get("msvc_fallbacks", tc.DEFAULT_FALLBACKS).items()))
    elif s.get("msvc_fallbacks", tc.DEFAULT_FALLBACKS):
        log("WARNING: could not add the MSVC fallbacks to the compatibility plugin "
                 "(unexpected layout in this Conan version)", "err")
    conan.run(["remote", "list"])


def write_managed_block(path: Path, lines: list[str], log) -> None:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    bak = path.with_suffix(path.suffix + ".bak")
    if path.exists() and not bak.exists():
        shutil.copy2(path, bak)
        log(f"Backed up {path.name} to {bak.name}")
    block_re = re.compile(re.escape(BLOCK_START) + r".*?" + re.escape(BLOCK_END) + r"\n?", re.S)
    text = block_re.sub("", text).rstrip("\n")
    for key in {ln.split("=")[0].strip() for ln in lines}:
        if re.search(rf"^\s*{re.escape(key)}\s*=", text, re.M):
            log(f"WARNING: {path.name} already sets {key} outside the managed block "
                     f"- the managed value (later in the file) wins", "err")
    if lines:
        text = (text + "\n\n" if text else "") + "\n".join([BLOCK_START, *lines, BLOCK_END])
    path.write_text(text + "\n", encoding="utf-8")
    log(f"Updated {path}", "ok")


# --------------------------------------------------------------------------- profile templates
MSVC_VERSIONS = ["195", "194", "193", "192", "191"]


def make_profile(os_: str, arch: str, compiler: str, version: str, cppstd: str,
                 build_type: str, runtime: str, cmake_gen: str) -> str:
    lines = ["[settings]", f"os={os_}", f"arch={arch}", f"build_type={build_type}",
             f"compiler={compiler}", f"compiler.version={version}", f"compiler.cppstd={cppstd}"]
    if compiler == "msvc":
        lines += [f"compiler.runtime={runtime}",
                  f"compiler.runtime_type={'Debug' if build_type == 'Debug' else 'Release'}"]
    elif compiler in ("gcc", "clang") and os_ == "Linux":
        lines.append("compiler.libcxx=libstdc++11")
    elif compiler == "apple-clang":
        lines.append("compiler.libcxx=libc++")
    if cmake_gen:
        lines += ["", "[conf]", f"tools.cmake.cmaketoolchain:generator={cmake_gen}"]
    return "\n".join(lines) + "\n"


class ProfileWizard(tk.Toplevel):
    """Generate a matrix of profiles (cppstd x build_type)."""

    def __init__(self, master, on_create):
        super().__init__(master)
        self.title("New profiles from template")
        self.on_create = on_create
        self.transient(master)
        f = ttk.Frame(self, padding=12)
        f.pack(fill="both", expand=True)

        self.v = {k: tk.StringVar(value=d) for k, d in {
            "os": "Windows", "arch": "x86_64", "compiler": "msvc", "version": "194",
            "runtime": "dynamic", "cmake_gen": "", "prefix": "win-msvc194"}.items()}
        choices = {
            "os": ["Windows", "Linux", "Macos"],
            "arch": ["x86_64", "armv8", "x86"],
            "compiler": ["msvc", "gcc", "clang", "apple-clang"],
            "version": MSVC_VERSIONS + ["13", "12", "11", "17", "16"],
            "runtime": ["dynamic", "static"],
            "cmake_gen": ["", "Ninja", "Visual Studio 17 2022"],
        }
        labels = {"os": "OS", "arch": "Arch", "compiler": "Compiler", "version": "Compiler version",
                  "runtime": "MSVC runtime", "cmake_gen": "CMake generator (optional)",
                  "prefix": "Name prefix"}
        for i, k in enumerate(labels):
            ttk.Label(f, text=labels[k]).grid(row=i, column=0, sticky="w", pady=2)
            if k in choices:
                w = ttk.Combobox(f, textvariable=self.v[k], values=choices[k], width=28)
            else:
                w = ttk.Entry(f, textvariable=self.v[k], width=30)
            w.grid(row=i, column=1, sticky="ew", pady=2)
        for k in ("os", "compiler", "version"):
            self.v[k].trace_add("write", lambda *_: self._auto_prefix())

        r = len(labels)
        ttk.Label(f, text="C++ standards").grid(row=r, column=0, sticky="nw", pady=(8, 2))
        sf = ttk.Frame(f)
        sf.grid(row=r, column=1, sticky="w", pady=(8, 2))
        self.stds = {s: tk.BooleanVar(value=s in ("20",)) for s in ("11", "14", "17", "20", "23")}
        for s, var in self.stds.items():
            ttk.Checkbutton(sf, text=f"C++{s}", variable=var).pack(side="left")
        ttk.Label(f, text="Build types").grid(row=r + 1, column=0, sticky="nw", pady=2)
        bf = ttk.Frame(f)
        bf.grid(row=r + 1, column=1, sticky="w", pady=2)
        self.bts = {b: tk.BooleanVar(value=b in ("Release", "Debug"))
                    for b in ("Release", "Debug", "RelWithDebInfo")}
        for b, var in self.bts.items():
            ttk.Checkbutton(bf, text=b, variable=var).pack(side="left")
        ttk.Label(f, foreground="gray", wraplength=380, justify="left",
                  text="MSVC has no C++11 mode (minimum is 14). ConanCenter binaries are usually "
                       "built with the compiler's default standard; Conan's compatibility plugin "
                       "reuses them for other standards where possible.").grid(
            row=r + 2, column=0, columnspan=2, sticky="w", pady=6)
        btns = ttk.Frame(f)
        btns.grid(row=r + 3, column=0, columnspan=2, sticky="e")
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(btns, text="Create", command=self._create).pack(side="right", padx=6)
        f.columnconfigure(1, weight=1)
        self.grab_set()

    def _auto_prefix(self):
        os_short = {"Windows": "win", "Linux": "linux", "Macos": "mac"}.get(self.v["os"].get(), "x")
        self.v["prefix"].set(f"{os_short}-{self.v['compiler'].get()}{self.v['version'].get()}")

    def _create(self):
        v = {k: x.get().strip() for k, x in self.v.items()}
        stds = [s for s, x in self.stds.items() if x.get()]
        bts = [b for b, x in self.bts.items() if x.get()]
        if not stds or not bts:
            messagebox.showwarning(APP_NAME, "Pick at least one C++ standard and build type.", parent=self)
            return
        out = {}
        for std in stds:
            if v["compiler"] == "msvc" and std == "11":
                continue
            for bt in bts:
                name = f"{v['prefix']}-cpp{std}-{bt.lower()}"
                out[name] = make_profile(v["os"], v["arch"], v["compiler"], v["version"], std, bt,
                                         v["runtime"], v["cmake_gen"])
        self.destroy()
        self.on_create(out)


# --------------------------------------------------------------------------- GUI
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1200x800")
        self.minsize(900, 600)
        self.settings = load_settings()
        self.creds: tuple[str, str] = ("", "")
        self.conan = Conan(self)
        self.q: queue.Queue = queue.Queue()
        self.busy = False
        self.profiles: list[str] = []
        self.current_profile: str | None = None
        self.center_bins: dict = {}

        self._build_ui()
        self.after(100, self._drain)
        self.run_task("Loading profiles", self._task_load_profiles)

    # ---- threading plumbing ---------------------------------------------
    def log(self, text: str, tag: str = "") -> None:
        self.q.put(("log", text, tag))

    def ui(self, fn, *args) -> None:
        self.q.put(("ui", fn, args))

    def _drain(self):
        try:
            while True:
                item = self.q.get_nowait()
                try:
                    if item[0] == "log":
                        self._append_log(item[1], item[2])
                    else:
                        item[1](*item[2])
                except Exception as e:  # noqa: BLE001 - one bad update must not stop the queue
                    try:
                        self._append_log(f"ERROR in UI update: {type(e).__name__}: {e}", "err")
                    except Exception:  # noqa: BLE001
                        pass
        except queue.Empty:
            pass
        finally:
            self.after(80, self._drain)

    def run_task(self, title: str, fn, *args) -> None:
        if self.busy:
            messagebox.showinfo(APP_NAME, "Another task is running - wait or press Cancel.")
            return
        self.busy = True
        self.conan.cancelled = False
        self.status.set(f"{title}...")
        self.progress.start(12)
        self.cancel_btn.state(["!disabled"])

        def worker():
            try:
                fn(*args)
                self.ui(self.status.set, f"{title}: done")
            except Cancelled:
                self.log("Cancelled.", "err")
                self.ui(self.status.set, f"{title}: cancelled")
            except ConanError as e:
                self.log(f"ERROR: {e}", "err")
                self.ui(self.status.set, f"{title}: failed")
            except Exception as e:  # noqa: BLE001
                self.log(f"ERROR: {type(e).__name__}: {e}", "err")
                self.ui(self.status.set, f"{title}: failed")
            finally:
                self.ui(self._task_done)

        threading.Thread(target=worker, daemon=True).start()

    def _task_done(self):
        self.busy = False
        self.progress.stop()
        self.cancel_btn.state(["disabled"])

    # ---- layout -----------------------------------------------------------
    def _build_ui(self):
        apply_theme(self, bool(self.settings.get("dark_mode")))
        paned = ttk.PanedWindow(self, orient="vertical")
        paned.pack(fill="both", expand=True, padx=6, pady=(6, 0))
        self.nb = ttk.Notebook(paned)
        paned.add(self.nb, weight=3)

        logf = ttk.Frame(paned)
        paned.add(logf, weight=1)
        bar = ttk.Frame(logf)
        bar.pack(fill="x")
        ttk.Label(bar, text="Log").pack(side="left")
        ttk.Button(bar, text="Clear", command=lambda: self.logbox.delete("1.0", "end")).pack(side="right")
        mono = ("Consolas", 9) if sys.platform == "win32" else ("Menlo", 11)
        self.logbox = ScrolledText(logf, height=10, font=mono, wrap="none")
        self.logbox.pack(fill="both", expand=True)
        for tag in ("cmd", "err", "ok"):
            self.logbox.tag_configure(tag, foreground=TAGS[tag])

        sb = ttk.Frame(self)
        sb.pack(fill="x", padx=6, pady=4)
        self.status = tk.StringVar(value="Ready")
        ttk.Label(sb, textvariable=self.status).pack(side="left")
        self.cancel_btn = ttk.Button(sb, text="Cancel", command=self.conan.cancel)
        self.cancel_btn.pack(side="right")
        self.cancel_btn.state(["disabled"])
        self.progress = ttk.Progressbar(sb, mode="indeterminate", length=160)
        self.progress.pack(side="right", padx=6)

        self._build_search_tab()
        self._build_plan_tab()
        self._build_internal_tab()
        self._build_profiles_tab()
        self._build_settings_tab()

    def _append_log(self, text: str, tag: str):
        at_end = self.logbox.yview()[1] > 0.98
        self.logbox.insert("end", text + "\n", tag)
        if int(self.logbox.index("end-1c").split(".")[0]) > 20000:
            self.logbox.delete("1.0", "2000.0")
        if at_end:
            self.logbox.see("end")

    @staticmethod
    def _tree(parent, cols: dict[str, int], selectmode="browse") -> ttk.Treeview:
        frame = ttk.Frame(parent)
        tv = ttk.Treeview(frame, columns=list(cols)[1:], selectmode=selectmode)
        for i, (c, w) in enumerate(cols.items()):
            cid = "#0" if i == 0 else c
            tv.heading(cid, text=c, anchor="w")
            tv.column(cid, width=w, stretch=(i == 0), anchor="w")
        ys = ttk.Scrollbar(frame, orient="vertical", command=tv.yview)
        xs = ttk.Scrollbar(frame, orient="horizontal", command=tv.xview)
        tv.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        tv.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        tv.frame = frame  # type: ignore[attr-defined]
        tv.tag_configure("good", foreground=TAGS["ok"])
        tv.tag_configure("bad", foreground=TAGS["err"])
        tv.tag_configure("warn", foreground=TAGS["warn"])
        return tv

    # ---- Search tab -----------------------------------------------------
    def _build_search_tab(self):
        tab = ttk.Frame(self.nb, padding=6)
        self.nb.add(tab, text="Search & Mirror")

        top = ttk.Frame(tab)
        top.pack(fill="x")
        ttk.Label(top, text="Search ConanCenter + internal:").pack(side="left")
        self.search_var = tk.StringVar()
        e = ttk.Entry(top, textvariable=self.search_var, width=30)
        e.pack(side="left", padx=4)
        e.bind("<Return>", lambda _e: self.on_search())
        ttk.Button(top, text="Search", command=self.on_search).pack(side="left")
        self.check_nexus = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="compare with Nexus", variable=self.check_nexus).pack(side="left", padx=8)
        ttk.Label(top, text="(wildcards ok: boost*, *json*)", foreground="gray").pack(side="left")

        body = ttk.PanedWindow(tab, orient="horizontal")
        body.pack(fill="both", expand=True, pady=6)

        # results
        self.results = self._tree(body, {"Reference": 220, "In Nexus": 80})
        body.add(self.results.frame, weight=1)
        self.results.bind("<<TreeviewSelect>>", self.on_result_select)

        # binaries
        mid = ttk.Frame(body)
        body.add(mid, weight=3)
        self.bin_label = tk.StringVar(value="Select a version to list its ConanCenter binaries")
        ttk.Label(mid, textvariable=self.bin_label).pack(anchor="w")
        self.bins = self._tree(mid, {"Package ID": 120, "os": 70, "arch": 60, "compiler": 80,
                                     "ver": 45, "cppstd": 50, "build_type": 80, "runtime": 65,
                                     "shared": 55, "In Nexus": 70})
        self.bins.frame.pack(fill="both", expand=True)

        # mirror panel
        right = ttk.LabelFrame(body, text="Mirror", padding=6)
        body.add(right, weight=1)
        ttk.Label(right, text="References or conanfiles - one per line:").pack(anchor="w")
        self.refs_text = tk.Text(right, height=5, width=28, wrap="none")
        self.refs_text.pack(fill="x")
        rf = ttk.Frame(right)
        rf.pack(fill="x", pady=(2, 0))
        for txt, cmd in (("Add conanfile...", self.on_add_conanfile),
                         ("Load manifest", self.on_manifest_load),
                         ("Save manifest", self.on_manifest_save)):
            ttk.Button(rf, text=txt, command=cmd).pack(side="left", expand=True, fill="x")
        ttk.Label(right, text="Profiles (multi-select):").pack(anchor="w", pady=(6, 0))
        lf = ttk.Frame(right)
        lf.pack(fill="both", expand=True)
        self.prof_list = tk.Listbox(lf, selectmode="extended", exportselection=False, height=8)
        self.prof_list.pack(side="left", fill="both", expand=True)
        ps = ttk.Scrollbar(lf, command=self.prof_list.yview)
        ps.pack(side="right", fill="y")
        self.prof_list.configure(yscrollcommand=ps.set)

        self.opt = {
            "build_missing": tk.BooleanVar(value=True),
            "fetch_sources": tk.BooleanVar(value=True),
            "skip_existing": tk.BooleanVar(value=True),
            "dry_run": tk.BooleanVar(value=False),
            "verify": tk.BooleanVar(value=True),
            "verify_sources": tk.BooleanVar(value=True),
        }
        for k, txt in (("build_missing", "Build missing binaries from source"),
                       ("fetch_sources", "Fetch sources (upload to raw repo)"),
                       ("skip_existing", "Skip if already complete in Nexus"),
                       ("dry_run", "Dry run (don't actually upload)"),
                       ("verify", "Verify afterwards (install from Nexus only)"),
                       ("verify_sources", "Verify sources too (raw repo, no login)")):
            ttk.Checkbutton(right, text=txt, variable=self.opt[k]).pack(anchor="w")
        bf = ttk.Frame(right)
        bf.pack(fill="x", pady=(6, 0))
        ttk.Button(bf, text="Plan", command=self.on_plan).pack(side="left", expand=True, fill="x")
        ttk.Button(bf, text="Mirror to Nexus", command=self.on_mirror).pack(side="left", expand=True,
                                                                             fill="x", padx=(4, 0))
        ttk.Button(right, text="Verify only (install from Nexus)", command=self.on_verify).pack(
            fill="x", pady=(4, 0))

    def options(self) -> Options:
        return Options(**{k: v.get() for k, v in self.opt.items()})

    def selected_refs(self) -> list[str]:
        return split_refs(self.refs_text.get("1.0", "end"))

    def _set_refs(self, refs: list[str]):
        self.refs_text.delete("1.0", "end")
        self.refs_text.insert("1.0", "\n".join(refs))

    def on_add_conanfile(self):
        f = filedialog.askopenfilename(title="Project conanfile", filetypes=[
            ("conanfile", "conanfile.py conanfile.txt"), ("All files", "*")])
        if f and f not in self.selected_refs():
            self._set_refs(self.selected_refs() + [str(Path(f))])

    def on_manifest_load(self):
        f = filedialog.askopenfilename(title="Load manifest", initialdir=str(HERE),
                                       initialfile=MANIFEST_FILE.name, filetypes=[("JSON", "*.json")])
        if not f:
            return
        try:
            jobs = load_manifest(Path(f))
        except ConanError as e:
            messagebox.showerror(APP_NAME, str(e))
            return
        refs = list(dict.fromkeys(r for r, _ in jobs))
        profs = set(p for _, p in jobs)
        self._set_refs(refs)
        self.prof_list.selection_clear(0, "end")
        for i, name in enumerate(self.profiles):
            if name in profs:
                self.prof_list.selection_set(i)
        missing = sorted(profs - set(self.profiles))
        self.log(f"Loaded {len(refs)} package(s) x {len(profs)} profile(s) from {f}", "ok")
        if missing:
            self.log(f"WARNING: profiles in the manifest that do not exist here: {', '.join(missing)}", "err")
        if len(jobs) != len(refs) * len(profs):
            self.log("NOTE: this manifest gives some packages their own profile list; the GUI mirrors "
                     "every listed package with every selected profile. Use --manifest on the command "
                     "line to follow it exactly.")

    def on_manifest_save(self):
        refs, profs = self.selected_refs(), self.selected_profiles()
        if not refs or not profs:
            messagebox.showwarning(APP_NAME, "Enter at least one package and select at least one profile.")
            return
        f = filedialog.asksaveasfilename(title="Save manifest", initialdir=str(HERE),
                                         initialfile=MANIFEST_FILE.name, defaultextension=".json",
                                         filetypes=[("JSON", "*.json")])
        if f:
            save_manifest(Path(f), refs, profs)
            self.log(f"Manifest written to {f} - commit it so the mirror can be repeated", "ok")

    def selected_profiles(self) -> list[str]:
        return [self.prof_list.get(i) for i in self.prof_list.curselection()]

    def on_search(self):
        pat = self.search_var.get().strip()
        if not pat:
            return
        if "*" not in pat and "/" not in pat:
            pat = f"*{pat}*"
        self.run_task(f"Searching '{pat}'", self._task_search, pat, self.check_nexus.get())

    def _task_search(self, pat: str, compare: bool):
        s = self.settings
        refs: list[str] = []
        for remote in source_remotes(self.conan, s):
            refs += [r for r in self.conan.search(pat, remote) if r not in refs]
        in_nexus: set[str] | None = None
        if compare and s["nexus_url"]:
            try:
                in_nexus = set(self.conan.search(pat, s["nexus_remote"]))
            except ConanError as e:
                self.log(f"Nexus search failed ({e}); falling back to per-name list", "err")
                in_nexus = set()
                for name in sorted({r.split("/")[0] for r in refs}):
                    in_nexus |= set(self.conan.list_remote(f"{name}/*", s["nexus_remote"]))
        self.log(f"{len(refs)} versions found", "ok")
        self.ui(self._fill_results, refs, in_nexus)

    def _fill_results(self, refs: list[str], in_nexus: set[str] | None):
        tv = self.results
        tv.delete(*tv.get_children())
        groups: dict[str, list[str]] = {}
        for r in refs:
            groups.setdefault(r.split("/")[0], []).append(r)
        for name in sorted(groups, key=natural_key):
            vers = sorted(groups[name], key=natural_key, reverse=True)
            have = sum(1 for v in vers if in_nexus and v in in_nexus)
            summary = "?" if in_nexus is None else f"{have}/{len(vers)}"
            parent = tv.insert("", "end", text=name, values=(summary,), open=len(groups) == 1)
            for v in vers:
                mark = "?" if in_nexus is None else ("yes" if v in in_nexus else "no")
                tv.insert(parent, "end", iid=v, text=v, values=(mark,),
                          tags=("good",) if mark == "yes" else ())

    def on_result_select(self, _e=None):
        sel = self.results.selection()
        if not sel or "/" not in sel[0]:
            return
        ref = sel[0]
        current = self.selected_refs()
        if ref not in current:
            # replace an existing line for the same package, otherwise append
            name = ref.split("/")[0]
            current = [r for r in current if r.split("/")[0] != name] + [ref]
            self.refs_text.delete("1.0", "end")
            self.refs_text.insert("1.0", "\n".join(current))
        self.run_task(f"Listing binaries of {ref}", self._task_bins, ref)

    def _task_bins(self, ref: str):
        s = self.settings
        center = self.conan.list_remote(f"{ref}#latest:*", s["center_remote"], quiet=False)
        revs = center.get(ref, {}).get("revisions", {})
        if not revs:
            self.ui(self._fill_bins, ref, "", {}, None)
            return
        rrev, data = next(iter(revs.items()))
        pkgs = data.get("packages", {}) or {}
        nexus_pkgs: set[str] | None = None
        if s["nexus_url"]:
            try:
                n = self.conan.list_remote(f"{ref}#{rrev}:*", s["nexus_remote"])
                nrevs = n.get(ref, {}).get("revisions", {})
                nexus_pkgs = set((nrevs.get(rrev) or {}).get("packages", {}) or {})
            except ConanError as e:
                self.log(str(e), "err")
        self.ui(self._fill_bins, ref, rrev, pkgs, nexus_pkgs)

    def _fill_bins(self, ref, rrev, pkgs: dict, nexus_pkgs):
        tv = self.bins
        tv.delete(*tv.get_children())
        self.bin_label.set(f"{ref}#{rrev[:8]} - {len(pkgs)} prebuilt binaries on ConanCenter"
                           + ("" if pkgs else " (will need building from source)"))

        def key(item):
            st = item[1].get("info", {}).get("settings", {})
            return (st.get("os", ""), st.get("compiler", ""), st.get("compiler.version", ""),
                    st.get("build_type", ""))

        for pid, p in sorted(pkgs.items(), key=key):
            info = p.get("info", {})
            st, op = info.get("settings", {}), info.get("options", {})
            have = "?" if nexus_pkgs is None else ("yes" if pid in nexus_pkgs else "no")
            tv.insert("", "end", text=pid, values=(
                st.get("os", "-"), st.get("arch", "-"), st.get("compiler", "-"),
                st.get("compiler.version", "-"), st.get("compiler.cppstd", "-"),
                st.get("build_type", "-"),
                st.get("compiler.runtime", st.get("compiler.libcxx", "-")),
                op.get("shared", "-"), have), tags=("good",) if have == "yes" else ())

    def _check_ready(self, need_nexus=True) -> tuple[list[str], list[str]] | None:
        refs, profs = self.selected_refs(), self.selected_profiles()
        if not refs:
            messagebox.showwarning(APP_NAME, "Enter or select at least one reference (e.g. fmt/11.0.2).")
            return None
        if not profs:
            messagebox.showwarning(APP_NAME, "Select at least one profile in the list.")
            return None
        if need_nexus and not self.settings["nexus_url"]:
            messagebox.showwarning(APP_NAME, "Set the Nexus URL in Settings and press 'Apply to Conan'.")
            return None
        return refs, profs

    def on_plan(self):
        ready = self._check_ready()
        if ready:
            self.plan_tree.delete(*self.plan_tree.get_children())
            self.nb.select(1)
            self.run_task("Planning", self._task_plan, *ready, self.options())

    def _task_plan(self, refs, profs, opts):
        m = Mirror(self.conan, self.settings)
        for ref in refs:
            for p in profs:
                self.log(f"== Plan {ref} [{p}]", "cmd")
                self.ui(self._add_plan_rows, ref, m.plan(ref, p, opts))

    def on_mirror(self):
        ready = self._check_ready()
        if not ready:
            return
        opts = self.options()
        # conan upload sends backup sources whenever the raw repo is configured, whatever the
        # checkboxes say, and internal packages download their sources from it.
        if self.settings["raw_url"] and not self.creds[0]:
            if messagebox.askyesno(APP_NAME, "The Nexus raw repo needs your Nexus credentials for this "
                                             "session (to upload sources, and to download the sources "
                                             "of internal packages). Log in now?"):
                if not self.ask_creds():
                    return
        refs, profs = ready
        n = len(refs) * len(profs)
        if not opts.dry_run and not messagebox.askyesno(
                APP_NAME, f"Mirror {len(refs)} package(s) x {len(profs)} profile(s) "
                          f"({n} job(s)) to '{self.settings['nexus_remote']}'?"):
            return
        self.plan_tree.delete(*self.plan_tree.get_children())
        self.nb.select(1)
        self.run_task("Mirroring", self._task_mirror, refs, profs, opts)

    def _task_mirror(self, refs, profs, opts):
        m = Mirror(self.conan, self.settings)
        results = run_jobs(m, [(r, p) for r in refs for p in profs], opts, self.log,
                           lambda r, rows: self.ui(self._add_plan_rows, r, rows))
        if any("FAILED" in r[2] for r in results):
            raise ConanError("some jobs failed - see the log")

    def on_verify(self):
        ready = self._check_ready()
        if not ready:
            return
        refs, profs = ready
        self.run_task("Verifying", self._task_verify, refs, profs, self.options())

    def _task_verify(self, refs, profs, opts):
        m = Mirror(self.conan, self.settings)
        results = run_verify(m, [(r, p) for r in refs for p in profs], opts, self.log)
        if any("FAILED" in r[2] for r in results):
            raise ConanError("some packages cannot be installed from Nexus alone - see the log")

    # ---- Plan tab ---------------------------------------------------------
    def _build_plan_tab(self):
        tab = ttk.Frame(self.nb, padding=6)
        self.nb.add(tab, text="Plan / Status")
        ttk.Label(tab, text="Every package in the dependency graph per profile, compared with Nexus. "
                            "Green = already in Nexus, orange = will be built from source, "
                            "red = cannot be satisfied.", foreground="gray").pack(anchor="w")
        self.plan_tree = self._tree(tab, {"Reference": 260, "Profile": 190, "Context": 60,
                                          "Package ID": 110, "ConanCenter": 90, "Nexus recipe": 90,
                                          "Nexus binary": 90, "Action": 220})
        self.plan_tree.frame.pack(fill="both", expand=True, pady=4)

    def _add_plan_rows(self, root_ref: str, rows: list[PlanRow]):
        tv = self.plan_tree
        parent = tv.insert("", "end", text=f"{root_ref}  [{rows[0].profile if rows else '?'}]",
                           values=("", "", "", "", "", "", "",
                                   "complete" if rows and all(r.done for r in rows) else "needs work"),
                           open=True)
        for r in rows:
            tag = ("good" if r.done else "bad" if r.binary in ("Missing", "Invalid")
                   else "warn" if r.binary == "Build" else ())
            tv.insert(parent, "end", text=r.ref, tags=(tag,) if tag else (), values=(
                r.profile, r.context, r.package_id[:10], r.binary, r.nexus_recipe,
                "yes" if r.nexus_binary else "no", r.action))

    # ---- Internal packages tab ------------------------------------------
    def _build_internal_tab(self):
        tab = ttk.Frame(self.nb, padding=6)
        self.nb.add(tab, text="Internal packages")
        ttk.Label(tab, foreground="gray", justify="left", wraplength=1100, text=(
            "For libraries that are not on ConanCenter. Download the source archive yourself, then "
            "'Create draft': the archive is uploaded to the Nexus raw repo (upstream/<name>/<version>/) "
            "and a draft recipe is written to ./recipes. Edit the recipe, 'Test build' until it passes, "
            "then 'Add to mirror list' and mirror it like any other package. "
            "See docs/INTERNAL_RECIPES.md.")).pack(anchor="w")
        body = ttk.PanedWindow(tab, orient="horizontal")
        body.pack(fill="both", expand=True, pady=6)

        self.int_tree = self._tree(body, {"Package / version": 220, "Recipe folder": 200})
        body.add(self.int_tree.frame, weight=1)
        self.int_tree.bind("<<TreeviewSelect>>", self.on_internal_select)

        form = ttk.LabelFrame(body, text="New package or new version", padding=8)
        body.add(form, weight=2)
        self.ivars = {k: tk.StringVar(value=d) for k, d in {
            "name": "", "version": "", "archive": "", "start": "cmake template", "base": "",
            "origin": ""}.items()}
        rows = [("name", "Package name", "lower case, e.g. cigi-ccl"),
                ("version", "Version", "e.g. 3.3.3a"),
                ("archive", "Source archive", "the zip / tar.gz you downloaded"),
                ("start", "Start the recipe from", ""),
                ("base", "ConanCenter reference", "only for 'ConanCenter recipe', e.g. cigi-ccl/4.0.6a"),
                ("origin", "Where the archive came from", "recorded as a comment in conandata.yml")]
        for i, (k, label, hint) in enumerate(rows):
            ttk.Label(form, text=label).grid(row=i, column=0, sticky="w", pady=3)
            if k == "start":
                w = ttk.Combobox(form, textvariable=self.ivars[k], state="readonly", width=47, values=[
                    "cmake template", "header-only template", "ConanCenter recipe"])
            else:
                w = ttk.Entry(form, textvariable=self.ivars[k], width=50)
            w.grid(row=i, column=1, sticky="ew", pady=3, padx=6)
            if k == "archive":
                ttk.Button(form, text="Browse...", command=self.on_internal_browse).grid(row=i, column=2, sticky="w")
            else:
                ttk.Label(form, text=hint, foreground="gray").grid(row=i, column=2, sticky="w")
        form.columnconfigure(1, weight=1)
        self.int_upload = tk.BooleanVar(value=True)
        ttk.Checkbutton(form, text="Upload the archive to the Nexus raw repo", variable=self.int_upload).grid(
            row=len(rows), column=1, sticky="w", pady=(4, 0))
        ttk.Label(form, foreground="gray", justify="left", wraplength=560, text=(
            "An existing recipe is never overwritten: for a package that is already listed, this only "
            "adds the new version to it. 'Test build' and 'Add to mirror list' use the name and "
            "version above and the profiles selected on the Search & Mirror tab.")).grid(
            row=len(rows) + 1, column=0, columnspan=3, sticky="w", pady=8)
        bf = ttk.Frame(form)
        bf.grid(row=len(rows) + 2, column=0, columnspan=3, sticky="w")
        for txt, cmd in (("Create draft / add version", self.on_internal_create),
                         ("Open recipe folder", self.on_internal_open),
                         ("Test build", self.on_internal_test),
                         ("Add to mirror list", self.on_internal_add_ref)):
            ttk.Button(bf, text=txt, command=cmd).pack(side="left", padx=(0, 6))
        self._fill_internal()

    def _fill_internal(self):
        tv = self.int_tree
        tv.delete(*tv.get_children())
        for name, versions in ir.list_packages().items():
            parent = tv.insert("", "end", text=name, values=("",), open=True)
            for v, folder in versions.items():
                tv.insert(parent, "end", iid=f"{name}/{v}", text=v, values=(f"recipes/{name}/{folder}",))

    def on_internal_select(self, _e=None):
        sel = self.int_tree.selection()
        if sel and "/" in sel[0]:
            name, version = sel[0].split("/", 1)
            self.ivars["name"].set(name)
            self.ivars["version"].set(version)

    def on_internal_browse(self):
        f = filedialog.askopenfilename(title="Source archive", filetypes=[
            # single extensions only: file dialogs don't match double ones like *.tar.gz
            ("Archives", "*.zip *.gz *.tgz *.bz2 *.xz *.tar"), ("All files", "*")])
        if f:
            self.ivars["archive"].set(f)

    def _internal_ref(self) -> tuple[str, str] | None:
        name, version = self.ivars["name"].get().strip(), self.ivars["version"].get().strip()
        if not name or not version:
            messagebox.showwarning(APP_NAME, "Fill in the package name and version (or pick one in the list).")
            return None
        return name, version

    def on_internal_create(self):
        ref = self._internal_ref()
        if not ref:
            return
        v = {k: x.get().strip() for k, x in self.ivars.items()}
        base = v["base"] if v["start"] == "ConanCenter recipe" else ""
        if v["start"] == "ConanCenter recipe" and not base:
            messagebox.showwarning(APP_NAME, "Enter the ConanCenter reference to start from.")
            return
        if not v["archive"]:
            messagebox.showwarning(APP_NAME, "Choose the source archive you downloaded.")
            return
        upload = self.int_upload.get()
        if upload and not self.creds[0] and not self.ask_creds():
            return
        s = self.settings

        def task():
            d = ir.create_draft(
                ref[0], ref[1], Path(v["archive"]), s["raw_url"], base=base,
                template="header-only" if v["start"].startswith("header") else "cmake",
                origin=v["origin"], upload=upload, user=self.creds[0], password=self.creds[1],
                cacert=s["cacert_path"], center_remote=s["center_remote"],
                run_conan=lambda a: self.conan.run(a, quiet=True), log=self.log)
            ir.ensure_remote(lambda a: self.conan.run(a, quiet=True), s["internal_remote"], log=self.log)
            self.log(f"Now edit {d / 'conanfile.py'} and press 'Test build'.", "ok")
            self.ui(self._fill_internal)

        self.run_task(f"Creating draft {ref[0]}/{ref[1]}", self._internal_task, task)

    @staticmethod
    def _internal_task(task):
        try:
            task()
        except ir.RecipeError as e:
            raise ConanError(str(e))

    def _internal_dir(self) -> Path | None:
        ref = self._internal_ref()
        if not ref:
            return None
        folder = ir.list_packages().get(ref[0], {}).get(ref[1])
        if not folder:
            messagebox.showwarning(APP_NAME, f"{ref[0]}/{ref[1]} has no recipe yet - create the draft first.")
            return None
        return ir.INDEX_ROOT / "recipes" / ref[0] / folder

    def on_internal_open(self):
        d = self._internal_dir()
        if d:
            open_folder(d)

    def on_internal_test(self):
        if not self._internal_dir():
            return
        name, version = self._internal_ref()  # type: ignore[misc]
        profs = self.selected_profiles()
        if not profs:
            messagebox.showwarning(APP_NAME, "Select at least one profile on the Search & Mirror tab.")
            return
        if self.settings["raw_url"] and not self.creds[0] and not self.ask_creds():
            return
        s = self.settings

        def task():
            ir.ensure_remote(lambda a: self.conan.run(a, quiet=True), s["internal_remote"], log=self.log)
            for p in profs:
                self.log(f"== Test build {name}/{version} [{p}]", "cmd")
                self.conan.run(ir.test_build_args(name, version, p, s["internal_remote"],
                                                  s["center_remote"], s["build_profile"]))
                self.log(f"== {name}/{version} [{p}]: built", "ok")

        self.run_task(f"Test build {name}/{version}", self._internal_task, task)

    def on_internal_add_ref(self):
        if not self._internal_dir():
            return
        ref = "/".join(self._internal_ref())  # type: ignore[arg-type]
        current = self.selected_refs()
        if ref not in current:
            self.refs_text.delete("1.0", "end")
            self.refs_text.insert("1.0", "\n".join(current + [ref]))
        self.nb.select(0)

    # ---- Profiles tab -----------------------------------------------------
    def _build_profiles_tab(self):
        tab = ttk.Frame(self.nb, padding=6)
        self.nb.add(tab, text="Profiles")
        left = ttk.Frame(tab)
        left.pack(side="left", fill="y")
        self.prof_box = tk.Listbox(left, width=34, exportselection=False)
        self.prof_box.pack(fill="y", expand=True)
        self.prof_box.bind("<<ListboxSelect>>", self.on_profile_select)
        for txt, cmd in (("Refresh", lambda: self.run_task("Loading profiles", self._task_load_profiles)),
                         ("New from template...", self.on_profile_wizard),
                         ("Detect from this machine...", self.on_profile_detect),
                         ("Import from my normal Conan home", self.on_profile_import),
                         ("Duplicate...", self.on_profile_duplicate),
                         ("Delete", self.on_profile_delete),
                         ("Open profiles folder", self.on_profile_folder)):
            ttk.Button(left, text=txt, command=cmd).pack(fill="x", pady=1)

        right = ttk.Frame(tab)
        right.pack(side="left", fill="both", expand=True, padx=(8, 0))
        self.prof_title = tk.StringVar(value="(no profile selected)")
        ttk.Label(right, textvariable=self.prof_title).pack(anchor="w")
        mono = ("Consolas", 10) if sys.platform == "win32" else ("Menlo", 12)
        self.prof_edit = tk.Text(right, font=mono, undo=True)
        self.prof_edit.pack(fill="both", expand=True)
        bb = ttk.Frame(right)
        bb.pack(fill="x", pady=4)
        ttk.Button(bb, text="Save", command=self.on_profile_save).pack(side="right")
        ttk.Button(bb, text="Show effective profile", command=self.on_profile_show).pack(side="right", padx=4)

    def profiles_dir(self) -> Path:
        return self.conan.home() / "profiles"

    def _task_load_profiles(self):
        self.conan.home()
        profs = self.conan.json(["profile", "list"], quiet=True)
        self.log(f"Conan home: {self.conan.home()}  ({len(profs)} profiles)")
        s = self.settings
        if s["nexus_url"] and s["nexus_remote"] not in {r["name"] for r in self.conan.json(["remote", "list"], quiet=True)}:
            self.log(f"This Conan home has no '{s['nexus_remote']}' remote yet - press 'Apply to Conan' "
                     "on the Settings tab.", "err")
        if not profs:
            self.log("No profiles here yet. On the Profiles tab: 'New from template', or 'Import from "
                     "my normal Conan home' to copy the ones you already have.")
        self.ui(self._fill_profiles, sorted(profs, key=natural_key))

    def _fill_profiles(self, profs: list[str]):
        prev = set(self.selected_profiles())
        self.profiles = profs
        for box in (self.prof_box, self.prof_list):
            box.delete(0, "end")
            for p in profs:
                box.insert("end", p)
        for i, p in enumerate(profs):
            if p in prev:
                self.prof_list.selection_set(i)

    def on_profile_select(self, _e=None):
        sel = self.prof_box.curselection()
        if not sel:
            return
        name = self.prof_box.get(sel[0])
        path = self.profiles_dir() / name
        self.current_profile = name
        self.prof_title.set(f"{name}   ({path})")
        self.prof_edit.delete("1.0", "end")
        try:
            self.prof_edit.insert("1.0", path.read_text(encoding="utf-8"))
        except OSError as e:
            self.prof_edit.insert("1.0", f"# could not read: {e}")
        self.prof_edit.edit_reset()

    def on_profile_save(self):
        if not self.current_profile:
            return
        path = self.profiles_dir() / self.current_profile
        path.write_text(self.prof_edit.get("1.0", "end-1c").rstrip() + "\n", encoding="utf-8")
        self.log(f"Saved profile {path}", "ok")

    def on_profile_show(self):
        if self.current_profile:
            self.run_task("Show profile", lambda: self.conan.run(
                ["profile", "show", "-pr:h", self.current_profile, "-pr:b", self.current_profile]))

    def _write_profiles(self, profiles: dict[str, str]):
        d = self.profiles_dir()
        existing = [n for n in profiles if (d / n).exists()]
        if existing and not messagebox.askyesno(
                APP_NAME, "Overwrite existing profiles?\n\n" + "\n".join(existing)):
            profiles = {k: v for k, v in profiles.items() if k not in existing}
        for name, text in profiles.items():
            (d / name).write_text(text, encoding="utf-8")
            self.log(f"Created profile {name}", "ok")
        self.run_task("Loading profiles", self._task_load_profiles)

    def on_profile_import(self):
        """Copy profiles from the Conan home you use outside this tool."""
        normal = Path(os.environ.get("CONAN_HOME") or Path.home() / ".conan2")
        src = normal / "profiles"
        if normal.resolve() == self.conan.home().resolve():
            messagebox.showinfo(APP_NAME, "The mirror is using your normal Conan home - nothing to import.")
            return
        found = {p.relative_to(src).as_posix(): p for p in sorted(src.rglob("*")) if p.is_file()} \
            if src.is_dir() else {}
        found.pop("default", None)      # machine-detected; make one here with 'Detect' if needed
        if not found:
            messagebox.showinfo(APP_NAME, f"No profiles found in {src}.")
            return
        if messagebox.askyesno(APP_NAME, f"Copy {len(found)} profile(s) from {src}?\n\n" + "\n".join(found)):
            self._write_profiles({n: p.read_text(encoding="utf-8") for n, p in found.items()})

    def on_profile_wizard(self):
        ProfileWizard(self, self._write_profiles)

    def on_profile_detect(self):
        name = simpledialog.askstring(APP_NAME, "Name for the detected profile:", parent=self)
        if name:
            self.run_task("Detect profile", lambda: (
                self.conan.run(["profile", "detect", "--name", name, "--force"]),
                self._task_load_profiles()))

    def on_profile_duplicate(self):
        if not self.current_profile:
            return
        name = simpledialog.askstring(APP_NAME, "New profile name:", parent=self,
                                      initialvalue=self.current_profile + "-copy")
        if name:
            self._write_profiles({name: self.prof_edit.get("1.0", "end-1c").rstrip() + "\n"})

    def on_profile_delete(self):
        if not self.current_profile:
            return
        if messagebox.askyesno(APP_NAME, f"Delete profile '{self.current_profile}'?"):
            (self.profiles_dir() / self.current_profile).unlink(missing_ok=True)
            self.log(f"Deleted profile {self.current_profile}")
            self.current_profile = None
            self.prof_edit.delete("1.0", "end")
            self.run_task("Loading profiles", self._task_load_profiles)

    def on_profile_folder(self):
        open_folder(self.profiles_dir())

    # ---- Settings tab -----------------------------------------------------
    def _build_settings_tab(self):
        tab = ttk.Frame(self.nb, padding=10)
        self.nb.add(tab, text="Settings")
        self.svars: dict[str, tk.StringVar] = {}
        fields = [
            ("conan_exe", "Conan executable", "conan, or full path to conan.exe"),
            ("center_remote", "ConanCenter remote name", ""),
            ("center_url", "ConanCenter URL", ""),
            ("proxy", "Proxy (for ConanCenter)", "e.g. 10.1.2.3:8080 - Nexus bypasses it"),
            ("nexus_remote", "Nexus remote name", ""),
            ("nexus_url", "Nexus Conan repo URL", "e.g. https://nexus.corp/repository/conan-hosted/"),
            ("raw_url", "Nexus raw (sources) URL", "e.g. https://nexus.corp/repository/conan-sources/"),
            ("cacert_path", "CA bundle (optional)", "only if the proxy intercepts TLS"),
            ("nexus_user", "Nexus username", "password is asked per session, never saved"),
            ("internal_remote", "Internal recipes remote name", "points at ./recipes next to this script"),
            ("conan_home", "Conan home for mirroring", "kept apart from your everyday Conan; "
                                                       "empty = use your normal one"),
            ("build_profile", "Build profile (for tools)", "a Release profile, e.g. win-msvc194-cpp20-release; "
                                                           "empty = same as each host profile"),
        ]
        for i, (k, label, hint) in enumerate(fields):
            ttk.Label(tab, text=label).grid(row=i, column=0, sticky="w", pady=3)
            v = tk.StringVar(value=self.settings.get(k, ""))
            self.svars[k] = v
            ttk.Entry(tab, textvariable=v, width=60).grid(row=i, column=1, sticky="ew", pady=3, padx=6)
            ttk.Label(tab, text=hint, foreground="gray").grid(row=i, column=2, sticky="w")
        tab.columnconfigure(1, weight=1)

        r = len(fields)
        self.dark_var = tk.BooleanVar(value=bool(self.settings.get("dark_mode")))
        ttk.Checkbutton(tab, text="Dark mode", variable=self.dark_var).grid(row=r, column=1, sticky="w",
                                                                           padx=6, pady=3)
        ttk.Label(tab, text="takes effect the next time the GUI starts", foreground="gray").grid(
            row=r, column=2, sticky="w")

        r += 1
        bf = ttk.Frame(tab)
        bf.grid(row=r, column=0, columnspan=3, sticky="w", pady=(12, 4))
        for txt, cmd in (("Save", self.on_settings_save),
                         ("Apply to Conan (remotes + global.conf)", self.on_apply_conan),
                         ("Log in to Nexus...", self.on_login),
                         ("Test network", self.on_test_network)):
            ttk.Button(bf, text=txt, command=cmd).pack(side="left", padx=(0, 6))
        bf2 = ttk.Frame(tab)
        bf2.grid(row=r + 1, column=0, columnspan=3, sticky="w", pady=4)
        for txt, cmd in (("Upload all cached source backups", self.on_backup_upload),
                         ("Export team config...", self.on_export_team),
                         ("Open Conan home", lambda: open_folder(self.conan.home()))):
            ttk.Button(bf2, text=txt, command=cmd).pack(side="left", padx=(0, 6))

        ttk.Label(tab, foreground="gray", justify="left", wraplength=900, text=(
            "'Apply to Conan' adds/updates both remotes, and writes a clearly marked block to Conan's "
            "global.conf (backup-sources upload/download URLs, optional CA bundle) plus a "
            "source_credentials.json that reads your session credentials from environment variables. "
            "A .bak copy of each file is kept the first time.\n\n"
            "'Export team config' creates a zip in Conan's own config format (Nexus remote, source "
            "location, MSVC fallbacks, your profiles) and can upload it to the Nexus raw repo. "
            "Developers install or update it with one command:  conan config install <zip or URL>")
        ).grid(row=r + 2, column=0, columnspan=3, sticky="w", pady=10)

    def on_settings_save(self) -> bool:
        new = {k: v.get().strip() for k, v in self.svars.items()}
        for k in ("nexus_url", "raw_url"):
            new[k] = slash(new[k])
            self.svars[k].set(new[k])
        if len({new["nexus_remote"], new["center_remote"], new["internal_remote"]}) < 3:
            messagebox.showerror(APP_NAME, "Nexus, ConanCenter and internal remote names must differ.")
            return False
        home_changed = new["conan_home"] != self.settings.get("conan_home")
        new["dark_mode"] = self.dark_var.get()
        theme_changed = new["dark_mode"] != bool(self.settings.get("dark_mode"))
        self.settings.update(new)
        save_settings(self.settings)
        self.log(f"Settings saved to {SETTINGS_FILE}", "ok")
        if theme_changed:
            self.log(f"{'Dark' if new['dark_mode'] else 'Light'} mode will be used the next time you "
                     "start the GUI.")
        if home_changed:
            self.conan._home = None
            self.log("Conan home changed - press 'Apply to Conan', then check the Profiles tab.", "err")
            self.after(200, lambda: self.run_task("Loading profiles", self._task_load_profiles))
        return True

    def on_apply_conan(self):
        if self.on_settings_save():
            self.run_task("Applying Conan config", self._task_apply)

    def _task_apply(self):
        apply_config(self.conan, self.settings, self.log)

    def ask_creds(self) -> bool:
        user = simpledialog.askstring(APP_NAME, "Nexus username:", parent=self,
                                      initialvalue=self.settings.get("nexus_user", ""))
        if not user:
            return False
        pw = simpledialog.askstring(APP_NAME, f"Nexus password / token for {user}:", parent=self, show="*")
        if pw is None:
            return False
        self.creds = (user, pw)
        if user != self.settings.get("nexus_user"):
            self.settings["nexus_user"] = user
            self.svars["nexus_user"].set(user)
            save_settings(self.settings)
        return True

    def on_login(self):
        if self.ask_creds():
            remote = self.settings["nexus_remote"]
            # password goes via CONAN_PASSWORD_<REMOTE> env var, never on the command line
            self.run_task("Logging in", lambda: self.conan.run(["remote", "login", remote, self.creds[0]]))

    def on_test_network(self):
        if not self.on_settings_save():
            return
        script = HERE / "check_network.py"
        if not script.exists():
            messagebox.showerror(APP_NAME, f"{script} not found (keep it next to this script).")
            return

        def task():
            p = subprocess.run([sys.executable, str(script)], capture_output=True, text=True,
                               **({"creationflags": 0x08000000} if os.name == "nt" else {}))
            for line in (p.stdout + p.stderr).splitlines():
                tag = "ok" if "[ OK ]" in line else "err" if "[FAIL]" in line else ""
                self.log(line, tag)

        self.run_task("Network test", task)

    def on_backup_upload(self):
        if not self.settings["raw_url"]:
            messagebox.showwarning(APP_NAME, "Set the raw (sources) URL first and Apply to Conan.")
            return
        if not self.creds[0] and not self.ask_creds():
            return
        self.run_task("Uploading source backups", lambda: self.conan.run(["cache", "backup-upload"]))

    def on_export_team(self):
        s = self.settings
        if not s["nexus_url"]:
            messagebox.showwarning(APP_NAME, "Set the Nexus URL first.")
            return
        dest = filedialog.asksaveasfilename(title="Save team config", defaultextension=".zip",
                                            initialfile="team-config.zip",
                                            filetypes=[("Zip", "*.zip")])
        if not dest:
            return
        # never ship 'default': it is machine-detected and would clobber each developer's own
        profs = [p for p in (self.selected_profiles() or self.profiles) if p != "default"]
        bp = s["build_profile"]
        if bp == "default":
            messagebox.showwarning(APP_NAME, "The build profile is 'default', which is never exported "
                                             "(it is different on every machine). Choose a named "
                                             "Release profile under Settings first.")
            return
        if bp and bp not in profs:
            if bp not in self.profiles:
                messagebox.showwarning(APP_NAME, f"Build profile '{bp}' does not exist - fix it under Settings.")
                return
            profs.append(bp)
        if not messagebox.askyesno(APP_NAME, f"Include {len(profs)} profile(s)?\n\n" + "\n".join(profs)
                                   + "\n\n(Select profiles on the Search tab to limit this.)"):
            return
        url = ""
        if s["raw_url"] and messagebox.askyesno(
                APP_NAME, "Also upload it to the Nexus raw repo, so developers can install it by URL?\n\n"
                          f"{s['raw_url']}{tc.CONFIG_PATH_IN_RAW}\n\n"
                          "(An existing team config there is replaced. Developers can only fetch it "
                          "if the raw repo allows anonymous read.)"):
            if not self.creds[0] and not self.ask_creds():
                return
            url = s["raw_url"] + tc.CONFIG_PATH_IN_RAW

        def task():
            try:
                version = self.conan.run(["--version"], quiet=True).strip().split()[-1]
                folder = tc.build(WORK_DIR / "team_config", s, profs, self.conan.home(), version, url, self.log)
                tc.make_zip(folder, Path(dest))
                self.log(f"Team config written to {dest}", "ok")
                if url:
                    ir.NexusRaw(self.creds[0], self.creds[1], s["cacert_path"]).put(url, Path(dest))
                    self.log(f"Uploaded to {url}", "ok")
            except (tc.TeamConfigError, ir.RecipeError, OSError) as e:
                raise ConanError(str(e))
            self.log(f"Developers run:  conan config install {url or Path(dest).name}", "ok")
            self.log(f"          then:  conan remote login {s['nexus_remote']} <username>")

        self.run_task("Exporting team config", task)


# --------------------------------------------------------------------------- headless
class Headless:
    """Stands in for the GUI when running from a manifest on the command line."""

    def __init__(self, user: str, password: str):
        self.settings = load_settings()
        self.creds = (user, password)

    @staticmethod
    def log(text: str, tag: str = "") -> None:
        print(text, flush=True)


def run_headless(a) -> int:
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
    conan = Conan(app)  # type: ignore[arg-type]
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
    App().mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
