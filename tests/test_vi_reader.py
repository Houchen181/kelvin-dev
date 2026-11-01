"""LabVIEW's own files read as bytes, never run (vifile.py, vigraph.py): the resource container, the linkage, the
front panel and block diagram heaps, and the VI summary made from them, on public VIs from several LabVIEW versions
(tests/fixtures/labview, with their sources and licences in SOURCES.txt), and on files built here byte by byte."""
import hashlib
import json
import os
import random
import struct
from pathlib import Path

import pytest

from labharness import labview, vifile, vigraph, viscan
from labharness.homelab import builtin_registry

FIXTURES = Path(__file__).parent / "fixtures" / "labview"
AIM = FIXTURES / "aim_tti_cpx400sp" / "Public"
KEYSIGHT = FIXTURES / "keysight_u2722"
JKI = FIXTURES / "jki_state_machine" / "Three Button Dialog CORE - JKI.vi"
READS = ("no", "names", "panel", "full")


def walk(nodes):
    return list(vigraph.walk(nodes))


def rsrc(blocks, kind=b"LVIN"):
    """A resource file laid out as LabVIEW writes one: its header, each section's data (a 4-byte length first), the
    header again, the list of blocks, each block's sections, and the sections' names. blocks: [(ident, [(index,
    name, data)])]."""
    data, entries = b"", []
    for ident, sections in blocks:
        placed = []
        for index, name, body in sections:
            placed.append((index, name, len(data)))
            data += struct.pack(">I", len(body)) + body
            data += b"\x00" * (-len(data) % 4)
        entries.append((ident, placed))
    heads, starts, names = b"", b"", b""
    for ident, placed in entries:
        heads += struct.pack(">4sII", ident, len(placed) - 1, 4 + 12 * len(entries) + len(starts))
        for index, name, at in placed:
            name_at = 0xFFFFFFFF
            if name:
                name_at = len(names)
                names += bytes([len(name)]) + name.encode()
            starts += struct.pack(">iIIII", index, name_at, 0, at, 0)
    info = struct.pack(">I", len(entries) - 1) + heads + starts + names
    head = b"RSRC\r\n" + struct.pack(">H4s4sIIII", 3, kind, b"LBVW", 32 + len(data), 52 + len(info), 32, len(data))
    return head + data + head + struct.pack(">IIIII", 0, 0, 32, 52, len(info)) + info


def entry(tag, scope=1, cls=None, uid=None, content=None):
    """One heap entry: scope 0 opens an object, 1 is one with nothing inside, 2 closes the last opened."""
    attrs = [(k, v) for k, v in ((-2, cls), (-3, uid)) if v is not None]
    size = 0 if content is None else len(content) if len(content) <= 4 else 6
    raw = tag + 31
    out = bytes([(size << 5) | (bool(attrs) << 4) | (scope << 2) | (raw >> 8), raw & 0xFF])
    if attrs:
        out += bytes([len(attrs)]) + b"".join(struct.pack(">bh", k, v) for k, v in attrs)
    if size == 6:
        out += bytes([len(content)])
    return out + (content or b"")


def heap_block(entries):
    body = b"".join(entries)
    heap = struct.pack(">I", len(body)) + body
    import zlib
    return struct.pack(">I", len(heap)) + zlib.compress(heap)


def pascal(text):
    return bytes([len(text)]) + text.encode()


def link(ident, names, path):
    """A link as linkage blocks keep one: four letters, the qualified name, a PTH0 path."""
    qualified = struct.pack(">I", len(names)) + b"".join(pascal(n) for n in names)
    parts = b"".join(pascal(p) for p in path)
    pth = b"PTH0" + struct.pack(">IHH", 4 + len(parts), 1 if path and not path[0].startswith("<") else 0,
                                len(path)) + parts
    return ident + qualified + (b"\x00" if len(qualified) % 2 else b"") + pth + b"\x00" * 4


# ------------------------------------------------------------------ the file, as bytes

def test_a_resource_file_reads_into_its_blocks_and_their_sections():
    raw = rsrc([(b"vers", [(4, "", b"\x18\x00\x80\x07\x00\x00\x0418.0\x00")]),
                (b"UCRF", [(0, "Inner.vi", b"inner bytes"), (1, "Other.vi", b"more")])], kind=b"LVAR")
    c = vifile.container(raw)
    assert c.kind == "library"
    assert [(s.index, s.name, s.data) for s in c.blocks["UCRF"]] == [(0, "Inner.vi", b"inner bytes"),
                                                                     (1, "Other.vi", b"more")]
    assert vifile.version_words(0x18008007) == ("18.0", "LabVIEW 2018 (18.0)")
    assert vifile.version_words(0x09008000) == ("9.0", "LabVIEW 2009 (9.0)")
    assert vifile.version_words(0x13018020)[1] == "LabVIEW 2013 SP1 (13.0.1)"
    assert vifile.version_words(0x23308000)[1] == "LabVIEW 2023 Q3 (23.3)"
    assert vifile.version_words(0x08508002)[1] == "LabVIEW 8.5"


