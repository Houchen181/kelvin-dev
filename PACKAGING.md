# Packaging Kelvin (Windows onedir)

Kelvin's desktop app is frozen with **PyInstaller** from `packaging/kelvin.spec` (onedir) and can be
wrapped by **Inno Setup 6** from `packaging/kelvin.iss`. This note is the v2-B packaging record: how
to build it, what is in the build, how to check it, how the in-process labMaster Sequence engine runs
its scripts in a frozen app (the `KelvinRunner.exe` problem), and what is still missing for a
self-contained installer. The source of truth for the release build is
`.github/workflows/release.yml`; this file is the hand version of the same steps.

## What the build is

```
dist/Kelvin/
  Kelvin.exe          the app (windowed, no console): the portal in a pywebview window
  KelvinRunner.exe    the "Python" the packaged Sequence engine starts for its child processes (below)
  _internal/          Python 3.12 runtime, dependencies, data files, the vendored Sequence package
```

- onedir, not onefile: start-up is fast, and the folder is what the installer copies.
- version: `src/labharness/_version.py` (0.3.33 at this build); the window, the log and History read it.
- this is the whole v2 delivery in one app: the in-process Sequence engine, the v2 phase-B SafetyGate
  wiring, the labMaster-style UI/History, and the v2-C MCP tool set (34 `labmaster_*` tools in a
  118-tool MCP server - the frozen build was checked for them, see "Check the build").
- `dist/` and `build/` are in `.gitignore` - never committed.

## Build

A Windows 10/11 x64 machine; Python 3.12 (CI uses 3.13); Inno Setup only if you want the installer.

```powershell
cd kelvin-dev
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[web,hardware,agent,desktop,packaging]"
.\.venv\Scripts\pyinstaller.exe packaging\kelvin.spec --noconfirm
```

`packaging` is the extra in `pyproject.toml` that adds what only a release build needs:
`pyinstaller`, `pywebview`, `pythonnet`, `clr-loader` (the last two are pywebview's WebView2 binding
on Windows). Kelvin itself needs its `web` + `hardware` + `agent` extras: **the frozen app carries
whatever this venv has** - a package that is missing is silently skipped by the spec (it prints
`skipping <package> ...`), so build from the same dependency set the release uses.

That is not a theoretical warning: the first build of this round was made from a venv that had
`web`+`agent` but not `hardware`. It built and started, but the hardware part of the frozen
self-test failed (`pyvisa`, `pyvisa-py`, `pyserial`, `pyusb`, `libusb-package`, `psutil`,
`zeroconf`, `ifaddr` missing from the bundle). Installing them
(`pip install pyvisa pyvisa-py pyserial pyusb libusb-package psutil zeroconf "pypdf[crypto]"`) and
rebuilding fixed it. If you only need the Windows app for a quick smoke test, check the self-test
line for `FAILED` and read `kelvin.log`; for a release build use the extras.

Measured here (2026-10-30, Windows 11 x64, Python 3.12.10, PyInstaller 6.22.3):

- build time: **190.4 s** cold (first build); **124-127 s** for later builds in the same tree
  (PyInstaller reuses the analysis caches: 124.1 / 126.6 / **124.8 s**, the last one the final build
  after the v2-B gate and the v2-C MCP tools landed). Two `Analysis` passes run: the app, then
  `KelvinRunner`.
- output size: **110.3 MB** / 570 files in `dist/Kelvin`; `Kelvin.exe` **24.7 MB**,
  `KelvinRunner.exe` **6.6 MB**, the rest is `_internal/`.
