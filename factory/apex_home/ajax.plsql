declare
  l_action varchar2(30) := apex_application.g_x01;
  l_in     json_object_t;
  l_out    json_object_t := json_object_t();
  l_id     number;

  -- Region and registry are tenancy-specific: they live in SBX.FACTORY_CONFIG so
  -- this page works in any tenancy without being edited.
  l_region      varchar2(64);
  l_registry    varchar2(200);
  l_prices      varchar2(4000);
  c_genai_url   varchar2(300);
  -- Model: chosen per request by the page (x02.model); must be one served in this region.
  c_genai_model varchar2(100) := 'google.gemini-2.5-flash';

  c_blocks constant varchar2(2000) :=
       'Building blocks available: an Autonomous Database (ATP, Always Free), Kafka (OCI Streaming with a Kafka-compatible endpoint), '
    || 'OCI NoSQL (serverless JSON tables), and a containerised app (a public container image, or a Git repository with a Dockerfile at its root, built for ARM). '
    || 'Every container receives ADB_CONNECT_STRING, ADB_ADMIN_PASSWORD, ADB_DB_NAME and KAFKA_BOOTSTRAP_SERVERS as environment variables. '
    || 'Sandboxes live 1 to 30 days (ttl_days 1-30, default 3; use what the user asks for), are tagged with their owner and expiry, and each gets its own public URL. ';

  -- Free Tier edition: appended to BLOCKS so the assistant never proposes what Always Free lacks.
  c_free_note constant varchar2(1200) :=
       ' THIS INSTALL IS THE FREE TIER EDITION (an Oracle Cloud Free Tier account): only an Always Free Autonomous Database '
    || '(with REST and in-database document search), OCI NoSQL tables and Object Storage buckets can be built. '
    || 'NOT available here: Kafka, Functions, Data Flow, Data Catalog, Queue, AI Data Platform, extra databases, paid database tiers, '
    || 'Select AI and extra container instances (app_instances). When the user asks for one of those, say plainly that it is not available on a '
    || 'Free Tier account, offer the free alternative, and never set those flags or add those objects.';
  c_free_apps constant varchar2(400) :=
       ' Containerised apps ARE available: they run on the Free Tier worker VM (podman, shared host, HTTP on a public IP and port, no HTTPS gateway); '
    || 'use enable_app / containers / git_url / app_files as normal.';
  c_free_noapps constant varchar2(300) :=
       ' Containerised apps and container images are NOT available on this install (its worker runs on the x86 Micro VM); decline them too.';

  -- What the current user has (for status questions and destroy-by-name).
  function cfg_value(p_key varchar2, p_default varchar2) return varchar2 is
    l_v varchar2(4000);
  begin
    select value into l_v from factory_config where key = p_key;
    return nvl(l_v, p_default);
  exception when no_data_found then return p_default;
  end;

  function my_sandboxes return clob is
    l_json clob;
  begin
    select json_arrayagg(
             json_object(
               'id'         value id,
               'sandbox_id' value sandbox_id,
               'action'     value action,
               'status'     value status,
               'created'    value to_char(created_at, 'HH24:MI'),
               'elapsed'    value round((cast(nvl(finished_at, systimestamp) as date) - cast(nvl(started_at, created_at) as date)) * 86400),
               'outputs'    value outputs,
               'error'      value error,
               'logtail'    value substr(log, greatest(1, nvl(dbms_lob.getlength(log), 0) - 1500))
               returning clob)
             order by id desc returning clob)
      into l_json
      from (select * from (
              select r.*, row_number() over (partition by sandbox_id order by id desc) rn
              from sandbox_requests r where requester = :APP_USER)
            where rn = 1
              and not (action = 'DESTROY' and status = 'DONE')
            order by id desc fetch first 8 rows only);
    return nvl(l_json, '[]');
  end;

  -- Ask OCI Generative AI as the database itself (resource principal, no keys).
  function my_history return clob is
    l_json clob;
  begin
    select json_arrayagg(
             json_object(
               'sandbox_id' value sandbox_id,
               'destroyed'  value to_char(finished_at, 'YYYY-MM-DD HH24:MI'),
               'built'      value (select to_char(min(c.finished_at), 'YYYY-MM-DD HH24:MI')
                                     from sandbox_requests c
                                    where c.sandbox_id = d.sandbox_id and c.action = 'CREATE'
                                      and c.status = 'DONE'),
               'reason'     value request_text
               returning clob)
             order by finished_at desc returning clob)
      into l_json
      from (select r.*, row_number() over (partition by sandbox_id order by id desc) rn
              from sandbox_requests r where requester = :APP_USER) d
     where rn = 1 and action = 'DESTROY' and status = 'DONE'
       and rownum <= 50;
    return nvl(l_json, '[]');
  end;

  procedure out_clob(p in clob) is
    o pls_integer := 1;
    n pls_integer := nvl(dbms_lob.getlength(p), 0);
  begin
    while o <= n loop
      htp.prn(dbms_lob.substr(p, 8000, o));
      o := o + 8000;
    end loop;
  end;

  function clob_to_blob(p in clob) return blob is
    l_blob blob;
    l_dest integer := 1;
    l_src  integer := 1;
    l_lang integer := 0;
    l_warn integer;
  begin
    dbms_lob.createtemporary(l_blob, true);
    dbms_lob.converttoblob(l_blob, p, dbms_lob.lobmaxsize, l_dest, l_src, dbms_lob.default_csid, l_lang, l_warn);
    return l_blob;
  end;

  function msg(p_role varchar2, p_text clob) return json_object_t;   -- defined below

  -- Free Tier edition: OCI Generative AI is not in Always Free, so the assistant
  -- calls Google's Gemini API with the installer's own key (SBX.FACTORY_CONFIG
  -- google_api_key). Same messages in, same text out as the OCI path below.
  function google_chat(p_messages json_array_t, p_retry boolean default true) return clob is
    l_req   json_object_t := json_object_t();
    l_cont  json_array_t  := json_array_t();
    l_sys   json_object_t;
    l_m     json_object_t;
    l_c     json_object_t;
    l_parts json_array_t;
    l_p     json_object_t;
    l_gen   json_object_t := json_object_t();
    l_resp  dbms_cloud_types.resp;
    l_body  clob;
    l_text  clob;
    l_again json_array_t;
    l_model varchar2(100) := nvl(cfg_value('google_model', ''), 'gemini-2.5-flash');
  begin
    for i in 0 .. p_messages.get_size - 1 loop
      l_m := treat(p_messages.get(i) as json_object_t);
      l_p := json_object_t(); l_p.put('text', treat(l_m.get_array('content').get(0) as json_object_t).get_clob('text'));
      l_parts := json_array_t(); l_parts.append(l_p);
      if l_m.get_string('role') = 'SYSTEM' then
        l_sys := json_object_t(); l_sys.put('parts', l_parts);
      else
        l_c := json_object_t();
        l_c.put('role', case when l_m.get_string('role') = 'ASSISTANT' then 'model' else 'user' end);
        l_c.put('parts', l_parts);
        l_cont.append(l_c);
      end if;
    end loop;
    l_req.put('contents', l_cont);
    if l_sys is not null then l_req.put('systemInstruction', l_sys); end if;
    l_gen.put('temperature', 0.2);
    l_gen.put('maxOutputTokens', 32000);
    l_req.put('generationConfig', l_gen);
    begin
      l_resp := dbms_cloud.send_request(
        credential_name => null,
        uri             => 'https://generativelanguage.googleapis.com/v1beta/models/' || l_model || ':generateContent?key=' || cfg_value('google_api_key', ''),
        method          => dbms_cloud.method_post,
        headers         => json_object('Content-Type' value 'application/json'),
        body            => clob_to_blob(l_req.to_clob));
    exception when others then
      raise_application_error(-20001, 'The Gemini API refused the request: ' || regexp_replace(substr(sqlerrm, 1, 300), 'key=[^ &]+', 'key=***'));
    end;
    l_body := dbms_cloud.get_response_text(l_resp);
    l_text := treat(treat(json_object_t.parse(l_body).get_array('candidates').get(0) as json_object_t)
                   .get_object('content').get_array('parts').get(0) as json_object_t).get_clob('text');
    if p_retry and instr(l_text, '{') = 0 then
      l_again := p_messages;
      l_again.append(msg('ASSISTANT', l_text));
      l_again.append(msg('USER', 'Answer again with ONLY the JSON object described above, no prose, no fences.'));
      return google_chat(l_again, false);
    end if;
    return l_text;
  end;

  function ai_chat(p_messages json_array_t, p_retry boolean default true) return clob is
    l_text  clob;
    l_again json_array_t;
    l_resp dbms_cloud_types.resp;
    l_req  json_object_t := json_object_t();
    l_sm   json_object_t := json_object_t();
    l_cr   json_object_t := json_object_t();
    l_body clob;
    l_j    json_object_t;
    l_comp varchar2(200);
  begin
    if cfg_value('genai_provider', 'oci') = 'google' then
      return google_chat(p_messages, p_retry);
    elsif cfg_value('edition', 'standard') = 'free' then
      raise_application_error(-20001, 'The assistant is off: OCI Generative AI is not part of Always Free. An administrator can switch it on by pasting a free Google AI Studio key in the box above the chat. The one-click starters and the form work without it.');
    end if;
    select value into l_comp from factory_config where key = 'compartment_ocid';
    l_req.put('compartmentId', l_comp);
    l_sm.put('servingType', 'ON_DEMAND');
    l_sm.put('modelId', c_genai_model);
    l_req.put('servingMode', l_sm);
    l_cr.put('apiFormat', 'GENERIC');
    l_cr.put('messages', p_messages);
    -- Gemini counts its thinking against maxTokens; a planner answer with a
    -- cost table needs room after it, or the JSON is cut off mid-string.
    l_cr.put('maxTokens', 32000);   -- thinking + a long JSON with code: 16000 was cut off mid-answer
    l_cr.put('temperature', 0.2);
    l_req.put('chatRequest', l_cr);

    l_resp := dbms_cloud.send_request(
      credential_name => 'OCI$RESOURCE_PRINCIPAL',
      uri             => c_genai_url,
      method          => dbms_cloud.method_post,
      headers         => json_object('Content-Type' value 'application/json'),
      body            => clob_to_blob(l_req.to_clob));
    l_body := dbms_cloud.get_response_text(l_resp);
    if dbms_cloud.get_response_status_code(l_resp) <> 200 then
      raise_application_error(-20001, 'Generative AI returned ' || dbms_cloud.get_response_status_code(l_resp) || ': ' || substr(l_body, 1, 300));
    end if;
    l_j := json_object_t.parse(l_body);
    l_text := treat(treat(l_j.get_object('chatResponse').get_array('choices').get(0) as json_object_t)
                   .get_object('message').get_array('content').get(0) as json_object_t).get_clob('text');
    -- Every caller expects one JSON object. Now and then the model answers in
    -- prose instead; ask once more, pointedly, before handing that back.
    if p_retry and instr(l_text, '{') = 0 then
      l_again := p_messages;
      l_again.append(msg('ASSISTANT', l_text));
      l_again.append(msg('USER', 'Answer again with ONLY the JSON object described above, no prose, no fences.'));
      return ai_chat(l_again, false);
    end if;
    return l_text;
  end;

  function templates return varchar2 is
    l_t varchar2(4000);
  begin
    select value into l_t from factory_config where key = 'templates';
    return l_t;
  exception when no_data_found then return '';
  end;

  function prompt_text(p_key varchar2) return clob is
    l clob;
  begin
    select text into l from factory_prompts where key = p_key;
    return l;
  exception when no_data_found then
    return '[prompt ' || p_key || ' is missing: run factory/prompts.py]';
  end;

  function fill(p clob) return clob is
    l clob := p;
  begin
    l := replace(l, '{MIGRATION}', prompt_text('migration'));
    l := replace(l, '{BLOCKS}', c_blocks || case when cfg_value('edition', 'standard') = 'free' then c_free_note || case when cfg_value('free_apps', '0') = '1' then c_free_apps else c_free_noapps end end);
    l := replace(l, '{TEMPLATES}', templates());
    l := replace(l, '{REGISTRY}', nvl(l_registry, '(none configured)'));
    l := replace(l, '{SANDBOXES}', my_sandboxes());
    l := replace(l, '{PRICES}', l_prices);
    l := replace(l, '{REQUEST}', substr(l_in.get_clob('text'), 1, 32000));
    return l;
  end;

  function msg(p_role varchar2, p_text clob) return json_object_t is
    l_m json_object_t := json_object_t();
    l_p json_object_t := json_object_t();
    l_c json_array_t  := json_array_t();
  begin
    l_p.put('type', 'TEXT');
    l_p.put('text', p_text);
    l_c.append(l_p);
    l_m.put('role', p_role);
    l_m.put('content', l_c);
    return l_m;
  end;

  -- submit: copy JSON values into locals first (JSON object methods are not allowed inside SQL)
  procedure do_submit is
    l_sid    varchar2(200)  := lower(substr(l_in.get_string('sandbox_id'), 1, 200));   -- checked below; a longer name must get the message, not ORA-06502
    l_act    varchar2(10)   := case when upper(substr(l_in.get_string('action'), 1, 10)) in ('CREATE','DEPLOY','DESTROY') then upper(substr(l_in.get_string('action'), 1, 10)) else 'CREATE' end;
    l_ttl    number         := least(30, greatest(1, round(nvl(l_in.get_number('ttl_days'), 3))));
    l_adb    varchar2(1)    := case when l_in.get_boolean('enable_adb') then 'Y' else 'N' end;
    l_kafka  varchar2(1)    := case when l_in.get_boolean('enable_kafka') then 'Y' else 'N' end;
    l_app    varchar2(1)    := case when l_in.get_boolean('enable_app') then 'Y' else 'N' end;
    l_image  varchar2(4000) := substr(l_in.get_string('app_image'), 1, 4000);
    l_git    varchar2(4000) := substr(l_in.get_string('git_url'), 1, 4000);
    l_port   number         := nvl(l_in.get_number('app_port'), 80);
    l_req    varchar2(4000) := substr(l_in.get_string('text'), 1, 4000);
    l_seed   clob           := l_in.get_clob('seed_sql');
    l_cont   clob           := case when l_in.has('containers') and l_in.get('containers').is_array then l_in.get_array('containers').to_clob else null end;
    l_files  clob           := l_in.get_clob('app_files');
    l_nosql  varchar2(1)    := case when l_in.get_boolean('enable_nosql') then 'Y' else 'N' end;
    l_skey   varchar2(40)   := substr(l_in.get_string('seed_key'), 1, 40);
    l_atpl   varchar2(40)   := substr(l_in.get_string('app_template'), 1, 40);
    l_dbs    clob           := case when l_in.has('databases') and l_in.get('databases').is_array
                                    then l_in.get_array('databases').to_clob else null end;
    l_fns    clob           := case when l_in.has('functions') and l_in.get('functions').is_array
                                    then l_in.get_array('functions').to_clob else null end;
    l_insts  clob           := case when l_in.has('app_instances') and l_in.get('app_instances').is_array
                                    then l_in.get_array('app_instances').to_clob else null end;
    l_bkts   clob           := case when l_in.has('buckets') and l_in.get('buckets').is_array
                                    then l_in.get_array('buckets').to_clob else null end;
    l_qs     clob           := case when l_in.has('queues') and l_in.get('queues').is_array
                                    then l_in.get_array('queues').to_clob else null end;
    l_dfj    clob           := case when l_in.has('dataflow_jobs') and l_in.get('dataflow_jobs').is_array
                                    then l_in.get_array('dataflow_jobs').to_clob else null end;
    l_cat    varchar2(1)    := case when l_in.get_boolean('enable_catalog') then 'Y' else 'N' end;
    l_aidp   varchar2(1)    := case when l_in.get_boolean('enable_aidp') then 'Y' else 'N' end;
    l_rag    varchar2(1)    := case when l_in.get_boolean('enable_rag') then 'Y' else 'N' end;
    l_kmode  varchar2(9)    := case when l_in.get_string('kafka_mode') in ('streaming','cluster') then l_in.get_string('kafka_mode') else 'cluster' end;
    l_tier   varchar2(4)    := case when l_in.get_string('adb_tier') in ('free','paid') then l_in.get_string('adb_tier') else 'paid' end;
    l_casset clob           := case when l_in.has('catalog_assets') and l_in.get('catalog_assets').is_array
                                    then l_in.get_array('catalog_assets').to_clob else null end;
    -- the user's environment variables: given on the page, never to the model
    l_env    clob           := case when l_in.has('env') and l_in.get('env').is_object
                                    then l_in.get_object('env').to_clob else null end;
    l_owner  varchar2(255);
    l_live   number;
    l_cap    number := to_number(cfg_value('max_sandboxes_per_user', '3'));
  begin
    if l_sid is null or not regexp_like(l_sid, '^[a-z][a-z0-9-]{1,19}$') then
      l_out.put('err', 'Sandbox name must be 2-20 characters: lower case letters, digits and dashes.');
      return;
    end if;
    if l_act = 'DEPLOY' and l_git is null then
      l_act := 'CREATE';
    end if;
    -- Free Tier edition: what Always Free does not include is refused here, with
    -- the free alternative, instead of being queued and failed by the worker.
    if cfg_value('edition', 'standard') = 'free' and l_act <> 'DESTROY' then
      declare
        l_no varchar2(1000);
      begin
        l_no := case when l_kafka = 'Y' then 'Kafka, ' end
             || case when cfg_value('free_apps', '0') = '1' then case when l_insts is not null then 'extra container instances, ' end
                     when l_app = 'Y' or l_image is not null or l_git is not null or l_cont is not null or l_files is not null
                      or l_atpl is not null or l_insts is not null then 'containerised apps, ' end
             || case when l_fns is not null then 'Functions, ' end
             || case when l_dfj is not null then 'Data Flow, ' end
             || case when l_qs is not null then 'Queue, ' end
             || case when l_cat = 'Y' then 'Data Catalog, ' end
             || case when l_aidp = 'Y' then 'AI Data Platform, ' end
             || case when l_dbs is not null then 'extra databases, ' end;
        if l_no is not null then
          l_out.put('err', 'Not available on an Oracle Cloud Free Tier account: ' || rtrim(l_no, ', ')
                        || '. This install can build an Always Free Autonomous Database (with REST and in-database document search), NoSQL tables and Object Storage buckets.');
          return;
        end if;
        l_tier := 'free';
      end;
    end if;

    -- A sandbox belongs to whoever first asked for it. Without this check any
    -- signed-in user could destroy someone else's sandbox, or reuse its name and
    -- have Resource Manager update their stack instead of creating a new one.
    select min(requester) into l_owner from sandbox_requests where sandbox_id = l_sid;
    if l_owner is not null and l_owner <> :APP_USER then
      l_out.put('err', 'Sandbox "' || l_sid || '" belongs to someone else. Pick another name.');
      return;
    end if;
    if l_act = 'DESTROY' and l_owner is null then
      l_out.put('err', 'No sandbox called "' || l_sid || '" that belongs to you.');
      return;
    end if;

    -- Per-user cap on live sandboxes, so one person cannot drain the budget.
    if l_act <> 'DESTROY' and l_owner is null then
      select count(*) into l_live from (
        select sandbox_id, action, status,
               row_number() over (partition by sandbox_id order by id desc) rn
          from sandbox_requests where requester = :APP_USER)
       where rn = 1 and not (action = 'DESTROY' and status = 'DONE');
      if l_live >= l_cap then
        l_out.put('err', 'You already have ' || l_live || ' sandboxes, and the limit is ' || l_cap
                      || '. Destroy one before creating another, or build again under the name of one that FAILED to replace it.');
        return;
      end if;
    end if;

    insert into sandbox_requests
      (requester, sandbox_id, action, ttl_days, enable_adb, adb_tier, enable_kafka, kafka_mode, enable_nosql, enable_app, app_image, git_url, app_port, request_text, seed_sql, app_containers, app_files, seed_key, app_template, adb_databases, functions, app_instances, buckets, queues, dataflow_jobs, enable_catalog, catalog_assets, enable_aidp, user_env, enable_rag)
    values
      (:APP_USER, l_sid, l_act, l_ttl, l_adb, l_tier, l_kafka, l_kmode, l_nosql, l_app, l_image, l_git, l_port, l_req, l_seed, l_cont, l_files, l_skey, l_atpl, l_dbs, l_fns, l_insts, l_bkts, l_qs, l_dfj, l_cat, l_casset, l_aidp, l_env, l_rag)
    returning id into l_id;
    l_out.put('id', l_id);
  end;

