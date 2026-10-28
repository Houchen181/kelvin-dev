"""The VISA library: each session is a connection to the bench's server."""
from __future__ import annotations

import os
import random
from typing import Any, Dict, Optional, Tuple

from pyvisa import attributes, constants, errors, highlevel, rname
from pyvisa.constants import ResourceAttribute as RA
from pyvisa.constants import StatusCode as SC
from pyvisa.util import LibraryPath

from labsim import __version__
from labsim.link import LabsimError, Link

ERRORS = {"timeout": SC.error_timeout, "rsrc_nfound": SC.error_resource_not_found,
          "rsrc_busy": SC.error_resource_busy, "nlisteners": SC.error_no_listeners,
          "conn_lost": SC.error_connection_lost, "io": SC.error_io, "invalid_resource": SC.error_invalid_resource_name,
          "invalid_object": SC.error_invalid_object, "no_server": SC.error_system_error,
          "internal": SC.error_system_error}
HOW = {"end": SC.success, "term": SC.success_termination_character_read, "max": SC.success_max_count_read}
PARITY = {0: "none", 1: "odd", 2: "even", 3: "mark", 4: "space"}
MAKER = "National Instruments"


class Session:
    def __init__(self, link: Optional[Link], sid: int, parsed: Any, bus: str, rm_session: int):
        self.link, self.sid, self.parsed, self.bus = link, sid, parsed, bus
        self.session_type = (parsed.interface_type_const, parsed.resource_class)
        self.attrs: Dict[int, Any] = {
            RA.resource_manager_session: rm_session,
            RA.resource_name: str(parsed),
            RA.resource_class: parsed.resource_class,
            RA.interface_type: parsed.interface_type_const,
            RA.resource_manufacturer_name: MAKER,
        }
        for name, attr in (("board", RA.interface_number), ("primary_address", RA.gpib_primary_address)):
            if hasattr(parsed, name):
                try:
                    self.attrs[attr] = int(getattr(parsed, name))
                except (TypeError, ValueError):
                    pass

    def get(self, attribute: int) -> Tuple[Any, SC]:
        if attribute == RA.resource_manufacturer_name:
            return MAKER, SC.success
        try:
            attr = attributes.AttributesByID[attribute]
        except KeyError:
            return 0, SC.error_nonsupported_attribute
        if not attr.in_resource(self.session_type):
            return 0, SC.error_nonsupported_attribute
        return self.attrs.setdefault(attribute, attr.default), SC.success

    def set(self, attribute: int, value: Any) -> SC:
        try:
            attr = attributes.AttributesByID[attribute]
        except KeyError:
            return SC.error_nonsupported_attribute
        if not attr.in_resource(self.session_type):
            return SC.error_nonsupported_attribute
        if not attr.write:
            return SC.error_attribute_read_only
        self.attrs[attribute] = value
        return SC.success

    def framing(self) -> Optional[list]:
        if self.bus != "asrl":
            return None
        baud, _ = self.get(RA.asrl_baud_rate)
        bits, _ = self.get(RA.asrl_data_bits)
        parity, _ = self.get(RA.asrl_parity)
        stop, _ = self.get(RA.asrl_stop_bits)
        return [int(baud), int(bits), PARITY.get(int(parity), "none"), int(stop) / 10.0]


