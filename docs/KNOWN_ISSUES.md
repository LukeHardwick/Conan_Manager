# Known issues

Findings from a review of `conan_mirror.py` (since split into `mirror_core.py`, `gui.py` and
`gui_theme.py`; line numbers below are in those files), `team_setup.py` (since replaced by
`conan config install`, see the README) and `check_network.py`
(November 2026, checked against Conan 2.34). Issues 1, 2, 3, 4, 6 and 7 are fixed or gone (see the end); for the rest, each entry says
what goes wrong and the suggested fix.

## What was checked

- **Dependencies:** the scripts import only the Python standard library, so what we
  ship has no third-party dependencies. Caveats:
  - tkinter is not always present (Homebrew Python needs `brew install python-tk`). Only the GUI
    needs it: `conan_mirror.py --manifest` runs without tkinter.
  - `check_network.py:105` calls `ssl._ssl._test_decode_cert`. It is in the standard
    library but private, so a future Python release could remove it.
- **Static checks:** every script passes `python -m mypy` in strict mode (configured in
  `pyproject.toml`), checked for Windows, macOS and Linux.
- **Conan behaviour:** the flags the mirror relies on (`tools.graph:skip_binaries`,
  `tools.build:download_source`, `conan list --graph-binaries=...`, `conan upload --list`,
  `CONAN_PASSWORD_<REMOTE>` for `conan remote login`, Jinja in `source_credentials.json`)
  all behave as the code expects.

## Issues, most severe first

### 5. Cancel leaves builds running on macOS/Linux
`mirror_core.py:199`. `p.terminate()` only stops the `conan` process; cmake and compiler
children keep running. (Windows is fine: it uses `taskkill /T`.)

**Fix:** start Conan with `start_new_session=True` and cancel with `os.killpg(p.pid, SIGTERM)`.

### 8. Profile wizard writes incomplete profiles for some compilers
`mirror_core.py:605` (`make_profile`). `compiler.libcxx` is only set for gcc/clang on Linux and
for apple-clang. gcc on Windows (MinGW) gets none, and C++ packages are then invalid for that
profile ("'settings.compiler.libcxx' value not defined"). Clang on Windows or macOS also gets
none; Conan accepts that, but on macOS the standard library is then missing from the profile.

**Fix:** set `libc++` for clang on macOS, `libstdc++11` for gcc on Windows, and leave clang-cl
on Windows without it.

### 9. Minor
- **Buttons fail silently when `conan` is missing.** *Open profiles folder*, *Open Conan home*
  and the profile editor call `self.conan.home()` on the UI thread. If Conan can't be run, the
  error goes to the console, not the log. The cached home path is also not refreshed when the
  Conan executable setting changes.
- **Profile names are not checked.** A name like `../x` in *Duplicate* or *Detect* writes
  outside the profiles folder.
- **Conan 2.34 has a new `core.net.http:trust_store` setting.** It verifies against the OS
  certificate store, but needs `pip install truststore` alongside Conan, which is not possible with the portable
  Conan build. Not needed for our
  setup; noted for reference.

## Fixed

Numbers are kept from the original review so references to them still work.

- **1. The app could freeze permanently after a UI error.** `App._drain` now catches an error
  in a queued update, logs it, and always reschedules itself.
- **2. "Show effective profile" and the end of *Apply to Conan* showed nothing.** Both
  commands print their answer on stdout, which `Conan.run` returned but nobody logged. They
  now use `Conan.show`, which copies that answer to the log.
- **3. Mirror did not always ask for credentials.** The login prompt now appears whenever the
  raw repo is configured, not only when *Fetch sources* is ticked.
- **4. Debug profiles were also used for build tools.** There is a *Build profile* setting
  (a Release profile) used for `-pr:b` in Plan, Mirror and Test build. The exported team
  config ships that profile and its README tells developers to pass it. It is empty by
  default, which keeps the old behaviour, so set it under Settings. Checked against
  ConanCenter: with a Debug host profile, `ninja` as a build tool went from "build from
  source" to "download".
- **6. Apply replaced the whole `source_credentials.json`.** The mirror now works in its own
  Conan home (`~/.conan_mirror/conan_home`), so the file it writes is the tool's own and your
  everyday Conan home is never touched. It only applies again if you clear the *Conan home
  for mirroring* setting.
- **7. `team_setup.py apply -y` still asked for a username.** Gone with `team_setup.py`: the
  team now installs its configuration with `conan config install`.

## Not tested

Mirror, upload, skip-on-rerun and Verify (including its failure cases) were run end to end
on macOS against a local `conan_server` and a small stand-in for the raw repo. Still untested:

- Anything against the real Nexus (its Conan repo and its raw repo may behave differently
  from the stand-ins, e.g. on permissions and redeploy).
- Anything on Windows, including the MSVC library names in the `cigi-ccl` recipe.
- The GUI buttons for Export team config, Create draft and Test build were not clicked; the
  functions behind them were run directly.
