"""RAG over documents with only what Oracle provides, inside the sandbox database.

The user drops files into the sandbox's docs bucket (PDF, Word, HTML, text,
JSON, and images). Oracle does the rest:

  * Select AI's vector index (DBMS_CLOUD_AI.CREATE_VECTOR_INDEX) watches the
    bucket, chunks and embeds every document with OCI Generative AI, and
    refreshes itself every few minutes - Oracle's own crawler.
  * Oracle's ingestion skips pictures, so a scheduled job in the database turns
    every image into text first: OCI Generative AI vision transcribes the text
    in the picture and describes what it shows, and the description is written
    next to the image as <image>.txt, which the index then ingests and cites.
  * Questions run inside the database as SELECT AI NARRATE, with Oracle's
    permissions and audit, and are exposed through ORDS as POST /rag/ask.

Everything authenticates as the database itself (resource principal); no key
is stored in the sandbox. Best effort like Select AI: a sandbox is never
failed because RAG could not be configured, but what failed is printed.
"""
from __future__ import annotations

import json

import sandbox_factory as sf

EMBEDDING_MODEL = "cohere.embed-multilingual-v3.0"     # 1024 dimensions, text in any language
VISION_MODEL = "google.gemini-2.5-flash"                 # reads text in pictures and describes them
REFRESH_MINUTES = 5

def _plsql_image_job(base: str, compartment: str, genai: str) -> str:
    """The scheduler job body. Kept as one string with the request built in
    PL/SQL; send_request takes a BLOB body, so the CLOB is converted."""
    return r"""
declare
  l_base   varchar2(1000) := '__BASE__';
  l_cred   varchar2(64)   := 'OCI$RESOURCE_PRINCIPAL';
  l_n      number;
  l_blob   blob;
  l_b64    clob;
  l_req    clob;
  l_body   blob;
  l_resp   dbms_cloud_types.resp;
  l_json   json_object_t;
  l_text   clob;
  l_mime   varchar2(40);
  l_chunk  raw(24000);
  l_len    integer;
  l_pos    integer;
  l_dest   integer;
  l_src    integer;
  l_lang   integer := 0;
  l_warn   integer;
  function part_text(p json_object_t) return clob is
    l_choices json_array_t; l_msg json_object_t; l_content json_array_t; l_part json_object_t; l_acc clob;
  begin
    l_choices := p.get_object('chatResponse').get_array('choices');
    l_msg := treat(l_choices.get(0) as json_object_t).get_object('message');
    l_content := l_msg.get_array('content');
    dbms_lob.createtemporary(l_acc, true);
    for i in 0 .. l_content.get_size - 1 loop
      l_part := treat(l_content.get(i) as json_object_t);
      if l_part.get_string('type') = 'TEXT' then dbms_lob.append(l_acc, l_part.get_clob('text')); end if;
    end loop;
    return l_acc;
  end;
begin
  for o in (select object_name from dbms_cloud.list_objects(l_cred, l_base)
             where regexp_like(lower(object_name), '\.(png|jpe?g|gif|webp)$')) loop
    select count(*) into l_n from dbms_cloud.list_objects(l_cred, l_base) where object_name = o.object_name || '.txt';
    if l_n = 0 then
      begin
        l_blob := dbms_cloud.get_object(l_cred, l_base || o.object_name);
        l_mime := case when lower(o.object_name) like '%.png' then 'image/png'
                       when lower(o.object_name) like '%.gif' then 'image/gif'
                       when lower(o.object_name) like '%.webp' then 'image/webp' else 'image/jpeg' end;
        dbms_lob.createtemporary(l_b64, true);
        l_len := dbms_lob.getlength(l_blob); l_pos := 1;
        while l_pos <= l_len loop
          l_chunk := dbms_lob.substr(l_blob, 24000, l_pos);
          dbms_lob.append(l_b64, replace(replace(utl_raw.cast_to_varchar2(utl_encode.base64_encode(l_chunk)), chr(13), ''), chr(10), ''));
          l_pos := l_pos + 24000;
        end loop;
        dbms_lob.createtemporary(l_req, true);
        dbms_lob.append(l_req, '{"compartmentId":"__COMPARTMENT__","servingMode":{"servingType":"ON_DEMAND","modelId":"__MODEL__"},'
          || '"chatRequest":{"apiFormat":"GENERIC","maxTokens":2000,"temperature":0.1,"messages":[{"role":"USER","content":['
          || '{"type":"TEXT","text":"This image is part of a document collection. First transcribe every piece of visible text exactly. Then describe what the image shows: charts with their values, tables row by row, diagrams, photos. Plain text only."},'
          || '{"type":"IMAGE","imageUrl":{"url":"data:' || l_mime || ';base64,');
        dbms_lob.append(l_req, l_b64);
        dbms_lob.append(l_req, '"}}]}]}}');
        dbms_lob.createtemporary(l_body, true);
        l_dest := 1; l_src := 1; l_warn := 0;
        dbms_lob.converttoblob(l_body, l_req, dbms_lob.lobmaxsize, l_dest, l_src, dbms_lob.default_csid, l_lang, l_warn);
        l_resp := dbms_cloud.send_request(
          credential_name => l_cred,
          uri             => '__GENAI__/20231130/actions/chat',
          method          => dbms_cloud.method_post,
          headers         => json_object('Content-Type' value 'application/json'),
          body            => l_body);
        if dbms_cloud.get_response_status_code(l_resp) = 200 then
          l_json := json_object_t.parse(dbms_cloud.get_response_text(l_resp));
          l_text := 'Image ' || o.object_name || chr(10) || part_text(l_json);
          dbms_lob.createtemporary(l_body, true);
          l_dest := 1; l_src := 1; l_warn := 0;
          dbms_lob.converttoblob(l_body, l_text, dbms_lob.lobmaxsize, l_dest, l_src, dbms_lob.default_csid, l_lang, l_warn);
          dbms_cloud.put_object(l_cred, l_base || o.object_name || '.txt', l_body);
        else
          dbms_cloud.put_object(l_cred, l_base || o.object_name || '.failed',
            utl_raw.cast_to_raw(substr(dbms_cloud.get_response_text(l_resp), 1, 2000)));
        end if;
      exception when others then
        dbms_cloud.put_object(l_cred, l_base || o.object_name || '.failed', utl_raw.cast_to_raw(substr(sqlerrm, 1, 2000)));
      end;
    end if;
  end loop;
end;""".replace("__BASE__", base).replace("__COMPARTMENT__", compartment).replace("__MODEL__", VISION_MODEL).replace("__GENAI__", genai)


