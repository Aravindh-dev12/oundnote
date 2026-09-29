(function(){
'use strict';
var state={session:null,busy:false};
var $=function(s){return document.querySelector(s)};
function api(path,opt){opt=opt||{};opt.headers=Object.assign({Accept:'application/json'},opt.body instanceof FormData?{}:{'Content-Type':'application/json'},opt.headers||{});return fetch(path,opt).then(function(r){return r.status===204?null:r.json().then(function(x){if(!r.ok)throw Error(x.detail||'Request failed');return x})})}
function setState(kind,label,detail){
  var root=$('#flowbar');root.classList.remove('live','paused','processing');if(kind)root.classList.add(kind);
  $('#state-label').textContent=label;$('#state-detail').textContent=detail||'';
}
function format(ms){var s=Math.floor(Math.max(0,Number(ms||0))/1000);return String(Math.floor(s/60)).padStart(2,'0')+':'+String(s%60).padStart(2,'0')}
function renderSession(s){
  state.session=s;
  if(!s){setState(null,'Ready','Press the circle to start listening');$('#record-button').setAttribute('aria-label','Start listening');$('#pause-button').classList.add('hidden');return}
  var paused=s.state==='paused';setState(paused?'paused':'live',paused?'Paused':'Listening',format(s.elapsed_ms)+' · '+(s.segment_count||0)+' segments');
  $('#record-button').setAttribute('aria-label','Stop listening');$('#pause-button').classList.remove('hidden');$('#pause-button').textContent=paused?'▶':'II';
}
function refresh(){
  return api('/api/capture/session').then(function(s){renderSession(s);return s}).catch(function(){return null})
}
function start(){
  if(state.busy)return Promise.resolve();
  state.busy=true;setState('processing','Starting','Warming up local capture…');
  return api('/api/audio/sources').then(function(d){
    var mic=(d.sources||[]).find(function(x){return x.available&&x.kind!=='system'&&x.is_default})
      ||(d.sources||[]).find(function(x){return x.available&&x.kind!=='system'});
    if(!mic)throw Error('No microphone is available');
    return api('/api/capture/sessions',{method:'POST',body:JSON.stringify({
      source_id:mic.id,profile_id:'default',language:null,task:'transcribe',allow_model_download:false
    })});
  }).then(function(s){renderSession(s)})
    .catch(function(e){setState(null,'Not ready',e.message);throw e})
    .finally(function(){state.busy=false});
}
function stop(){
  if(!state.session||state.busy)return Promise.resolve();
  state.busy=true;setState('processing','Saving','Finishing the capture…');
  return api('/api/capture/sessions/'+encodeURIComponent(state.session.session_id)+'/stop',{
    method:'POST',body:JSON.stringify({final_transcription:true,postprocess_options:{diarization:true,summary:true}})
  }).then(function(){renderSession(null)})
    .catch(function(e){setState('live','Listening','Could not stop: '+e.message)})
    .finally(function(){state.busy=false});
}
function pause(){
  if(!state.session||state.busy)return;
  var action=state.session.state==='paused'?'resume':'pause';
  state.busy=true;
  return api('/api/capture/sessions/'+encodeURIComponent(state.session.session_id)+'/'+action,{method:'POST'})
    .then(refresh).finally(function(){state.busy=false});
}
window.__oundnoteHotkey=function(){
  return refresh().then(function(s){return s?stop():start()});
};
document.addEventListener('DOMContentLoaded',function(){
  $('#record-button').addEventListener('click',window.__oundnoteHotkey);
  $('#pause-button').addEventListener('click',pause);
  $('#brand-chip').addEventListener('click',function(){window.location.href='/'});
  refresh();
  setInterval(refresh,900);
});
}());