def test_what_isnt_a_labview_file_is_said_in_words():
    for bad in (b"", b"hello", b"RSRC\r\n" + b"\x00" * 10, b"RSRC\r\n" + b"\xff" * 40):
        with pytest.raises(vifile.ReadError):
            vifile.container(bad)


def test_a_heap_reads_into_objects_with_their_classes_uids_and_content():
    data = b"".join([entry(-7, 0, cls=126, uid=1), entry(214, 0, cls=27, uid=2), entry(172, content=b"\x01\x02"),
                     entry(127, 0, cls=10, uid=3), entry(270, 0, cls=1), entry(3, content=b"Start (V)"),
                     entry(270, 2), entry(127, 2), entry(214, 2), entry(-7, 2)])
    top = vifile.heap(struct.pack(">I", len(data)) + data)
    root = top.get("root")
    assert (root.kind, root.uid, root.number("objFlags")) == ("diag", 2, 0x0102)
    assert vifile.label_of(root) == "Start (V)"
    with pytest.raises(vifile.ReadError):
        vifile.heap(struct.pack(">I", 50) + data[:7])


def test_linkage_names_each_sub_vi_and_where_it_lives():
    block = b"\x00\x01LVIN\x00\x00\x00\x03\x00\x02" + \
        link(b"VIVI", ["Keithley 2400 Series.lvlib", "Configure Source.vi"],
             ["<instrlib>", "Keithley 2400 Series", "Public", "Configure Source.vi"]) + \
        link(b"VIVI", ["Wait For Settle.vi"], ["", "Utilities", "Wait For Settle.vi"]) + \
        link(b"VICC", ["Units.ctl"], ["<vilib>", "Utility", "Units.ctl"])
    found = vifile.links_in(block)
    assert [(ln.ident, ln.name, ln.kind, ln.where) for ln in found] == [
        ("VIVI", "Keithley 2400 Series.lvlib:Configure Source.vi", "vi", "instr.lib"),
        ("VIVI", "Wait For Settle.vi", "vi", ""),
        ("VICC", "Units.ctl", "control", "vi.lib")]
    assert vifile.path_text(found[1].path) == "../Utilities/Wait For Settle.vi"


# ------------------------------------------------------------------ public VIs

def test_a_drivers_write_is_read_with_the_values_the_diagram_fills_in():
    s = vigraph.summary(AIM / "set_v.vi")
    assert (s["labview"], s["read"], s["unread"]) == ("LabVIEW 2018 (18.0)", "full", [])
    controls = {c["name"]: c for c in s["controls"]}
    assert controls["Voltage (V)"] == {"name": "Voltage (V)", "type": "number", "unit": "V", "default": 0,
                                       "choices": [], "indicator": False}
    assert (controls["Channel"]["default"], controls["VISA Refnum in"]["type"]) == ("1", "visa")
    assert controls["error in (no error)"]["unit"] == ""   # words in brackets are no unit
    assert controls["VISA Refnum out"]["indicator"]
    assert s["diagram"] == [{"node": "write", "resource": {"control": "VISA Refnum in"},
                             "text": "V{Channel} {Voltage}",   # "V" + channel + Space Constant.vi + the number
                             "values": {"Channel": {"control": "Channel"}, "Voltage": {"control": "Voltage (V)"}}}]
    assert s["scpi"] == ["V{Channel} {Voltage}"]
    assert {"name": "Aim-TTi CPX400SP.lvlib", "library": "Aim-TTi CPX400SP.lvlib", "vi": "", "where": "instr.lib",
            "maker": "", "model": ""} in s["linkage"]   # the library it belongs to: a driver's


def test_a_write_then_a_read_of_one_resource_is_a_query_into_the_indicator_it_shows():
    s = vigraph.summary(AIM / "measure_v.vi")
    assert s["diagram"] == [{"node": "query", "resource": {"control": "VISA Refnum in"}, "text": "V{Channel}O?",
                             "values": {"Channel": {"control": "Channel"}}, "into": "V readback (V)"}]
    assert vigraph.summary(AIM / "get_set_v.vi")["diagram"][0]["text"] == "V{Channel}?"