- warnings in the build log are expected for packages this build machine does not have (that is the
  "install on demand" design of Kelvin's instrument libraries). With the release extras there were
  only harmless ones (`pycparser.lextab`, `tzdata`, `importlib_resources.trees`).
- build log: `D:\Codex\Kelvin\build-final.log` (outside the repo) for the final build; earlier rounds
  `build.log` / `build-initial.log`, CI keeps its own log. The source tree the final build was made
  from was snapshotted before and after (git status + sha256 of every build input) in
  `D:\Codex\Kelvin\packtest\build-snapshot-final.txt`; the two hashes matched, so the artifact matches
  the delivered source.

## Check the build

1. **The app's own self-test** - starts the portal on a free port, checks the pages, `/api/bootstrap`,
   the mobile page, that a run file can be written and read (h5py), the library bridge, and loads the
   model list; then exits. A window is *not* opened.

   ```powershell
   dist\Kelvin\Kelvin.exe --self-test --home D:\tmp\kelvin-selftest
   ```

   It prints `Kelvin self-test: ok (lab ...)` and exits 0 (`kelvin.log` in `--home`). Exit 1 and the
   log say what failed. **Always give it its own `--home`**: it writes settings, chats and the lab
   record there; never point a test at the real `%USERPROFILE%\Kelvin`.

2. **The bundled runner**:

   ```powershell
   dist\Kelvin\KelvinRunner.exe --self-test
   ```

   Prints `KelvinRunner self-test: ok (...)`, exit 0. It checks runpy, `-u -c`, and that the vendored
   Sequence runners (`run_block.py`, `run_myscript.py`, `myscript_guard.py`, `paths.py`) are in the
   bundle.

3. **Serving the portal**: run the packaged app on a port of its own and an isolated home, then ask it
   from another process:

   ```powershell
   Start-Process dist\Kelvin\Kelvin.exe -ArgumentList "--home","D:\tmp\kelvin-app","--port","8797"
   (Invoke-WebRequest http://127.0.0.1:8797/).Content -match '<title>Kelvin</title>'   # True
   ```

   (The window opens in front: that is the app; there is no `--no-window` switch. `--browser` opens the
   default browser instead, and `--self-test` exits after the checks. Add `--no-setup` to skip the
   instrument scan and the "Your instruments are set up" dialog it opens on the first start - useful for
   an isolated test box.)

   Results of this build (2026-10-30): `Kelvin.exe --self-test` exited 0 in 5-9 s (8.2 s on the final
   build), log `Kelvin self-test: ok (lab 'My lab')` (portal came up on a free port);
   `KelvinRunner.exe --self-test` exited 0 in ~1 s; `GET /` on port 8797 returned HTTP 200 with
   `<title>Kelvin</title>` in it.

4. **Screenshots of the packaged app.** A packaged Kelvin always runs with accounts, and a new install
   takes them from a Kelvin hub (`central.DEFAULT_HUB`). For an isolated test either sign in with a real
   account (the app was built for that), or point the *test* Kelvin at a throwaway local hub - plain
   `http://` is accepted for a loopback address only - and make the first account there. Then read the
   access key from `<home>\settings.json` and let Edge render one view per page; `?view=home|seq|history`
   opens a rail page directly, and the token goes in the URL fragment:

   ```powershell
   # a throwaway hub on 8798 (the test's own file, not part of the app):
   #   python D:\Codex\Kelvin\packtest\fake_hub.py 8798
   $homeDir = "D:\tmp\kelvin-app"; $key = (Get-Content "$homeDir\settings.json" -Raw | ConvertFrom-Json).access_key
   $headers = @{ "X-Kelvin-Token" = $key }; $base = "http://127.0.0.1:8797"
   Invoke-RestMethod -Method Post -Uri "$base/api/session/hub" -Headers $headers -ContentType application/json `
       -Body '{"url":"http://127.0.0.1:8798"}'
   Invoke-RestMethod -Method Post -Uri "$base/api/accounts" -Headers $headers -ContentType application/json `
       -Body '{"name":"pack","email":"pack@example.test","password":"packpass","consent":true}'
   & "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" --headless=new --disable-gpu `
       --hide-scrollbars --window-size=1440,900 --virtual-time-budget=12000 `
       --screenshot=D:\Codex\Kelvin\shots\pack-home.png "http://127.0.0.1:8797/?view=home#$key"
   ```

   (PowerShell is case-insensitive: `$homeDir`/`$headers`, not `$h`/`$H` - one variable otherwise.)

   The three screenshots from this build: `shots/pack-home.png`, `shots/pack-seq.png`,
   `shots/pack-history.png` (they show a signed-in Kelvin with a Sequence block that ran). If another
   Kelvin is *installed* on the same PC (registry uninstall entry), the running dist copy shows a thin
   banner "This copy of Kelvin isn't the installed one ..." - that is the updater being honest, not a
   packaging fault.

5. **The v2 features are really inside the frozen build.** The MCP tool table lives in the app process,
   not behind an HTTP route, so it is checked on the artifact itself: `build/kelvin/PYZ-00.pyz`
   (PyInstaller's archive) is opened with `PyInstaller.archive.readers.ZlibArchiveReader`, and the code
   objects are walked for the names:

   ```powershell
   # source checkout: the tool table build_server() makes
   .\.venv\Scripts\python.exe -c "import sys, tempfile; sys.path[:0]=['src','tests']; ..."   # 118 tools,
   #   34 of them labmaster_* (see tests/test_labmaster_mcp.py for the fixture)
   # the frozen artifact:
   .\.venv\Scripts\python.exe -c "from PyInstaller.archive.readers import ZlibArchiveReader; z=ZlibArchiveReader(r'build\kelvin\PYZ-00.pyz'); ..."
   ```

   The final build checked (all present): the **34 `labmaster_*` tool names** in
   `labharness.agent.server` (13 READ + 21 ACT: add/update/delete/move/duplicate/clear block, saved
   save/load/delete, import/write/read/update/duplicate/remove functions, history/run detail, analysis
   scripts/write/run/cancel/status, datasets/dataset files, log, home, armed control, rename); the new
   `web/labmaster.py` facade functions (`write_script`, `read_script`, `history_runs`, `run_detail`,
   `write_analysis`, `run_analysis`, `datasets`, `log_lines`, ...); `attach_gate` and the
   `/api/history/all` + `/api/labmaster/history` handlers in `web/app.py`; the `gate` module with
   `BlockGate`/`make_engine` in `sequence/gate.py`. On the running packaged app, the HTTP doors of
   those features answered `ok: true`: `/api/labmaster/{state,palette,scripts,history,armed}`,
   `/api/history/all`, `/api/home`; after a SIM block, `/api/labmaster/history` had 1 run and
   `/api/history/all` agreed (`total: 1, counts.panel: 1`).

   Source checkout regression for the same features: `pytest tests/test_labmaster_mcp.py
   tests/test_sequence_gate.py tests/test_tool_loading.py -q` -> **86 passed**.

## Sequence in the packaged app: `KelvinRunner.exe`

**The problem.** The vendored labMaster engine (`src/labharness/sequence/`) runs a block's measurement
script, the safe stop and the analysis scripts as *child processes*, started with
`[paths.python_exe(cfg), "-u", <runner.py>, ...]` (`engine.py` `_spawn`/`_my_cmd`, safe stop in
`_run_safe_stop_child`). In a source checkout that interpreter is the venv's Python. In a frozen app
`sys.executable` is **`Kelvin.exe`**, so the same command would start a second Kelvin instead of
running the runner script: a Sequence block would "run" by opening another app and the rep would never
finish.

**The answer (v2-B, plan A).** The build ships a second executable, **`KelvinRunner.exe`**:

- `packaging/kelvin_runner.py` is its entry point; `packaging/kelvin.spec` freezes it as a second
  `EXE`+`PYZ` in the same `COLLECT`, beside `Kelvin.exe`.
- `labharness/sequence/paths.py` gained one small frozen branch: `python_exe()` returns the configured
  `paths.python_exe` when a lab set one, else `KelvinRunner.exe` **when `sys.frozen`**, else
  `sys.executable` as before. Source checkouts are untouched. `tools/vendor_sequence.py` must keep this
  patch when it regenerates the vendored files (`paths.py`: `frozen_runner()` + the branch in
  `python_exe()`).
- `KelvinRunner.exe` is a minimal interpreter, not a general Python: `-u <script.py> [args...]` runs
  the file as `__main__` with `runpy` (so `sys.argv`, `__file__`, exit codes, `KeyboardInterrupt` and
  the `##PANEL {...}` event lines are exactly what the runners expect); `-u -c <code>` runs the
  engine's pre-flight import check; `--self-test` checks the build. The leading `-X utf8`, `-E`, `-u`
  ... flags are accepted and skipped, because a frozen interpreter cannot change those settings.
- The runner also emulates what `site.py` does for a normal Python: `PYTHONPATH` entries join
  `sys.path` and the first `sitecustomize.py` among them is executed. That is how Kelvin's instrument
  stand-in (`labharness/data/scriptshim`, the v2 phase-B **SafetyGate** bridge) and the analysis guard
  (`sequence/runners/guard_site`) reach a child the app starts - a frozen interpreter never runs
  `site`, so without this the gate would silently not load and a real block would run ungated.
- the vendored `sequence` package travels in the bundle as **files** (not only inside the PYZ), because
  `run_block.py`/`run_myscript.py` put their own folder on `sys.path` and `import paths` as a
  top-level module; `packaging/kelvin.spec` puts them under `_internal/labharness/sequence/`.
- `KelvinRunner.exe` carries the standard library plus `pyvisa`, `pyvisa_py`, `pyserial`, `pyusb`,
  `libusb-package`, `numpy`, `h5py`, `yaml`, `psutil`, `zeroconf`, `ifaddr` as hidden imports, so a
  measurement script that imports only those (or reaches instruments through Kelvin's gate shim) runs
  from the app with no other Python installed. Its DLLs and data files are shared from the main build.

What this means in practice, on a packaged Kelvin:

| Sequence use | Works out of the box? |
| --- | --- |
| simulation (`sim: true`, the default) | yes - the simulated runner never imports the script |
| safe stop in real mode | yes - the safe-stop script is started by `KelvinRunner.exe` |
| real block through Kelvin's SafetyGate (pyvisa/pyserial access) | yes - the gate shim is loaded by `KelvinRunner.exe` and drives the instrument through Kelvin |
| block whose script imports `numpy`/`h5py`/`pyvisa`/... | yes - those are in the runner |
| block whose script imports a maker's library, `scipy`, `matplotlib`, ... | needs a real lab Python (below) |
| analysis scripts (`my_analysis`, run under the guard) | stdlib ones yes; anything the app does not carry needs a real lab Python |

Verified from the packaged app of the **final build** (build 4, after the v2-B gate and v2-C tools;
isolated `--home`, port 8797, `--no-setup`):

- **simulation**: a "Pack demo sweep" block (a stdlib-only measurement script imported through
  `/api/labmaster/import`) started, completed in 6.5 s with `sequence finished`, and appeared in
  History as `source: panel, status: done`; `/api/labmaster/history` and `/api/history/all` both
  showed it. A `KelvinRunner` process was there while the block ran and no second `Kelvin.exe`
  started (process count before/after the run: 3/3, this box's two other Kelvins included).
- **real mode** (`config.json` `"sim": false`, nothing else): the same block ran the real script under
  `KelvinRunner.exe` - the Sequence log shows the script's own lines, `Data saved to ...\sequence\data\
  2026-10-30\pack_demo_17-28-49`, the dataset event, and History says `1 block, 1 dataset`
  (`n_datasets: 1`). The engine's pre-flight import check (`KelvinRunner.exe -u -c ...`) passed too, so
  `-c` mode works in the frozen runner.
- **safe stop and analysis runners**: the same command the engine uses for the safe-stop child
  (`KelvinRunner.exe -u <safe_stop.py>`) ran a stop script; and `run_myscript.py --guard` (the
  analysis runner) ran a stdlib analysis script under the instrument guard to the end - its
  `##PANEL {"event": "analysis", ... "verdict": "ok"}` line and `analysis.json` came out, with the
  one-time "rerun with -X utf8 is still not in UTF-8 mode" note described below. So the third kind of
  Sequence child (analysis) also works frozen for scripts that only need what the app carries.
- **the gate (v2 phase B) in a frozen app**: a **real-mode** block was started from the packaged app on
  the final build. The Sequence log of the block contains the script's own line
  `packaged runner: KELVIN_IPC=True frozen=True mode=real` - so the gated engine injected its
  environment into the `KelvinRunner.exe` child, and the frozen runner ran `sitecustomize.py` /
  `kelvin_shim.install()` and completed its IPC handshake with the running Kelvin (the shim would have
  killed the child with exit 3 had the handshake failed). Kelvin's lab record got the matching entries:
  `sequence_block_start ... "gate": "in-process"` and `sequence_block_end ... "status": "done",
  "commands": 0` (the demo script drives no instrument, so there were no commands to approve - a
  command-level approval test still needs a real/virtual instrument; the gate's own unit tests cover
  that logic). What the packaging does is unchanged: the same `PYTHONPATH` hand-off the gate uses from
  source is what the runner emulates.

`KelvinRunner.exe` is started by the engine only; a person never runs it except `--self-test`.

## Real lab scripts: point the engine at a lab Python (plan B)

When a script needs packages the app does not carry, give the Sequence engine a real Python - it is
one setting, and the app keeps working as before:

```jsonc
// <home>/sequence/config.json   (<home> is %USERPROFILE%\Kelvin unless Kelvin was started with --home)
{
  "paths": {
    "python_exe": "C:\\Users\\me\\miniconda3\\envs\\lab\\python.exe",
    "safe_stop_script": "scripts/my_safe_stop.py"
  }
}
```

- empty `paths.python_exe` = automatic (`KelvinRunner.exe` in a packaged app, `sys.executable` from source);
- this is the *Sequence engine's* setting, in `sequence/config.json`. Kelvin's own Settings →
  "Python for lab scripts" (`lab_scripts_python`) is a different setting (it is for Kelvin's script tools,
  not for Sequence blocks);
- the engine reads `config.json` **when it starts**, so restart Kelvin after editing it;
- the child still receives Kelvin's gate environment (the shim is reached through `PYTHONPATH`/env, not
  the interpreter), so a real Python is still gated by the SafetyGate;
- Kelvin also installs maker instrument libraries into its own instrument environments (`envs.py`);
  the packaged app can use those interpreters the same way (name them here).

## Known issues / next steps

- **Installer not built on this machine.** `packaging/kelvin.iss` (Inno Setup 6) wraps `dist/Kelvin/*`
  and is already used by `.github/workflows/release.yml`; this build machine has neither Inno Setup
  (`ISCC.exe`) nor Chocolatey, so no `dist/Kelvin-Setup-<version>.exe` was produced here. Next step:
  install Inno Setup 6 (`winget install JRSoftware.InnoSetup`, or the workflow's
  `choco install innosetup -y`) and run `ISCC.exe /DAppVersion=<version> packaging\kelvin.iss`. The
  workflow also installs the app and re-runs `--self-test` against the installed copy. The `[Files]`
  section copies the whole folder, so `KelvinRunner.exe` needs no installer change.
- **`paths.py` is a vendored file with a Kelvin patch.** `tools/vendor_sequence.py` (owned by the
  engine work) must keep `frozen_runner()` and the `python_exe()` branch; after re-vendoring, check
  `git diff src/labharness/sequence/paths.py` still shows them, or a packaged app starts `Kelvin.exe`
  again instead of the runner.
- **Not in git**: `dist/` and `build/` are already in `.gitignore`; the packaging work adds
  `packaging/kelvin_runner.py` and `PACKAGING.md`, and edits `packaging/kelvin.spec`, `pyproject.toml`
  and the frozen branch in `sequence/paths.py` (that one is mirrored in `tools/vendor_sequence.py` and
  recorded in `src/labharness/sequence/VENDORED.json`; `python tools/vendor_sequence.py --check` must
  keep reporting the 14 vendored files current).
- **Self-contained installer** (the real next step): the onedir app already carries Python and
  `KelvinRunner.exe`; still missing for a "nothing else on the PC" install are code signing (SmartScreen
  on an unsigned exe), a WebView2 runtime check in the installer, and a decision about instrument
  environment downloads (they need network). Onefile packaging is not supported/tested: the Sequence
  runner and the analysis guard expect the bundle's files on disk.
- **`-X utf8` rerun in `run_myscript.py`**: a frozen interpreter cannot switch UTF-8 mode, so a guarded
  analysis run starts itself once more (`KelvinRunner.exe -X utf8 -u run_myscript.py ...`), prints the
  runner's "still not in UTF-8 mode" note and goes on. One extra start (~1 s); no functional loss. From
  source/venv this does not happen.
- **`KelvinRunner.exe` is not a general Python**: no `-m`, no REPL, no `pip`. It runs one script with
  the interpreter the app carries.
- **Build hygiene**: build from a venv with the release dependency set; the spec silently skips
  packages that are not installed (`skipping <package>`) and the self-test only fails on what it checks.
  A missing optional instrument library is expected, a missing `h5py`/`pypdf`/`psutil`/`zeroconf` is not
  (the frozen self-test checks those).
- **macOS**: the same spec builds `Kelvin.app` (with `KelvinRunner` inside `Contents/MacOS`), but this
  packaging round only built and verified the Windows onedir app.
- **Other people's data**: a packaged Kelvin is a normal Kelvin - it writes `%USERPROFILE%\Kelvin`
  unless `--home` is given. Use an isolated `--home` and a port of your own (8795/8796/8797...; the
  demo instance uses 8791) when testing a build.
- `dist/` is not committed; screenshots and build logs live outside the repo
  (`D:\Codex\Kelvin\shots\pack-*.png`, `D:\Codex\Kelvin\build-final.log` for the final build,
  `D:\Codex\Kelvin\packtest\` for the isolated test homes, the demo measurement script and the local
  test hub).
