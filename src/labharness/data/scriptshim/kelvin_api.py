"""Kelvin's experiment API, for a script Kelvin runs: the script imports it as kelvin. kelvin_shim.install puts it in
place before the script's own code runs, so a kelvin.py beside the script, or a package of that name, never takes its
place. Every call goes to Kelvin over the shim's connection, and Kelvin carries it out through its safety gate, by the
lab's own names for its instruments and their parameters (labharness/experiment.py reads an experiment ahead):

    from kelvin import lab
    lab.set("smu.V", 0.5)            # checked against the limits, ramped and watched, as any drive change
    x = lab.get("lockin.X")
    lab.do("stage.home")             # one of the card's named actions
    lab.raw("smu", "SYST:BEEP")      # a command no parameter of the card covers

A refusal ends the run with KelvinRefused, as a refused command does; an instrument that didn't answer raises
KelvinInstrumentError. Standard library only, Python 3.6 or later."""

_shim = None   # kelvin_shim, once attached
KelvinRefused = RuntimeError          # the shim's own, once attached
KelvinInstrumentError = IOError


def attach(shim):
    """Kelvin's stand-in (kelvin_shim) this API speaks through: its connection, and its errors as this module's."""
    global _shim, KelvinRefused, KelvinInstrumentError
    _shim = shim
    KelvinRefused = shim.KelvinRefused
    KelvinInstrumentError = shim.KelvinInstrumentError


def _ask(op, **fields):
    if _shim is None or _shim._LINK is None:
        raise RuntimeError("kelvin works only in a script Kelvin runs")
    return _shim._LINK.request(op, **fields)


def _plain(value):
    """A value as it travels to Kelvin: numpy's numbers and arrays as Python's own, tuples as lists."""
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return dict((str(k), _plain(v)) for k, v in value.items())
    tolist = getattr(value, "tolist", None)
    if callable(tolist) and not isinstance(value, (str, bytes)):
        return _plain(tolist())
    return value


def _key(method, key, what="parameter", example="smu.V"):
    key = str(key)
    name, dot, rest = key.partition(".")
    if not dot or not name or not rest or "." in rest:
        raise ValueError("lab.%s takes 'instrument.%s', such as '%s'" % (method, what, example))
    return key


class _Lab(object):
    """The lab's instruments, by the lab's names, through Kelvin's safety gate."""

    def get(self, key):
        """A reading or a setting, read now: lab.get("lockin.X")."""
        return _ask("get", key=_key("get", key)).get("value")

    def set(self, key, value, wait=True):
        """A setting or a drive changed: lab.set("smu.V", 0.5). wait: until it gets there (a ramp, a set point).
        The value as it was set."""
        return _ask("set", key=_key("set", key), value=_plain(value), wait=bool(wait)).get("value")

    def do(self, key):
        """One of the card's named actions: lab.do("stage.home"). The instrument's reply, or None."""
        return _ask("act", key=_key("do", key, "action", "stage.home")).get("reply")

    def raw(self, instrument, command):
        """A command no parameter of the card covers, to the instrument by its lab name: a query's reply, or None."""
        return _ask("raw", instrument=str(instrument), command=str(command)).get("reply")

    def __repr__(self):
        return "<the lab's instruments, through Kelvin's safety gate>"


lab = _Lab()

def _number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def trace(values, x=None):
    """A trace for record, with its x values: record(f=f, S21=trace(s21, x=freqs))."""
    return {"kelvin": "trace", "values": _plain(values), "x": None if x is None else _plain(x)}


def _column(name, value):
    if isinstance(value, dict) and value.get("kelvin") == "trace":
        return value
    v = _plain(value)
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, list) and all(_number(e) for e in v):
        return v
    if isinstance(v, list) and v and all(isinstance(r, list) and all(_number(e) for e in r) for r in v):
        return v
    raise TypeError("record takes numbers, text, a list of numbers (a trace) or a list of lists (an image); %s is %s"
                    % (name, type(value).__name__))


def record(**columns):
    """One row of the run's data, its columns in the order given: record(V=v, I=lab.get("smu.I")). The first column
    of the first row is the run's axis, unless columns() named them."""
    if "i" in columns or "t" in columns:
        raise ValueError("i and t are Kelvin's own columns")
    _ask("record", row=dict((k, _column(k, v)) for k, v in columns.items()))


def columns(axes=(), units=None):
    """The run's axes (the columns the others are plotted against) and units: columns(axes=("V",), units={"V": "V",
    "I": "A"})."""
    _ask("columns", axes=[str(a) for a in axes], units=dict((str(k), str(v)) for k, v in (units or {}).items()))


def live(name, default=None):
    """A live input's value now: one a person or the chat may change while the experiment runs (Stop, Break, a set
    point). Ask again to see a change: live("Break", False) in each pass of a loop."""
    return _ask("live", name=str(name), default=_plain(default)).get("value")


def stopped():
    """Has Kelvin stopped the run? For the experiment's own threads (the main one gets KeyboardInterrupt)."""
    link = _shim._LINK if _shim is not None else None
    return link is not None and link.stopping.is_set()


_ACTIONS = {}   # action name -> its function, in the order the file defines them


def action(name=None, main=False):
    """Marks a function as one of the experiment's actions, each run on its own (Kelvin runs the one asked for, after
    the module's own code): @action, @action("scan"), @action("scan", main=True) for the one a run without a name
    runs."""
    def mark(fn, as_name):
        _ACTIONS[as_name or fn.__name__] = fn
        fn.kelvin_action = as_name or fn.__name__
        return fn
    if callable(name):   # @action, bare
        return mark(name, None)
    return lambda fn: mark(fn, name)


def _run(name):
    """The action Kelvin was asked to run (it chose it before the run: experiment.choose)."""
    fn = _ACTIONS.get(name)
    if fn is None:
        raise RuntimeError("no action %r: it has %s" % (name, ", ".join(_ACTIONS) or "none"))
    return fn()
