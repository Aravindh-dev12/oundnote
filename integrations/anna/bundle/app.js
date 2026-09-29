const $ = id => document.getElementById(id);
let anna, busy = false, page = null, selectedId = null, notesText = '';
const toolId = window.__ANNA_TOOL_IDS__?.['oundnote-library'] || 'tool-dev-oundnote';
const timestamp = ms => { const s = Math.floor(ms / 1000); return `${Math.floor(s/3600)}:${String(Math.floor(s/60)%60).padStart(2,'0')}:${String(s%60).padStart(2,'0')}`; };
function node(tag, text, cls) { const el = document.createElement(tag); el.textContent = text; if(cls) el.className = cls; return el; }
function button(text, action) { const el = node('button', text); el.type = 'button'; el.addEventListener('click', () => run(action)); return el; }
function metadata(meeting) {
  const date = new Date(meeting.meeting_date || meeting.created_at || '');
  const label = Number.isNaN(date.getTime()) ? '' : new Intl.DateTimeFormat('en-US', {month:'short',day:'numeric',year:'numeric',hour:'2-digit',minute:'2-digit'}).format(date);
  return [label, `#${meeting.meeting_id ?? meeting.id}`].filter(Boolean).join(' · ');
}
// Render a small Markdown subset with text nodes only: meeting content is untrusted.
function inline(el, text) {
  text.split(/(\*\*[^*]+\*\*)/g).forEach(part => el.append(part.startsWith('**') && part.endsWith('**') ? node('strong',part.slice(2,-2)) : document.createTextNode(part)));
}
function renderNotes(text) {
  const root = node('div','','notes'); let list = null, paragraph = null;
  for(const line of text.split(/\r?\n/)) {
    const heading = line.match(/^#{1,6}\s+(.+)$/), bullet = line.match(/^\s*(?:[-*+]\s+|\d+[.)]\s+)(.*)$/);
    if(!line.trim()) { list = paragraph = null; continue; }
    if(heading) { list = paragraph = null; const h = node('h3',''); inline(h,heading[1]); root.append(h); }
    else if(bullet) {
      paragraph = null; const tag = /^\s*\d/.test(line) ? 'ol' : 'ul';
      if(!list || list.tagName.toLowerCase() !== tag) { list = node(tag,''); root.append(list); }
      const li = node('li',''); inline(li,bullet[1]); list.append(li);
    } else {
      list = null;
      if(!paragraph) { paragraph = node('p',''); root.append(paragraph); } else paragraph.append(document.createTextNode(' '));
      inline(paragraph,line);
    }
  }
  return root;
}
async function run(action) {
  if(busy || !anna) return;
  busy = true;
  document.querySelectorAll('button').forEach(el => el.disabled = true);
  $('status').className = ''; $('status').textContent = 'Querying Oundnote…';
  try { await action(); } catch(error) { $('status').className = 'error'; $('status').textContent = error.message || String(error); }
  finally { busy = false; document.querySelectorAll('button').forEach(el => el.disabled = false); }
}
async function call(args) {
  let out = await anna.tools.invoke({tool_id:toolId, method:'library', args});
  if(out?.ok === false || out?.success === false) throw new Error(out.error?.message || out.error);
  if(out?.ok === true) out = out.result;
  if(out?.success === false) throw new Error(out.error?.message || out.error);
  return out?.success === true ? out.data : out;
}
async function connection() {
  const out = await call({action:'status'});
  if(!out.connected || !out.enabled) throw new Error(out.message || 'Open Oundnote and enable MCP access in Settings. Run Anna Local Agent on the same computer.');
  $('status').textContent = `Oundnote ${out.app_version || ''} connected. Ready to search.`;
}
async function read(action, meetingId, cursor=-1, summaryId=null, append=false) {
  const out = await call({action, meeting_id:meetingId, cursor, summary_id:summaryId});
  if(!append) { $('detail').replaceChildren(); notesText = ''; }
  $('detail').className = '';
  [...$('actions').children].forEach(el => { const active = el.dataset.action === action; el.classList.toggle('active',active); el.setAttribute('aria-pressed',String(active)); });
  if(action === 'transcript') {
    for(const s of out.segments) {
      const name = s.speaker || 'Speaker'; let turn = $('detail').lastElementChild;
      if(!turn || turn.dataset.speaker !== name) {
        turn = node('div','','turn'); turn.dataset.speaker = name;
        const label = node('div','','speaker'); label.append(node('span',name.split(/\s+/).map(p=>p[0]).slice(0,2).join('').toUpperCase(),'avatar'),document.createTextNode(name));
        turn.append(label); $('detail').append(turn);
      }
      const segment = node('div','','segment');
      segment.append(node('time',timestamp(s.start_ms)),node('p',`${s.text}${s.text_truncated ? '\n[Excerpt truncated]' : ''}`)); turn.append(segment);
    }
    if(!out.segments.length && !append) $('detail').textContent = 'No segments available.';
  } else {
    notesText += out.content_markdown || '';
    $('detail').replaceChildren(notesText ? renderNotes(notesText) : node('pre',JSON.stringify(out.structured || {},null,2),'notes'));
  }
  page = out.next_cursor == null ? null : {action, meetingId, cursor:out.next_cursor, summaryId:action === 'summary' ? out.id : null};
  $('more').hidden = !page;
  $('status').textContent = out.truncated ? 'Partial content. Use Read more when available.' : 'Content loaded.';
}
function selectMeeting(meeting) {
  const id = meeting.meeting_id ?? meeting.id;
  selectedId = String(id);
  document.querySelectorAll('.card').forEach(card => { const active = card.dataset.id === selectedId; card.classList.toggle('selected',active); card.querySelector('button').setAttribute('aria-pressed',String(active)); });
  $('detail-title').textContent = meeting.meeting_title || meeting.title;
  $('detail-meta').textContent = metadata(meeting);
  const tabs = ['transcript','summary'].map(action => { const tab = button(action === 'transcript' ? 'Transcript' : 'AI notes',()=>read(action,id)); tab.dataset.action = action; tab.setAttribute('aria-pressed','false'); return tab; });
  $('actions').replaceChildren(...tabs);
  $('detail').className = 'empty';
  $('detail').textContent = 'Choose Transcript or AI notes.'; $('more').hidden = true; page = null;
  $('status').textContent = `Meeting #${id} selected.`;
}
$('search').addEventListener('submit', event => {
  event.preventDefault();
  run(async () => {
    const action = $('mode').value, query = $('query').value.trim();
    if(action !== 'list' && !query) throw new Error('Enter a search query.');
    const out = await call({action,query}); const items = out.meetings || out.results || [];
    $('results').replaceChildren();
    $('result-count').textContent = String(items.length);
    for(const item of items) {
      const card = node('article','','card');
      card.dataset.id = String(item.meeting_id ?? item.id); card.classList.toggle('selected',card.dataset.id === selectedId);
      card.append(button(item.meeting_title || item.title, () => selectMeeting(item)));
      card.querySelector('button').setAttribute('aria-pressed',String(card.dataset.id === selectedId));
      card.append(node('p',metadata(item),'meta'));
      if(item.text) card.append(node('p',`${timestamp(item.start_ms)} · ${item.speaker || 'Excerpt'}\n${item.text}`,'excerpt'));
      $('results').append(card);
    }
    if(!items.length) $('results').textContent = 'No results found.';
    $('status').textContent = `${items.length} results (up to ${action === 'list' ? 50 : action === 'semantic' ? 8 : 20} per search).`;
  });
});
$('connect').addEventListener('click', () => run(connection));
$('more').addEventListener('click', () => run(() => read(page.action,page.meetingId,page.cursor,page.summaryId,true)));
try {
  const {AnnaAppRuntime} = await import('/static/anna-apps/_sdk/latest/index.js');
  anna = await AnnaAppRuntime.connect(); await run(connection);
} catch(error) { $('status').textContent = `Could not connect to Anna. Open this interface through Anna or anna-app dev. ${error.message}`; }
