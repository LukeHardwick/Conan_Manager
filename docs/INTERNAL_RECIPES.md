# Packages that aren't on ConanCenter

Some libraries we need aren't on ConanCenter, or not in the version we need. The first one is
the **CIGI Class Library (CCL) for CIGI 3.3**: ConanCenter only has `cigi-ccl/4.0.6a`.

We keep our own recipes in a git repository and register it with Conan as a
**local-recipes-index** remote called `internal`. Packages are then built from it and uploaded
to Nexus the same way ConanCenter packages are. The team never sees the `internal` remote; they
only use Nexus.

## 1. Recipe repository layout

A local-recipes-index uses the same layout as
[conan-center-index](https://github.com/conan-io/conan-center-index):

```
conan-recipes/                 (git repository)
└── recipes/
    └── cigi-ccl/
        ├── config.yml         which folder builds which version
        └── 3.x/
            ├── conanfile.py
            └── conandata.yml  source URL + sha256 per version
```

`recipes/cigi-ccl/config.yml`:

```yaml
versions:
  "3.3.3a":
    folder: "3.x"
```

`3.3.3a` is a placeholder used throughout this guide: check the exact release name on
SourceForge and use that as the version.

Use a separate `3.x` folder rather than adding 3.3 to a copy of the 4.x recipe: the 3.3 source
tree may build differently, and keeping them apart means a fix for one can't break the other.

## 2. Writing the CIGI 3.3 recipe

Start from ConanCenter's `cigi-ccl` recipe. Either copy it from conan-center-index
(`recipes/cigi-ccl/all/`) or fetch it with Conan:

```bash
conan download cigi-ccl/4.0.6a -r conancenter --only-recipe
```

```bash
conan cache path cigi-ccl/4.0.6a
```

That recipe is short: it downloads a zip from SourceForge, patches the old `CMakeLists.txt`,
builds the static or shared target and packages `ccl_lib` / `ccl_dll` (with a `D` suffix for
Windows Debug). Then:

1. **Get the 3.3 source.** Download the CCL 3.3 release from the
   [CIGI SourceForge project](https://sourceforge.net/projects/cigi/files/) (folder
   "CIGI Class Library (CCL)").
2. **Fill in `conandata.yml`** with the download URL and its sha256:

   ```yaml
   sources:
     "3.3.3a":
       url: "https://.../<ccl 3.3 archive>.zip"
       sha256: "<sha256 of that file>"
   ```

   Compute the hash with `shasum -a 256 <file>` (macOS/Linux) or
   `certutil -hashfile <file> SHA256` (Windows). The sha256 matters beyond integrity: Conan's
   backup-sources feature stores the archive in our Nexus raw repo under that hash, which is
   what lets the team build it without internet access.
3. **Check the build files in the 3.3 archive.** If it has a `CMakeLists.txt`, the 4.x
   `_patch_sources()` fixes are a starting point; adjust them to what is actually in the file.
   If it only ships Visual Studio projects or autotools, the `build()` step needs rewriting
   (for example with `MSBuild` from `conan.tools.microsoft`).
4. **Check `package_info()`.** Confirm the library names (`ccl_lib`, `ccl_dll`, Debug suffix)
   and the `CCL_DLL` define match what the 3.3 build produces.
5. **Tidy the metadata.** Change `url` from conan-center-index to our recipes repository.
   Keep `license = "LGPL-2.1-only"` and keep copying the licence files into `licenses/`.

## 3. Register the remote (admin machine only)

```bash
git clone <recipes repo url> C:\conan\conan-recipes
```

```bash
conan remote add internal C:\conan\conan-recipes --type=local-recipes-index
```

Conan reads recipes straight from that folder, so after a `git pull` new packages and
versions are available without re-adding the remote. A changed recipe for a version that is
already in the local cache is **not** picked up unless you pass `--update`; without it Conan
keeps using the cached copy. The commands below include `--update` for that reason.

Don't put this remote in the team config: `team_setup.py` only configures Nexus, and that's
where the built packages end up.

## 4. Test a build

Build it for one profile first, using ConanCenter for anything else it needs:

```bash
conan install --requires cigi-ccl/3.3.3a -r internal -r conancenter -pr:h win-msvc194-cpp20-release -pr:b win-msvc194-cpp20-release --build=missing --update
```

Fix the recipe until this passes for every profile you care about (Release and Debug, static
and shared if needed).

## 5. Mirror it to Nexus

The Conan Mirror GUI currently resolves packages from **one** source remote (the
"ConanCenter remote name" setting, passed as `-r conancenter`), so it can't build from
`internal` and ConanCenter together yet. Until it can, run the same three steps the GUI runs,
by hand, for each profile:

```bash
conan install --requires cigi-ccl/3.3.3a -r internal -r conancenter -pr:h <profile> -pr:b <profile> --build=missing --update -c tools.graph:skip_binaries=False -c tools.build:download_source=True --format=json --out-file graph.json
```

```bash
conan list --graph=graph.json --graph-recipes=* --graph-binaries=download --graph-binaries=build --graph-binaries=cache --format=json --out-file pkglist.json
```

```bash
conan upload --list=pkglist.json -r nexus -c
```

The upload pushes the recipe, every binary in the graph, and (because `core.sources:upload_url`
is set by *Apply to Conan*) the source archive to the Nexus raw repo. Log in to Nexus first so
the raw-repo upload has credentials. Add `--dry-run` to the upload for a first test.

Use `--out-file` rather than `> file`: in Windows PowerShell the redirect writes UTF-16, which
the next command may fail to read.

To confirm the version and its binaries are in Nexus, ask Nexus directly:

```bash
conan list "cigi-ccl/3.3.3a:*" -r nexus
```

The GUI is not a reliable check here. Its Search tab lists what is on ConanCenter, so an
internal-only version never appears. The Plan tab does resolve the package, but only from the
local cache left by the build above, not from the `internal` remote.

### Planned GUI support (not implemented)

The change needed in `conan_mirror.py` is small: allow a list of source remotes instead of one,
and pass each as `-r` in `Mirror._common()` (`conan_mirror.py:279`). Search would also need to
query `internal` alongside ConanCenter.

## 6. How the team uses it

Nothing changes for them. Once the package is in Nexus they use it like any other:

```bash
conan install . -pr:h <profile> -pr:b <profile>
```

with `cigi-ccl/3.3.3a` in their `conanfile`. If they need a profile that hasn't been mirrored,
`--build=missing` builds it from the recipe in Nexus using the source archive from the raw repo.
That download only works if the raw repo allows anonymous read: `team_setup.py` does not give
developers credentials for it.

## Adding more internal packages

Same steps: a new `recipes/<name>/` folder with `config.yml`, a recipe and `conandata.yml`
with sha256 hashes, then test, mirror, and commit the recipe to the recipes repository.
