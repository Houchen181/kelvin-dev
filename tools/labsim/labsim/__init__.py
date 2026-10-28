"""labsim: simulated laboratory hardware, for testing Kelvin (or any lab program) as if instruments were plugged in.

One server process per bench holds the physics and the instruments wired to it. Programs reach the instruments the
way they reach real ones, so nothing in the program changes:
  - PyVISA through the labsim VISA library (KELVIN_VISA_BACKEND=@labsim, or ResourceManager("@labsim")), which
    presents itself as NI-VISA: GPIB, USB-TMC, LAN (VXI-11, HiSLIP, raw sockets) and serial resources;
  - pyserial COM ports (shims/sitecustomize.py adds the bench's ports to serial.Serial and list_ports);
  - the makers' Python libraries (shims/: pypylon, pyAndorSDK3, TimeTagger, zhinst, MultiPyVu, nidaqmx ...),
    stand-ins with the real libraries' names and calls, and Andor's spectrograph DLL through ctypes.
LABSIM_HOME names the bench's folder; server.json there says where its server listens.
"""
__version__ = "0.1.0"