begin
  l_in := json_object_t.parse(nvl(apex_application.g_x02, '{}'));
  -- a request too big for x02 (a pipeline's files, a function's source) rides whole in the CLOB
  if l_in.has('__clob') and apex_application.g_clob_01 is not null then
    l_in := json_object_t.parse(apex_application.g_clob_01);
  end if;
  -- The models this region actually serves, and where, come from the tenancy
  -- profile (factory/profile.py, refreshed by every worker at start); a model
  -- the user picked is used only if it is one of them.
  c_genai_model := nvl(cfg_value('genai_model', ''), c_genai_model);
  if l_in.get_string('model') is not null
     and instr(cfg_value('genai_models', '[]'), '"' || l_in.get_string('model') || '"') > 0 then
    c_genai_model := l_in.get_string('model');
  end if;

  l_prices   := cfg_value('oci_prices', '');
  l_region   := nvl(cfg_value('genai_region', ''), cfg_value('region', ''));
  l_registry := cfg_value('registry_prefix', '');
  c_genai_url := 'https://inference.generativeai.' || l_region || '.oci.oraclecloud.com/20231130/actions/chat';

  if l_action = 'config' then
    l_out.put('registry_prefix', l_registry);
    l_out.put('region', l_region);
    l_out.put('models', json_array_t.parse(cfg_value('genai_models', '[]')));
    l_out.put('prices', l_prices);
    l_out.put('prices_updated', cfg_value('prices_updated', ''));
    l_out.put('model', c_genai_model);
    l_out.put('user', :APP_USER);
    l_out.put('cap', to_number(cfg_value('max_sandboxes_per_user', '3')));
    l_out.put('edition', cfg_value('edition', 'standard'));
    l_out.put('provider', cfg_value('genai_provider', 'oci'));
    l_out.put('free_apps', cfg_value('free_apps', '0') = '1');
    begin
      select case when is_admin = 'Yes' then 1 else 0 end into l_id from apex_workspace_apex_users where user_name = :APP_USER;
    exception when others then l_id := 0;
    end;
    l_out.put('is_admin', l_id = 1);
    select count(*) into l_id from (
      select sandbox_id, action, status,
             row_number() over (partition by sandbox_id order by id desc) rn
        from sandbox_requests where requester = :APP_USER)
     where rn = 1 and not (action = 'DESTROY' and status = 'DONE');
    l_out.put('live', l_id);

  elsif l_action = 'set_key' then
    -- Free Tier edition: switch the assistant on with a Google AI Studio key (no reinstall).
    declare
      l_key  varchar2(200) := substr(trim(l_in.get_string('key')), 1, 200);
      l_msgs json_array_t := json_array_t();
      l_adm  number := 0;
    begin
      begin
        select case when is_admin = 'Yes' then 1 else 0 end into l_adm from apex_workspace_apex_users where user_name = :APP_USER;
      exception when others then l_adm := 0;
      end;
      if l_adm = 0 then
        l_out.put('err', 'Only a factory administrator can set the assistant key.');
      elsif not regexp_like(l_key, '^AIza[A-Za-z0-9_-]{30,}$') then
        l_out.put('err', 'That does not look like a Google AI Studio key (they start with AIza).');
      else
        merge into factory_config c using (select 'google_api_key' key, l_key value from dual) s on (c.key = s.key)
          when matched then update set c.value = s.value when not matched then insert (key, value) values (s.key, s.value);
        merge into factory_config c using (select 'genai_provider' key, 'google' value from dual) s on (c.key = s.key)
          when matched then update set c.value = s.value when not matched then insert (key, value) values (s.key, s.value);
        commit;
        begin
          l_msgs.append(msg('USER', 'Reply with the single word OK.'));
          l_out.put('reply', substr(google_chat(l_msgs, false), 1, 40));
          l_out.put('ok', true);
        exception when others then
          update factory_config set value = 'oci' where key = 'genai_provider';
          delete from factory_config where key = 'google_api_key';
          commit;
          l_out.put('err', 'Google did not accept that key: ' || regexp_replace(substr(sqlerrm, 1, 300), 'key=[^ &]+', 'key=***'));
        end;
      end if;
    end;

  elsif l_action = 'plan' then
    declare
      l_msgs json_array_t := json_array_t();
    begin
      l_msgs.append(msg('USER', fill(prompt_text('plan'))));
      l_out.put('raw', ai_chat(l_msgs));
    end;

  elsif l_action = 'chat' then
    -- Conversational mode: the model answers AND may propose one action for the user to confirm.
    declare
      l_msgs json_array_t := json_array_t();
      l_hist json_array_t := l_in.get_array('messages');
      l_m    json_object_t;
    begin
      l_msgs.append(msg('SYSTEM', fill(prompt_text('chat'))));
      if l_hist is not null then
        for i in 0 .. l_hist.get_size - 1 loop
          l_m := treat(l_hist.get(i) as json_object_t);
          -- Attached code arrives in the CLOB parameter (x02 is capped at
          -- 32767 bytes) and is appended to the latest user message.
          l_msgs.append(msg(case when l_m.get_string('role') = 'assistant' then 'ASSISTANT' else 'USER' end,
                            l_m.get_clob('text') || case when i = l_hist.get_size - 1 and l_m.get_string('role') <> 'assistant'
                                                         then apex_application.g_clob_01 end));
        end loop;
      end if;
      declare
        -- Did the user ask for a build? Decided from their own words (and any
        -- attached code), never from the model's reply, so a deployment request
        -- always ends with an action the page can turn into a button.
        l_last varchar2(4000) := case when l_hist is not null and l_hist.get_size > 0
                                      then substr(treat(l_hist.get(l_hist.get_size - 1) as json_object_t).get_clob('text'), 1, 4000) end;
        l_ask_build boolean := apex_application.g_clob_01 is not null
                               or regexp_like(l_last, '(^|\W)(create|deploy|build|spin up|set up|launch|provision|destroy|delete|extend|recreate|retry)(\W|$)|github\.com|here is the code|for [0-9]+ days?', 'i');
        l_raw  clob := ai_chat(l_msgs);
        l_j    json_object_t;
        l_txt  varchar2(4000);
      begin
        for l_try in 1 .. 2 loop
          begin
            l_j   := json_object_t.parse(substr(l_raw, instr(l_raw, '{'), instr(l_raw, '}', -1) - instr(l_raw, '{') + 1));
            l_txt := substr(l_j.get_clob('reply'), 1, 4000);   -- get_string raises ORA-06502 past 32767 characters
          exception when others then l_j := null;
          end;
          if l_j is null then
            l_msgs.append(msg('ASSISTANT', l_raw));
            l_msgs.append(msg('USER', 'That answer was not one complete JSON object (it may have been cut off). Answer again with ONLY the JSON object described above, complete and shorter: no prose before or after it, no code fences, no code written into the action when the user attached code (workload lists the file paths), and any code you write under 60 lines.'));
            l_raw := ai_chat(l_msgs);
          elsif (not l_j.has('action') or l_j.get('action').is_null)
             and (l_ask_build
                  or ((l_j.get('questions') is null or l_j.get('questions').is_null or l_j.get_array('questions').get_size = 0)
                      and regexp_like(l_txt, '(go ahead|shall i|would you like me to|should i (create|deploy|build))', 'i'))) then
            l_msgs.append(msg('ASSISTANT', l_raw));
            l_msgs.append(msg('USER', 'The user asked for this to be built. Answer again with the same JSON and the action object filled in (use sensible defaults for anything not given), so the page can show the Create button.'));
            l_raw := ai_chat(l_msgs);
          else
            exit;
          end if;
        end loop;
        l_out.put('raw', l_raw);
      end;
    end;

  elsif l_action = 'submit' then
    do_submit;

  elsif l_action = 'history' then
    out_clob(my_history());
    return;

  elsif l_action = 'retry' then
    -- Queue the sandbox's last build again. The stack is idempotent, so this
    -- re-applies (no changes) and re-runs the database setup: sample data,
    -- Select AI, REST. Only the owner may do it, and only for a live sandbox.
    declare
      l_sid varchar2(64) := l_in.get_string('sandbox_id');
      l_src number;
      l_r   sandbox_requests%rowtype;
    begin
      select max(id) into l_src from sandbox_requests
       where sandbox_id = l_sid and requester = :APP_USER
         and action in ('CREATE', 'DEPLOY') and status = 'DONE';
      if l_src is null then
        l_out.put('err', 'No finished build of ' || l_sid || ' to retry.');
        out_clob(l_out.to_clob);
        return;
      end if;
      -- Read first, then insert: the ownership trigger queries this table,
      -- and an INSERT ... SELECT on it raises ORA-04091 (mutating table).
      select * into l_r from sandbox_requests where id = l_src;
      -- A new lifetime: the re-apply re-tags everything with now + ttl_days,
      -- and the reaper follows the tag.
      if l_in.get_number('ttl_days') is not null then
        l_r.ttl_days := least(30, greatest(1, round(l_in.get_number('ttl_days'))));
        l_r.request_text := 'lifetime set to ' || l_r.ttl_days || ' day(s) from now';
      end if;
      insert into sandbox_requests (requester, sandbox_id, action, ttl_days, enable_adb, adb_tier, enable_kafka, kafka_mode, enable_app, app_image, git_url, app_port, request_text, seed_sql, app_containers, app_files, seed_key, app_template, enable_nosql, adb_databases, functions, app_instances, buckets, queues, dataflow_jobs, enable_catalog, catalog_assets, enable_aidp, user_env, enable_rag)
      values (l_r.requester, l_r.sandbox_id, l_r.action, l_r.ttl_days, l_r.enable_adb, l_r.adb_tier, l_r.enable_kafka, l_r.kafka_mode, l_r.enable_app, l_r.app_image, l_r.git_url, l_r.app_port, l_r.request_text, l_r.seed_sql, l_r.app_containers, l_r.app_files, l_r.seed_key, l_r.app_template, l_r.enable_nosql, l_r.adb_databases, l_r.functions, l_r.app_instances, l_r.buckets, l_r.queues, l_r.dataflow_jobs, l_r.enable_catalog, l_r.catalog_assets, l_r.enable_aidp, l_r.user_env, l_r.enable_rag);
      select max(id) into l_id from sandbox_requests where sandbox_id = l_sid and requester = :APP_USER;
      l_out.put('id', l_id);
    exception when others then
      l_out.put('err', substr(sqlerrm, 1, 300));
    end;

  elsif l_action in ('dbrun','dbres') then
    out_clob(db_panel(l_action, apex_application.g_x02, :APP_USER));
    return;

  elsif l_action = 'status' then
    out_clob(my_sandboxes());
    return;

  else
    l_out.put('err', 'unknown action');
  end if;

  out_clob(l_out.to_clob);
exception
  when others then
    htp.p('{"err":"' || apex_escape.json(sqlerrm) || '"}');
end;
