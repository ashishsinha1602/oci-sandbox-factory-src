declare
  -- Ajax callback "SF" for the Sandbox Factory home page.
  --   x01 = action (plan | chat | submit | status), x02 = JSON payload
  -- Replies never use an "error" key: APEX would treat that as a failed request.
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
    || 'Sandboxes live at most 3 days (ttl_days 1-3), are tagged with their owner and expiry, and each gets its own public URL. ';

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
              and not (action = 'DESTROY' and status = 'DONE' and finished_at < systimestamp - interval '1' hour)
            order by id desc fetch first 8 rows only);
    return nvl(l_json, '[]');
  end;

  -- Ask OCI Generative AI as the database itself (resource principal, no keys).
  function ai_chat(p_messages json_array_t) return clob is
    l_resp dbms_cloud_types.resp;
    l_req  json_object_t := json_object_t();
    l_sm   json_object_t := json_object_t();
    l_cr   json_object_t := json_object_t();
    l_body clob;
    l_j    json_object_t;
    l_comp varchar2(200);
  begin
    select value into l_comp from factory_config where key = 'compartment_ocid';
    l_req.put('compartmentId', l_comp);
    l_sm.put('servingType', 'ON_DEMAND');
    l_sm.put('modelId', c_genai_model);
    l_req.put('servingMode', l_sm);
    l_cr.put('apiFormat', 'GENERIC');
    l_cr.put('messages', p_messages);
    l_cr.put('maxTokens', 6000);
    l_cr.put('temperature', 0.2);
    l_req.put('chatRequest', l_cr);

    l_resp := dbms_cloud.send_request(
      credential_name => 'OCI$RESOURCE_PRINCIPAL',
      uri             => c_genai_url,
      method          => dbms_cloud.method_post,
      headers         => json_object('Content-Type' value 'application/json'),
      body            => utl_raw.cast_to_raw(l_req.to_clob));
    l_body := dbms_cloud.get_response_text(l_resp);
    if dbms_cloud.get_response_status_code(l_resp) <> 200 then
      raise_application_error(-20001, 'Generative AI returned ' || dbms_cloud.get_response_status_code(l_resp) || ': ' || substr(l_body, 1, 300));
    end if;
    l_j := json_object_t.parse(l_body);
    return treat(treat(l_j.get_object('chatResponse').get_array('choices').get(0) as json_object_t)
                   .get_object('message').get_array('content').get(0) as json_object_t).get_string('text');
  end;

  function templates return varchar2 is
    l_t varchar2(4000);
  begin
    select value into l_t from factory_config where key = 'templates';
    return l_t;
  exception when no_data_found then return '';
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
    l_sid    varchar2(20)   := lower(l_in.get_string('sandbox_id'));
    l_act    varchar2(10)   := nvl(l_in.get_string('action'), 'CREATE');
    l_ttl    number         := least(3, greatest(1, nvl(l_in.get_number('ttl_days'), 3)));
    l_adb    varchar2(1)    := case when l_in.get_boolean('enable_adb') then 'Y' else 'N' end;
    l_kafka  varchar2(1)    := case when l_in.get_boolean('enable_kafka') then 'Y' else 'N' end;
    l_app    varchar2(1)    := case when l_in.get_boolean('enable_app') then 'Y' else 'N' end;
    l_image  varchar2(500)  := l_in.get_string('app_image');
    l_git    varchar2(500)  := l_in.get_string('git_url');
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
    l_casset clob           := case when l_in.has('catalog_assets') and l_in.get('catalog_assets').is_array
                                    then l_in.get_array('catalog_assets').to_clob else null end;
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
                      || '. Destroy one before creating another.');
        return;
      end if;
    end if;

    insert into sandbox_requests
      (requester, sandbox_id, action, ttl_days, enable_adb, enable_kafka, enable_nosql, enable_app, app_image, git_url, app_port, request_text, seed_sql, app_containers, app_files, seed_key, app_template, adb_databases, functions, app_instances, buckets, queues, dataflow_jobs, enable_catalog, catalog_assets)
    values
      (:APP_USER, l_sid, l_act, l_ttl, l_adb, l_kafka, l_nosql, l_app, l_image, l_git, l_port, l_req, l_seed, l_cont, l_files, l_skey, l_atpl, l_dbs, l_fns, l_insts, l_bkts, l_qs, l_dfj, l_cat, l_casset)
    returning id into l_id;
    l_out.put('id', l_id);
  end;

