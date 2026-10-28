# labsim: a simulated lab that programs take for real hardware

labsim runs a **bench**, a set of simulated instruments wired to shared physics, in its own server process. Programs
reach it the way they reach real instruments, so nothing in the program changes. Kelvin runs unmodified on top of
it: its start-up scan, its cards, its adapters, its makers' drivers and its safety gate all do what they would do in
a lab.

| What the program uses | What labsim gives it |
|---|---|
| PyVISA (`KELVIN_VISA_BACKEND=@labsim`, or `ResourceManager("@labsim")`) | A VISA library that says it is NI-VISA (`visa64.dll`, National Instruments): GPIB, USB-TMC, LAN (VXI-11, HiSLIP, raw sockets), serial |
| pyserial (`serial.Serial("COM5")`, `list_ports.comports()`) | The bench's COM ports, with USB VID:PID, next to the computer's real ones |
| Makers' Python libraries | Stand-ins with the real names and calls: `pypylon`, `pyAndorSDK3`, `pyAndorSDK2`, `TimeTagger` / `Swabian.TimeTagger`, `zhinst.core`, `MultiPyVu`, `nidaqmx`, `seabreeze` (each with package metadata, so it counts as installed) |
| `ctypes.WinDLL("atspectrograph.dll")` | Andor's Shamrock / Kymera spectrograph DLL |
| Its own sockets | Instruments with `tcp: PORT` also listen on `127.0.0.1:PORT` (e.g. `anc300://127.0.0.1:7230`) |

## Quick start

```bash
# a bench in a folder of its own (one per tester)
python -m labsim up benches/transport.yaml --home benchhome/transport

# point a program at it: this prints the three variables
python -m labsim env --home benchhome/transport
#   LABSIM_HOME=...  KELVIN_VISA_BACKEND=@labsim  PYTHONPATH=<labsim>/shims;<labsim>;...

# or run Kelvin on it in one go (starts the bench if it isn't running)
./kelvin.sh benches/transport.yaml benchhome/transport --port 8801 --no-accounts
```

Then `python -m labsim status`, `log NAME`, and the fault commands below work against the same `--home`, or against
`LABSIM_HOME`.

Requirements: Python 3.9+ with `pyvisa` (and `pyvisa-py` if the program also does a LAN broadcast), `numpy`,
`pyyaml`, and `pyserial` for COM ports. These are Kelvin's own `[hardware]` extras.

## What behaves like the real thing

- **Command sets and replies come from the instruments' manuals, not from Kelvin's cards.** That way a wrong card
  fails here as it would in a lab. The parsers handle short and long SCPI forms, `;` chains (with SCPI's relative
  paths), numeric suffixes and `MIN`/`MAX`/`DEF`.
- **Errors and status:**
  - An unknown command gives -113 and no reply, so a query for it times out.
  - Errors queue up for `SYST:ERR?` (on instruments that have one), and `*ESR?`/`*STB?` carry the status bits.
  - On GPIB, USB-TMC, VXI-11 and HiSLIP, a reply that isn't read is dropped when the next command arrives (-410).
  - On a raw socket or serial line, the reply stays in the buffer to be read late.
- **Buses:**
  - GPIB opens any address, then finds no listeners; a LAN address nobody answers at takes a second to fail.
  - A pulled USB cable gives I/O errors and the device leaves the listing; a COM port another program has open is busy.
  - Serial framing must match: a Lake Shore at 57600 7O1 doesn't understand 9600 8N1.
- **Instrument quirks:**
  - The AMI 430 greets each LAN connection, and the Coherent OBIS answers `OK` after everything.
  - A PI E-727 starts with its servo off, and a Newport SMC100 refuses moves until homed.
  - An Arduino resets when its port opens (heater off, 2 s of boot), and a Pfeiffer gauge speaks ENQ/ACK.
  - The SR830 can be set to answer on RS-232 only (`outx: 0`).
- **Physics shared between instruments:**
  - A lock-in finds a signal only at the frequency the sample is actually excited at.
  - The 2400's compliance limits a gate as the leakage rises.
  - A 6221 in Delta mode with its 2182A cancels the sample's thermal EMF, which a DC reading includes.
  - A Lake Shore's heater range caps how hot it can go.
  - Ramping a magnet too fast quenches it.
  - Too much gate voltage breaks the dielectric, and too much current burns the device.
  - The flux coil tunes the resonator, and a tone at the qubit frequency shifts it dispersively.
  - Where the piezo points decides the photon counts, the camera image and the CCD's spectrum (photoluminescence,
    silicon's Raman line, the laser leaking through).
  - A warm CCD is dark-current limited.
  - A PPMS ramps at the rate it is given and settles on its set point: it owns the stage temperature, so the
    cryostat's own cooling doesn't hold it short.
- **Timing:**
  - Measurements take their integration time (NPLC, time constants, exposures, sweeps).
  - Cool-downs take minutes, not hours, but they do take minutes.

## Benches

