/* TitanX Control Panel — Main JS */

// ── Theme ────────────────────────────────────────────────────────────────────
const THEME_KEY = 'titanx_theme';
function applyTheme(t){ document.documentElement.setAttribute('data-theme', t); }
function toggleTheme(){
  const cur = document.documentElement.getAttribute('data-theme') || 'dark';
  const next = cur === 'dark' ? 'light' : 'dark';
  applyTheme(next);
  localStorage.setItem(THEME_KEY, next);
  const btn = document.getElementById('theme-btn');
  if(btn) btn.innerHTML = next === 'dark' ? '☀️' : '🌙';
}
(function(){ applyTheme(localStorage.getItem(THEME_KEY) || 'dark'); })();

// ── Sidebar toggle ────────────────────────────────────────────────────────────
function toggleSidebar(){
  const sb = document.querySelector('.sidebar');
  const main = document.querySelector('.main');
  const overlay = document.getElementById('mobile-overlay');
  if(!sb) return;
  if(window.innerWidth <= 768){
    const isOpen = sb.classList.toggle('mobile-open');
    if(overlay) overlay.classList.toggle('visible', isOpen);
  } else {
    const collapsed = sb.classList.toggle('collapsed');
    if(main) main.classList.toggle('expanded', collapsed);
  }
}
function closeSidebar(){
  const sb = document.querySelector('.sidebar');
  const overlay = document.getElementById('mobile-overlay');
  if(sb) sb.classList.remove('mobile-open');
  if(overlay) overlay.classList.remove('visible');
}

// ── Toast ─────────────────────────────────────────────────────────────────────
function showToast(msg, type='info'){
  let container = document.getElementById('toast-container');
  if(!container){
    container = document.createElement('div');
    container.id = 'toast-container';
    document.body.appendChild(container);
  }
  const toast = document.createElement('div');
  const icons = { success:'✅', error:'❌', info:'ℹ️', warning:'⚠️' };
  toast.className = `toast ${type}`;
  toast.innerHTML = `<span>${icons[type]||'ℹ️'}</span><span>${msg}</span>`;
  container.appendChild(toast);
  setTimeout(()=>toast.remove(), 3500);
}

// ── API helpers ───────────────────────────────────────────────────────────────
async function apiGet(url){
  const r = await fetch(url);
  return r.json();
}
async function apiPost(url, body){
  const isForm = body instanceof FormData;
  const opts = {
    method:'POST',
    body: isForm ? body : JSON.stringify(body),
  };
  if(!isForm) opts.headers = {'Content-Type':'application/json'};
  const r = await fetch(url, opts);
  return r.json();
}

// ── Modal ─────────────────────────────────────────────────────────────────────
function openModal(id){ document.getElementById(id)?.classList.remove('hidden'); }
function closeModal(id){ document.getElementById(id)?.classList.add('hidden'); }
function closeAllModals(){
  document.querySelectorAll('.modal-overlay').forEach(m=>m.classList.add('hidden'));
}
document.addEventListener('click', e=>{
  if(e.target.classList.contains('modal-overlay')) closeAllModals();
});

// ── Animated counter ─────────────────────────────────────────────────────────
function animateCount(el, target, duration=1200){
  const start = 0;
  const step = timestamp => {
    if(!el._startTime) el._startTime = timestamp;
    const prog = Math.min((timestamp - el._startTime) / duration, 1);
    const ease = 1 - Math.pow(1 - prog, 3);
    el.textContent = Math.round(start + (target - start) * ease).toLocaleString('ar-EG');
    if(prog < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}
function initCounters(){
  document.querySelectorAll('[data-count]').forEach(el=>{
    const val = parseInt(el.getAttribute('data-count')) || 0;
    animateCount(el, val);
  });
}

// ── Tabs ──────────────────────────────────────────────────────────────────────
function initTabs(){
  document.querySelectorAll('.tabs').forEach(tabs=>{
    tabs.querySelectorAll('.tab-btn').forEach(btn=>{
      btn.addEventListener('click',()=>{
        const target = btn.getAttribute('data-tab');
        tabs.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));
        btn.classList.add('active');
        const parent = tabs.closest('.tab-container') || document;
        parent.querySelectorAll('.tab-pane').forEach(p=>{
          p.classList.toggle('active', p.id===target);
        });
      });
    });
  });
}

