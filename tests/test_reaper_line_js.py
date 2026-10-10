"""The page header line built from reaper_last_pass (sf.js reaperLine), run in node with a two-element DOM stand-in:
recent, stale (red, says expired sandboxes are not being deleted) and never. Skips without node."""
import json
import pathlib
import re
import shutil
import subprocess

import pytest

SF = pathlib.Path(__file__).resolve().parent.parent / "factory" / "apex_home" / "sf.js"


def _run(iso):
    if not shutil.which("node"):
        pytest.skip("node not installed")
    src = SF.read_text(encoding="utf-8")
    fn = re.search(r"function reaperLine\(iso\)\{.*?\n\}\r?\n", src, re.S).group(0)
    js = """
var kids=[]; var who={appendChild:function(e){kids.push(e)}};
var document={createElement:function(){return {style:{}}}};
function $(s){return s==='#sf-who'?who:null}
%s
reaperLine(%s);
console.log(JSON.stringify({text:kids[0].textContent, color:kids[0].style.color||'', title:kids[0].title||''}));
""" % (fn, json.dumps(iso))
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _ago(minutes):
    import datetime as dt
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_a_recent_pass_reads_as_minutes_ago():
    r = _run(_ago(12))
    assert "last checked 12 min ago" in r["text"] and r["color"] == "" and "UTC" in r["title"]


def test_a_stale_pass_is_red_and_says_what_it_means():
    r = _run(_ago(200))
    assert r["color"] and "not being deleted" in r["text"] and "h ago" in r["text"]


def test_no_pass_yet():
    assert "not checked yet" in _run("")["text"]
