"""Load the assistant's prompts into SBX.FACTORY_PROMPTS.

    python prompts.py            # upsert every factory/prompts/*.txt

The APEX ajax process reads them at run time (prompt_text/fill in
apex_home/ajax.plsql). Keeping them in a table means the process stays well
under APEX's 32767-byte cap however long the briefs get, and a prompt can be
tuned without redeploying the page. Placeholders the process fills:
{BLOCKS} {TEMPLATES} {REGISTRY} {SANDBOXES} {PRICES} {MIGRATION} {REQUEST}.
"""
import pathlib
import sys

import oracledb

import controldb

HERE = pathlib.Path(__file__).resolve().parent
DDL = """create table factory_prompts (
  key        varchar2(40) primary key,
  text       clob not null,
  updated_at timestamp default systimestamp not null)"""


def load(conn=None) -> list[str]:
    own = conn is None
    conn = conn or controldb.connect("ADMIN")
    cur = conn.cursor()
    cur.execute("alter session set current_schema = " + controldb.SCHEMA)
    cur.execute("select count(*) from all_tables where owner = :o and table_name = 'FACTORY_PROMPTS'", o=controldb.SCHEMA)
    if not cur.fetchone()[0]:
        cur.execute(DDL)
    done = []
    for f in sorted((HERE / "prompts").glob("*.txt")):
        text = f.read_text(encoding="utf-8")
        # One CLOB bind per statement: a MERGE that binds the same large text
        # twice failed with ORA-03146 (LONG binds on this driver/database),
        # first on a laptop, then again on a fresh install's worker.
        cur.setinputsizes(t=oracledb.DB_TYPE_CLOB)
        cur.execute("update factory_prompts set text = :t, updated_at = systimestamp where key = :k", t=text, k=f.stem)
        if cur.rowcount == 0:
            cur.setinputsizes(t=oracledb.DB_TYPE_CLOB)
            cur.execute("insert into factory_prompts (key, text) values (:k, :t)", k=f.stem, t=text)
        done.append(f"{f.stem} ({len(text)} chars)")
    conn.commit()
    if own:
        conn.close()
    return done


if __name__ == "__main__":
    for line in load():
        print("loaded", line)
    sys.exit(0)
