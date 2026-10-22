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
    ("anthropic", "anthropic", ("anthropic._vendor.httpx_aiohttp",)),
    ("openai", "openai", ()),
    ("google.genai", "google-genai", ()),
    ("yaml", "PyYAML", ()),
    ("pyvisa", "PyVISA", ()),                      # real instruments
    ("pyvisa_py", "PyVISA-py", ()),                # loaded by name when no vendor VISA is installed
    ("serial", "pyserial", ()),
    ("usb", "pyusb", ()),                          # USB instruments without a vendor VISA ...
    ("libusb_package", "libusb-package", ()),      # ... through Kelvin's own libusb
    ("psutil", "psutil", ()),                      # network instruments: broadcast on every interface
    ("zeroconf", "zeroconf", ()),                  # network instruments: mDNS (LXI) adverts
    ("ifaddr", "ifaddr", ()),
    ("pypdf", "pypdf", ()),                        # reading instrument manuals
    # the makers' instrument libraries: only the private build installs them (release.yml); skipped otherwise
    ("nidaqmx", "nidaqmx", ()),                    # NI DAQ cards (NI-DAQmx itself is NI's installer)
    ("seabreeze", "seabreeze", ()),                # Ocean Insight spectrometers
    ("pypylon", "pypylon", ()),                    # Basler cameras, with the pylon runtime
    ("pylablib.devices.Attocube", "pylablib", ()), # attocube ANC300 / ANC350
    ("pylablib.devices.Andor", "pylablib", ()),    # Andor Shamrock / Kymera spectrographs
    ("pylablib.core", "pylablib", ()),
]
if sys.platform == "win32":
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
        # the makers' libraries look up their dependencies by metadata (nidaqmx: nitypes...), so bring theirs too
        datas += copy_metadata(dist, recursive=dist in ("nidaqmx", "seabreeze", "pylablib"))
    except Exception:
        pass

# the helper for a maker's library runs as a script, with this app or another Python (labharness/library.py)
datas.append((str(ROOT / "src" / "labharness" / "data" / "bridge" / "kelvin_bridge.py"), "labharness/data/bridge"))

a = Analysis(
    [str(ROOT / "packaging" / "kelvin_app.py")],
    pathex=[str(ROOT / "src")],
    datas=datas,
    binaries=binaries,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "pytest", "PyInstaller"],
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
