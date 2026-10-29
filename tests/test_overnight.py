"""Overnight durability (field tests, 2026-10: a queue re-asked in the night and declined after 5 minutes; a lost
instrument paused the queue with a raw VISA code and no alarm; garbled replies flapped as errors 42 times; off-desk
alerts needed a third-party webhook). Each check reproduces what testers met, on the virtual lab, a fake bus or labsim."""
import pytest

from labharness import ScriptedApprover
from simlab import build_virtual_lab
from labharness.monitor import Watch

SWEEP = """plan: {name}
setup: {{ smu.output: on }}
sweep: {{ smu.V: {{from: 0, to: 1, step: 0.5}} }}
settle: 0.1
measure: [smu.I]
after: {{ smu.V: 0, smu.output: off }}"""


def runtime(tmp_path, answers, lab=None):
    pytest.importorskip("mcp")
    from labharness.agent.runtime import LabRuntime
    return LabRuntime(lab or build_virtual_lab(seed=1), ScriptedApprover(list(answers)), tmp_path / "data", None)


def run_queue(rt):
    out = rt.queue_start()
    assert out.startswith("queue started"), out
    rt._queue_thread.join(120)
    return [(i.name, i.status, i.message) for i in rt.queue.items]


# ---------------------------------------------------------------------------------------------- the queue's approval


def test_a_queue_approval_covers_its_whole_path_so_nothing_asks_in_the_night(tmp_path):
    """The output was left on and the gate at 2 V when the queue started: the first item's end switched the output off,
    so the second item's switching it on was a new question at 3 a.m. (and the sweep up from 0 another), which nobody
    answered. The one approval covers every value the queue sets away from safe, wherever the drive is when it starts."""
    rt = runtime(tmp_path, [True])                                  # one answer: a second ask fails the item
    smu = rt.lab.instruments["smu"].backend
    smu.state.update(output=True, V=2.0)                            # left from the day's work
    rt.queue_add(SWEEP.format(name="first"))
    rt.queue_add(SWEEP.format(name="second"))
    assert run_queue(rt) == [("first", "done", ""), ("second", "done", "")]
    [card] = rt.gate.approver.requests
    assert "smu.output" in str(card) and "smu.V" in str(card)       # both on the one card, though both were on now
    assert rt.queue.state == "idle"


def test_the_queue_approval_covers_what_a_plan_sets_after_a_stop(tmp_path):
    """A plan's on_abort runs after a stop or a failure, in the night too: the one approval covers what it sets."""
    rt = runtime(tmp_path, [True])
    rt.queue_add(SWEEP.format(name="first") + "\non_abort: { smu.V: 0.5 }")
    out = rt.queue_start(dry_run=True)
    assert out.startswith("REHEARSAL ONLY")
    lines, grants, _ = rt.gate.plan_approval(rt._queue_drives(rt.queue.waiting()), whole_path=True)
    assert grants["smu.V"].covers(0.5) and grants["smu.output"].covers(True)


INTERLOCKED = """plan: field_points
sweep: { magnet.B: {values: [0.1, 0.2]} }
measure: [lockin.X]
after: { magnet.B: 0 }"""

COOL = """step: cool to 2 K
set: { tc.setpoint: 2 }
stabilize: { read: tc.T_sample, within: 0.05, for: 20, timeout: 900 }"""


def warm(rt, kelvin):
    tc = rt.lab.instruments["tc"].backend
    tc.state.update(heater_range="medium", setpoint=kelvin, T_sample=kelvin)


def test_a_queue_that_cools_first_starts_while_the_sample_is_still_warm(tmp_path):
    """The magnet's interlock needs the sample below 10 K; the queue cools to 2 K first. Checked against the lab now,
    the field sweep was refused and the queue couldn't start (a tester fell back to four approvals in the night). Its
    interlock is now checked when the field moves, after the cool-down, and the card says so."""
    rt = runtime(tmp_path, [True])
    warm(rt, 12.0)
    rt.queue_add(COOL)
    rt.queue_add(INTERLOCKED)
    assert run_queue(rt) == [("cool to 2 K", "done", ""), ("field_points", "done", "")]
    [card] = rt.gate.approver.requests
    assert "its interlock needs tc.T_sample < 10, which doesn't hold now (now 12" in str(card)


