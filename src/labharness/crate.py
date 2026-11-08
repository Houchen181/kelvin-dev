"""A data package other programs and repositories read (phase 5 of the ELN plan; asked for 2026-11-07: "Kelvin's data
saving protocol and experimental procedure recording should follow the practice of modern ELN/LIMS system").

An RO-Crate (https://w3id.org/ro/crate/1.1), zipped: runs and what goes with them, each file as Kelvin keeps it,
described in ro-crate-metadata.json (JSON-LD, schema.org terms):
- runs/<id>/: run.h5 with its seal (run.h5.sha256), derived.h5 and notes.jsonl; each run a CreateAction (who, when,
  on which sample, with Kelvin, following which procedure version) whose result is its run.h5 (sha256, uuid, the
  quantities it measured with their units as UCUM codes);
- figures/: the figures drawn from them, each with its record (isBasedOn its runs);
- notebook/: entries with every version and signature (author: the person; contributor: the AI that drafted it);
- samples/: the samples' records (what each came from); procedures/: the procedure versions the runs followed.
Kelvin writes it by code from what it keeps. It stays in the data folder's exports/: nothing leaves the computer
unless a person sends it (to Zenodo, an institutional repository, another ELN)."""
from __future__ import annotations

import json
import shutil
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from . import notebook, rundata, runnotes
from ._version import __version__

CONTEXT = "https://w3id.org/ro/crate/1.1/context"
LICENSES = {"cc-by-4.0": "https://creativecommons.org/licenses/by/4.0/",
            "cc0-1.0": "https://creativecommons.org/publicdomain/zero/1.0/",
            "cc-by-sa-4.0": "https://creativecommons.org/licenses/by-sa/4.0/"}


def ucum(unit: str) -> str:
    """A unit as UCUM writes it (µV as uV, Ω as Ohm, °C as Cel, dBm as dB[mW])."""
    u = str(unit or "").strip()
    for a, b in (("µ", "u"), ("μ", "u"), ("Ω", "Ohm"), ("°C", "Cel"), ("dBm", "dB[mW]"), ("°", "deg")):
        u = u.replace(a, b)
    return u


