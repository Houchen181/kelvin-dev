# PyInstaller recipe for the Kelvin desktop app.
#   pyinstaller packaging/kelvin.spec --noconfirm
# Windows: dist/Kelvin/Kelvin.exe (folder app; the installer wraps it)
# macOS:   dist/Kelvin.app
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules, copy_metadata

ROOT = Path(SPECPATH).parent
ICONS = ROOT / "packaging" / "icons"
sys.path.insert(0, str(ROOT / "src"))
from labharness._version import __version__  # noqa: E402

# (import name, distribution name, submodule prefixes to leave out)
PACKAGES = [
    ("labharness", "kelvin-lab", ()),
    ("webview", "pywebview", ("webview.platforms.android",)),
    ("uvicorn", "uvicorn", ()),
    ("starlette", "starlette", ()),
    ("mcp", "mcp", ("mcp.cli",)),                  # the CLI needs typer, which Kelvin does not use
    ("websockets", "websockets", ()),              # phones through the Kelvin hub (web/relay.py)
    ("anthropic", "anthropic", ("anthropic._vendor.httpx_aiohttp",)),
    ("openai", "openai", ()),
    ("google.genai", "google-genai", ()),
    ("yaml", "PyYAML", ()),
    ("h5py", "h5py", ()),                          # each run's data file, run.h5 (rundata.py)
    ("pyvisa", "PyVISA", ()),                      # real instruments
    ("pyvisa_py", "PyVISA-py", ()),                # loaded by name when no vendor VISA is installed
    ("serial", "pyserial", ()),
    ("usb", "pyusb", ()),                          # USB instruments without a vendor VISA ...
    ("libusb_package", "libusb-package", ()),      # ... through Kelvin's own libusb
    ("psutil", "psutil", ()),                      # network instruments: broadcast on every interface
    ("zeroconf", "zeroconf", ()),                  # network instruments: mDNS (LXI) adverts
    ("ifaddr", "ifaddr", ()),
    ("pypdf", "pypdf", ()),                        # reading instrument manuals
    # no maker's instrument library: Kelvin installs the one an instrument needs, once a person approves (envs.py)
]
if sys.platform == "win32":   # the window's own (pywebview's WebView2 through .NET), which LightField's automation uses
    PACKAGES += [("clr_loader", "clr_loader", ()), ("pythonnet", "pythonnet", ())]

datas, binaries, hiddenimports = [], [], []
for package, dist, skip in PACKAGES:
    keep = lambda name, skip=skip: ".tests" not in name and not name.startswith(skip)  # noqa: E731
    try:
        hiddenimports += collect_submodules(package, filter=keep, on_error="ignore")
        datas += collect_data_files(package, excludes=["**/tests/**"])
        binaries += collect_dynamic_libs(package)
    except Exception as e:  # a platform-specific package that is not installed here
        print("skipping", package, e)
        continue
    try:
        datas += copy_metadata(dist)   # so envs.have() sees what the app brings
    except Exception:
        pass

# Kelvin's own documentation, which kelvin_help searches (labharness/guide.py)
datas += [(str(ROOT / "docs" / name), "labharness/data/docs") for name in
          ("portal.md", "safety.md", "data.md", "instruments.md", "agents.md", "hub.md")]

# the helper for a maker's library runs as a script, with this app or another Python (labharness/library.py)
datas.append((str(ROOT / "src" / "labharness" / "data" / "bridge" / "kelvin_bridge.py"), "labharness/data/bridge"))

a = Analysis(
    [str(ROOT / "packaging" / "kelvin_app.py")],
    pathex=[str(ROOT / "src")],
    datas=datas,
    binaries=binaries,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "pytest", "PyInstaller",
              # makers' instrument libraries, whatever this computer has installed: Kelvin installs them on demand
              "seabreeze", "pypylon", "pylablib", "nidaqmx", "zhinst", "MultiPyVu", "qick", "Pyro4",
              "pulsestreamer", "spinapi", "TimeTagger", "Swabian", "scipy", "pandas"],
    noarchive=False,
)
# pypylon brings Basler's image-processing plugins (vTools, ~230 MB); Kelvin only grabs frames, so they stay out
UNUSED = ("pylonDataProcessingPlugins", "DataProcessingPluginsB")
a.binaries = [b for b in a.binaries if not any(part in b[0].replace("\\", "/").split("/") for part in UNUSED)]
a.datas = [d for d in a.datas if not any(part in d[0].replace("\\", "/").split("/") for part in UNUSED)]
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Kelvin",
    console=False,
    icon=str(ICONS / ("kelvin.icns" if sys.platform == "darwin" else "kelvin.ico")),
)
coll = COLLECT(exe, a.binaries, a.datas, name="Kelvin")
if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="Kelvin.app",
        icon=str(ICONS / "kelvin.icns"),
        bundle_identifier="io.github.houchen181.kelvin",
        version=__version__,
        info_plist={"CFBundleDisplayName": "Kelvin", "NSHighResolutionCapable": True,
                    "LSMinimumSystemVersion": "11.0"},
    )
