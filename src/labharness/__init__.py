"""Kelvin: a safe interface between AI agents and lab instruments.

The agent-facing surface is SafetyGate (get, set, raw, undo, describe,
status) and RunEngine (validate, simulate, run). Everything else
is plumbing the agent never touches.
"""
from ._version import __version__
from .cards import Card, CardError, CardRegistry, ParamSpec, load_card, parse_card
from .clock import RealClock, SimClock
from .engine import RunEngine, RunReport
from .eventlog import EventLog, RunStore
from .gate import (ApprovalRequest, Approver, ConsoleApprover, DenyAll, Result, SafetyGate,
                   ScriptedApprover, SimulationApprover)
from .lab import HealthResult, Lab
from .labconfig import ConfigError, Envelope, LabConfig, load_envelope, load_lab_config
from .plans import Plan, PlanError, load_plan
from .homelab import build_lab, builtin_registry, empty_lab, open_home_lab

__all__ = [
    "__version__", "ApprovalRequest", "Approver", "Card", "CardError", "CardRegistry", "ConfigError",
    "ConsoleApprover", "DenyAll", "Envelope", "EventLog", "HealthResult", "Lab", "LabConfig", "ParamSpec",
    "Plan", "PlanError", "RealClock", "Result", "RunEngine", "RunReport", "RunStore", "SafetyGate",
    "ScriptedApprover", "SimClock", "SimulationApprover", "build_lab", "builtin_registry", "empty_lab",
    "load_card", "load_envelope", "load_lab_config", "load_plan", "open_home_lab", "parse_card",
]