def _file(crate: Path, rel: str, kind: str, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    p = crate / rel
    out: Dict[str, Any] = {"@id": rel, "@type": kind, "name": p.name, "contentSize": str(p.stat().st_size)}
    if p.suffix == ".h5":
        out["encodingFormat"] = "application/x-hdf5"
    elif p.suffix in (".json", ".jsonl"):
        out["encodingFormat"] = "application/json"
    elif p.suffix == ".png":
        out["encodingFormat"] = "image/png"
    elif p.suffix == ".yaml":
        out["encodingFormat"] = "application/yaml"
    out.update(extra or {})
    return out


def build(rt: Any, runs: Iterable[str] = (), entries: Iterable[str] = (), figures: Iterable[str] = (),
          samples: Iterable[str] = (), title: str = "", license: str = "", orcid: str = "") -> Dict[str, Any]:
    """Write the crate as exports/crate-<time>.zip in the data folder: {"path", "files", "bytes", "runs"}.
    KeyError/ValueError: a run, entry or figure that isn't here."""
    from .derivations import find_figure
    from .procedures import ProcedureBook
    from .samples import SampleBook
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    exports = Path(rt.data_dir) / "exports"
    crate = exports / ("crate-%s" % stamp)
    crate.mkdir(parents=True, exist_ok=False)
    graph: List[Dict[str, Any]] = []
    parts: List[Dict[str, str]] = []
    people: Dict[str, Dict[str, Any]] = {}
    ais: Dict[str, Dict[str, Any]] = {}
    sample_names: List[str] = [str(s) for s in samples if str(s).strip()]
    procs: Dict[str, int] = {}

    def person(name: Optional[str]) -> Optional[Dict[str, str]]:
        if not name:
            return None
        pid = ("https://orcid.org/" + orcid.strip()) if orcid.strip() and name == getattr(rt, "user", None) \
            else "#person-" + name
        people.setdefault(pid, {"@id": pid, "@type": "Person", "name": name})
        return {"@id": pid}

    def ai_of(ai: Any) -> Optional[Dict[str, str]]:
        if not isinstance(ai, dict) or not ai.get("model"):
            return None
        aid = "#ai-" + str(ai["model"])
        ais.setdefault(aid, {"@id": aid, "@type": "SoftwareApplication", "name": str(ai["model"]),
                             "description": "the AI model that wrote it, through Kelvin"})
        return {"@id": aid}

    wanted = list(dict.fromkeys(str(r) for r in runs if str(r).strip()))
    kept_entries = notebook.read(rt.data_dir)
    for eid in entries:
        e = kept_entries.get(str(eid))
        if e is None:
            raise KeyError("no entry %s" % eid)
        wanted += [r for r in notebook.latest(e).get("runs") or [] if r not in wanted]
    figure_records = []
    for name in figures:
        path, record = find_figure(Path(rt.data_dir) / "figures", str(name))
        figure_records.append((path, record))
        wanted += [s["run"] for s in record.get("runs") or [] if s["run"] not in wanted]
    run_files: Dict[str, str] = {}
    for rid in wanted:
        d = rt._run_dir(rid)
        rec = rundata.load(d, with_rows=False)
        meta = rec.meta
        target = crate / "runs" / d.name
        target.mkdir(parents=True)
        for f in (rundata.RUN_FILE, rundata.SEAL, rundata.DERIVED, runnotes.FILE, rundata.RECORD):
            if (d / f).exists():   # the content as kept (not its read-only mark, so the folder can go once zipped)
                shutil.copyfile(str(d / f), str(target / f))
        main = rundata.RUN_FILE if (target / rundata.RUN_FILE).exists() else rundata.RECORD
        rel = "runs/%s/%s" % (d.name, main)
        units = meta.get("units") or {}
        measured = [{"@type": "PropertyValue", "name": m.get("key"), "unitText": m.get("unit") or units.get(m.get("key"))
                     or "", "unitCode": ucum(m.get("unit") or units.get(m.get("key")) or "")}
                    for m in (rec.columns.get("axes") or []) + (rec.columns.get("measures") or [])]
        extra: Dict[str, Any] = {"variableMeasured": measured, "dateCreated": meta.get("finished") or meta.get("started")}
        if rundata.sealed(d):
            extra["sha256"] = rundata.sealed(d)
        if meta.get("uuid"):
            extra["identifier"] = "urn:uuid:%s" % meta["uuid"]
        graph.append(_file(crate, rel, "File", extra))
        parts.append({"@id": rel})
        run_files[d.name] = rel
        for f in sorted(target.iterdir()):
            if f.name != main:
                r2 = "runs/%s/%s" % (d.name, f.name)
                graph.append(_file(crate, r2, "File", {"about": {"@id": rel}}))
                parts.append({"@id": r2})
        if meta.get("device_name") and meta["device_name"] not in sample_names:
            sample_names.append(meta["device_name"])
        proc = meta.get("procedure") if isinstance(meta.get("procedure"), dict) else None
        if proc and proc.get("name"):
            procs[str(proc["name"])] = int(proc.get("version") or 0)
        action: Dict[str, Any] = {"@id": "#run-%s" % d.name, "@type": "CreateAction", "name": meta.get("plan") or d.name,
                                  "startTime": meta.get("started"), "endTime": meta.get("finished"),
                                  "actionStatus": "CompletedActionStatus" if meta.get("status") == "done"
                                  else "FailedActionStatus", "result": {"@id": rel},
                                  "instrument": [{"@id": "#kelvin"}]}
        who = person(meta.get("user") or meta.get("operator"))
        if who:
            action["agent"] = who
        if meta.get("device_name"):
            action["object"] = {"@id": "#sample-%s" % meta["device_name"]}
        if proc:
            action["description"] = "procedure %s v%s, step %s%s" % (
                proc.get("name"), proc.get("version"), proc.get("step"),
                "; deviations: " + "; ".join(proc["deviations"]) if proc.get("deviations") else "")
        if ai_of(meta.get("ai")):
            action["participant"] = ai_of(meta.get("ai"))
        graph.append(action)
    for path, record in figure_records:
        (crate / "figures").mkdir(exist_ok=True)
        for name in [path.name] + list(record.get("files") or []):
            src = path.parent / name
            if src.exists():
                shutil.copyfile(str(src), str(crate / "figures" / name))
                rel = "figures/" + name
                kind = ["File", "ImageObject"] if src.suffix in (".png", ".svg", ".pdf") else "File"
                extra = {"isBasedOn": [{"@id": run_files[s["run"]]} for s in record.get("runs") or []
                                       if s["run"] in run_files]}
                if ai_of(record.get("ai")):
                    extra["contributor"] = ai_of(record.get("ai"))
                graph.append(_file(crate, rel, kind, extra))
                parts.append({"@id": rel})
    for eid in entries:
        e = kept_entries[str(eid)]
        (crate / "notebook").mkdir(exist_ok=True)
        rel = "notebook/%s.json" % eid
        (crate / rel).write_text(json.dumps(e, ensure_ascii=False, indent=1), encoding="utf-8")
        v = notebook.latest(e)
        extra = {"name": v.get("title"), "dateModified": v.get("at"),
                 "isBasedOn": [{"@id": run_files[r]} for r in v.get("runs") or [] if r in run_files]}
        authors = [person(s.get("account")) for s in e["signs"] if s.get("role") == "author"]
        if authors:
            extra["author"] = authors
        if ai_of(v.get("ai")):
            extra["contributor"] = ai_of(v.get("ai"))
        graph.append(_file(crate, rel, "File", extra))
        parts.append({"@id": rel})
    book = SampleBook(rt.data_dir)
    for name in sample_names:
        _, s = book.find(None, name)
        thing: Dict[str, Any] = {"@id": "#sample-%s" % name, "@type": "Thing", "name": name,
                                 "additionalType": "Sample"}
        if s:
            (crate / "samples").mkdir(exist_ok=True)
            rel = "samples/%s.json" % "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
            (crate / rel).write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")
            graph.append(_file(crate, rel, "File", {"about": {"@id": "#sample-%s" % name}}))
            parts.append({"@id": rel})
            if s.get("parent"):
                thing["isBasedOn"] = {"@id": "#sample-%s" % s["parent"]}
            if s.get("description") or s.get("kind"):
                thing["description"] = "; ".join(x for x in (s.get("kind"), s.get("material"), s.get("description"))
                                                 if x)
        graph.append(thing)
    pbook = ProcedureBook(Path(rt.home) / "procedures")
    for name, version in procs.items():
        try:
            text, n = pbook.text(name, version)
        except KeyError:
            continue
        rel = "procedures/%s/v%d.yaml" % ("".join(c if c.isalnum() or c in "-_" else "_" for c in name), n)
        (crate / rel).parent.mkdir(parents=True, exist_ok=True)
        (crate / rel).write_text(text, encoding="utf-8")
        graph.append(_file(crate, rel, ["File", "HowTo"], {"name": "%s v%d" % (name, n), "version": str(n)}))
        parts.append({"@id": rel})
    root: Dict[str, Any] = {"@id": "./", "@type": "Dataset", "name": title or "Runs from %s" % (
        getattr(getattr(rt.lab, "config", None), "name", "") or "the lab"),
        "description": "Runs measured with Kelvin, each as kept (run.h5, sealed by its SHA-256), with what goes with "
                       "them.", "datePublished": datetime.now().date().isoformat(), "hasPart": parts}
    if license.strip():
        lic = LICENSES.get(license.strip().lower(), license.strip())
        root["license"] = {"@id": lic} if lic.startswith("http") else lic
    if getattr(rt, "user", None):
        root["author"] = person(rt.user)
    graph = [{"@id": "ro-crate-metadata.json", "@type": "CreativeWork",
              "conformsTo": {"@id": "https://w3id.org/ro/crate/1.1"}, "about": {"@id": "./"}}, root,
             {"@id": "#kelvin", "@type": "SoftwareApplication", "name": "Kelvin", "version": __version__,
              "url": "https://github.com/Houchen181/kelvin"}] + graph + list(people.values()) + list(ais.values())
    (crate / "ro-crate-metadata.json").write_text(json.dumps({"@context": CONTEXT, "@graph": graph},
                                                             ensure_ascii=False, indent=1), encoding="utf-8")
    zpath = exports / ("crate-%s.zip" % stamp)
    files = 0
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(crate.rglob("*")):
            if p.is_file():
                z.write(str(p), p.relative_to(crate).as_posix())
                files += 1
    shutil.rmtree(str(crate), ignore_errors=True)
    return {"path": str(zpath), "files": files, "bytes": zpath.stat().st_size, "runs": len(run_files)}
