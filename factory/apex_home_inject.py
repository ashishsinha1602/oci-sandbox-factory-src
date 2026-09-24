"""Inject the Ajax + AI request UI into the Home page of an APEX export.

Used by apex_customize.py. Replaces the wizard's hero region on page 1 with
apex_home/home.html (static content region) and adds the Ajax callback
process from apex_home/ajax.plsql.
"""

from __future__ import annotations

import pathlib
import re

HERE = pathlib.Path(__file__).resolve().parent
HOME_PAGE = 1
HOME_REGION_ID = 9200000000000001
HOME_PROCESS_ID = 9200000000000002
STATIC_FILE_ID = 9200000000000003
STATIC_FILE_NAME = "sf.js"
# A Static Content region's source is VARCHAR2(32767). The page's JavaScript
# outgrew it and was silently truncated mid-function, which left the page inert.
# The script now ships as an application static file (a BLOB, no such limit) and
# the region just references it.
SCRIPT_RE = re.compile(r"<script>([\s\S]*?)</script>\s*$")
STANDARD_REGION_TEMPLATE = 4502917002193490937
NL = "\n"

HERO_RE = re.compile(
    r"wwv_flow_imp_page\.create_page_plug\(\s+p_id=>wwv_flow_imp\.id\(\d+\)\s+,p_plug_name=>'Sandbox Factory'.*?\n\);\n",
    re.S,
)
NAV_RE = re.compile(
    r"wwv_flow_imp_page\.create_page_plug\(\s+p_id=>wwv_flow_imp\.id\(\d+\)\s+,p_plug_name=>'Page Navigation'.*?\n\);\n",
    re.S,
)
PROC_RE = re.compile(
    r"wwv_flow_imp_page\.create_page_process\(\s+p_id=>wwv_flow_imp\.id\(\d+\)(?:.*?\n)*?,p_process_name=>'SF'.*?\n\);\n",
    re.S,
)


def join_lines(text: str) -> str:
    """Render text as wwv_flow_string.join(wwv_flow_t_varchar2('l1','l2',...)).
    Every element must stay under 4000 bytes; long lines are split with ||."""
    parts = []
    for line in text.replace("\r", "").split(NL):
        esc = line.replace("'", "''")
        chunks = []
        while len(esc.encode()) > 3500:
            chunks.append("'" + esc[:3000] + "'")
            esc = esc[3000:]
        chunks.append("'" + esc + "'")
        parts.append("||".join(chunks))
    return "wwv_flow_string.join(wwv_flow_t_varchar2(" + NL + ("," + NL).join(parts) + "))"


def hex_table(data: bytes, per_element: int = 200) -> str:
    """Render bytes as the g_varchar2_table hex assignments APEX uses for files.

    200 hex characters (100 bytes) per element is what APEX itself emits; the
    collection's element type is narrower than a normal VARCHAR2 and a longer
    chunk fails the import with ORA-06502.
    """
    hexed = data.hex().upper()
    lines = ["wwv_flow_imp.g_varchar2_table := wwv_flow_imp.empty_varchar2_table;"]
    for i in range(0, len(hexed), per_element):
        lines.append(f"wwv_flow_imp.g_varchar2_table({i // per_element + 1}) := '{hexed[i:i + per_element]}';")
    return NL.join(lines)


def static_file_block(script: str) -> str:
    """The page script as an application static file, referenced as #APP_FILES#sf.js."""
    return f"""prompt --application/shared_components/files/{STATIC_FILE_NAME.replace('.', '_')}
begin
{hex_table(script.encode("utf-8"))}
wwv_flow_imp_shared.create_app_static_file(
 p_id=>wwv_flow_imp.id({STATIC_FILE_ID})
,p_file_name=>'{STATIC_FILE_NAME}'
,p_mime_type=>'text/javascript'
,p_file_charset=>'utf-8'
,p_file_content=>wwv_flow_imp.varchar2_to_blob(wwv_flow_imp.g_varchar2_table)
);
end;
/
"""


