# Conan Mirror

A small tkinter GUI (standard library only, Python 3.11+) for copying packages from
ConanCenter into an internal Nexus. You use it as the admin who can reach ConanCenter;
your team only ever talks to Nexus.

| File | Purpose |
|---|---|
| `conan_mirror.py` | The GUI. Run `python conan_mirror.py`. With `--manifest` it runs without the GUI. |
| `mirror_core.py` | Settings, the Conan runner, plan / mirror / verify. No tkinter, shared by the GUI and `--manifest` |
| `gui.py`, `gui_theme.py` | The window, its tabs and the light / dark colours |
| `internal_recipes.py` | Draft and test recipes for packages that aren't on ConanCenter (also the GUI's *Internal packages* tab) |
| `team_config.py` | Builds the configuration the team installs with `conan config install` |
| `check_network.py` | Proxy / Nexus / TLS-interception check (also run from the GUI) |
| `proxytest.py` | Shows how the proxy answers different tunnel requests, for when Conan gets 407 but curl or a browser doesn't |
| `recipes/` | Our own recipes, created by `internal_recipes.py` |
| `mirror.json` | Optional manifest: the packages and profiles to mirror (see [Manifest](#manifest-and-running-without-the-gui)) |

Requires:

- **Conan 2 on PATH.** The portable (self-contained) build and a `pip install conan` both
  work; the tool only ever runs the `conan` command. If it isn't on PATH, set *Conan
  executable* under Settings to the full path of `conan.exe`.
- **Python 3.11+ with tkinter** to run these scripts. This is separate from Conan: the
  portable Conan build carries its own Python, which these scripts can't use. On Windows,
  tkinter ships with the python.org installer. On macOS with Homebrew Python:
  `brew install python-tk`.
- **CMake and a compiler** for anything that is built from source.

## How it works

```
ConanCenter ──(proxy)──► your machine ──(direct)──► Nexus conan2 repo  (recipes + binaries)
recipes/    ───────────►  conan install             Nexus raw repo     (source archives)
                          --build=missing
```

1. **Search & Mirror** tab: search ConanCenter and our own recipes. Results show which
   versions are already in Nexus. Clicking a version lists ConanCenter's prebuilt binaries
   (os, compiler, cppstd, ...) and which of them are already in Nexus.
2. **Plan**: for each package × profile, `conan graph info` resolves the *full* dependency
   graph (transitive and tool requirements included), and each node is checked against
   Nexus. Each node is shown as already in Nexus, download + upload, build from source, or missing.
3. **Mirror**: `conan install --build=missing` downloads binaries ConanCenter has and
   builds the ones it doesn't. Then `conan list --graph` → `conan upload --list` pushes
   every recipe and binary in the graph to Nexus. Jobs that are already complete in Nexus are
   skipped. The newest recipe revision is always used (`--update`).
4. **Sources**: Conan's *backup sources* feature uploads every upstream source tarball
   (from GitHub etc.) to the raw repo during `conan upload`. Files already in the raw repo
   are skipped. The team's config points `core.sources:download_urls` at the raw repo, so
   `--build=missing` works for them without internet access.
5. **Verify**: after an upload, the same package is installed again in a throwaway Conan home
   that has only the team configuration: Nexus as the only remote, no proxy, an empty cache
   and no raw-repo login. If that works, it works for a developer. *Verify only* runs just
   this check.
6. **Internal packages** tab: for libraries that aren't on ConanCenter. You supply the source
   archive, the tool stores it in the raw repo and drafts a recipe in `recipes/`.

### What to mirror: references or a project's conanfile

The box on the Search & Mirror tab takes one entry per line. An entry is either

- a **reference** such as `fmt/11.0.2`: mirrored with its default options, or
- the **path to a project's `conanfile.py` / `conanfile.txt`** (*Add conanfile...*): mirrored
  exactly as that project resolves it, including its options (`shared=True`, ...), version
  overrides, and its `conan.lock` if one sits next to the conanfile.

Prefer the conanfile when a project uses non-default options. Different options mean different
binaries, and those would otherwise be missing from Nexus.

### A separate Conan home

The mirror does not use your everyday Conan home. It works in `~/.conan_mirror/conan_home`
(*Conan home for mirroring* under Settings), with its own cache, remotes, profiles and
`global.conf`. So nothing you build or export for other work can end up in Nexus by accident,
and the tool never edits your own Conan configuration.

The first time, that home is empty: press *Apply to Conan*, then create profiles on the
Profiles tab or copy your existing ones with *Import from my normal Conan home*.

## First-time setup (admin, on the Windows machine)

1. **Settings** tab: fill in the proxy (`10.x.x.x:port`), the Nexus conan repo URL and the raw repo URL.
   The proxy is only used for ConanCenter. Nexus hosts go into `NO_PROXY`.
2. **Test network**: checks that Nexus is reachable directly and ConanCenter through the proxy,
   and whether the proxy intercepts TLS. If it does, run
   `python check_network.py --export-bundle C:\conan\ca-bundle.pem`, then put that
   path in *CA bundle* (Conan uses its own CA list, not the Windows store).
   If it reports **407 Proxy Authentication Required**, the proxy wants a login that browsers
   send silently but Conan cannot. When it asks for Windows sign-in (NTLM / Negotiate), ask
   IT to let this machine reach ConanCenter and GitHub without proxy authentication, or run a
   local helper proxy that signs in for you (e.g. Px or CNTLM) and enter its address as the proxy.
3. **Apply to Conan**: sets up the mirror's Conan home. It adds the remotes, writes a marked
   block to `global.conf`, writes a `source_credentials.json` that reads credentials from
   environment variables, and adds the MSVC fallbacks (195→194, 194→193) to the compatibility
   plugin, the same ones the team gets.
4. **Log in to Nexus**: runs `conan remote login`. The password stays in memory for the
   session and is passed to Conan by environment variable, never on the command line or to disk.
5. **Profiles** tab → *New from template*: generates e.g. `win-msvc194-cpp20-release`
   / `-debug` for each C++ standard you tick. Or *Import from my normal Conan home*.
6. **Settings** → *Build profile (for tools)*: enter one of the Release profiles and *Save*.
   Build tools (cmake, ninja, ...) are then resolved with that profile for every mirror job,
   so a Debug profile doesn't rebuild them from source. The team config ships the same
   profile, and developers pass it as `-pr:b`.

## Team setup

**Settings → Export team config** writes `team-config.zip` in Conan's own configuration
format and offers to upload it to the raw repo. Developers install it, and later update it,
with one command:

```bash
conan config install https://<nexus>/repository/conan-sources/config/team-config.zip
```

```bash
conan remote login nexus <username>
```

(or `conan config install team-config.zip` with the file). It contains:

| File | Effect on the developer's Conan home |
|---|---|
| `remotes.json` | **Replaced.** Nexus is the only remote; ConanCenter is gone. |
| `global.conf` | **Replaced.** Sources for `--build=missing` come from the raw repo. Requires at least the Conan version the admin used. |
| `extensions/plugins/compatibility/compatibility.py` | **Replaced.** MSVC 195 may use 194 binaries, and 194 may use 193. |
| `profiles/*` | Added or overwritten (never `default`). |

There is no undo command. A developer who has their own `global.conf` or remotes should back
them up first; the `README.txt` in the zip says so. Re-export after changing settings or
profiles, and after upgrading Conan (the compatibility plugin is taken from your Conan version).

Installing by URL needs the raw repo to allow anonymous read, as do developer source builds.

*Replaces the old `team_setup.py`.* A developer who used it should run
`python team_setup.py revert` with their old copy before installing the new config.

## Manifest and running without the GUI

`mirror.json` records what should be in Nexus, so a mirror can be repeated and reviewed:

```json
{
  "profiles": ["win-msvc194-cpp20-release", "win-msvc194-cpp20-debug"],
  "packages": [
    "fmt/11.0.2",
    "cigi-ccl/3.3.3a",
    "../my-app/conanfile.py",
    {"ref": "zlib/1.3.1", "profiles": ["win-msvc194-cpp20-release"]}
  ]
}
```

A package is a reference or a conanfile path (relative to the manifest). An entry can carry
its own profile list. *Save manifest* writes the current boxes to this file and *Load
manifest* fills them from it. Commit the file.

```bash
python conan_mirror.py --manifest mirror.json --plan
```

```bash
python conan_mirror.py --manifest mirror.json
```

`--plan` only reports what is missing from Nexus. Without it, everything listed is mirrored
and verified; jobs already complete in Nexus are skipped, so it is safe to run repeatedly
(for example from a scheduled task). Other flags: `--dry-run`, `--no-verify`, `--verify-only`,
`--user`. The password comes from the `CONAN_MIRROR_RAW_PASSWORD` environment variable, or a
prompt. The exit code is 0 when every job succeeded, 1 when some failed.

It uses the same settings and Conan home as the GUI, so run the GUI once on that machine
first. The profiles named in the manifest must exist in that Conan home.

## Further docs

- [docs/INTERNAL_RECIPES.md](docs/INTERNAL_RECIPES.md): step-by-step guide to adding a package
  that isn't on ConanCenter (e.g. the CIGI 3.3 class library) from the *Internal packages* tab
- [docs/KNOWN_ISSUES.md](docs/KNOWN_ISSUES.md): known bugs, what has been fixed, and what has
  not been tested

## Notes

- **Binaries depend on the platform.** Build Windows binaries on Windows. On the Mac you can
  still search and **Plan** with Windows profiles, which shows whether ConanCenter has
  prebuilt binaries.
- **C++ standards:** ConanCenter builds with each compiler's default standard (C++14 for MSVC).
  Conan's compatibility plugin reuses those binaries for C++17/20/23 profiles, so you
  usually don't need a separate binary per standard. MSVC has no C++11 mode.
- **Dry run** (checkbox) does everything except the actual upload. Use it for a first test.
- **Dark mode:** tick *Dark mode* on the Settings tab. It switches straight away and is
  remembered. On macOS it uses the native light / dark window appearance, whatever the system
  setting is.
- Settings, work files and the mirror's Conan home live in `~/.conan_mirror/`.
- **Type checking:** `python -m mypy` (strict, settings in `pyproject.toml`). Only needed when
  changing the scripts.
