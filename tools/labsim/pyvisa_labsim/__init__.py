"""A PyVISA backend for labsim benches: ResourceManager("@labsim"), or KELVIN_VISA_BACKEND=@labsim for Kelvin.

It presents itself as NI-VISA does (visa64.dll, made by National Instruments) and behaves like it: viFindRsrc lists
what answers, GPIB opens any address and then finds no listeners, a LAN address nobody answers at takes a while
before it fails, a serial port another program holds is busy, and reads end at the termination character or END
as the session's attributes say.
"""
from .highlevel import LabsimVisaLibrary

WRAPPER_CLASS = LabsimVisaLibrary
