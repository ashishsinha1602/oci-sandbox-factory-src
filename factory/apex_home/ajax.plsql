declare
  -- Ajax callback "SF" for the Sandbox Factory home page.
  --   x01 = action (plan | chat | submit | status), x02 = JSON payload
  -- Replies never use an "error" key: APEX would treat that as a failed request.
  l_action varchar2(30) := apex_application.g_x01;
  l_in     json_object_t;
  l_out    json_object_t := json_object_t();
  l_id     number;

  c_genai_url   constant varchar2(200) := 'https://inference.generativeai.us-phoenix-1.oci.oraclecloud.com/20231130/actions/chat';
  c_genai_model constant varchar2(100) := 'google.gemini-2.5-flash';

  c_blocks constant varchar2(2000) :=
       'Building blocks available: an Autonomous Database (ATP, Always Free), Kafka (OCI Streaming with a Kafka-compatible endpoint), '
    || 'and a containerised app (a public container image, or a Git repository with a Dockerfile at its root, built for ARM). '
    || 'Every container receives ADB_CONNECT_STRING, ADB_ADMIN_PASSWORD, ADB_DB_NAME and KAFKA_BOOTSTRAP_SERVERS as environment variables. '
    || 'Sandboxes live at most 3 days (ttl_days 1-3) and each is an isolated OCI compartment with its own public URL. ';

  -- What the current user has (for status questions and destroy-by-name).
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
    l_cr.put('maxTokens', 1500);
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
  begin
    if l_act = 'DEPLOY' and l_git is null then
      l_act := 'CREATE';
    end if;
    insert into sandbox_requests
      (requester, sandbox_id, action, ttl_days, enable_adb, enable_kafka, enable_app, app_image, git_url, app_port, request_text)
    values
      (:APP_USER, l_sid, l_act, l_ttl, l_adb, l_kafka, l_app, l_image, l_git, l_port, l_req)
    returning id into l_id;
    l_out.put('id', l_id);
  end;

begin
  l_in := json_object_t.parse(nvl(apex_application.g_x02, '{}'));

  if l_action = 'plan' then
    declare
      l_msgs json_array_t := json_array_t();
    begin
      l_msgs.append(msg('USER',
           'You are the planner of the OCI Sandbox Factory. A user describes what they want to build in a sandbox on Oracle Cloud. '
        || c_blocks
        || 'Respond with ONLY one JSON object and no markdown fences, with exactly these keys: '
        || 'sandbox_id (2-20 chars, lowercase letters, digits, dashes, derived from the request), '
        || 'enable_adb (boolean), enable_kafka (boolean), enable_app (boolean), '
        || 'app_image (string or null), git_url (string or null, only if the user gave a repository URL), app_port (integer, 80 if unknown), '
        || 'summary (2-3 sentences: what will be created and how the pieces fit together), '
        || 'steps (array of 3-6 short strings: what the user should do next, e.g. how to connect, which env vars to read), '
        || 'tips (array of 1-3 short strings), '
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
        || c_blocks
        || 'You help the user decide what to build and you can act for them. '
        || 'The user''s current sandbox requests (newest first) are: ' || my_sandboxes() || '. '
        || 'Statuses: QUEUED (waiting for the worker), RUNNING (Terraform in progress, 3-5 minutes), DONE (outputs contain URLs and connect strings), FAILED. '
        || 'Always respond with ONLY one JSON object, no markdown fences, with keys: '
        || 'reply (string, friendly, concise, may contain short line breaks; explain what and how, mention URLs from outputs when relevant), '
        || 'action (null, or an object when the user clearly wants something done: {type: "create"|"deploy"|"destroy", sandbox_id, ttl_days (1-3, default 3), '
        || 'enable_adb, enable_kafka, enable_app (booleans), app_image (string or null), git_url (string or null: a Git repository URL, or a local folder path the user gave such as C:\Users\me\myapp, kept exactly as given), app_port (integer)}). '
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
