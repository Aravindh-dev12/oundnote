(function(){
'use strict';
function api(path,opt){opt=opt||{};opt.headers=Object.assign({'Accept':'application/json'},opt.body instanceof FormData?{}:{'Content-Type':'application/json'},opt.headers||{});return fetch(path,opt).then(function(r){if(!r.ok)return r.json().catch(function(){return{}}).then(function(x){throw Error(x.detail||'Request failed')});return r.status===204?null:r.json()})}
function toast(msg,t){if(window.OundnoteToast)window.OundnoteToast(msg,t||'info');else alert(msg)}
document.addEventListener('DOMContentLoaded',function(){
var q=document.getElementById('hub-search'),rows=[].slice.call(document.querySelectorAll('.history-row')),empty=document.getElementById('search-empty');
if(q){q.addEventListener('input',function(){var term=q.value.trim().toLowerCase(),count=0;rows.forEach(function(row){var ok=!term||((row.dataset.title||'')+' '+(row.dataset.description||'')).indexOf(term)>=0;row.hidden=!ok;if(ok)count++});if(empty)empty.classList.toggle('hidden',!term||count>0)});document.addEventListener('keydown',function(e){if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==='k'){e.preventDefault();q.focus()}})}
var b=document.getElementById('quick-capture');
if(b)b.addEventListener('click',function(){b.disabled=true;b.querySelector('strong').textContent='Starting…';api('/api/audio/sources').then(function(data){var mic=(data.sources||[]).filter(function(x){return x.kind!=='system'&&x.available}).sort(function(a,z){return Number(z.is_default)-Number(a.is_default)})[0];if(!mic)throw Error('No available microphone was found.');return api('/api/capture/sessions',{method:'POST',body:JSON.stringify({source_id:mic.id,title:null,profile_id:'default',language:null,task:'transcribe',allow_model_download:false})})}).then(function(s){location.href='/capture?meeting='+encodeURIComponent(s.meeting_id)}).catch(function(e){b.disabled=false;b.querySelector('strong').textContent='Start listening';toast(e.message,'error')})});
});
}());