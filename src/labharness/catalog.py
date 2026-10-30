"""Every run in one searchable place: catalog.sqlite in the data folder (SQLite, part of Python).

The catalog is an index: each run's run.h5 stays the record, and the catalog is brought up to date from the run
folders (a run added, changed or gone). It lets the AI find runs across a whole data folder without opening them:
find_runs for plain questions, query_runs for SQL (read-only), then describe_run for a run's card and get_run_data
or compute for its numbers. SCHEMA_TEXT is what query_runs tells the AI about the tables.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import rundata

FILE = "catalog.sqlite"
SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, plan TEXT, status TEXT, started TEXT, finished TEXT, duration_s REAL,
  points INTEGER, planned_points INTEGER, project TEXT, project_name TEXT, device TEXT, device_name TEXT,
  lab TEXT, simulated INTEGER, operator TEXT, user TEXT, swept TEXT, measured TEXT, assessment TEXT, reason TEXT,
  card TEXT, mtime REAL, marks TEXT);
CREATE TABLE IF NOT EXISTS columns (
  run_id TEXT, key TEXT, role TEXT, quantity TEXT, unit TEXT, kind TEXT,
  n INTEGER, min REAL, max REAL, mean REAL, first REAL, last REAL, PRIMARY KEY (run_id, key));
CREATE TABLE IF NOT EXISTS conditions (
  run_id TEXT, key TEXT, value REAL, text TEXT, unit TEXT, PRIMARY KEY (run_id, key));
CREATE TABLE IF NOT EXISTS results (
  run_id TEXT, reading TEXT, grp TEXT, name TEXT, value REAL, error REAL, unit TEXT, text TEXT);
CREATE INDEX IF NOT EXISTS runs_started ON runs (started);
CREATE INDEX IF NOT EXISTS columns_key ON columns (key);
CREATE INDEX IF NOT EXISTS columns_quantity ON columns (quantity);
CREATE INDEX IF NOT EXISTS conditions_key ON conditions (key);
CREATE INDEX IF NOT EXISTS results_run ON results (run_id);
"""
SCHEMA_TEXT = """runs: one row per finished run. run_id, plan (its name), status (done, aborted, interrupted),
  started and finished (ISO local time, e.g. 2026-10-24T15:30:12.123), duration_s, points, planned_points,
  project and project_name, device and device_name (the sample), lab, simulated (1 for a lab of simulated instruments),
  operator, user, swept and measured (comma-separated column keys), assessment (the automatic analysis' verdict),
  reason (why it stopped early), card (the run's card as JSON: json_extract(card, '$.held') and so on), marks
  (whose work it is, as JSON: the chat that started it, and incognito, unshared, rules, confidential).
columns: one row per column of each run. run_id, key (instrument.parameter, or i and t), role (axis, measured,
  index or time), quantity (voltage, current, temperature, magnetic_field, frequency, power, phase, resistance,
  position, wavelength, counts...), unit, kind (scalar, trace or image), n, and for scalars min, max, mean,
  first and last.
conditions: every instrument setting and reading each run measured with (as at its start, with the plan's setup
  applied). run_id, key (instrument.parameter), value (when a number; on/off as 1/0), text (otherwise, or on/off),
  unit.
results: the automatic fits. run_id, reading (the fitted column), grp (the group, when a run has several curves),
  name, value, error, unit, text (as shown)."""
MAX_ROWS = 200          # rows a query answers with at most
QUERY_SECONDS = 5.0     # a query that runs longer is stopped
TABLES = ("runs", "columns", "conditions", "results")
# a run's marks of whose work it is (LabRuntime._attribute): who may see it (LabRuntime.may_see)
MARKS = ("chat", "incognito", "unshared", "unpublished", "confidential", "rules")
_lock = threading.Lock()


def _stamp(folder: Path) -> float:
    for name in (rundata.RUN_FILE, rundata.RECORD, "meta.json", "data.jsonl"):
        p = folder / name
        if p.exists():
            return p.stat().st_mtime
    return 0.0


def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float)) and math.isfinite(float(v)):
        return float(v)
    return None


class QueryError(ValueError):
    pass