| Bench | What is on it |
|---|---|
| `transport.yaml` | Graphene Hall bar, back gate: Keithley 2400 (gate), 6221 (AC current, phase marker), 2 x SR830, 2182A, Lake Shore 336 (COM3, 57600 7O1), AMI 430 (LAN socket), HP 34401A (Cernox), GS200 |
| `cqed.yaml` | Resonators and a flux-tunable transmon at 12 mK: PNA (HiSLIP), GS200 on the flux coil, SMB100A qubit tone, Lake Shore 350, MFLI (zhinst) |
| `ppms.yaml` | PPMS DynaCool through MultiPyVu (qd://127.0.0.1:5000), a superconducting film (Tc 8.6 K): 6221, 2182A, SR860 |
| `confocal.yaml` | Quantum emitters at room temperature: OBIS (COM5), PM100D, PI E-727 (LAN), ANC300 (COM7 and TCP 7230), Arduino (COM4), Shamrock + Andor CCD, Basler, Zyla, Time Tagger, NI USB-6363 (galvos), Ocean USB4000 |
| `pumpprobe.yaml` | SMC100 delay line (COM6), pump and probe OBIS (COM8, COM9), SR830 on a balanced photodiode locked to the chopper, PM100D, Pfeiffer TPG 362 (COM3) |
| `no_cards.yaml` | Instruments Kelvin has no card for: 6221, 34401A, Pfeiffer gauge, Arduino hot plate, Mercury iTC (LAN), SMC100 |

`python -m labsim models` lists every model. A bench file says where each instrument sits (`address:` a VISA
resource, `COMn`, or `sdk:<kind>:<serial>`) and what it is wired to (`connect: {port: terminal}`; a terminal is
`bias`, `gate`, `vxx`, `vxy`, `flux`, `chip_in`/`chip_out`, `qubit_drive`, `mw`, `pd`, `ref`, ...).

## Making things go wrong

| Command | What happens |
|---|---|
| `python -m labsim power NAME off` | Switched off: no listeners on GPIB, no answer elsewhere. `on` brings it back with its power-on settings |
| `python -m labsim unplug NAME` / `plug NAME` | Cable pulled: USB and LAN sessions error, the device leaves the listing |
| `python -m labsim hold NAME` | Another program holds it: opening gives "resource busy" |
| `python -m labsim fault NAME dead\|garble\|slow\|noisy` | No replies, corrupted replies, 2 s late replies, 10x noise |
| `python -m labsim fault tc sensor_openA` / `heater_open` | A broken thermometer (reads 0, RDGST 128) or heater (no heating, HTRST 1) |
| `python -m labsim fault laser interlock_open` | The OBIS key interlock opens: emission stops |
| `python -m labsim set PATH VALUE` | Change the world: `cryo.stage 4.2`, `vacuum.leak 1e-4`, `sample.damaged none` (repair), `magnet.quench_rate 0.05` |
| `python -m labsim status` / `log NAME [N]` | What every instrument is doing, the world, recent events (quench, sample damage); what NAME was sent and answered |

## In Kelvin's tests

`tests/labsim_bench.py`, in Kelvin's own tests folder, has two pytest fixtures (`tests/test_labsim.py` uses both):

- `labsim_bench(bench, sdk=False)` starts the bench's server in the test's temporary folder and points the test's own
  process at it: Kelvin's VISA (`KELVIN_VISA_BACKEND=@labsim`), the bench's COM ports in pyserial and, with
  `sdk=True`, the makers' libraries as labsim's stand-ins. It returns the bench: `.env` points another program at
  it, and `.ctl("fault", "tc", "sensor_openA")` does what the command line does. When the test ends the server stops
  and everything is put back. It needs PyVISA; without it the test is skipped.
- `labsim_offline(bench)` builds a bench's world and instruments in the test's process, with no server.
  `advance(bench, seconds)` lets that much time pass at once. Use it for one instrument's physics.

```python
from labsim_bench import labsim_bench  # noqa: F401  (the fixture)

def test_gate_sweep(labsim_bench, tmp_path):
    bench = labsim_bench("transport")
    lab = build_lab(tmp_path / "lab.yaml", partial=True)   # Kelvin's own adapters and cards, on the bench
    bench.ctl("fault", "li_xx", "garble")
```

## Limits

- Models follow the manuals as far as written here, not every command. A command a model doesn't know gives -113,
  which can look like a card bug. Check `python -m labsim log NAME` and the manual before you blame the program.
- Not modelled:
  - Keithley 6221 Pulse Delta and differential conductance (Delta is modelled: a bench names the 6221's
    `nanovoltmeter:`);
  - pulse sequencers (Pulse Streamer, PulseBlaster), PICam/LightField, Harvesters/GenICam, OpenCV cameras;
  - Modbus, M Squared, TOPTICA, QICK, web-API instruments;
  - LAN discovery by broadcast or mDNS (sockets aren't advertised).
- The physics is qualitative: right shapes, scales and failure modes, not a materials simulation.

## Layout

- `labsim/`: the server (`server.py`), the client link, the instrument base and SCPI parser (`instrument.py`), the
  physics (`world.py`), the models (`models/`), the pyserial and DLL hooks (`hooks.py`, `dlls.py`).
- `pyvisa_labsim/`: the VISA library.
- `shims/`: `sitecustomize.py` and the SDK stand-ins; this folder goes first on PYTHONPATH.
- `benches/`, `tests/`: bench files, and scripts that drive each bench as a lab script would.
