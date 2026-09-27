
function sfInit(){
  var mode=null, plan=null, cfg={enable_adb:false,enable_kafka:false,enable_nosql:false,enable_app:false}, timer=null;
  var $=function(s){return document.querySelector(s)}, $$=function(s){return Array.prototype.slice.call(document.querySelectorAll(s))};
  // x02 is a VARCHAR2(32767): anything big (attached code) rides in p_clob_01.
  function call(action,payload){payload=payload||{}; var m=document.querySelector('#sf-model'); if(m&&!payload.model)payload.model=m.value;
    var clob=payload.clob; delete payload.clob; var o={x01:action,x02:JSON.stringify(payload)}; if(clob)o.p_clob_01=clob;
    // x02 holds 32767 characters; a bigger request (pipeline files, function source) goes whole in the CLOB
    else if(action!=='chat'&&o.x02.length>30000){ o.p_clob_01=o.x02; o.x02=JSON.stringify({__clob:1, model:payload.model}); }
    // a failed call always comes back as {err: a sentence}: never a bare status text such as "OK"
    return new Promise(function(res){ apex.server.process('SF',o,{dataType:'json'}).then(res,function(jq,status){
      var body=(jq&&jq.responseText)||'', ora=(body.match(/ORA-\d{5}[^<"]*/)||[])[0];
      res({err: ora ? 'The factory could not do that: '+ora : (jq&&jq.status===0 ? 'The factory did not answer. Check the connection and send it again.' : 'The factory answered in an unexpected way ('+((jq&&jq.status)||status)+'). Please send it again.')});
    }); })}
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
      plan=modelJson(r.raw);
      if(!plan){showAI('<h4>AI answer</h4><p>'+esc(r.raw)+'</p>');return}
      var h='<h4>&#10024; Plan</h4><p>'+esc(plan.summary)+'</p><div>';
      if(plan.enable_adb)h+='<span class="sf-chip">Autonomous Database</span>';
      if(plan.enable_kafka)h+='<span class="sf-chip">Kafka</span>';
      if(plan.containers&&plan.containers.length){plan.containers.forEach(function(c){h+='<span class="sf-chip">'+esc(c.name)+': '+esc(c.image)+' :'+esc(c.port)+'</span>'})}
      else if(plan.enable_app)h+='<span class="sf-chip">App: '+esc(plan.git_url||plan.app_image||'(image or repo needed)')+' on port '+esc(plan.app_port||80)+'</span>';
      if(plan.seed_sql)h+='<span class="sf-chip">sample data: '+esc(String(plan.seed_sql).split(';').filter(function(x){return x.trim()}).length)+' SQL statements</span>';
      h+='<span class="sf-chip">'+(plan.ttl_days||3)+' days</span></div>';
      if(plan.steps&&plan.steps.length){h+='<h4 style="margin-top:10px">What to do next</h4><ol>'+plan.steps.map(function(s){return '<li>'+esc(s)+'</li>'}).join('')+'</ol>'}
      h+=infraHtml(plan);
      if(plan.tips&&plan.tips.length){h+='<ul>'+plan.tips.map(function(s){return '<li>'+esc(s)+'</li>'}).join('')+'</ul>'}
      h+=widgetsHtml({cost:plan.cost,sandboxes:[]},'','');
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
    var planHas=mode==='describe'&&plan&&['buckets','queues','functions','dataflow_jobs','databases','app_instances'].some(function(k){return plan[k]&&plan[k].length})||(plan&&plan.enable_catalog);
    if(!cfg.enable_adb&&!cfg.enable_kafka&&!cfg.enable_nosql&&!cfg.enable_app&&!planHas){err('Pick at least one piece.');return}
    var git=$('#sf-git').value.trim(), payload={sandbox_id:id, ttl_days:+$('#sf-ttl').value, enable_adb:cfg.enable_adb, enable_kafka:cfg.enable_kafka, enable_nosql:cfg.enable_nosql, enable_app:cfg.enable_app,
      action:(cfg.enable_app&&git)?'DEPLOY':'CREATE', git_url:git||null, app_image:$('#sf-image').value.trim()||null, app_port:+$('#sf-port').value||80, text:mode==='describe'?$('#sf-text').value.trim():null, env:envFrom(document.querySelector('#sf-form-env'))};
    if(mode==='describe'&&plan){ if(plan.containers&&plan.containers.length)payload.containers=plan.containers; if(plan.seed_sql)payload.seed_sql=plan.seed_sql;
      payload.buckets=nz(plan.buckets); payload.queues=nz(plan.queues); payload.functions=nz(plan.functions); payload.dataflow_jobs=nz(plan.dataflow_jobs);
      payload.databases=nz(plan.databases); payload.app_instances=nz(plan.app_instances); payload.enable_catalog=!!plan.enable_catalog; payload.enable_aidp=!!plan.enable_aidp; }
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
      rows=rows||[]; var active=false;
      var h='<div class="sf-tabs" style="margin:18px 0 10px">'
        +'<button type="button" class="'+(window.__sfTab==='history'?'':'on')+'" data-tab="live">Running ('+rows.length+')</button>'
        +'<button type="button" class="'+(window.__sfTab==='history'?'on':'')+'" data-tab="history">History</button></div>';
      if(window.__sfTab==='history'){ $('#sf-status').innerHTML=h+'<div id="sf-history"><span class="sf-spin"></span>Loading history...</div>'; loadHistory(); return; }
      if(!rows.length)h+='<p style="color:#6b7280;font-size:13px">Nothing running. Destroyed sandboxes are under History.</p>';
      window.__sfRows = rows;
      rows.forEach(function(r){
        if(r.status==='QUEUED'||r.status==='RUNNING')active=true;
        var o=null; try{o=r.outputs?JSON.parse(r.outputs):null}catch(e){}
        var exp=o&&o.sandbox&&o.sandbox.expires;
        h+='<div class="sf-req"><div class="hd">'+(r.status==='RUNNING'?'<span class="sf-spin"></span>':'')+'<b>'+esc(r.sandbox_id)+'</b><span class="sf-badge '+esc(r.status)+'">'+esc(r.status)+'</span><span>'+esc(r.action)+'</span>'
          +(exp&&r.status==='DONE'&&r.action!=='DESTROY'?'<span style="font-size:12px;color:#6b7280">expires '+esc(exp)+' &middot; <label>lifetime <select class="sf-ttl-pick" data-ttl="'+esc(r.sandbox_id)+'" title="Re-applies the sandbox with a new lifetime from now"><option value="">change&hellip;</option><option value="1">1 day</option><option value="2">2 days</option><option value="3">3 days</option><option value="5">5 days</option><option value="7">7 days</option><option value="14">14 days</option><option value="21">21 days</option><option value="30">30 days</option></select></label></span>':'')
          +'<span class="meta">'+esc(r.created)+' &middot; '+esc(r.elapsed)+'s</span></div>';
        if(r.status==='DONE'&&r.action!=='DESTROY'&&r.outputs){
          h+='<button type="button" class="sf-btn sec" style="padding:4px 12px;font-size:12px;margin-top:6px" '
            +'data-open="'+esc(r.sandbox_id)+'">Open everything</button>';
        }
        // a built or failed sandbox can be destroyed from its card (a failed one may hold resources)
        if((r.status==='DONE'||r.status==='FAILED')&&r.action!=='DESTROY'){
          h+=' <button type="button" class="sf-btn sec" style="padding:4px 12px;font-size:12px;margin-top:6px" '
            +'data-destroy="'+esc(r.sandbox_id)+'" title="Deletes everything in this sandbox">Destroy</button>';
        }
        if(o&&r.status==='DONE'){ h+='<div class="sf-links">';
          var pu=primaryUrl(o);
          if(pu){h+='<a class="sf-open" href="'+esc(pu)+'" target="_blank" rel="noopener">Open '+esc(r.sandbox_id)+' &rarr;</a><br>'}
          else{h+='<button type="button" class="sf-open" style="border:0;cursor:pointer" data-open="'+esc(r.sandbox_id)+'">Open '+esc(r.sandbox_id)+' &rarr;</button><br>'}
          if(o.app&&o.app.urls){var us=o.app.urls.filter(function(u){return u.indexOf('https://')===0}); if(!us.length)us=o.app.urls; us.forEach(function(u){h+='<a href="'+esc(u)+'" target="_blank">'+esc(u)+'</a>'});
            if(o.app.containers&&o.app.containers.length>1&&us.length){o.app.containers.slice(1).forEach(function(n){h+='<a href="'+esc(us[0])+'/'+esc(n)+'" target="_blank">'+esc(us[0])+'/'+esc(n)+'</a>'})}}
          (o.logins||[]).forEach(function(l){
            h+='<div style="margin-top:6px">'+esc(l.service)+' &middot; user <code>'+esc(l.user)+'</code> &middot; password <code>'+esc(l.password)+'</code></div>';
          });
          if(o.adb){h+='<div style="margin-top:6px"><a href="'+esc(o.adb.sql_web_url)+'" target="_blank">SQL Developer Web</a> &middot; user <code>'+esc(o.adb.admin_user||'ADMIN')+'</code>'
              +(o.adb.admin_password?' &middot; password <code>'+esc(o.adb.admin_password)+'</code> <button type="button" class="sf-btn sec" style="padding:2px 8px;font-size:12px" onclick="navigator.clipboard.writeText(this.previousElementSibling.textContent)">copy</button>':'')
              +'<br>connect string <code>'+esc(o.adb.connect_string)+'</code></div>'}

          if(o.low_code&&o.low_code.rest_base)h+='<code>REST: '+esc(o.low_code.rest_base)+'</code>';
          if(o.rag){ h+=o.rag.error?'<div class="sf-err" style="margin-top:6px">RAG was not set up: '+esc(o.rag.error)+'</div>'
            :'<div style="margin-top:6px"><b>Documents</b>: drop files into <code>'+esc(o.rag.bucket)+'/docs/</code> (PDF, Word, HTML, text, images). Oracle indexes them every '+esc(o.rag.refresh_minutes)+' min; images are described as text first.<br>Ask: <code>POST '+esc(o.rag.ask_url||'')+'</code> with <code>{"question": "..."}</code> as ADMIN, or <code>select ai narrate ...</code> in SQL.</div>'; }
          if(o.kafka)h+='<div style="margin-top:6px">kafka <code>'+esc(o.kafka.public_bootstrap||o.kafka.bootstrap_servers)+'</code>'
            +(o.kafka.username?' &middot; user <code>'+esc(o.kafka.username)+'</code> &middot; password <code>'+esc(o.kafka.password)+'</code>':'')+'</div>';




          resourceLinks(o).forEach(function(x){h+=x[1]?'<a class="sf-reslink" href="'+esc(x[1])+'" target="_blank" rel="noopener">'+esc(x[0])+' &#8599;</a>':'<code>'+esc(x[0])+'</code>'});
          (o.databases||[]).filter(function(d){return d.name!=='primary'}).forEach(function(d){
            h+='<div style="margin-top:6px">'+(d.sql_web_url?'<a href="'+esc(d.sql_web_url)+'" target="_blank">'+esc(d.name)+' SQL Developer Web</a>':esc(d.name))
              +' &middot; user <code>ADMIN</code>'+(d.admin_password?' &middot; password <code>'+esc(d.admin_password)+'</code>':'')+'</div>'});
          if(o.kafka&&o.kafka.console_url)h+='<a class="sf-reslink" href="'+esc(o.kafka.console_url)+'" target="_blank" rel="noopener">Kafka stream &#8599;</a>';
          if(o.warnings&&o.warnings.length)h+='<div class="sf-err" style="margin-top:6px">'
              +o.warnings.map(esc).join('<br>')
              +' <button type="button" class="sf-btn sec" style="padding:3px 10px;font-size:12px;margin-left:6px" data-retry="'+esc(r.sandbox_id)+'">Retry setup</button></div>';
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
  // ---- a conversation that revises one plan -------------------------------
  var LAST_ACT=null;
  function compactAction(a){
    var c=JSON.parse(JSON.stringify(a));
    (c.functions||[]).forEach(function(f){ if(f.files)Object.keys(f.files).forEach(function(k){f.files[k]='(kept)'}); });
    (c.dataflow_jobs||[]).forEach(function(d){ if(d.script)d.script='(kept)'; });
    if(c.app_files)c.app_files='(kept)';
    if(c.seed_sql&&c.seed_sql.length>600)c.seed_sql='(kept)';
    delete c.workload_files;
    return JSON.stringify(c);
  }
  // A revised plan keeps the code of the one before: "(kept)" or a missing
  // script/source for a job or function of the same name is copied back.
  function keepFromLast(a){
    if(!LAST_ACT)return;
    var byName=function(list){var m={};(list||[]).forEach(function(x){m[x.name]=x});return m};
    var lf=byName(LAST_ACT.functions), ld=byName(LAST_ACT.dataflow_jobs);
    (a.functions||[]).forEach(function(f){ var o=lf[f.name]; if(!o)return;
      var kept=!f.files||Object.keys(f.files).some(function(k){return f.files[k]==='(kept)'});
      if(kept&&o.files&&!f.git_url&&!f.image)f.files=o.files; });
    (a.dataflow_jobs||[]).forEach(function(d){ var o=ld[d.name]; if(o&&(!d.script||d.script==='(kept)')&&o.script)d.script=o.script; });
    if((!a.app_files||a.app_files==='(kept)')&&LAST_ACT.app_files)a.app_files=LAST_ACT.app_files;
    if(a.seed_sql==='(kept)')a.seed_sql=LAST_ACT.seed_sql;
  }
  // The request field holds 32767 characters: send the newest turns that fit,
  // long ones shortened, so a long conversation never breaks the chat.
  function fitHistory(h){
    var out=[], used=0, budget=26000;
    for(var i=h.length-1;i>=0;i--){
      var m={role:h[i].role,text:String(h[i].text||'')};
      var cap=i===h.length-1?12000:4000; if(m.text.length>cap)m.text=m.text.slice(0,cap)+' ...';
      if(used+m.text.length>budget&&out.length)break;
      used+=m.text.length; out.unshift(m);
    }
    return out;
  }
  function pushHist(m){Array.prototype.push.call(hist,m);saveHist();return m}
  // esc() escapes markup but drops newlines, so multi-line answers used to
  // arrive as one run-on line. Keep the escaping, restore the shape.
  function fmt(s){return '<p>'+esc(s)
      .replace(/`([^`\n]+)`/g,'<code>$1</code>')
      .replace(/\n{2,}/g,'</p><p>')
      .replace(/\n/g,'<br>')+'</p>'}
  // Every sandbox hands back one thing to click. Newer stacks output `url`
  // directly; older rows are derived so existing sandboxes still get a button.
  function consolesFor(o){
    if(!o)return {};
    if(o.consoles)return o.consoles;
    var c=o.sandbox&&o.sandbox.compartment_id, rg=window.__sfRegion||'';
    if(!c)return {};
    var q='?region='+rg+'&compartmentId='+c;
    return {data_flow:'https://cloud.oracle.com/data-flow/apps'+q, data_catalog:'https://console.'+rg+'.oraclecloud.com/datacatalogexplorer',
      object_storage:'https://cloud.oracle.com/object-storage/buckets'+q, functions:'https://cloud.oracle.com/functions/applications'+q,
      queues:'https://cloud.oracle.com/queue/queues'+q, nosql:'https://cloud.oracle.com/nosql/tables'+q};
  }
  // What a sandbox has, each with the place to open it. Used by the dashboard
  // card, the chat widget and the landing page so they never disagree.
  // Deep links: the resource's own console page, not a compartment listing.
  // Everything a sandbox makes is in the sandboxes compartment, so the id is
  // all the console needs.
  function resourceUrls(o){
    var k=consolesFor(o), rg=window.__sfRegion||'us-phoenix-1', u={};
    (o.buckets||[]).forEach(function(b){u['bucket:'+b.name]=b.namespace?'https://cloud.oracle.com/object-storage/buckets/'+b.namespace+'/'+b.name+'/objects?region='+rg:k.object_storage});
    (o.dataflow_jobs||[]).forEach(function(d){u['spark:'+d.name]=d.id?'https://cloud.oracle.com/data-flow/apps/details/'+d.id+'?region='+rg:k.data_flow});
    if(o.catalog)u['catalog']=o.catalog.console_url||'https://console.'+rg+'.oraclecloud.com/datacatalogexplorer';  // Data Catalog lives on console.<region>.oraclecloud.com; older rows still carry the 404 cloud.oracle.com link, so it is not used as a fallback
    (o.queues||[]).forEach(function(q){u['queue:'+q.name]=q.id?'https://cloud.oracle.com/queue/queues/'+q.id+'?region='+rg:k.queues});
    if(o.nosql&&o.nosql.tables)o.nosql.tables.forEach(function(n){u['nosql:'+n]=(o.nosql.table_urls||{})[n]||k.nosql});
    return u;
  }
  function resourceLinks(o){
    var k=consolesFor(o), L=[], u=resourceUrls(o);
    if(o.nosql&&o.nosql.tables&&o.nosql.tables.length){ var tu=o.nosql.table_urls||{};
      o.nosql.tables.forEach(function(n){L.push(['NoSQL table '+n, tu[n]||k.nosql])}); }
    (o.buckets||[]).forEach(function(b){L.push(['Bucket '+b.name,u['bucket:'+b.name]])});
    (o.queues||[]).forEach(function(q){L.push(['Queue '+q.name,u['queue:'+q.name]])});
    (o.functions||[]).forEach(function(f){ if(f.url)L.push(['Function '+f.name,f.url]); });
    if(o.functions&&o.functions.length&&!o.functions.some(function(f){return f.url}))L.push(['Functions: '+o.functions.map(function(f){return f.name}).join(', '),k.functions]);
    (o.dataflow_jobs||[]).forEach(function(d){L.push([(d.query?'Iceberg tables: query app ':'Spark job ')+d.name,u['spark:'+d.name]])});
    // Iceberg: the query app writes its answers as CSV under query-results/ in the lake bucket
    var qa=(o.dataflow_jobs||[]).filter(function(d){return d.query})[0], lake=(o.buckets||[])[0];
    if(qa&&lake&&lake.namespace)L.push(['Iceberg query results (CSV)',u['bucket:'+lake.name]+'&prefix=query-results/']);
    if(o.catalog){ var na=(o.catalog.assets||[]).length, hv=o.catalog.harvest||{}, bad=Object.keys(hv).filter(function(k){return hv[k].state!=='SUCCEEDED'}).length;
      L.push(['Data Catalog '+(o.catalog.display_name||'')+(na?' ('+na+' bucket'+(na>1?'s':'')+' registered'+(bad?', harvest did not complete':'')+')':''),u['catalog']]); }
    if(o.logs&&o.logs.console_url)L.push(['Logs ('+(o.logs.logs||[]).length+')',o.logs.console_url]);
    if(o.rag&&!o.rag.error){ var db=(o.buckets||[]).filter(function(b){return b.name===o.rag.bucket})[0]; if(db)L.push(['Documents: drop files here',u['bucket:'+db.name]]); if(o.rag.ask_url)L.push(['Ask the documents (REST)',null]); }
    if(o.aidp)L.push(['AI Data Platform '+(o.aidp.display_name||''),o.aidp.console_url]);
    return L;
  }
  function primaryUrl(o){
    if(!o)return null;
    if(o.url)return o.url;
    if(o.app&&o.app.url)return o.app.url;
    if(o.app&&o.app.urls&&o.app.urls.length){
      var https=o.app.urls.filter(function(u){return u.indexOf('https://')===0});
      return https[0]||o.app.urls[0];
    }
    if(o.adb)return o.adb.apex_url||o.adb.sql_web_url;
    var fu=(o.functions||[]).filter(function(f){return f.url})[0]; if(fu)return fu.url;
    // a Spark-only sandbox: Open goes to its Data Flow application
    var df=(o.dataflow_jobs||[])[0]; if(df)return resourceUrls(o)['spark:'+df.name];
    return null;
  }
  // The model is asked for one JSON object but sometimes wraps it in prose or
  // a ```json fence. Take the fenced block if there is one, else the first
  // '{' that starts a valid object; null only when there is truly none.
  function modelJson(raw){
    raw=raw||''; var tries=[], f=raw.match(/```(?:json)?\s*([\s\S]*?)```/i); if(f)tries.push(f[1]);
    var end=raw.lastIndexOf('}');
    for(var i=raw.indexOf('{'); i>=0 && i<end && tries.length<40; i=raw.indexOf('{',i+1)) tries.push(raw.slice(i,end+1));
    for(var k=0;k<tries.length;k++){
      var cand=[tries[k], fixCtl(tries[k])];
      for(var q=0;q<2;q++){ try{ var o=JSON.parse(cand[q]); if(o&&typeof o==='object'&&!Array.isArray(o))return o; }catch(e){} }
    }
    return null;
  }
  // A literal line break or tab inside a JSON string (multi-line SQL, say) is
  // invalid JSON; escape control characters that sit inside strings.
  function fixCtl(t){
    var out='', inStr=false, esc=false;
    for(var i=0;i<t.length;i++){ var ch=t.charAt(i), code=t.charCodeAt(i);
      if(inStr){
        if(esc){ esc=false; }
        else if(code===92){ esc=true; }                 // backslash
        else if(code===34){ inStr=false; }              // closing quote
        else if(code===10){ out+=String.fromCharCode(92,110); continue; }   // raw line feed -> \n
        else if(code===13){ out+=String.fromCharCode(92,114); continue; }   // raw carriage return -> \r
        else if(code===9){ out+=String.fromCharCode(92,116); continue; }    // raw tab -> \t
      } else if(code===34){ inStr=true; }
      out+=ch; }
    return out;
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
     p:{enable_adb:true,seed_key:'sales',containers:[{name:'studio',image:'{R}studio:release',port:8770},{name:'mcp',image:'{R}schemagate:release',port:8765}]}, tags:['MCP','Studio','Select AI']},
    {k:'mcponly', t:'MCP endpoint only', d:'Just the MCP server over a seeded database, to point Claude or your own agent at.',
     p:{enable_adb:true,seed_key:'sales',containers:[{name:'mcp',image:'{R}schemagate:release',port:8765}]}, tags:['MCP','Autonomous DB']},
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
    {k:'airflow', t:'Apache Airflow', d:'Airflow in a container instance - your DAGs run unchanged. The admin password is generated for this sandbox and shown on its landing page. Oracle has no managed Airflow.',
     p:{containers:[{name:'airflow',image:'docker.io/apache/airflow:2.10.3',port:8080,command:['bash','-c'],args:['airflow db migrate && airflow users create --role Admin --username admin --password \"$AIRFLOW_ADMIN_PASSWORD\" --firstname Sandbox --lastname Admin --email admin@example.com; airflow scheduler & exec airflow webserver --port 8080'],env:{AIRFLOW_ADMIN_PASSWORD:'{{GENERATE_PASSWORD}}',AIRFLOW__CORE__LOAD_EXAMPLES:'False',AIRFLOW__WEBSERVER__WORKERS:'2',AIRFLOW__WEBSERVER__EXPOSE_CONFIG:'False',AIRFLOW__API__AUTH_BACKENDS:'airflow.api.auth.backend.basic_auth,airflow.api.auth.backend.session'}}]}, tags:['Airflow','DAGs','login']},
    {k:'airflowdf', t:'Airflow + Spark + bucket', d:'Airflow to orchestrate, a bucket for the data - the MWAA + Glue + S3 shape. Point a DAG task at Data Flow for Spark.',
     p:{containers:[{name:'airflow',image:'docker.io/apache/airflow:2.10.3',port:8080,command:['bash','-c'],args:['airflow db migrate && airflow users create --role Admin --username admin --password \"$AIRFLOW_ADMIN_PASSWORD\" --firstname Sandbox --lastname Admin --email admin@example.com; airflow scheduler & exec airflow webserver --port 8080'],env:{AIRFLOW_ADMIN_PASSWORD:'{{GENERATE_PASSWORD}}',AIRFLOW__CORE__LOAD_EXAMPLES:'False',AIRFLOW__WEBSERVER__WORKERS:'2',AIRFLOW__WEBSERVER__EXPOSE_CONFIG:'False',AIRFLOW__API__AUTH_BACKENDS:'airflow.api.auth.backend.basic_auth,airflow.api.auth.backend.session'}}],buckets:[{name:'data'}]}, tags:['Airflow','Object Storage','Data Flow']},
    {k:'kafka', t:'Kafka cluster only', d:'A broker and an events topic, for producing and consuming.',
     p:{enable_kafka:true}, tags:['Kafka']},
    {k:'grafana', t:'Grafana on a database', d:'Grafana wired to a fresh database with the telemetry schema loaded.',
     p:{enable_adb:true,seed_key:'iot',containers:[{name:'grafana',image:'docker.io/grafana/grafana:latest',port:3000}]}, tags:['Grafana','Autonomous DB']},
    {k:'jupyter', t:'Jupyter on a database', d:'A notebook with the sales schema already there to poke at from Python.',
     p:{enable_adb:true,seed_key:'sales',containers:[{name:'lab',image:'docker.io/jupyter/minimal-notebook:latest',port:8888}]}, tags:['Jupyter','Autonomous DB']},
    {k:'n8n', t:'n8n automation', d:'Workflow automation with a database behind it to store runs.',
     p:{enable_adb:true,containers:[{name:'n8n',image:'docker.io/n8nio/n8n:latest',port:5678}]}, tags:['n8n','Autonomous DB']},
    {k:'anyimage', t:'Your own container image', d:'Run any public image with a public HTTPS URL. Pick this, then put your image and port in the form.',
     p:{enable_app:true,app_image:'',app_port:8080,ttl_days:3}, tags:['container','your image']}
  ];
  function newId(pfx){return (pfx||'sbx')+'-'+Math.random().toString(36).slice(2,7)}
  function needsRegistry(r){return JSON.stringify(r.p.containers||[]).indexOf('{R}')>=0}
  function recipePayload(r){
    // Pass the recipe through whole. This used to rebuild each container as
    // {name, image, port} and drop seed_key and app_template, so a starter that
    // needed a start command, an environment or a template reached the worker
    // with none of them - the knowledge base starter arrived with no app at all.
    var p=r.p, cs=(p.containers||[]).map(function(c){
      var x={name:c.name, image:String(c.image).replace('{R}',OCIR), port:c.port};
      if(c.env)x.env=c.env;
      if(c.command)x.command=c.command;
      if(c.args)x.args=c.args;
      return x;
    });
    function list(v){return (v&&v.length)?v:undefined}
    return {sandbox_id:newId(r.k), action:'CREATE', ttl_days:p.ttl_days||3,
      enable_adb:!!p.enable_adb, enable_kafka:!!p.enable_kafka, enable_nosql:!!p.enable_nosql,
      enable_catalog:!!p.enable_catalog, enable_aidp:!!p.enable_aidp,
      enable_app:!!p.enable_app||cs.length>0||!!p.app_template||!!p.app_files,
      app_image:p.app_image||null, git_url:null,
      app_port:p.app_port||(cs[0]&&cs[0].port)||80, text:'one-click recipe: '+r.t,
      containers:list(cs), seed_sql:p.seed_sql||undefined, seed_key:p.seed_key||undefined,
      app_template:p.app_template||undefined,
      app_files:p.app_files?JSON.stringify(p.app_files):undefined,
      databases:list(p.databases), buckets:list(p.buckets), queues:list(p.queues),
      functions:list(p.functions), dataflow_jobs:list(p.dataflow_jobs)};
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
      + (opts.open?' <a class="sf-btn" style="padding:3px 10px;font-size:12px;text-decoration:none;margin-left:6px" href="'+esc(opts.open)+'" target="_blank" rel="noopener">Open &#8599;</a>':'')
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
      if(o.adb.sql_web_url) t += row('SQL Developer Web', o.adb.sql_web_url, {link:true, hint:'Sign in as ADMIN with the password below.'});
      if(o.adb.console_url) t += row('Database console', o.adb.console_url, {link:true});
      if(o.adb.apex_url)    t += row('APEX', o.adb.apex_url, {link:true});
      t += row('Database', o.adb.db_name || '', {});
      t += row('User', o.adb.admin_user || 'ADMIN', {});
      if(o.adb.admin_password) t += row('Password', o.adb.admin_password, {});
      if(o.adb.connect_string) t += row('Connect string', o.adb.connect_string,
        {hint:'python: oracledb.connect(user="ADMIN", password=..., dsn="'+esc(o.adb.connect_string)+'")'});
    }
    (o.databases||[]).filter(function(d){return d.name!=='primary'}).forEach(function(d){
      t += row('Database '+d.name, d.db_name, {open:d.sql_web_url||d.console_url, hint:'Open = SQL Developer Web. User <b>'+esc(d.admin_user||'ADMIN')+'</b>'+(d.admin_password?', password below.':'')});
      if(d.admin_password) t += row('  password', d.admin_password, {});
      if(d.connect_string) t += row('  connect string', d.connect_string, {});
    });
    if(o.kafka){
      if(o.kafka.public_bootstrap){
        t += row('Kafka bootstrap (public)', o.kafka.public_bootstrap, {open:o.kafka.console_url, hint:'Reachable from your laptop. SASL_SSL, SCRAM-SHA-512. Topics: '+esc((o.kafka.topics||[]).join(', '))});
        if(o.kafka.username) t += row('  username', o.kafka.username, {});
        if(o.kafka.password) t += row('  password', o.kafka.password, {});
        if(o.kafka.client_properties) t += row('  client.properties', o.kafka.client_properties, {hint:'Paste into a file and run: kafka-console-producer --bootstrap-server '+esc(o.kafka.public_bootstrap)+' --producer.config client.properties --topic '+esc((o.kafka.topics||['events'])[0])});
        if(o.kafka.bootstrap_servers) t += row('  private bootstrap', o.kafka.bootstrap_servers, {hint:'For containers inside the sandbox (KAFKA_BOOTSTRAP_SERVERS).'});
      } else {
        t += row('Kafka bootstrap', o.kafka.bootstrap_servers || '', {open:o.kafka.console_url, hint:'topics: '+esc((o.kafka.topics||[]).join(', '))+(o.kafka.auth_note?'<br>'+esc(o.kafka.auth_note):'')});
      }
    }
    var ru=resourceUrls(o);
    (o.buckets||[]).forEach(function(b){
      t += row('Bucket', b.name, {open:ru['bucket:'+b.name], hint:'Open it to upload or browse files. From a terminal: oci os object put -bn '+esc(b.name)+' --file ./x --namespace '+esc(b.namespace)});
    });
    (o.queues||[]).forEach(function(q){
      t += row('Queue', q.name, {open:ru['queue:'+q.name], hint:'Open it to send a test message; consumers read from the endpoint below.'});
      if(q.messages_endpoint) t += row('  endpoint', q.messages_endpoint, {link:true});
    });
    if(o.nosql&&o.nosql.tables) o.nosql.tables.forEach(function(n){
      var tu=(o.nosql.table_urls||{})[n];
      if(tu) t += row('NoSQL table', n, {open:tu, hint:'Open it, then Table Explorer, to add and query rows. From a terminal: oci nosql query --compartment-id '+esc(o.nosql.compartment_id||(o.sandbox&&o.sandbox.compartment_id)||'')+' --statement "select * from '+esc(n)+'"'});
      else t += row('NoSQL table', n, {hint:'oci nosql query --compartment-id '+esc((o.sandbox&&o.sandbox.compartment_id)||'')+' --statement "select * from '+esc(n)+'"'});
    });
    (o.functions||[]).forEach(function(f){
      if(f.url){ t += row('Function '+f.name, f.url, {link:true, hint:"curl -s -X POST "+esc(f.url)+" -d '{\"name\":\"world\"}'   (any method, no signing needed)"}); }
      else { t += row('Function', f.name, {hint:"oci fn function invoke --function-id "+esc(f.id||'')+" --body '{}' --file -"}); }
    });
    (o.dataflow_jobs||[]).forEach(function(d){
      t += row('Spark job', d.name, {open:ru['spark:'+d.name], hint:'Open it and press Run; each run shows its logs there. Script: '+esc(d.file_uri||'')});
    });
    if(o.catalog) t += row('Data Catalog', o.catalog.display_name || '', {open:ru['catalog'], hint:'Open it to harvest the bucket and browse the tables it finds.'});
    if(o.aidp) t += row('AI Data Platform', o.aidp.display_name || '', {open:o.aidp.console_url, hint:'Open it: workspace <b>'+esc(o.aidp.workspace||'')+'</b>. Create a compute cluster inside (smallest size), attach the sandbox bucket, run the same Spark code.'+(o.aidp.web_socket_endpoint?'<br>endpoint '+esc(o.aidp.web_socket_endpoint):'')});
    if(o.low_code&&o.low_code.rest_base) t += row('REST (ORDS)', o.low_code.rest_base,
      {hint:'authenticated as ADMIN with the password above'});
    t += '</table>';
    h += t;
    var rl=resourceLinks(o).filter(function(x){return x[1]});
    if(rl.length) h += '<div style="margin:14px 0 0"><div style="font-size:12px;color:#6b7280;margin-bottom:6px">Open in the OCI console</div>'
      + rl.map(function(x){return '<a class="sf-btn sec" style="display:inline-block;margin:0 6px 6px 0;padding:6px 12px;font-size:12px;text-decoration:none" href="'+esc(x[1])+'" target="_blank" rel="noopener">'+esc(x[0])+' &#8599;</a>'}).join('')
      + '</div>';
    if(o.warnings&&o.warnings.length) h += '<p class="sf-err" style="margin-top:10px">'
      + o.warnings.map(esc).join('<br>') + '</p>';
    return h;
  }
  function loadHistory(){
    call('history',{}).then(function(items){
      items=items||[];
      var el=$('#sf-history'); if(!el)return;
      if(!items.length){ el.innerHTML='<p style="color:#6b7280;font-size:13px">Nothing destroyed yet.</p>'; return; }
      el.innerHTML='<table style="width:100%;border-collapse:collapse;font-size:13.5px">'
        +'<tr style="text-align:left;color:#6b7280;font-size:12px"><th style="padding:6px 0">Sandbox</th><th>Built</th><th>Destroyed</th><th>Why</th></tr>'
        +items.map(function(x){
          return '<tr style="border-top:1px solid #eef2f7"><td style="padding:7px 12px 7px 0"><b>'+esc(x.sandbox_id)+'</b></td>'
            +'<td style="padding-right:12px">'+esc(x.built||'-')+'</td>'
            +'<td style="padding-right:12px">'+esc(x.destroyed||'-')+'</td>'
            +'<td style="color:#6b7280">'+esc(x.reason||'')+'</td></tr>';
        }).join('')+'</table>';
    });
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
  document.addEventListener('change', function(e){
    var sel = e.target.closest && e.target.closest('select[data-ttl]');
    if(!sel || !sel.value) return;
    var sid=sel.getAttribute('data-ttl'), days=+sel.value; sel.disabled=true;
    call('retry',{sandbox_id:sid, ttl_days:days}).then(function(s){
      addMsg&&$('#sf-msgs')&&addMsg('ai', s&&s.err ? '<span class="sf-err">'+esc(s.err)+'</span>' : 'Lifetime of <b>'+esc(sid)+'</b> set to '+days+' day'+(days===1?'':'s')+' from now (request #'+esc(s.id)+'). The expiry updates when it finishes.');
      refresh(); });
  });
  document.addEventListener('click', function(e){
    var tb = e.target.closest && e.target.closest('[data-tab]');
    if(tb){ window.__sfTab = tb.getAttribute('data-tab'); refresh(); return; }
    var op = e.target.closest && e.target.closest('[data-open]');
    if(op){ showLanding(op.getAttribute('data-open')); return; }
    // Destroy from the card: the first click asks, the second (within 6 s) destroys
    var ds = e.target.closest && e.target.closest('[data-destroy]');
    if(ds){ var sid=ds.getAttribute('data-destroy');
      if(!ds.classList.contains('armed')){ ds.classList.add('armed'); ds.textContent='Confirm destroy '+sid;
        ds.style.background='#b91c1c'; ds.style.color='#fff';
        setTimeout(function(){ if(ds.classList.contains('armed')&&!ds.disabled){ ds.classList.remove('armed'); ds.textContent='Destroy'; ds.style.background=''; ds.style.color=''; } },6000); return; }
      ds.disabled=true; ds.textContent='Destroying...';
      call('submit',{sandbox_id:sid, action:'DESTROY', ttl_days:1, enable_adb:false, enable_kafka:false, enable_app:false}).then(function(s){
        ds.textContent = s&&s.err ? s.err : 'Destroy queued (request #'+(s&&s.id)+')'; refresh(); }); return; }
    var rt = e.target.closest && e.target.closest('[data-retry]');
    // (lifetime changes are handled on 'change', below)
    if(rt){ rt.disabled=true; rt.textContent='Queueing...';
      call('retry',{sandbox_id:rt.getAttribute('data-retry')}).then(function(s){
        rt.textContent = s&&s.err ? s.err : 'Queued as request #'+(s&&s.id); refresh(); }); return; }
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
        +'<select id="sf-f-ttl"><option value="1">1 day</option><option value="2">2 days</option><option value="3" selected>3 days</option><option value="5">5 days</option><option value="7">7 days</option><option value="14">14 days</option><option value="21">21 days</option><option value="30">30 days (max)</option></select></div>'
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
        if(extra==='mcp'||extra==='both')cs.push({name:'mcp',image:'{R}schemagate:release',port:8765});
        if(extra==='both')cs.unshift({name:'studio',image:'{R}studio:release',port:8770});
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
      +'<select id="sf-f-ttl"><option value="1">1 day</option><option value="2">2 days</option><option value="3" selected>3 days</option><option value="5">5 days</option><option value="7">7 days</option><option value="14">14 days</option><option value="21">21 days</option><option value="30">30 days (max)</option></select></div>'
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
    if(a.enable_adb)L.push('<li><b>Autonomous Database</b> &mdash; '+esc(a.adb_tier||'paid, 2 ECPU')+', Oracle 23ai, private subnet. Select AI (plain-English queries) and AI cataloguing are switched on for you. Every table is also published as a REST endpoint through ORDS, and an APEX workspace is waiting if you want to click a low-code app together. You get SQL Developer Web and APEX URLs.</li>');
    if(a.enable_nosql)L.push('<li><b>OCI NoSQL</b> &mdash; serverless JSON tables with on-demand capacity. You get the table names and the compartment; no cluster to size and nothing running when idle.</li>');
    if(a.enable_kafka)L.push('<li><b>Kafka</b> &mdash; Streaming with Apache Kafka, 1 broker, 50 GB, topic <code>events</code>, a public bootstrap endpoint with a SASL/SCRAM superuser (username + password on the card), and a private one for containers in the sandbox.</li>');
    var cs=(a.containers&&a.containers.length)?a.containers:((a.enable_app||a.git_url||a.app_image)?[{name:'web',image:a.git_url||a.app_image||'nginx:alpine',port:a.app_port||80}]:[]);
    if(cs.length)L.push('<li><b>'+(cs.length>1?cs.length+' containers in one instance':'1 container')+'</b> on CI.Standard.A1.Flex (Arm)'
      +(cs.length>1?' &mdash; they share a host and reach each other on localhost':'')+', behind a public HTTPS URL: '+cs.map(function(c){return '<code>'+esc(c.name||'web')+'</code> &rarr; '+esc(c.image)+':'+esc(c.port||80)}).join(', ')+'.</li>');
    if(a.app_files){var n=Object.keys(typeof a.app_files==='string'?JSON.parse(a.app_files):a.app_files).length;
      L.push('<li><b>Your app, built here</b> &mdash; '+n+' source files are sent with the request, built into an image in OCI and deployed. Nothing to push to a registry.</li>')}
    if(a.enable_rag)L.push('<li><b>Documents and RAG</b> &mdash; a docs bucket the database watches: every file you drop in is chunked by Oracle Select AI and embedded inside the database by Oracle&rsquo;s all-MiniLM ONNX model (vector index, refreshed every 5 minutes); images are first described as text by OCI Generative AI vision. Ask with <code>select ai narrate</code> or <code>POST .../rag/ask</code>.</li>');
    (a.functions||[]).forEach(function(f){
      var src=f.files?Object.keys(f.files).length+' source file'+(Object.keys(f.files).length>1?'s':'')+', built into an image in OCI':(f.git_url?'built in OCI from '+esc(f.git_url):(f.image?esc(f.image):'the starter function, built in OCI'));
      L.push('<li><b>Function '+esc(f.name)+'</b> &mdash; '+src+'; serverless, billed per call, free when idle'+(f.schedule?'. Runs on a schedule (<code>'+esc(f.schedule)+'</code>) through OCI Resource Scheduler':'')+'. You get a plain HTTPS URL for it.</li>')});
    (a.dataflow_jobs||[]).forEach(function(d){ if(d.iceberg)L.push('<li><b>Iceberg tables</b> &mdash; job '+esc(d.name)+' writes Iceberg to Object Storage (catalog <code>lake</code>, under iceberg/ in the bucket).</li>'); });
    (a.buckets||[]).forEach(function(b){L.push('<li><b>Bucket '+esc(b.name)+'</b> &mdash; Object Storage, '+(b.public?'public read':'private')+'.</li>')});
    (a.queues||[]).forEach(function(q){L.push('<li><b>Queue '+esc(q.name)+'</b> &mdash; OCI Queue, serverless point-to-point messaging (the SQS counterpart).</li>')});
    (a.dataflow_jobs||[]).forEach(function(d){L.push('<li><b>Spark job '+esc(d.name)+'</b> &mdash; OCI Data Flow, managed Spark billed per run (the Glue counterpart).</li>')});
    if(a.enable_aidp)L.push('<li><b>Oracle AI Data Platform</b> &mdash; one lakehouse service (managed Spark, Iceberg catalog, notebooks, AI) with a default workspace; size and start compute inside it. Destroyed with the sandbox.</li>');
    if(a.enable_catalog)L.push('<li><b>Data Catalog</b> &mdash; the metastore Spark resolves table names against (the Glue Data Catalog counterpart).</li>');
    (a.databases||[]).forEach(function(d){L.push('<li><b>Extra database '+esc(d.name)+'</b> &mdash; its own Autonomous Database with its own ADMIN password.</li>')});
    if(a.seed_sql)L.push('<li><b>Sample data</b> &mdash; '+esc(String(a.seed_sql).split(';').filter(function(x){return x.trim()}).length)+' SQL statements loaded into the new database before anything starts.</li>');
    L.push('<li>Everything is tagged with your sandbox id and <b>auto-destroyed after '+esc(a.ttl_days||3)+' day'+((a.ttl_days||3)==1?'':'s')+'</b>.</li>');
    return '<h4 style="margin:10px 0 4px">What this builds in OCI</h4><ul style="margin:0;padding-left:18px">'+L.join('')+'</ul>';
  }
  function nz(x){return (x&&x.length)?x:undefined}

  // ---- Code the user hands to the chat ------------------------------------
  // A GitHub URL in the message, or a folder picked with the clip button, is
  // read in the browser: text files only, small, and never sent anywhere but
  // to the factory's own model as part of the message. The model answers with
  // `workload`, and the page turns that into the request (bucket, Data Flow
  // job per Spark file, catalog, Airflow image with the DAGs baked in).
  var CODE=null;   // {source, files:{path:content}}
  var TEXT_EXT=/\.(py|sql|md|txt|ya?ml|json|toml|cfg|ini|sh|csv|tsv|env\.example|dockerfile)$|(^|\/)(Dockerfile|requirements\.txt|Makefile)$/i;
  function ghParse(url){
    var m=String(url).match(/^https?:\/\/github\.com\/([^\/\s]+)\/([^\/\s#?]+)(?:\/tree\/([^\/\s]+)(?:\/([^\s#?]*))?)?/);
    if(!m)return null;
    return {owner:m[1], repo:m[2].replace(/\.git$/,''), ref:m[3]||null, dir:(m[4]||'').replace(/\/+$/,'')};
  }
  function fetchGitHub(url){
    var g=ghParse(url); if(!g)return Promise.reject(new Error('not a GitHub URL'));
    var api='https://api.github.com/repos/'+g.owner+'/'+g.repo;
    var refP=g.ref?Promise.resolve(g.ref):fetch(api).then(function(r){if(!r.ok)throw new Error('GitHub: '+r.status);return r.json()}).then(function(x){return x.default_branch});
    return refP.then(function(ref){
      return fetch(api+'/git/trees/'+encodeURIComponent(ref)+'?recursive=1').then(function(r){if(!r.ok)throw new Error('GitHub tree: '+r.status);return r.json()}).then(function(t){
        var paths=(t.tree||[]).filter(function(e){return e.type==='blob'&&TEXT_EXT.test(e.path)&&(!g.dir||e.path.indexOf(g.dir+'/')===0)&&e.size<200000}).slice(0,40);
        var files={};
        return Promise.all(paths.map(function(e){
          return fetch('https://raw.githubusercontent.com/'+g.owner+'/'+g.repo+'/'+ref+'/'+e.path).then(function(r){return r.ok?r.text():''}).then(function(txt){
            var rel=g.dir?e.path.slice(g.dir.length+1):e.path; if(txt)files[rel]=txt; });
        })).then(function(){ return {source:url, files:files}; });
      });
    });
  }
  function readFolder(fileList){
    var files={}, jobs=[], root=null;
    Array.prototype.forEach.call(fileList,function(f){
      var rel=f.webkitRelativePath||f.name; if(!TEXT_EXT.test(rel)||f.size>200000)return;
      if(rel.indexOf('/')>0){ if(root===null)root=rel.split('/')[0]; if(rel.indexOf(root+'/')===0)rel=rel.slice(root.length+1); }
      if(/(^|\/)(node_modules|\.git|__pycache__|\.venv|venv)\//.test(rel))return;
      jobs.push(f.text().then(function(t){files[rel]=t}));
    });
    return Promise.all(jobs).then(function(){ return {source:'folder '+(root||''), files:files}; });
  }
  function codeDigest(code){
    var names=Object.keys(code.files), out='', budget=24000;
    var rank=function(n){return /dag|airflow/i.test(n)?0:/spark|etl|glue|job/i.test(n)?1:/requirements|Dockerfile/i.test(n)?2:/\.sql$/i.test(n)?3:/readme/i.test(n)?4:5};
    names.sort(function(a,b){return rank(a)-rank(b)||a.localeCompare(b)});
    out+='\n\nATTACHED CODE ('+names.length+' files from '+code.source+'):\nfiles: '+names.join(', ')+'\n';
    names.forEach(function(n){ if(budget<=0)return; var c=code.files[n]; if(!/\.(py|sql|txt|ya?ml|toml|cfg|md)$|Dockerfile|requirements/i.test(n))return;
      var take=Math.min(c.length, Math.min(8000,budget)); out+='\n=== '+n+' ===\n'+c.slice(0,take)+(take<c.length?'\n... ('+(c.length-take)+' more chars)':'')+'\n'; budget-=take; });
    return out;
  }
  function showCodeChip(){
    var bar=$('#sf-code-chip'); if(!bar){bar=document.createElement('div'); bar.id='sf-code-chip'; bar.style.cssText='margin:6px 0 0;font-size:12px;color:#374151'; var c=$('#sf-chat-in').closest('.sf-composer'); (c&&c.parentNode).insertBefore(bar,c.nextSibling);}
    if(!CODE){bar.innerHTML='';return}
    var n=Object.keys(CODE.files).length;
    bar.innerHTML='<span class="sf-chip">&#128206; '+n+' file'+(n===1?'':'s')+' from '+esc(CODE.source)+'</span> <button type="button" class="sf-btn sec" style="padding:2px 8px;font-size:11px" id="sf-code-x">remove</button>';
    $('#sf-code-x').onclick=function(){CODE=null;showCodeChip()};
  }
  function attachFolder(){
    var inp=document.createElement('input'); inp.type='file'; inp.multiple=true; inp.setAttribute('webkitdirectory','');
    inp.onchange=function(){ readFolder(inp.files).then(function(c){CODE=c;showCodeChip(); if(!Object.keys(c.files).length)addMsg('ai','<span class="sf-err">No readable code files in that folder.</span>');}); };
    inp.click();
  }
  // The files decide what gets built, not the model's wording. A folder with
  // a Dockerfile is an app to deploy; a folder of DAGs and Spark jobs is a
  // pipeline. A model once answered "deploy" for the telemetry pipeline (no
  // Dockerfile), and the build died in kaniko: read the files and correct it.
  function hasDockerfile(files){ return Object.keys(files).some(function(p){return /^(Dockerfile|[^\/]*\.dockerfile)$/i.test(p)}); }
  function inferWorkload(files){
    var dags=[], spark=[], reqs=[], oracle=false;
    Object.keys(files).forEach(function(p){
      var c=files[p]||'';
      if(/(^|\/)requirements[^\/]*\.txt$/i.test(p))reqs.push(p);
      if(!/\.py$/i.test(p))return;
      if(/^\s*(from|import)\s+airflow\b/m.test(c))dags.push(p);
      else if(/^\s*(from|import)\s+pyspark\b/m.test(c))spark.push(p);
      if(/\boracledb\b|ADB_CONNECT_STRING|DBMS_CLOUD/.test(c))oracle=true;
    });
    return (dags.length||spark.length)?{dags:dags,spark:spark,requirements:reqs,bucket:'data',catalog:true,oracle:oracle,schedule:'@once'}:null;
  }
  // Lambda-style (event, context) or Fn-style (ctx, data) handlers: a function.
  function isFunctionCode(files){ return Object.keys(files).some(function(p){return /\.py$/i.test(p)&&/^def \w+\(\s*event\s*,\s*context\s*\)|^def handler\(\s*ctx\b/m.test(files[p]||'')}); }
  function functionFiles(files){ var o={}; Object.keys(files).forEach(function(p){ if(/\.(py|txt|json|ya?ml|cfg|toml)$|(^|\/)Dockerfile$/i.test(p))o[p]=files[p]; }); return o; }
  // Environment variables the user's code reads: names found in the attached
  // code (.env / .env.example, os.environ, os.getenv, process.env, config
  // files) plus any the model listed. The values are typed on the page and go
  // straight into the request; they never reach the model or the chat history.
  var FACTORY_ENV=/^(SANDBOX_|ADB_|KAFKA_|NOSQL_|OCI_REGION$|CHAT_MODEL$|DATA_BUCKET$|BUCKET_|OBJECT_NAMESPACE$|DATAFLOW_APP_NAME$|DATA_CATALOG_NAME$|PIPELINE_SCHEDULE$|AIRFLOW|PATH$|HOME$|PORT$|PYTHON|LANG$)/;
  function envNamesFrom(files, extra){
    var names={};
    Object.keys(files||{}).forEach(function(p){
      var c=files[p]||'', base=p.split('/').pop();
      if(/^\.env(\..*)?$/.test(base)||/\.(env|ini|cfg|toml|properties)$/i.test(base)){
        c.split('\n').forEach(function(l){var m=l.match(/^\s*(?:export\s+)?([A-Z][A-Z0-9_]{1,127})\s*=/); if(m)names[m[1]]=1;});
      }
      var re=/(?:os\.environ(?:\.get)?\s*[\[(]\s*|os\.getenv\s*\(\s*|process\.env\.|System\.getenv\s*\(\s*)['"]?([A-Z][A-Z0-9_]{1,127})['"]?/g, m;
      while((m=re.exec(c)))names[m[1]]=1;
    });
    (extra||[]).forEach(function(n){ if(/^[A-Z][A-Z0-9_]{1,127}$/.test(n))names[n]=1; });
    return Object.keys(names).filter(function(n){return !FACTORY_ENV.test(n)}).sort();
  }
  // An editor of KEY=value lines under a proposal; prefilled with the names found.
  function envEditorHtml(names, open){
    return '<details class="sf-envbox"'+(open?' open':'')+' style="margin:8px 0"><summary style="cursor:pointer;font-size:13px">Environment variables'+(names.length?' <b>('+names.length+' needed)</b>':' (optional)')+'</summary>'
      +'<div style="font-size:12px;color:#6b7280;margin:4px 0">'+(names.length?'Your code reads these. Fill in the values, one per line; leave a line empty if it is not needed.':'KEY=value, one per line. Injected into every container, function and Spark job of this sandbox.')
      +' Values stay in this request and are never sent to the AI.</div>'
      +'<textarea class="sf-env" rows="'+Math.min(8,Math.max(3,names.length+1))+'" style="width:100%;font-family:monospace;font-size:12px" spellcheck="false" placeholder="API_KEY=...">'+esc(names.map(function(n){return n+'='}).join('\n'))+'</textarea></details>';
  }
  function envFrom(container){
    var ta=container&&container.querySelector('.sf-env'); if(!ta)return undefined;
    var out={}, any=false;
    ta.value.split('\n').forEach(function(l){ var m=l.match(/^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]{0,127})\s*=\s*(.*?)\s*$/); if(!m)return;
      var v=m[2]; if(/^(["']).*\1$/.test(v))v=v.slice(1,-1); if(v!==''){out[m[1]]=v; any=true;} });
    return any?out:undefined;
  }
  function correctAction(a){
    if(!CODE||a.type==='destroy'||a.type==='retry')return null;
    var files=CODE.files||{};
    // functions the model proposed without code of their own get the attached code
    if(a.functions&&a.functions.length&&isFunctionCode(files)){
      var fn=a.functions.filter(function(f){return !f.files&&!f.git_url})[0];
      if(fn){ fn.files=functionFiles(files); delete fn.image; }
      // The model sometimes answers with its own rewrite of the attached handler. The user's
      // code is the one that ships: any function file that also exists in the attachment is
      // replaced by the attached original, byte for byte.
      var att=functionFiles(files);
      a.functions.forEach(function(f){ if(!f.files)return; Object.keys(f.files).forEach(function(n){ var base=n.split('/').pop();
        Object.keys(att).forEach(function(m){ if(m.split('/').pop()===base && att[m]!==f.files[n]) f.files[n]=att[m]; }); }); });
    }
    if(a.workload||hasDockerfile(files))return null;
    if(a.type==='deploy'||a.git_url){
      var w=inferWorkload(files);
      if(w){ a.workload=w; a.type='create'; a.git_url=null; return null; }
      if(isFunctionCode(files)){
        a.type='create'; a.git_url=null; a.enable_app=false; a.app_image=null;
        a.functions=[{name:(a.sandbox_id||'fn').replace(/[^a-z0-9-]/g,'-').slice(0,20), files:functionFiles(files)}];
        return null;
      }
      return 'This code has no Dockerfile, no Airflow DAG or Spark job, and no function handler, so there is nothing to build from it. Add a Dockerfile to deploy it as an app.';
    }
    return null;
  }
  // Expand the model's `workload` into the pieces of a request.
  function expandWorkload(a){
    var w=a.workload; if(!w||!CODE)return;
    // A workload is built from the files generated below (app_files), never by
    // cloning the repository: a pipeline folder has no Dockerfile to build.
    a.type='create'; a.git_url=null;
    // ... and it is the whole app: an extra instance the model may also have
    // listed (a second Airflow, say) would be built and billed twice.
    a.app_instances=[];
    var files=CODE.files, id=a.sandbox_id;
    // The files decide, not the model's spelling of their paths: a path is
    // matched exactly or by its file name, and a kind the model left out or
    // misnamed is found in the files themselves (a pipeline once shipped
    // without its Spark job because the model wrote gold_etl.py for spark/gold_etl.py).
    var byName={}; Object.keys(files).forEach(function(p){byName[p.split('/').pop()]=p});
    var resolve=function(list){ var out=[]; (list||[]).forEach(function(p){ var q=files[p]?p:byName[String(p).split('/').pop()]; if(q&&out.indexOf(q)<0)out.push(q); }); return out; };
    var inf=inferWorkload(files)||{};
    var dags=resolve(w.dags); if(!dags.length)dags=inf.dags||[];
    var spark=resolve(w.spark); if(!spark.length)spark=inf.spark||[];
    var reqs=resolve(w.requirements); if(!reqs.length)reqs=inf.requirements||[];
    if(inf.oracle&&w.oracle==null)w.oracle=true;
    var bucket=w.bucket||'data';
    a.buckets=(a.buckets||[]); if(!a.buckets.some(function(b){return b.name===bucket}))a.buckets.push({name:bucket});
    a.dataflow_jobs=(a.dataflow_jobs||[]).concat(spark.map(function(p){
      var name=p.split('/').pop().replace(/\.py$/,'').replace(/[^a-z0-9]+/gi,'-').toLowerCase();
      return {name:name, script:files[p], language:'PYTHON'}; }));
    if(w.catalog!==false)a.enable_catalog=true;
    if(w.oracle){a.enable_adb=true; a.adb_tier=a.adb_tier||'paid';}
    if(dags.length){
      var extra=reqs.map(function(p){return files[p]}).join('\n').split('\n').map(function(l){return l.trim()}).filter(function(l){return l&&l[0]!=='#'&&!/^apache-airflow\b/.test(l)});
      // The build context is flat (a config-file volume), so DAG files sit
      // beside the Dockerfile and are copied one by one into dags/.
      var names=dags.map(function(p){return p.split('/').pop()});
      var app={'Dockerfile':'FROM docker.io/apache/airflow:2.10.3\nUSER airflow\nRUN pip install --no-cache-dir oci oracledb'+(extra.length?' '+extra.map(function(x){return JSON.stringify(x)}).join(' '):'')+'\n'+names.map(function(n){return 'COPY '+n+' /opt/airflow/dags/'+n+'\n'}).join('')};
      dags.forEach(function(p){ app[p.split('/').pop()]=files[p]; });
      a.app_files=app;
      var jobName=(a.dataflow_jobs[0]||{}).name||'gold-etl';
      a.containers=[{name:'airflow',image:'built',port:8080,command:['bash','-c'],
        args:['airflow db migrate && airflow users create --role Admin --username admin --password "$AIRFLOW_ADMIN_PASSWORD" --firstname Sandbox --lastname Admin --email admin@example.com; airflow scheduler & exec airflow webserver --port 8080'],
        env:{AIRFLOW_ADMIN_PASSWORD:'{{GENERATE_PASSWORD}}',AIRFLOW__CORE__LOAD_EXAMPLES:'False',AIRFLOW__WEBSERVER__WORKERS:'2',AIRFLOW__WEBSERVER__EXPOSE_CONFIG:'False',
             AIRFLOW__API__AUTH_BACKENDS:'airflow.api.auth.backend.basic_auth,airflow.api.auth.backend.session',
             DATA_BUCKET:'sbx-'+id+'-'+bucket, DATAFLOW_APP_NAME:'sbx-'+id+'-'+jobName, DATA_CATALOG_NAME:'sbx-'+id+'-catalog',
             PIPELINE_SCHEDULE:(w.schedule||'@once')}}];
      a.enable_app=true; a.app_port=8080;
    }
  }

  // AI answers come back as widgets, not bullet lists: the sandboxes a reply is
  // about render as live cards from the dashboard's own data, and a cost
  // estimate renders as a table. The model names them (sandboxes, cost); when
  // it forgets, any sandbox id that appears in the text still gets its card.
  function sbxCard(r){
    var o=null; try{o=r.outputs?JSON.parse(r.outputs):null}catch(e){}
    var parts=[];
    if(o){
      if(o.adb)parts.push('Autonomous DB');
      var xdb=(o.databases||[]).filter(function(d){return d.name!=='primary'}).length; if(xdb)parts.push(xdb+' extra DB');
      if(o.nosql)parts.push('NoSQL');
      if(o.kafka)parts.push('Kafka');
      if(o.app)parts.push((o.app.containers&&o.app.containers.length>1)?o.app.containers.length+' containers':'App');
      (o.logins||[]).forEach(function(l){parts.push(l.service)});
      ((o.buckets||[]).length)&&parts.push(o.buckets.length+' bucket'+(o.buckets.length>1?'s':''));
      ((o.queues||[]).length)&&parts.push('Queue');
      ((o.functions||[]).length)&&parts.push('Functions');
      ((o.dataflow_jobs||[]).length)&&parts.push('Spark');
      if(o.catalog)parts.push('Data Catalog');
      if(o.aidp)parts.push('AI Data Platform');
      if(o.logs)parts.push('Logs');
      if(o.rag&&!o.rag.error)parts.push('RAG');
    }
    var exp=o&&o.sandbox&&o.sandbox.expires, pu=primaryUrl(o), done=r.status==='DONE'&&r.action!=='DESTROY';
    return '<div class="sf-w-card"><div class="sf-w-hd"><b>'+esc(r.sandbox_id)+'</b><span class="sf-badge '+esc(r.status)+'">'+esc(r.status)+'</span></div>'
      +(exp?'<div class="sf-w-meta">expires '+esc(exp)+'</div>':'')
      +(parts.length?'<div class="sf-w-parts">'+parts.map(function(p){return '<span>'+esc(p)+'</span>'}).join('')+'</div>':'')
      +'<div class="sf-w-acts">'+(done&&pu?'<a class="sf-btn" href="'+esc(pu)+'" target="_blank" rel="noopener">Open</a>':(done&&o?'<button type="button" class="sf-btn" data-open="'+esc(r.sandbox_id)+'">Open</button>':''))
      +(done&&o?'<button type="button" class="sf-btn sec" data-open="'+esc(r.sandbox_id)+'">Details &amp; passwords</button>':'')
      +((done||r.status==='FAILED')&&r.action!=='DESTROY'?'<button type="button" class="sf-btn sec" data-destroy="'+esc(r.sandbox_id)+'">Destroy</button>':'')
      +(r.status==='FAILED'&&r.error?'<span class="sf-err">'+esc(String(r.error).slice(0,140))+'</span>':'')+'</div></div>';
  }
  // What a proposed build costs, from the live price list and the sizes the
  // sandbox stack really uses (paid database 2 ECPU + 20 GB, app container
  // A1 1 OCPU / 4 GB, Kafka 1 broker / 1 OCPU). Services without a published
  // hourly rate are listed as per use, never given a made-up number.
  function costFor(a){
    var P=window.__sfPrices||{}, H=744, items=[], total=0, perUse=[];   // 744 h: Oracle's own estimator month
    if(!P.adb_ecpu&&!P.e4_ocpu)return null;
    // Arm A1 is published only as a free-tier row: price it at the E4 rate and say so
    var armEst=!P.a1_ocpu, cpu=P.a1_ocpu||P.e4_ocpu||0, mem=P.a1_memory||P.e4_memory||0;
    var add=function(name,detail,m){items.push({name:name,detail:detail,monthly_usd:m==null?null:Math.round(m*100)/100}); if(m!=null)total+=m;};
    var w=a.workload||null;
    var dbs=((a.enable_adb||a.enable_rag||(w&&w.oracle))?1:0)+((a.databases||[]).length);
    if(dbs&&P.adb_ecpu){ var m=2*P.adb_ecpu*H+20*(P.adb_storage||0);
      add('Autonomous Database'+(dbs>1?' x'+dbs:''),'Transaction Processing, 2 ECPU + 20 GB, license included, per ECPU-hour while it exists',m*dbs); }
    var apps=((a.enable_app||a.git_url||a.app_image||a.app_template||(a.containers&&a.containers.length)||w)?1:0)+((a.app_instances||[]).length);
    if(apps&&cpu){ var c=(cpu+4*mem)*H;
      add('Container instance'+(apps>1?' x'+apps:''),'1 OCPU / 4 GB, per hour while it runs'+(armEst?' (Arm A1 has no public rate; estimated at the E4 rate)':' (Arm A1 rate; the Always Free allowance is not assumed)'),c*apps); }
    if(a.enable_kafka&&P.kafka_ocpu) add('Kafka cluster','1 broker, 1 OCPU, 50 GB, billed per OCPU-hour',P.kafka_ocpu*H);
    // Per-use services, priced from the same list at a stated volume. Tiered
    // SKUs carry a monthly free allowance (key_free, in the SKU's own unit),
    // which is per tenancy, so the note says it is shared with anything else.
    var over=function(key,units){var f=P[key+'_free']||0; return Math.max(0,units-f)*(P[key]||0)};
    var usd=function(v){return v<0.01&&v>0?'$'+v.toFixed(4):'$'+v.toFixed(2)};
    if(a.enable_nosql&&P.nosql_read){ var nt=Math.max(1,(a.nosql_tables||[]).length);
      add('NoSQL'+(nt>1?' x'+nt:''),'provisioned 5 read + 5 write units + 1 GB per table',nt*(5*P.nosql_read+5*(P.nosql_write||0)+(P.nosql_storage||0))); }
    var jobs=(a.dataflow_jobs||[]).length||(w&&(w.spark||[]).length)||0;
    if(jobs&&P.e4_ocpu){
      // runs a month: the pipeline's schedule, or the schedule of a function that starts the job
      var fsch=((a.functions||[]).filter(function(f){return f.schedule})[0]||{}).schedule;
      var sch=String((w&&w.schedule)||fsch||'@once'), runs=1, mm=sch.match(/^\*\/(\d+)\s/);
      if(mm)runs=Math.round(H*60/Number(mm[1])); else if(sch==='@hourly')runs=H; else if(sch==='@daily'||/^\d+\s+\d+\s+\*\s+\*\s+\*$/.test(sch))runs=31;
      var perRun=2*(1*P.e4_ocpu+16*(P.e4_memory||0))*(10/60);
      add('Data Flow'+(jobs>1?' x'+jobs:''),'Spark driver + 1 executor, E4 1 OCPU / 16 GB each, about 10 min a run ('+usd(perRun)+'), '+(runs===1?'one run':runs+' runs a month')+'; nothing between runs',jobs*runs*perRun); }
    var fns=(a.functions||[]).length;
    if(fns&&P.fn_calls){ var calls=1, gbs=1e6*0.25*1/1e4;   // 1M calls, 256 MB, 1 s each
      add('Functions'+(fns>1?' x'+fns:''),'at 1M calls a month, 256 MB, 1 s each; the first '+(P.fn_calls_free||0)+'M calls and '+((P.fn_time_free||0)*1e4).toLocaleString()+' GB-s a month are free, then $'+P.fn_calls+' per 1M calls + $'+P.fn_time+' per 10,000 GB-s',fns*(over('fn_calls',calls)+over('fn_time',gbs))); }
    if((a.queues||[]).length&&P.queue_req)
      add('Queue','at 1M requests a month; the first '+(P.queue_req_free||0)+'M are free, then $'+P.queue_req+' per 1M',over('queue_req',1));
    if(((a.buckets||[]).length||w||jobs)&&P.object_gb)
      add('Object Storage','at 10 GB stored; the first '+(P.object_gb_free||0)+' GB are free, then $'+P.object_gb+' per GB-month',over('object_gb',10));
    if((apps||fns)&&P.gateway_calls)
      add('API Gateway','HTTPS in front of the app and functions, at 100,000 calls a month; $'+P.gateway_calls+' per 1M',0.1*P.gateway_calls);
    if(a.enable_catalog||(w&&w.catalog)) perUse.push('Data Catalog (not in Oracle’s price list)');
    if(a.enable_aidp) perUse.push('AI Data Platform (per its own compute)');
    if(a.enable_rag) perUse.push('Generative AI vision for images and the answers (per token); embeddings run inside the database at no extra cost');
    perUse.forEach(function(n){add(n.split(' (')[0],(n.match(/\((.*)\)/)||[])[1]||'per use',null);});
    if(!items.length)return null;
    var days=Number(a.ttl_days)||3, life=total*days*24/H;
    return {items:items,total_usd:Math.round(total*100)/100,
      note:'From Oracle’s published price list'+(window.__sfPricesDate?', fetched '+window.__sfPricesDate:'')+'. For this sandbox’s '+days+'-day lifetime: $'+life.toFixed(2)+'. Per-use rows are at the volume stated; free allowances are per tenancy, shared with anything else it runs. Deleted when the lifetime ends.'};
  }
  function widgetsHtml(j,reply,asked){
    var rows=window.__sfRows||[], h='';
    var all=/(running|what do i have|my sandbox|sandboxes|list|show|resources|active|urls?|links?)/i.test(asked||'');
    var ids=all?rows.map(function(r){return r.sandbox_id}):(j&&Array.isArray(j.sandboxes))?j.sandboxes:rows.map(function(r){return r.sandbox_id}).filter(function(id){
      return new RegExp('(^|[^a-z0-9-])'+id.replace(/[-]/g,'\\-')+'([^a-z0-9-]|$)','i').test(reply||'')});
    var picked=rows.filter(function(r){return ids.indexOf(r.sandbox_id)>=0});
    if(picked.length)h+='<div class="sf-w-grid">'+picked.map(sbxCard).join('')+'</div>';
    var c=j&&j.cost;
    if(c&&c.items&&c.items.length){
      var money=function(v){if(v==null||v==='')return '';var n=Number(v);return isFinite(n)?'$'+n.toFixed(2):esc(v)};
      // an item with no price is a heading: the model uses them to separate options
      h+='<div class="sf-w-cost"><table><thead><tr><th>Item</th><th>How it is billed</th><th>Per month</th></tr></thead><tbody>'
        +c.items.map(function(i){var hd=(i.monthly_usd==null&&!i.detail); return hd?'<tr class="hd"><td colspan="3">'+esc(i.name||i.item||'')+'</td></tr>':'<tr><td>'+esc(i.name||i.item||'')+'</td><td>'+esc(i.detail||'')+'</td><td class="n">'+money(i.monthly_usd)+'</td></tr>'}).join('')
        +'</tbody>'+(c.total_usd!=null?'<tfoot><tr><td colspan="2">Total</td><td class="n">'+money(c.total_usd)+'</td></tr></tfoot>':'')+'</table>'
        +(c.note?'<div class="sf-w-meta">'+esc(c.note)+'</div>':'')+'</div>';
    }
    return h;
  }
  function sendChat(){
    var t=$('#sf-chat-in').value.trim(); if(!t)return;
    hideSugg();
    var gh=(t.match(/https?:\/\/github\.com\/[^\s)]+/)||[])[0];
    if(gh&&!(CODE&&CODE.source===gh)){
      // show the message while the repository is read, then let the real send
      // replace it (with the file count), so it appears once, not twice
      $('#sf-chat-in').value=''; var pending=addMsg('me',esc(t)); var fetching=addMsg('ai','<span class="sf-typing"><i></i><i></i><i></i></span> reading '+esc(gh));
      fetchGitHub(gh).then(function(c){ CODE=c; showCodeChip(); fetching.remove(); pending.remove(); $('#sf-chat-in').value=t; sendChat(); })
        .catch(function(e){ fetching.innerHTML='<span class="sf-err">Could not read '+esc(gh)+': '+esc(e.message||e)+'. Is it public? You can attach the folder instead.</span>'; });
      return;
    }
    $('#sf-chat-in').value=''; $('#sf-chat-in').style.height='auto'; $('#sf-chat-send').classList.remove('ready'); dropQuickChips();
    addMsg('me',esc(t)+(CODE?'<div style="font-size:11px;opacity:.8;margin-top:4px">&#128206; '+Object.keys(CODE.files).length+' files attached</div>':'')); pushHist({role:'user',text:t});
    var typing=typingBubble(); $('#sf-chat-send').disabled=true;
    var sentCode=!!CODE;   // attached code belongs to this message only; dropped once its reply is built
    call('chat',{messages:fitHistory(hist.slice(-12)), clob:CODE?codeDigest(CODE):undefined}).then(function(r){
      if(typing)typing.remove(); $('#sf-chat-send').disabled=false; $('#sf-chat-in').focus();
      if(r.err){addMsg('ai','<span class="sf-err">'+esc(r.err)+'</span>');return}
      var j=modelJson(r.raw);
      // an answer that is not JSON after the server's retries was cut off or garbled: say so, never show raw JSON
      var rawTxt=String(r.raw||''), looksJson=/^\s*(```|\{)/.test(rawTxt);
      var reply=j&&j.reply?j.reply:(looksJson?'I lost the end of that answer. Please send your message again.':rawTxt);
      if(j&&j.action&&j.action.type)keepFromLast(j.action);
      // the proposal rides along in the history (without its long code) so a
      // follow-up such as "no database" revises this plan instead of starting over
      pushHist({role:'assistant',text:reply+(j&&j.action&&j.action.type?' [PROPOSED ACTION] '+compactAction(j.action):'')});
      if(j&&j.action&&/^(create|deploy)$/.test(j.action.type||''))LAST_ACT=j.action;
      var d=addMsg('ai',fmt(reply));
      // a build is priced here, from the price list, not by the model's arithmetic
      if(j&&j.action&&/^(create|deploy)$/.test(j.action.type||'')){ var cc=costFor(j.action); if(cc)j.cost=cc; }
      d.insertAdjacentHTML('beforeend',widgetsHtml(j,reply,t));
      var qs=j&&j.questions, hasAct=!!(j&&j.action&&j.action.type); if(qs&&qs.length){
        // With a proposal on the table the questions are optional refinements:
        // the Create button stays, so a build never hinges on a follow-up.
        d.insertAdjacentHTML('beforeend','<h4 style="margin:10px 0 4px">'+(hasAct?'You can also tell me':'A couple of things first')+'</h4><ol style="margin:0;padding-left:18px">'
          +qs.map(function(q){return '<li>'+esc(q)+'</li>'}).join('')+'</ol>');
        if(!hasAct){ if(sentCode){CODE=null;showCodeChip();} return; }   // nothing proposed yet: wait for the answer
      }
      var a=j&&j.action;
      if(a&&a.type){
        // the name the model picks must fit the rule (2-20: a-z, 0-9, dash) or the build is refused
        if(a.sandbox_id){ a.sandbox_id=String(a.sandbox_id).toLowerCase().replace(/[^a-z0-9-]+/g,'-').replace(/^[^a-z]+/,'').slice(0,20).replace(/-+$/,'')||'sandbox'; }
        var bad=null; try{ bad=correctAction(a); }catch(e){ console.error('correct', e); }
        if(bad){ d.insertAdjacentHTML('beforeend','<div class="sf-err" style="margin-top:8px">'+esc(bad)+'</div>'); a=null; }
      }
      if(a&&a.type){
        try{ expandWorkload(a); }catch(e){ console.error('workload', e); }
        // The first container is served at the sandbox URL and the rest under
        // /<name>; a UI must be first and MCP answers at /mcp. The worker puts
        // them in that order, so describe (and send) them in that order too.
        var uiFirst=function(cs){return (cs||[]).slice().sort(function(x,y){return ((x.name||'').toLowerCase()==='mcp')-((y.name||'').toLowerCase()==='mcp')})};
        if(a.containers)a.containers=uiFirst(a.containers);
        (a.app_instances||[]).forEach(function(i){ if(i.containers)i.containers=uiFirst(i.containers); });
        var box=document.createElement('div'); box.className='sf-act';
        if(a.type!=='destroy'&&a.type!=='retry')d.insertAdjacentHTML('beforeend','<div class="sf-w-plan">'+infraHtml(a)+'</div>');
        var label=a.type==='destroy'?'Destroy '+a.sandbox_id:(a.type==='retry'?(a.ttl_days?'Set '+a.sandbox_id+' to '+a.ttl_days+' days':'Retry setup of '+a.sandbox_id):(a.type==='deploy'?'Deploy it':'Create it'));
        var envNames=(a.type==='destroy'||a.type==='retry')?[]:envNamesFrom(CODE?CODE.files:{}, a.env_names);
        var envBox=(a.type==='destroy'||a.type==='retry')?null:document.createElement('div');
        if(envBox){ envBox.innerHTML=envEditorHtml(envNames, envNames.length>0); d.appendChild(envBox); }
        box.innerHTML='<button type="button" class="sf-btn">'+esc(label)+'</button><button type="button" class="sf-btn sec">Not now</button>';
        d.appendChild(box);
        box.children[0].onclick=function(){ box.innerHTML='<span class="sf-spin"></span>Queueing...';
          if(a.type==='retry'){ call('retry',{sandbox_id:a.sandbox_id, ttl_days:a.ttl_days||undefined}).then(function(s){ box.innerHTML = s.err ? '<span class="sf-err">'+esc(s.err)+'</span>' : '<span class="sf-chip">Queued as request #'+esc(s.id)+'</span>'; refresh(); }); return; }
          var payload=a.type==='destroy'?{sandbox_id:a.sandbox_id,action:'DESTROY',ttl_days:1,enable_adb:false,enable_kafka:false,enable_app:false}
            :{sandbox_id:a.sandbox_id,action:a.git_url?'DEPLOY':'CREATE',ttl_days:a.ttl_days||3,enable_adb:!!a.enable_adb,enable_kafka:!!a.enable_kafka,enable_nosql:!!a.enable_nosql,enable_app:!!a.enable_app||!!a.git_url||!!a.app_image||!!(a.containers&&a.containers.length),
              app_image:a.app_image||null,git_url:a.git_url||null,app_port:a.app_port||80,text:t,containers:(a.containers&&a.containers.length)?a.containers:undefined,seed_sql:a.seed_sql||undefined,env:envFrom(envBox),enable_rag:!!a.enable_rag,
              enable_catalog:!!a.enable_catalog,enable_aidp:!!a.enable_aidp,databases:nz(a.databases),buckets:nz(a.buckets),queues:nz(a.queues),
              functions:nz(a.functions),dataflow_jobs:nz(a.dataflow_jobs),app_instances:nz(a.app_instances),
              app_files:a.app_files?JSON.stringify(a.app_files):undefined};
          call('submit',payload).then(function(s){ if(s.err){box.innerHTML='<span class="sf-err">'+esc(s.err)+'</span>';return}
            box.innerHTML='<span class="sf-chip">Queued as request #'+esc(s.id)+'</span>'; pushHist({role:'user',text:'(confirmed: '+a.type+' '+a.sandbox_id+' queued)'});
            // the attached code has been built; it must not ride along with the next, unrelated question
            if(CODE){ CODE=null; showCodeChip(); }
            refresh(); });
        };
        box.children[1].onclick=function(){box.remove()};
      }
      // the plan above already carries what it needs from the code (app_files,
      // workload); the next, unrelated question must not be answered about it
      if(sentCode){ CODE=null; showCodeChip(); }
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
      '.sf-ttl-pick{font-size:12px;border:1px solid #d6dde6;border-radius:6px;padding:1px 4px;background:#fff;color:#374151}',
      '.sf-reslink{display:inline-block;background:#eef4fb;color:#0b4a8b;border-radius:6px;padding:3px 9px;font-size:12px;margin:6px 6px 0 0;text-decoration:none}.sf-reslink:hover{background:#dbe8f8}',
      '.sf-w-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:10px;margin:10px 0 2px}',
      '.sf-w-card{background:#fff;border:1px solid #dde4ec;border-radius:12px;padding:11px 13px;box-shadow:0 1px 3px rgba(15,23,42,.05)}',
      '.sf-w-hd{display:flex;align-items:center;justify-content:space-between;gap:8px}.sf-w-hd b{font-size:14px}',
      '.sf-w-meta{font-size:12px;color:#6b7280;margin-top:3px}',
      '.sf-w-parts{margin-top:7px}.sf-w-parts span{display:inline-block;background:#eef4fb;color:#0b4a8b;border-radius:6px;padding:2px 7px;font-size:11px;margin:0 4px 4px 0}',
      '.sf-w-acts{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}.sf-w-acts .sf-btn{padding:5px 12px;font-size:12px;text-decoration:none}',
      '.sf-w-cost{margin-top:10px;background:#fff;border:1px solid #dde4ec;border-radius:12px;padding:6px 10px;overflow-x:auto}',
      '.sf-w-cost table{width:100%;border-collapse:collapse;font-size:13px}.sf-w-cost th{text-align:left;font-size:11px;color:#6b7280;font-weight:600;padding:6px 6px;border-bottom:1px solid #e5e9f0}',
      '.sf-w-cost td{padding:6px;border-bottom:1px solid #f1f4f8}.sf-w-cost .n{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}',
      '.sf-w-cost tfoot td{font-weight:700;border-bottom:0}',
      '.sf-w-cost tr.hd td{font-weight:700;color:#0b4a8b;background:#f5f8fc;padding-top:9px}',
      '.sf-w-plan{margin-top:10px;background:#fff;border:1px solid #dde4ec;border-left:3px solid #1a73e8;border-radius:10px;padding:8px 12px}',
      '.sf-msg p:last-child{margin:0}',
      '#sf-sugg.sf-pane{max-height:56vh;overflow:auto;padding:2px 4px 2px 0}',
      '#sf-recs{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:8px}',
      '#sf-recs .sf-rec{padding:10px 12px}',
      '#sf-recs .sf-rec .t{font-size:13px;line-height:1.25}',
      '#sf-recs .sf-rec .d{font-size:12px;line-height:1.35;margin-top:3px}',
      '#sf-recs .sf-rec .p{margin-top:6px}',
      '#sf-recs .sf-rec .p span{font-size:10.5px;padding:1px 6px}',
      '#sf-chat-in{width:100%;line-height:1.4}',
      '.sf-composer{display:flex !important;align-items:flex-end;gap:8px;flex-wrap:nowrap !important;border:1px solid #d6dde6;border-radius:18px;padding:8px 8px 8px 16px;background:#fff;box-shadow:0 2px 12px rgba(15,23,42,.06);transition:border-color .15s,box-shadow .15s}',
      '.sf-composer:focus-within{border-color:#1a73e8;box-shadow:0 0 0 3px rgba(26,115,232,.12)}',
      '.sf-composer .sf-field{margin:0 !important;flex:0 0 auto !important}',
      '.sf-composer .sf-field:first-of-type{flex:1 1 auto !important;align-self:center}',
      '.sf-composer #sf-attach{align-self:center;flex:0 0 auto}',
      '.sf-composer #sf-chat-in{border:0 !important;outline:0 !important;box-shadow:none !important;padding:7px 0 !important;min-height:24px !important;font-size:14px;font-family:inherit;background:transparent}',
      '.sf-composer #sf-model{width:auto;border:1px solid #e3e8ef;border-radius:999px;padding:6px 26px 6px 12px;font-size:12px;color:#374151;background-color:#f8fafc;cursor:pointer}',
      '.sf-composer #sf-chat-send{width:38px;height:38px;padding:0;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;background:#cbd5e1;flex:0 0 38px;transition:background .15s,transform .1s}',
      '.sf-composer #sf-chat-send.ready{background:#1a73e8}.sf-composer #sf-chat-send.ready:hover{background:#1557b0}',
      '.sf-composer #sf-chat-send:active{transform:scale(.94)}',
      '@media (max-width:640px){.sf-composer{flex-wrap:wrap !important}.sf-composer .sf-field:first-child{flex:1 1 100% !important}}',
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
    ['What do I have running?','A React app on sample sales data for 2 days',
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
    var row=ta.closest('.sf-row'); if(row)row.classList.add('sf-composer');
    var clip=document.createElement('button'); clip.type='button'; clip.id='sf-attach'; clip.title='Attach a folder of code'; clip.innerHTML='&#128206;';
    clip.style.cssText='border:0;background:transparent;font-size:18px;cursor:pointer;padding:6px 4px;color:#6b7280'; clip.onclick=attachFolder;
    var fld=ta.parentNode; if(fld.parentNode)fld.parentNode.insertBefore(clip, fld); else fld.insertBefore(clip, ta);
    var send=$('#sf-chat-send'); if(send){send.title='Send (Enter)'; send.setAttribute('aria-label','Send');
      send.innerHTML='<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5"/><path d="M5 12l7-7 7 7"/></svg>';}
    var sync=function(){if(send)send.classList.toggle('ready',!!ta.value.trim())};
    ta.addEventListener('input',sync); sync();
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
    if(c.region)window.__sfRegion=c.region;
    // the live price list ("key=price/unit; ..."), for cost tables computed here, not by the model
    window.__sfPricesDate=c.prices_updated||''; window.__sfPrices={}; (c.prices||'').split(';').forEach(function(kv){var m=kv.match(/^\s*([a-z0-9_]+)=([0-9.]+)/i); if(m)window.__sfPrices[m[1]]=Number(m[2]);});
    // offer only the models this region serves (the tenancy profile), default first
    var sel=document.querySelector('#sf-model');
    if(sel&&c.models&&c.models.length){
      sel.innerHTML=c.models.map(function(m){return '<option value="'+esc(m.id)+'">'+esc(m.label||m.id)+'</option>'}).join('');
      if(c.model)sel.value=c.model;
      var nm=document.querySelector('#sf-model-name'); if(nm)nm.textContent=sel.options[sel.selectedIndex].text;
    }
    if(c.user){var w=$('#sf-who');
      w.textContent='Signed in as '+c.user+' · '+(c.live||0)+' of '+(c.cap||3)+' sandboxes';
      w.classList.remove('sf-hide');}
  })
    .catch(function(){}).then(function(){drawRecipes(); buildTabs(); addClear(); restoreChat()});
  refresh();
}
(function(){ function go(){ if(window.apex&&apex.server){sfInit()} else {setTimeout(go,100)} } if(document.readyState==='loading'){document.addEventListener('DOMContentLoaded',go)} else {go()} })();