def enable(outputs: dict, region: str, bucket: str, namespace: str, chat_model: str) -> dict | None:
    """Wire RAG into the sandbox database. Returns what to show on the card."""
    adb = outputs.get("adb") or {}
    connect, pw = adb.get("connect_string"), adb.get("admin_password")
    if not (connect and pw and bucket):
        return None
    import oracledb
    from worker import adb_dsn
    compartment = (outputs.get("sandbox") or {}).get("compartment_id") or sf.foundation()["compartments"]["sandboxes"]
    os_host = f"objectstorage.{region}.oraclecloud.com"
    genai = f"https://inference.generativeai.{region}.oci.oraclecloud.com"
    base = f"https://{os_host}/n/{namespace}/b/{bucket}/o/docs/"
    result = {"bucket": bucket, "prefix": "docs/", "profile": "SANDBOX_RAG", "index": "DOCS_IDX",
              "refresh_minutes": REFRESH_MINUTES, "images": "described by OCI Generative AI vision, every 5 minutes"}
    try:
        with oracledb.connect(user="ADMIN", password=pw, dsn=adb_dsn(connect), ssl_server_dn_match=True) as db:
            cur = db.cursor()
            cur.execute("begin dbms_cloud_admin.enable_resource_principal(); end;")
            for host in (os_host, f"inference.generativeai.{region}.oci.oraclecloud.com"):
                cur.execute("""
                    begin
                      dbms_network_acl_admin.append_host_ace(
                        host => :h,
                        ace  => xs$ace_type(privilege_list => xs$name_list('http', 'connect', 'resolve'),
                                            principal_name => 'ADMIN', principal_type => xs_acl.ptype_db));
                    end;""", h=host)
            # a profile of its own: the chat model plus the embedding model the index uses
            cur.execute("""
                begin
                  begin dbms_cloud_ai.drop_vector_index('DOCS_IDX'); exception when others then null; end;
                  begin dbms_cloud_ai.drop_profile('SANDBOX_RAG'); exception when others then null; end;
                  dbms_cloud_ai.create_profile(profile_name => 'SANDBOX_RAG', attributes => :attrs);
                end;""", attrs=json.dumps({
                "provider": "oci", "credential_name": "OCI$RESOURCE_PRINCIPAL", "region": region,
                "model": chat_model, "embedding_model": EMBEDDING_MODEL,
                "provider_endpoint": genai, "oci_compartment_id": compartment, "oci_apiformat": "GENERIC",
            }))
            # Oracle's crawler: the vector index over the docs prefix, refreshed on a schedule
            cur.execute("""
                begin
                  dbms_cloud_ai.create_vector_index(index_name => 'DOCS_IDX', attributes => :attrs);
                end;""", attrs=json.dumps({
                "vector_db_provider": "oracle", "location": base,
                "object_storage_credential_name": "OCI$RESOURCE_PRINCIPAL",
                "profile_name": "SANDBOX_RAG", "vector_dimension": 1024, "vector_distance_metric": "cosine",
                "chunk_size": 1024, "chunk_overlap": 128, "refresh_rate": REFRESH_MINUTES,
                "similarity_threshold": 0, "match_limit": 5,
            }))
            # pictures become text before the crawler sees them
            cur.execute("""
                begin
                  begin dbms_scheduler.drop_job('ADMIN.DOCS_IMAGE_TEXT', true); exception when others then null; end;
                  dbms_scheduler.create_job(
                    job_name        => 'ADMIN.DOCS_IMAGE_TEXT',
                    job_type        => 'PLSQL_BLOCK',
                    job_action      => :body,
                    start_date      => systimestamp + interval '1' minute,
                    repeat_interval => 'FREQ=MINUTELY;INTERVAL=' || :every,
                    enabled         => true,
                    comments        => 'Describes every new image in the docs bucket as text, so the vector index can ingest it');
                end;""", body=_plsql_image_job(base, compartment, genai), every=REFRESH_MINUTES)
            # ask over REST: POST <rest_base>rag/ask {"question": "..."}, as ADMIN (basic auth)
            cur.execute("""
                begin
                  ords.define_module(p_module_name => 'rag', p_base_path => '/rag/', p_items_per_page => 0, p_status => 'PUBLISHED');
                  ords.define_template(p_module_name => 'rag', p_pattern => 'ask');
                  ords.define_handler(p_module_name => 'rag', p_pattern => 'ask', p_method => 'POST',
                    p_source_type => 'plsql/block', p_mimes_allowed => 'application/json',
                    p_source => q'[
                      declare
                        l_q varchar2(4000) := :question;
                        l_a clob;
                      begin
                        dbms_cloud_ai.set_profile('SANDBOX_RAG');
                        l_a := dbms_cloud_ai.generate(prompt => l_q, profile_name => 'SANDBOX_RAG', action => 'narrate');
                        htp.p(json_object('question' value l_q, 'answer' value l_a returning clob));
                      exception when others then
                        htp.p(json_object('question' value l_q, 'error' value substr(sqlerrm, 1, 500)));
                      end;]');
                  begin
                    ords.create_privilege(p_name => 'rag.ask', p_role_name => 'SQL Developer', p_patterns => '/rag/*',
                                          p_label => 'Ask the documents', p_description => 'Basic auth as a database user');
                  exception when others then null;
                  end;
                  commit;
                end;""")
            db.commit()
        lc = outputs.get("low_code") or {}
        if lc.get("rest_base"):
            result["ask_url"] = lc["rest_base"].rstrip("/") + "/rag/ask"
        result["sql"] = "select ai narrate <your question>"
        print(f"RAG: vector index DOCS_IDX over oci://{bucket}@{namespace}/docs/, refreshed every {REFRESH_MINUTES} min; images described by {VISION_MODEL}", flush=True)
        return result
    except Exception as e:  # noqa: BLE001
        print(f"RAG not configured ({type(e).__name__}: {str(e)[:300]})", flush=True)
        return {"error": f"{type(e).__name__}: {str(e)[:200]}", "bucket": bucket}