// ── File browser ──────────────────────────────────────────────────────────────
let _currentPath = '';
async function loadDir(path=''){
  _currentPath = path;
  const data = await apiGet(`/files/api/list?path=${encodeURIComponent(path)}`);
  if(data.error){ showToast(data.error,'error'); return; }
  renderDir(data);
}
function renderDir(data){
  const bc = document.getElementById('breadcrumb');
  if(bc){
    bc.innerHTML = data.breadcrumbs.map((b,i)=>{
      const sep = i > 0 ? '<span>/</span>' : '';
      return `${sep}<a href="#" onclick="loadDir('${b.rel}');return false">${b.name}</a>`;
    }).join('');
  }
  const tree = document.getElementById('file-tree');
  if(!tree) return;
  tree.innerHTML = '';
  data.items.forEach(item=>{
    const div = document.createElement('div');
    div.className = `file-item ${item.is_dir?'dir':''} ${item.protected?'protected':''}`;
    const icon = item.is_dir ? '📁' : _fileIcon(item.name);
    const actionsHtml = item.protected ? '' : `
      <div style="display:flex;gap:3px;flex-shrink:0" onclick="event.stopPropagation()">
        ${!item.is_dir && item.is_text ? `<button class="btn btn-sm btn-ghost" onclick="openEditor('${item.rel}')" title="تحرير">✏️</button>` : ''}
        <button class="btn btn-sm btn-ghost" onclick="downloadFile('${item.rel}')" title="تحميل">⬇️</button>
        <button class="btn btn-sm btn-ghost" onclick="renameFile('${item.rel}','${item.name}')" title="إعادة التسمية">✍️</button>
        <button class="btn btn-sm btn-danger" onclick="deleteFile('${item.rel}')" title="حذف">🗑️</button>
      </div>`;
    div.innerHTML = `
      <span class="file-icon">${icon}</span>
      <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${item.name}</span>
      ${item.size ? `<span style="color:var(--text2);font-size:.73rem;flex-shrink:0">${item.size}</span>` : ''}
      ${actionsHtml}
    `;
    if(item.is_dir){
      div.querySelector('.file-icon').addEventListener('click', ()=>loadDir(item.rel));
      div.querySelector('span:nth-child(2)').addEventListener('click', ()=>loadDir(item.rel));
      div.style.cursor = 'pointer';
    }
    tree.appendChild(div);
  });
}
async function renameFile(oldRel, oldName){
  const newName = prompt(`إعادة تسمية "${oldName}" إلى:`, oldName);
  if(!newName || newName === oldName) return;
  const data = await apiPost('/files/api/rename', {old: oldRel, new: newName});
  if(data.error) showToast(data.error, 'error');
  else { showToast('تمت إعادة التسمية ✅', 'success'); loadDir(_currentPath); }
}
function _fileIcon(name){
  const ext = name.split('.').pop()?.toLowerCase();
  const map = {py:'🐍',js:'📜',ts:'📘',json:'📋',md:'📄',html:'🌐',css:'🎨',
               txt:'📃',log:'📓',zip:'📦',sh:'⚙️',yml:'⚙️',yaml:'⚙️',sql:'🗄️'};
  return map[ext] || '📄';
}
async function openEditor(path){
  const data = await apiGet(`/files/api/read?path=${encodeURIComponent(path)}`);
  if(data.error){ showToast(data.error,'error'); return; }
  const editor = document.getElementById('file-editor');
  const editorPath = document.getElementById('editor-path');
  const editorModal = document.getElementById('editor-modal');
  if(!editor) return;
  editor.value = data.content;
  if(editorPath) editorPath.textContent = path;
  editor._path = path;
  editorModal?.classList.remove('hidden');
}
async function saveFile(){
  const editor = document.getElementById('file-editor');
  if(!editor || !editor._path) return;
  const saveBtn = document.getElementById('save-btn');
  if(saveBtn){ saveBtn.disabled=true; saveBtn.innerHTML='<span class="spinner"></span> جارٍ الحفظ…'; }
  try {
    const data = await apiPost('/files/api/save', {path:editor._path, content:editor.value});
    if(data.error){
      showToast(data.error, 'error');
      if(data.syntax_error){
        const diffEl = document.getElementById('diff-view');
        if(diffEl){ diffEl.textContent = `خطأ: ${data.error}`; diffEl.className='code-block'; }
      }
    } else {
      showToast('تم الحفظ بنجاح ✅','success');
      const diffEl = document.getElementById('diff-view');
      if(diffEl && data.diff){
        diffEl.innerHTML = data.diff.split('\n').map(l=>{
          const cls = l.startsWith('+') ? 'add' : l.startsWith('-') ? 'remove' : 'ctx';
          return `<div class="diff-line ${cls}">${_esc(l)}</div>`;
        }).join('');
      }
    }
  } catch(e){ showToast('فشل الاتصال','error'); }
  if(saveBtn){ saveBtn.disabled=false; saveBtn.innerHTML='💾 حفظ'; }
}
function _esc(s){ return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
async function deleteFile(path){
  if(!confirm(`هل تريد حذف ${path}؟`)) return;
  const data = await apiPost('/files/api/delete',{path});
  if(data.error) showToast(data.error,'error');
  else { showToast('تم الحذف','success'); loadDir(_currentPath); }
}
function downloadFile(path){
  window.location.href = `/files/api/download?path=${encodeURIComponent(path)}`;
}

// ── Upload zone ───────────────────────────────────────────────────────────────
function initUploadZone(){
  const zone = document.getElementById('upload-zone');
  if(!zone) return;
  const input = zone.querySelector('input[type=file]');
  zone.addEventListener('click',()=>input?.click());
  zone.addEventListener('dragover',e=>{e.preventDefault();zone.classList.add('drag-over');});
  zone.addEventListener('dragleave',()=>zone.classList.remove('drag-over'));
  zone.addEventListener('drop',e=>{
    e.preventDefault(); zone.classList.remove('drag-over');
    handleUpload(e.dataTransfer.files);
  });
  input?.addEventListener('change',()=>handleUpload(input.files));
}
async function handleUpload(files){
  for(const file of files){
    const fd = new FormData();
    fd.append('path', _currentPath);
    fd.append('file', file);
    const data = await apiPost('/files/api/upload', fd);
    if(data.error) showToast(`فشل رفع ${file.name}: ${data.error}`,'error');
    else showToast(`تم رفع ${data.name} (${data.size})`,'success');
  }
  loadDir(_currentPath);
}

// ── User management ───────────────────────────────────────────────────────────
async function banUser(uid){ await userAction('/users/api/ban','user_id='+uid,'POST-FORM'); }
async function unbanUser(uid){ await userAction('/users/api/unban','user_id='+uid,'POST-FORM'); }
async function grantPremium(uid, days=30){ await userAction('/users/api/premium',`user_id=${uid}&days=${days}`,'POST-FORM'); }
async function removePremium(uid){ await userAction('/users/api/remove_premium','user_id='+uid,'POST-FORM'); }
async function userAction(url, params, method){
  const fd = new FormData();
  params.split('&').forEach(p=>{ const [k,v]=p.split('='); fd.append(k,v); });
  const data = await apiPost(url, fd);
  if(data.error) showToast(data.error,'error');
  else { showToast('تم بنجاح ✅','success'); setTimeout(()=>location.reload(),800); }
}
async function addRemovePoints(uid){
  const amt = prompt('أدخل النقاط (رقم سالب لخصم):');
  if(!amt) return;
  const fd = new FormData();
  fd.append('user_id', uid); fd.append('amount', amt);
  const data = await apiPost('/users/api/points', fd);
  if(data.error) showToast(data.error,'error');
  else showToast(`تم! النقاط الجديدة: ${data.new_points}`,'success');
}
async function showUserDetail(uid){
  const data = await apiGet(`/users/api/${uid}`);
  if(data.error){ showToast(data.error,'error'); return; }
  const modal = document.getElementById('user-detail-modal');
  if(!modal) return;
  modal.querySelector('#ud-name').textContent = data.first_name || '---';
  modal.querySelector('#ud-id').textContent = data.user_id;
  modal.querySelector('#ud-username').textContent = data.username ? '@'+data.username : '---';
  modal.querySelector('#ud-points').textContent = data.points;
  modal.querySelector('#ud-downloads').textContent = data.download_count;
  modal.querySelector('#ud-referrals').textContent = data.referrals;
  modal.querySelector('#ud-lang').textContent = data.language === 'ar' ? 'عربي' : 'English';
  modal.querySelector('#ud-banned').textContent = data.is_banned ? '✅ محظور' : '✅ نشط';
  modal.querySelector('#ud-vip').textContent = data.vip_until || 'لا';
  modal.querySelector('#ud-ban-btn').onclick = ()=>banUser(uid);
  modal.querySelector('#ud-unban-btn').onclick = ()=>unbanUser(uid);
  modal.querySelector('#ud-premium-btn').onclick = ()=>grantPremium(uid,30);
  modal.querySelector('#ud-points-btn').onclick = ()=>addRemovePoints(uid);
  modal.classList.remove('hidden');
}

// ── Broadcast ─────────────────────────────────────────────────────────────────
async function sendBroadcast(){
  const text = document.getElementById('bc-text')?.value;
  const mode = document.getElementById('bc-mode')?.value || 'HTML';
  if(!text){ showToast('أدخل نص الرسالة','warning'); return; }
  const btn = document.getElementById('bc-btn');
  if(btn){ btn.disabled=true; btn.innerHTML='<span class="spinner"></span> جارٍ الإرسال…'; }
  const fd = new FormData();
  fd.append('text', text); fd.append('parse_mode', mode);
  const data = await apiPost('/broadcast/api/send', fd);
  if(data.error){ showToast(data.error,'error'); }
  else {
    showToast(`جارٍ البث لـ ${data.total} مستخدم…`,'info');
    pollBroadcast();
  }
  if(btn){ btn.disabled=false; btn.innerHTML='📢 إرسال البث'; }
}
function pollBroadcast(){
  const interval = setInterval(async()=>{
    const s = await apiGet('/broadcast/api/status');
    const el = document.getElementById('bc-status');
    if(el){
      el.innerHTML = `✅ ${s.success} | ❌ ${s.failed} | 📤 ${s.total}`;
    }
    if(s.done){ clearInterval(interval); showToast(`اكتمل البث: ${s.success} ناجح، ${s.failed} فشل`,'success'); }
  }, 1000);
}

// ── System stats live update ──────────────────────────────────────────────────
async function refreshStats(){
  try{
    const data = await apiGet('/system/api/stats');
    _setBar('cpu-bar', data.cpu_percent);
    _setBar('mem-bar', data.mem_percent);
    _setBar('disk-bar', data.disk_percent);
    _setText('cpu-val', data.cpu_percent.toFixed(1)+'%');
    _setText('mem-val', data.mem_used_h+' / '+data.mem_total_h);
    _setText('disk-val', data.disk_used_h+' / '+data.disk_total_h);
    _setText('net-sent', data.net_sent_h);
    _setText('net-recv', data.net_recv_h);
    _setText('uptime-val', data.uptime);
  }catch{}
}
function _setBar(id, pct){
  const el = document.getElementById(id);
  if(!el) return;
  el.style.width = pct+'%';
  el.className = 'progress-bar ' + (pct>85?'red':pct>60?'yellow':'');
}
function _setText(id, val){
  const el = document.getElementById(id);
  if(el) el.textContent = val;
}

// ── Update center ─────────────────────────────────────────────────────────────
let _analyzedZipPath = '';
async function analyzeUpdate(file){
  if(!file){ showToast('اختر ملف ZIP أولاً','warning'); return; }
  const fd = new FormData(); fd.append('file', file);
  const btn = document.getElementById('analyze-btn');
  if(btn){ btn.disabled=true; btn.innerHTML='<span class="spinner"></span> جارٍ التحليل…'; }
  const data = await apiPost('/updates/api/analyze', fd);
  if(btn){ btn.disabled=false; btn.innerHTML='🔍 تحليل'; }
  if(data.error){ showToast(data.error,'error'); return; }
  _analyzedZipPath = data.zip_path;
  const res = document.getElementById('analysis-result');
  if(res){
    res.classList.remove('hidden');
    res.innerHTML = `
      <div class="alert alert-info">
        📦 إجمالي: <b>${data.total}</b> |
        🆕 جديد: <b>${data.new.length}</b> |
        ✏️ معدَّل: <b>${data.modified.length}</b> |
        🔐 محمي: <b>${data.protected.length}</b>
        ${data.deps_changed?'<br>⚠️ سيتم تحديث التبعيات':''}
      </div>
      ${data.new.length?`<details><summary>ملفات جديدة</summary><ul>${data.new.map(f=>`<li><code>${f}</code></li>`).join('')}</ul></details>`:''}
      ${data.modified.length?`<details><summary>ملفات معدَّلة</summary><ul>${data.modified.map(f=>`<li><code>${f}</code></li>`).join('')}</ul></details>`:''}
    `;
  }
  const applyBtn = document.getElementById('apply-btn');
  if(applyBtn) applyBtn.disabled = false;
}
async function applyUpdate(){
  if(!_analyzedZipPath){ showToast('حلّل التحديث أولاً','warning'); return; }
  const ver = document.getElementById('version-input')?.value || new Date().toISOString().slice(0,10);
  const btn = document.getElementById('apply-btn');
  if(btn){ btn.disabled=true; btn.innerHTML='<span class="spinner"></span> جارٍ التطبيق…'; }
  const data = await apiPost('/updates/api/apply',{zip_path:_analyzedZipPath,version:ver});
  if(data.error){ showToast(data.error,'error'); if(btn){btn.disabled=false;btn.innerHTML='🚀 تطبيق التحديث';} return; }
  showToast('بدأ التطبيق…','info');
  pollUpdateStatus();
}
function pollUpdateStatus(){
  const log = document.getElementById('update-log');
  const interval = setInterval(async()=>{
    const s = await apiGet('/updates/api/status');
    if(log) log.innerHTML = s.log.map(l=>`<div>${l}</div>`).join('');
    if(s.done){
      clearInterval(interval);
      if(s.success) showToast('🎉 اكتمل التحديث بنجاح!','success');
      else showToast('❌ فشل التحديث، راجع السجل','error');
      setTimeout(()=>location.reload(),2000);
    }
  }, 1000);
}

// ── Logs ──────────────────────────────────────────────────────────────────────
let _logInterval = null;
function switchLog(key){
  document.querySelectorAll('.log-tab').forEach(b=>b.classList.remove('active'));
  document.querySelector(`[data-log="${key}"]`)?.classList.add('active');
  loadLog(key);
}
async function loadLog(key, search=''){
  const lines = await apiGet(`/logs/api/read?log=${key}&lines=300&search=${encodeURIComponent(search)}`);
  const el = document.getElementById('log-view');
  if(!el) return;
  el.innerHTML = lines.lines.map(l=>{
    const cls = /ERROR|خطأ/.test(l)?'error':/WARN|تحذير/.test(l)?'warning':/INFO/.test(l)?'info':'';
    return `<div class="log-line ${cls}">${_esc(l)}</div>`;
  }).join('');
  el.scrollTop = el.scrollHeight;
}
function initLogAutoRefresh(key){
  if(_logInterval) clearInterval(_logInterval);
  _logInterval = setInterval(()=>loadLog(key), 5000);
}

// ── GitHub ────────────────────────────────────────────────────────────────────
async function gitPull(){
  const btn = document.getElementById('pull-btn');
  if(btn){ btn.disabled=true; btn.innerHTML='<span class="spinner"></span>'; }
  const data = await apiPost('/github/api/pull',{});
  if(btn){ btn.disabled=false; btn.innerHTML='⬇️ Pull'; }
  showToast(data.output || (data.ok?'تم Pull بنجاح':'فشل Pull'), data.ok?'success':'error');
  refreshGitInfo();
}
async function gitPush(){
  const msg = document.getElementById('commit-msg')?.value || 'Update via TitanX';
  const btn = document.getElementById('push-btn');
  if(btn){ btn.disabled=true; btn.innerHTML='<span class="spinner"></span>'; }
  const data = await apiPost('/github/api/push',{message:msg});
  if(btn){ btn.disabled=false; btn.innerHTML='⬆️ Push'; }
  showToast(data.output || (data.ok?'تم Push بنجاح':'فشل Push'), data.ok?'success':'error');
  refreshGitInfo();
}
async function refreshGitInfo(){
  const data = await apiGet('/github/api/info');
  _setText('git-branch', '🌿 '+data.branch);
  const commits = document.getElementById('git-commits');
  if(commits && data.commits){
    commits.innerHTML = data.commits.map(c=>`<div class="scroll-list-item"><code style="font-size:.75rem">${_esc(c)}</code></div>`).join('');
  }
}

// ── DB manager ────────────────────────────────────────────────────────────────
async function dbRepair(action){
  const data = await apiPost('/database/api/repair',{action});
  if(data.results){
    showToast(data.results.join(' | '),'success');
  }
}
async function dbVacuum(){ await dbRepair('vacuum'); }
async function dbFixOrphans(){ await dbRepair('orphans'); }
async function dbIntegrity(){
  const data = await apiPost('/database/api/repair',{action:'integrity'});
  showToast(data.results?.join(', ')||'تم','info');
}

// ── Create backup ─────────────────────────────────────────────────────────────
async function createBackup(){
  const btn = document.getElementById('backup-btn');
  if(btn){ btn.disabled=true; btn.innerHTML='<span class="spinner"></span> جارٍ…'; }
  const data = await apiPost('/updates/api/backup',{});
  if(btn){ btn.disabled=false; btn.innerHTML='💾 نسخة احتياطية'; }
  if(data.ok) showToast(`تم إنشاء النسخة: ${data.name}`,'success');
  else showToast(data.error||'فشل','error');
}

// ── Init on DOM ready ─────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', ()=>{
  // Apply saved theme
  const savedTheme = localStorage.getItem(THEME_KEY) || 'dark';
  applyTheme(savedTheme);
  const themeBtn = document.getElementById('theme-btn');
  if(themeBtn) themeBtn.innerHTML = savedTheme === 'dark' ? '☀️' : '🌙';

  // Init tabs
  initTabs();

  // Animate counters
  initCounters();

  // Init upload zone if present
  initUploadZone();

  // Load file tree if on files page
  if(document.getElementById('file-tree')){
    loadDir('');
  }

  // System stats refresh
  if(document.getElementById('cpu-bar')){
    refreshStats();
    setInterval(refreshStats, 3000);
  }

  // Sidebar tab highlighting
  const path = location.pathname;
  document.querySelectorAll('.nav-item').forEach(a=>{
    const href = a.getAttribute('href');
    if(href && path.startsWith(href) && href !== '/'){
      a.classList.add('active');
    } else if(href === '/' && path === '/'){
      a.classList.add('active');
    }
  });

  // Git info refresh if on GitHub page
  if(document.getElementById('git-commits')) refreshGitInfo();
});