def test_an_iv_sweep_is_read_with_its_drivers_commands_from_the_vis_beside_it():
    s = vigraph.summary(KEYSIGHT / "SMU_IV single.vi")
    assert (s["labview"], s["read"]) == ("LabVIEW 2022 (22.3)", "full")
    top = s["diagram"]
    outer = next(n for n in top if n["node"] == "loop")
    inner = next(n for n in outer["body"] if n["node"] == "loop")
    assert (outer["kind"], outer["var"], inner["var"]) == ("while", "i", "j")
    assert inner["range"] == {"from": {"control": "Starting Voltage (V)"}, "to": {"control": "Stopping Voltage (V)"},
                              "step": {"control": "Voltage Interval  (DV)"}}   # the ramp each value comes from
    calls = [n for n in inner["body"] if n["node"] == "call"]
    # a polymorphic VI's call is the instance it uses, read from beside the VI, its inputs named by its own panel
    drain = calls[0]
    assert drain["vi"] == "Agilent U2722.lvlib:Configure Channel (Voltage).vi"
    assert drain["resource"] == {"control": "VISA resource name"}
    assert drain["inputs"]["Output Level (1.0 V)"] == {"loop": "j"}
    assert drain["inputs"]["Channel (1: Channel 1)"] == {"control": "Drain - Source Channel"}
    sent = drain["sends"][0]
    assert ":VOLT {j},(@{Drain_Source_Channel3})" in sent["text"] and sent["values"]["j"] == {"loop": "j"}
    assert ":VOLT:LIM 2,(@" in sent["text"]   # the driver's own saved value, formatted as its %g writes it
    assert next(n for n in inner["body"] if n["node"] == "wait") == {"node": "wait",
                                                                    "s": {"control": "Interval Time (s)"}}
    read = [n for n in calls if n["vi"].endswith("Read Output (Single).vi")]
    assert [x["text"] for x in read[0]["sends"]] == [":MEAS:VOLT? (@{Gate_Source_Channel})",
                                                    ":MEAS:CURR? (@{Gate_Source_Channel})"]
    assert all(x.get("query") for x in read[0]["sends"]) and read[0]["into"] == "Actual Current Level (A)"
    saved = next(n for n in outer["body"] if n["node"] == "file")
    assert saved["kind"] == "spreadsheet" and all("read" in c["from"] for c in saved["columns"])
    outputs = [n for n in top if n["node"] == "call" and n["vi"].endswith("Configure Output.vi")]
    assert [n["sends"][0]["text"] for n in outputs] == [":OUTP 1,(@{Drain_Source_Channel})",
                                                       ":OUTP 1,(@{Gate_Source_Channel})",
                                                       ":OUTP 0,(@{Gate_Source_Channel})",
                                                       ":OUTP 0,(@{Drain_Source_Channel})"]
    missing = next(n for n in top if n["node"] == "call" and n["vi"].endswith("Close.vi"))
    assert missing["sends"] == [] and "isn't beside the VI" in missing["sends_unread"]
    poly = vigraph.summary(KEYSIGHT / "Agilent U2722" / "Public" / "Configure" / "Configure Channel.vi")
    assert (poly["read"], poly["diagram"]) == ("panel", [])   # its instances run, never the polymorphic VI itself
    assert "polymorphic VI" in poly["unread"][0] and "Current, Voltage" in poly["unread"][0]


def test_a_state_machine_is_said_with_its_states_never_translated_as_a_sweep():
    s = vigraph.summary(JKI)
    assert [n["node"] for n in s["diagram"]] == ["state_machine"]
    machine = s["diagram"][0]
    assert {"Idle", "Exit", "Error Handler"} <= {name for states in machine["states"] for name in states.split(", ")}
    assert "Idle" in machine["cases"]
    assert any(n["node"] == "event" for n in walk(machine["cases"]["Idle"]))


def test_vis_of_labview_2009_and_8_5_read_and_say_what_they_keep_elsewhere():
    old = vigraph.summary(FIXTURES / "labolabo2009" / "RemoveDuplicates_D.vi")
    assert (old["labview"], old["read"], old["unread"]) == ("LabVIEW 2009 (9.0)", "full", [])
    loop = old["diagram"][0]
    assert (loop["node"], loop["kind"], loop["range"]) == ("loop", "for", {"values": {"control": "1D Array In"}})
    older = vigraph.summary(FIXTURES / "svs4od" / "Loop Counter 2.vi")
    assert older["labview"] == "LabVIEW 8.5" and older["read"] == "full"
    assert any("LabVIEW 8.5 keeps them" in u for u in older["unread"])


