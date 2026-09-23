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


def home_blocks() -> str:
    html = (HERE / "apex_home" / "home.html").read_text(encoding="utf-8")
    plsql = (HERE / "apex_home" / "ajax.plsql").read_text(encoding="utf-8")
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
    return text[:start] + page + text[end:]
