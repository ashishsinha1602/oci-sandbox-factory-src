declare
  -- Ajax callback "SF" for the Sandbox Factory home page.
  --   x01 = action (plan | submit | status), x02 = JSON payload
  l_action varchar2(30) := apex_application.g_x01;
  l_in     json_object_t;
  l_out    json_object_t := json_object_t();
  l_id     number;
  l_text   clob;

  c_genai_url   constant varchar2(200) := 'https://inference.generativeai.us-phoenix-1.oci.oraclecloud.com/20231130/actions/chat';
  c_genai_model constant varchar2(100) := 'google.gemini-2.5-flash';

  function yn(p_key varchar2) return varchar2 is
  begin
    return case when l_in.get_boolean(p_key) then 'Y' else 'N' end;
  end;

  -- Ask OCI Generative AI as the database itself (resource principal, no keys).
  function ai_chat(p_prompt clob) return clob is
    l_resp dbms_cloud_types.resp;
    l_req  json_object_t := json_object_t();
    l_sm   json_object_t := json_object_t();
    l_cr   json_object_t := json_object_t();
    l_msg  json_object_t := json_object_t();
    l_part json_object_t := json_object_t();
    l_msgs json_array_t  := json_array_t();
    l_cont json_array_t  := json_array_t();
    l_body clob;
    l_j    json_object_t;
  begin
    select value into l_body from factory_config where key = 'compartment_ocid';
    l_req.put('compartmentId', l_body);
    l_sm.put('servingType', 'ON_DEMAND');
    l_sm.put('modelId', c_genai_model);
    l_req.put('servingMode', l_sm);
    l_part.put('type', 'TEXT');
    l_part.put('text', p_prompt);
    l_cont.append(l_part);
    l_msg.put('role', 'USER');
    l_msg.put('content', l_cont);
    l_msgs.append(l_msg);
    l_cr.put('apiFormat', 'GENERIC');
    l_cr.put('messages', l_msgs);
    l_cr.put('maxTokens', 1200);
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

begin
  l_in := json_object_t.parse(nvl(apex_application.g_x02, '{}'));

  if l_action = 'plan' then
    l_text := ai_chat(
      'You are the planner of the OCI Sandbox Factory. A user describes what they want to build in a sandbox on Oracle Cloud. '
      || 'Building blocks available: an Autonomous Database (ATP, Always Free), Kafka (OCI Streaming with a Kafka-compatible endpoint), '
      || 'and a containerised app (a public container image, or a Git repository with a Dockerfile at its root, built for ARM). '
      || 'Every container receives ADB_CONNECT_STRING, ADB_ADMIN_PASSWORD, ADB_DB_NAME and KAFKA_BOOTSTRAP_SERVERS as environment variables. '
      || 'Sandboxes live at most 3 days. '
      || 'Respond with ONLY one JSON object and no markdown fences, with exactly these keys: '
      || 'sandbox_id (2-20 chars, lowercase letters, digits, dashes, derived from the request), '
      || 'enable_adb (boolean), enable_kafka (boolean), enable_app (boolean), '
      || 'app_image (string or null), git_url (string or null, only if the user gave a repository URL), app_port (integer, 80 if unknown), '
      || 'summary (2-3 sentences: what will be created and how the pieces fit together), '
      || 'steps (array of 3-6 short strings: what the user should do next, e.g. how to connect, which env vars to read), '
      || 'tips (array of 1-3 short strings). '
      || 'User request: ' || l_in.get_string('text'));
    l_out.put('raw', l_text);

  elsif l_action = 'submit' then
    insert into sandbox_requests
      (requester, sandbox_id, action, ttl_days, enable_adb, enable_kafka, enable_app,
       app_image, git_url, app_port, request_text)
    values
      (:APP_USER, lower(l_in.get_string('sandbox_id')), nvl(l_in.get_string('action'), 'CREATE'),
       least(3, greatest(1, nvl(l_in.get_number('ttl_days'), 3))),
       yn('enable_adb'), yn('enable_kafka'), yn('enable_app'),
       l_in.get_string('app_image'), l_in.get_string('git_url'), nvl(l_in.get_number('app_port'), 80),
       l_in.get_string('text'))
    returning id into l_id;
    l_out.put('id', l_id);

  elsif l_action = 'status' then
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
      into l_text
      from (select * from sandbox_requests where requester = :APP_USER order by id desc fetch first 8 rows only);
    htp.p(nvl(l_text, '[]'));
    return;

  else
    l_out.put('error', 'unknown action');
  end if;

  htp.p(l_out.to_clob);
exception
  when others then
    htp.p('{"error":"' || apex_escape.json(sqlerrm) || '"}');
end;