class LabsimVisaLibrary(highlevel.VisaLibraryBase):
    sessions: Dict[int, Any]

    @staticmethod
    def get_library_paths():
        return (LibraryPath(r"C:\Windows\system32\visa64.dll", "labsim"),)

    @staticmethod
    def get_debug_info():
        return {"Version": "labsim %s (presents itself as NI-VISA)" % __version__,
                "Bench": os.environ.get("LABSIM_HOME", "(LABSIM_HOME not set)")}

    def _init(self) -> None:
        self.sessions = {}
        self._rm_link: Optional[Link] = None

    def _register(self, obj: Any) -> int:
        session = None
        while session is None or session in self.sessions:
            session = random.randint(1000000, 9999999)
        self.sessions[session] = obj
        return session

    def _link(self) -> Link:
        if self._rm_link is None:
            self._rm_link = Link()
        return self._rm_link

    # -- resource manager
    def open_default_resource_manager(self):
        try:
            self._link().must("hello")
        except LabsimError as e:
            raise errors.VisaIOError(SC.error_system_error) from e
        return self._register(self), SC.success

    def list_resources(self, session, query: str = "?*::INSTR"):
        try:
            found = self._link().must("resources")["resources"]
        except LabsimError:
            return ()
        return tuple(rname.filter(found, query))

    def open(self, session, resource_name, access_mode=constants.AccessModes.no_lock,
             open_timeout=constants.VI_TMO_IMMEDIATE):
        try:
            parsed = rname.parse_resource_name(resource_name)
        except rname.InvalidResourceName:
            return 0, self.handle_return_value(None, SC.error_invalid_resource_name)
        try:
            link = Link()
        except LabsimError:
            return 0, self.handle_return_value(None, SC.error_system_error)
        r = link.call("open", resource=str(parsed), owner=os.getpid())
        if "err" in r:
            link.close()
            return 0, self.handle_return_value(None, ERRORS.get(r["err"], SC.error_system_error))
        sess = Session(link, r["sid"], parsed, r["bus"], session)
        handle = self._register(sess)
        status = SC.success_device_not_present if r["bus"] == "gpib" and not r.get("present") else SC.success
        return handle, self.handle_return_value(handle, status)

    def close(self, session):
        obj = self.sessions.pop(session, None)
        if obj is None:
            return SC.error_invalid_object
        if obj is self:
            if self._rm_link is not None:
                self._rm_link.close()
                self._rm_link = None
            return SC.success
        try:
            obj.link.call("close", sid=obj.sid)
        except LabsimError:
            pass
        obj.link.close()
        return SC.success

    def _sess(self, session) -> Optional[Session]:
        s = self.sessions.get(session)
        return s if isinstance(s, Session) else None

    def _call(self, s: Session, op: str, **kw) -> Dict[str, Any]:
        try:
            return s.link.call(op, sid=s.sid, **kw)
        except LabsimError as e:
            return {"err": e.code}

    # -- I/O
    def read(self, session, count):
        s = self._sess(session)
        if s is None:
            return b"", self.handle_return_value(session, SC.error_invalid_object)
        tmo, _ = s.get(RA.timeout_value)
        timeout = 86400.0 if tmo in (None, constants.VI_TMO_INFINITE) else int(tmo) / 1000.0
        termchar, _ = s.get(RA.termchar)
        term_en, _ = s.get(RA.termchar_enabled)
        term = int(termchar) if term_en else None
        if s.bus == "asrl":
            end_in, _ = s.get(RA.asrl_end_in)
            if end_in == constants.SerialTermination.termination_char:
                term = int(termchar)
            elif end_in == constants.SerialTermination.none:
                term = int(termchar) if term_en else None
        suppress, _ = s.get(RA.suppress_end_enabled) if s.bus != "asrl" else (False, None)
        r = self._call(s, "read", count=int(count), term=term, end=not suppress, timeout=timeout,
                       framing=s.framing())
        if "err" in r:
            return bytes(r.get("data") or b""), self.handle_return_value(session, ERRORS.get(r["err"],
                                                                                          SC.error_io))
        return r["data"], self.handle_return_value(session, HOW[r["how"]])

    def write(self, session, data):
        s = self._sess(session)
        if s is None:
            return 0, self.handle_return_value(session, SC.error_invalid_object)
        end, _ = s.get(RA.send_end_enabled) if s.bus not in ("asrl", "socket") else (False, None)
        r = self._call(s, "write", data=bytes(data), end=bool(end), framing=s.framing())
        if "err" in r:
            return 0, self.handle_return_value(session, ERRORS.get(r["err"], SC.error_io))
        return r["n"], self.handle_return_value(session, SC.success)

    def clear(self, session):
        s = self._sess(session)
        if s is None:
            return self.handle_return_value(session, SC.error_invalid_object)
        r = self._call(s, "clear")
        return self.handle_return_value(session, ERRORS.get(r.get("err"), SC.success) if "err" in r else SC.success)

    def read_stb(self, session):
        s = self._sess(session)
        if s is None:
            return 0, self.handle_return_value(session, SC.error_invalid_object)
        r = self._call(s, "stb")
        if "err" in r:
            return 0, self.handle_return_value(session, ERRORS.get(r["err"], SC.error_io))
        return r["stb"], self.handle_return_value(session, SC.success)

    def assert_trigger(self, session, protocol):
        s = self._sess(session)
        if s is None:
            return self.handle_return_value(session, SC.error_invalid_object)
        self._call(s, "trigger")
        return SC.success

    def flush(self, session, mask):
        return SC.success

    def lock(self, session, lock_type, timeout, requested_key=None):
        return (requested_key or "labsim"), SC.success

    def unlock(self, session):
        return SC.success

    def gpib_control_ren(self, session, mode):
        return SC.success

    def disable_event(self, session, event_type, mechanism):
        return SC.success

    def discard_events(self, session, event_type, mechanism):
        return SC.success

    def enable_event(self, session, event_type, mechanism, context=None):
        return SC.success

    # -- attributes
    def get_attribute(self, session, attribute):
        obj = self.sessions.get(session)
        if obj is None:
            return 0, SC.error_invalid_object
        if obj is self:
            if attribute == RA.resource_manufacturer_name:
                return MAKER, SC.success
            if attribute == RA.resource_name:
                return "::INSTR", SC.success
            return 0, SC.error_nonsupported_attribute
        return obj.get(attribute)

    def set_attribute(self, session, attribute, attribute_state):
        obj = self.sessions.get(session)
        if obj is None:
            return SC.error_invalid_object
        if obj is self:
            return SC.success
        return obj.set(attribute, attribute_state)
