"""The Kelvin safety gate between the sequence engine and a block's measurement script (v2 phase B).

A labMaster-style block runs a measurement Python script in a child process (`runners/run_block.py`).
By itself that child would drive the instruments directly, with nobody checking it. Since v2 its
child gets Kelvin's stand-ins for the instruments (`data/scriptshim`, the same ones a lab script run
gets): every pyvisa / pyserial / socket command becomes a JSON request to this process, and
`scriptrun.ScriptRun`'s handlers pass it through `SafetyGate` - limits, interlocks, ramps, guards,
approvals, the lab record - as if Kelvin's agent had sent it.

What this module adds on top of scriptrun:

    BlockApprover   one person's yes covers the block run: the first request reaches a person with the
                    block's script name, the instrument and its limits; later requests are answered
                    "approved once for this block" (limits and guards still apply to every command).
    BlockGate       one block run's bridge: a ScriptRun handler for the block's name, a ShimServer,
                    the approval wrapper in place for the run, the drives brought back to safe after a
                    failed or stopped block, and run_start / run_end entries in the lab record.
    GatedEngine     the vendored Engine, with the gate environment added to the measurement runner
                    only. The safe-stop child is NOT gated (stopping must always be possible), and a
                    SIM run (`--sim`) does not start the script at all, so it gets nothing.

The sequence service chooses GatedEngine when a gate is attached (`SequenceService.attach_gate`);
without one the engine is exactly the vendored one and nothing changes (the old path, no gate).
"""
from __future__ import annotations

import os
import re
import threading
import time
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from .. import scriptrun
from ..gate import Approver

DEFAULT_BLOCK = "sequence block"


# ---------------------------------------------------------------- one block run's approval

def _limits_line(gate: Any, request: Any) -> str:
    """The limits of the instruments a request names, for the card: '<name> limits: V [-10, 10] V, ...'."""
    text = "%s\n%s" % (getattr(request, "title", ""), "\n".join(str(i) for i in getattr(request, "items", [])))
    bits: List[str] = []
    try:
        instruments = gate.lab.instruments
    except Exception:                                            # noqa: BLE001 - a card line never stops a card
        return ""
    for name, inst in instruments.items():
        if not re.search(r"(?<![\w.])%s(?![\w.])" % re.escape(str(name)), text):
            continue
        shown: List[str] = []
        try:
            for param, spec in inst.card.params.items():
                limits = inst.limits(param)
                if limits is None:
                    continue
                shown.append("%s [%s, %s]" % (param, spec.show(limits[0]), spec.show(limits[1])))
        except Exception:                                        # noqa: BLE001
            continue
        if shown:
            bits.append("%s limits: %s" % (name, ", ".join(shown)))
    if not bits:
        return ""
    return "Checked against " + "; ".join(bits) + " (and the card's interlocks and guards)."


class BlockApprover(Approver):
    """The person's yes for ONE block run, in front of the lab's usual approver.

    The first approval request a block's script causes goes to a person as it is - with the block's
    script named, and the limits of the instruments it names added - and, once approved, covers that
    block run: later requests are approved without asking again (every command is still checked
    against the limits, interlocks and guards, and one beyond a limit is refused). Declining one
    declines the rest, so the script stops at the first no. Requests from any other thread (another
    chat's run, the portal) go straight to the real approver: this wrapper is only for the block.
    """

    def __init__(self, gate: Any, name: str = DEFAULT_BLOCK, real: Optional[Approver] = None):
        self.gate = gate
        self.name = str(name or DEFAULT_BLOCK)
        self.real: Approver = real if real is not None else gate.approver
        self.thread_id: Optional[int] = None      # the block's serving thread: only its requests are covered
        self.approved: Optional[str] = None
        self.declined: Optional[str] = None

    def approve(self, request: Any) -> bool:
        if self.thread_id is not None and threading.get_ident() != self.thread_id:
            return bool(self.real.approve(request))            # somebody else's card: unaffected
        if self.declined is not None:
            request.note = "declined earlier in this block run (%s)" % self.declined
            return False
        if self.approved is not None:
            request.note = ("approved once for this sequence block run, so its later commands are "
                            "not asked again")
            return True
        request.title = 'Sequence block "%s": %s' % (self.name, request.title)
        request.items.insert(0, 'The script of sequence block "%s" sends the instruments commands '
                                 'through Kelvin\'s safety gate.' % self.name)
        limits = _limits_line(self.gate, request)
        if limits:
            request.items.append(limits)
        request.items.append("Approving this covers this block run: its later commands are still "
                             "checked against the same limits, interlocks and guards, but a person "
                             "is not asked again.")
        ok = bool(self.real.approve(request))
        if ok:
            self.approved = request.title
        else:
            self.declined = str(request.note or "declined")
        return ok

    def __getattr__(self, item: str) -> Any:                    # inform(), vetoes, ... are the real approver's
        return getattr(self.real, item)


