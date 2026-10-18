# Instruments

How Kelvin finds and talks to real instruments, what it keeps about each one, and how to add an instrument it doesn't know yet.

## Connecting instruments

Kelvin talks to instruments over VISA: GPIB, USB-TMC, LAN and RS-232 / USB-serial. It uses the VISA library you already have (NI-VISA or Keysight IO Libraries), or a pure-Python one if there is none. Install the extra with `pip install "kelvin-lab[hardware]"`; the desktop app includes it.

**Kelvin sets up what it finds.** The first time the desktop app starts (and on every start until you have a lab of your own), Kelvin asks each connected instrument who it is. If any match a card, it connects them right away:
- **Watching only.** Kelvin writes `~/Kelvin/lab.yaml` with the instruments it found, marked `observe_only: true`. Readings and measurement settings work at once. Every output (voltage, current, field, temperature, laser power) stays at its safe value until you check the limits in **Set up…** and press **Unlock outputs**. Until then, Kelvin refuses to drive anything away from safe and says why.
- **A folder for each instrument** in `~/Kelvin/instruments/<name>/`:
  - `instrument.json`: who it is (maker, model, serial number and firmware from its identity reply), its address, the connection settings it answered with (such as 57600 baud, 7 data bits, odd parity), and where its manual came from.
  - `settings.json`: every setting and reading it had (ranges, time constants, sensitivities...), refreshed on every start.
  - `manual.pdf`: the maker's own manual, fetched once from the address on the card, and `manual.txt` with its text by page.
- **Manuals for involved scans.** Before a scan that uses buffers, triggers, sweep or list modes, fast acquisition, ranges or filters, Kelvin reads the instrument's manual with `instrument_info` and `search_manual`. It works from the passages it finds and cites the page. Search weighs rare words most, so a mnemonic such as `OFLT` or a SCPI path such as `SOURce:CURRent:LIMit` finds its own entry.
- **Manuals you add yourself.** 17 of the 21 cards carry the maker's link. Keysight hands out its manuals only after a sign-in, and the Oxford Mercury iTC manual is no longer public. For these, or for any other instrument, choose the PDF in **Set up…** (**Choose PDF…**) or put it in the folder as `manual.pdf`.
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
| Cryostat systems | Quantum Design PPMS, DynaCool, VersaLab, MPMS3 (through MultiVu) |
| Magnet supplies | Lake Shore 625, AMI 430 |
| Lasers and optics | Coherent OBIS, Thorlabs ITC4000 laser/TEC controller, Thorlabs PM100/PM400 power meter |
| Amplifiers | SRS SR560 voltage preamp, SRS SR570 current preamp (listen-only: Kelvin shows what it last set) |
| Meters and generators | Keysight/Agilent 344xxA DMMs, Keithley 2182A nanovoltmeter, Keysight 33500B/33600A generators |

Real hardware gets the same gate as the virtual lab, and a few protections of its own:
- **Measuring never powers anything up.** On some source-meters a reading command switches the output on, so those readings are only taken while the output is already on (`read_when`).
- **No speed-up on real time.** A lab with any real instrument runs on real time, so ramps keep their rates.
- **Risky commands stay locked.** Commands that change what readings mean, or that bypass the instrument's own protection, are blocked in the card: sensor curves, coil constants, persistent-switch heaters, current limits, and DMM resistance modes that push current into a device.
- **Connecting only configures.** A card's `on_connect` commands are checked never to touch a drive.

**Instruments behind vendor software.** Some instruments are not driven with text commands but through their maker's library. Kelvin has drivers for two of them; the address says which:
- `zhinst://dev1234` for a Zurich Instruments lock-in, through LabOne's Python API (`pip install zhinst-core`). Add `?host=...&port=8004&interface=1GbE` if the data server runs elsewhere. A scan finds these devices through LabOne.
- `qd://<computer>:5000` for a Quantum Design system, through MultiPyVu (`pip install MultiPyVu`, with its server running next to MultiVu). MultiVu ramps temperature and field itself; Kelvin sets the rate and setpoint, then waits for the reading.

Their cards use the same command templates (`SET <path> {value}`, `GET <path>`), so limits, ramps, approvals and the simulated twins work as for any instrument. The vendor libraries are not part of the desktop app; install Kelvin from source to use them (`pip install "kelvin-lab[desktop,zhinst]"` or `[desktop,qd]`). These cards and drivers follow the vendors' documentation and haven't been verified on hardware yet, so tighten the limits and watch the first runs.

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

## Cards for new instruments

**Kelvin writes the card itself.** When a scan finds an instrument that says who it is but matches no card, Kelvin writes one. It does this at start-up, and when you press **Find its manual and write its card** on that instrument in **Set up…**.
1. **It finds the manual.** Your default model suggests where the maker publishes it, and Kelvin also tries known addresses for some makers. A product or support page is followed one step to the PDFs it links, on the maker's own site only. A document counts only once Kelvin has downloaded it and its text names the model. Of those, the one with the most command syntax wins.
2. **It drafts the card.** The model writes the card from the manual's command pages, the card checker reviews it, and the model fixes what the checker found, once.
3. **It makes the card safe to try, whatever the model wrote:**
   - The card is a draft. On real hardware its outputs stay simulation-only, and every setting change asks you.
   - No commands are sent on connect.
   - Reads that start something are left out (`MEASure?`, `READ?`, `INITiate`, `*TST?`, `*CAL?`), because on some instruments they switch an output on or run a self-test.
   - Calibration and firmware commands are blocked.
   - The identity pattern is checked against the instrument's own reply.
4. **It sets the instrument up.** At start-up, the instrument joins the watching-only lab with the others, and its folder gets the manual Kelvin downloaded. The card's first lines say where it came from and which model wrote it.

What leaves the computer: the maker and model name, and the manual's command pages, go to your model; the manual's address goes to the maker's site. The serial number never leaves. Switch this off with **Write cards for instruments Kelvin doesn't know** in Settings. If Kelvin can't find the manual (some makers want a sign-in) or there is no model, it says so; then draft the card from a PDF yourself:

For an instrument Kelvin doesn't know, open **Lab → Set up → Make a card for it from its manual…**.
- **Drafting:** paste the manual's remote-programming chapter (copy it from your PDF viewer, or load a text file). Pick a model and press **Draft the card**. The model writes the card from that text only, and Kelvin checks it against the card format, asking the model to fix any problems once.
- **Checking:** read the draft against the manual, edit it, press **Check**, then **Save card**. It is kept in `~/Kelvin/cards/` and appears in the card list for your lab config.
- **In a chat:** "here is the command reference of my magnet supply, make a card". Kelvin uses `card_guide`, `check_card` and `save_card`.
- **Drafts are safe to try:** a new card is a **draft**. On real hardware its readings work, so you can try it, but its outputs stay simulation-only, and changing any of its settings asks you first (nobody has yet checked which of its parameters only configure).
- **Reviewing:** when you have checked the commands, limits and safe values, press **Mark reviewed**. Only a person can do that, and Kelvin never overwrites a card a person has reviewed.

## Writing a card

```yaml
card: 1
id: acme/sm-2000
model: ACME SM-2000 Source-Meter
kind: source-meter
status: draft                 # draft cards keep their drives simulation-only on real hardware
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
