const COMPANY_URL='./data/companies.json';
const JOBS_URL='./data/jobs.json';
let companies=[],jobs=[];
const KEY='pharmaRadarState', COMPANY_KEY='pharmaRadarImportedCompanies';

function companyId(c){return (c.name+'|'+c.country).toLowerCase().trim();}
function getImportedCompanies(){try{return JSON.parse(localStorage.getItem(COMPANY_KEY)||'[]')}catch(e){return []}}
function saveImportedCompanies(list){localStorage.setItem(COMPANY_KEY,JSON.stringify(list));}

async function load(){
  const base=await fetch(COMPANY_URL,{cache:'no-store'}).then(r=>r.json());
  const imported=getImportedCompanies(), seen=new Set(base.map(companyId));
  companies=base.concat(imported.filter(c=>!seen.has(companyId(c))));
  let jobsBase=[];
  try{jobsBase=await fetch(JOBS_URL,{cache:'no-store'}).then(r=>r.ok?r.json():[])}catch(e){}
  const saved=JSON.parse(localStorage.getItem(KEY)||'{}'), overrides=saved.statusOverrides||{};
  jobs=jobsBase.map(j=>({...j,status:overrides[j.id]||j.status}));
  const lastScan=jobs.reduce((max,j)=>j.lastSeenAt&&j.lastSeenAt>max?j.lastSeenAt:max,'');
  document.querySelector('#companies').textContent=companies.length;
  document.querySelector('#scan').textContent=lastScan?new Date(lastScan).toLocaleString():(jobs.length?'Unknown':'Not yet');
  render();
}

function normalizeRow(row){
 const get=(...keys)=>{for(const k of Object.keys(row)){const nk=k.toLowerCase().trim();if(keys.includes(nk))return row[k];}return undefined};
 const name=get('name','company','company name'),country=get('country'),careers=get('careers','careers url','careers page','website','url');
 if(!name||!country||!careers)return null;
 const priority=(get('priority')||'B').toString().trim().toUpperCase()||'B';
 let sponsor=parseInt(get('sponsor','sponsorship','sponsor score'),10);if(isNaN(sponsor)||sponsor<1)sponsor=3;if(sponsor>4)sponsor=4;
 const rolesRaw=get('roles','role','keywords')||'',roles=String(rolesRaw).split(/[;,]/).map(s=>s.trim()).filter(Boolean);
 return {name:String(name).trim(),country:String(country).trim(),priority,sponsor,careers:String(careers).trim(),roles:roles.length?roles:['QC','QA']};
}

function importFromWorkbook(file){
 const reader=new FileReader();
 reader.onload=e=>{try{
  const wb=XLSX.read(e.target.result,{type:'binary'}),sheet=wb.Sheets[wb.SheetNames[0]];
  const rows=XLSX.utils.sheet_to_json(sheet,{defval:''}),parsed=rows.map(normalizeRow).filter(Boolean);
  if(!parsed.length){document.querySelector('#importMsg').textContent='No valid rows found.';return}
  const existing=getImportedCompanies(),existingIds=new Set(companies.map(companyId));let added=0;
  parsed.forEach(c=>{if(!existingIds.has(companyId(c))){existing.push(c);existingIds.add(companyId(c));added++}});
  saveImportedCompanies(existing);
  document.querySelector('#importMsg').textContent=`Imported ${added} new compan${added===1?'y':'ies'}.`;load();
 }catch(err){document.querySelector('#importMsg').textContent='Could not read that file.';console.error(err)}};
 reader.readAsBinaryString(file);
}
function exportCompaniesJson(){const blob=new Blob([JSON.stringify(companies,null,2)],{type:'application/json'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='companies.json';a.click();URL.revokeObjectURL(url)}

function render(){
 const q=document.querySelector('#search').value.toLowerCase().trim(), c=document.querySelector('#country').value, s=document.querySelector('#status').value;
 const filtered=jobs.filter(j=>
   (!q||`${j.title} ${j.company} ${j.location||''} ${j.description||''}`.toLowerCase().includes(q)) &&
   (!c||j.country===c)&&(!s||j.status===s)
 );
 document.querySelector('#jobs').textContent=jobs.length;
 document.querySelector('#new').textContent=jobs.filter(j=>j.status==='new').length;
 document.querySelector('#jobsList').innerHTML=filtered.length?filtered.map(j=>`
 <article class="job">
  <div>
   <b>${j.title}</b>
   <span class="badge">${j.country}</span><span class="badge">${j.company}</span>
   <span class="badge ${j.score>=80?'green':j.score>=60?'orange':'red'}">Match ${j.score}%</span>
   ${j.location?`<span class="badge">${j.location}</span>`:''}
   <div class="muted small">${(j.description||'').slice(0,700)}</div>
   <div class="small">Source: ${j.source||'career site'} · Seen: ${j.lastSeenAt?new Date(j.lastSeenAt).toLocaleString():''}</div>
  </div>
  <div><a class="link" target="_blank" rel="noopener" href="${j.url}">VIEW</a><br>
   <select onchange="setStatus('${j.id}',this.value)">
    <option ${j.status==='new'?'selected':''}>new</option><option ${j.status==='applied'?'selected':''}>applied</option>
    <option ${j.status==='interview'?'selected':''}>interview</option><option ${j.status==='offer'?'selected':''}>offer</option>
    <option ${j.status==='rejected'?'selected':''}>rejected</option>
   </select>
  </div>
 </article>`).join(''):'<div class="card muted">No relevant QC/QA/GMP vacancies match these filters.</div>';
 document.querySelector('#companiesList').innerHTML=companies.map(x=>`
 <article class="job"><div><b>${x.name}</b> <span class="badge">${x.country}</span>
 <span class="badge">Priority ${x.priority}</span><span class="badge">Sponsor ${'🟢'.repeat(x.sponsor)}</span>
 <div class="muted small">${x.roles.join(' · ')}</div></div>
 <a class="link" target="_blank" rel="noopener" href="${x.careers}">CAREERS</a></article>`).join('');
}
function setStatus(id,v){const j=jobs.find(x=>x.id===id);if(j){j.status=v;const saved=JSON.parse(localStorage.getItem(KEY)||'{}'),overrides=saved.statusOverrides||{};overrides[id]=v;localStorage.setItem(KEY,JSON.stringify({...saved,statusOverrides:overrides}));render()}}
window.setStatus=setStatus;
document.querySelectorAll('input,select').forEach(e=>e.addEventListener('input',render));
document.querySelector('#clear').onclick=()=>{document.querySelector('#search').value='';document.querySelector('#country').value='';document.querySelector('#status').value='';render()};
document.querySelector('#notify').onclick=async()=>{if(!('Notification'in window)){alert('Notifications are not supported in this browser.');return}const p=await Notification.requestPermission();document.querySelector('#notify').textContent=p==='granted'?'Notifications enabled':'Enable notifications'};
if('serviceWorker'in navigator)navigator.serviceWorker.register('sw.js');
document.querySelector('#importFile').addEventListener('change',e=>{const f=e.target.files[0];if(f)importFromWorkbook(f);e.target.value=''});
document.querySelector('#exportCompanies').addEventListener('click',exportCompaniesJson);
load();