begin
  l_in := json_object_t.parse(nvl(apex_application.g_x02, '{}'));
  if l_in.get_string('model') in ('google.gemini-2.5-flash', 'google.gemini-2.5-pro', 'xai.grok-4', 'xai.grok-3', 'cohere.command-a-03-2025', 'openai.gpt-oss-120b') then
    c_genai_model := l_in.get_string('model');
  end if;

  l_prices   := cfg_value('oci_prices', '');
  l_region   := cfg_value('genai_region', 'us-phoenix-1');
  l_registry := cfg_value('registry_prefix', '');
  c_genai_url := 'https://inference.generativeai.' || l_region || '.oci.oraclecloud.com/20231130/actions/chat';

  if l_action = 'config' then
    l_out.put('registry_prefix', l_registry);
    l_out.put('region', l_region);
    l_out.put('user', :APP_USER);
    l_out.put('cap', to_number(cfg_value('max_sandboxes_per_user', '3')));
    select count(*) into l_id from (
      select sandbox_id, action, status,
             row_number() over (partition by sandbox_id order by id desc) rn
        from sandbox_requests where requester = :APP_USER)
     where rn = 1 and not (action = 'DESTROY' and status = 'DONE');
    l_out.put('live', l_id);

  elsif l_action = 'plan' then
    declare
      l_msgs json_array_t := json_array_t();
    begin
      l_msgs.append(msg('USER',
           'You are the planner of the OCI Sandbox Factory. A user describes what they want to build in a sandbox on Oracle Cloud. '
        || c_blocks || templates() || ' '
        || 'Respond with ONLY one JSON object and no markdown fences, with exactly these keys: '
        || 'sandbox_id (2-20 chars, lowercase letters, digits, dashes, derived from the request), '
        || 'enable_adb (boolean), enable_kafka (boolean), enable_nosql (boolean: OCI NoSQL serverless JSON tables), enable_app (boolean), '
        || 'app_image (string or null), git_url (string or null, only if the user gave a repository URL), app_port (integer, 80 if unknown), '
        || 'summary (2-3 sentences: what will be created and how the pieces fit together), '
        || 'steps (array of 3-6 short strings: what the user should do next, e.g. how to connect, which env vars to read), '
        || 'tips (array of 1-3 short strings), '
        || 'containers (array, optional: several containers in one sandbox, each {name, image, port}; the first is served at / on the Oracle hostname, the others at /<name>; use it for bundles such as MCP + Studio, where <registry> is ' || nvl(l_registry,'(none configured)') || ': [{"name":"studio","image":"<registry>studio/app:latest","port":8770},{"name":"mcp","image":"<registry>schemagate/app:latest","port":8765}]), '
        || 'seed_sql (string or null: when the user wants sample data or a named domain schema such as telemetry, orders, IoT, HR, write Oracle SQL that creates 2-4 small tables with primary keys and inserts 5-10 realistic rows each, statements separated by semicolons, no PL/SQL blocks, no comments; it runs once in the new database before the containers start), '
        || 'Every database this factory creates gets Oracle Select AI (NL2SQL) and AI cataloguing switched on automatically over all its schemas, so an agent that answers questions in plain English needs only enable_adb plus seed_sql - no container. Add containers only when the user wants a UI, an MCP endpoint, or an app of their own. '
        || 'questions (array of 0-3 short questions the user should answer before creating when something essential is missing or ambiguous, e.g. which producer or consumer app, which image or repository, which port; empty array when nothing is missing). '
        || 'User request: ' || l_in.get_string('text')));
      l_out.put('raw', ai_chat(l_msgs));
    end;

  elsif l_action = 'chat' then
    -- Conversational mode: the model answers AND may propose one action for the user to confirm.
    declare
      l_msgs json_array_t := json_array_t();
      l_hist json_array_t := l_in.get_array('messages');
      l_m    json_object_t;
    begin
      l_msgs.append(msg('SYSTEM',
           'You are the assistant of the OCI Sandbox Factory, a self-service tool that creates temporary sandboxes on Oracle Cloud. '
        || c_blocks || templates() || ' '
        || 'You help the user decide what to build and you can act for them. '
        || 'The user''s current sandbox requests (newest first) are: ' || my_sandboxes() || '. '
        || 'Statuses: QUEUED (waiting for the worker), RUNNING (Terraform in progress, 3-5 minutes), DONE (outputs contain URLs and connect strings), FAILED. '
        || 'Always respond with ONLY one JSON object, no markdown fences, with keys: '
        || 'reply (string, friendly, concise, may contain short line breaks; explain what and how, mention URLs from outputs when relevant), '
        || 'action (null, or an object when the user clearly wants something done: {type: "create"|"deploy"|"destroy", sandbox_id, ttl_days (1-3, default 3), '
        || 'enable_catalog (boolean: OCI Data Catalog, the metastore a Spark job resolves table names against - the counterpart to the AWS Glue Data Catalog), buckets (optional array of Object Storage buckets, each {name, public}; cheap and outside the container quota), queues (optional array of OCI Queues, each {name}; serverless point-to-point messaging, the counterpart to Kafka streams), dataflow_jobs (optional array of Spark applications on OCI Data Flow, the equivalent of an AWS Glue ETL job, each {name, file_uri}; managed Spark billed per run), databases (optional array of EXTRA Autonomous Databases, each {name, tier}; every one is a real database with its own ADMIN credential and uses a tenancy slot), functions (optional array of OCI Functions, each {name, image}; serverless, billed per invocation, free when idle, and they do not consume the container core quota), app_instances (optional array of ADDITIONAL container instances, each {name, containers:[{name,image,port}]}; containers within ONE instance share a host and localhost, separate instances do not), enable_adb, enable_kafka, enable_nosql (OCI NoSQL: serverless JSON/key-value tables, good for events, sessions, device state, anything schemaless), enable_app (booleans), app_image (string or null), git_url (string or null: a Git repository URL, or a local folder path the user gave such as C:\Users\me\myapp, kept exactly as given), app_port (integer), '
        || 'containers (optional array of {name, image, port} for bundles, first served at /, others at /<name>), '
        || 'seed_sql (optional string: Oracle SQL creating 2-4 small tables with 5-10 realistic rows each for the domain the user named, semicolon-separated, no PL/SQL, no comments; runs once in the new database)}). '
        || 'Every database this factory creates gets Oracle Select AI (NL2SQL) and AI cataloguing switched on automatically over all its schemas, so an agent that answers questions in plain English needs only enable_adb plus seed_sql - no container. Add containers only when the user wants a UI, an MCP endpoint, or an app of their own. '
        || 'MOVING A WORKLOAD FROM AWS. When the user describes an existing stack - Lambda, Glue, S3, Iceberg, Athena, SQS, Kinesis, DynamoDB, RDS, EKS - do not just name the OCI service. Answer in this order: '
        || '(1) what you would build, as two options: RUN THE SAME CODE (the closest service, what changes - usually an endpoint and credentials) and CHANGE THE CODE (the service that fits OCI better, and why it is worth the edit). '
        || '(2) what it costs per month at their volume, as a short list of line items with a total. Say which pieces are billed per use and cost nothing idle, and if a cluster is shared across N workloads divide it by N and say so. '
        || '(3) ask for the code: a Git URL or a folder path. Say you will read it and point out anything missing before deploying. '
        || '(4) only once you have the repo, propose the action that builds it. '
        || 'The mapping: Lambda -> OCI Functions (same handler in a container) or a small container instance when it runs for minutes or holds a database connection. S3 -> Object Storage, S3-compatible so usually only the endpoint changes. Glue ETL -> Data Flow, managed Spark, PySpark runs unchanged. Glue Data Catalog -> OCI Data Catalog. Iceberg -> the same iceberg jar on Data Flow writing to Object Storage, or an Autonomous Database instead if the data is under a few terabytes, because it already has ACID tables and time travel. Athena -> Data Flow SQL or Autonomous Database external tables. SQS -> OCI Queue. Kinesis/MSK -> OCI Streaming. DynamoDB -> OCI NoSQL. RDS -> Autonomous Database. EKS -> OKE, or container instances when it is a few containers rather than a platform. '
        || 'Live OCI unit prices, use these and show your arithmetic: ' || l_prices || '. '
        || 'Be honest about what you cannot price: there is no published rate for Data Flow, Data Catalog or Container Instances, so estimate those from the compute shape and say that is what you did. Never present a free-tier allowance as the real price. '
        || 'You may also return questions (array of 0-3 short questions). Ask when a detail you need is genuinely missing and the answer would change what gets built: which domain the sample data should cover, how many days they need it for, whether they want a UI on top or just the database, or which repo or image to deploy. When you ask questions, leave action out entirely and wait for the answer - do not guess and build. Ask at most two at a time, and do not ask about anything they already told you or anything with an obvious default. '
        || 'In your reply, say in one or two plain sentences what will actually be created in OCI - the database and its tier, the Kafka cluster, the containers - so the user knows what is being spun up before they confirm. '
        || 'When the user asks for an MCP server plus a way to chat with or explore the data, propose containers [studio on 8770, mcp on 8765] with a database and seed_sql for their domain. '
        || 'Use type "deploy" when the user gives a Git repository URL or a local folder path (a folder with a Dockerfile; the worker builds it); "create" with app_image for a public image; "destroy" to delete an existing sandbox by its sandbox_id. '
        || 'Never invent a sandbox_id that does not exist for destroy. Ask a short question instead of guessing when something essential is missing. '
        || 'When you propose an action, describe it in reply and end with a question like "Shall I go ahead?".'));
      if l_hist is not null then
        for i in 0 .. l_hist.get_size - 1 loop
          l_m := treat(l_hist.get(i) as json_object_t);
          l_msgs.append(msg(case when l_m.get_string('role') = 'assistant' then 'ASSISTANT' else 'USER' end, l_m.get_string('text')));
        end loop;
      end if;
      l_out.put('raw', ai_chat(l_msgs));
    end;

  elsif l_action = 'submit' then
    do_submit;

  elsif l_action = 'status' then
    htp.p(my_sandboxes());
    return;

  else
    l_out.put('err', 'unknown action');
  end if;

  htp.p(l_out.to_clob);
exception
  when others then
    htp.p('{"err":"' || apex_escape.json(sqlerrm) || '"}');
end;