def split_home() -> tuple[str, str]:
    """(region markup with a script reference, the script itself).

    region.html and sf.js are read back out of the LIVE application and are the
    source of truth when present: the browser UI is edited in APEX as well as
    here, and splitting home.html instead silently reverted that work on
    2026-09-24. home.html remains the fallback for a fresh install.
    """
    region = HERE / "apex_home" / "region.html"
    script = HERE / "apex_home" / "sf.js"
    if region.exists() and script.exists():
        markup = region.read_text(encoding="utf-8")
        if len(markup.encode()) > 30000:
            raise SystemExit(f"region.html is {len(markup.encode())} bytes; the APEX limit is 32767")
        return markup, script.read_text(encoding="utf-8")

    html = (HERE / "apex_home" / "home.html").read_text(encoding="utf-8")
    m = SCRIPT_RE.search(html)
    if not m:
        raise SystemExit("home.html: expected a trailing <script> block to extract")
    script = m.group(1)
    markup = html[: m.start()] + f'<script src="#APP_FILES#{STATIC_FILE_NAME}"></script>' + NL
    if len(markup.encode()) > 30000:
        raise SystemExit(f"region markup is {len(markup.encode())} bytes; the APEX limit is 32767")
    return markup, script


def home_blocks() -> str:
    html, _ = split_home()
    plsql = (HERE / "apex_home" / "ajax.plsql").read_text(encoding="utf-8")
    if len(plsql.encode()) > 30000:
        raise SystemExit(f"ajax.plsql is {len(plsql.encode())} bytes; the APEX limit is 32767")
    return f"""wwv_flow_imp_page.create_page_plug(
 p_id=>wwv_flow_imp.id({HOME_REGION_ID})
,p_plug_name=>'Sandbox Factory'
,p_static_id=>'factory'
,p_region_template_options=>'#DEFAULT#:t-Region--noPadding:t-Region--hideHeader:t-Region--noUI'
,p_plug_template=>{STANDARD_REGION_TEMPLATE}
,p_plug_display_sequence=>5
,p_plug_item_display_point=>'ABOVE'
,p_plug_source=>{join_lines(html)}
,p_plug_query_num_rows=>15
,p_attributes=>wwv_flow_t_plugin_attributes(wwv_flow_t_varchar2(
  'expand_shortcuts', 'N',
  'output_as', 'HTML')).to_clob
);
wwv_flow_imp_page.create_page_process(
 p_id=>wwv_flow_imp.id({HOME_PROCESS_ID})
,p_process_sequence=>10
,p_process_point=>'ON_DEMAND'
,p_process_type=>'NATIVE_PLSQL'
,p_process_name=>'SF'
,p_static_id=>'sf'
,p_process_sql_clob=>{join_lines(plsql)}
,p_process_clob_language=>'PLSQL'
,p_internal_uid=>{HOME_PROCESS_ID}
);
"""


def install_static_file(text: str) -> str:
    """Add (or replace) the sf.js static file alongside the export's other files."""
    block = static_file_block(split_home()[1])
    head = "prompt --application/shared_components/files/" + STATIC_FILE_NAME.replace(".", "_")
    start = text.find(head)
    if start >= 0:                                  # replace the block we wrote last time
        tail = text.find(NL + "/" + NL, start)
        if tail < 0:
            raise SystemExit("existing sf.js block looks malformed")
        return text[:start] + block + text[tail + len(NL + "/" + NL):]
    anchor = text.find("prompt --application/shared_components/files/")
    if anchor < 0:
        anchor = text.find("prompt --application/pages/page_00001")
    if anchor < 0:
        raise SystemExit("nowhere to add the static file")
    return text[:anchor] + block + text[anchor:]


def rewrite_home(text: str) -> str:
    start = text.find(f"prompt --application/pages/page_{HOME_PAGE:05d}")
    end = text.find("prompt --application/pages/page_", start + 10)
    if start < 0:
        raise SystemExit("home page not found in export")
    page = text[start:end]
    page = HERO_RE.sub("", page)   # wizard hero region
    page = NAV_RE.sub("", page)    # wizard "Requests / Sandboxes" card tiles (the side menu has them)
    page = PROC_RE.sub("", page)   # an earlier copy of our process
    marker = NL + "end;" + NL + "/"
    if marker not in page:
        raise SystemExit("unexpected home page block layout")
    page = page.replace(marker, NL + home_blocks() + "end;" + NL + "/", 1)
    return install_static_file(text[:start] + page + text[end:])