def test_a_locked_diagram_is_never_read(tmp_path):
    raw = (AIM / "set_v.vi").read_bytes()   # BDPW keeps the password's MD5: an empty one's, made a password's
    (tmp_path / "locked.vi").write_bytes(raw.replace(hashlib.md5(b"").digest(), hashlib.md5(b"secret").digest()))
    s = vigraph.summary(tmp_path / "locked.vi")
    assert (s["read"], s["diagram"]) == ("panel", [])
    assert any("password" in u for u in s["unread"])
    assert "Voltage (V)" in {c["name"] for c in s["controls"]}   # its panel and linkage still are


def test_damaged_files_are_said_never_raised(tmp_path):
    (tmp_path / "text.vi").write_text("not a VI", encoding="utf-8")
    s = vigraph.summary(tmp_path / "text.vi")
    assert s["read"] == "no" and "RSRC" in s["unread"][0]
    assert vigraph.summary(tmp_path / "gone.vi")["read"] == "no"
    good = (AIM / "measure_v.vi").read_bytes()
    rnd = random.Random(7)
    for k in range(40):
        raw = bytearray(good[:rnd.randrange(32, len(good))] if k % 4 == 0 else good)
        for _ in range(rnd.randrange(1, 30)):
            raw[rnd.randrange(len(raw))] = rnd.randrange(256)
        (tmp_path / "bad.vi").write_bytes(bytes(raw))
        s = vigraph.summary(tmp_path / "bad.vi")
        assert s["read"] in READS and isinstance(s["unread"], list)
        json.dumps(s)   # plain data, always


def test_a_vi_library_s_vis_each_have_a_summary(tmp_path):
    inner = (AIM / "set_v.vi").read_bytes()
    (tmp_path / "drivers.llb").write_bytes(rsrc([(b"ADir", [(0, "", b"\x00" * 8)]),
                                                 (b"UCRF", [(0, "set_v.vi", inner), (1, "notes.txt", b"x")])],
                                                kind=b"LVAR"))
    out = vigraph.summaries(tmp_path / "drivers.llb")
    assert [s["name"] for s in out] == ["set_v.vi"]
    assert out[0]["read"] == "full" and out[0]["diagram"][0]["node"] == "write"
    assert vigraph.summaries(AIM / "set_v.vi")[0]["read"] == "no"   # a VI isn't a library


def test_text_is_read_in_the_encoding_the_vi_was_saved_in():
    with vifile.text_encoding("gbk"):
        assert vifile._text("测试".encode("gbk")) == "测试"
    assert vifile._text("测试".encode("utf-8")) == "测试"
    assert vifile._text(b"\xb5V") == "µV"   # Windows' Western, by default


def test_summaries_fit_the_layout_the_labview_survey_takes():
    for p in sorted(FIXTURES.rglob("*.vi")):
        s = labview.read_vi(p)   # the survey's reader is this one
        assert labview.validate(s) == [], p.name
        assert s == vigraph.summary(p, link=labview.link)
    driver = next(e for e in labview.read_vi(AIM / "set_v.vi")["linkage"] if e["name"] == "Aim-TTi CPX400SP.lvlib")
    assert (driver["where"], driver["model"]) == ("instr.lib", "CPX400SP")   # maker and model, as labview.link says


def test_the_survey_reads_a_labs_real_vis_and_their_drivers(monkeypatch):
    monkeypatch.setattr(labview, "VISACONF", FIXTURES / "no NI-VISA here" / "visaconf.ini")
    found = labview.survey(KEYSIGHT, builtin_registry())
    read = {s["name"]: s["read"] for s in found.vis}
    assert read == {"SMU_IV single.vi": "full", "Configure Channel.vi": "panel", "Read Output.vi": "panel",
                    "Configure Channel (Current).vi": "full", "Configure Channel (Voltage).vi": "full",
                    "Configure Output.vi": "full", "Read Output (Single).vi": "full"}
    driver = next(i for i in found.instruments if "Agilent U2722.lvlib" in i.calls)   # its driver, from the linkage
    assert driver.address == "" and "U2722" in driver.hints
    view = {v["name"]: v for v in found.view()["vis"]}
    assert view["SMU_IV single.vi"]["measures"] and not view["Configure Channel.vi"]["measures"]
    text = found.text()
    assert "SMU_IV single.vi (LabVIEW 2022 (22.3)): read full" in text
    assert "it is a polymorphic VI" in text


