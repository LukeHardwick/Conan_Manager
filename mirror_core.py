"""
Conan Mirror core: everything that does not need a GUI.

Settings, the `conan` command runner, planning / mirroring / verifying jobs, the Conan home
setup ('Apply to Conan'), manifests and profile templates. Used by the GUI (gui.py) and by
the command line (conan_mirror.py --manifest). Must not import tkinter.
"""
from __future__ import annotations

import functools
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

import internal_recipes as ir
import team_config as tc
from internal_recipes import Log, Settings

APP_NAME = "Conan Mirror"
HERE = Path(__file__).resolve().parent
MANIFEST_FILE = HERE / "mirror.json"
CONFIG_DIR = Path.home() / ".conan_mirror"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
WORK_DIR = CONFIG_DIR / "work"
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
BLOCK_START = "# >>> conan-mirror (managed) >>>"
BLOCK_END = "# <<< conan-mirror (managed) <<<"

DEFAULTS: Settings = {
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



# --------------------------------------------------------------------------- helpers
class Cancelled(Exception):
    pass


class ConanError(Exception):
    pass


def load_settings() -> Settings:
    s = DEFAULTS.copy()
    s["msvc_fallbacks"] = dict(DEFAULTS["msvc_fallbacks"])   # not shared with DEFAULTS
    try:
        s.update(json.loads(SETTINGS_FILE.read_text()))
    except (OSError, ValueError):
        pass
    return s


def save_settings(s: Settings) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(s, indent=2))


def env_suffix(remote: str) -> str:
    """Conan's env-var naming: CONAN_PASSWORD_<REMOTE> with '-' -> '_'."""
    return remote.replace("-", "_").upper()


def slash(url: str) -> str:
    url = url.strip()
    return url if not url or url.endswith("/") else url + "/"


def natural_key(s: str) -> list[int | str]:
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def strip_ts(ref: str) -> str:
    """'zlib/1.3#abc%1700000000.0' -> 'zlib/1.3#abc'"""
    return ref.split("%", 1)[0]


def fmt_cmd(cmd: list[str]) -> str:
    return subprocess.list2cmdline(cmd) if os.name == "nt" else shlex.join(cmd)


def open_folder(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(path)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


# --------------------------------------------------------------------------- conan runner
class ConanHost(Protocol):
    """What the runner needs from whoever drives it: the GUI window, or the command line."""
    settings: Settings
    creds: tuple[str, str]      # Nexus user and password for this session (never saved)

    def log(self, text: str, tag: str = "") -> None: ...


class Conan:
    """Runs conan commands synchronously (call from a worker thread)."""

    def __init__(self, host: ConanHost):
        self.host = host
        self.proc: subprocess.Popen[str] | None = None
        self.cancelled = False
        self._home: Path | None = None

    def env(self) -> dict[str, str]:
        return ir.conan_env(self.host.settings, *self.host.creds)

    def run(self, args: list[str], check: bool = True, quiet: bool = False,
            env: dict[str, str] | None = None) -> str:
        if self.cancelled:
            raise Cancelled()
        exe = shutil.which(self.host.settings["conan_exe"] or "conan") or self.host.settings["conan_exe"]
        cmd = [exe, *args]
        if not quiet:
            self.host.log("$ " + fmt_cmd(["conan", *args]), "cmd")
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                                 errors="replace", env=env or self.env(), cwd=WORK_DIR,
                                 creationflags=flags)
        except OSError as e:
            raise ConanError(f"Could not run '{exe}': {e}. Set the Conan executable in Settings.")
        self.proc = p

        tail: list[str] = []

        def pump() -> None:
            assert p.stderr
            for line in p.stderr:
                line = ANSI.sub("", line.rstrip("\n"))
                tail.append(line)
                del tail[:-5]
                if not quiet:
                    self.host.log(line)

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

    def json(self, args: list[str], quiet: bool = False) -> Any:
        """Parsed `--format=json` output. Its shape depends on the command, so it is Any here
        and each caller annotates what it reads."""
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
    def remote_section(self, data: dict[str, Any], remote: str) -> dict[str, Any]:
        r = data.get(remote, {})
        if isinstance(r, dict) and "error" in r:
            msg = str(r["error"])
            if "404" in msg or "not found" in msg.lower():
                return {}
            raise ConanError(f"{remote}: {msg}")
        section: dict[str, Any] = r
        return section

    def list_remote(self, pattern: str, remote: str, quiet: bool = True) -> dict[str, Any]:
        return self.remote_section(self.json(["list", pattern, "-r", remote], quiet=quiet), remote)

    def search(self, pattern: str, remote: str) -> list[str]:
        return list(self.remote_section(self.json(["search", pattern, "-r", remote]), remote))


# --------------------------------------------------------------------------- mirror logic
def source_remotes(conan: Conan, s: Settings) -> list[str]:
    internal = s.get("internal_remote", "")
    remotes: list[dict[str, Any]] = conan.json(["remote", "list"], quiet=True)
    enabled = {str(r["name"]) for r in remotes if r.get("enabled", True)}
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
    def __init__(self, conan: Conan, settings: Settings):
        self.c = conan
        self.s = settings
        self._nexus_cache: dict[str, tuple[str, set[str]]] = {}
        self._sources: list[str] | None = None
        self._verify_env: dict[str, str] | None = None

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
        g: dict[str, Any] = self.c.json(["graph", "info", *self._common(ref, profile, opts)])
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

    def mirror(self, ref: str, profile: str, opts: Options, log: Log,
               on_plan: Callable[[list[PlanRow]], None]) -> str:
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
    def _prepare_verify(self, log: Log) -> dict[str, str]:
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
            env = ir.conan_env({**self.s, "proxy": "", "conan_home": str(home)}, *self.c.host.creds)
            for k in ("CONAN_MIRROR_RAW_USER", "CONAN_MIRROR_RAW_PASSWORD"):
                env.pop(k, None)    # developers have no raw-repo credentials
            self._verify_env = env
            log(f"Verify home: {home} (team config, Nexus only, empty cache)")
        return self._verify_env

    def verify(self, ref: str, profile: str, opts: Options, log: Log) -> None:
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


def run_jobs(m: Mirror, jobs: list[tuple[str, str]], opts: Options, log: Log,
             on_plan: Callable[[str, list[PlanRow]], None]) -> list[tuple[str, str, str]]:
    """Mirror (and verify) each (ref, profile). Returns [(ref, profile, result)]; never raises ConanError."""
    results = []
    for ref, p in jobs:
        log(f"== Mirror {ref} [{p}]", "cmd")
        try:
            res = m.mirror(ref, p, opts, log, functools.partial(on_plan, ref))
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


def run_verify(m: Mirror, jobs: list[tuple[str, str]], opts: Options, log: Log) -> list[tuple[str, str, str]]:
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
def apply_config(conan: Conan, s: Settings, log: Log) -> None:
    """Set up the mirror's Conan home: remotes, global.conf, source credentials, MSVC fallbacks."""
    listed: list[dict[str, Any]] = conan.json(["remote", "list"], quiet=True)
    remotes = {str(r["name"]): r for r in listed}
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


def write_managed_block(path: Path, lines: list[str], log: Log) -> None:
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