class Catalog:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.synced = 0.0     # time.monotonic() of the last sync

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(self.path), timeout=10)
        try:
            con.executescript(SCHEMA)
        except sqlite3.OperationalError:   # locked, a full disk...: not a reason to start again
            con.close()
            raise
        except sqlite3.DatabaseError:      # damaged (say, on a network drive): it is only an index, so start again
            con.close()
            for suffix in ("", "-journal", "-wal", "-shm"):
                try:
                    os.remove(str(self.path) + suffix)
                except OSError:
                    pass
            con = sqlite3.connect(str(self.path), timeout=10)
            con.executescript(SCHEMA)
            self.synced = 0.0
        if "marks" not in [r[1] for r in con.execute("PRAGMA table_info(runs)")]:   # from before runs kept whose
            con.execute("ALTER TABLE runs ADD COLUMN marks TEXT")                      # they are: each run is read
            con.execute("UPDATE runs SET mtime = NULL")                                # again at the next look
            con.commit()
            self.synced = 0.0
        return con

    def ready(self) -> None:
        """Open it once: a damaged catalog starts again, empty (synced is then 0, so it is brought up to date)."""
        with _lock:
            self._connect().close()

    # ------------------------------------------------------------------ keeping it up to date

    def sync(self, runs_dir: Path, skip: Sequence[str] = ()) -> int:
        """Bring the catalog up to date with the run folders: add new and changed runs, drop the ones gone.
        runs_dir: a folder of runs, or every folder a person's runs are in (eventlog.RunFolders).
        skip: runs being measured. Returns how many runs were read."""
        runs_dir = runs_dir if hasattr(runs_dir, "roots") else Path(runs_dir)
        folders = {d.name: d for d in runs_dir.iterdir() if d.is_dir()} if runs_dir.exists() else {}
        with _lock:
            con = self._connect()
            try:
                known = dict(con.execute("SELECT run_id, mtime FROM runs").fetchall())
                for gone in set(known) - set(folders):
                    self._drop(con, gone)
                read = 0
                for name, d in sorted(folders.items()):
                    if name in skip or not rundata.is_run(d):
                        continue
                    stamp = _stamp(d)
                    if known.get(name) == stamp:
                        continue
                    try:
                        read += self._add(con, d, stamp)
                    except Exception:
                        continue   # a run Kelvin can't read stays out of the catalog (and in its folder)
                con.commit()
            finally:
                con.close()
        self.synced = time.monotonic()
        return read

    def add(self, folder: Path) -> None:
        """Catalog one run now (when it has just ended)."""
        folder = Path(folder)
        with _lock:
            con = self._connect()
            try:
                self._add(con, folder, _stamp(folder))
                con.commit()
            finally:
                con.close()

    @staticmethod
    def _drop(con: sqlite3.Connection, run_id: str) -> None:
        for table in ("runs", "columns", "conditions", "results"):
            con.execute("DELETE FROM %s WHERE run_id = ?" % table, (run_id,))

    def _add(self, con: sqlite3.Connection, folder: Path, stamp: float) -> int:
        h5 = (folder / rundata.RUN_FILE).exists() and rundata.have_h5()
        rec = rundata.load(folder, with_rows=not h5)
        meta = rec.meta
        if meta.get("status") in (None, "running"):
            return 0
        run_id = folder.name
        stats = _h5_stats(folder) if h5 else rundata.column_stats(rec.rows)
        card = rundata.card(folder) if h5 else rundata.make_card(rec, stats)
        info = rundata.column_info(rec.columns)
        swept = [a["key"] for a in rec.columns.get("axes") or []]
        measured = [m["key"] for m in rec.columns.get("measures") or []]
        self._drop(con, run_id)
        marks = {k: meta[k] for k in MARKS if meta.get(k)}
        con.execute("INSERT INTO runs VALUES (%s)" % ",".join("?" * 23), (
            run_id, meta.get("plan"), meta.get("status"), meta.get("started"), meta.get("finished"),
            _num(meta.get("duration_s")), meta.get("points"), meta.get("planned_points"), meta.get("project"),
            meta.get("project_name"), meta.get("device"), meta.get("device_name"), meta.get("lab"),
            1 if meta.get("simulated") else 0, meta.get("operator"), meta.get("user"), ",".join(swept),
            ",".join(measured), (rec.analysis or {}).get("assessment"), meta.get("reason"),
            json.dumps(card, ensure_ascii=False), stamp, json.dumps(marks, ensure_ascii=False) if marks else None))
        for key, s in stats.items():
            col = info.get(key) or {"role": "measured", "unit": "", "quantity": rundata.quantity_of(key, "")}
            kind = "image" if s.get("image") else "trace" if s.get("trace") else "scalar"
            con.execute("INSERT INTO columns VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
                run_id, key, col.get("role"), col.get("quantity"), col.get("unit"), kind, s.get("n"),
                _num(s.get("min")), _num(s.get("max")), _num(s.get("mean")), _num(s.get("first")),
                _num(s.get("last"))))
        units = meta.get("units") or {}
        measuring = {"%s.%s" % (instr, param): v for instr, values in (rec.snapshots.get("start") or {}).items()
                     if isinstance(values, dict) for param, v in values.items()}
        measuring.update(meta.get("setup") or {})   # as it measured: the plan's setup applied
        for key, v in measuring.items():
            if isinstance(v, (list, dict)) or v is None:
                continue
            text = ("on" if v else "off") if isinstance(v, bool) else None if _num(v) is not None else str(v)
            con.execute("INSERT OR REPLACE INTO conditions VALUES (?,?,?,?,?)",
                        (run_id, key, _num(v), text, units.get(key)))
        from .reports.kinds import result_number
        for series in (rec.analysis or {}).get("series") or []:
            for r in series.get("results") or []:
                value, err, unit = result_number(r)
                con.execute("INSERT INTO results VALUES (?,?,?,?,?,?,?,?)", (
                    run_id, series.get("y"), series.get("group"), r.get("name"), value, err, unit,
                    None if r.get("value") is None else str(r.get("value"))))
        return 1

    # ------------------------------------------------------------------ asking it

    def find(self, text: str = "", device: str = "", project: str = "", plan: str = "", status: str = "",
             since: str = "", until: str = "", quantity: str = "", limit: int = 20,
             skip: Iterable[str] = ()) -> List[Dict[str, Any]]:
        """Runs matching every filter given, newest first. since/until: dates or times (2026-10-24, inclusive);
        quantity: a swept or measured quantity (temperature) or column (smu.V, or smu for any of its columns).
        skip: runs left out (unshared work the asker may not see)."""
        where: List[str] = []
        args: List[Any] = []
        skip = sorted(skip)
        if skip:
            where.append("run_id NOT IN (SELECT value FROM json_each(?))")
            args.append(json.dumps(skip))
        for col, value in (("device_name", device), ("project_name", project), ("plan", plan)):
            if value:
                where.append("%s LIKE ?" % col)
                args.append("%" + value + "%")
        if status:
            where.append("status = ?")
            args.append(status)
        if since:
            where.append("started >= ?")
            args.append(since)
        if until:   # the whole of that day (or minute...)
            where.append("substr(started, 1, %d) <= ?" % len(until))
            args.append(until)
        if text:
            where.append("(plan LIKE ? OR assessment LIKE ? OR device_name LIKE ? OR project_name LIKE ? "
                         "OR run_id LIKE ? OR card LIKE ?)")
            args += ["%" + text + "%"] * 6
        if quantity:
            where.append("run_id IN (SELECT run_id FROM columns WHERE quantity = ? OR key = ? OR key LIKE ?)")
            args += [quantity, quantity, quantity + ".%"]
        sql = "SELECT * FROM runs%s ORDER BY started DESC LIMIT ?" % (
            " WHERE " + " AND ".join(where) if where else "")
        with _lock:
            con = self._connect()
            con.row_factory = sqlite3.Row
            try:
                rows = [dict(r) for r in con.execute(sql, args + [max(1, min(int(limit), MAX_ROWS))])]
            finally:
                con.close()
        for r in rows:
            r["card"] = json.loads(r["card"] or "{}")
            r.pop("mtime", None)
            r.pop("marks", None)
        return rows

    def count(self) -> int:
        with _lock:
            con = self._connect()
            try:
                return con.execute("SELECT count(*) FROM runs").fetchone()[0]
            finally:
                con.close()

    def marks(self) -> List[Tuple[str, str, str, str, Dict[str, Any]]]:
        """Every run's (run_id, plan, started, finished, whose it is: its project and marks)."""
        with _lock:
            con = self._connect()
            try:
                rows = con.execute("SELECT run_id, plan, started, finished, project, marks FROM runs").fetchall()
            finally:
                con.close()
        out = []
        for run_id, plan, started, finished, project, marks in rows:
            try:
                whose = json.loads(marks) if marks else {}
            except ValueError:
                whose = {"unshared": True}   # unreadable: kept apart
            out.append((run_id, plan, started, finished, dict(whose if isinstance(whose, dict) else {},
                                                              project=project)))
        return out

    def query(self, sql: str, skip: Iterable[str] = ()) -> Tuple[List[str], List[Tuple[Any, ...]], bool]:
        """A read-only SQL query: (column names, rows, whether there were more than MAX_ROWS). Only reading is
        allowed (no writes, ATTACH or PRAGMA), and a query running longer than QUERY_SECONDS is stopped. skip:
        runs the query never reads (unshared work the asker may not see): it reads a copy of the catalog without
        them."""
        self._connect().close()
        skip = sorted(skip)
        if skip:
            con = sqlite3.connect(":memory:")
            con.execute("ATTACH DATABASE ? AS whole", (str(self.path),))
            for table in TABLES:
                con.execute("CREATE TABLE %s AS SELECT * FROM whole.%s WHERE run_id NOT IN "
                            "(SELECT value FROM json_each(?))" % (table, table), (json.dumps(skip),))
            con.commit()
            con.execute("DETACH DATABASE whole")
        else:
            con = sqlite3.connect(str(self.path), timeout=10)
        try:
            con.execute("PRAGMA query_only = 1")
            if hasattr(con, "setlimit"):   # Python 3.11+: no value over 10 MB (a huge string or blob)
                con.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 10_000_000)
            con.set_authorizer(_read_only)
            deadline = time.monotonic() + QUERY_SECONDS
            con.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10000)
            try:
                cur = con.execute(sql)
                names = [d[0] for d in cur.description or []]
                rows = cur.fetchmany(MAX_ROWS + 1)
            except sqlite3.DatabaseError as e:
                msg = str(e)
                if "interrupted" in msg:
                    raise QueryError("the query ran longer than %g s and was stopped" % QUERY_SECONDS)
                if "not authorized" in msg or "prohibited" in msg:
                    raise QueryError("only reading is allowed: one SELECT over runs, columns, conditions and results")
                raise QueryError(msg)
            except sqlite3.Warning as e:   # more than one statement
                raise QueryError(str(e))
            return names, rows[:MAX_ROWS], len(rows) > MAX_ROWS
        finally:
            con.close()