def test_a_real_iv_sweep_translates_as_far_as_its_vi_says_and_names_the_rest():
    s = labview.read_vi(KEYSIGHT / "SMU_IV single.vi")
    t = viscan.translate(s, {})
    assert t["plan"] is None   # no address is saved with the VI: the person picks the instrument in Set up
    assert "Agilent U2722.lvlib:Configure Output.vi on ?: :OUTP 1,(@{Drain_Source_Channel}): no instrument of the lab " \
           "is at ?" in t["gaps"]   # the command its driver sends, read from the driver beside the VI
    assert "calls Agilent U2722.lvlib:Initialize.vi: what it sends to the instrument isn't known" in t["gaps"]
    assert "While loop over i (from Starting Voltage (V) 2 = 0 to Stopping Voltage (V) 2 = 1, step Voltage " \
           "Interval 2 (DV) = 0): nothing it sets follows its value" in t["gaps"]
    loop = next(n for n in s["diagram"] if n["node"] == "loop")
    assert loop["range"] == {"from": {"control": "Starting Voltage (V) 2"}, "to": {"control": "Stopping Voltage (V) 2"},
                             "step": {"control": "Voltage Interval 2 (DV)"}}   # LabVIEW's Ramp Pattern by Delta
    assert labview.range_values({"from": {"value": 0.0}, "to": {"value": 1.0}, "step": {"value": 0.5}}, {}) == \
        [0.0, 0.5, 1.0]


def test_a_vi_library_in_the_labs_folder_has_its_vis_surveyed(tmp_path, monkeypatch):
    monkeypatch.setattr(labview, "VISACONF", tmp_path / "no NI-VISA here" / "visaconf.ini")
    (tmp_path / "Lab").mkdir()
    (tmp_path / "Lab" / "Power supply.llb").write_bytes(rsrc([(b"UCRF", [(0, "set_v.vi", (AIM / "set_v.vi").read_bytes())])],
                                                             kind=b"LVAR"))
    found = labview.survey(tmp_path / "Lab", builtin_registry())
    assert [(s["name"], s["read"]) for s in found.vis] == [("set_v.vi", "full")]
    assert found.vis[0]["path"].endswith("Power supply.llb/set_v.vi")


def test_a_while_loops_stop_after_its_last_value_is_read_exactly():
    """LabVIEW's While loop goes past the end of an array it takes value by value unless told to stop: the I-V sweep
    stops when its count is the ramp's number of samples less one, its last value."""
    s = vigraph.summary(KEYSIGHT / "SMU_IV single.vi")
    outer = next(n for n in s["diagram"] if n["node"] == "loop")
    inner = next(n for n in outer["body"] if n["node"] == "loop")
    assert outer["stop_if"] == inner["stop_if"] == {"last": True}
    assert "Equal?" in outer["until"]   # said in words too
    assert labview.validate(s) == []


# ------------------------------------------------------------------ files made to hang or swell the reader

class Diagram:
    """A block diagram built here: LabVIEW functions (by their number) with numbered terminals, constants on the
    diagram (of the VI's types), and the wires between them."""

    def __init__(self):
        self.uid, self.nodes, self.consts, self.wires = 100, [], [], []

    def next(self):
        self.uid += 1
        return self.uid

    def prim(self, rid, parms):
        node, terms = self.next(), {}
        self.nodes += [entry(-6, 0, cls=47, uid=node), entry(203, content=rid.to_bytes(2, "big")), entry(268, 0)]
        for p in parms:
            terms[p] = self.next()
            self.nodes += [entry(-6, 0, cls=21, uid=terms[p]), entry(48, 0, cls=48, uid=self.next()),
                           entry(191, content=bytes([p])), entry(48, 2), entry(-6, 2)]
        self.nodes += [entry(268, 2), entry(-6, 2)]
        return terms

    def constant(self, type_id, value):
        term = self.next()
        self.consts += [entry(-6, 0, cls=21, uid=term), entry(48, 0, cls=19, uid=self.next()),
                        entry(283, content=type_id.to_bytes(2, "big")), entry(589, content=value), entry(48, 2),
                        entry(-6, 2)]
        return term

    def wire(self, src, *sinks):
        self.wires.append((src, sinks))

    def heap(self):
        holder = [entry(-6, 0, cls=29, uid=self.next()), entry(268, 0)] + self.consts + [entry(268, 2), entry(-6, 2)]
        sig = []
        for src, sinks in self.wires:
            sig += [entry(-6, 0, cls=23, uid=self.next()), entry(268, 0), entry(-6, 1, uid=src)]
            sig += [entry(-6, 1, uid=s) for s in sinks] + [entry(268, 2), entry(-6, 2)]
        return heap_block([entry(214, 0, cls=27, uid=1), entry(164, 0)] + holder + self.nodes +
                          [entry(164, 2), entry(233, 0)] + sig + [entry(233, 2), entry(214, 2)])


