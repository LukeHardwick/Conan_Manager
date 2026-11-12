#!/usr/bin/env python3
"""
internal_recipes.py - recipes for packages that are not on ConanCenter.

Standard library only (Python 3.11+). Used by the Conan Mirror GUI ("Internal packages"
tab) and usable on its own:

    python internal_recipes.py new --name cigi-ccl --version 3.3.3a \\
        --archive C:\\Downloads\\ccl_3_3_3a.tar.gz --base cigi-ccl/4.0.6a
    python internal_recipes.py list
    python internal_recipes.py test --name cigi-ccl --version 3.3.3a -pr win-msvc194-cpp20-release

`new` never downloads the source. You download the archive yourself and pass the file. It
  1. computes the file's sha256,
  2. uploads it to the Nexus raw repo at  upstream/<name>/<version>/<filename>,
  3. writes recipes/<name>/config.yml, <folder>/conandata.yml (pointing at that Nexus URL)
     and a draft <folder>/conanfile.py, copied from a ConanCenter recipe (--base) or a template.
Run it again with a new --version to add a version to an existing recipe.

The repository root (the folder holding recipes/) is registered with Conan as a
local-recipes-index remote, so `test` and the GUI's Mirror can build from it.

Settings (Nexus URLs, proxy, remote names) come from ~/.conan_mirror/settings.json,
which the GUI writes.
"""
from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlparse

HERE = Path(__file__).resolve().parent
INDEX_ROOT = HERE                       # the local-recipes-index remote points here
SETTINGS_FILE = Path.home() / ".conan_mirror" / "settings.json"
NAME_RE = re.compile(r"[a-z0-9_][a-z0-9_+.-]{1,100}")
VERSION_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_+.-]{0,50}")
TEMPLATES = ("cmake", "header-only")


class RecipeError(Exception):
    pass


# --------------------------------------------------------------------------- environment
def conan_env(settings: dict, user: str = "", password: str = "") -> dict:
    """Environment for running conan: proxy for ConanCenter only, Nexus credentials."""
    env = dict(os.environ)
    env["NO_COLOR"] = "1"
    proxy = (settings.get("proxy") or "").strip()
    if proxy:
        if "://" not in proxy:
            proxy = "http://" + proxy
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            env[k] = proxy
        # Nexus is reachable directly - keep it off the proxy.
        hosts = ["localhost", "127.0.0.1"]
        for u in (settings.get("nexus_url", ""), settings.get("raw_url", "")):
            h = urlparse(u).hostname
            if h and h not in hosts:
                hosts.append(h)
        existing = env.get("NO_PROXY") or env.get("no_proxy") or ""
        env["NO_PROXY"] = env["no_proxy"] = ",".join(filter(None, [existing, *hosts]))
    if user:
        sfx = (settings.get("nexus_remote") or "nexus").replace("-", "_").upper()
        env[f"CONAN_LOGIN_USERNAME_{sfx}"] = user
        env[f"CONAN_PASSWORD_{sfx}"] = password
        env["CONAN_MIRROR_RAW_USER"] = user        # read by source_credentials.json
        env["CONAN_MIRROR_RAW_PASSWORD"] = password
    return env


# --------------------------------------------------------------------------- Nexus raw repo
def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def upstream_url(raw_url: str, name: str, version: str, filename: str) -> str:
    """Where a hand-supplied source archive lives in the Nexus raw repo."""
    base = raw_url if raw_url.endswith("/") else raw_url + "/"
    return f"{base}upstream/{quote(name)}/{quote(version)}/{quote(filename)}"


