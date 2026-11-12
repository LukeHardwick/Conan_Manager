# Packages that aren't on ConanCenter

Some libraries we need aren't on ConanCenter, or not in the version we need. The first one is
the **CIGI Class Library (CCL) 3.3**: ConanCenter only has `cigi-ccl/4.0.6a`.

For these we write our own recipe, kept in the `recipes/` folder of this repository. The
package is then built and mirrored to Nexus exactly like a ConanCenter package, and the team
uses it like any other. This guide is for the admin who adds one. You don't need to know how
the mirror tool works inside.

**One rule:** the tool never downloads source code from the internet. You download the
archive with a browser, the tool uploads it to our Nexus raw repo, and every build after that
takes it from Nexus.

## The short version

1. Download the source archive (zip or tar.gz) from the project's website.
2. In Conan Mirror, open the **Internal packages** tab, fill in the form and press
   **Create draft / add version**.
3. Edit the draft `conanfile.py` until **Test build** passes.
4. Press **Add to mirror list**, then **Mirror to Nexus** on the Search & Mirror tab.
5. Commit the new `recipes/<name>/` folder.

Steps 1, 2, 4 and 5 are mechanical. Step 3 is the only part that needs thought, and
[Editing the draft](#3-edit-the-draft-until-it-builds) walks through it.

## Before you start

- **Settings** are filled in and *Apply to Conan* has been pressed (see the README).
- You are **logged in to Nexus** with an account that can write to the raw repo.
- You have at least one **profile** (Profiles tab).

## 1. Download the source archive

Get the release archive from the upstream project and save it anywhere on your machine.
Don't unpack, rename or re-zip it: the file is stored as it is, so its hash can be checked
against the publisher's copy later.

*CIGI example:* on the [CIGI SourceForge project](https://sourceforge.net/projects/cigi/files/),
open "CIGI Class Library (CCL)" → "CCL Version 3.3.3" and download `ccl_3_3_3a.tar.gz`.

## 2. Create the draft

On the **Internal packages** tab:

| Field | What to enter | CIGI example |
|---|---|---|
| Package name | Lower case. Use ConanCenter's name if the library exists there in another version. | `cigi-ccl` |
| Version | The upstream version. | `3.3.3a` |
| Source archive | *Browse* to the file from step 1. | `ccl_3_3_3a.tar.gz` |
| Start the recipe from | *ConanCenter recipe* if another version is on ConanCenter, otherwise the template that matches how the library builds. | ConanCenter recipe |
| ConanCenter reference | Only for *ConanCenter recipe*: the version to copy the recipe from. | `cigi-ccl/4.0.6a` |
| Where the archive came from | Free text, recorded as a comment. | `CIGI SourceForge, CCL Version 3.3.3` |

Press **Create draft / add version**. The log shows what happened:

- The archive's sha256 is computed and the file is uploaded to the raw repo at
  `upstream/<name>/<version>/<filename>`. If the same file is already there it is left alone;
  if a *different* file is there the tool stops rather than overwrite it.
- `recipes/<name>/` is created with three files (see [What gets created](#what-gets-created)).
- This repository is registered with Conan as a remote called `internal`.

**Adding a version to a package that already has a recipe** is the same form: enter the
existing name and the new version. The recipe is kept, and only the new version is added.
If that version builds the same way, go straight to Test build.

## 3. Edit the draft until it builds

Press **Open recipe folder** and open `conanfile.py` in an editor. The comment block at the
top lists what to check. In order:

1. **`source()`** unpacks the archive. `strip_root=True` expects the archive to contain one
   top-level folder. If Test build fails with *"Can't untar ... files in the root"*, remove
   `strip_root=True`.
2. **`build()`** must match the build files in *this* version. A recipe copied from
   ConanCenter often patches `CMakeLists.txt` with `replace_in_file`; if the text it looks
   for isn't in this version, Test build fails with *"replace_in_file didn't find pattern"*.
   Open the archive, compare, and adjust or delete each replacement. If the archive has no
   `CMakeLists.txt` at all (only Visual Studio projects or a `configure` script), the build
   step has to be rewritten; ask for help with that one.
3. **`package()`** copies headers, libraries and the licence file into the package. If
   `cmake.install()` installs nothing, copy the files explicitly with `copy(...)`.
4. **`package_info()`** lists the library names a consumer links against. They must match the
   files that were actually built, including any Debug suffix. A wrong name here doesn't fail
   Test build; it fails later, when someone links against the package.
5. **Metadata.** Set `url` to this repository, and check `license`, `homepage` and
   `description`. A copied recipe may also contain a `validate()` that rejects some
   platforms; keep it unless you have checked that platform builds.

Then select one profile on the **Search & Mirror** tab and press **Test build**. Read the last
error in the log, fix the recipe, press Test build again. Edits are picked up automatically.
When one profile passes, select all the profiles the team uses (Release and Debug) and run
it once more.

*CIGI example:* `recipes/cigi-ccl/3.x/conanfile.py` is the finished result, and shows each of
the checks above in practice. Copied from 4.0.6a, the draft needed five changes:

| Check | 4.0.6a recipe | What CCL 3.3.3a needed |
|---|---|---|
| `build()` patches | Moves `CMAKE_MINIMUM_REQUIRED` and fixes `Header Files\` paths | Neither text exists in 3.3. It has no `CMAKE_MINIMUM_REQUIRED` at all, so one is added. |
| `generate()` | Sets `USE_MSVC_RUNTIME_LIBRARY_DLL` | 3.3 has no such option; removed. |
| `package_info()` names | `ccl_lib` / `ccl_dll`, `D` suffix for Debug | `cigicl_static` / `cigicl`, `_debug` suffix (Windows only) |
| `package_info()` headers | Default `include` | Headers install to `include/cigicl` |
| `validate()` | Refuses macOS | Removed: 3.3 builds on macOS |

It has been built on macOS (static, and shared Debug) and a small program linked against it.
It has **not** been built on Windows yet, which is where the library names and the `_debug`
suffix actually differ, so run Test build with a Release and a Debug MSVC profile and check
the names in the package's `lib` folder against `package_info()`.

The committed `conandata.yml` has a placeholder URL. Running **Create draft / add version**
once with `ccl_3_3_3a.tar.gz` uploads the archive and replaces it with the real Nexus URL.

## 4. Mirror it to Nexus

Press **Add to mirror list**. This puts `<name>/<version>` in the list on the Search & Mirror
tab. Select the profiles and press **Mirror to Nexus**, as for any ConanCenter package.

The mirror uploads the recipe, the binaries and a second copy of the source archive (in
Conan's own format, which is what the team's machines read).

To confirm it arrived, ask Nexus directly:

```bash
conan list "cigi-ccl/3.3.3a:*" -r nexus
```

## 5. Commit the recipe

Commit the `recipes/<name>/` folder. The recipe is the only record of how the package was
built, and the next version starts from it.

## How the team uses it

Nothing changes for them. Once the package is in Nexus they add it to their `conanfile` and
install as usual. They never see the `internal` remote or this repository.

If they need a profile that wasn't mirrored, `--build=missing` builds it from the recipe and
source in Nexus. That only works if the raw repo allows anonymous read, because
the team config does not give developers credentials for it. The *Verify* step after
mirroring checks exactly this.

---

## Reference

### What gets created

```
recipes/
└── cigi-ccl/
    ├── config.yml         which folder builds which version
    └── 3.x/
        ├── conanfile.py   the recipe (the file you edit)
        └── conandata.yml  Nexus URL + sha256 for each version
```

The folder is named after the major version (`3.x`), so a 4.x recipe can sit beside it
without one affecting the other. This is the same layout as
[conan-center-index](https://github.com/conan-io/conan-center-index).

`conandata.yml` always points at Nexus, never at the upstream site:

```yaml
sources:
  # 3.3.3a originally from: CIGI SourceForge, CCL Version 3.3.3
  "3.3.3a":
    url: "https://<nexus>/repository/conan-sources/upstream/cigi-ccl/3.3.3a/ccl_3_3_3a.tar.gz"
    sha256: "<sha256 of the archive>"
```

### The Nexus raw repo

```
conan-sources/                      the raw repo from Settings
├── <sha256>                        Conan's own store, written during Mirror
├── <sha256>.json                   (never edit these by hand)
└── upstream/
    └── cigi-ccl/
        └── 3.3.3a/
            └── ccl_3_3_3a.tar.gz   the archive exactly as you downloaded it
```

The admin machine builds from `upstream/`. Developers' machines read the files at the top
level by hash. The repo must allow redeploy, because Conan rewrites the `.json` files.

### The same steps from the command line

```bash
python internal_recipes.py new --name cigi-ccl --version 3.3.3a --archive C:\Downloads\ccl_3_3_3a.tar.gz --base cigi-ccl/4.0.6a
```

```bash
python internal_recipes.py test --name cigi-ccl --version 3.3.3a -pr win-msvc194-cpp20-release
```

```bash
python internal_recipes.py list
```

`new` takes `--template cmake` or `--template header-only` in place of `--base`, and
`--no-upload` to write the recipe without touching Nexus.

### Things to know

- **Don't reuse a version that exists on ConanCenter.** If both have `foo/1.0`, which one is
  used depends on which recipe is newer. Give ours a distinct version.
- **The `internal` remote is admin-only.** It is a folder on your machine, so it is not part
  of the team config.
- **Dependencies** of an internal package come from ConanCenter (or other internal recipes)
  and are mirrored with it automatically.
- **Templates** cover CMake libraries and header-only libraries. Anything else starts from the
  CMake template and needs its `build()` rewritten.