def packed(body):
    import zlib
    return struct.pack(">I", len(body)) + zlib.compress(body)


def rsrc_shared(ident, body, n, kind=b"LVIN"):
    """A resource file whose one block has n sections that all point at the same bytes, kept once."""
    data = struct.pack(">I", len(body)) + body + b"\x00" * (-len(body) % 4)
    heads = struct.pack(">4sII", ident, n - 1, 16)
    starts = b"".join(struct.pack(">iIIII", i, 0xFFFFFFFF, 0, 0, 0) for i in range(n))
    info = struct.pack(">I", 0) + heads + starts
    head = b"RSRC\r\n" + struct.pack(">H4s4sIIII", 3, kind, b"LBVW", 32 + len(data), 52 + len(info), 32, len(data))
    return head + data + head + struct.pack(">IIIII", 0, 0, 32, 52, len(info)) + info


def test_a_value_used_over_and_over_is_followed_once(tmp_path, monkeypatch):
    """30 Adds chained x + x (each one's output into both inputs of the next) into a VISA Write: 2^30 ways back
    through them, one value; followed once each (a small budget of steps is plenty), said in a few words."""
    monkeypatch.setattr(vigraph, "MAX_STEPS", 5000)
    d, prev = Diagram(), None
    for _ in range(30):
        t = d.prim(1050, (1, 2, 3))
        if prev is not None:
            d.wire(prev, t[1], t[2])
        prev = t[3]
    write = d.prim(1926, (10, 11))
    d.wire(prev, write[10])
    (tmp_path / "chain.vi").write_bytes(rsrc([(b"BDHb", [(0, "", d.heap())])]))
    s = vigraph.summary(tmp_path / "chain.vi")
    assert s["diagram"][0]["node"] == "write" and not any("bigger" in u for u in s["unread"])
    assert len(json.dumps(s["diagram"])) < 2000
    monkeypatch.setattr(vigraph, "MAX_STEPS", 50)   # past its budget: said, with what was read
    s = vigraph.summary(tmp_path / "chain.vi")
    assert s["read"] == "names" and s["unread"] == ["its front panel (not in the file)",
                                                    "its block diagram: bigger than Kelvin follows in one VI"]


def clusters_vi(n, levels):
    """A VI whose one control is a cluster of n clusters of n clusters... (levels deep, the innermost empty: each takes
    no bytes), saved with a one-byte value."""
    types = [struct.pack(">HBBH", 6, 0, 0x50, 0)] + [struct.pack(">HBBH", 6 + 2 * n, 0, 0x50, n) +
                                                     struct.pack(">H", k - 1) * n for k in range(1, levels + 1)]
    vctp = struct.pack(">I", len(types)) + b"".join(types) + struct.pack(">HH", 1, levels)
    panel = [entry(214, 0, cls=18, uid=5), entry(52, 1, cls=83, uid=6), entry(283, content=b"\x00\x01"),
             entry(571, content=b"\x00"), entry(214, 2)]
    return rsrc([(b"VCTP", [(0, "", packed(vctp))]), (b"DTHP", [(0, "", struct.pack(">HH", 1, 1))]),
                 (b"FPHb", [(0, "", heap_block(panel))])])


def test_a_saved_value_of_clusters_in_clusters_is_read_only_so_far(tmp_path):
    """Seven levels of ten empty clusters are 10^7 values in a 300-byte file: read up to MAX_DECODE, then said."""
    (tmp_path / "deep.vi").write_bytes(clusters_vi(10, 7))
    vi = vifile.read(tmp_path / "deep.vi")
    assert vi.controls[0].kind == "cluster" and not vi.controls[0].has_default
    assert "the saved value of unlabelled cluster 1: bigger than Kelvin reads" in vi.unread
    (tmp_path / "small.vi").write_bytes(clusters_vi(2, 2))   # one that fits is read
    assert vifile.read(tmp_path / "small.vi").controls[0].default == [[[], []], [[], []]]


def test_a_type_is_read_from_its_own_bytes_only():
    """A cluster (or enum) that claims 10000 parts and has none reads none from the types after it: 2000 of them in a
    file of some kilobytes were 20 million reads."""
    for code in (0x50, 0x15):
        vctp = struct.pack(">I", 2000) + struct.pack(">HBBH", 6, 0, code, 10000) * 2000 + bytes(40000) + \
            struct.pack(">HH", 0, 0)
        kinds = vifile.types(vifile.container(rsrc([(b"VCTP", [(0, "", packed(vctp))])])))
        assert len(kinds.flat) == 2000 and all(t.items == [] and t.names == [] for t in kinds.flat)