class NexusRaw:
    """Minimal client for a Nexus raw repo. Talks to Nexus directly, never via the proxy."""

    def __init__(self, user: str = "", password: str = "", cacert: str = ""):
        ctx = ssl.create_default_context()
        if cacert:
            ctx.load_verify_locations(cacert)
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=ctx))
        self._auth = ""
        if user:
            self._auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()

    def _open(self, url: str, method: str, data=None, headers: dict | None = None):
        req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
        if self._auth:
            req.add_header("Authorization", self._auth)
        try:
            return self._opener.open(req, timeout=120)
        except urllib.error.HTTPError as e:
            if e.code == 404 and method in ("HEAD", "GET"):
                return None
            hint = " - log in to Nexus first" if e.code in (401, 403) else ""
            raise RecipeError(f"Nexus answered HTTP {e.code} for {method} {url}{hint}")
        except (urllib.error.URLError, OSError) as e:
            raise RecipeError(f"Could not reach Nexus ({url}): {getattr(e, 'reason', e)}")

    def remote_sha256(self, url: str) -> str | None:
        """sha256 of the file at url, or None if it is not there."""
        r = self._open(url, "GET")
        if r is None:
            return None
        h = hashlib.sha256()
        with r:
            for chunk in iter(lambda: r.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    def put(self, url: str, path: Path) -> None:
        with open(path, "rb") as f:
            r = self._open(url, "PUT", data=f, headers={
                "Content-Length": str(path.stat().st_size),
                "Content-Type": "application/octet-stream"})
            if r is not None:
                r.close()


def publish_archive(archive: Path, url: str, sha: str, nexus: NexusRaw, log=print) -> None:
    """Upload the archive unless the same file is already there. Never overwrites."""
    existing = nexus.remote_sha256(url)
    if existing == sha:
        log(f"Already in Nexus (same file): {url}")
    elif existing is not None:
        raise RecipeError(f"A DIFFERENT file already exists at {url} (sha256 {existing}). "
                          "Not overwriting it - check which archive is the right one.")
    else:
        nexus.put(url, archive)
        log(f"Uploaded {archive.name} to {url}")


# --------------------------------------------------------------------------- recipe files
def default_folder(version: str) -> str:
    """'3.3.3a' -> '3.x' so different major versions keep separate recipes."""
    m = re.match(r"(\d+)\.", version)
    return f"{m.group(1)}.x" if m else "all"


def list_packages(root: Path = INDEX_ROOT) -> dict[str, dict[str, str]]:
    """{name: {version: folder}} read from recipes/*/config.yml."""
    out: dict[str, dict[str, str]] = {}
    for cfg in sorted((root / "recipes").glob("*/config.yml")):
        versions: dict[str, str] = {}
        current = None
        for line in cfg.read_text(encoding="utf-8").splitlines():
            m = re.match(r"\s+[\"']?([^\"':\s]+)[\"']?:\s*$", line)
            if m and m.group(1) != "versions":
                current = m.group(1)
            m = re.match(r"\s+folder:\s*[\"']?([^\"'\s]+)", line)
            if m and current:
                versions[current] = m.group(1)
        out[cfg.parent.name] = versions
    return out


def _has_key(text: str, key: str) -> bool:
    return re.search(rf"^\s+[\"']?{re.escape(key)}[\"']?:\s*$", text, re.M) is not None


def _insert_under(path: Path, top_key: str, key: str, block: str) -> bool:
    """Add `block` under `top_key:` in a small YAML file. False if `key` is already there."""
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if _has_key(text, key):
        return False
    if not re.search(rf"^{top_key}:\s*$", text, re.M):
        text = (text.rstrip("\n") + "\n" if text.strip() else "") + f"{top_key}:\n"
    text = re.sub(rf"^({top_key}:\s*\n)", lambda m: m.group(1) + block, text, count=1, flags=re.M)
    path.write_text(text, encoding="utf-8")
    return True


def write_config(pkg_dir: Path, version: str, folder: str) -> None:
    _insert_under(pkg_dir / "config.yml", "versions", version,
                  f'  "{version}":\n    folder: "{folder}"\n')


def write_conandata(recipe_dir: Path, version: str, url: str, sha: str, origin: str,
                    log=print) -> None:
    block = f"  # {version} originally from: {origin}\n" if origin else ""
    block += f'  "{version}":\n    url: "{url}"\n    sha256: "{sha}"\n'
    path = recipe_dir / "conandata.yml"
    if _insert_under(path, "sources", version, block):
        return
    # The version is already listed: same file -> make sure the URL is the current Nexus one.
    text = path.read_text(encoding="utf-8")
    entry = re.search(rf'^(\s+["\']?{re.escape(version)}["\']?:\s*\n\s+url:\s*")([^"]*)("\s*\n\s+sha256:\s*")([0-9a-f]+)"',
                      text, re.M)
    if not entry or entry.group(4) != sha:
        raise RecipeError(f"{path} already has version {version} with a different sha256 "
                          "(or in a layout this tool did not write). Edit or remove that entry first.")
    if entry.group(2) != url:
        path.write_text(text[:entry.start(2)] + url + text[entry.end(2):], encoding="utf-8")
        log(f"Updated the URL of {version} in {path.name} to {url}")


DRAFT_HEADER = '''\
# DRAFT created by internal_recipes.py from {origin}.
# Delete this comment block once the recipe builds. Work through docs/INTERNAL_RECIPES.md:
#   1. source():       the archive unpacks as expected (strip_root, sub-folders).
#   2. build():        patches / CMake options / targets match THIS version's build files.
#   3. package():      headers, libraries and licence files end up in the package.
#   4. package_info(): library names (and Debug suffixes) match what was built.
#   5. url / homepage / license / description are right for us.
# Then: Test build -> fix -> repeat, and mirror it to Nexus when it passes.
'''

TEMPLATE_CMAKE = '''\
from conan import ConanFile
from conan.tools.cmake import CMake, CMakeToolchain, cmake_layout
from conan.tools.files import copy, get
import os

required_conan_version = ">=2.0"


class @CLASS@Conan(ConanFile):
    name = "@NAME@"
    description = "TODO: one-line description"
    license = "TODO: SPDX licence id, e.g. MIT"
    homepage = "TODO: upstream project page"
    package_type = "library"
    settings = "os", "arch", "compiler", "build_type"
    options = {"shared": [True, False], "fPIC": [True, False]}
    default_options = {"shared": False, "fPIC": True}

    def config_options(self):
        if self.settings.os == "Windows":
            del self.options.fPIC

    def configure(self):
        if self.options.shared:
            self.options.rm_safe("fPIC")

    def layout(self):
        cmake_layout(self, src_folder="src")

    def source(self):
        # URL and sha256 come from conandata.yml (the archive lives in our Nexus raw repo)
        get(self, **self.conan_data["sources"][self.version], strip_root=True)

    def generate(self):
        tc = CMakeToolchain(self)
        # TODO: tc.variables["SOME_OPTION"] = False   (tests, examples, ...)
        tc.generate()

    def build(self):
        cmake = CMake(self)
        cmake.configure()
        cmake.build()

    def package(self):
        copy(self, "LICENSE*", self.source_folder, os.path.join(self.package_folder, "licenses"))
        copy(self, "COPYING*", self.source_folder, os.path.join(self.package_folder, "licenses"))
        cmake = CMake(self)
        cmake.install()

    def package_info(self):
        self.cpp_info.libs = ["@NAME@"]  # TODO: the library file name(s) without lib/.lib/.a
'''

TEMPLATE_HEADER_ONLY = '''\
from conan import ConanFile
from conan.tools.files import copy, get
from conan.tools.layout import basic_layout
import os

required_conan_version = ">=2.0"


class @CLASS@Conan(ConanFile):
    name = "@NAME@"
    description = "TODO: one-line description"
    license = "TODO: SPDX licence id, e.g. MIT"
    homepage = "TODO: upstream project page"
    package_type = "header-library"
    settings = "os", "arch", "compiler", "build_type"
    no_copy_source = True

    def layout(self):
        basic_layout(self, src_folder="src")

    def package_id(self):
        self.info.clear()  # one package for every profile

    def source(self):
        # URL and sha256 come from conandata.yml (the archive lives in our Nexus raw repo)
        get(self, **self.conan_data["sources"][self.version], strip_root=True)

    def package(self):
        copy(self, "LICENSE*", self.source_folder, os.path.join(self.package_folder, "licenses"))
        # TODO: adjust the header folder and patterns
        copy(self, "*.h*", os.path.join(self.source_folder, "include"),
             os.path.join(self.package_folder, "include"))

    def package_info(self):
        self.cpp_info.bindirs = []
        self.cpp_info.libdirs = []
'''


def _template_text(template: str, name: str) -> str:
    text = TEMPLATE_HEADER_ONLY if template == "header-only" else TEMPLATE_CMAKE
    cls = "".join(p.capitalize() for p in re.split(r"[^A-Za-z0-9]+", name) if p) or "Pkg"
    return text.replace("@CLASS@", cls).replace("@NAME@", name)


def _fetch_base(run_conan, ref: str, center_remote: str, recipe_dir: Path, log) -> None:
    """Copy the recipe (not the sources) of an existing ConanCenter package as a start."""
    run_conan(["download", ref, "-r", center_remote, "--only-recipe"])
    export = Path(run_conan(["cache", "path", ref]).strip())
    if not (export / "conanfile.py").exists():
        raise RecipeError(f"Could not find the recipe of {ref} in the Conan cache ({export})")
    text = (export / "conanfile.py").read_text(encoding="utf-8")
    (recipe_dir / "conanfile.py").write_text(
        DRAFT_HEADER.format(origin=f"ConanCenter's {ref}") + text, encoding="utf-8")
    extras = Path(run_conan(["cache", "path", ref, "--folder=export_source"]).strip())
    if extras.is_dir() and any(extras.iterdir()):
        shutil.copytree(extras, recipe_dir, dirs_exist_ok=True)
        log(f"Copied extra recipe files from {ref} (e.g. patches/) - they were written for "
            f"that version, check whether they still apply")


def create_draft(name: str, version: str, archive: Path, raw_url: str, *,
                 base: str = "", template: str = "cmake", folder: str = "", origin: str = "",
                 upload: bool = True, user: str = "", password: str = "", cacert: str = "",
                 center_remote: str = "conancenter", run_conan=None, log=print,
                 root: Path = INDEX_ROOT) -> Path:
    """Create recipes/<name>/... for `version`, or add `version` to an existing recipe."""
    if not NAME_RE.fullmatch(name):
        raise RecipeError(f"'{name}' is not a valid package name (lower case letters, digits, - _ . +)")
    if not VERSION_RE.fullmatch(version):
        raise RecipeError(f"'{version}' is not a valid version")
    archive = Path(archive)
    if not archive.is_file():
        raise RecipeError(f"Source archive not found: {archive}")
    if not raw_url:
        raise RecipeError("The Nexus raw (sources) URL is not set - fill it in under Settings.")
    if template not in TEMPLATES:
        raise RecipeError(f"Unknown template '{template}' (choose from {', '.join(TEMPLATES)})")

    pkg_dir = root / "recipes" / name
    known = list_packages(root).get(name, {})
    if version in known:
        folder = known[version]
    folder = folder or default_folder(version)
    recipe_dir = pkg_dir / folder

    sha = sha256_file(archive)
    url = upstream_url(raw_url, name, version, archive.name)
    log(f"sha256 {sha}  {archive.name}")
    if upload:
        publish_archive(archive, url, sha, NexusRaw(user, password, cacert), log)
    else:
        log(f"NOT uploaded. Before building, put the archive at {url}")

    recipe_dir.mkdir(parents=True, exist_ok=True)
    write_config(pkg_dir, version, folder)
    write_conandata(recipe_dir, version, url, sha, origin, log)
    conanfile = recipe_dir / "conanfile.py"
    if conanfile.exists():
        log(f"Kept the existing {conanfile.relative_to(root)} (added version {version} to it)")
    elif base:
        if run_conan is None:
            raise RecipeError("A base recipe needs Conan - no runner given")
        _fetch_base(run_conan, base, center_remote, recipe_dir, log)
        log(f"Draft recipe copied from {base}: {conanfile}")
    else:
        conanfile.write_text(DRAFT_HEADER.format(origin=f"the '{template}' template")
                             + _template_text(template, name), encoding="utf-8")
        log(f"Draft recipe written from the '{template}' template: {conanfile}")
    return recipe_dir


# --------------------------------------------------------------------------- building
def ensure_remote(run_conan, remote: str, root: Path = INDEX_ROOT, log=print) -> None:
    """Register `root` as a local-recipes-index remote (first in the list)."""
    remotes = {r["name"]: r for r in json.loads(run_conan(["remote", "list", "--format=json"]) or "[]")}
    have = remotes.get(remote)
    if have is None:
        run_conan(["remote", "add", remote, str(root), "--type=local-recipes-index", "--index", "0"])
        log(f"Added remote '{remote}' -> {root}")
    elif Path(have["url"]).resolve() != root.resolve():
        run_conan(["remote", "update", remote, "--url", str(root)])
        log(f"Remote '{remote}' now points at {root}")
    if have is not None and not have.get("enabled", True):
        run_conan(["remote", "enable", remote])


def test_build_args(name: str, version: str, profile: str, internal_remote: str,
                    center_remote: str, build_profile: str = "") -> list[str]:
    # --update: without it Conan keeps using a cached copy of a recipe you have just edited
    return ["install", "--requires", f"{name}/{version}", "-r", internal_remote, "-r", center_remote,
            "-pr:h", profile, "-pr:b", build_profile or profile, "--build=missing", "--update"]


# --------------------------------------------------------------------------- command line
def _load_settings() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _cli_runner(settings: dict, creds: tuple[str, str]):
    def run(args: list[str]) -> str:
        exe = shutil.which(settings.get("conan_exe") or "conan")
        if not exe:
            raise RecipeError("'conan' not found on PATH. Install it with:  pip install conan")
        p = subprocess.run([exe, *args], text=True, stdout=subprocess.PIPE,
                           env=conan_env(settings, *creds))
        if p.returncode != 0:
            raise RecipeError(f"conan {' '.join(args[:2])} failed (exit {p.returncode}) - see above")
        return p.stdout
    return run


def _ask_creds(settings: dict, a) -> tuple[str, str]:
    user = a.user or os.environ.get("CONAN_MIRROR_RAW_USER") or settings.get("nexus_user") or ""
    if not user:
        user = input("Nexus username: ").strip()
    pw = os.environ.get("CONAN_MIRROR_RAW_PASSWORD")
    if pw is None:
        pw = getpass.getpass(f"Nexus password / token for {user}: ")
    return user, pw


def main() -> int:
    s = _load_settings()
    ap = argparse.ArgumentParser(description="Recipes for packages that are not on ConanCenter.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("new", help="create a draft recipe, or add a version to an existing one")
    p.add_argument("--name", required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--archive", required=True, type=Path,
                   help="source archive you downloaded yourself (zip / tar.gz)")
    p.add_argument("--base", default="", help="start from this ConanCenter recipe, e.g. cigi-ccl/4.0.6a")
    p.add_argument("--template", default="cmake", choices=TEMPLATES, help="used when --base is not given")
    p.add_argument("--folder", default="", help="recipe folder (default: '<major>.x')")
    p.add_argument("--origin", default="", help="where the archive came from (recorded as a comment)")
    p.add_argument("--raw-url", default=s.get("raw_url", ""), help="Nexus raw repo URL")
    p.add_argument("--no-upload", action="store_true", help="don't upload the archive to Nexus")
    p.add_argument("--user", default="", help="Nexus username")

    sub.add_parser("list", help="list internal packages and versions")

    p = sub.add_parser("test", help="build one version from the recipes in this folder")
    p.add_argument("--name", required=True)
    p.add_argument("--version", required=True)
    p.add_argument("-pr", "--profile", action="append", required=True, help="repeat for several")
    p.add_argument("--build-profile", default="", help="default: same as the host profile")
    p.add_argument("--user", default="", help="Nexus username")
    a = ap.parse_args()

    internal = s.get("internal_remote") or "internal"
    center = s.get("center_remote") or "conancenter"
    try:
        if a.cmd == "list":
            for name, versions in list_packages().items():
                for v, folder in versions.items():
                    print(f"{name}/{v}   (recipes/{name}/{folder})")
        elif a.cmd == "new":
            creds = ("", "") if a.no_upload else _ask_creds(s, a)
            d = create_draft(a.name, a.version, a.archive, a.raw_url, base=a.base,
                             template=a.template, folder=a.folder, origin=a.origin,
                             upload=not a.no_upload, user=creds[0], password=creds[1],
                             cacert=s.get("cacert_path", ""), center_remote=center,
                             run_conan=_cli_runner(s, creds))
            print(f"\nNext: edit {d / 'conanfile.py'}, then\n"
                  f"  python internal_recipes.py test --name {a.name} --version {a.version} -pr <profile>")
        elif a.cmd == "test":
            run = _cli_runner(s, _ask_creds(s, a))
            ensure_remote(run, internal)
            for prof in a.profile:
                print(f"\n== {a.name}/{a.version} [{prof}]", flush=True)
                run(test_build_args(a.name, a.version, prof, internal, center, a.build_profile))
            print("\nBuilt. Mirror it to Nexus from the GUI when you are happy with it.")
    except RecipeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
