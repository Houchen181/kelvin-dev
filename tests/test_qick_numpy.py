"""A QICK board's numpy can be older than Kelvin's. Field report (2026-11-13): a board with QICK 0.2.422 on Python 3.10
and numpy 1.21.5, Kelvin's drivers on numpy 2.5.4, and every read of i, q, magnitude and phase failed with "No module
named 'numpy.core.multiarray'". QICK has Pyro4 pickle what goes between them (protocol 4). numpy 1.x pickles its arrays
and numbers under numpy.core, which numpy 2 renamed numpy._core; numpy 2 keeps numpy.core's modules for such pickles,
but the app Kelvin installed had none of them (PyInstaller didn't see numpy's lazy imports). The other way, numpy 2
pickles under numpy._core, which numpy 1.x doesn't have: what Kelvin sends must be plain Python."""
import base64
import io
import math
import pickle
import pickletools
import sys
import types

import pytest

np = pytest.importorskip("numpy")
if int(np.__version__.split(".")[0]) < 2:
    pytest.skip("numpy 1.x reads its own pickles", allow_module_level=True)

from labharness import builtin_registry, rfsoc  # noqa: E402

# A board's poll_data() answer as QICK's Pyro server pickles it under numpy 1.x: [(points, ([I/Q sums per readout],
# stats))]. Made with numpy 1.20.3 (on Python 3.9.7), the nearest to the board's 1.21.5 on hand: numpy 1.x pickles
# arrays and numbers the same way. Protocol 4 as QICK sets Pyro4, and 5 (numpy.core.numeric._frombuffer).
OLD_READING = base64.b64decode(
    "gASV+wAAAAAAAABdlEsBXZSMFW51bXB5LmNvcmUubXVsdGlhcnJheZSMDF9yZWNvbnN0cnVjdJSTlIwFbnVtcHmUjAduZGFycmF5lJOUSwCFlEMB"
    "YpSHlFKUKEsBSwFLAoaUaAWMBWR0eXBllJOUjAJpOJSJiIeUUpQoSwOMATyUTk5OSv////9K/////0sAdJRiiUMQsAQAAAAAAAC48v///////5R0"
    "lGJhaAKMBnNjYWxhcpSTlGgOjAJmOJSJiIeUUpQoSwNoEk5OTkr/////Sv////9LAHSUYkMImpmZmZmZiT+UhpRSlGgXaBFDCAEAAAAAAAAAlIaU"
    "UpSGlIaUhpRhLg==")
OLD_READING_5 = base64.b64decode(
    "gAWV/AAAAAAAAABdlEsBXZSMEm51bXB5LmNvcmUubnVtZXJpY5SMC19mcm9tYnVmZmVylJOUKJYQAAAAAAAAALAEAAAAAAAAuPL///////+UjAVu"
    "dW1weZSMBWR0eXBllJOUjAJpOJSJiIeUUpQoSwOMATyUTk5OSv////9K/////0sAdJRiSwFLAoaUjAFDlHSUUpRhjBVudW1weS5jb3JlLm11bHRp"
    "YXJyYXmUjAZzY2FsYXKUk5RoCIwCZjiUiYiHlFKUKEsDaAxOTk5K/////0r/////SwB0lGJDCJqZmZmZmYk/lIaUUpRoFGgLQwgBAAAAAAAAAJSG"
    "lFKUhpSGlIaUYS4=")
I, Q = 1200, -3400
CFG = {"board": "RFSoC4x2", "sw_version": "0.2.422", "tprocs": [{"type": "qick_processor"}],
       "gens": [{"has_mixer": False}], "readouts": [{"tproc_ctrl": 0}]}


def named(blob):
    """The module.name of each class or function a pickle names (the first time it names it: enough here)."""
    out, strings = set(), []
    for op, arg, _ in pickletools.genops(blob):
        if op.name in ("SHORT_BINUNICODE", "BINUNICODE", "UNICODE"):
            strings.append(arg)
        elif op.name == "STACK_GLOBAL":
            out.add(".".join(strings[-2:]))
        elif op.name == "GLOBAL":
            out.add(arg.replace(" ", "."))
    return out


BUNDLED = ("numpy.core", "numpy.core._utils")   # all of numpy.core that the app's PyInstaller build took


def in_core(name):
    return name == "numpy.core" or name.startswith("numpy.core.")


class NoCoreModules:
    """An import finder with only the numpy.core modules the app Kelvin installed had; whole: no numpy.core at all, as
    a later numpy will have."""

    def __init__(self, whole):
        self.whole = whole

    def find_spec(self, name, path=None, target=None):
        if in_core(name) and (self.whole or name not in BUNDLED):
            raise ModuleNotFoundError("No module named %r" % name, name=name)
        return None