def test_a_linkage_block_is_read_in_one_pass():
    """Thousands of paths with no link's name before them (256 kB unpacked from a 700-byte file): one pass, never a
    search back from each; a block that would take more than MAX_LINK_WORK tries says so."""
    assert vifile.links_in((b"PTH0" + b"\x00" * 4 + b"\xff" * 4) * 20000) == []
    notes = []
    assert vifile.links_in((b"\x00" * 1196 + b"PTH0\x00\x00\x00\x00") * 400, notes) == []
    assert notes == ["its linkage: more than Kelvin reads (the links after the first 0 left out)"]


def test_sections_that_share_bytes_are_left_out_never_copied():
    """200 sections of one block all pointing at the same megabyte: one kept, as a view of the file's bytes, and
    the rest said; a file's sections hold no more than the file."""
    c = vifile.container(rsrc_shared(b"XTRA", b"\x00" * 2 ** 20, 200))
    assert len(c.blocks["XTRA"]) == 1 and isinstance(c.blocks["XTRA"][0].data, memoryview)
    assert c.problems == ["block XTRA: its data overlaps another block's (it is damaged)"]


def test_a_vi_librarys_vis_are_unpacked_one_at_a_time_within_one_budget(tmp_path, monkeypatch):
    """Each VI of a library is unpacked only when it is reached; past MAX_LLB_UNPACKED in all, the rest are said."""
    monkeypatch.setattr(vifile, "MAX_LLB_UNPACKED", 3 * 2 ** 20)
    vis = [(i, "m%d.vi" % i, packed(bytes([i]) * 2 ** 20)) for i in range(5)]
    (tmp_path / "big.llb").write_bytes(rsrc([(b"ZCRF", vis)], kind=b"LVAR"))
    unpacked = []
    real = vifile.unpack
    monkeypatch.setattr(vifile, "unpack", lambda data: unpacked.append(1) or real(data))
    held = vifile.members(tmp_path / "big.llb")
    assert unpacked == []
    assert next(held)[0] == "m0.vi" and unpacked == [1]   # the first only, once reached
    out = vigraph.summaries(tmp_path / "big.llb")
    assert [s["name"] for s in out] == ["m0.vi", "m1.vi", "m2.vi", "big.llb"]
    assert out[-1]["unread"] == ["not read: its VIs are bigger than Kelvin unpacks from one library (3 MB): m3.vi "
                                 "and the VIs after it"]


def test_a_vi_librarys_vi_whose_name_is_a_path_isnt_read_as_one(tmp_path):
    """An .llb's VI named ../Main.vi would take the path of the folder's own Main.vi, and COM3.vi would open a port:
    neither is read, and the library says so."""
    inner = (AIM / "set_v.vi").read_bytes()
    (tmp_path / "x.llb").write_bytes(rsrc([(b"UCRF", [(0, "../Main.vi", inner), (1, "COM3.vi", inner),
                                                      (2, "set_v.vi", inner)])], kind=b"LVAR"))
    out = vigraph.summaries(tmp_path / "x.llb")
    assert [(s["name"], s["read"]) for s in out] == [("set_v.vi", "full"), ("x.llb", "no")]
    assert out[0]["path"] == str(tmp_path / "x.llb") + "/set_v.vi"
    assert out[1]["unread"] == ["not read: 2 VIs in it whose names aren't plain file names ('../Main.vi', "
                                "'COM3.vi')"]


def test_a_path_a_file_names_is_only_ever_one_in_the_folder(tmp_path, monkeypatch):
    """Another computer's share, a device, an absolute place, a drive's own folder, or a way out of the folder: told
    from the text alone, before anything is looked at (on Windows, looking at \\\\host\\share logs on to it)."""
    (tmp_path / "lab").mkdir()
    (tmp_path / "lab" / "sub.vi").write_bytes(b"RSRC")
    (tmp_path / "elsewhere.vi").write_bytes(b"RSRC")
    looked = []
    for name in ("stat", "lstat"):
        real = getattr(os, name)
        monkeypatch.setattr(os, name, lambda p, *a, _real=real, **k: looked.append(str(p)) or _real(p, *a, **k))
    lab = tmp_path / "lab"
    for parts in (["\\\\kelvin-test.invalid\\share", "x.vi"], ["", "", "kelvin-test.invalid", "x.vi"],
                  ["\\\\?\\UNC", "kelvin-test.invalid", "x.vi"], ["\\\\.\\COM3"], ["COM3.vi"], ["NUL"], ["C:", "x.vi"],
                  ["C:x.vi"], ["/C", "x.vi"], ["\\x.vi"], ["..", "elsewhere.vi"], ["sub.vi:stream"], ["..  ", "x.vi"],
                  ["a\x00b.vi"]):
        assert vifile.within(lab, parts) is None, parts
    assert not [p for p in looked if "kelvin-test" in p or "COM3" in p or "elsewhere" in p]
    assert vifile.within(lab, ["sub.vi"]) == (lab / "sub.vi").resolve()
    assert vifile.within(lab, ["..", "sub.vi"], lab / "main.vi") == (lab / "sub.vi").resolve()   # beside a VI


