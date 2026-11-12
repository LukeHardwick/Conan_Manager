# Known issues

Findings from a review of `conan_mirror.py`, `team_setup.py` and `check_network.py`
(November 2026, checked against Conan 2.34). None of these are fixed yet; each entry says
what goes wrong and the suggested fix.

## What was checked

- **Dependencies:** all three scripts import only the Python standard library, so what we
  ship has no third-party dependencies. Caveats:
  - tkinter is not always present (Homebrew Python needs `brew install python-tk`).
  - `check_network.py:103` calls `ssl._ssl._test_decode_cert`. It is in the standard
    library but private, so a future Python release could remove it.
- **Static checks:** ruff and mypy report only typing nitpicks, no real errors.
- **Conan behaviour:** the flags the mirror relies on (`tools.graph:skip_binaries`,
  `tools.build:download_source`, `conan list --graph-binaries=...`, `conan upload --list`,
  `CONAN_PASSWORD_<REMOTE>` for `conan remote login`, Jinja in `source_credentials.json`)
  all behave as the code expects.
- **team_setup round trip:** `apply` → `apply` again → `revert` in a throwaway Conan home
  restored every file byte for byte.

## Issues, most severe first

### 1. The app can freeze permanently after a UI error
`conan_mirror.py:478` (`App._drain`). If any queued UI callback raises, the exception escapes
`_drain` before `self.after(80, self._drain)` runs, so the queue is never drained again. The
app stays "busy", logs stop and every button reports "Another task is running".

**Fix:** wrap each queued call in `try/except` (log the error) and reschedule in a `finally`.

### 2. "Show effective profile" shows nothing
`conan_mirror.py:925`. `conan profile show` prints to **stdout**, but `Conan.run` only logs
stderr and returns stdout, which this caller ignores. The same applies to the `conan remote list`
at the end of *Apply to Conan* (`conan_mirror.py:1073`).

**Fix:** log the returned stdout for these two calls (or add a `log_stdout=True` option to `run`).

### 3. Mirror does not always ask for credentials when the upload needs them
`conan_mirror.py:794`. The login prompt only appears when *Fetch sources* is ticked. But
`conan upload` uploads backup sources whenever `core.sources:upload_url` is set, and building
from source downloads sources regardless of that checkbox. The upload then fails with
"Authentication to source backup server ... failed".

**Fix:** prompt whenever `raw_url` is set and this is not a dry run.

### 4. Debug profiles are also used for build tools
`conan_mirror.py:280` passes the same profile as `-pr:h` and `-pr:b`. With a Debug profile,
tool requirements (cmake, ninja, ...) are resolved as Debug too, which ConanCenter usually has
no binaries for, so they get built from source. The README's `-pr:a <profile>` advice to the
team does the same.

**Fix:** add a "build profile" setting (a Release profile) used for `-pr:b`, and tell the team
to use `-pr:h <profile> -pr:b <release profile>`.

### 5. Cancel leaves builds running on macOS/Linux
`conan_mirror.py:217`. `p.terminate()` only stops the `conan` process; cmake and compiler
children keep running. (Windows is fine: it uses `taskkill /T`.)

**Fix:** start Conan with `start_new_session=True` and cancel with `os.killpg(p.pid, SIGTERM)`.

### 6. Apply replaces the whole `source_credentials.json`
`conan_mirror.py:1066`. A `.bak` is kept, but credentials for any other URLs in the file stop
working.

**Fix:** merge our entry into the existing `credentials` list instead of overwriting the file.

### 7. `team_setup.py apply -y` still asks for a username
`team_setup.py:221`. `--yes` is documented as "don't ask questions", but without `--user` it
still calls `input()`.

**Fix:** with `--yes` and no `--user`, fall back to `getpass.getuser()` without prompting.

### 8. Profile wizard writes incomplete profiles for some compilers
`conan_mirror.py:358` (`make_profile`). `compiler.libcxx` is only set for gcc/clang on Linux and
for apple-clang. Clang on Windows or macOS and gcc on Windows (MinGW) get none, and Conan
then rejects or misidentifies the profile.

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
  certificate store, but needs `pip install truststore` alongside Conan. Not needed for our
  setup; noted for reference.
