"""Run by Python at start-up when this folder is on PYTHONPATH. With LABSIM_HOME set, the bench's COM ports appear
in pyserial and Andor's spectrograph DLL in ctypes (labsim/hooks.py); the makers' Python libraries beside this file
(pypylon, pyAndorSDK3, TimeTagger, zhinst, MultiPyVu, nidaqmx, seabreeze) talk to the bench too."""
import os
import sys

if os.environ.get("LABSIM_HOME"):
    try:
        from labsim import hooks as _labsim_hooks
        _labsim_hooks.install()
    except Exception as _e:   # never stop the program itself from starting
        sys.stderr.write("labsim: hooks not installed (%s)\n" % _e)