def test_a_sub_vi_outside_the_folder_or_in_kelvins_own_is_never_read(tmp_path):
    """A VI's links (as LabVIEW saves them: an empty part is up one) lead only to sub-VIs in the summary's folder,
    and never into Kelvin's own folders."""
    (tmp_path / "lab").mkdir()
    (tmp_path / "lab" / "kelvin").mkdir()
    for p in (tmp_path / "secret.vi", tmp_path / "lab" / "sub.vi", tmp_path / "lab" / "kelvin" / "k.vi"):
        p.write_bytes((AIM / "set_v.vi").read_bytes())
    main = vifile.VI(path=str(tmp_path / "lab" / "main.vi"), name="main.vi")
    reader = vigraph._Reader(main, vigraph._link, 0, {}, shared=vigraph._Shared(
        folder=str(tmp_path / "lab"), own=(str(tmp_path / "lab" / "kelvin"),)))

    def opened(path):
        return reader._open(main.path, vifile.Link("x.vi", path, "vi", "", "VIVI"))
    assert opened(["", "sub.vi"]).name == "sub.vi"
    assert opened(["", "", "secret.vi"]) is None                                   # up out of the folder
    assert opened(["", str(tmp_path / "secret.vi")]) is None                        # an absolute part never wins
    assert opened(["", "\\\\kelvin-test.invalid\\share", "x.vi"]) is None
    assert opened(["<vilib>", "..", "..", "secret.vi"]) is None
    assert opened(["", "kelvin", "k.vi"]) is None                                  # Kelvin's own folder
    assert list(reader.files) == [str((tmp_path / "lab" / "sub.vi").resolve())]


def test_numbers_that_arent_finite_are_said_never_kept(tmp_path):
    """A constant of +Inf wired to a Wait, a control saved as NaN: the summary keeps no number that isn't finite (it
    is plain JSON a survey can always send), and says each."""
    dbl = struct.pack(">HBB", 4, 0, 0x0A)
    vctp = struct.pack(">I", 1) + dbl + struct.pack(">HH", 1, 0)
    d = Diagram()
    wait = d.prim(1302, (1,))
    d.wire(d.constant(1, struct.pack(">d", float("inf"))), wait[1])
    panel = [entry(214, 0, cls=18, uid=5), entry(52, 1, cls=80, uid=6), entry(283, content=b"\x00\x01"),
             entry(571, content=struct.pack(">d", float("nan"))), entry(214, 2)]
    (tmp_path / "inf.vi").write_bytes(rsrc([(b"VCTP", [(0, "", packed(vctp))]),
                                            (b"DTHP", [(0, "", struct.pack(">HH", 1, 1))]),
                                            (b"FPHb", [(0, "", heap_block(panel))]), (b"BDHb", [(0, "", d.heap())])]))
    s = vigraph.summary(tmp_path / "inf.vi")
    assert s["diagram"] == [{"node": "wait", "ms": {"computed": "a constant that isn't a finite number (inf)"}}]
    assert s["controls"][0]["default"] is None
    assert "the saved value of unlabelled number 1 isn't a finite number (Inf or NaN): left out" in s["unread"]
    json.dumps(s, allow_nan=False)


def test_every_downloaded_vi_reads_without_an_error():
    """The public VIs a developer downloaded for the reader (KELVIN_LABVIEW_SAMPLES: their folder), when there."""
    folder = os.environ.get("KELVIN_LABVIEW_SAMPLES")
    if not folder or not Path(folder).is_dir():
        pytest.skip("no folder of downloaded VIs (KELVIN_LABVIEW_SAMPLES)")
    for p in sorted(Path(folder).rglob("*.vi"))[:2000]:
        s = vigraph.summary(p)
        assert s["read"] in READS, p
