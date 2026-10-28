"""labsim's command line.

  python -m labsim up BENCH.yaml --home DIR      start the bench's server in the background
  python -m labsim serve BENCH.yaml --home DIR   run it here
  python -m labsim down                          stop it
  python -m labsim status                        instruments, what they are doing, the world, recent events
  python -m labsim log NAME [N]                  what NAME was sent and answered
  python -m labsim power NAME on|off             switch an instrument off or on (it comes back reset)
  python -m labsim unplug NAME | plug NAME       pull or put back its cable
  python -m labsim fault NAME FAULT [on|off]     dead, garble, slow, noisy, sensor_openA, heater_open, ...
  python -m labsim hold NAME|RESOURCE [on|off]   another program holds it (opening gives "resource busy")
  python -m labsim set PATH VALUE                change the world: cryo.stage 4.2, sample.damaged none ...
  python -m labsim env                           the environment variables that point a program at the bench
  python -m labsim scan                          what the labsim VISA library lists, and the COM ports
  python -m labsim models                        the instrument models a bench can use
The bench folder comes from --home or LABSIM_HOME.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHIMS = os.path.join(ROOT, "shims")


def _home(args) -> str:
    h = getattr(args, "home", None) or os.environ.get("LABSIM_HOME")
    if not h:
        sys.exit("say which bench: --home DIR or LABSIM_HOME")
    return os.path.abspath(h)


def _link(home: str):
    from .link import Link
    return Link(home)


def _ctl(home: str, cmd: str, *args: str) -> dict:
    from .link import LabsimError
    try:
        r = _link(home).call("ctl", cmd=cmd, args=list(args))
    except LabsimError as e:
        sys.exit(str(e))
    if "err" in r:
        sys.exit("labsim: %s" % r.get("msg", r["err"]))
    return r


def env_lines(home: str) -> dict:
    sep = os.pathsep
    return {"LABSIM_HOME": home, "KELVIN_VISA_BACKEND": "@labsim",
            "PYTHONPATH": sep.join([SHIMS, ROOT] + ([os.environ["PYTHONPATH"]] if os.environ.get("PYTHONPATH") else []))}


def cmd_up(args) -> None:
    home = _home(args)
    os.makedirs(home, exist_ok=True)
    info_path = os.path.join(home, "server.json")
    if os.path.exists(info_path):
        try:
            _link(home).must("hello")
            sys.exit("a labsim server is already running for %s (python -m labsim down first)" % home)
        except Exception:
            os.remove(info_path)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([ROOT] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    log = open(os.path.join(home, "server.log"), "a", encoding="utf-8")
    flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0
    p = subprocess.Popen([sys.executable, "-u", "-m", "labsim", "serve", os.path.abspath(args.bench), "--home", home,
                          "--port", str(args.port)], stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         env=env, creationflags=flags, close_fds=True, start_new_session=os.name != "nt")
    for _ in range(200):
        time.sleep(0.1)
        if p.poll() is not None:
            log.close()
            sys.exit("the server stopped at once; see %s" % os.path.join(home, "server.log"))
        if os.path.exists(info_path):
            try:
                hello = _link(home).must("hello")
                with open(info_path, encoding="utf-8") as f:
                    pid = json.load(f)["pid"]   # the server itself (a venv's python.exe starts it as a child)
                print("labsim bench %r is up (pid %d); folder %s" % (hello["name"], pid, home))
                for k, v in env_lines(home).items():
                    print("  %s=%s" % (k, v))
                return
            except Exception:
                pass
    sys.exit("the server didn't come up; see %s" % os.path.join(home, "server.log"))


def cmd_serve(args) -> None:
    from .server import serve
    serve(args.bench, _home(args), args.port)


def cmd_down(args) -> None:
    home = _home(args)
    path = os.path.join(home, "server.json")
    try:
        with open(path, encoding="utf-8") as f:
            pid = json.load(f)["pid"]
    except OSError:
        sys.exit("no server.json in %s" % home)
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
        else:
            os.kill(pid, 15)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    print("stopped the labsim server (pid %d)" % pid)


def _short(d: dict) -> str:
    out = []
    for k, v in d.items():
        if isinstance(v, float):
            v = "%.4g" % v
        elif isinstance(v, (list, tuple)):
            v = "(" + ", ".join("%.4g" % x if isinstance(x, float) else str(x) for x in v) + ")"
        elif isinstance(v, dict):
            v = "{" + ", ".join("%s: %s" % (a, ("%.4g" % b if isinstance(b, float) else b)) for a, b in v.items()) + "}"
        out.append("%s=%s" % (k, v))
    return " ".join(out)


def cmd_status(args) -> None:
    r = _ctl(_home(args), "status")
    if args.json:
        print(json.dumps(r, indent=1, default=str))
        return
    print("bench %s, up %d s, %d open sessions%s" % (r["bench"], r["uptime_s"], r["sessions"],
                                                   ("; held: " + ", ".join(r["held"])) if r["held"] else ""))
    for i in r["instruments"]:
        flags = ("" if i["powered"] else " OFF") + ("".join(" [%s]" % f for f in i["faults"]))
        flags += " (%d errors queued)" % i["errors"] if i["errors"] else ""
        print("  %-10s %-18s %s%s" % (i["name"], i["model"], ", ".join(i["addresses"]), flags))
        if i["status"]:
            print("             " + _short(i["status"]))
    w = dict(r["world"])
    events = w.pop("events", [])
    print("world: " + _short(w))
    for t, e in events:
        print("  %s %s" % (t, e))


def cmd_log(args) -> None:
    r = _ctl(_home(args), "log", args.name, str(args.n))
    for t, d, text in r["lines"]:
        print("%s %s %s" % (t, d, text))


def cmd_simple(args) -> None:
    extra = [a for a in (getattr(args, "state", None),) if a]
    r = _ctl(_home(args), args.cmd, *([args.name] + ([args.fault] if args.cmd == "fault" else []) + extra))
    print(r.get("msg") or "ok")


def cmd_set(args) -> None:
    print(_ctl(_home(args), "set", args.path, args.value)["msg"])


def cmd_env(args) -> None:
    for k, v in env_lines(_home(args)).items():
        if args.shell == "powershell":
            print('$env:%s = "%s"' % (k, v))
        else:
            print("export %s='%s'" % (k, v))


def cmd_scan(args) -> None:
    home = _home(args)
    link = _link(home)
    print("VISA resources:")
    for r in link.must("resources")["resources"]:
        print("  " + r)
    print("COM ports:")
    for p in link.must("ports")["ports"]:
        print("  %s  %s%s" % (p["device"], p["description"], ("  USB %s" % p["usb"]) if p["usb"] else ""))
    for kind in ("pylon", "andor", "andor2", "shamrock", "timetagger", "zhinst", "ppms", "nidaq", "seabreeze"):
        devs = link.must("sdk_list", kind=kind)["devices"]
        for d in devs:
            print("%s device: %s (%s)" % (kind, d["serial"], d["name"]))


def cmd_models(args) -> None:
    from .models import MODELS
    for name, cls in sorted(MODELS.items()):
        print("  %-22s %s" % (name, (cls.__doc__ or "").strip().split("\n")[0][:90]))


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="labsim", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--home", help="the bench folder (default: LABSIM_HOME)")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("up", "serve"):
        s = sub.add_parser(name)
        s.add_argument("bench")
        s.add_argument("--home", default=argparse.SUPPRESS)
        s.add_argument("--port", type=int, default=0)
        s.set_defaults(fn=cmd_up if name == "up" else cmd_serve)
    s = sub.add_parser("down"); s.add_argument("--home", default=argparse.SUPPRESS); s.set_defaults(fn=cmd_down)
    s = sub.add_parser("status"); s.add_argument("--home", default=argparse.SUPPRESS); s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_status)
    s = sub.add_parser("log"); s.add_argument("name"); s.add_argument("n", nargs="?", type=int, default=40)
    s.add_argument("--home", default=argparse.SUPPRESS); s.set_defaults(fn=cmd_log)
    for name in ("power", "unplug", "plug", "hold"):
        s = sub.add_parser(name); s.add_argument("name"); s.add_argument("--home", default=argparse.SUPPRESS)
        if name in ("power", "hold"):
            s.add_argument("state", nargs="?" if name == "hold" else None, choices=["on", "off"])
        s.set_defaults(fn=cmd_simple)
    s = sub.add_parser("fault"); s.add_argument("name"); s.add_argument("fault")
    s.add_argument("state", nargs="?", choices=["on", "off"], default="on"); s.add_argument("--home", default=argparse.SUPPRESS)
    s.set_defaults(fn=cmd_simple)
    s = sub.add_parser("set"); s.add_argument("path"); s.add_argument("value"); s.add_argument("--home", default=argparse.SUPPRESS)
    s.set_defaults(fn=cmd_set)
    s = sub.add_parser("env"); s.add_argument("--home", default=argparse.SUPPRESS); s.add_argument("--shell", choices=["bash", "powershell"],
                                                                      default="bash")
    s.set_defaults(fn=cmd_env)
    s = sub.add_parser("scan"); s.add_argument("--home", default=argparse.SUPPRESS); s.set_defaults(fn=cmd_scan)
    s = sub.add_parser("models"); s.set_defaults(fn=cmd_models)
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
