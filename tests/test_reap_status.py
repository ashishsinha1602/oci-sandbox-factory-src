"""Expired sandboxes: a delete that fails must show on the card, must not stop the other reapers, and a card that
says "failed" must clear once the sandbox is really gone. No cloud and no database: the reapers and the control
table are stand-ins. Run: python -m pytest tests/test_reap_status.py"""
import argparse
import pathlib
import sys
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "factory"))

import sandbox_factory as sf  # noqa: E402
import worker  # noqa: E402


class FakeTable:
    """sbx.sandbox_requests, reduced to what the reaper reads and writes."""

    def __init__(self):
        self.rows = []

    def add(self, sandbox_id, action, status, requester="u@x", log="", request_text=""):
        self.rows.append(dict(id=len(self.rows) + 1, sandbox_id=sandbox_id, requester=requester, action=action,
                              status=status, log=log, error=None, request_text=request_text))

    def latest(self, sid):
        mine = [r for r in self.rows if r["sandbox_id"] == sid]
        return mine[-1] if mine else None

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        pass


class FakeCursor:
    def __init__(self, t):
        self.t, self._one, self._all = t, None, []

    def execute(self, sql, params=None, **kw):
        s = " ".join(sql.split()).lower()
        if s.startswith("select id, requester, action, status from sbx.sandbox_requests"):
            r = self.t.latest(kw["s"])
            self._one = (r["id"], r["requester"], r["action"], r["status"]) if r else None
        elif s.startswith("insert into sbx.sandbox_requests"):
            status = "DONE" if "'destroy', 1, 'done'" in s else "FAILED"
            requester, sid = params[0], params[1]
            log = params[-1]
            text = "deleted on expiry" if status == "DONE" and "deleted on expiry" in s else (
                "expired: automatic delete" if "expired: automatic delete" in s else "destroyed outside the app (reaper or CLI)")
            self.t.add(sid, "DESTROY", status, requester=requester, log=log if isinstance(log, str) else "", request_text=text)
            if status == "FAILED":
                self.t.rows[-1]["error"] = params[2]
        elif s.startswith("update sbx.sandbox_requests set finished_at = systimestamp, error"):
            r = next(x for x in self.t.rows if x["id"] == kw["i"])
            r["error"], r["log"] = kw["e"], r["log"] + "\n" + kw["l"]
        elif "row_number() over (partition by sandbox_id" in s:
            latest = {}
            for r in self.t.rows:
                latest[r["sandbox_id"]] = r
            want = []
            for sid, r in latest.items():
                if (r["status"] == "DONE" and r["action"] in ("CREATE", "DEPLOY")) or (
                        "(status = 'failed' and action = 'destroy')" in s and r["status"] == "FAILED" and r["action"] == "DESTROY"):
                    want.append((sid, r["requester"]))
            self._all = want
        else:
            raise AssertionError("unexpected SQL: " + s[:120])

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._all


# --- the stack reaper ------------------------------------------------------------------------------------

def _stack(sid, exp="2000-01-01T00:00Z"):
    return types.SimpleNamespace(id=f"ocid.{sid}", display_name=f"sbx-{sid}",
                                 freeform_tags={"managed_by": "sandbox-factory", "sandbox_id": sid, "expires": exp})


def test_one_stack_that_will_not_delete_does_not_stop_the_others(monkeypatch):
    stacks = [_stack("bad1"), _stack("good2"), _stack("good3")]
    rm = types.SimpleNamespace(list_stacks=lambda **k: types.SimpleNamespace(data=stacks))
    monkeypatch.setattr(sf, "config", lambda: {})
    monkeypatch.setattr(sf, "foundation", lambda: {"compartments": {"control": "c"}})
    monkeypatch.setattr(sf, "client", lambda cls: rm)
    deleted = []

    def destroy(rm_, s):
        if s.freeform_tags["sandbox_id"] == "bad1":
            raise SystemExit("DESTROY job FAILED: bucket not empty")
        deleted.append(s.freeform_tags["sandbox_id"])

    monkeypatch.setattr(sf, "destroy_stack", destroy)
    out = sf.cmd_reap(argparse.Namespace(dry_run=False))
    assert deleted == ["good2", "good3"]
    assert {r["sandbox_id"]: r["error"] for r in out} == {
        "bad1": "DESTROY job FAILED: bucket not empty", "good2": None, "good3": None}


# --- every reaper runs, whatever the one before it did ---------------------------------------------------

def test_a_failing_stack_reaper_does_not_skip_the_schema_reaper(monkeypatch):
    def boom(_args):
        raise RuntimeError("Resource Manager unreachable")

    monkeypatch.setattr(worker.sf, "cmd_reap", boom)
    monkeypatch.setattr(worker.shared_db, "reap", lambda now, failed: (failed.update({"sch2": "drop failed"}), ["sch1"])[1])
    res = worker.run_reapers()
    assert res == {"sch1": None, "sch2": "drop failed"}


# --- the card --------------------------------------------------------------------------------------------

def test_failed_delete_shows_on_the_card_then_clears_when_it_works():
    t = FakeTable()
    t.add("s1", "CREATE", "DONE")
    worker.record_reap(t, {"s1": "bucket not empty"})
    card = t.latest("s1")
    assert (card["action"], card["status"]) == ("DESTROY", "FAILED")
    assert "bucket not empty" in card["log"] and "30 minutes" in card["log"]

    worker.record_reap(t, {"s1": "bucket not empty"})            # next pass, still failing
    assert len([r for r in t.rows if r["sandbox_id"] == "s1"]) == 2, "a retry updates the card, it does not add one"
    assert "tried again, still failing" in t.latest("s1")["log"]

    worker.record_reap(t, {"s1": None})                           # the pass after, it works
    card = t.latest("s1")
    assert (card["action"], card["status"], card["request_text"]) == ("DESTROY", "DONE", "deleted on expiry")


def test_a_successful_reap_is_recorded_once():
    t = FakeTable()
    t.add("s2", "CREATE", "DONE")
    worker.record_reap(t, {"s2": None})
    worker.record_reap(t, {"s2": None})
    assert [(r["action"], r["status"]) for r in t.rows] == [("CREATE", "DONE"), ("DESTROY", "DONE")]


def test_a_sandbox_made_outside_the_app_is_ignored():
    t = FakeTable()
    worker.record_reap(t, {"cli-made": "whatever"})
    assert t.rows == []


def test_reconcile_clears_a_failed_card_once_the_sandbox_is_gone(monkeypatch):
    """Deleted by hand in the console after the reaper failed: the card must not say FAILED for ever."""
    t = FakeTable()
    t.add("s3", "CREATE", "DONE")
    worker.record_reap(t, {"s3": "private endpoint still attached"})
    rm = types.SimpleNamespace(list_stacks=lambda **k: types.SimpleNamespace(data=[]))
    monkeypatch.setattr(worker.sf, "foundation", lambda: {"compartments": {"control": "c"}})
    monkeypatch.setattr(worker.sf, "client", lambda cls: rm)
    monkeypatch.setattr(worker.shared_db, "alive_ids", lambda: set())
    worker.reconcile(t)
    card = t.latest("s3")
    assert (card["action"], card["status"]) == ("DESTROY", "DONE")
