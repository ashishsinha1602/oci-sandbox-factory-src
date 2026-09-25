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
        # Past ~8K chars a plain string bind is sent as a LONG, and two LONG
        # binds in one MERGE fail with ORA-03146; bind the text as a CLOB.
        cur.setinputsizes(t1=oracledb.DB_TYPE_CLOB, t2=oracledb.DB_TYPE_CLOB)
        cur.execute("""merge into factory_prompts t using (select :k as key from dual) s on (t.key = s.key)
                       when matched then update set text = :t1, updated_at = systimestamp
                       when not matched then insert (key, text) values (:k2, :t2)""",
                    k=f.stem, t1=text, k2=f.stem, t2=text)
        done.append(f"{f.stem} ({len(text)} chars)")
    conn.commit()
    if own:
        conn.close()
    return done


if __name__ == "__main__":
    for line in load():
        print("loaded", line)
    sys.exit(0)
