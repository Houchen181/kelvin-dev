# Instruments

How Kelvin finds and talks to real instruments, what it keeps about each one, and how to add an instrument it doesn't know yet.

## Connecting instruments

Kelvin talks to instruments over VISA: GPIB, USB-TMC, LAN and RS-232 / USB-serial. It uses the VISA library you already have (NI-VISA or Keysight IO Libraries), or a pure-Python one if there is none. Install the extra with `pip install "kelvin-lab[hardware]"`; the desktop app includes it.

**Kelvin sets up what it finds.** The first time the desktop app starts (and on every start until you have a lab of your own), Kelvin asks each connected instrument who it is. If any match a card, it connects them right away:
- **Asking until you review the limits.** Kelvin writes `~/Kelvin/lab.yaml` with the instruments it found, each marked `added_by: kelvin`. Readings and measurement settings work at once. Their limits are the maker's maximums, so until you check them in **Set up…** and press **Limits reviewed**, Kelvin asks you before every output change on them (voltage, current, field, temperature, laser power), with a line saying why, and neither auto mode nor a project envelope covers them. **Limits reviewed** takes the marks out of the lab config; nothing is reconnected. Labs from older versions marked `observe_only: true` work the same way.
- **Instruments you name.** Say "the Keithley 2450 on GPIB 18 is connected" or "use the lock-in I just plugged in" in a chat, and Kelvin sets it up itself: it scans, picks the matching card (or finds the maker's manual online and writes a draft card, then tries it), adds the instrument to your lab marked `added_by: kelvin`, connects just that one (the others stay connected as they are), and tells you. It asks you first only for an address the scan didn't find, or to move from the virtual lab you chose to your instruments. It stops and tells you what it needs when it can't go on by itself: a setting on the instrument (remote control, its address, a list of allowed computers), the maker's software or driver, or a manual that isn't public (choose its PDF).
- **Switched-off instruments.** An instrument you switched off in the Lab panel stays off. When Kelvin needs it, it asks you to switch it on and says why; it is switched on only if you say yes.
- **A folder for each instrument** in `~/Kelvin/instruments/<name>/`:
  - `instrument.json`: who it is (maker, model, serial number and firmware from its identity reply), its address, the connection settings it answered with (such as 57600 baud, 7 data bits, odd parity), and where its manual came from.
  - `settings.json`: every setting and reading it had (ranges, time constants, sensitivities...), refreshed on every start.
  - `manual.pdf`: the maker's own manual, fetched once from the address on the card, and `manual.txt` with its text by page.
- **Manuals for involved scans.** Before a scan that uses buffers, triggers, sweep or list modes, fast acquisition, ranges or filters, Kelvin reads the instrument's manual with `instrument_info` and `search_manual`. It works from the passages it finds and cites the page. Search weighs rare words most, so a mnemonic such as `OFLT` or a SCPI path such as `SOURce:CURRent:LIMit` finds its own entry.
- **Manuals you add yourself.** 27 of the 37 cards carry the maker's link. Keysight hands out some manuals only after a sign-in, the Oxford Mercury iTC manual is no longer public, SpinCore's is behind its support pages, and M Squared, Light Conversion and TOPTICA give their protocol documents to owners. QICK's documentation is on its project site. For these, or for any other instrument, choose the PDF in **Set up…** (**Choose PDF…**) or put it in the folder as `manual.pdf`.
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
2. Review the drafted lab config, mainly the limits.
3. **Save and connect** saves it as `~/Kelvin/lab.yaml` and health-checks every instrument. It checks that each one is the model its card says, so a changed address can't send commands to the wrong instrument. It also reads every reading once and checks that drives sit inside your limits.

Kelvin opens on your lab from then on. You can switch back to the virtual lab at any time. If your lab can't be connected at start-up, Kelvin opens the virtual lab and says why. Kelvin only switches labs while it is idle, and approvals go back to asking.

From a terminal:

```bash
kelvin-scan --write lab.yaml
```

The scan only asks each instrument who it is (`*IDN?`, or the card's own identity query); it never sets anything. Serial ports are tried with each known card's framing; for example, the Lake Shore 336 needs 57600 baud, 7 data bits and odd parity. The command matches each instrument to a card and drafts a lab config for you to review. The agent can run the same scan (`scan_instruments`), but only a person adds an instrument to the lab.

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
- `zhinst://dev1234` for a Zurich Instruments lock-in, through LabOne's Python API (`zhinst-core`, which Kelvin installs the first time, once you approve). Add `?host=...&port=8004&interface=1GbE` if the data server runs elsewhere. A scan finds these devices through LabOne.
- `qd://<computer>:5000` for a Quantum Design system, through MultiPyVu (Kelvin installs it once you approve; its server runs next to MultiVu). MultiVu ramps temperature and field itself; Kelvin sets the rate and setpoint, then waits for the reading.

More instruments with a library of their own:
- `pulsestreamer://<ip>` for a Swabian Pulse Streamer (`pulsestreamer`), `pulseblaster://0?clock=500` for a SpinCore PulseBlaster (SpinCore's SpinAPI, and the `spinapi` wrapper), and `timetagger://<serial>?click=1&gate=2` for a Swabian Time Tagger (`Swabian-TimeTagger`, and Swabian's installer for its USB driver). A scan finds the Swabian devices. Kelvin installs the Python packages they need, once you approve; the makers' installers are yours to run (see [What an instrument needs on this computer](#what-an-instrument-needs-on-this-computer)).
- `qick://<name server>:8888/<proxy name>` for an RFSoC board running QICK, through its Pyro4 server (tProc v2 firmware), e.g. `qick://192.168.3.1:8888/myqick` for an RFSoC 4x2 on its USB network.
  - **Starting the server:** on a PYNQ board, in its Jupyter (`http://<board>:9090`), run `qick/pyro4/00_nameserver.ipynb`, then `01_server.ipynb`, whose proxy name goes in the address.
  - **The client:** `qick` and `Pyro4` on the computer that runs Kelvin, which Kelvin installs once you approve (the app does not carry them). The board's QICK and this computer's should be the same version: the instrument's identity shows both, and Kelvin installs the board's.
  - **What Kelvin does:** it plays one generator as a continuous tone or as pulses, and reads I/Q from one readout. A tone keeps playing after a program ends, so Kelvin stops every generator (`reset_gens`) when the output goes off and when it disconnects.

**DAQ cards, spectrometers, monochromators, cameras and nanopositioners.**

| Instrument | Address | What the computer needs | Card |
|---|---|---|---|
| National Instruments DAQ (USB-600x, USB-62xx, PCIe/PXIe-63xx, cDAQ) | `nidaq://Dev1?terminal=rse` | NI-DAQmx (the app brings nidaqmx) | `ni/daqmx` |
| Teledyne Princeton Instruments camera (PyLoN, PIXIS, ProEM, BLAZE, KURO, SOPHIA) | `picam://<serial>` | PICam, or LightField which installs it | `princeton/picam` |
| The same cameras with their spectrograph (IsoPlane, SpectraPro, HRS), through LightField | `lightfield://<experiment>` | LightField, with its license (the app brings pythonnet) | `princeton/lightfield` |
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

- **The app brings only what it needs:** nidaqmx, numpy, and pythonnet for LightField's automation. PICam, Andor's spectrograph library and the Andor SDKs are the makers' own installs, which Kelvin calls directly. The ANC300, SpectraPro, PI and Thorlabs controllers speak text. The last three rows need a maker's Python package the app doesn't carry: Kelvin installs it on this computer the first time, once you approve.

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

Their cards use the same command templates (`SET <path> {value}`, `GET <path>`), so limits, ramps, approvals and the simulated twins work as for any instrument. The vendor libraries are not part of the desktop app: Kelvin installs the one an instrument needs, once you approve (from source, the extras `zhinst`, `qd`, `nidaq`, `ocean`, `basler` and `pylablib` install them). Andor's pyAndorSDK2/3 come with Andor's SDK, and Kelvin installs them from its Python folder. The private app brings nidaqmx. These cards and drivers follow the vendors' documentation and haven't been verified on hardware yet, so tighten the limits and watch the first runs.

**Instruments on the network with their own protocol.** No vendor software is needed; the address says how to talk:
- `http://<ip>:<port>/<base>` (or `https://`) for an instrument with a web API, such as a Light Conversion PHAROS (`http://<ip>:20022/v1`). Card commands are the method, the path and a JSON body: `GET /Basic/ActualOutputPower`, `PUT /Basic/TargetAttenuatorPercentage {value}`. Add `?auth_env=NAME` for a bearer token kept in that environment variable, or `?insecure=1` for a self-signed certificate.
- `m2://<ip>:<port>` for an M Squared laser (SolsTiS and other ICE-BLOC controllers): JSON over TCP. On the laser's web page, under Remote interface, enter this computer's IP address and the port. Kelvin starts tuning with the target wavelength and follows it with the tuning status and the wavelength reading. The protocol has no emission switch, so the pump and the shutter keep their own control.
- `toptica://<ip>` for a TOPTICA DLC pro, through its command line (TCP port 1998). The card is a draft until someone checks the current's unit on a real controller.

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
1. **It finds the manual.** Kelvin searches the web for it, through your model service's own web search. It also tries the addresses your default model suggests, and known addresses for some makers. On a product page, Kelvin follows the links on the maker's own site that look most like this model's manual (naming the model or its series, PDFs, "manual" or "programming"), skipping the site's general pages, and from a page about the model's manuals on to its PDFs. A document counts only once Kelvin has downloaded it and its text names the model. Of those, the one with the most command syntax wins.
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
- **Trying:** type the instrument's address under the card and press **Try it**. Kelvin asks the instrument who it is and reads every reading, and shows each value or the error it gave. It sets nothing. To a text instrument it sends only queries, and it skips reads that could start a measurement or a self-test. Compare the values with the front panel, fix the card and try again until they agree. An instrument already connected in the lab is asked through that connection.
- **Checking:** read the draft against the manual, edit it, press **Check**, then **Save card**. It is kept in `~/Kelvin/cards/` and appears in the card list for your lab config.
- **In a chat:** "here is the command reference of my magnet supply, make a card", or "the attoDRY examples are in C:\attocube\examples, make a card for it". Kelvin reads them (`read_examples`), writes the card (`card_guide`, `check_card`), tries it on an instrument in the lab or one the scan found (`try_card`, readings only; another address asks you first), fixes it with you, saves it (`save_card`) and adds the instrument (`add_instrument`).
- **Drafts are safe to try:** a new card is a **draft**. On real hardware its readings work, so you can try it, but every change to its settings or outputs asks you first, saying the card is a draft (nobody has yet checked which of its parameters only configure, or its limits).
- **Reviewing:** when you have checked the commands, limits and safe values, press **Mark reviewed**. Only a person can do that, and Kelvin never overwrites a card a person has reviewed.

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

Kelvin runs the library in a helper process, so a crash or a 32-bit DLL never takes Kelvin down. The helper loads only the module or DLL the card names, from that folder (or an installed package of the maker's), never part of Python itself or a system DLL. It calls only the functions on the card, and only the maker's own: a path goes only through objects the maker's package made, never into Python's modules or objects, such as a socket the library holds. Readings must call functions that only read (`get…`, `is…`, `query…`), and a switch that only toggles is read before it is toggled. The included attoDRY card is a draft written from attocube's header (attoDRYLib, for the attoDRY's USB connection): try it, compare it with the touch screen, and mark it reviewed when it agrees.

## What an instrument needs on this computer

The app carries no instrument maker's software: every lab has different instruments. When one needs its maker's Python package (QICK, LabOne, MultiPyVu, a camera's SDK) or the maker's files (a DLL, an SDK's Python folder), Kelvin installs them on this computer, once you approve, in an environment of its own:
- **What you approve:** each package with its version and where it comes from (PyPI, with the project's summary and home page), the maker's files and their source, and the first time uv, the installer Kelvin uses, from its GitHub release (checked against its published checksum). Nothing is installed system-wide, and no administrator rights are needed.
- **Where it goes:** `~/Kelvin/env/<name>`, each with its own Python. Kelvin's built-in drivers use the `drivers` environment, which has the app's own Python version, so the app loads from it. A library card uses the environment its address names, `lib://<address>?env=<name>`, with the Python its library needs: a 32-bit one for a 32-bit DLL, an older one where the maker's package wants it.
- **Versions that must match:** some instruments need the same version on both sides, such as a QICK board and this computer's `qick`. Kelvin installs the version the instrument has.
- **The makers' installers stay yours:** a USB driver or a runtime that needs administrator rights (NI-DAQmx, the Opal Kelly driver, LightField) comes from the maker's installer, which you run. Kelvin says which one, and where it is.
- **Cards say what they need:** a card's `requires` lists the maker's Python packages its driver imports, such as `requires: [qick, Pyro4]`.
- **Seeing and removing them:** ask Kelvin what it has installed (`environments`), or to remove an environment (`remove_environment`, which you approve).
- **Searching outside the lab** (Settings) also covers these downloads: with it off, or in a project that keeps its work off the internet, Kelvin downloads nothing.

**Finding out what an instrument is.** When a scan doesn't find an instrument, or finds it but no card matches, Kelvin looks further by itself:
- `inspect_address`, for an address on the lab's network: which of the ports instruments use answer, and what they say (an `*IDN?` reply where it speaks SCPI, a web page's title, its LXI identification, a Jupyter or Pyro server on a board). It looks only at local networks, and leaves an instrument already in the lab connected as it is.
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
- **`acquires: true`:** the reading runs an acquisition, so Kelvin takes it only when asked or measured.
- **`connection: { echo: true }`:** the instrument echoes each command as a line, which Kelvin reads and drops before the answer.
- **Commands that aren't SCPI-shaped** (`?NM`, `%`, `{value} NM/MIN`): matched as written, ignoring case.