# ---------------------------------------------------------------- one block run's bridge


class BlockGate:
    """Kelvin's end of one block run: the stand-in server, the approval wrapper, and the record.

    The engine opens a block's bridge before it spawns the measurement runner and closes it when the
    runner has ended. One block at a time (the engine runs one), and a closed bridge can be opened
    again by the next block."""

    def __init__(self, gate: Any):
        self.gate = gate
        self._lock = threading.RLock()
        self._open: Optional[Dict[str, Any]] = None
        self.opened = 0                 # block runs served
        self.last: Dict[str, Any] = {}  # the last run's outcome (tests, the report)

    # -- the engine's side

    def open_env(self, name: str, target: str = "", base_env: Optional[Dict[str, str]] = None,
                 run_id: Optional[str] = None) -> Dict[str, str]:
        with self._lock:
            self.close(None)                                   # one block at a time
            run = scriptrun._BlockScriptRun(self.gate, SimpleNamespace(name=str(name or DEFAULT_BLOCK),
                                                                       path=str(target or "")),
                                            None, {}, set())
            approver = BlockApprover(self.gate, name)
            preapproved = self.gate.preapproved({})
            preapproved.__enter__()
            self.gate.approver = approver
            try:
                server = scriptrun.ShimServer(run).start()
                approver.thread_id = server.thread.ident
                base = dict(base_env or {})
                if "PYTHONPATH" not in base:                   # keep the child's own path, as scriptrun does
                    base["PYTHONPATH"] = os.environ.get("PYTHONPATH", "")
                env = server.env(base)
                self.opened += 1
                self._open = {"name": str(name or DEFAULT_BLOCK), "target": str(target or ""),
                              "run": run, "server": server, "approver": approver,
                              "preapproved": preapproved, "run_id": run_id,
                              "started": time.time()}
                self._log("sequence_block_start", plan=name, script=target or None, run=run_id,
                          gate="in-process")
                return env
            except BaseException:
                self.gate.approver = approver.real
                preapproved.__exit__(None, None, None)
                raise

    def stop(self, why: str = "stop requested") -> None:
        """The engine's Stop/Skip: the script's time.sleep returns and it gets one KeyboardInterrupt."""
        with self._lock:
            state = self._open
        if state is not None:
            state["server"].stop(why)

    def close(self, code: Optional[int] = None) -> None:
        """The runner has ended (code is its exit code, when the engine saw it): bring the drives the
        block set back to safe if the block failed or was stopped, and record how it went."""
        with self._lock:
            state, self._open = self._open, None
        if state is None:
            return
        run, server = state["run"], state["server"]
        failed = str(getattr(run, "failed", "") or "")
        stopping = server.stopping
        server.close()
        cleanup: List[str] = []
        status = "done"
        reason = ""
        try:
            if failed:
                status, reason = "failed", failed
            elif stopping:
                status, reason = "stopped", stopping
            elif code not in (0, None):
                status, reason = "failed", "the block's script ended with exit code %s" % code
            if status != "done" and getattr(run, "touched", None):
                try:
                    cleanup = list(run._back_to_safe()) or []
                except Exception as exc:                       # noqa: BLE001 - a stop must not be blocked
                    reason = (reason + "; " if reason else "") + "bringing the drives back to safe failed: %s" % exc
            try:
                run._flush()
            except Exception:                                  # noqa: BLE001
                pass
        finally:
            self.last = {"name": state["name"], "status": status, "reason": reason,
                         "commands": int(getattr(run, "n_commands", 0) or 0),
                         "cleanup": cleanup, "opened_at": state["started"]}
            try:
                self._log("sequence_block_end", plan=state["name"], script=state["target"] or None,
                          run=state["run_id"], status=status, reason=reason or None,
                          commands=self.last["commands"], cleanup=cleanup or None,
                          failed=failed or None)
            except Exception:                                  # noqa: BLE001 - the record never blocks a stop
                pass
            self.gate.approver = state["approver"].real
            state["preapproved"].__exit__(None, None, None)

    # -- record and status

    def _log(self, kind: str, **fields: Any) -> None:
        try:
            self.gate.log.write(kind, by=self.gate.operator, **fields)
        except Exception:                                      # noqa: BLE001 - never in the way
            pass

    def state(self) -> Optional[Dict[str, Any]]:
        """The open block (its name and target), or None."""
        with self._lock:
            state = self._open
        return None if state is None else {"name": state["name"], "target": state["target"],
                                           "run_id": state["run_id"]}