@pytest.fixture(params=[False, True], ids=["installed app", "no numpy.core"])
def packaged_numpy(request):
    """numpy 2 as the app Kelvin installs has it; afterwards, as it was."""
    before = {k: v for k, v in sys.modules.items() if in_core(k)}
    for k in before:
        if request.param or k not in BUNDLED:
            del sys.modules[k]
    finder = NoCoreModules(request.param)
    sys.meta_path.insert(0, finder)
    yield request.param
    sys.meta_path.remove(finder)
    for k in [k for k in sys.modules if in_core(k)]:
        del sys.modules[k]
    sys.modules.update(before)


class Numpy1Unpickler(pickle.Unpickler):
    """How numpy 1.x unpickles: it has numpy.core, and no numpy._core."""

    def find_class(self, module, name):
        if module == "numpy._core" or module.startswith("numpy._core."):
            raise ModuleNotFoundError("No module named 'numpy._core'", name="numpy._core")
        return super().find_class(module, name)


class PyroBoard:
    """A QICK board on numpy 1.21 behind its Pyro4 server: what Kelvin sends is pickled (protocol 4, as QICK sets
    Pyro4) and read as numpy 1.x reads it; what the board answers comes as numpy 1.x pickled it."""

    def __init__(self):
        self._pyroUri = "PYRO:obj_5a3c@192.168.3.1:41231"
        self._pyroTimeout = None
        self.got = []   # each call: its name, and its arguments as the board read them

    def _call(self, name, *args, **kwargs):
        sent = pickle.dumps((args, kwargs), protocol=4)
        self.got.append((name,) + Numpy1Unpickler(io.BytesIO(sent)).load())

    def get_cfg(self):
        self._call("get_cfg")
        return pickle.loads(pickle.dumps(CFG, protocol=4))

    def set_nyquist(self, ch, nqz):
        self._call("set_nyquist", ch, nqz)

    def configure_readout(self, ch, ro_regs):
        self._call("configure_readout", ch, ro_regs)

    def load_bin_program(self, binprog, load_mem=True):
        self._call("load_bin_program", binprog, load_mem=load_mem)

    def start_readout(self, total_shots, counter_addr=1, ch_list=None, reads_per_shot=1):
        self._call("start_readout", total_shots, counter_addr=counter_addr, ch_list=ch_list,
                   reads_per_shot=reads_per_shot)

    def poll_data(self):
        self._call("poll_data")
        return pickle.loads(OLD_READING)   # as Pyro4's pickle serializer reads the answer, in Kelvin

    def reset_gens(self):
        self._call("reset_gens")


def qick_over_pyro(board, numpy_args):
    """QICK's client as it works a board through Pyro4: make_proxy reads the board's config, a program's acquire sets
    the board up, loads its program, starts the readout and polls the data (qick_asm.py). numpy_args: what it sends
    is numbers and arrays from numpy, else Python's (QICK 0.2's own make most of them Python's)."""
    as_sent = (lambda x, kind: kind(x)) if numpy_args else (lambda x, kind: x)

    class AveragerProgramV2:
        def __init__(self, soccfg, reps, final_delay, cfg=None):
            self.cfg, self.reps = cfg, reps
            self._initialize(cfg)
            self._body(cfg)

        def __getattr__(self, name):   # declare_gen, add_pulse, trigger...: nothing goes to the board yet
            if name.startswith("_"):
                raise AttributeError(name)
            return lambda *a, **kw: None

        def acquire(self, soc, progress=True):
            cfg = self.cfg
            soc.set_nyquist(cfg["gen"], as_sent(cfg["nqz"], np.int64))
            soc.configure_readout(cfg["ro"], {"f_rounded": as_sent(cfg["frequency"], np.float64),
                                              "f_int": as_sent(1 << 20, np.int64)})
            pmem = [[0, 1, 2, 3, 4, 5, 6, 7]]
            soc.load_bin_program({"pmem": as_sent(pmem, lambda x: np.array(x, dtype=np.int32)), "wmem": None,
                                  "dmem": None}, load_mem=True)
            soc.start_readout(as_sent(self.reps, np.int64), counter_addr=1, ch_list=[cfg["ro"]], reads_per_shot=[1])
            [(points, (data, stats))] = soc.poll_data()
            assert data[0].shape == (points, 2)   # QICK reads the arrays' shapes, so they must stay arrays
            return [[data[0][0]]]

    return types.SimpleNamespace(make_proxy=lambda ns_host, ns_port, proxy_name: (board, board.get_cfg()),
                                 AveragerProgramV2=AveragerProgramV2, AveragerProgram=None, version="0.2.422")