def test_an_interlock_that_still_fails_when_its_item_comes_pauses_the_queue_there(tmp_path):
    """Left for later is still checked: a cool-down that doesn't get below 10 K stops the queue at the field sweep."""
    rt = runtime(tmp_path, [True])
    warm(rt, 12.0)
    rt.queue_add(COOL.replace("setpoint: 2", "setpoint: 11").replace("cool to 2 K", "cool to 11 K"))
    rt.queue_add(INTERLOCKED)
    (_, first, _), (_, second, why) = run_queue(rt)
    assert first == "done" and second == "failed" and "interlock: magnet.B needs tc.T_sample < 10" in why
    assert rt.queue.state == "paused" and rt.lab.read("magnet", "B") == 0


def test_a_field_sweep_first_in_the_queue_is_still_refused_at_the_start(tmp_path):
    """Only an item before the first one that moves the drive counts: one that sweeps the field first can't start."""
    rt = runtime(tmp_path, [])
    warm(rt, 12.0)
    rt.queue_add(INTERLOCKED)
    rt.queue_add(COOL)
    rt.queue_add(INTERLOCKED.replace("field_points", "field_again"))
    out = rt.queue_start()
    assert out.startswith("the queue did not start") and "interlock: magnet.B needs tc.T_sample < 10" in out


# ---------------------------------------------------------------------------------------------- instruments that are lost


class VisaError(Exception):
    """What PyVISA raises: VisaIOError, with the status code."""

    def __init__(self, code, name, text):
        super().__init__("%s (%d): %s" % (name, code, text))
        self.error_code = code


NLISTENERS = (-1073807265, "VI_ERROR_NLISTENERS", "No listeners condition is detected (both NRFD and NDAC are deasserted).")
TMO = (-1073807339, "VI_ERROR_TMO", "Timeout expired before operation completed.")


def test_a_lost_instrument_is_said_in_plain_words_with_its_bus_and_what_to_check():
    """l09 got "queue paused" with a raw VI_ERROR_NLISTENERS and no instrument named. The plan's words: "the 2182A
    doesn't answer on GPIB: cable or power?"."""
    from labharness.adapters import lost_kind, short_model, where_and_cause
    assert where_and_cause("GPIB0::7::INSTR") == ("on GPIB (address 7)", "cable or power?")
    assert where_and_cause("USB0::0xB506::0x2000::003456::INSTR") == ("on USB", "USB cable or power?")
    assert where_and_cause("TCPIP0::192.168.1.20::inst0::INSTR")[0] == "on the network (192.168.1.20)"
    assert where_and_cause("serial://COM3")[0] == "on COM3" and "baud rate" in where_and_cause("ASRL3::INSTR")[1]
    assert where_and_cause("qd://127.0.0.1:5000") == ("through MultiVu at 127.0.0.1:5000",
                                                      "is MultiVu running, with its server started?")
    assert where_and_cause("sim://lockin") == ("", "")
    assert short_model("Keithley 2182 / 2182A nanovoltmeter") == "Keithley 2182 / 2182A nanovoltmeter"
    assert short_model("Quantum Design PPMS / DynaCool / VersaLab / MPMS3 (MultiVu)") == "Quantum Design PPMS / DynaCool"
    assert lost_kind(VisaError(*NLISTENERS)) == "gone" and lost_kind(VisaError(*TMO)) == "silent"
    assert lost_kind(Exception("timed out waiting for the device to answer")) == "silent"   # MultiPyVu
    assert lost_kind(ConnectionResetError()) == "gone"
    assert lost_kind(ValueError("V needs a number, got 'k5.14'")) is None   # it answered: not a lost instrument


def real_twin(lab, name):
    twin = lab.instruments[name].backend
    twin.simulated = False   # as a real instrument: the monitor checks it
    return twin


