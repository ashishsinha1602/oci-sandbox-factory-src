
function sfInit(){
  var mode=null, plan=null, cfg={enable_adb:false,enable_kafka:false,enable_nosql:false,enable_app:false}, timer=null;
  var $=function(s){return document.querySelector(s)}, $$=function(s){return Array.prototype.slice.call(document.querySelectorAll(s))};
  function call(action,payload){payload=payload||{}; var m=document.querySelector('#sf-model'); if(m&&!payload.model)payload.model=m.value; return apex.server.process('SF',{x01:action,x02:JSON.stringify(payload)},{dataType:'json'})}
  function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]})}

  $$('.sf-card').forEach(function(c){c.onclick=function(){
    $$('.sf-card').forEach(function(x){x.classList.remove('sel')}); c.classList.add('sel');
    mode=c.dataset.mode; $('#sf-panel').classList.remove('sf-hide');
    $('#sf-chat').classList.toggle('sf-hide',mode!=='chat');
    $('#sf-describe').classList.toggle('sf-hide',mode!=='describe');
    $('#sf-form').classList.toggle('sf-hide',mode==='describe'||mode==='chat');
    if(mode==='chat'){$('#sf-chat-in').focus();return}
    $('#sf-toggles').classList.remove('sf-hide');
    if(mode==='build'){setCfg({enable_adb:true,enable_kafka:false,enable_app:true})}
    $('#sf-id').focus();
  }});

  function setCfg(c){for(var k in c){cfg[k]=c[k]} $$('.sf-toggle').forEach(function(t){t.classList.toggle('on',!!cfg[t.dataset.k])}); $('#sf-appfields').classList.toggle('sf-hide',!cfg.enable_app)}
  $$('.sf-toggle').forEach(function(t){t.onclick=function(){var k=t.dataset.k; var c={}; c[k]=!cfg[k]; setCfg(c)}});

  $('#sf-plan').onclick=function(){
    var text=$('#sf-text').value.trim(); if(!text){$('#sf-text').focus();return}
    $('#sf-plan').disabled=true; $('#sf-plan-wait').classList.remove('sf-hide'); $('#sf-ai').classList.add('sf-hide');
    call('plan',{text:text}).then(function(r){
      $('#sf-plan').disabled=false; $('#sf-plan-wait').classList.add('sf-hide');
      if(r.err){showAI('<p class="sf-err">'+esc(r.err)+'</p>');return}
      var m=(r.raw||'').match(/\{[\s\S]*\}/); try{plan=m?JSON.parse(m[0]):null}catch(e){plan=null}
      if(!plan){showAI('<h4>AI answer</h4><p>'+esc(r.raw)+'</p>');return}
      var h='<h4>&#10024; Plan</h4><p>'+esc(plan.summary)+'</p><div>';
      if(plan.enable_adb)h+='<span class="sf-chip">Autonomous Database</span>';
      if(plan.enable_kafka)h+='<span class="sf-chip">Kafka</span>';
      if(plan.containers&&plan.containers.length){plan.containers.forEach(function(c){h+='<span class="sf-chip">'+esc(c.name)+': '+esc(c.image)+' :'+esc(c.port)+'</span>'})}
      else if(plan.enable_app)h+='<span class="sf-chip">App: '+esc(plan.git_url||plan.app_image||'(image or repo needed)')+' on port '+esc(plan.app_port||80)+'</span>';
      if(plan.seed_sql)h+='<span class="sf-chip">sample data: '+esc(String(plan.seed_sql).split(';').filter(function(x){return x.trim()}).length)+' SQL statements</span>';
      h+='<span class="sf-chip">3 days</span></div>';
      if(plan.steps&&plan.steps.length){h+='<h4 style="margin-top:10px">What to do next</h4><ol>'+plan.steps.map(function(s){return '<li>'+esc(s)+'</li>'}).join('')+'</ol>'}
      h+=infraHtml(plan);
      if(plan.tips&&plan.tips.length){h+='<ul>'+plan.tips.map(function(s){return '<li>'+esc(s)+'</li>'}).join('')+'</ul>'}
      if(plan.questions&&plan.questions.length){h+='<h4 style="margin-top:10px">Before I create it, tell me</h4><ol>'+plan.questions.map(function(s){return '<li>'+esc(s)+'</li>'}).join('')+'</ol><p class="sf-err">Add the answers to your description and ask again, or adjust the form.</p>'}
      h+='<div class="sf-actions" style="margin-top:12px"><button type="button" class="sf-btn" id="sf-usePlan">Looks good, create it</button><button type="button" class="sf-btn sec" id="sf-editPlan">Adjust first</button></div>';
      showAI(h);
      $('#sf-usePlan').onclick=function(){applyPlan(); submit()};
      $('#sf-editPlan').onclick=function(){applyPlan(); $('#sf-form').classList.remove('sf-hide'); $('#sf-toggles').classList.remove('sf-hide'); $('#sf-id').focus()};
    }).catch(function(e){$('#sf-plan').disabled=false;$('#sf-plan-wait').classList.add('sf-hide');showAI('<p class="sf-err">'+esc(e.message||e)+'</p>')});
  };
  function showAI(h){$('#sf-ai').innerHTML=h;$('#sf-ai').classList.remove('sf-hide')}
  function applyPlan(){ if(!plan)return; $('#sf-id').value=(plan.sandbox_id||'').toLowerCase().replace(/[^a-z0-9-]/g,'-').replace(/^-+/,'').slice(0,20)||'sandbox';
    setCfg({enable_adb:!!plan.enable_adb,enable_kafka:!!plan.enable_kafka,enable_nosql:!!plan.enable_nosql,enable_app:!!plan.enable_app});
    if(plan.git_url)$('#sf-git').value=plan.git_url; if(plan.app_image)$('#sf-image').value=plan.app_image; if(plan.app_port)$('#sf-port').value=plan.app_port; $('#sf-ttl').value='3'}

  $('#sf-submit').onclick=submit;
  function submit(){
    var id=$('#sf-id').value.trim().toLowerCase(); $('#sf-err').classList.add('sf-hide');
    if(!/^[a-z][a-z0-9-]{1,19}$/.test(id)){err('Sandbox id: 2-20 chars, lowercase letters, digits, dashes, starting with a letter.');return}
    if(!cfg.enable_adb&&!cfg.enable_kafka&&!cfg.enable_nosql&&!cfg.enable_app){err('Pick at least one piece.');return}
    var git=$('#sf-git').value.trim(), payload={sandbox_id:id, ttl_days:+$('#sf-ttl').value, enable_adb:cfg.enable_adb, enable_kafka:cfg.enable_kafka, enable_nosql:cfg.enable_nosql, enable_app:cfg.enable_app,
      action:(cfg.enable_app&&git)?'DEPLOY':'CREATE', git_url:git||null, app_image:$('#sf-image').value.trim()||null, app_port:+$('#sf-port').value||80, text:mode==='describe'?$('#sf-text').value.trim():null};
    if(mode==='describe'&&plan){ if(plan.containers&&plan.containers.length)payload.containers=plan.containers; if(plan.seed_sql)payload.seed_sql=plan.seed_sql; }
    $('#sf-submit').disabled=true; $('#sf-submit-wait').classList.remove('sf-hide');
    call('submit',payload).then(function(r){
      $('#sf-submit').disabled=false; $('#sf-submit-wait').classList.add('sf-hide');
      if(r.err){err(r.err);return}
      refresh(); window.scrollTo({top:$('#sf-status').offsetTop-20,behavior:'smooth'});
    }).catch(function(e){$('#sf-submit').disabled=false;$('#sf-submit-wait').classList.add('sf-hide');err(e.message||e)});
  }
  function err(m){$('#sf-err').textContent=m;$('#sf-err').classList.remove('sf-hide')}

  function refresh(){
    call('status',{}).then(function(rows){
      rows=rows||[]; var active=false, h=rows.length?'<h3>Your sandboxes</h3>':'';
      window.__sfRows = rows;
      rows.forEach(function(r){
        if(r.status==='QUEUED'||r.status==='RUNNING')active=true;
        var o=null; try{o=r.outputs?JSON.parse(r.outputs):null}catch(e){}
        h+='<div class="sf-req"><div class="hd">'+(r.status==='RUNNING'?'<span class="sf-spin"></span>':'')+'<b>'+esc(r.sandbox_id)+'</b><span class="sf-badge '+esc(r.status)+'">'+esc(r.status)+'</span><span>'+esc(r.action)+'</span><span class="meta">'+esc(r.created)+' &middot; '+esc(r.elapsed)+'s</span></div>';
        if(r.status==='DONE'&&r.action!=='DESTROY'&&r.outputs){
          h+='<button type="button" class="sf-btn sec" style="padding:4px 12px;font-size:12px;margin-top:6px" '
            +'data-open="'+esc(r.sandbox_id)+'">Open everything</button>';
        }
        if(o&&r.status==='DONE'){ h+='<div class="sf-links">';
          var pu=primaryUrl(o);
          if(pu){h+='<a class="sf-open" href="'+esc(pu)+'" target="_blank" rel="noopener">Open '+esc(r.sandbox_id)+' &rarr;</a><br>'}
          if(o.app&&o.app.urls){var us=o.app.urls.filter(function(u){return u.indexOf('https://')===0}); if(!us.length)us=o.app.urls; us.forEach(function(u){h+='<a href="'+esc(u)+'" target="_blank">'+esc(u)+'</a>'});
            if(o.app.containers&&o.app.containers.length>1&&us.length){o.app.containers.slice(1).forEach(function(n){h+='<a href="'+esc(us[0])+'/'+esc(n)+'" target="_blank">'+esc(us[0])+'/'+esc(n)+'</a>'})}}
          if(o.adb){h+='<div style="margin-top:6px"><a href="'+esc(o.adb.sql_web_url)+'" target="_blank">SQL Developer Web</a> &middot; user <code>'+esc(o.adb.admin_user||'ADMIN')+'</code>'
              +(o.adb.admin_password?' &middot; password <code>'+esc(o.adb.admin_password)+'</code> <button type="button" class="sf-btn sec" style="padding:2px 8px;font-size:12px" onclick="navigator.clipboard.writeText(this.previousElementSibling.textContent)">copy</button>':'')
              +'<br>connect string <code>'+esc(o.adb.connect_string)+'</code></div>'}
          if(o.nosql&&o.nosql.tables&&o.nosql.tables.length)h+='<code>nosql: '+esc(o.nosql.tables.join(', '))+'</code>';
          if(o.low_code&&o.low_code.rest_base)h+='<code>REST: '+esc(o.low_code.rest_base)+'</code>';
          if(o.kafka)h+='<code>kafka: '+esc(o.kafka.bootstrap_servers)+'</code>';
          if(o.buckets&&o.buckets.length)h+='<code>buckets: '+esc(o.buckets.map(function(b){return b.name}).join(', '))+'</code>';
          if(o.queues&&o.queues.length)h+='<code>queues: '+esc(o.queues.map(function(q){return q.name}).join(', '))+'</code>';
          if(o.functions&&o.functions.length)h+='<code>functions: '+esc(o.functions.map(function(f){return f.name}).join(', '))+'</code>';
          if(o.dataflow_jobs&&o.dataflow_jobs.length)h+='<code>spark: '+esc(o.dataflow_jobs.map(function(j){return j.name}).join(', '))+'</code>';
          if(o.catalog&&o.catalog.display_name)h+='<code>catalog: '+esc(o.catalog.display_name)+'</code>';
          if(o.databases&&o.databases.length>1){o.databases.slice(1).forEach(function(d){
            h+='<code>'+esc(d.name)+': '+esc(d.db_name)+'</code>'})}
          if(o.warnings&&o.warnings.length)h+='<div class="sf-err" style="margin-top:6px">'
              +o.warnings.map(esc).join('<br>')+'</div>';
          if(o.consoles){
            var open=[];
            if(o.dataflow_jobs&&o.dataflow_jobs.length)open.push(['Data Flow',o.consoles.data_flow]);
            if(o.catalog)open.push(['Data Catalog',o.consoles.data_catalog]);
            if(o.buckets&&o.buckets.length)open.push(['Buckets',o.consoles.object_storage]);
            if(o.functions&&o.functions.length)open.push(['Functions',o.consoles.functions]);
            if(o.queues&&o.queues.length)open.push(['Queues',o.consoles.queues]);
            if(o.nosql)open.push(['NoSQL',o.consoles.nosql]);
            if(open.length)h+='<div style="margin-top:6px">open in OCI: '+open.map(function(x){
              return '<a href="'+esc(x[1])+'" target="_blank">'+esc(x[0])+'</a>'}).join(' &middot; ')+'</div>';
          }
          if(o.destroyed)h+='destroyed';
          h+='</div>'}
        if(r.error)h+='<div class="sf-err">'+esc(r.error)+'</div>';
        if(r.logtail&&r.status!=='DONE')h+='<div class="sf-log">'+esc(r.logtail)+'</div>';
        h+='</div>';
      });
      $('#sf-status').innerHTML=h;
      clearTimeout(timer); if(active)timer=setTimeout(refresh,5000);
    });
  }

  // ---- chat mode: conversation with the AI; it may propose one action to confirm
  // Conversation state. Kept in localStorage so a reload, a second tab or an
  // APEX session timeout no longer throws the conversation away - the page used
  // to come back empty every time, which is most of why this did not read as a chat.
  var HIST_KEY='sbx.chat.'+((window.apex&&apex.env&&apex.env.APP_USER)||'anon'), HIST_MAX=200;
  function loadHist(){try{return JSON.parse(localStorage.getItem(HIST_KEY))||[]}catch(e){return[]}}
  function saveHist(){try{localStorage.setItem(HIST_KEY,JSON.stringify(hist.slice(-HIST_MAX)))}catch(e){}}
  var hist=loadHist();
  function pushHist(m){Array.prototype.push.call(hist,m);saveHist();return m}
  // esc() escapes markup but drops newlines, so multi-line answers used to
  // arrive as one run-on line. Keep the escaping, restore the shape.
  function fmt(s){return '<p>'+esc(s)
      .replace(/`([^`\n]+)`/g,'<code>$1</code>')
      .replace(/\n{2,}/g,'</p><p>')
      .replace(/\n/g,'<br>')+'</p>'}
  // Every sandbox hands back one thing to click. Newer stacks output `url`
  // directly; older rows are derived so existing sandboxes still get a button.
  function primaryUrl(o){
    if(!o)return null;
    if(o.url)return o.url;
    if(o.app&&o.app.url)return o.app.url;
    if(o.app&&o.app.urls&&o.app.urls.length){
      var https=o.app.urls.filter(function(u){return u.indexOf('https://')===0});
      return https[0]||o.app.urls[0];
    }
    if(o.adb)return o.adb.apex_url||o.adb.sql_web_url;
    return null;
  }
  function addMsg(cls,html){var d=document.createElement('div');d.className='sf-msg '+cls;d.innerHTML=html;$('#sf-msgs').appendChild(d);$('#sf-msgs').scrollTop=1e9;return d}
  document.addEventListener('click',function(e){
    if(!e.target.closest)return;
    var rec=e.target.closest('#sf-recs .sf-rec');
    if(rec){var keys=($('#sf-recs').getAttribute('data-keys')||'').split(',');
      offerRecipe(RECIPES[+keys[+rec.getAttribute('data-i')]]);return}
    var b=e.target.closest('#sf-sugg b'); if(!b)return;
    if(b.getAttribute('data-form')){openForm(b.getAttribute('data-form'));return}
    $('#sf-chat-in').value=b.textContent.trim(); sendChat();
  });

  // ---- One-click recipes -------------------------------------------------
  // Each is a complete submit payload: click it, glance at what it builds, Build.
  // Filled from SBX.FACTORY_CONFIG.registry_prefix so this page carries no
  // tenancy of its own. Recipes write {R} and it is substituted at build time.
  var OCIR='';
  // Sample schemas and starter apps live in factory/app_templates.py; the page
  // only names them (seed_key / app_template) because an APEX region caps at 32767 bytes.

  // ---- Seeded starter apps -----------------------------------------------
  // Shipped with the request as app_files {name: contents}. The worker writes
  // them to a folder and builds them, so there is no repo and nothing to push.







  var RECIPES=[
    {k:'reactapp', t:'React explorer + Python API', d:'A React UI and a FastAPI backend on seeded data, with a plain-English question box. Deploy and look at it.',
     p:{enable_adb:true,seed_key:'sales',app_template:'react',app_port:8080}, tags:['React','FastAPI','Select AI']},
    {k:'pyapi', t:'Python REST API (FastAPI)', d:'A FastAPI service over the sandbox database, with /docs and a Select AI endpoint.',
     p:{enable_adb:true,seed_key:'sales',app_template:'api',app_port:8080}, tags:['Python','FastAPI','OpenAPI']},
    {k:'streamlit', t:'Python dashboard (Streamlit)', d:'Browse every table and chart it, straight from the database. No front-end code to write.',
     p:{enable_adb:true,seed_key:'iot',app_template:'streamlit',app_port:8080}, tags:['Python','Streamlit','charts']},
    {k:'apex', t:'Low-code app (APEX)', d:'Sample tables plus an APEX workspace. Open it, click Create App, and you have a working CRUD app.',
     p:{enable_adb:true,seed_key:'sales'}, tags:['APEX','no code','Select AI']},
    {k:'ords', t:'Instant REST API (ORDS)', d:'Every seeded table gets a REST endpoint automatically. No container, nothing to write.',
     p:{enable_adb:true,seed_key:'sales'}, tags:['ORDS','REST','no code']},
    {k:'sales', t:'Sales database + Select AI', d:'Customers, products and orders, then ask questions in plain English. No container needed.',
     p:{enable_adb:true,seed_key:'sales'}, tags:['Autonomous DB','Select AI','seeded']},
    {k:'hr', t:'HR database + Select AI', d:'Departments, employees and salaries. "Who earns the most in Engineering?"',
     p:{enable_adb:true,seed_key:'hr'}, tags:['Autonomous DB','Select AI','seeded']},
    {k:'iot', t:'IoT telemetry + Select AI', d:'Sensors and readings with warn and alarm states, ready to interrogate.',
     p:{enable_adb:true,seed_key:'iot'}, tags:['Autonomous DB','Select AI','seeded']},
    {k:'iotk', t:'IoT + Kafka end to end', d:'The telemetry schema plus a Kafka cluster to stream new readings into.',
     p:{enable_adb:true,enable_kafka:true,seed_key:'iot'}, tags:['Autonomous DB','Kafka','Select AI']},
    {k:'rag', t:'Knowledge base (upload, crawl, ask)', d:'Upload PDFs or crawl a URL. Oracle parses, chunks and embeds them, then you ask the documents or the database itself.',
     p:{enable_adb:true,seed_key:'rag',app_template:'rag',app_port:8080}, tags:['23ai','AI Vector Search','RAG']},
    {k:'mcp', t:'MCP server + Studio', d:'An MCP endpoint over seeded sales data, with a chat UI served at /studio.',
     p:{enable_adb:true,seed_key:'sales',containers:[{name:'studio',image:'{R}studio/app:latest',port:8770},{name:'mcp',image:'{R}schemagate/app:latest',port:8765}]}, tags:['MCP','Studio','Select AI']},
    {k:'mcponly', t:'MCP endpoint only', d:'Just the MCP server over a seeded database, to point Claude or your own agent at.',
     p:{enable_adb:true,seed_key:'sales',containers:[{name:'mcp',image:'{R}schemagate/app:latest',port:8765}]}, tags:['MCP','Autonomous DB']},
    {k:'api', t:'Orders REST API', d:'A small REST service on top of the orders schema, on a public HTTPS URL.',
     p:{enable_adb:true,seed_key:'sales',containers:[{name:'api',image:'{R}orders-demo/app:latest',port:8080}]}, tags:['REST API','Autonomous DB']},
    {k:'empty', t:'Empty 23ai database', d:'A clean Autonomous Database with Select AI and AI cataloguing already switched on.',
     p:{enable_adb:true}, tags:['Autonomous DB','Select AI']},
    {k:'nosql', t:'NoSQL event store', d:'Serverless OCI NoSQL JSON tables. Nothing to size, nothing running when idle.',
     p:{enable_nosql:true}, tags:['NoSQL','serverless']},
    {k:'nosqlk', t:'NoSQL + Kafka pipeline', d:'Stream events through Kafka and land them in NoSQL JSON tables.',
     p:{enable_nosql:true,enable_kafka:true}, tags:['NoSQL','Kafka','events']},
    {k:'polyglot', t:'Polyglot: SQL + NoSQL + Kafka', d:'One sandbox with all three, to compare them or build something that spans them.',
     p:{enable_adb:true,enable_nosql:true,enable_kafka:true,seed_key:'sales'}, tags:['Autonomous DB','NoSQL','Kafka']},
    {k:'kafka', t:'Kafka cluster only', d:'A broker and an events topic, for producing and consuming.',
     p:{enable_kafka:true}, tags:['Kafka']},
    {k:'grafana', t:'Grafana on a database', d:'Grafana wired to a fresh database with the telemetry schema loaded.',
     p:{enable_adb:true,seed_key:'iot',containers:[{name:'grafana',image:'docker.io/grafana/grafana:latest',port:3000}]}, tags:['Grafana','Autonomous DB']},
    {k:'jupyter', t:'Jupyter on a database', d:'A notebook with the sales schema already there to poke at from Python.',
     p:{enable_adb:true,seed_key:'sales',containers:[{name:'lab',image:'docker.io/jupyter/minimal-notebook:latest',port:8888}]}, tags:['Jupyter','Autonomous DB']},
    {k:'n8n', t:'n8n automation', d:'Workflow automation with a database behind it to store runs.',
     p:{enable_adb:true,containers:[{name:'n8n',image:'docker.io/n8nio/n8n:latest',port:5678}]}, tags:['n8n','Autonomous DB']},
    {k:'nginx', t:'nginx smoke test', d:'One container and a public URL, to check the plumbing end to end.',
     p:{enable_app:true,app_image:'docker.io/library/nginx:alpine',app_port:80,ttl_days:1}, tags:['container','1 day']}
  ];
  function newId(pfx){return (pfx||'sbx')+'-'+Math.random().toString(36).slice(2,7)}
  function needsRegistry(r){return JSON.stringify(r.p.containers||[]).indexOf('{R}')>=0}
  function recipePayload(r){
    var p=r.p, cs=(p.containers||[]).map(function(c){
      return {name:c.name, image:String(c.image).replace('{R}',OCIR), port:c.port};
    });
    return {sandbox_id:newId(r.k), action:'CREATE', ttl_days:p.ttl_days||3,
      enable_adb:!!p.enable_adb, enable_kafka:!!p.enable_kafka, enable_nosql:!!p.enable_nosql,
      enable_app:!!p.enable_app||cs.length>0, app_image:p.app_image||null, git_url:null,
      app_port:p.app_port||80, text:'one-click recipe: '+r.t,
      containers:cs.length?cs:undefined, seed_sql:p.seed_sql||undefined,
      app_files:p.app_files?JSON.stringify(p.app_files):undefined};
  }
  // ---- Per-sandbox landing page --------------------------------------
  // Everything a sandbox produced, in one place, clickable and copyable, with
  // a command for each so it can be tried rather than just looked at.
  function copyBtn(v){
    return '<button type="button" class="sf-btn sec" style="padding:2px 8px;font-size:11px;margin-left:6px" '
      + 'data-copy="'+esc(v)+'">copy</button>';
  }
  function row(label, value, opts){
    opts = opts || {};
    var body = opts.link ? '<a href="'+esc(value)+'" target="_blank">'+esc(value)+'</a>'
                         : '<code>'+esc(value)+'</code>';
    return '<tr><td style="padding:5px 12px 5px 0;color:#6b7280;white-space:nowrap;vertical-align:top">'
      + esc(label)+'</td><td style="padding:5px 0;word-break:break-all">'+body
      + (opts.copy===false?'':copyBtn(value))
      + (opts.hint?'<div style="font-size:11.5px;color:#6b7280;margin-top:2px">'+opts.hint+'</div>':'')
      + '</td></tr>';
  }
  function landingHtml(sid, o){
    var h = '<h3 style="margin:0 0 2px">'+esc(sid)+'</h3>'
      + '<p class="sf-sub" style="color:#6b7280;margin:0 0 12px;font-size:13px">'
      + 'Everything this sandbox created. Links open directly; the rest is here to copy.</p>';
    var t = '<table style="width:100%;border-collapse:collapse;font-size:13.5px">';
    var app = o.app && (o.app.urls||[]).filter(function(u){return u.indexOf('https://')===0})[0];
    if(app){
      t += row('App URL', app, {link:true});
      t += row('Test it', 'curl -s ' + app + ' | head', {});
    }
    if(o.adb){
      if(o.adb.sql_web_url) t += row('SQL Developer Web', o.adb.sql_web_url, {link:true});
      if(o.adb.apex_url)    t += row('APEX', o.adb.apex_url, {link:true});
      t += row('Database', o.adb.db_name || '', {});
      t += row('User', o.adb.admin_user || 'ADMIN', {});
      if(o.adb.admin_password) t += row('Password', o.adb.admin_password, {});
      if(o.adb.connect_string) t += row('Connect string', o.adb.connect_string,
        {hint:'python: oracledb.connect(user="ADMIN", password=..., dsn="'+esc(o.adb.connect_string)+'")'});
    }
    (o.databases||[]).slice(1).forEach(function(d){
      t += row('Database ('+d.name+')', d.db_name, {});
      if(d.sql_web_url) t += row('  SQL Web', d.sql_web_url, {link:true});
    });
    if(o.kafka) t += row('Kafka bootstrap', o.kafka.bootstrap_servers || '',
      {hint:'topics: '+esc((o.kafka.topics||[]).join(', '))});
    (o.buckets||[]).forEach(function(b){
      t += row('Bucket', b.name, {hint:'oci os object put -bn '+esc(b.name)+' --file ./x --namespace '+esc(b.namespace)});
    });
    (o.queues||[]).forEach(function(q){
      t += row('Queue', q.name, {});
      if(q.messages_endpoint) t += row('  endpoint', q.messages_endpoint, {link:true});
    });
    if(o.nosql&&o.nosql.tables) o.nosql.tables.forEach(function(n){
      t += row('NoSQL table', n, {hint:'oci nosql row get --table-name-or-id '+esc(n)});
    });
    (o.functions||[]).forEach(function(f){
      t += row('Function', f.name, {});
      if(f.invoke_endpoint) t += row('  invoke', f.invoke_endpoint, {hint:'oci fn function invoke --function-id '+esc(f.id||'')});
    });
    (o.dataflow_jobs||[]).forEach(function(j){
      t += row('Spark job', j.name, {hint:'script: '+esc(j.file_uri||'')});
    });
    if(o.catalog) t += row('Data Catalog', o.catalog.display_name || '', {});
    if(o.low_code&&o.low_code.rest_base) t += row('REST (ORDS)', o.low_code.rest_base,
      {hint:'authenticated as ADMIN with the password above'});
    t += '</table>';
    h += t;
    if(o.consoles){
      var links=[];
      for(var k in o.consoles){ if(o.consoles[k]) links.push(
        '<a href="'+esc(o.consoles[k])+'" target="_blank">'+esc(k.replace(/_/g,' '))+'</a>'); }
      if(links.length) h += '<p style="margin:12px 0 0;font-size:13px">Open in the OCI console: '
        + links.join(' &middot; ') + '</p>';
    }
    if(o.warnings&&o.warnings.length) h += '<p class="sf-err" style="margin-top:10px">'
      + o.warnings.map(esc).join('<br>') + '</p>';
    return h;
  }
  function showLanding(sid){
    var r=(window.__sfRows||[]).filter(function(x){return x.sandbox_id===sid})[0];
    if(!r||!r.outputs)return;
    var o; try{o=typeof r.outputs==='string'?JSON.parse(r.outputs):r.outputs}catch(e){return}
    var box=$('#sf-landing');
    if(!box){
      box=document.createElement('div'); box.id='sf-landing'; box.className='sf-panel';
      box.style.marginTop='14px';
      $('#sf-status').parentNode.insertBefore(box, $('#sf-status'));
    }
    box.innerHTML=landingHtml(sid,o)
      +'<div style="margin-top:12px"><button type="button" class="sf-btn sec" id="sf-landing-close">Close</button></div>';
    box.scrollIntoView({behavior:'smooth',block:'start'});
    $('#sf-landing-close').onclick=function(){box.remove()};
  }
  document.addEventListener('click', function(e){
    var op = e.target.closest && e.target.closest('[data-open]');
    if(op){ showLanding(op.getAttribute('data-open')); return; }
    var b = e.target.closest && e.target.closest('[data-copy]');
    if(b){ navigator.clipboard.writeText(b.getAttribute('data-copy'));
           var t=b.textContent; b.textContent='copied'; setTimeout(function(){b.textContent=t},900); }
  });

  function drawRecipes(){
    var el=$('#sf-recs'); if(!el)return;
    var list=RECIPES.filter(function(r){return OCIR||!needsRegistry(r)});
    el.innerHTML=list.map(function(r,i){
      return '<div class="sf-rec" data-i="'+i+'"><div class="t">'+esc(r.t)+'</div><div class="d">'+esc(r.d)+'</div>'
        +'<div class="p">'+r.tags.map(function(x){return '<span>'+esc(x)+'</span>'}).join('')+'</div></div>';
    }).join('');
    el.setAttribute('data-keys', list.map(function(r){return RECIPES.indexOf(r)}).join(','));
  }
  function submitPayload(box,payload,note){
    box.innerHTML='<span class="sf-spin"></span>Queueing...';
    call('submit',payload).then(function(s){
      if(s.err){box.innerHTML='<span class="sf-err">'+esc(s.err)+'</span>';return}
      box.innerHTML='<span class="sf-chip">Queued as request #'+esc(s.id)+'</span>'
        +'<span class="sf-chip">'+esc(payload.sandbox_id)+'</span>';
      pushHist({role:'user',text:note||('(queued '+payload.sandbox_id+')')});
      refresh();
    });
  }
  function hideSugg(){ if($('#sf-chat-tabs')){showTab('chat');return}
    var g=$('#sf-sugg'); if(g)g.classList.add('sf-hide'); var t=$('#sf-starters'); if(t)t.classList.remove('sf-hide') }
  function offerRecipe(r){
    var payload=recipePayload(r);
    hideSugg();
    addMsg('me',esc(r.t));
    var d=addMsg('ai','<b>'+esc(r.t)+'</b> &mdash; '+esc(r.d));
    d.insertAdjacentHTML('beforeend',infraHtml({enable_adb:payload.enable_adb,enable_kafka:payload.enable_kafka,
      enable_app:payload.enable_app,containers:payload.containers,app_image:payload.app_image,
      app_port:payload.app_port,seed_sql:payload.seed_sql,app_files:payload.app_files,ttl_days:payload.ttl_days}));
    var box=document.createElement('div'); box.className='sf-act';
    box.innerHTML='<button type="button" class="sf-btn">Build it now</button><button type="button" class="sf-btn sec">Not now</button>';
    d.appendChild(box);
    box.children[0].onclick=function(){submitPayload(box,payload,'(building '+r.t+' as '+payload.sandbox_id+')')};
    box.children[1].onclick=function(){box.remove()};
  }


  // ---- Bring your own image, repo or SQL, without leaving the chat --------
  var SRC_HELP={
    image:'Any image a public registry will serve, or one you have already pushed to our OCIR namespace.<br>Examples: <code>docker.io/library/nginx:alpine</code> &middot; <code>ghcr.io/you/app:latest</code> &middot; <code>'+OCIR+'yourapp/app:latest</code>',
    git:'A public Git repository with a <code>Dockerfile</code> at its root. The factory clones it and builds the image for you.<br>Example: <code>https://github.com/you/yourapp</code>',
    path:'A folder on the machine running the worker, with a <code>Dockerfile</code> in it. Useful for code that is not pushed anywhere yet.<br>Example: <code>C:\\Users\\you\\projects\\yourapp</code>'
  };
  function openForm(kind){
    hideSugg();
    addMsg('me', kind==='deploy'?'Deploy something of my own':'Load my own SQL');
    var d=addMsg('ai', kind==='deploy'
      ? 'Tell me where the code lives and I will build it, put it on a public HTTPS URL and wire a database into it if you want one.'
      : 'Paste the SQL you want in the new database. It runs once, before anything starts, and Select AI picks the schema up automatically.');
    var f=document.createElement('div'); f.className='sf-form'; d.appendChild(f);
    if(kind==='sql'){
      f.innerHTML='<div class="sf-field"><label>SQL to run in the new database</label>'
        +'<textarea id="sf-f-sql" style="min-height:130px" placeholder="create table ...;&#10;insert into ... values (...);"></textarea></div>'
        +'<div class="sf-row"><div class="sf-field" style="flex:0 1 150px"><label>Keep it for</label>'
        +'<select id="sf-f-ttl"><option value="1">1 day</option><option value="2">2 days</option><option value="3" selected>3 days (max)</option></select></div>'
        +'<div class="sf-field" style="flex:0 1 220px"><label>Also give me</label>'
        +'<select id="sf-f-extra"><option value="">just the database</option><option value="mcp">an MCP endpoint</option>'
        +'<option value="both">MCP + the Studio chat UI</option></select></div></div>'
        +'<div class="sf-act"><button type="button" class="sf-btn">Create it</button><button type="button" class="sf-btn sec">Cancel</button></div>';
      var act=f.querySelector('.sf-act');
      act.children[1].onclick=function(){f.remove()};
      act.children[0].onclick=function(){
        var sql=f.querySelector('#sf-f-sql').value.trim();
        if(!sql){f.querySelector('#sf-f-sql').focus();return}
        var extra=f.querySelector('#sf-f-extra').value, cs=[];
        if(extra==='mcp'||extra==='both')cs.push({name:'mcp',image:'{R}schemagate/app:latest',port:8765});
        if(extra==='both')cs.unshift({name:'studio',image:'{R}studio/app:latest',port:8770});
        submitPayload(act,{sandbox_id:newId('sql'),action:'CREATE',ttl_days:+f.querySelector('#sf-f-ttl').value,
          enable_adb:true,enable_kafka:false,enable_app:cs.length>0,app_image:null,git_url:null,app_port:80,
          text:'own SQL loaded from chat',containers:cs.length?cs:undefined,seed_sql:sql},'(created a database with my own SQL)');
      };
      return;
    }
    f.innerHTML='<div class="sf-tabs"><button type="button" data-s="image" class="on">Public image</button>'
      +'<button type="button" data-s="git">Git repo</button><button type="button" data-s="path">Folder on the worker</button></div>'
      +'<p id="sf-f-help" style="margin:0 0 10px;font-size:12px;color:#6b7280;line-height:1.5">'+SRC_HELP.image+'</p>'
      +'<div class="sf-row"><div class="sf-field"><label id="sf-f-lab">Image</label>'
      +'<input id="sf-f-src" placeholder="docker.io/library/nginx:alpine"></div>'
      +'<div class="sf-field" style="flex:0 1 120px"><label>Port</label><input id="sf-f-port" value="80"></div></div>'
      +'<div class="sf-row"><div class="sf-field" style="flex:0 1 150px"><label>Keep it for</label>'
      +'<select id="sf-f-ttl"><option value="1">1 day</option><option value="2">2 days</option><option value="3" selected>3 days (max)</option></select></div>'
      +'<div class="sf-field" style="flex:0 1 250px"><label>Database</label>'
      +'<select id="sf-f-db"><option value="">no database</option><option value="empty">empty database, Select AI on</option>'
      +'<option value="sales">database + sample sales data</option></select></div></div>'
      +'<div class="sf-act"><button type="button" class="sf-btn">Build and deploy</button><button type="button" class="sf-btn sec">Cancel</button></div>';
    var src='image';
    f.querySelectorAll('.sf-tabs button').forEach(function(b){
      b.onclick=function(){
        src=b.getAttribute('data-s');
        f.querySelectorAll('.sf-tabs button').forEach(function(x){x.classList.toggle('on',x===b)});
        f.querySelector('#sf-f-help').innerHTML=SRC_HELP[src];
        f.querySelector('#sf-f-lab').textContent=src==='image'?'Image':(src==='git'?'Repository URL':'Folder path');
        f.querySelector('#sf-f-src').placeholder=src==='image'?'docker.io/library/nginx:alpine'
          :(src==='git'?'https://github.com/you/yourapp':'C:\\Users\\you\\projects\\yourapp');
      };
    });
    var act=f.querySelector('.sf-act');
    act.children[1].onclick=function(){f.remove()};
    act.children[0].onclick=function(){
      var v=f.querySelector('#sf-f-src').value.trim();
      if(!v){f.querySelector('#sf-f-src').focus();return}
      var db=f.querySelector('#sf-f-db').value, isImg=(src==='image');
      submitPayload(act,{sandbox_id:newId('app'),action:isImg?'CREATE':'DEPLOY',
        ttl_days:+f.querySelector('#sf-f-ttl').value,
        enable_adb:!!db, enable_kafka:false, enable_app:true,
        app_image:isImg?v:null, git_url:isImg?null:v, app_port:+f.querySelector('#sf-f-port').value||80,
        text:'deployed from chat: '+v,
        seed_key:db==='sales'?'sales':undefined},'(deploying '+v+')');
    };
  }

  // What a plan or a chat action will actually build in OCI, in plain words.
  function infraHtml(a){
    var L=[];
    if(a.enable_adb)L.push('<li><b>Autonomous Database</b> &mdash; '+esc(a.adb_tier||'always free')+' tier, Oracle 23ai, private subnet. Select AI (plain-English queries) and AI cataloguing are switched on for you. Every table is also published as a REST endpoint through ORDS, and an APEX workspace is waiting if you want to click a low-code app together. You get SQL Developer Web and APEX URLs.</li>');
    if(a.enable_nosql)L.push('<li><b>OCI NoSQL</b> &mdash; serverless JSON tables with on-demand capacity. You get the table names and the compartment; no cluster to size and nothing running when idle.</li>');
    if(a.enable_kafka)L.push('<li><b>Kafka</b> &mdash; '+esc(a.kafka_mode||'OCI Streaming')+', topic <code>events</code>. You get a bootstrap server address.</li>');
    var cs=(a.containers&&a.containers.length)?a.containers:((a.enable_app||a.git_url||a.app_image)?[{name:'web',image:a.git_url||a.app_image||'nginx:alpine',port:a.app_port||80}]:[]);
    if(cs.length)L.push('<li><b>'+(cs.length>1?cs.length+' containers in one instance':'1 container')+'</b> on CI.Standard.A1.Flex (Arm)'
      +(cs.length>1?' &mdash; they share a host and reach each other on localhost':'')+', behind a public HTTPS URL: '+cs.map(function(c){return '<code>'+esc(c.name||'web')+'</code> &rarr; '+esc(c.image)+':'+esc(c.port||80)}).join(', ')+'.</li>');
    if(a.app_files){var n=Object.keys(JSON.parse(a.app_files)).length;
      L.push('<li><b>Your app, built here</b> &mdash; '+n+' source files are sent with the request, built into an image in OCI and deployed. Nothing to push to a registry.</li>')}
    if(a.seed_sql)L.push('<li><b>Sample data</b> &mdash; '+esc(String(a.seed_sql).split(';').filter(function(x){return x.trim()}).length)+' SQL statements loaded into the new database before anything starts.</li>');
    L.push('<li>Everything is tagged with your sandbox id and <b>auto-destroyed after '+esc(a.ttl_days||3)+' day'+((a.ttl_days||3)==1?'':'s')+'</b>.</li>');
    return '<h4 style="margin:10px 0 4px">What this builds in OCI</h4><ul style="margin:0;padding-left:18px">'+L.join('')+'</ul>';
  }
  function sendChat(){
    var t=$('#sf-chat-in').value.trim(); if(!t)return;
    hideSugg();
    $('#sf-chat-in').value=''; $('#sf-chat-in').style.height='auto'; dropQuickChips(); addMsg('me',esc(t)); pushHist({role:'user',text:t});
    var typing=typingBubble(); $('#sf-chat-send').disabled=true;
    call('chat',{messages:hist.slice(-12)}).then(function(r){
      if(typing)typing.remove(); $('#sf-chat-send').disabled=false; $('#sf-chat-in').focus();
      if(r.err){addMsg('ai','<span class="sf-err">'+esc(r.err)+'</span>');return}
      var m=(r.raw||'').match(/\{[\s\S]*\}/), j=null; try{j=m?JSON.parse(m[0]):null}catch(e){}
      var reply=j&&j.reply?j.reply:(r.raw||''); pushHist({role:'assistant',text:reply});
      var d=addMsg('ai',fmt(reply));
      var qs=j&&j.questions; if(qs&&qs.length){
        d.insertAdjacentHTML('beforeend','<h4 style="margin:10px 0 4px">A couple of things first</h4><ol style="margin:0;padding-left:18px">'
          +qs.map(function(q){return '<li>'+esc(q)+'</li>'}).join('')+'</ol>');
        return;   // hold the action until they answer
      }
      var a=j&&j.action; if(a&&a.type){
        var box=document.createElement('div'); box.className='sf-act';
        if(a.type!=='destroy')d.insertAdjacentHTML('beforeend',infraHtml(a));
        var label=a.type==='destroy'?'Destroy '+a.sandbox_id:(a.type==='deploy'?'Deploy it':'Create it');
        box.innerHTML='<button type="button" class="sf-btn">'+esc(label)+'</button><button type="button" class="sf-btn sec">Not now</button>';
        d.appendChild(box);
        box.children[0].onclick=function(){ box.innerHTML='<span class="sf-spin"></span>Queueing...';
          var payload=a.type==='destroy'?{sandbox_id:a.sandbox_id,action:'DESTROY',ttl_days:1,enable_adb:false,enable_kafka:false,enable_app:false}
            :{sandbox_id:a.sandbox_id,action:a.git_url?'DEPLOY':'CREATE',ttl_days:a.ttl_days||3,enable_adb:!!a.enable_adb,enable_kafka:!!a.enable_kafka,enable_nosql:!!a.enable_nosql,enable_app:!!a.enable_app||!!a.git_url||!!a.app_image||!!(a.containers&&a.containers.length),
              app_image:a.app_image||null,git_url:a.git_url||null,app_port:a.app_port||80,text:t,containers:(a.containers&&a.containers.length)?a.containers:undefined,seed_sql:a.seed_sql||undefined};
          call('submit',payload).then(function(s){ if(s.err){box.innerHTML='<span class="sf-err">'+esc(s.err)+'</span>';return}
            box.innerHTML='<span class="sf-chip">Queued as request #'+esc(s.id)+'</span>'; pushHist({role:'user',text:'(confirmed: '+a.type+' '+a.sandbox_id+' queued)'}); refresh(); });
        };
        box.children[1].onclick=function(){box.remove()};
      }
    }).catch(function(e){if(typing)typing.remove();$('#sf-chat-send').disabled=false;addMsg('ai','<span class="sf-err">'+esc(e&&e.statusText||e)+'</span>')});
  }
    // ---- Chat / Starters as real tabs ------------------------------------
  // The catalogue used to live inside #sf-msgs, so nineteen cards pushed the
  // conversation off the screen. It is its own pane now, and #sf-msgs no longer
  // needs the fixed 720px height it was given to hold those cards.
  function injectCss(){
    if($('#sf-chat-css'))return;
    var st=document.createElement('style'); st.id='sf-chat-css';
    st.textContent=[
      '#sf-chat-tabs{display:flex;align-items:center;gap:2px;border-bottom:1px solid rgba(0,0,0,.12);margin:0 0 10px}',
      '#sf-chat-tabs button{appearance:none;background:none;border:0;border-bottom:2px solid transparent;padding:8px 14px;font:inherit;font-size:13px;color:#5b6270;cursor:pointer}',
      '#sf-chat-tabs button.on{color:#111;border-bottom-color:#2563eb;font-weight:600}',
      '#sf-chat-tabs button:hover{color:#111}',
      '#sf-chat-tabs .count{opacity:.5;font-weight:400;margin-left:5px}',
      // was height:min(64vh,720px);min-height:360px - sized for the card grid
      '#sf-msgs{height:auto;min-height:120px;max-height:min(56vh,520px);overflow-y:auto}',
      '.sf-msg p{margin:0 0 8px}',
      '.sf-msg p:last-child{margin:0}',
      '#sf-sugg.sf-pane{max-height:56vh;overflow:auto;padding:2px 4px 2px 0}',
      '#sf-recs{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:8px}',
      '#sf-recs .sf-rec{padding:10px 12px}',
      '#sf-recs .sf-rec .t{font-size:13px;line-height:1.25}',
      '#sf-recs .sf-rec .d{font-size:12px;line-height:1.35;margin-top:3px}',
      '#sf-recs .sf-rec .p{margin-top:6px}',
      '#sf-recs .sf-rec .p span{font-size:10.5px;padding:1px 6px}',
      '#sf-chat-in{width:100%;line-height:1.4}',
      '.sf-open{display:inline-block;background:#2563eb;color:#fff !important;padding:7px 15px;border-radius:6px;font-weight:600;text-decoration:none;margin:0 0 8px}',
      '.sf-open:hover{background:#1d4fd7}',
      '.sf-nourl{display:inline-block;color:#8a90a0;font-size:12px;margin:0 0 8px}',
      // the AI is thinking: show it in the conversation, not as a detached spinner
      '.sf-typing{display:inline-flex;gap:4px;align-items:center;padding:3px 0}',
      '.sf-typing i{width:6px;height:6px;border-radius:50%;background:#94a3b8;display:inline-block;animation:sfblink 1.2s infinite}',
      '.sf-typing i:nth-child(2){animation-delay:.2s}',
      '.sf-typing i:nth-child(3){animation-delay:.4s}',
      '@keyframes sfblink{0%,80%,100%{opacity:.25}40%{opacity:1}}',
      '#sf-quick{display:flex;flex-wrap:wrap;gap:6px;margin-top:2px}',
      '.sf-chip-q{appearance:none;border:1px solid rgba(0,0,0,.14);background:#fff;border-radius:16px;padding:6px 12px;font:inherit;font-size:12.5px;color:#334155;cursor:pointer;text-align:left}',
      '.sf-chip-q:hover{border-color:#2563eb;color:#1d4fd7}'
    ].join('\n');
    document.head.appendChild(st);
  }
  function showTab(name){
    var msgs=$('#sf-msgs'), sugg=$('#sf-sugg');
    if(msgs)msgs.classList.toggle('sf-hide',name!=='chat');
    if(sugg)sugg.classList.toggle('sf-hide',name!=='starters');
    $$('#sf-chat-tabs button[data-tab]').forEach(function(b){b.classList.toggle('on',b.dataset.tab===name)});
    if(name==='chat'&&msgs){msgs.scrollTop=1e9; var i=$('#sf-chat-in'); if(i)i.focus()}
  }
  function buildTabs(){
    var chat=$('#sf-chat'), msgs=$('#sf-msgs'), sugg=$('#sf-sugg');
    if(!chat||!msgs||$('#sf-chat-tabs'))return;
    if(sugg){sugg.classList.add('sf-pane'); msgs.parentNode.insertBefore(sugg,msgs.nextSibling)}
    var bar=document.createElement('div'); bar.id='sf-chat-tabs';
    bar.innerHTML='<button type="button" data-tab="chat" class="on">Chat</button>'+
      '<button type="button" data-tab="starters">Starters<span class="count">'+RECIPES.length+'</span></button>';
    chat.insertBefore(bar,msgs);
    bar.addEventListener('click',function(e){
      var b=e.target.closest&&e.target.closest('button[data-tab]'); if(b)showTab(b.dataset.tab);
    });
    var old=$('#sf-starters'); if(old)old.classList.add('sf-hide');
    showTab('chat');
  }
  function addClear(){
    var bar=$('#sf-chat-tabs'); if(!bar||$('#sf-chat-clear'))return;
    var b=document.createElement('button');
    b.type='button'; b.id='sf-chat-clear'; b.textContent='Clear chat';
    b.title='Forget this conversation';
    b.style.marginLeft='auto'; b.style.fontSize='12px'; b.style.color='#8a90a0';
    b.onclick=function(){
      hist.length=0; saveHist();
      $$('#sf-msgs .sf-msg').forEach(function(n,i){if(i)n.remove()});
      addQuickChips(); showTab('chat');
    };
    bar.appendChild(b);
  }
  // Something to click on an empty conversation, instead of dead space.
  function addQuickChips(){
    var msgs=$('#sf-msgs'); if(!msgs||$('#sf-quick')||hist.length)return;
    var wrap=document.createElement('div'); wrap.id='sf-quick';
    ['What do I have running?','Deploy nginx for a day',
     'A database with sample sales data','Destroy my oldest sandbox'
    ].forEach(function(q){
      var b=document.createElement('button');
      b.type='button'; b.className='sf-chip-q'; b.textContent=q;
      wrap.appendChild(b);
    });
    msgs.appendChild(wrap);
    wrap.addEventListener('click',function(e){
      var b=e.target.closest&&e.target.closest('button'); if(!b)return;
      $('#sf-chat-in').value=b.textContent; sendChat();
    });
  }
  function dropQuickChips(){var q=$('#sf-quick'); if(q)q.remove()}
  // The wait used to be a spinner off to the side. Put it in the conversation
  // where the answer will appear, so the chat reads as a back-and-forth.
  function typingBubble(){
    return addMsg('ai','<span class="sf-typing"><i></i><i></i><i></i></span>');
  }
  // A single-line <input> cannot hold a paragraph, so promote it to a textarea
  // that grows with the message. Enter sends, Shift+Enter starts a new line.
  function upgradeInput(){
    var i=$('#sf-chat-in'); if(!i||i.tagName==='TEXTAREA')return;
    var ta=document.createElement('textarea');
    ta.id='sf-chat-in'; ta.rows=1; ta.className=i.className;
    ta.placeholder='Message the factory…  Enter to send, Shift+Enter for a new line';
    ta.style.resize='none'; ta.style.overflowY='auto'; ta.style.maxHeight='160px';
    i.parentNode.replaceChild(ta,i);
    ta.addEventListener('input',function(){this.style.height='auto';this.style.height=Math.min(this.scrollHeight,160)+'px'});
  }
  function restoreChat(){
    if(!hist.length){addQuickChips();return}
    hist.forEach(function(m){addMsg(m.role==='user'?'me':'ai',m.role==='user'?esc(m.text):fmt(m.text))});
    var c=$('#sf-msgs'); if(c)c.scrollTop=1e9;
  }
  injectCss();
  upgradeInput();

  $('#sf-chat-send').onclick=sendChat; $('#sf-model').onchange=function(){$('#sf-model-name').textContent=this.options[this.selectedIndex].text}; $('#sf-chat-in').addEventListener('keydown',function(e){if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendChat()}});
  $('#sf-starters').onclick=function(){
    var g=$('#sf-sugg'); if(!g)return;
    g.classList.remove('sf-hide'); this.classList.add('sf-hide');
    g.scrollIntoView({block:'start',behavior:'smooth'});
  };
  call('config',{}).then(function(c){
    if(!c)return;
    if(c.registry_prefix)OCIR=c.registry_prefix;
    if(c.user){var w=$('#sf-who');
      w.textContent='Signed in as '+c.user+' · '+(c.live||0)+' of '+(c.cap||3)+' sandboxes';
      w.classList.remove('sf-hide');}
  })
    .catch(function(){}).then(function(){drawRecipes(); buildTabs(); addClear(); restoreChat()});
  refresh();
}
(function(){ function go(){ if(window.apex&&apex.server){sfInit()} else {setTimeout(go,100)} } if(document.readyState==='loading'){document.addEventListener('DOMContentLoaded',go)} else {go()} })();
