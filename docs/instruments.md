# Instruments

How Kelvin finds and talks to real instruments, what it keeps about each one, and how to add an instrument it doesn't know yet.

## Connecting instruments

Kelvin talks to instruments over VISA: GPIB, USB-TMC, LAN and RS-232 / USB-serial. It uses the VISA library you already have (NI-VISA or Keysight IO Libraries), or a pure-Python one if there is none. Install the extra with `pip install "kelvin-lab[hardware]"`; the desktop app includes it.

**Kelvin sets up what it finds.** The first time the desktop app starts (and on every start until you have a lab of your own), Kelvin asks each connected instrument who it is. If any match a card, it connects them right away:
- **Your lab's scripts help:** during that first setup, Kelvin asks whether your lab runs its instruments from scripts (Python, notebooks or MATLAB) and, if so, for their folder. It reads them and never runs them. If the answer comes before it writes your lab config, Kelvin names each instrument as your scripts do, takes the values they use as its limits (marked "from your scripts", for you to check), and adds an instrument the scan saw but that said nothing to who-are-you (as serial devices often don't) when your scripts surely say what it is, with their connection settings. An instrument that answered with a reply no card matches still gets its card written from its manual. If the answer comes later, **Set up…** opens on what the scripts add once the setup is done: instruments to add, and **Use these limits** for those already in your lab.
- **Asking until you review the limits.** Kelvin writes `~/Kelvin/lab.yaml` with the instruments it found, each marked `added_by: kelvin`. Readings and measurement settings work at once. Their limits are the maker's maximums, so until you check them in **Set up…** and press **Limits reviewed**, Kelvin asks you before every output change on them (voltage, current, field, temperature, laser power), with a line saying why, and neither auto mode nor a project envelope covers them. **Limits reviewed** takes the marks out of the lab config; nothing is reconnected. Labs from older versions marked `observe_only: true` work the same way.
- **Instruments you name.** Say "the Keithley 2450 on GPIB 18 is connected" or "use the lock-in I just plugged in" in a chat, and Kelvin sets it up itself: it scans, picks the matching card (or finds the maker's manual online and writes a draft card, then tries it), adds the instrument to your lab marked `added_by: kelvin`, connects just that one (the others stay connected as they are), and tells you. It asks you first only for an address the scan didn't find, or to move from the virtual lab you chose to your instruments. It stops and tells you what it needs when it can't go on by itself: a setting on the instrument (remote control, its address, a list of allowed computers), the maker's software or driver, or a manual that isn't public (choose its PDF).
- **Switched-off instruments.** An instrument you switched off in the Lab panel stays off. When Kelvin needs it, it asks you to switch it on and says why; it is switched on only if you say yes.
- **Instruments that can't be connected.** One instrument that can't be connected (its maker's package missing, switched off at the wall, unplugged) no longer takes your whole lab to the virtual one: the Lab panel marks it **not connected** and says why, with an Install button when a package is missing, and the rest of your lab works. Kelvin sends it nothing, and plans that use it, and drives guarded by its readings, are refused. Once it is fixed, switch it on in the Lab panel, or Kelvin connects it (`reconnect_instrument`; an install connects the instruments it was for by itself).
- **Kelvin opens before your instruments connect.** Connecting can take minutes, so the app opens at once on your lab and connects its instruments in the background, one after another; you can sign in and use Kelvin meanwhile ([portal.md](portal.md#signing-in)). Until one is connected, the Lab panel shows it as connecting, and Kelvin says so when asked to use it. One that can't be connected is marked **not connected** with why, as above; Kelvin stays on your lab even when none connect.
- **A folder for each instrument** in `~/Kelvin/instruments/<name>/`:
  - `instrument.json`: who it is (maker, model, serial number and firmware from its identity reply), its address, the connection settings it answered with (such as 57600 baud, 7 data bits, odd parity), and where its manual came from.
  - `settings.json`: every setting and reading it had (ranges, time constants, sensitivities...), refreshed on every start.
  - `manual.pdf`: the maker's own manual, fetched once from the address on the card, and `manual.txt` with its text by page.
- **Manuals for involved scans.** Before a scan that uses buffers, triggers, sweep or list modes, fast acquisition, ranges or filters, Kelvin reads the instrument's manual with `instrument_info` and `search_manual`. It works from the passages it finds and cites the page. Search weighs rare words most, so a mnemonic such as `OFLT` or a SCPI path such as `SOURce:CURRent:LIMit` finds its own entry.
- **Manuals you add yourself.** 28 of the 49 cards carry the maker's link, the Oxford Mercury iTC's among them (Oxford Instruments publishes its manual on the Mercury support site). Keysight hands out some manuals only after a sign-in, SpinCore's is behind its support pages, and M Squared, Light Conversion and TOPTICA give their protocol documents to owners. The cards for instruments driven through a maker's library follow its SDK's documentation, and QICK's documentation is on its project site. For these, or for any other instrument, choose the PDF in **Set up…** (**Choose PDF…**) or put it in the folder as `manual.pdf`.
- **Switching it off.** Settings has **Set them up when Kelvin starts**, **Fetch their manuals from the makers** and **Look on the local network too**. Fetching a manual sends only its address to the maker's site. An existing `lab.yaml` is never rewritten. Instruments it doesn't list are only reported.

**Where a scan looks.** No vendor driver is needed for the network or USB.
- **VISA:** the one you have (NI-VISA, Keysight IO Libraries, R&S VISA), or Kelvin's own if there is none.
- **The local network:** instruments that announce themselves (LXI), through a VXI-11 broadcast and their mDNS adverts (VXI-11, HiSLIP, raw SCPI on port 5025). The first time, Windows may ask whether Kelvin may use the network: allow private networks.
- **USB:** every device, through Kelvin's own libusb. An instrument is recognised by its USB test-and-measurement interface or its maker's ID. If no driver lets Kelvin open it, the scan names it and the driver to install.
- **Serial ports and GPIB adapters.**

**Why Kelvin is on the virtual lab.** While Kelvin is on the virtual lab, the Lab panel says why:
- No instruments are connected to this computer (Kelvin has to run on the computer the instruments are plugged into).
- An instrument answered, but Kelvin has no card for it yet.
- A USB instrument needs a driver.
- Looking at start-up is switched off.
- You chose the virtual lab.

**What Kelvin looked through** lists each place it searched, and so do **Scan** in **Set up…** and Kelvin's `scan_instruments` tool. Every start-up scan is also written to `~/Kelvin/kelvin.log`.

**Set up your instruments in the app:** open the Lab panel and press **Set up…**. In the setup:
1. **Scan** finds the connected instruments. You can also add one by address, such as `GPIB0::24::INSTR` or `serial://COM3`, and pick its card.
2. Review the drafted lab config, mainly the limits. **By instrument** shows one row per instrument (its name, card and address) with a search box: type part of a name, card or address to find one, and click it to edit just its part of the file, comments included. **Whole file** shows all of it, with the lab's name, interlocks and watches.
3. **Save and connect** saves it as `~/Kelvin/lab.yaml` and health-checks every instrument. It checks that each one is the model its card says, so a changed address can't send commands to the wrong instrument. It also reads every reading once and checks that drives sit inside your limits.

**From your lab's scripts.** Many labs already run their instruments from their own Python, Jupyter or MATLAB scripts. In **Set up…**, **From your scripts…** reads the scripts in a folder you choose. It never runs or imports them, and sends nothing from them to an instrument.
- **What it finds:** for each instrument the scripts open, its address, the connection settings they use (baud rate, line endings, timeout), the commands they send with the values used, and the Kelvin card that fits: a model named in the scripts (a comment, a driver's class) and how many of their commands are on the card. An instrument at a VISA address is asked who it is, as a scan asks, and its reply decides the card.
- **Limits from the values used:** the range the scripts use (with the card's safe value), inside the maker's limits.
- **Adding them:** tick the ones you want and press **Add the checked ones to the lab config**. They go into the draft with those limits and settings, marked "from your scripts" for you to check; then **Save and connect** as usual.
- **Recipes:** **Turn the measurement scripts into recipes…** opens a new chat with that request written in. Send it, and Kelvin proposes a recipe for each measurement script (its loops become sweeps, its readings the measure list, its waits the settle times) and keeps each one you agree to.
- **In a chat:** ask Kelvin to read your scripts and it does the same (`survey_scripts`).
- **What it skips:** library folders (`site-packages`, `python_libs`, `venv`, `lib`, ...). It reads up to 300 files, each up to 1 MB.

**Limits for one instrument.** In **Set up…**, each instrument of your lab has **Limits…** on its row. It lists the instrument's numeric settings, outputs first, with the card's limits and your lab's.
- **Changing them:** set the lowest and highest values (leave both empty for the card's limits) and press **Save limits**. They are written into `lab.yaml`, and the rest of the file, comments included, stays as it was. They hold at once, also during a run, without reconnecting.
- **Beyond the card:** a limit beyond the card's own is refused; that needs [your version of the card](#cards-for-new-instruments).
- **Instruments Kelvin added:** tick **I checked these for my sample** to take out its `added_by: kelvin` mark when you save.
- **Only people:** Kelvin can't change limits. Only a person can, here or in the lab config.
- **Where a limit comes from:** an instrument's limits are its card's range, narrowed where your lab's `limits:` or a mounted device's protection say so. When Kelvin refuses a value, it says which of these it hit, and it can read your lab config (`lab_config`) to show you what the file says, including where the file differs from what Kelvin uses until you press **Save and connect**.

Kelvin opens on your lab from then on. You can switch back to the virtual lab at any time. If your lab can't be connected at start-up, Kelvin opens the virtual lab and says why. Kelvin only switches labs while it is idle, and approvals go back to asking.

From a terminal:

```bash
kelvin-scan --write lab.yaml
```

The scan only asks each instrument who it is (`*IDN?`, or the card's own identity query); it never sets anything. Serial ports are tried with each known card's framing and identity query (Newport's `1VE` at 57600 8N1, Pfeiffer's `AYT` through its ENQ protocol); for example, the Lake Shore 336 needs 57600 baud, 7 data bits and odd parity. An Arduino or a CH340 USB-serial board resets each time its port opens, so it is asked once, at 9600 8N1, after it has booted. A command line that echoes what it is sent is read past its echo, and a LAN socket that greets each connection (an AMI 430's) past its greeting. The command matches each instrument to a card and drafts a lab config for you to review; a laser whose identity reply names its rating (an OBIS's `20mW`) gets that as its power limit, not the card's for the whole series. The agent can run the same scan (`scan_instruments`), but only a person adds an instrument to the lab.

**Cards included** (written from the manuals; marked `community` until a lab verifies them on hardware):

| Kind | Models |
|---|---|
| Source-meters and sources | Keithley 2400-series, Keithley 2450/2460/2461/2470, Keysight B2900, Yokogawa GS200/GS210 |
| Lock-ins | SRS SR830/SR810, SRS SR860/SR865, Zurich Instruments MFLI (through LabOne) |
| Temperature controllers (cryostats) | Lake Shore 335/336, Lake Shore 350, Oxford Instruments Mercury iTC |
| Cryostat systems | Quantum Design PPMS, DynaCool, VersaLab, MPMS3 (through MultiVu), attocube attoDRY1100/2100/800 (through attoDRYLib, draft) |
| Magnet supplies | Lake Shore 625, AMI 430 |
| Lasers and optics | Coherent OBIS, Thorlabs ITC4000 laser/TEC controller, Thorlabs PM100/PM400 power meter |
| Amplifiers | SRS SR560 voltage preamp, SRS SR570 current preamp (listen-only: Kelvin shows what it last set) |
| Meters and generators | Keysight/Agilent 344xxA DMMs, Keithley 2182A nanovoltmeter, Keysight 33500B/33600A generators |
| Microwave and RF sources | SRS SG384/SG386, Rohde & Schwarz SMB100A, Keysight N5183B MXG, Windfreak SynthHD (draft) |
| Network analyzers | HP/Agilent 8719, 8720 and 8722 (D and ES), Keysight PNA family (PNA, PNA-L, PNA-X, E5080, P50xx, M98xx): S11, S21, S12, S22 in dB and S21's phase, one sweep per reading |
| AOM and EOM drivers | MOGLabs XRF/ARF/QRF agile RF drivers, Novatech 409B DDS (an EOM is usually driven by one of the RF sources above) |
| Pulse and delay generators, AWGs | Swabian Pulse Streamer 8/2, SpinCore PulseBlaster, SRS DG645, Rigol DG1000Z, RFSoC boards running QICK |
| Photon counting | Swabian Time Tagger (Time Tagger 20, Ultra, X) |
| Networked lasers | M Squared SolsTiS, Light Conversion PHAROS, TOPTICA DLC pro (draft) |

Real hardware gets the same gate as the virtual lab, and a few protections of its own:
- **Measuring never powers anything up.** On some source-meters a reading command switches the output on, so those readings are only taken while the output is already on (`read_when`).
- **No speed-up on real time.** A lab with any real instrument runs on real time, so ramps keep their rates.
- **Risky commands stay locked.** Commands that change what readings mean, or that bypass the instrument's own protection, are blocked in the card: sensor curves, coil constants, persistent-switch heaters, current limits, and DMM resistance modes that push current into a device.
- **Connecting changes nothing.** Kelvin leaves every instrument as it finds it: no reset, no default values. A card's `on_connect` commands may only set up the connection itself (listen mode, headers, a driver's wiring); a card whose `on_connect` would set any parameter is refused. A drive with no read-back (an RF switch, a DDS amplitude) is unknown until Kelvin sets it: switching it to its safe value always goes ahead, anything else needs approval, and a ramped one first goes to safe.

**Instruments behind vendor software.** Some instruments are not driven with text commands but through their maker's library. Kelvin has drivers for two of them; the address says which:
- `zhinst://dev1234` for a Zurich Instruments lock-in, through LabOne's Python API (`zhinst-core`, which Kelvin installs the first time). Add `?host=...&port=8004&interface=1GbE` if the data server runs elsewhere. A scan finds these devices through LabOne.
- `qd://<computer>:5000` for a Quantum Design system, through MultiPyVu (Kelvin installs it; its server runs next to MultiVu). MultiVu ramps temperature and field itself; Kelvin sets the rate and setpoint, then waits for the reading.

More instruments with a library of their own:
- `pulsestreamer://<ip>` for a Swabian Pulse Streamer (`pulsestreamer`), `pulseblaster://0?clock=500` for a SpinCore PulseBlaster (SpinCore's SpinAPI, and the `spinapi` wrapper), and `timetagger://<serial>?click=1&gate=2` for a Swabian Time Tagger (`Swabian-TimeTagger`, and Swabian's installer for its USB driver). A scan finds the Swabian devices. Kelvin installs the Python packages they need, once you approve; the makers' installers are yours to run (see [What an instrument needs on this computer](#what-an-instrument-needs-on-this-computer)).
- `qick://<name server>:8888/<proxy name>` for an RFSoC board running QICK, through its Pyro4 server, e.g. `qick://192.168.3.1:8888/myqick` for an RFSoC 4x2 on its USB network.
  - **Firmware:** tProc v1 or v2. Kelvin reads which from the board and runs programs written for it; the gain stays a fraction of full scale either way (on v1 Kelvin converts it to DAC units, and durations to clock cycles). For firmware it doesn't recognise, set `vars: {tproc: "1"}` (or `"2"`) on the instrument in the lab config.
  - **Starting the server:** on a PYNQ board, in its Jupyter (`http://<board>:9090`), run `qick/pyro4/00_nameserver.ipynb`, then `01_server.ipynb`, whose proxy name goes in the address. Kelvin can run them itself (`run_on_board`), once you approve the notebooks' code; it also reads the board's QICK version that way. PYNQ's default Jupyter password is `xilinx`.
  - **Boards without Jupyter** (a Red Pitaya, a Raspberry Pi controller): `run_on_board` runs a shell command over SSH, `ssh://root@<board>`, once you approve it; this computer's SSH key signs in, or a password. The board's host key is kept the first time and checked after. Kelvin installs paramiko for this once you approve.
  - **The client:** `qick` and `Pyro4` on the computer that runs Kelvin, which Kelvin installs (the app does not carry them). The board's QICK and this computer's should be the same version: the instrument's identity shows both, and Kelvin installs the board's.
  - **What Kelvin does:** it plays one generator as a continuous tone or as pulses, and reads I/Q from one readout. A tone keeps playing after a program ends, so Kelvin stops every generator (`reset_gens`) when the output goes off and when it disconnects.

**DAQ cards, spectrometers, monochromators, cameras and nanopositioners.**

| Instrument | Address | What the computer needs | Card |
|---|---|---|---|
| National Instruments DAQ (USB-600x, USB-62xx, PCIe/PXIe-63xx, cDAQ) | `nidaq://Dev1?terminal=rse` | NI-DAQmx (NI's installer), and `nidaqmx`, which Kelvin installs | `ni/daqmx` |
| Teledyne Princeton Instruments camera (PyLoN, PIXIS, ProEM, BLAZE, KURO, SOPHIA) | `picam://<serial>` | PICam, or LightField which installs it | `princeton/picam` |
| The same cameras with their spectrograph (IsoPlane, SpectraPro, HRS), through LightField | `lightfield://<experiment>` | LightField, with its license | `princeton/lightfield` (Kelvin's lab status says so when LightField is installed and not in the lab yet: Kelvin drives it, never its window) |
| Princeton Instruments (Acton) SpectraPro monochromator | `serial://COM4` | nothing (text commands) | `princeton/spectrapro` |
| Andor Shamrock / Kymera spectrograph | `shamrock://0` | Andor Solis or the Andor SDK | `andor/shamrock` |
| Andor sCMOS camera (Zyla, Neo, Sona, Marana) | `andor://0?sdk=3` | Andor SDK3 and its pyAndorSDK3 | `andor/scmos` (draft) |
| Andor CCD / EMCCD camera (iXon, Newton, iDus) | `andor://0?sdk=2` | Andor SDK2 and its pyAndorSDK2 | `andor/ccd` (draft) |
| attocube ANC300 (open loop, scanner offset) | `anc300://COM5` or `anc300://<ip>` | nothing (its command line) | `attocube/anc300` |
| PI piezo scanner controllers (GCS: E-727, E-709, E-712...) | `TCPIP0::<ip>::50000::SOCKET` | nothing (GCS text commands) | `pi/gcs-piezo` |
| Thorlabs MDT693A / MDT694A piezo controller | `serial://COM6` | nothing (text commands) | `thorlabs/mdt69xa` |
| attocube ANC350 (closed loop) | `anc350://0` | attocube's USB driver, and `pylablib`, which Kelvin installs | `attocube/anc350` |
| Ocean Insight spectrometer | `seabreeze://<serial>` | `seabreeze`, which Kelvin installs, and its USB driver once | `ocean/spectrometer` |
| Basler camera | `pylon://<serial>` | `pypylon`, which Kelvin installs | `basler/camera` |
| USB webcam, microscope or inspection camera (UVC) | `uvc://0` | `opencv-python-headless`, which Kelvin installs | `generic/usb-camera` |
| GenICam industrial camera (Teledyne FLIR, Allied Vision, IDS, The Imaging Source, Lucid) | `genicam://<serial>?cti=<the maker's .cti file>` | the maker's SDK, and `harvesters`, which Kelvin installs | `generic/genicam-camera` |

- **The app brings no maker's library:** Kelvin installs the Python package an instrument needs (nidaqmx, seabreeze, pypylon, pylablib) on this computer the first time. LightField's automation uses pythonnet, which the app has for its own window. PICam, Andor's spectrograph library and the Andor SDKs are the makers' own installs, which Kelvin calls directly. The ANC300, SpectraPro, PI and Thorlabs controllers speak text.

- **Frames as pictures:** a camera's frame is kept as a file (in its run's run.h5 once the run ends). `look_at` shows Kelvin one as a picture, from a run or read now, with its numbers (size, range, the brightest pixel, where the bright part is centered), so a model that sees can judge a spot, a beam or an image by eye.

- **Scans find them:** the NI devices, Princeton Instruments cameras, Andor spectrographs and sCMOS cameras (and, once their packages are installed, Ocean spectrometers, Basler cameras and ANC350s), through their libraries, which only list what they see. Add the others by address. Andor SDK2 cameras aren't listed, because finding one means initializing it, which resets it.
- **The scan says what PICam saw:** each camera that is free, each one open in another program (LightField, say), PICam missing, and whether LightField is installed. Kelvin never starts LightField by itself: add it in the Lab panel.
- **Through LightField:** Kelvin starts LightField, a window you can watch and use, and loads the experiment saved there. It sets exposure, frames (averaged per reading), the grating (as LightField names it, or by grooves per mm), the center wavelength and the sensor setpoint, and waits while the spectrograph moves. Each reading captures without saving a file. A spectrum comes with LightField's wavelength calibration, for the frame's own columns and binning. One program per camera: close a LightField you opened yourself first, and don't add the same camera through PICam as well.
- **PICam's errors in words:** a failure names what Kelvin was doing and PICam's own name for the error, such as "PICam couldn't open the camera: Device Open Elsewhere", with what helps.
- **Connecting changes nothing,** as for every instrument. The exceptions are in the SDKs themselves, and the cards say so. Andor's SDK2 starts each session from its own defaults. PICam keeps a camera's settings on the computer, starting from its own values when a program opens the camera, and the first acquisition sends them.
- **Camera frames are images in the run's data.** A frame is a reading of type `image`. While a run is measured, each frame waits as a `.npy` file in the run's `frames/` folder; when the run ends, they go into its `run.h5` (`frames/<reading>`, one image per point, compressed). Mean, max, min and sum are readings of a frame: one taken just before is used, otherwise a new one.
- **Spectra keep their wavelengths.** A spectrum is recorded with the wavelengths it was taken at (they aren't evenly spaced), and `run.h5` keeps them with it (`traces/<reading>.x`).
- **Taken only when asked:** readings that run an acquisition (a frame, its statistics, a spectrum's peak, a DAQ counter) are marked `acquires: true`. The Lab panel and the conditions recorded with each run leave them out; plans and commands take them.
- **One axis per lab entry** for the positioners and PI controllers, with `vars: { axis: ... }`. Entries on the same attocube controller share one connection.
- **High voltages are drives:** an ANC300's DC offset and an MDT69x's axis voltages rise only with approval, ramped, and go back to 0 V as their safe value. Positions (ANC350, PI) are like a stage: part of the measurement, inside the limits the lab sets.
- **Outputs without read-back:** a DAQ's analog and digital outputs, a spectrometer's integration time. These are unknown until Kelvin sets them, and nothing is assumed. For outputs: switching to safe always goes ahead, any other value asks you, and a ramped one starts from safe.
- **Not yet verified on hardware:** the Andor camera cards are drafts, written from Andor's SDK functions without a camera at hand. Check them against Solis, then mark them reviewed. The others follow their makers' documentation, but haven't met real hardware yet either, so watch the first runs:
  - the Acton commands are from its manual;
  - the PICam parameters, the Shamrock calls and the ANC300 commands are from pylablib's open-source drivers;
  - the LightField calls are from Teledyne's automation interface as labs' own scripts use it.
- **For a model that isn't here** (a Newport Cornerstone, a Horiba monochromator, a Hamamatsu or Thorlabs camera, a SmarAct or Mad City Labs stage): Kelvin writes a card from its manual or the maker's example scripts (see [Cards for new instruments](#cards-for-new-instruments)).

Their cards use the same command templates (`SET <path> {value}`, `GET <path>`), so limits, ramps, approvals and the simulated twins work as for any instrument. The vendor libraries are not part of the desktop app: Kelvin installs the one an instrument needs, once you approve (from source, the extras `zhinst`, `qd`, `nidaq`, `ocean`, `basler` and `pylablib` install them). Andor's pyAndorSDK2/3 come with Andor's SDK, and Kelvin installs them from its Python folder. These cards and drivers follow the vendors' documentation and haven't been verified on hardware yet, so tighten the limits and watch the first runs.

**Instruments on the network with their own protocol.** No vendor software is needed; the address says how to talk:
- `http://<ip>:<port>/<base>` (or `https://`) for an instrument with a web API, such as a Light Conversion PHAROS (`http://<ip>:20022/v1`). Card commands are the method, the path and a JSON body: `GET /Basic/ActualOutputPower`, `PUT /Basic/TargetAttenuatorPercentage {value}`. Add `?auth_env=NAME` for a bearer token kept in that environment variable, or `?insecure=1` for a self-signed certificate.
- `m2://<ip>:<port>` for an M Squared laser (SolsTiS and other ICE-BLOC controllers): JSON over TCP. On the laser's web page, under Remote interface, enter this computer's IP address and the port. Kelvin starts tuning with the target wavelength and follows it with the tuning status and the wavelength reading. The protocol has no emission switch, so the pump and the shutter keep their own control.
- `toptica://<ip>` for a TOPTICA DLC pro, through its command line (TCP port 1998). The card is a draft until someone checks the current's unit on a real controller.
- `modbus://<ip>?unit=1` for an instrument that speaks Modbus TCP, and `modbus://COM5?unit=1&baud=9600&parity=N` for Modbus RTU on a serial line: compressors (such as Cryomech's), temperature controllers (Eurotherm, Omega, Watlow), chillers, flow and vacuum controllers, PLCs. Kelvin speaks Modbus itself. Card commands come from the register map in the manual: `READ 30008 float32` (input register 30008 in the usual numbering), `READ HR 6 int16 *0.1` (a holding register counted from 0, in tenths), `WRITE 40001 int16 *0.1 {value}`, `WRITE COIL 3 {value}`; add `swapped` when the device sends the low word first. A device's refusal comes back in words (Modbus exception 2, illegal data address).

What Kelvin does not send: the PHAROS's start, standby and preset actions, its preset edits and interlock resets; the SolsTiS's raw hardware outputs, profiles and TeraScan; the DLC pro's exec commands and current clip. The PHAROS and the SolsTiS have no login of their own, so keep them on the lab network.

**Names that differ between installations.** A card can declare variables, such as a Mercury iTC's sensor UID or a lock-in's demodulator. Set them per instrument with `vars`. The draft lab config lists them with their defaults so you can check them, and values may only contain letters, digits and `. _ + -`, so nothing can be slipped into a command.

A lab can mix real and simulated instruments, and override a card's connection settings per instrument:

```yaml
lab: Cryostat 2
instruments:
  smu:    { card: keithley/2400, address: "GPIB0::24::INSTR", limits: { V: [-5, 5] }, rates: { V: 0.2 },
            guards: { V: [{ read: I, max_abs: 1.0e-9 }] } }
  lockin: { card: srs/sr830, address: "serial://COM3", connection: { read_termination: "\r" } }
  tc:     { card: lakeshore/336, address: "ASRL5::INSTR" }
  laser:  { card: coherent/obis, address: "serial://COM7", auto: never }
  itc:    { card: oxford/mercury-itc, address: "TCPIP0::10.0.0.9::7020::SOCKET", vars: { sensor: DB6.T1 } }
  li:     { card: zhinst/mfli, address: "zhinst://dev5678", limits: { amplitude: [0, 0.5] } }
```

## The instrument collection

Kelvin's public repository, [Houchen181/Kelvin](https://github.com/Houchen181/Kelvin), holds its releases and an instrument collection: cards, where each maker publishes its manuals, experiments with plans, and lab configs for common setups. Kelvin keeps a copy in `~/Kelvin/collection/` and works from it, offline too.

- **From the public repository only.** Kelvin asks GitHub for the repository first. It refuses one that GitHub reports as private, so it never reads the private source, and it follows a rename to the repository's new name.
- **Kept up to date, and checked.** Soon after Kelvin starts, then twice a day, it asks GitHub for the newest commit and takes `index.json` and every card, experiment and lab config from that commit (`raw.githubusercontent.com`), so nothing can change halfway. Every file must have the SHA-256 that `index.json` gives it, when it arrives and each time Kelvin reads it. If anything is missing or doesn't match, or GitHub can't be reached, the copy Kelvin has stays in use. A file this computer can't keep (a disk error) leaves out only that file's card, and Settings lists it; paths beyond Windows' 260 characters are fine. When an update starts failing, or leaves files out, the portal says so once in a notice. Only files that changed are fetched, and Kelvin only downloads: nothing about your lab goes to GitHub. **Update now** in Settings → Instruments looks at once; Settings also shows which commit Kelvin has and what it holds.
- **Before Kelvin writes a card.** Setting up instruments it found, Kelvin first brings the collection up to date, so an instrument the collection has a card for gets that card, and one it only knows gets its card written from the manual the collection links. At a first start the copy may not have arrived yet otherwise.
- **Finding a card.** In a chat, `find_card` lists the cards Kelvin has for a maker and model, a kind, an identity reply or a driver address (Kelvin's own, the collection's and your lab's), and the instruments the collection knows without a card, with their manuals. Kelvin uses a card it has rather than drafting another: `add_instrument` takes the card a name means (`AMI 430`) or a driver address calls for (`qd://...` is the PPMS's `qd/multivu`), `draft_card` refuses a model Kelvin has a card for, and `save_card` takes the id of one of Kelvin's cards only as your lab's version of it (`version: true`).
- **Which card Kelvin uses.** For each instrument model, the first of:
  1. your card in `~/Kelvin/cards`, if a person reviewed it on this computer (marked it reviewed, or saved it as community or lab-verified). Nothing replaces it;
  2. the collection's card: a fix of one of Kelvin's own cards, a lab's check of it on hardware, or a card Kelvin doesn't bring. A collection card that is a copy of an older version of Kelvin's own card never replaces the newer one Kelvin brings: Kelvin knows every version of its cards (`card_versions.json`);
  3. Kelvin's own card;
  4. your draft. The Cards dialog says when Kelvin uses its own card instead.

  A collection card keeps its status, so a draft stays simulation-only on real hardware. A card for a model the lab has no card for can be used at once; a newer card for one the lab uses takes effect when the lab is next connected. Cards that the collection's `kic install` copied into `~/Kelvin/cards` count as the collection's while nobody has changed them, so its newer versions replace them.
- **Instruments without a card.** When a scan gets an identity reply that no card matches, Kelvin looks it up in the collection, which names the instrument (*Keithley 6221 AC and DC current source* rather than its reply). To write its card, Kelvin starts from the public manual the collection links, and asks your model where the maker publishes it only if that is not a command reference. The draft gets the collection's id, name and kind, so a card the collection adds for that model later replaces the draft, unless you reviewed it.
- **Experiments.** In a chat, Kelvin finds the collection's experiments with `experiment_guide`: what each needs (its roles), setup, procedure, analysis, pitfalls and plans. The plans are written for one of the collection's lab configs. Kelvin renames their instruments to yours (the instrument with the same card, otherwise one of a kind the role takes), says which have no match in your lab, and lists parameters your cards don't have. They are a starting point: every plan goes through the same checks and approvals as any other.
- **Switching it off.** Settings → Instruments → **Use Kelvin's instrument collection**. When it is off, Kelvin doesn't contact GitHub for it; scans and `experiment_guide` stop using it at once, and from the next time the lab is connected Kelvin uses only its own cards and yours. The copy stays on disk, for when it is switched on again.

## Spin experiments (NV centers)

Kelvin builds the pulse sequences for spin experiments itself, checks them, and plays them on a pulse generator:

| Protocol | What it measures |
|---|---|
| `cw` | CW ODMR: the laser on, the microwave on for half of each period, signal and reference windows |
| `pulsed_odmr` | the resonance with a π pulse (`t_pi`), narrower than CW |
| `rabi` | a microwave pulse of length `tau`: the oscillation gives the π time |
| `ramsey` | π/2, free evolution `tau`, π/2: T2* and detuning |
| `hahn_echo` | π/2, π, π/2 with `tau` on each side: T2 |
| `t1` | dark time `tau` after initialization: T1 |

Each sequence has three channels: the laser (through the AOM), the microwave (a switch, or the source's pulse-modulation input) and the counter's gate, with a signal and a reference window. Before anything plays, Kelvin checks the timing against the generator's resolution, looks for overlapping windows, and keeps the microwave duty cycle below `max_mw_duty` (0.5 by default) so the sample doesn't heat. `show_sequence` draws a sequence before it runs. Starting a sequence (`running`) counts as a drive, so a person approves it.

The usual order is CW ODMR to find the resonance at a low microwave power, then Rabi for the π time, then pulsed ODMR, Ramsey, echo or T1. Kelvin fits the Rabi oscillation and reports the π time.

The virtual lab has an NV center, a pulse generator, a microwave source and a photon counter to try this on. A real setup names which output goes where, in the pulse generator's `vars`:

```yaml
lab: NV setup
instruments:
  pg:      { card: swabian/pulse-streamer, address: "pulsestreamer://169.254.8.2",
             vars: { laser: "0", mw: "1", gate: "2", laser_delay: "4.5e-7", max_mw_duty: "0.3" } }
  mw:      { card: srs/sg384, address: "TCPIP0::10.0.0.20::5025::SOCKET", limits: { power: [-60, 0] } }
  counter: { card: swabian/time-tagger, address: "timetagger://1234000ABC?click=1&gate=2" }
  aom:     { card: moglabs/xrf, address: "TCPIP0::10.0.0.30::7802::SOCKET", vars: { ch: "1" }, limits: { power: [-50, 30] } }
```

For pulsing the SG384 from the pulse generator, set its `mod_type` to pulse, `pulse_source` to external and `modulation` on. MOGLabs and Novatech drivers have no read-back for some settings, so Kelvin switches their RF off when it connects and remembers what it set.

## Cards for new instruments

**Kelvin writes the card itself.** When a scan finds an instrument that says who it is but matches no card, Kelvin writes one. It does this at start-up, and when you press **Find its manual and write its card** on that instrument in **Set up…**.
1. **It finds the manual.** If the [instrument collection](#the-instrument-collection) knows the model, Kelvin starts from the manual it links. Otherwise Kelvin searches the web for it, through your model service's own web search. It also tries the addresses your default model suggests, and known addresses for some makers. On a product page, Kelvin follows the links on the maker's own site that look most like this model's manual (naming the model or its series, PDFs, "manual" or "programming"), skipping the site's general pages, and from a page about the model's manuals on to its PDFs. A document counts only once Kelvin has downloaded it and its text names the model. A page that lists documents (a site's manuals for many models) is followed but never taken for the manual, and a brochure or data sheet that isn't a command reference is passed over. Of those left, the one with the most command syntax wins.
2. **It drafts the card.** The model writes the card from the manual's most useful pages, up to about 60,000 characters:
   - pages with this model's ranges;
   - pages on how to talk to it (serial settings, line ends, a handshake such as Pfeiffer's ACK and ENQ);
   - then the pages that define commands. Outputs, levels and readings come first (`OUTPut[:STATe]`, `VOLTage`, `SETP`), deep commands and housekeeping last, and the contents and the index not at all.

   The card checker reviews the card, and the model fixes what the checker found, once. What the model left out or is unsure of goes to you and into the card's first lines.
   - **Ranges in another document.** Some manuals leave an output's ranges to another document; Tektronix's programmer manuals send them to a specifications document. The model then keeps that output as a reading and says which document has its ranges. Kelvin looks for that document the same way and has the card written again with its pages on those outputs, so the output gets its limits. If Kelvin can't get it, it asks you for its PDF or web address, as for a manual.
   - **Instruments with their own handshake.** Pfeiffer's gauge controllers answer each command with ACK and send the data on ENQ. Their card says `enq: true`, and Kelvin speaks that protocol, passing over the readings they send by themselves from power-up.
3. **It makes the card safe to try, whatever the model wrote:**
   - The card is a draft. On real hardware every setting and output change asks you, saying the card is a draft.
   - No commands are sent on connect.
   - Reads that start something are left out (`MEASure?`, `READ?`, `INITiate`, `*TST?`, `*CAL?`), because on some instruments they switch an output on or run a self-test.
   - Calibration and firmware commands are blocked.
   - The identity pattern is checked against the instrument's own reply. For an instrument that gave none (you named it), the manual's own identity check is kept, and trying the card tests it.
4. **It sets the instrument up.** At start-up, the instrument joins the lab with the others, marked as added by Kelvin, and its folder gets the manual Kelvin downloaded. The card's first lines say where it came from and which model wrote it.

In a chat, Kelvin does the same for an instrument you name (`draft_card`): with the maker and model from its identity reply, or from what you said for one that doesn't answer `*IDN?`. Then it tries the card (`try_card`) and adds the instrument (`add_instrument`), and tells you what the card left out. Where the maker's site doesn't give the manual up, Kelvin searches the web for it (`web_search`), reads pages and PDFs (`read_web_page`), and keeps the manual (`save_manual`), which then goes into the instrument's folder. A manual or specifications document nobody publishes, you can give it (`draft_card` with `manual` or `specs`: a PDF on this computer or its web address).

What leaves the computer: the maker and model name, and the manual's command pages, go to your model; the manual's address goes to the maker's site. The serial number never leaves. Switch this off with **Write cards for instruments Kelvin doesn't know** in Settings. If Kelvin can't find the manual (some makers want a sign-in) or there is no model, it says so; then draft the card from a PDF yourself:

For an instrument Kelvin doesn't know, open **Lab → Set up → Make a card for it from its manual or example scripts…**.
- **Drafting:** paste the manual's remote-programming chapter (copy it from your PDF viewer), or load the maker's example scripts: **Load files** takes several at once, such as the Python or MATLAB examples, a DLL's header file and the README. Pick a model and press **Draft the card**. The model writes the card from that text only, and Kelvin checks it against the card format, asking the model to fix any problems once.
- **Trying:** type the instrument's address under the card and press **Try it**. Kelvin asks the instrument who it is and reads every reading, and shows each value or the error it gave. It changes nothing: to a text instrument it sends queries, and writes one setting (never an output) with the value it already has, then reads it back and reads the error queue, so a set command the instrument doesn't take shows now rather than at your first run. It skips reads that could start a measurement or a self-test, and lists them as not tried, never as checked. Compare the values with the front panel, fix the card and try again until they agree. An instrument already connected in the lab is asked through that connection.
- **Checking:** read the draft against the manual, edit it, press **Check**, then **Save card**. It is kept in `~/Kelvin/cards/` and appears in the card list for your lab config.
- **In a chat:** "here is the command reference of my magnet supply, make a card", or "the attoDRY examples are in C:\attocube\examples, make a card for it", or "the AOM's manual is in this folder". Kelvin reads them, manual PDFs included (`read_examples`; `save_manual` keeps the manual, given its file or its folder), writes the card (`card_guide`, `check_card`), tries it on an instrument in the lab or one the scan found (`try_card`, readings only; another address asks you first), fixes it with you, saves it (`save_card`) and adds the instrument (`add_instrument`).
- **Drafts are safe to try:** a new card is a **draft**. On real hardware its readings work, so you can try it, but every change to its settings or outputs asks you first, saying the card is a draft (nobody has yet checked which of its parameters only configure, or its limits).
- **Reviewing:** when you have checked the commands, limits and safe values, press **Mark reviewed** (**Lab** → **Cards…**, or **Review cards…** in the Lab panel's notice). Only a person can do that, and Kelvin never overwrites a card a person has reviewed. It counts at once, without reconnecting.
- **Your lab's version of a Kelvin card:** labs differ: another firmware, a model variant the card doesn't list, a magnet with a wider range, a different command. In **Lab** → **Cards…**, every card of Kelvin's that your lab uses has **Make your version**: edit it under the same id and **Save card**, and it replaces Kelvin's card in your lab only (Kelvin's stays for everyone else, and later Kelvins don't overwrite yours). Your version is listed with your cards, marked "your lab's version of Kelvin's card", with **Back to Kelvin's card** to undo it. Instruments use it once the lab reconnects. Kelvin can make one too, when one of its cards doesn't fit your instrument (`save_card` under the card's id); as with every card Kelvin writes, it is a draft, so every setting and output change asks you until you review it. For smaller differences, the lab config already sets per instrument its `limits` (inside the card's), `rates`, `auto` ranges, `guards`, `connection` settings and the card's `vars` (a sensor, a channel, a firmware choice); a limit beyond the card's own needs your version of the card.
- **Kelvin's own draft cards:** some cards that come with Kelvin are drafts too (the attoDRY, the Andor cameras, the Toptica DLC pro, the Windfreak SynthHD). When your lab uses one, it is listed with your cards, marked "comes with Kelvin", and you review it the same way. The review holds for your lab, kept in your cards folder (`_reviewed.json`); if a later Kelvin changes that card, it asks again until you review the new one.

**Instruments driven through the maker's library.** Many instruments come with example scripts that call the maker's Python module or DLL instead of sending text commands: cryostats such as the attoDRY, piezo controllers, cameras, spectrometers. A card for one of them has a `library` section that names the module or the DLL (with each function's C arguments, taken from its header file), the calls that connect and disconnect, and switches the library can only toggle. Each parameter calls one function: `get: "GET getSampleTemperature"`, `set: "SET setUserTemperature {value}"`. Its address says where the library is:

```yaml
cryo: { card: attocube/attodry, address: "lib://COM4?python=C:/Python38-32/python.exe&path=C:/attocube/attoDRYLib",
        vars: { device: "1" } }
```

- `COM4` is what the library connects to (`{address}` in the card's connect calls).
- `python` is a Python that can load the library: a 32-bit Python for a 32-bit DLL, or the Python that has the maker's package. Without it, Kelvin uses its own (64-bit).
- `path` is the folder with the maker's module or DLL.
- `env`, instead of both, is an environment Kelvin installed for the library, with its Python and the maker's package or files in it: `lib://COM4?env=attodry` (see [What an instrument needs on this computer](#what-an-instrument-needs-on-this-computer)). Kelvin writes and tries addresses like these itself; one with `python` or `path` names a program on this computer, so only you add it.

**Libraries with sub-objects.** Some libraries put each part of the instrument on a sub-object of the object you create. attocube's network instruments do this, for example the attoDRY2100 through its `atto_device` package:

```python
device = Device(ip)
device.connect()
device.sample.getTemperature()
```

Their card creates the object with the address (`object: Device`, `create: true`, `create_args: ["{address}"]`, `open: [connect]`, `close: [close]`) and calls each function by its path: `get: "GET sample.getTemperature"`. The address is then the instrument's IP: `lib://192.168.1.20?env=attodry`, with attocube's `atto_device` package installed in that environment (or `?path=<the folder that holds atto_device>`).

**.NET libraries and COM objects.** Many makers give a .NET library (Thorlabs Kinesis motion and piezo controllers, many stages and cameras) or, in older programs, a COM object. A .NET card names its assemblies and class, the static calls the examples make first, and the factory that makes the object:

```yaml
library:
  assembly: [Thorlabs.MotionControl.DeviceManagerCLI.dll, Thorlabs.MotionControl.KCube.DCServoCLI.dll]
  type: Thorlabs.MotionControl.KCube.DCServoCLI.KCubeDCServo
  setup: [Thorlabs.MotionControl.DeviceManagerCLI.DeviceManagerCLI.BuildDeviceList]
  create: CreateKCubeDCServo
  create_args: ["{address}"]
  properties: [Position]
  open: ["Connect {address}", "WaitForSettingsInitialized 10000", "StartPolling 250", "EnableDevice"]
  close: [StopPolling, Disconnect]
```

Its parameters read a property (`get: "GET Position"`), call a method (`set: "SET MoveTo {value} 60000"`; Kelvin passes the value as the Decimal the method takes, and an enum by its name) and set a property (`set: "SET Velocity= {value}"`). The address is the serial number and the folder the maker's installer put the assemblies in, `lib://27000001?path=C:/Program Files/Thorlabs/Kinesis`; Kelvin may add one in Program Files itself, through pythonnet, which the app has. A COM object is `library: {com: <its ProgID>}`, through pywin32, which Kelvin installs. Windows' and Microsoft's own COM objects, and the .NET platform's own namespaces, are refused. `library_api` lists an assembly's types, methods and properties, or a COM object's, to write the card from.

Kelvin runs the library in a helper process, so a crash or a 32-bit DLL never takes Kelvin down. The helper loads only the module or DLL the card names, from that folder (or an installed package of the maker's), never part of Python itself or a system DLL. It calls only the functions on the card, and only the maker's own: a path goes only through objects the maker's package made, never into Python's modules or objects, such as a socket the library holds. Readings must call functions that only read (`get…`, `is…`, `query…`), and a switch that only toggles is read before it is toggled. The included attoDRY card is a draft written from attocube's header (attoDRYLib, for the attoDRY's USB connection): try it, compare it with the touch screen, and mark it reviewed when it agrees.

## What an instrument needs on this computer

The app carries no instrument maker's software: every lab has different instruments. When one needs its maker's Python package (QICK, LabOne, MultiPyVu, a camera's SDK) or the maker's files (a DLL, an SDK's Python folder), Kelvin installs them on this computer, once you approve, in an environment of its own:
- **What the chat shows, and asks:** a download onto your computer is yours to say yes to, so Kelvin asks you first (the Lab panel's **Install** button is that yes already): each package with its version and where it comes from (PyPI, with the project's summary and home page), the maker's files and their source, and the first time uv, the installer Kelvin uses, from its GitHub release (checked against its published checksum). Nothing is installed system-wide, and no administrator rights are needed.
- **Where it goes:** `~/Kelvin/env/<name>`, each with its own Python. Kelvin's built-in drivers use the `drivers` environment, which has the app's own Python version, so the app loads from it. A library card uses the environment its address names, `lib://<address>?env=<name>`, with the Python its library needs: a 32-bit one for a 32-bit DLL, an older one where the maker's package wants it.
- **Versions that must match:** some instruments need the same version on both sides, such as a QICK board and this computer's `qick`. Kelvin installs the version the instrument has.
- **The makers' installers stay yours:** a USB driver or a runtime that needs administrator rights (NI-DAQmx, the Opal Kelly driver, LightField) comes from the maker's installer, which you run. Kelvin says which one, and where it is.
- **Cards say what they need:** a card's `requires` lists the maker's Python packages its driver imports, such as `requires: [qick, Pyro4]`.
- **When your lab can't connect** because an instrument's package is missing, the Lab panel says so with an **Install** button: you approve the download, and Kelvin connects the lab again.
- **Scans say what is missing:** when a maker's program is on this computer (NI-DAQmx, LabOne, MultiVu, Basler's pylon, Swabian's Time Tagger software, SpinAPI) but not the package Kelvin reaches its instruments through, the scan says so, and how Kelvin installs it.
- **Seeing and removing them:** Settings, Instruments, lists what Kelvin installed, each with its packages, size and what it was for, and a Remove button. In a chat, ask Kelvin (`environments`), or to remove one (`remove_environment`, which you approve).
- **Searching outside the lab** (Settings) also covers these downloads: with it off, or in a project that keeps its work off the internet, Kelvin downloads nothing.

**Finding out what an instrument is.** When a scan doesn't find an instrument, or finds it but no card matches, Kelvin looks further by itself:
- `inspect_address`, for an address on the lab's network: which of the ports instruments use answer, and what they say (an `*IDN?` reply where it speaks SCPI, a web page's title, its LXI identification, a Jupyter or Pyro server on a board). It looks only at local networks, and leaves an instrument already in the lab connected as it is.
- `inspect_serial`, for an instrument on a serial port the scan didn't identify: what it sends by itself (some stream their readings), then its answer to an identity query at each speed (1200 to 115200 baud) and framing (8N1, 7O1, 7E1), with the settings to add it with. It sends nothing else, and leaves a port an instrument of the lab uses alone.
- `connected_devices`, for what is plugged in: every USB device with the name it reports, its maker and its driver (or that it has none), the serial ports, and the networks this computer is on (a board on USB networking makes one).
- `installed_software` and `find_maker_files`, for the makers' programs, drivers, examples, SDK folders and manuals on this computer.

Then it reads the manual and the maker's examples (on this computer or the web), installs what the instrument needs, writes and tries its card, and adds it.

## Writing a card

```yaml
card: 1
id: acme/sm-2000
model: ACME SM-2000 Source-Meter
kind: source-meter
status: draft                 # on real hardware, every change on a draft card asks a person
manual: "ACME SM-2000 Reference Manual, SCPI commands"
manual_url: "https://www.acme.example/manuals/sm-2000.pdf"   # the maker's own copy, fetched once per instrument
identify: { idn: "^ACME,SM-2000" }
error_query: "SYSTem:ERRor?"
parameters:
  V:
    label: Voltage            # what the lab panel shows
    kind: drive
    unit: V
    set: "SOURce:VOLTage[:LEVel] {value}"
    get: "SOURce:VOLTage[:LEVel]?"
    limits: [-200, 200]       # vendor limits; the lab sets tighter ones
    safe: 0
    ramp: gate
    max_rate: 10              # V/s
    applies_when: output      # only reaches the sample while the output is on ("armed" otherwise)
  output:
    label: Output
    kind: drive
    type: bool
    set: "OUTPut {value}"
    get: "OUTPut?"
    safe: off
    enables: [V]
  I:
    label: Current
    kind: measure
    unit: A
    get: "MEASure:CURRent?"
display: { headline: I }      # the reading shown large in the lab panel
raw:
  blocked: ["CALibration", "SYSTem:FIRMware"]
  reset: ["*RST"]
```

More that a card can say:
- **A source's range and current limit,** on its level, so a mounted device's protection can cap the instrument itself ([safety.md](safety.md#protecting-the-sample)): `source_range: { param: V_range, auto: V_autorange, steps: [0.2, 2, 20, 200] }` names its range setting, its autorange switch and the ranges it has, and `current_limit: compliance_I` its compliance. Kelvin changes the range only while the level is at safe.
- **`type: image`:** a camera frame, kept as a file (its driver answers with the file's path).
- **`axis: { values: wavelengths }`:** a trace's x values are another trace, recorded with it. `axis: { start: f_start, stop: f_stop }` is for evenly spaced ones.
- **Acquisitions and triggers:** a multimeter's buffer, a scope's single shot or an analyzer's sweep is a reading that arms, triggers and waits before it fetches: `get: ["INIT", "*TRG", "*OPC?", "TRAC:DATA?"]` with `acquires: true` and `wait: 20` (seconds). Kelvin sends them in one go, with that time for `*OPC?` to answer; a stop ends the wait within a second. Those commands only arm, trigger and wait; a setting such as the trigger source is a parameter of its own.
- **Network analyzers' traces:** an S-parameter is a trace of the formatted data in dB, taken by one sweep in the reading, with the text format sent first so a binary format another program left can't break it: `get: ["FORMat:DATA ASCii,0", ..., "*OPC?", "CALCulate1:MEASure1:DATA:FDATA?"]` (Kelvin's PNA card). A reply with two numbers per point (complex data, real and imaginary; HP's formatted data, the value and one that means nothing) says `pairs: true`, and `y` can use `re` and `im`, e.g. `y: "20*log10(hypot(re, im))"` for dB; without `y`, the first of each pair. A trace sent a line per point (HP's 87xx and 8753) is read whole on GPIB, USB and LAN instruments, to the end of the message.
- **Values that run into the mnemonic** (`set: "SOUP{value}"` with `bool_text: ["ON", "OFF"]` sends `SOUPON`, as HP's analyzers take it): a raw `SOUPON` counts as that setting, so it goes through the gate like any other.
- **Actions:** named commands with no value, such as `actions: {trigger: {send: "*TRG"}, fire: {send: ":BURS:FIRE", label: Fire a burst, drives: true}}`. Kelvin does one with `do_action`; one that drives the sample asks you first. A plan does them at every point, after settling and before measuring, with `trigger: [gen.fire]`, and asks you once for the run.
- **Waveforms from oscilloscopes:** `axis: { origin: xorig, step: xinc }` gives each point's time from the first point's time and the step between points, as scopes report them (`:WAV:XORigin?`, `:WAV:XINCrement?`). A trace whose reply starts with an IEEE 488.2 block header (`#800012345...`) is read as it comes. One sent as binary numbers (BYTE or WORD format) says `binary: uint8` or `binary: int16` (`int16 little` when the lowest byte comes first). `y: "(raw - yref) * yinc + yorig"` turns the digitizer's levels into volts with the instrument's own scale readings, as its manual gives the formula; it may use the card's readable parameters, numbers and functions such as `clip`.
- **A maker's Python package without example scripts:** `library_api` lists its classes and functions with their arguments and documentation, read in Kelvin's helper process without calling anything, so a library card can be written from it.
- **`acquires: true`:** the reading runs an acquisition, so Kelvin takes it only when asked or measured.
- **`connection: { echo: true }`:** the instrument echoes each command as a line, which Kelvin reads and drops before the answer.
- **Replies to set commands:** `set_reply: "^OK$"` when only set commands get a reply (an Arduino's `OK`, Oxford Mercury's `...:VALID`): Kelvin reads it after each set and checks it. `ack: OK` is for an instrument that sends that line after every command, queries too (Coherent's OBIS sends `OK` after each answer). A card with `ack` on an instrument that acks only its sets times out on every reading; the error says to use `set_reply`.
- **Greetings:** `greeting_lines: 2` when the instrument sends lines by itself as a connection opens, as the AMI 430 does on its LAN port. Kelvin reads and drops them before the first command; without that, every reply would be taken for the command before. One that sends none (the same 430 on RS-232) costs a short wait when it connects.
- **Serial framing as the manual writes it:** `parity: N` (or `none`, `odd`, `even`, `mark`, `space`, and their letters), or all three at once with `framing: 8N1`, also in a lab config's `connection`. Anything else is refused when the card is checked.
- **An error reply that means no error:** a reply starting with 0 (`0,"No error"`, `0000`) means no error. For any other, `no_error` is a pattern for it, e.g. `error_query: 1TE` with `no_error: "^1TE@$"` for Newport's SMC100.
- **Optional nodes** in brackets (`:SYSTem:ERRor[:NEXT]?`) are left out wherever Kelvin sends a card's command: parameters, health checks, the identity query, actions, a reading's arming commands and `on_connect`. An instrument takes the brackets for an unknown header.
- **Settings with no read-back** (a set command and no get): Kelvin remembers what it set, shows the value as unknown until then, and checks the error queue after each set.
- **Commands that aren't SCPI-shaped** (`?NM`, `%`, `{value} NM/MIN`): matched as written, ignoring case.