# ---------------------------------------------------------------- the engine with the gate

def measurement_runner(cmd: Any) -> bool:
    """Is this command the engine's measurement runner (a block's script), and not its simulated run?
    Only that child gets the gate: `--sim` never runs the script, and the safe-stop child is started
    elsewhere (`_run_safe_stop_child`) and must never need Kelvin to be stopped."""
    try:
        parts = [str(c) for c in cmd]
    except TypeError:
        return False
    if "--sim" in parts:
        return False
    return any(part.replace("\\", "/").endswith("runners/run_block.py") for part in parts)


class GatedEngine:
    """Factory mixin: the vendored Engine, built with a BlockGate for its measurement runners.

    `panel_engine.Engine` is vendored (and must not be edited by hand); the engine `make_engine`
    builds changes only two methods: `_spawn` adds the gate environment around the measurement
    runner, and `_kill_child` tells the block's stand-in server that the engine is stopping it (so
    time.sleep returns and the script gets its one KeyboardInterrupt before the engine's own kill and
    safe stop)."""

    @staticmethod
    def make(config: Any, gate: Any) -> Any:
        return make_engine(config, gate)


def make_engine(config: Any, gate: Any) -> Any:
    """The vendored Engine, with the block gate attached; built here so `engine.py` stays untouched."""
    from . import engine as panel_engine

    class _GatedEngine(panel_engine.Engine):
        def _spawn(self, cmd, uid, on_event=None, env=None, stop_file=None):
            bridge = getattr(self, "_block_gate", None)
            gated = bridge is not None and measurement_runner(cmd)
            code: Optional[int] = None
            if gated:
                name, target = self._block_item(uid)
                env = bridge.open_env(name, target, dict(env or {}),
                                      run_id=getattr(self, "run_id", None))
            try:
                code = panel_engine.Engine._spawn(self, cmd, uid, on_event=on_event, env=env,
                                                  stop_file=stop_file)
            finally:
                if gated:
                    bridge.close(code)
            return code

        def _block_item(self, uid: Any):
            try:
                with self.lock:
                    for item in self.seq.get("items") or []:
                        if item.get("uid") == uid:
                            return (str(item.get("name") or uid or DEFAULT_BLOCK),
                                    str(item.get("target") or ""))
            except Exception:                                  # noqa: BLE001 - the name is only for the card
                pass
            return str(uid or DEFAULT_BLOCK), ""

        def _kill_child(self, proc=None):
            bridge = getattr(self, "_block_gate", None)
            if bridge is not None:
                bridge.stop("stop requested")
            return panel_engine.Engine._kill_child(self, proc)

    engine = _GatedEngine(config)
    engine._block_gate = BlockGate(gate)
    engine._kelvin_gate = gate
    return engine
