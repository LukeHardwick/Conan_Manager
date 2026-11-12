# Conan Mirror

A small tkinter GUI (standard library only, Python 3.11+) for copying packages from
ConanCenter into an internal Nexus. You use it as the admin who can reach ConanCenter;
your team only ever talks to Nexus.

| File | Purpose |
|---|---|
| `conan_mirror.py` | The GUI. Run `python conan_mirror.py` |
| `check_network.py` | Proxy / Nexus / TLS-interception check (also run from the GUI) |
| `internal_recipes.py` | Draft and test recipes for packages that aren't on ConanCenter (also the GUI's *Internal packages* tab) |
| `recipes/` | Our own recipes, created by the above |

Requires Conan 2 on PATH (`pip install conan`). On Windows, tkinter ships with the
python.org installer. On macOS with Homebrew Python: `brew install python-tk`.

## How it works

```
ConanCenter ──(proxy)──► your machine ──(direct)──► Nexus conan2 repo  (recipes + binaries)
                          conan install              Nexus raw repo     (upstream source tarballs)
                          --build=missing
```

1. **Search & Mirror** tab: search ConanCenter. Results show which versions are already
   in Nexus. Clicking a version lists ConanCenter's prebuilt binaries (os, compiler,
   cppstd, ...) and which of them are already in Nexus.
2. **Plan**: for each reference × profile, `conan graph info` resolves the *full* dependency
   graph (transitive and tool requirements included), and each node is checked against
   Nexus. Each node is shown as already in Nexus, download + upload, build from source, or missing.
3. **Mirror**: `conan install --build=missing` downloads binaries ConanCenter has and
   builds the ones it doesn't. Then `conan list --graph` → `conan upload --list` pushes
   every recipe and binary in the graph to Nexus. Jobs that are already complete in Nexus are skipped.
4. **Sources**: Conan's *backup sources* feature uploads every upstream source tarball
   (from GitHub etc.) to the raw repo during `conan upload`. Files already in the raw repo
   are skipped. The team's config points `core.sources:download_urls` at the raw repo, so
   `--build=missing` works for them without internet access.
5. **Internal packages** tab: for libraries that aren't on ConanCenter. You supply the source
   archive, the tool stores it in the raw repo and drafts a recipe in `recipes/`. Search, Plan
   and Mirror then treat those recipes as a second source next to ConanCenter, and always use
   the newest recipe revision (`--update`).

## First-time setup (admin, on the Windows machine)

1. **Settings** tab: fill in the proxy (`10.x.x.x:port`), the Nexus conan repo URL and the raw repo URL.
   The proxy is only used for ConanCenter. Nexus hosts go into `NO_PROXY`.
2. **Test network**: checks that Nexus is reachable directly and ConanCenter through the proxy,
   and whether the proxy intercepts TLS. If it does, run
   `python check_network.py --export-bundle C:\conan\ca-bundle.pem`, then put that
   path in *CA bundle* (Conan uses its own CA list, not the Windows store).
3. **Apply to Conan**: adds both remotes and writes a marked block to Conan's `global.conf`
   (a `.bak` copy is kept). It also writes a `source_credentials.json` that reads credentials from
   environment variables.
4. **Log in to Nexus**: runs `conan remote login`. The password stays in memory for the
   session and is passed to Conan by environment variable, never on the command line or to disk.
5. **Profiles** tab → *New from template*: generates e.g. `win-msvc194-cpp20-release`
   / `-debug` for each C++ standard you tick.

## Team setup

**Settings → Export team config** writes `team-config.zip` containing
[`team_setup.py`](team_setup.py), `team_config.json` (your Nexus settings) and your profiles
(never `default`). Developers unzip it anywhere and run:

```
python team_setup.py apply     # configure + log in
python team_setup.py status    # what's applied, current remotes
python team_setup.py revert    # undo everything
```

`apply` adds the Nexus remote first and disables ConanCenter. It also points source downloads
for `--build=missing` at the raw repo, adds the MSVC fallbacks (195→194, 194→193) to Conan's
compatibility plugin, and copies in the team profiles. Before the first change, every file it
touches is backed up to `%APPDATA%\ConanTeamSetup\<id>\` (macOS:
`~/Library/Application Support/ConanTeamSetup`). Files that didn't exist before are recorded and
deleted on revert, so `revert` restores Conan's configuration exactly as it was. Running `apply`
again is safe because the original backup is kept.

## Further docs

- [docs/INTERNAL_RECIPES.md](docs/INTERNAL_RECIPES.md): step-by-step guide to adding a package
  that isn't on ConanCenter (e.g. the CIGI 3.3 class library) from the *Internal packages* tab
- [docs/KNOWN_ISSUES.md](docs/KNOWN_ISSUES.md): known bugs and their suggested fixes

## Notes

- **Binaries depend on the platform.** Build Windows binaries on Windows. On the Mac you can
  still search and **Plan** with Windows profiles, which shows whether ConanCenter has
  prebuilt binaries.
- **C++ standards:** ConanCenter builds with each compiler's default standard (C++14 for MSVC).
  Conan's compatibility plugin reuses those binaries for C++17/20/23 profiles, so you
  usually don't need a separate binary per standard. MSVC has no C++11 mode.
- **Dry run** (checkbox) does everything except the actual upload. Use it for a first test.
- Work files and settings live in `~/.conan_mirror/`.