def plain_python(value):
    if isinstance(value, dict):
        return all(plain_python(k) and plain_python(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return all(plain_python(v) for v in value)
    return type(value).__module__ == "builtins"


def test_the_reported_error_and_what_now_reads_the_board(packaged_numpy):
    with pytest.raises(ModuleNotFoundError, match=r"No module named 'numpy\.core(\.multiarray)?'"):
        pickle.loads(OLD_READING)   # what the report saw, in a Kelvin without the fix
    board = PyroBoard()
    card = builtin_registry().get("qick/rfsoc")
    rf = rfsoc.Qick("rf", card, "qick://192.168.3.1:8888/myqick", qick_over_pyro(board, numpy_args=False))
    assert rf.identity().startswith("QICK,RFSoC4x2,192.168.3.1:8888/myqick,tProc v2,board's qick 0.2.422")
    assert float(rf.query("GET i")) == I and float(rf.query("GET q")) == Q
    assert float(rf.query("GET magnitude")) == pytest.approx(math.hypot(I, Q))
    assert float(rf.query("GET phase")) == pytest.approx(math.degrees(math.atan2(Q, I)))
    assert [c[0] for c in board.got].count("poll_data") == 1           # the four values share one acquisition


def test_numpy_numbers_and_arrays_reach_a_numpy_1_board_as_plain_python():
    """numpy 2 pickles under numpy._core, which numpy 1.x doesn't have: the board couldn't read the call at all."""
    assert "numpy._core.multiarray.scalar" in named(pickle.dumps(np.int64(3), protocol=4))
    with pytest.raises(ModuleNotFoundError, match="numpy._core"):
        Numpy1Unpickler(io.BytesIO(pickle.dumps(np.float64(512.5), protocol=4))).load()
    board = PyroBoard()
    card = builtin_registry().get("qick/rfsoc")
    rf = rfsoc.Qick("rf", card, "qick://192.168.3.1:8888/myqick", qick_over_pyro(board, numpy_args=True))
    rf.write("SET frequency 512.5")
    assert float(rf.query("GET magnitude")) == pytest.approx(math.hypot(I, Q))
    calls = {c[0]: c[1:] for c in board.got}
    assert calls["set_nyquist"] == ((0, 1), {})
    assert calls["configure_readout"] == ((0, {"f_rounded": 512.5, "f_int": 1 << 20}), {})
    assert calls["load_bin_program"] == (({"pmem": [[0, 1, 2, 3, 4, 5, 6, 7]], "wmem": None, "dmem": None},),
                                         {"load_mem": True})
    assert calls["start_readout"][0] == (100,)
    assert all(plain_python(args) for _, *args in board.got)
    # the proxy's own attributes stay its own: QICK sends a pulse's samples as lists where the proxy has a _pyroUri,
    # and each call's timeout is the proxy's
    assert hasattr(rf.soc, "_pyroUri") and board._pyroTimeout == rfsoc.CALL_S
    rf.close()


@pytest.mark.parametrize("blob", [OLD_READING, OLD_READING_5], ids=["protocol 4", "protocol 5"])
def test_what_numpy_1_pickled_reads_as_the_same_arrays_and_numbers(packaged_numpy, blob):
    rfsoc.read_old_numpy()
    rfsoc.read_old_numpy()   # again: nothing changes
    [(points, (data, (seconds, count)))] = pickle.loads(blob)
    assert points == 1 and isinstance(data[0], np.ndarray) and data[0].dtype == np.int64
    assert data[0].tolist() == [[I, Q]] and seconds == np.float64(0.0125) and count == np.int64(1)
    assert sys.modules["numpy.core.multiarray"] is sys.modules["numpy._core.multiarray"]


def test_where_numpy_has_numpy_core_it_stays_numpys_own():
    """Python from source has numpy 2's numpy.core modules for old pickles: Kelvin leaves them as they are."""
    import importlib
    before = importlib.import_module("numpy.core.multiarray")
    rfsoc.read_old_numpy()
    assert sys.modules["numpy.core.multiarray"] is before
    assert pickle.loads(OLD_READING)[0][1][0][0].tolist() == [[I, Q]]


def test_plain_leaves_python_values_and_converts_numpys():
    sent = rfsoc.plain((1, "gen", 2.5, None, True, b"\x01",
                        {"pmem": np.array([[1, 2]], dtype=np.int32), np.int64(3): [np.float64(1.5), np.bool_(True)]}))
    assert sent == (1, "gen", 2.5, None, True, b"\x01", {"pmem": [[1, 2]], 3: [1.5, True]})
    assert plain_python(sent) and not any(n.startswith("numpy") for n in named(pickle.dumps(sent, protocol=4)))