def test_an_instrument_nothing_reads_is_checked_while_work_runs_and_alarms_once(lab, make_gate):
    """l09 unplugged the 2182A for 2 min 40 s during a cool-down: nothing read it, so nothing noticed. While a plan
    or a queue item runs, every real instrument is asked to answer; one that can't be reached at all alarms at once,
    in words, and once; it clears when it answers again."""
    from labharness.monitor import Monitor
    gate, _ = make_gate([])
    notices = []
    monitor = Monitor(lab, gate, lambda kind, title, text="": notices.append((kind, title)))
    twin = real_twin(lab, "lockin")
    lab.instruments["lockin"].config.address = "GPIB0::7::INSTR"
    real = twin.query

    def unplugged(command):
        raise VisaError(*NLISTENERS)
    with gate.preapproved({}):                                      # a run is going
        monitor.tick()
        assert not monitor.alarms
        twin.query = unplugged
        lab.clock.sleep(21)
        monitor.tick()
        [alarm] = monitor.alarms
        assert alarm.kind == "silent" and alarm.text == (
            "lockin (Virtual Lock-in Amplifier) doesn't answer on GPIB (address 7): cable or power? "
            "[VI_ERROR_NLISTENERS]")
        for _ in range(3):
            lab.clock.sleep(21)
            monitor.tick()
        assert len(monitor.alarms) == 1 and [k for k, _ in notices] == ["alarm"]   # once, not at every check
        twin.query = real
        lab.clock.sleep(21)
        monitor.tick()
    assert alarm.cleared and notices[-1][1].startswith("Cleared: lockin answers again (it didn't for ")


def test_nothing_is_asked_of_an_instrument_while_no_work_runs(lab, make_gate):
    from labharness.monitor import Monitor
    gate, _ = make_gate([])
    monitor = Monitor(lab, gate, lambda *a: None)
    twin = real_twin(lab, "lockin")
    sent = []
    real = twin.query
    twin.query = lambda c: sent.append(c) or real(c)
    for _ in range(3):
        lab.clock.sleep(21)
        monitor.tick()
    assert not any("IDN" in c for c in sent)                        # idle: only the minute's error-queue check


def test_an_output_that_a_run_switched_on_and_that_restarted_off_stops_the_run(lab, make_gate):
    """A 1 s power cut on the 6221 went unnoticed: it came back with its output off and the queue measured on, at no
    current. An output the running plan switched on that reads off twice, a few seconds apart, alarms once and stops
    the run."""
    from labharness.monitor import RESET_EVERY, Monitor
    gate, _ = make_gate([True])
    notices = []
    monitor = Monitor(lab, gate, lambda kind, title, text="": notices.append((kind, title, text)))
    smu = real_twin(lab, "smu")
    with gate.preapproved({}):
        assert gate.set("smu", "output", True).ok                    # the run switched it on
        monitor.tick()
        assert not monitor.alarms
        smu.state["output"] = False                                  # a power cut: back with its power-on settings
        generation = gate.stop_generation
        lab.clock.sleep(RESET_EVERY)
        monitor.tick()
        assert not monitor.alarms                                    # once may be a switch still following
        lab.clock.sleep(RESET_EVERY)
        monitor.tick()
        [alarm] = monitor.alarms
        assert alarm.kind == "reset" and "smu.output is off, though the running plan switched it on" in alarm.text
        assert gate.stop_generation == generation + 1 and "stopped the running plan" in alarm.actions[0]
        lab.clock.sleep(RESET_EVERY)
        monitor.tick()
        assert len(monitor.alarms) == 1 and len(notices) == 1


def test_a_lost_instrument_that_ends_a_queue_item_alarms_at_once_and_names_itself_in_the_pause(tmp_path):
    rt = runtime(tmp_path, [])
    rt.queue.add("plan: look\ntime: { every: 1, points: 3 }\nmeasure: [lockin.X]")
    rt.lab.instruments["lockin"].backend.fault = "offline"           # it stopped answering
    rt.queue.state = "running"
    rt._queue_loop({}, "test")
    [item] = rt.queue.items
    assert item.status == "failed" and "lockin (Virtual Lock-in Amplifier) doesn't answer" in item.message
    assert rt.queue.state == "paused" and "lockin (Virtual Lock-in Amplifier) doesn't answer" in rt.queue.reason
    [alarm] = rt.monitor.active()                                    # at once, not after three more failed reads
    assert alarm.kind == "silent" and alarm.key == "lockin"
    rt.monitor.tick()
    assert len(rt.monitor.alarms) == 1


# ---------------------------------------------------------------------------------------------- garbled replies