_ALLOWED = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
            getattr(sqlite3, "SQLITE_RECURSIVE", 33)}
_DENIED_FUNCTIONS = {"load_extension", "readfile", "writefile", "edit", "fts3_tokenizer", "randomblob", "zeroblob"}


def _read_only(action: int, a: Any, b: Any, db: Any, trigger: Any) -> int:
    if action not in _ALLOWED:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION and str(b or "").lower() in _DENIED_FUNCTIONS:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _h5_stats(folder: Path) -> Dict[str, Dict[str, Any]]:
    """Each column's statistics straight from run.h5 (no rows read into Python)."""
    import h5py
    import numpy as np
    out: Dict[str, Dict[str, Any]] = {}
    with h5py.File(folder / rundata.RUN_FILE, "r") as f:
        for key, ds in f.get("points", {}).items():
            if ds.dtype.kind not in ("f", "i", "u"):
                continue
            v = ds[()]
            ok = v[np.isfinite(v)]
            if ok.size:
                out[key] = {"n": int(ok.size), "min": float(ok.min()), "max": float(ok.max()),
                            "mean": float(ok.mean()), "first": float(ok[0]), "last": float(ok[-1])}
        traces = f.get("traces", {})
        for key, ds in traces.items():
            base = key.rsplit(".", 1)[0]
            if key.endswith((".length", ".x", ".axis")) and base in traces:
                continue
            out[key] = {"n": int(ds.shape[0]), "samples": int(ds.shape[1]) if ds.ndim > 1 else 0, "trace": True}
        for key, item in f.get("frames", {}).items():
            out[key] = {"n": int(item.shape[0]) if hasattr(item, "shape") else len(item), "image": True}
    return out