def garbling(twin, every=2):
    """Its replies garbled as a bad cable does: one in `every` comes through clean, the others with characters
    flipped ("0,"No error"" arrives as "0$"No!errov"", a number with a control character in it)."""
    real, n = twin.query, [0]

    def query(command):
        reply = real(command)
        n[0] += 1
        if n[0] % every == 0:
            return reply
        if reply.startswith("0,"):
            return '0$"No!errov"'
        return reply[:2] + "\x0b" + reply[3:]
    twin.query = query
    return real


def test_garbled_replies_raise_one_comms_fault_that_holds_until_they_are_clean(lab, make_gate):
    """l09 garbled the 2182A's replies for 2.7 h: "nvm reports an error: 0$"No!errov"" was raised and cleared about
    42 times. Garbled replies are one comms fault: raised at the first, kept while they come, cleared once they have
    been clean for five minutes; never an instrument error, never an unreadable reading."""
    from labharness.monitor import GARBLE_CLEAR_S, Monitor
    gate, _ = make_gate([])
    notices = []
    monitor = Monitor(lab, gate, lambda kind, title, text="": notices.append((kind, title)))
    twin = real_twin(lab, "lockin")
    monitor.add(Watch("lockin.X", above=1, every=0))
    monitor.tick()                                                   # clean: its replies are known
    real = garbling(twin)
    for _ in range(42):                                              # a night of it, a check a minute
        lab.clock.sleep(61)
        monitor.tick()
    [alarm] = monitor.alarms
    assert alarm.kind == "comms" and alarm.text.startswith("lockin's reply to ") and "arrived garbled" in alarm.text
    assert "a loose cable, a bad adapter or electrical noise" in alarm.text and not alarm.cleared
    assert [k for k, _ in notices] == ["alarm"]
    twin.query = real
    lab.clock.sleep(61)
    monitor.tick()
    assert not alarm.cleared                                         # clean once is not clean
    for _ in range(int(GARBLE_CLEAR_S // 61) + 1):
        lab.clock.sleep(61)
        monitor.tick()
    assert alarm.cleared and len(monitor.alarms) == 1
    assert notices[-1][1].startswith("Cleared: lockin's replies are clean again (none garbled for 5 min")


def test_a_garbled_reading_stops_a_run_in_words_and_a_garbled_error_check_fails_a_set(lab, make_engine):
    from labharness import load_plan
    from labharness.instrument import Garbled
    engine, gate, _ = make_engine([])
    twin = lab.instruments["lockin"].backend
    assert isinstance(lab.read("lockin", "X"), float)                 # it reads, as it always has
    assert isinstance(lab.read("lockin", "frequency"), float)
    garbling(twin, every=10 ** 6)
    with pytest.raises(Garbled, match="lockin's reply to .* arrived garbled"):
        lab.read("lockin", "X")
    report = engine.run(load_plan("plan: look\ntime: { every: 1, points: 3 }\nmeasure: [lockin.X]"))
    assert report.status == "refused" and "arrived garbled" in report.message   # its check before the run says it
    r = gate.set("lockin", "frequency", 1234)                         # where it is now can't be known: no set
    assert not r.ok and "arrived garbled" in r.message


def test_a_reading_its_card_never_read_is_the_cards_problem_not_a_garbled_reply(lab):
    """The first reply a reading ever gets that can't be read, with nothing but printable characters, is the card's
    problem (a wrong command or format), said as before: only one that read before, or has noise in it, is garbled."""
    from labharness.instrument import Garbled
    twin = lab.instruments["lockin"].backend
    real = twin.query
    twin.query = lambda c: "OVLD" if c.upper().startswith("MEAS") or "X" in c.upper() else real(c)
    with pytest.raises(ValueError) as e:
        lab.read("lockin", "X")
    assert not isinstance(e.value, Garbled)


# ---------------------------------------------------------------------------------------------- telling people away


def test_a_request_nobody_answers_goes_to_the_phone_instead_of_being_declined():
    """In the night a card was declined after 5 minutes, which ended the queue. Unanswered at the computer, it now goes
    to the people away from it and waits for them; answered from the phone, it goes ahead."""
    import threading
    from labharness.agent.approval import WebApprover
    from labharness.gate import ApprovalRequest
    approver = WebApprover(timeout=0.3, escalate_s=30)
    told = []
    approver.escalate = lambda r: told.append(r) or True
    answers = []
    t = threading.Thread(target=lambda: answers.append(approver.approve(ApprovalRequest("Run the queue", ["x"]))))
    t.start()
    entry = approver.wait_for_pending(timeout=5)
    import time
    time.sleep(0.8)                                                  # past the first wait: it went to the phone
    assert len(told) == 1 and told[0]["title"] == "Run the queue" and told[0]["escalated"]
    [pending] = approver.pending_list()
    assert pending["escalated"] and 20 < pending["seconds_left"] <= 30
    assert approver.answer(entry["id"], True, "human (phone Pixel)", {"channel": "phone", "device": "Pixel"})
    t.join(5)
    assert answers == [True]


def test_a_request_nobody_can_be_reached_for_is_declined_as_before_and_says_so():
    from labharness.agent.approval import WebApprover
    from labharness.gate import ApprovalRequest
    approver = WebApprover(timeout=0.2, escalate_s=30)
    approver.escalate = lambda r: False                               # no phone, email or webhook
    request = ApprovalRequest("Run the queue", ["x"])
    assert approver.approve(request) is False and request.note == "no answer within 0 s"
    approver = WebApprover(timeout=0.2, escalate_s=0.3)
    approver.escalate = lambda r: True
    request = ApprovalRequest("Run the queue", ["x"])
    assert approver.approve(request) is False
    assert request.note.startswith("no answer within 0 s at the computer, nor in ") and "after it went to the phone" \
        in request.note


def test_the_runtime_asks_people_away_only_when_it_can_reach_them(tmp_path):
    from labharness.agent.approval import WebApprover
    pytest.importorskip("mcp")
    from labharness.agent.runtime import LabRuntime
    rt = LabRuntime(build_virtual_lab(seed=1), WebApprover(timeout=5, escalate_s=60), tmp_path / "data", None)
    assert rt.approver.escalate == rt._escalate
    request = {"title": "Run the queue: 3 scans", "items": ["smu.V: now 0 V, plan uses 0 … 1 V"], "until": 0}
    assert rt._escalate(request) is False                            # nobody to reach: declined as before
    rt.notifier.phones = lambda: True                                # a paired phone
    assert rt._escalate(request) is True
    notice = rt.notifier.notices[-1]
    assert notice["kind"] == "alarm" and notice["title"] == "Waiting for your answer: Run the queue: 3 scans"
    assert "Kelvin waits until" in notice["text"] and "smu.V" in notice["text"]
    assert any(e["type"] == "approval_escalated" for e in rt.gate.log.events())


def test_email_notices_go_out_without_credentials_in_any_words():
    from labharness.notify import Notifier, email_from
    sent = []
    mail = email_from("smtp.example.edu", "pi@example.edu, me@example.edu", "", "starttls", "kelvin-lab", "hunter2",
                      "kelvin@example.edu")
    assert mail.port == 587 and mail.to == ["pi@example.edu", "me@example.edu"] and "hunter2" not in repr(mail)
    with pytest.raises(ValueError, match="not an email address"):
        email_from("smtp.example.edu", "pi at example")
    assert email_from("", "pi@example.edu") is None and email_from("smtp.example.edu", "") is None   # off
    n = Notifier(mail=lambda cfg, subject, text: sent.append((cfg.host, subject, text)))
    n.configure(None, ["alarm"], email=mail)
    assert n.reaches_away("alarm") and not n.reaches_away("run")
    n("alarm", "Alarm: nvm doesn't answer", "cable or power?")
    n("run", "Run finished")                                          # not ticked: not sent
    assert n.test() == "sent"
    assert sent[0] == ("smtp.example.edu", "Alarm: nvm doesn't answer", "cable or power?") or len(sent) == 2

    def refused(cfg, subject, text):
        raise OSError("535 5.7.8 authentication failed for kelvin-lab with password hunter2")
    n = Notifier(mail=refused)
    n.configure(None, ["alarm"], email=mail)
    said = n.test()
    assert said.startswith("could not send the email: 535") and "hunter2" not in said and "kelvin-lab" not in said


def test_the_heartbeat_pings_while_kelvin_watches_and_stops_when_it_doesnt():
    from labharness.notify import Notifier
    pinged = []
    n = Notifier(ping=pinged.append)
    alive = [True]
    n.alive = lambda: alive[0]
    with pytest.raises(ValueError, match="heartbeat"):
        n.configure(None, heartbeat="ftp://example.org")
    n.configure(None, heartbeat="https://hc-ping.com/abc", heartbeat_s=60)
    import time
    end = time.time() + 5
    while not pinged and time.time() < end:
        time.sleep(0.05)
    assert pinged == ["https://hc-ping.com/abc"] and n.heartbeat_s == 60   # at once, then every minute
    alive[0] = False                                                  # the monitor stopped: no more pings
    assert n.test() == "heartbeat: not pinged: Kelvin isn't watching the lab" and len(pinged) == 1
    n.configure(None)                                                 # off
    assert n.heartbeat is None


def test_the_settings_keep_email_heartbeat_and_how_long_a_request_waits(tmp_path):
    pytest.importorskip("starlette")
    from labharness.web.settings import Settings
    s = Settings(tmp_path / "settings.json")
    assert s.email() is None and s.approval_wait_s == 3600 and s.heartbeat_s == 300   # email off; an hour; 5 min
    s.update({"smtp_host": "smtp.example.edu", "smtp_to": "pi@example.edu", "smtp_user": "lab",
              "smtp_password": "hunter2", "smtp_security": "SSL", "heartbeat": "https://hc-ping.com/abc",
              "heartbeat_minutes": "15", "approval_wait": "off"})
    mail = s.email()
    assert mail.host == "smtp.example.edu" and mail.security == "ssl" and mail.port == 465 and mail.password == "hunter2"
    assert s.approval_wait_s == 0 and s.heartbeat_s == 900
    public = s.public()
    assert public["email"]["host"] == "smtp.example.edu" and public["keys"]["smtp_password"] == "settings"
    assert "hunter2" not in str(public) and "hc-ping" not in str(public)   # secrets are never shown
    with pytest.raises(ValueError, match="port"):
        s.update({"smtp_port": "twenty"})
    with pytest.raises(ValueError, match="heartbeat"):
        s.update({"heartbeat": "hc-ping.com/abc"})


# ---------------------------------------------------------------------------------------------- the morning's account


def test_the_morning_summary_says_what_was_done_what_stopped_and_every_alarm(tmp_path):
    """l09's morning summary came from the model: an unplug "when you replugged", a wrong set point, watches it said
    were there. The queue now keeps its own account, from the record alone, and sends it when it ends."""
    rt = runtime(tmp_path, [True])
    rt.queue_add(SWEEP.format(name="first"))
    rt.queue_add("plan: look\ntime: { every: 1, points: 3 }\nmeasure: [lockin.X]")
    rt.queue_add(SWEEP.format(name="third"))
    real = rt.lab.instruments["lockin"].backend.query
    n = [0]

    def dies(command):   # answers through the first scan, then stops
        n[0] += 1
        if rt.queue.items[0].status == "done":
            raise TimeoutError("lockin: offline")
        return real(command)
    rt.lab.instruments["lockin"].backend.query = dies
    run_queue(rt)
    lines = rt.queue.last["lines"]
    assert lines[0].startswith("The queue ran ") and " and paused: look: " in lines[0]
    assert lines[1].startswith("Done: #1 first (3 points, run ")
    assert lines[2].startswith("Stopped with no data: #2 look: ") and "lockin (Virtual Lock-in Amplifier) doesn't " \
        "answer" in lines[2]
    assert lines[3] == "Not run: #3 third"
    assert lines[4].startswith("Alarms while it ran: ") and lines[4].count("doesn't answer") == 1 \
        and lines[4].endswith("(still on)")
    notice = rt.notifier.notices[-1]
    assert notice["title"] == "Queue paused" and notice["text"].splitlines() == lines
    text = rt.queue_show()
    assert "last run:" in text and "  Not run: #3 third" in text
    from labharness.scanqueue import ScanQueue
    assert ScanQueue(rt.data_dir / "queue.json").last["lines"] == lines   # kept for the morning, across a restart
