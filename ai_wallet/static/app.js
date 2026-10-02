/* 本项目原创；未引入第三方代码或素材。 */
'use strict';

let state = null;
let activePage = 'overview';
let renderedHash = null;
let orderFilter = 'all';
let busy = false;
let toastTimer;
let fetchSequence = 0;
let refreshing = false;
let lastUpdated = null;
let syncError = '';
let orderSearch = '';
let taskFilter = 'all';
let deferredRender = false;
const drafts = {};
let guideExpanded = null;
let devToolsExpanded = false;
let helpQuery = '';
const expandedHelp = new Set();
const pendingTaskStorageKey = 'yuxing.demo.pendingTask.v1';
const pendingProposalStorageKey = 'yuxing.demo.pendingBudgetProposal.v1';
const pendingTicketStorageKey = 'yuxing.demo.pendingTicket.v1';
let supportTickets=[];
let supportLoaded=false;
let supportLoading=false;
let supportError='';
let supportOrderId='';
let supportSequence=0;
const todoStatuses = ['pending_approval','approved','unknown','delivery_failed'];
function awaitingDelivery(order) { return order.status==='paid'&&!order.delivery; }
function needsAttention(order) { return todoStatuses.includes(order.status)||awaitingDelivery(order); }
const pendingRequestStorageKey = 'yuxing.demo.pendingPurchase.v1';
const main = document.querySelector('#main');
const labels = {overview:'钱包概览',inbox:'待办',policy:'授权规则',orders:'交易记录',tasks:'AI 与任务',operations:'帮助说明',integration:'AI 接入'};
const taskStatusLabels = {active:['可采购',''],paused:['已暂停','amber'],archived:['已归档','gray'],pending_budget:['待确认预算','amber'],closed:['已结束','gray'],rejected:['预算未获准','gray']};
const statusLabels = {pending_approval:['待你批准','amber'],approved:['已批准 · 未付款','blue'],paid:['已支付并交付',''],rejected:['已拒绝','red'],failed:['支付失败','red'],unknown:['结果未知 · 额度保留','amber'],delivery_failed:['已付款 · 交付失败','amber'],refunded:['已模拟退款','gray']};
const categoryLabels = {image:'图片素材',data:'资料与数据',tool:'工作工具'};
const scenarioLabels = {success:'正常支付并交付',decline:'模拟付款被拒绝',timeout:'模拟支付结果未知',delivery_failure:'模拟已付但交付失败'};

// 所有来自接口的文本仅用 textContent 写入，避免商品或订单文本变成可执行内容。
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}
function append(parent, ...children) { children.filter(Boolean).forEach(child => parent.append(child)); return parent; }
function money(cents) { return new Intl.NumberFormat('zh-CN',{style:'currency',currency:'CNY'}).format((cents || 0)/100); }
function date(value) { const d = new Date(value); return Number.isNaN(d.getTime()) ? String(value || '') : d.toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}); }
function taskName(id) { return state.tasks.find(task=>task.id===id)?.title || id; }
// 草稿只保存在当前页面内存；刷新数据和切换栏目不会抹掉输入。
function wireDraft(form,key,baseRevision=state.policy.revision) {
  const controls=[...form.querySelectorAll('input,select,textarea')];
  const saved=drafts[key];
  controls.forEach((control,index)=>{if(saved&&saved.values[index]!==undefined){if(control.type==='checkbox')control.checked=saved.values[index];else control.value=saved.values[index];}});
  const revision=saved?.revision ?? baseRevision;
  const remember=()=>{drafts[key]={revision,values:controls.map(control=>control.type==='checkbox'?control.checked:control.value)};};
  form.addEventListener('input',remember);form.addEventListener('change',remember);
  return revision;
}
function display(value) { return typeof value === 'object' && value !== null ? JSON.stringify(value) : String(value || ''); }
function button(label, callback, kind = '') { const b = el('button',`button ${kind}`,label); b.type='button'; b.addEventListener('click',callback); return b; }
function badge(label, tone = '') { return el('span',`status-badge ${tone}`,label); }
function status(order) {
  // 付款记录与成果交付独立；不能仅凭 paid 向用户承诺素材已拿到。
  if(order.status==='paid')return order.delivery?badge('已支付 · 已有交付记录'):badge('已支付 · 待确认交付','amber');
  const item = statusLabels[order.status] || [order.status,'gray']; return badge(item[0],item[1]);
}
function notice(message, tone='') { return el('div',`notice ${tone}`,message); }
function showToast(message, error=false) { const box=document.querySelector('#toast'); clearTimeout(toastTimer); box.textContent=message; box.className=`toast${error?' error':''}`; box.hidden=false; toastTimer=setTimeout(()=>box.hidden=true,error?9000:5500); }
// 使用页面内对话框：浏览器与自动化工具均能审阅说明，只有明确确认才继续操作。
function confirmAction(message,title='确认操作') {
  return new Promise(resolve=>{
    const trigger=document.activeElement;
    const dialog=el('dialog','confirm-dialog');
    const headingId=`confirm-title-${crypto.randomUUID()}`;
    const descriptionId=`confirm-description-${crypto.randomUUID()}`;
    const heading=el('h2','',title);heading.id=headingId;
    const description=el('p','confirm-description',message);description.id=descriptionId;
    dialog.setAttribute('aria-labelledby',headingId);dialog.setAttribute('aria-describedby',descriptionId);
    let settled=false;
    function finish(confirmed){if(settled)return;settled=true;dialog.close();dialog.remove();if(trigger&&trigger.isConnected)trigger.focus();resolve(confirmed);}
    const cancel=button('取消',()=>finish(false));
    const confirm=button('确认操作',()=>finish(true),'button-primary');
    append(dialog,el('div','eyebrow','SIMULATION CONFIRMATION'),heading,description,append(el('div','confirm-actions'),cancel,confirm));
    dialog.addEventListener('cancel',event=>{event.preventDefault();finish(false);});
    document.body.append(dialog);dialog.showModal();cancel.focus();
  });
}
function empty(message) { return append(el('div','empty'),el('span','empty-symbol','◇'),el('span','',message)); }
function heading(title, description, eyebrow) { return append(el('div','page-heading'),append(el('div'),el('div','eyebrow',eyebrow),el('h1','',title),el('p','page-description',description)),el('div','heading-aside','人民币 CNY / 支付工具未连接')); }
function panel(title, subtitle, content, action) { return append(el('section','panel'),append(el('div','panel-head'),append(el('div'),el('h2','',title),subtitle?el('div','panel-subtitle',subtitle):null),action),content); }
function progress(spent,total) { const p=el('div','progress'); const fill=el('span'); fill.style.width=`${Math.max(0,Math.min(100,total?spent/total*100:0))}%`; return append(p,fill); }
function field(label, control, help) { const wrapper=el('label','field'); if(!control.getAttribute('aria-label'))control.setAttribute('aria-label',label); append(wrapper,el('span','field-label',label),control,help?el('p','field-help',help):null); return wrapper; }
function select(options, value) { const node=el('select'); options.forEach(([id,name])=>{const option=el('option','',name); option.value=id; node.append(option);}); if(value!==undefined)node.value=value; return node; }
function auditPanel() { const details=el('details','panel details-panel section-gap'); details.append(el('summary','details-toggle',`授权与操作审计 · ${(state.audit||[]).length} 条记录`)); const list=el('div','panel-body audit-list'); const events=(state.audit||[]).slice(0,30); events.forEach(item=>list.append(append(el('div','audit-item'),el('div','audit-time',date(item.at)),append(el('div'),el('strong','',`${item.action} · ${item.actor||'系统'}`),el('p','',`${item.order_id?`${item.order_id} · `:''}${display(item.detail)}`))))); if(!events.length)list.append(empty('完成一次采购后，这里会留下操作记录。')); details.append(list); return details; }

function readPendingRequest() {
  const raw=sessionStorage.getItem(pendingRequestStorageKey);
  if(!raw)return null;
  try { const value=JSON.parse(raw); if(!value.key||!value.payload||!value.scenario)throw new Error(); return value; }
  catch { throw new Error('未确认采购记录无法读取。请先核对交易记录，不要重新发起采购。'); }
}
function forgetPendingRequest() { sessionStorage.removeItem(pendingRequestStorageKey); }

async function request(path,options={}) {
  const controller=new AbortController();
  const timer=setTimeout(()=>controller.abort(),10000);
  try {const response=await fetch(path,{...options,signal:controller.signal});const data=await response.json();return {response,data};}
  catch(error){if(error.name==='AbortError')throw new Error('请求超时，结果尚未确认。请刷新核对原订单，不要重复采购。');throw error;}
  finally{clearTimeout(timer);}
}
async function fetchState() {
  const sequence=++fetchSequence;
  const {response,data}=await request('/api/state',{cache:'no-store'});
  if(sequence!==fetchSequence)return false;
  if(response.status===401){showLogin();const error=new Error('本地工作空间已锁定，请重新登录。');error.code='login_required';throw error;}
  if(!response.ok)throw new Error('无法读取钱包状态，请确认本地服务已启动。');
  if(sequence!==fetchSequence)return false;
  state=data;
  state.orders.sort((a,b)=>String(b.created_at).localeCompare(String(a.created_at)));
  state.audit.sort((a,b)=>Number(b.id)-Number(a.id));
  lastUpdated=new Date();syncError='';return true;
}
async function post(path,body={}) {
  const {response,data}=await request(path,{method:'POST',headers:{'Content-Type':'application/json','X-Wallet-Token':state.csrf_token},body:JSON.stringify(body)});
  if(!response.ok || data.ok===false){const error=new Error(data.error || '操作未完成，请刷新后重试。');error.status=response.status;error.code=data.code;if(response.status===401){showLogin();error.code='login_required';}throw error;}
  return data;
}
async function act(callback, message) {
  if(busy)return;
  busy=true;
  ++fetchSequence; // 丢弃写操作前已经发出、尚未返回的旧状态请求。
  document.querySelectorAll('button').forEach(node=>node.disabled=true);
  try { await callback(); await fetchState(); render(); if(message)showToast(message); }
  catch(error) { showToast(error.message,true); if(error.code!=='login_required')try { await fetchState(); render(); } catch(refreshError) { if(refreshError.code!=='login_required')showToast(`${error.message} 状态刷新失败，请重新加载页面后核对。`,true); } }
  finally { busy=false; document.querySelectorAll('button').forEach(node=>node.disabled=false);updateShell();flushDeferredRender(); }
}
function route() {const [page,id]=location.hash.slice(1).split('/');return {page:labels[page]?page:'overview',id:['orders','tasks'].includes(page)?id:null};}
function navigate(page,id) { if(!labels[page])page='overview'; activePage=page; location.hash=page+(id?`/${encodeURIComponent(id)}`:''); render();window.scrollTo(0,0); }
// 引导只定位已有控件，不创建任务、不保存规则，也不自动发起采购。
function navigateToSection(page,sectionId) {
  navigate(page);
  const target=document.querySelector(`#${sectionId}`);
  if(target){for(let parent=target.parentNode;parent;parent=parent.parentNode){if(parent.tagName==='DETAILS')parent.open=true;}target.setAttribute('tabindex','-1');target.focus();target.scrollIntoView({block:'start'});}
}
function updateShell() {
  if(!state){document.querySelector('#pause-button').disabled=true;const refresh=document.querySelector('#refresh-button');refresh.disabled=busy||refreshing;refresh.textContent=refreshing?'刷新中…':'刷新';return;}
  document.querySelectorAll('[data-page]').forEach(node=>{node.classList.toggle('active',node.dataset.page===activePage);node.setAttribute('aria-current',node.dataset.page===activePage?'page':'false');});
  document.querySelector('#breadcrumb-page').textContent=labels[activePage];
  const indicator=document.querySelector('#wallet-status'); indicator.textContent=state.policy.enabled?'消费授权已开启':'消费授权已暂停'; indicator.className=`status-badge ${state.policy.enabled?'':'amber'}`;
  const pause=document.querySelector('#pause-button'); pause.disabled=busy; pause.textContent=state.policy.enabled?'暂停消费授权':'恢复消费授权';
  const count=document.querySelector('#inbox-count');if(count){const n=state.orders.filter(needsAttention).length+(state.allocation?budgetProposals().length:0);count.textContent=n;count.hidden=n===0;}
  const sync=document.querySelector('#sync-status');if(sync)sync.textContent=syncError || (lastUpdated?`更新于 ${date(lastUpdated)} · 北京时间`:'正在更新');
  const refresh=document.querySelector('#refresh-button');if(refresh){refresh.disabled=busy||refreshing;refresh.textContent=refreshing?'刷新中…':'刷新';}
  const logout=document.querySelector('#logout-button');if(logout){logout.hidden=!state.owner_protected;logout.disabled=busy||refreshing;}
}
async function refreshState(manual=false) {
  if(!state&&!manual)return;
  if(busy||refreshing||document.querySelector('dialog[open]'))return;
  refreshing=true;updateShell();
  try {
    const applied=await fetchState();
    if(applied&&!busy){
      supportLoaded=false;
      const editing=main.contains(document.activeElement)&&document.activeElement.matches('input,select,textarea');
      if(!editing)render();else {deferredRender=true;updateShell();}
      if(manual)showToast('状态已更新，未提交的表单内容已保留。');
    }
  }catch(error){if(error.code!=='login_required'){syncError='刷新失败 · 显示的是旧数据';updateShell();}if(manual)showToast(error.message,true);}
  finally{refreshing=false;updateShell();flushDeferredRender();}
}
function flushDeferredRender() {
  if(!deferredRender||busy||refreshing||document.querySelector('dialog[open]'))return;
  if(main.contains(document.activeElement)&&document.activeElement.matches('input,select,textarea'))return;
  render();
}
function render() {
  if(!state)return;
  renderedHash=location.hash;
  deferredRender=false;
  updateShell();
  main.replaceChildren();
  if(activePage==='overview')renderOverview();
  if(activePage==='inbox')renderInbox();
  if(activePage==='policy')renderPolicy();
  if(activePage==='orders')renderOrders();
  if(activePage==='tasks')renderTasks();
  if(activePage==='operations')renderOperations();
  if(activePage==='integration')renderIntegration();
}
function statCard(label,value,foot,extra,primary=false,used,total) {
  const card=el('section',`stat-card${primary?' primary':''}`);
  const val=el('div','stat-value'); append(val,el('small','','¥'),document.createTextNode((value/100).toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2})));
  append(card,append(el('div','stat-top'),el('span','',label),el('span','',primary?(state.allocation?'TOTAL':'MONTHLY'):'CNY')),val,append(el('div','stat-foot'),el('span','',foot),el('span','',extra)));
  if(used!==undefined)card.append(progress(used,total));
  return card;
}
function renderOverview() {
  if(state.allocation)return renderAllocationOverview();
  main.append(heading('预算、待办和结果，一处查看','先设好边界，日常只处理需要你决定的采购。当前仍为本地模拟。','YOUR WORKSPACE'));
  main.append(append(el('div','workspace-actions'),button('新增任务',()=>navigateToSection('tasks','task-create-start'),'button-primary'),button('调整授权规则',()=>navigateToSection('policy','policy-start')),button('AI 接入',()=>navigate('integration'),'button-ghost')));
  if(!state.policy.enabled)main.append(notice('消费授权已暂停。新的采购与支付会被阻止；已发生的支付仍需核验或处理。','warning'));
  if(sessionStorage.getItem(pendingRequestStorageKey))main.append(append(el('div','inbox-summary'),el('strong','','有一笔演示采购申请尚未确认'),button('核对原申请',()=>navigate('integration'),'button-primary')));
  const pending=state.orders.filter(needsAttention);
  const summary=el('div','inbox-summary');
  append(summary,el('strong','',pending.length?`${pending.length} 笔采购需要处理`:'目前没有待办'),el('span','',`待审批 ${pending.filter(o=>o.status==='pending_approval').length} · 待执行 ${pending.filter(o=>o.status==='approved').length} · 异常 ${pending.filter(o=>['unknown','delivery_failed'].includes(o.status)||awaitingDelivery(o)).length}`),button('打开待办 →',()=>navigate('inbox'),'button-primary'));
  main.append(summary);
  const s=state.summary,p=state.policy;
  const stats=el('div','stats-grid');
  append(stats,statCard('本月剩余可用额度',s.month_available_cents,`总授权 ${money(p.monthly_limit_cents)}`,`预留 ${money(s.month_reserved_cents)}`,true,s.month_spent_cents+s.month_reserved_cents,p.monthly_limit_cents),statCard('本月累计消费',s.month_spent_cents,'退款不恢复累计消费额度','额度不代表现金余额'),statCard('今日剩余额度',s.day_available_cents,`今日上限 ${money(p.daily_limit_cents)}`,`待你审批 ${s.pending_count} 笔`));
  main.append(stats);
  const left=el('div','stack');
  const taskBody=el('div','panel-body');
  const visibleTasks=state.tasks.filter(task=>task.status!=='archived').slice(0,3);
  visibleTasks.forEach(task=>{const card=el('div','task-card');append(card,append(el('div','task-top'),el('div','task-icon','◇'),append(el('div','task-copy'),button(task.title,()=>navigate('tasks',task.id),'text-link'),el('div','task-meta',`${taskStatusLabels[task.status]?.[0]||'未知状态，禁止采购'} · 本地演示任务`)),el('div','task-amount',money(task.available_cents))),progress(task.spent_cents+task.reserved_cents,task.budget_cents),append(el('div','task-bottom'),el('span','',`已消费 ${money(task.spent_cents)} · 预留 ${money(task.reserved_cents)}`),el('span','',`预算 ${money(task.budget_cents)}`)));taskBody.append(card);});
  if(!visibleTasks.length)taskBody.append(empty('暂无未归档任务。点击“新增任务”设置用途和预算。'));
  left.append(panel('任务预算','每项采购都关联具体任务；右侧金额为任务剩余额度',taskBody,button('管理任务 →',()=>navigate('tasks'),'button-small')));
  const pendingBody=el('div','panel-body');
  pending.slice(0,3).forEach(order=>pendingBody.append(orderCard(order)));
  if(!pending.length)pendingBody.append(empty('待审批、待执行和异常采购都会集中出现在这里。'));
  const pendingPanel=panel('需要你处理','先看买什么、为什么买、多少钱；首页最多展示 3 笔',pendingBody,button(`全部待办 ${pending.length} →`,()=>navigate('inbox'),'button-small'));
  main.append(append(el('div','owner-grid'),pendingPanel,left));
  const recent=el('div','table-wrap');
  if(state.orders.length){const table=el('table');const head=el('tr');['采购项目','金额','状态','时间'].forEach(v=>head.append(el('th','',v)));table.append(append(el('thead'),head));const tbody=el('tbody');state.orders.slice(0,5).forEach(order=>tbody.append(append(el('tr'),append(el('td'),button(order.product_title,()=>navigate('orders',order.id),'text-link'),el('span','cell-sub',order.merchant_name)),el('td','number',money(order.amount_cents)),append(el('td'),status(order)),el('td','number',date(order.created_at)))));table.append(tbody);recent.append(table);}else recent.append(empty('发起第一笔模拟采购，记录会出现在这里。'));
  const viewAll=button('查看全部 →',()=>navigate('orders'),'button-ghost button-small');
  const recentPanel=el('details','panel details-panel section-gap');append(recentPanel,el('summary','details-toggle','最近交易 · 展开查看'),recent,append(el('div','panel-body'),viewAll));main.append(recentPanel,startGuide());
}
// 任务预算模式独立于旧演示数据：额度只经用户确认划分，AI 提案不能自行授权。
function budgetProposals() {return state.tasks.filter(task=>task.proposed_budget_cents!==null&&task.proposed_budget_cents!==undefined&&!['closed','rejected'].includes(task.status));}
function allocationGuide() {
  const guide=el('details','panel start-guide');guide.open=guideExpanded===true;
  guide.addEventListener('toggle',()=>{if(guide.isConnected)guideExpanded=guide.open;});
  append(guide,el('summary','details-toggle','使用方式：设总额度 → 确认任务预算 → 管理权限'),append(el('div','panel-body'),el('p','','你设定 AI 总授权额度，AI 提出任务和建议预算，你确认后划分给任务。追加必须再次确认；暂停或结束任务可以收回后续消费权限，未决付款仍需核对。'),el('p','field-help','资金保留在原账户。当前支付工具尚未连接，保存规则不会转移资金，也尚不能限制银行卡扣款。'),button('查看 AI 接入',()=>navigate('integration'),'button-small')));return guide;
}
function allocationTaskCard(task) {
  const card=el('article','task-management-card');
  append(card,append(el('div','order-title-row'),el('h3','',task.title),taskBadge(task)),append(el('div','task-budget-line'),el('strong','',`任务可用 ${money(task.available_cents)}`),el('span','',`已确认预算 ${money(task.budget_cents)}`)),progress(task.spent_cents+task.reserved_cents,task.budget_cents),el('p','field-help',`已消费 ${money(task.spent_cents)} · 付款占用 ${money(task.reserved_cents)}`),task.proposed_budget_cents!==null&&task.proposed_budget_cents!==undefined?el('p','field-help',`预算提案 ${money(task.proposed_budget_cents)}，等待你确认`):null,button('查看任务',()=>navigate('tasks',task.id),'button-small'));return card;
}
function renderAllocationOverview() {
  const a=state.allocation;
  main.append(heading('管理 AI 的预算与消费权限','你设定授权总额，确认任务预算，随时暂停；选购与网站操作由 AI 完成。','YOUR WORKSPACE'));
  main.append(append(el('div','workspace-actions'),button(a.total_cents?'调整 AI 总额度':'设置 AI 总额度',()=>navigateToSection('policy','allocation-pool'),'button-primary'),button('查看我的任务',()=>navigate('tasks')),button('查看授权规则',()=>navigateToSection('policy','policy-start'),'button-ghost')));
  const pending=budgetProposals(),orders=state.orders.filter(needsAttention);
  if(pending.length||orders.length)main.append(append(el('div','inbox-summary'),el('strong','',`${pending.length} 项任务预算待确认${orders.length?` · ${orders.length} 笔采购需要处理`:''}`),button('处理待办',()=>navigate('inbox'),'button-primary')));
  main.append(notice('支付工具未连接：当前可管理本地预算规则，尚未形成银行卡扣款保护。','warning'));
  if(!state.policy.enabled)main.append(notice('本地消费授权已暂停。任务预算保留，钱包入口拒绝新的采购申请。','warning'));
  if(!a.total_cents)main.append(notice('先设置 AI 可操控总额度。提案提交不会占用额度，只有你确认预算后才分配给任务。'));
  const stats=el('div','stats-grid');append(stats,statCard('尚可分配给任务',a.unallocated_cents,`AI 总额度 ${money(a.total_cents)}`,'不是现金余额',true,a.total_cents-a.unallocated_cents,a.total_cents),statCard('任务尚未消费的额度',a.allocated_cents,'包括任务可用与付款占用','结束任务后可释放未用部分'),statCard('累计已消费',a.spent_cents,'退款不恢复累计额度','所有任务合计'));main.append(stats);
  const needs=el('div','panel-body');pending.slice(0,3).forEach(task=>needs.append(budgetDecisionCard(task)));if(!pending.length)needs.append(empty('没有待确认预算。日常只需确认 AI 的预算提案与处理例外。'));
  if(orders.length)needs.append(append(el('div','inbox-summary'),el('strong','',`${orders.length} 笔采购需要处理`),button('处理采购例外',()=>navigate('inbox'),'button-small')));
  const tasks=el('div','panel-body');state.tasks.filter(task=>['active','paused'].includes(task.status)).slice(0,3).forEach(task=>tasks.append(allocationTaskCard(task)));if(!tasks.children.length)tasks.append(empty('任务预算确认后，会出现在这里。'));
  main.append(append(el('div','owner-grid'),panel('待你确认任务预算',`共 ${pending.length} 项；确认前 AI 不能使用这部分额度`,needs,button('全部待办 →',()=>navigate('inbox'),'button-small')),panel('进行中的任务','每个任务使用自己的预算，不能挪用其他任务额度',tasks)));
  if(sessionStorage.getItem(pendingProposalStorageKey)||sessionStorage.getItem(pendingRequestStorageKey))main.append(append(el('div','inbox-summary'),el('strong','','有一项演示请求尚未确认'),button('核对原请求',()=>navigate('integration'),'button-small')));
  main.append(allocationGuide());
}
function allocationPoolForm() {
  const form=el('form','panel-body guide-target');form.id='allocation-pool';const amount=el('input');amount.inputMode='decimal';amount.required=true;amount.value=(state.allocation.total_cents/100).toFixed(2);
  append(form,field('AI 可操控总额度（元）',amount,'这是所有任务共用的累计授权上限，不会向 AI 转账。不能低于已消费和已划分任务额度之和。'));
  const key='allocation-pool',revision=wireDraft(form,key,state.allocation.revision);
  if(revision!==state.allocation.revision)form.append(notice('额度已变化，草稿已保留。请先读取最新额度再保存。','warning'));
  const save=el('button','button button-primary','保存 AI 总额度');save.type='submit';append(form,save,button('放弃草稿，读取最新额度',async()=>{delete drafts[key];await refreshState(true);render();},'button-ghost button-small'));
  form.addEventListener('submit',async event=>{event.preventDefault();if(busy||refreshing||!form.reportValidity())return;try{const total=centsFromInput(amount,'AI 总额度');if(total>100000000)throw new Error('AI 总额度最多为 1,000,000 元。');if(!await fetchState())throw new Error('正在更新，请稍后重试。');if(state.allocation.revision!==revision)throw new Error('额度已变化，请读取最新额度后重新核对。');const occupied=state.allocation.spent_cents+state.allocation.allocated_cents;if(total<occupied)throw new Error(`总额度不能低于已消费和任务占用合计 ${money(occupied)}。`);if(!await confirmAction(`AI 可操控总额度设为 ${money(total)}，尚可分配 ${money(total-occupied)}。这只调整本地消费权限，不转账、不发起采购。`,'确认 AI 总额度'))return;await act(async()=>{await post('/api/allocation/pool',{total_cents:total,expected_revision:revision});delete drafts[key];},'AI 总额度已更新。');}catch(error){showToast(error.message,true);}});return form;
}
function budgetDecisionCard(task) {
  const card=el('article','budget-decision-card');const form=el('form');
  append(card,append(el('div','order-title-row'),el('h3','',task.title),badge(task.status==='pending_budget'?'新任务预算':'追加预算','amber')),el('p','order-reason',task.budget_reason||'未提供预算说明'),el('p','field-help',`AI 建议总预算 ${money(task.proposed_budget_cents)} · 已确认 ${money(task.budget_cents)}`));
  const amount=el('input');amount.inputMode='decimal';amount.required=true;amount.value=(task.proposed_budget_cents/100).toFixed(2);append(form,field('确认任务总预算（元）',amount,'可以修改金额；追加时填写调整后的总预算，不能低于原已确认预算。'));
  const key=`budget-decide-${task.id}`,revision=wireDraft(form,key,task.revision);const impact=el('p','budget-impact');
  const preview=()=>{try{const target=centsFromInput(amount,'任务预算'),delta=target-task.budget_cents;if(delta<0){impact.textContent='确认预算不能低于已确认预算；释放未用额度请结束任务。';return;}impact.textContent=`${delta>=0?'本次划分':'本次释放'} ${money(Math.abs(delta))} · 确认后尚可分配 ${money(state.allocation.unallocated_cents-delta)}`;}catch{impact.textContent='请输入有效金额后查看额度变化。';}};amount.addEventListener('input',preview);preview();form.append(impact);
  if(revision!==task.revision)form.append(notice('提案已更新，当前草稿不能直接提交。请读取最新提案后确认。','warning'));
  const approve=el('button','button button-primary','确认并分配预算');approve.type='submit';
  const decide=async decision=>{if(busy||refreshing)return;try{const target=decision==='approve'?centsFromInput(amount,'任务预算'):undefined;if(target>100000000)throw new Error('任务预算最多为 1,000,000 元。');await act(async()=>{if(!await fetchState())throw new Error('状态正在更新，请重新核对。');const current=state.tasks.find(t=>t.id===task.id);if(!current||current.revision!==revision)throw new Error('提案已变化，未提交决定。请读取最新提案后重新核对。');if(decision==='approve'&&(target<current.budget_cents||target<current.spent_cents+current.reserved_cents||target-current.budget_cents>state.allocation.unallocated_cents))throw new Error('预算不能低于原已确认预算或已消费和付款占用，也不能超过尚可分配额度。请修改金额或调整 AI 总额度。');await post(`/api/allocation/tasks/${encodeURIComponent(task.id)}/decide`,{decision,...(decision==='approve'?{budget_cents:target}:{}),expected_revision:revision});delete drafts[key];},decision==='approve'?'任务预算已确认。符合授权范围的采购可以在预算内执行。':'预算提案已拒绝；已确认的旧预算不变。');}catch(error){showToast(error.message,true);}};
  form.addEventListener('submit',event=>{event.preventDefault();if(form.reportValidity())return decide('approve');});append(form,append(el('div','order-actions'),approve,button('拒绝预算提案',()=>decide('reject'),'button-small'),button('读取最新提案',async()=>{delete drafts[key];await refreshState(true);render();},'button-ghost button-small')),el('p','field-help','确认仅分配预算，不会立即购买商品。'));card.append(form);return card;
}
function readPendingProposal(){const raw=sessionStorage.getItem(pendingProposalStorageKey);if(!raw)return null;try{const value=JSON.parse(raw);if(!value.key||!value.payload)throw new Error();return value;}catch{throw new Error('原预算提案记录无法读取，请核对任务列表，不要重复提交。');}}
function allocationProposalForm(task=null) {
  const form=el('form','panel-body');let pending;try{if(!task)pending=readPendingProposal();}catch(error){form.append(notice(error.message,'error'));return form;}
  if(pending){append(form,notice('上一项提案提交结果尚未确认。原参数和请求编号已保留；核验只重试原提案，不创建新请求。','warning'),el('p','',`${pending.payload.title} · 建议预算 ${money(pending.payload.budget_cents)}`),el('p','',pending.payload.reason),button('核验并重试原预算提案',()=>act(async()=>{const original=readPendingProposal();if(!original||original.key!==pending.key)throw new Error('原提案记录已变化，请重新核对。');const result=await post('/api/allocation/proposals',{...original.payload,idempotency_key:original.key});if(!result.task?.id)throw new Error('尚未确认提案，继续保留原请求编号。');sessionStorage.removeItem(pendingProposalStorageKey);delete drafts['budget-propose-new'];},'原提案已确认，请到待办决定预算。'),'button-primary'));return form;}
  const title=el('input');title.required=true;title.maxLength=120;title.placeholder='例如：完成产品宣传视频';title.value=task?.title||'';const amount=el('input');amount.required=true;amount.inputMode='decimal';amount.value=task?((task.proposed_budget_cents??task.budget_cents)/100).toFixed(2):'100.00';const reason=el('textarea');reason.required=true;reason.maxLength=500;reason.placeholder='说明要完成什么、预算用于哪些资料或素材';
  if(!task)form.append(field('任务名称',title));append(form,field(task?'建议调整后的总预算（元）':'建议任务预算（元）',amount),field('预算用途与理由',reason));const key=`budget-propose-${task?.id||'new'}`,revision=wireDraft(form,key,task?.revision??state.allocation.revision);
  const submit=el('button','button button-primary',task?'提交追加预算提案':'提交模拟 AI 预算提案');submit.type='submit';append(form,submit,el('p','field-help','提交只是模拟 AI 提案，不会自行划分额度或执行采购；需要用户在待办确认。'));
  form.addEventListener('submit',async event=>{event.preventDefault();if(busy||refreshing||!form.reportValidity())return;try{const budget=centsFromInput(amount,'建议预算');if(budget>100000000)throw new Error('建议预算最多为 1,000,000 元。');if(task&&budget<=task.budget_cents)throw new Error('追加后的总预算必须高于已确认预算。');const explanation=taskText(reason,'预算用途与理由',500);const proposalTitle=task?task.title:taskText(title,'任务名称',120);await act(async()=>{if(task){await post(`/api/allocation/tasks/${encodeURIComponent(task.id)}/propose`,{budget_cents:budget,reason:explanation,expected_revision:revision});}else{if(readPendingProposal())throw new Error('有一项提案尚未确认，请核验原提案。');const original={key:crypto.randomUUID(),payload:{title:proposalTitle,agent_id:`demo-${crypto.randomUUID()}`,budget_cents:budget,reason:explanation}};sessionStorage.setItem(pendingProposalStorageKey,JSON.stringify(original));const result=await post('/api/allocation/proposals',{...original.payload,idempotency_key:original.key});if(!result.task?.id)throw new Error('尚未确认提案，原请求编号已保留。');sessionStorage.removeItem(pendingProposalStorageKey);}delete drafts[key];},'模拟 AI 预算提案已提交，请到待办确认预算。');}catch(error){showToast(error.message,true);}});return form;
}
function renderAllocationTasks() {
  main.append(heading('按任务分配预算，按成果查看进度','AI 先提案，你确认预算后才分配额度。这里不会直接创建已授权的任务。','TASK BUDGETS'));
  const id=route().id;if(id){main.append(button('← 返回任务列表',()=>navigate('tasks'),'order-detail-back'));const task=state.tasks.find(t=>t.id===id);if(!task){main.append(empty('没有找到这个任务。'));return;}main.append(panel('任务预算','消费权限按任务独立计算',append(el('div','panel-body'),allocationTaskCard(task),button('查看任务交易',()=>taskOrders(task),'button-small'))));if(budgetProposals().some(t=>t.id===id))main.append(panel('待确认提案','用户确认后生效',append(el('div','panel-body'),budgetDecisionCard(task))));if(['active','paused'].includes(task.status)){const actions=el('div','panel-body');append(actions,button(task.status==='active'?'暂停此任务':'恢复此任务',()=>changeTaskStatus(task,task.status==='active'?'paused':'active'),'button-small'),button('结束任务并释放未用额度',async()=>{if(busy||refreshing)return;if(!await confirmAction(`结束“${task.title}”后不能继续采购。未付款申请将取消，未使用的预算回到尚可分配额度；已消费不退回，结果未知的付款继续占用，核验后再结算。任务结束不能恢复。`,'结束任务'))return;await act(()=>post(`/api/allocation/tasks/${encodeURIComponent(task.id)}/close`,{expected_revision:task.revision}),'任务已结束，请核对释放后的总额度。');},'button-small'));main.append(panel('任务操作','暂停保留任务预算；结束释放可释放的未用额度',actions));const proposal=el('details','panel details-panel section-gap');append(proposal,el('summary','details-toggle','演示：让 AI 申请追加预算'),allocationProposalForm(task));main.append(proposal);}const details=el('details','panel details-panel section-gap');append(details,el('summary','details-toggle','任务技术信息'),el('p','panel-body',`任务 ${task.id} · 模拟主体 ${task.agent_id} · 版本 ${task.revision}，未连接真实 AI`));main.append(details);return;}
  const list=el('div','panel-body allocation-task-list');state.tasks.forEach(task=>list.append(allocationTaskCard(task)));if(!state.tasks.length)list.append(empty('还没有任务。先设置总额度，接入 AI 后由它提出任务预算，再由你确认。'));main.append(append(el('div','workspace-actions'),button('查看 AI 接入',()=>navigate('integration'),'button-primary'),button('设置 AI 总额度',()=>navigateToSection('policy','allocation-pool'))),panel('全部任务',`共 ${state.tasks.length} 项`,list));
}
function startGuide() {
  if(state.allocation)return allocationGuide();
  const guide=el('details','panel start-guide');
  guide.open=guideExpanded===true;
  guide.addEventListener('toggle',()=>{if(guide.isConnected)guideExpanded=guide.open;});
  guide.append(el('summary','details-toggle','使用方式：设任务、定边界、处理待办'));
  const body=el('div','panel-body');
  body.append(el('p','field-help','这里是本地演示：不用充值或绑定银行卡。预置任务仅供体验；完成下面的操作也不会连接真实 AI 或扣款。'));
  const steps=el('ol','start-steps');
  [
    ['创建你的任务','写清用途并设置任务累计预算，例如“产品宣传配图”。已有任务也可以继续使用。','创建我的任务',()=>navigateToSection('tasks','task-create-start')],
    ['核对消费边界','确认单笔、每日、每月上限和允许的商家、类别；所有任务共用这些规则。','查看授权规则',()=>navigateToSection('policy','policy-start')],
    ['只处理需要你决定的事','到待办核对购买理由和金额；完成后到交易记录查结果。模拟采购由独立演示入口提供。','查看我的待办',()=>navigate('inbox')],
  ].forEach(([title,copy,label,action],index)=>steps.append(append(el('li'),el('span','step-number',index+1),append(el('div'),el('h3','',title),el('p','field-help',copy),button(label,action,'button-small')))));
  append(body,steps,button('使用中遇到问题？查看帮助',()=>navigate('operations'),'button-ghost button-small'));guide.append(body);return guide;
}
function renderIntegration() {
  main.append(heading('AI 接入与支付管控','钱包管理授权与付款许可；购物网站由 AI 操作，网站信息用于消费记录。','AGENT ACCESS'));
  const connection=el('div','panel-body');
  append(connection,el('p','','预算管理：可通过本地工具提出任务、查询预算和申请追加，预算仍由你确认。'),el('p','','支付工具：未连接。当前不能执行真实付款，也尚未形成银行卡扣款保护。'),el('p','field-help','防护生效需要支付工具强制检查钱包许可，且 AI 不能从其他入口绕过。仅保存预算或记录网址不能证明这一点。'));
  main.append(panel('当前接入状态','连接状态与预算规则分开显示',connection));
  const flow=el('div','panel-body');
  append(flow,el('p','','用户授权总额 → 确认任务预算 → 钱包检查付款请求 → 支付工具执行 → 返回结果并记账。'),el('p','field-help','这是目标付款流程；当前默认 AI 入口只开放预算管理。网站、用途等由 AI 提交的信息不能自动当成已验证的付款事实。'),button('管理授权规则',()=>navigate('policy'),'button-primary'),button('查看我的任务',()=>navigate('tasks'),'button-small'));
  main.append(panel('用户只管理预算和权限','无需填写采购商品或逐个对接网站',flow));
  const technical=el('details','panel details-panel section-gap');
  append(technical,el('summary','details-toggle','接入说明（供 AI 工具配置者查看）'),append(el('div','panel-body'),el('p','','MCP 是 AI 软件连接工具的通用协议。默认提供任务提案、状态查询、追加申请三个工具；没有批准预算、修改总额度或付款工具。'),el('p','field-help','配置生成方式见项目 ai_wallet/docs/agent-mcp.md。当前未自动注册到任何 AI 软件；旧本地测试入口不能作为支付保护边界。')));
  main.append(technical);
  const dev=el('details','panel details-panel section-gap');dev.id='developer-tools';
  dev.open=devToolsExpanded||Boolean(sessionStorage.getItem(pendingProposalStorageKey)||sessionStorage.getItem(pendingRequestStorageKey));
  dev.addEventListener('toggle',()=>{if(dev.isConnected)devToolsExpanded=dev.open;});
  dev.append(el('summary','details-toggle','开发验收工具（模拟数据，日常无需使用）'));
  if(state.allocation){const proposal=panel('测试 AI 预算提案','仅供开发验收；提交后仍需用户确认，不能自行分配预算',allocationProposalForm());proposal.id='proposal-start';dev.append(proposal);}
  const purchasePanel=panel('模拟 AI 采购','仅用于开发测试预算与异常处理，不连接账户',purchaseForm());purchasePanel.id='purchase-start';purchasePanel.classList.add('guide-target','section-gap');dev.append(purchasePanel,auditPanel());main.append(dev);
}
function purchaseForm() {
  const form=el('form','panel-body');
  let pendingRequest;
  try{pendingRequest=readPendingRequest();}
  catch(error){append(form,notice(error.message,'error'),button('核对交易记录 →',()=>navigate('orders'),'button-small'));return form;}
  // 未确认申请先独立核验：即使原任务已暂停或归档，也可用原编号取回原订单。
  if(pendingRequest){
    append(form,notice('上一笔创建请求尚未确认。核验前不能发起新采购；下面只取回或重试同一申请，不会自动执行付款。','warning'),el('p','order-info',`原任务：${taskName(pendingRequest.payload.task_id)}`),el('p','order-info',`原商品：${state.products.find(p=>p.id===pendingRequest.payload.product_id)?.title||pendingRequest.payload.product_id}`),el('p','order-reason',pendingRequest.payload.reason),append(el('div','order-actions'),button('核验并重试原申请',()=>act(async()=>{
      const original=readPendingRequest();
      if(!original||original.key!==pendingRequest.key)throw new Error('待核验申请已变化，请刷新后重新核对。');
      const data=await post('/api/orders',{...original.payload,idempotency_key:original.key});
      if(!data.order?.id)throw new Error('尚未确认原订单，继续保留原编号，请稍后重试。');
      forgetPendingRequest();delete drafts.purchase;
    },'原申请已确认，没有自动付款；请到待办或交易记录查看结果。'),'button-primary'),button('先核对交易记录 →',()=>navigate('orders'),'button-small')));
    return form;
  }
  const activeTasks=state.tasks.filter(t=>t.status==='active');
  if(!activeTasks.length){append(form,notice(state.allocation?'没有已确认预算且可采购的任务。请先提交 AI 预算提案，到待办确认分配；已暂停任务需要恢复后才能继续采购。':'没有可采购的任务。请先新建任务或恢复一个已暂停的任务；已归档和未知状态的任务不能采购。','warning'),button('前往 AI 与任务 →',()=>navigate('tasks'),'button-primary'));return form;}
  const taskSelect=select(activeTasks.map(t=>[t.id,t.title]));taskSelect.required=true;
  const productSelect=select(state.products.map(p=>[p.id,`${p.title} · ${money(p.price_cents)}`]));
  const scenarioSelect=select(Object.entries(scenarioLabels));
  const reason=el('textarea'); reason.name='reason'; reason.required=true; reason.maxLength=500; reason.value='为当前任务获取所需资源，完成工作交付。';
  const detail=el('div','product-detail');
  function updateProduct(){ const product=state.products.find(p=>p.id===productSelect.value);detail.replaceChildren();if(!product)return; const merchant=state.merchants.find(m=>m.id===product.merchant_id);append(detail,el('div','product-price',money(product.price_cents)),el('span','',`${merchant?merchant.name:product.merchant_id} · ${categoryLabels[product.category]||product.category}`),el('span','',`使用许可：${product.license||'未提供'}`),el('span','field-help',product.is_subscription?'订阅类商品：第一版规则应阻止订阅。':'演示目录商品，不代表真实商家报价或真实素材授权。')); }
  productSelect.addEventListener('change',updateProduct);updateProduct();
  const submit=el('button','button button-primary','模拟 AI 采购 →');submit.type='submit';
  append(form,field('关联任务',taskSelect),field('采购商品',productSelect),detail,field('购买理由',reason),field('本次支付模拟情景',scenarioSelect,state.allocation?'已确认任务预算内且符合规则的采购直接模拟执行；只有高级规则额外要求时才逐笔审批。异常情景只作用于本次直接执行的演示。':'小额通过规则后立即模拟执行；大额停在待办，由用户点击“批准并模拟执行”。异常情景选择只作用于本次直接执行的小额演示。'),append(el('div','form-footer'),el('p','field-help',state.allocation?'预算不足时 AI 需要提出追加预算申请，不能自行从尚可分配额度取用。':`单笔 ${money(state.policy.approval_threshold_cents)} 以内可自动执行，仍须符合所有规则。`),submit));
  form.addEventListener('submit',event=>{event.preventDefault();if(!form.reportValidity())return;if(!reason.value.trim()){showToast('请填写购买理由。',true);return;}act(async()=>{
    if(!state.tasks.some(t=>t.id===taskSelect.value&&t.status==='active'))throw new Error('该任务当前不可采购，请到 AI 与任务核对状态。');
    const payload={task_id:taskSelect.value,product_id:productSelect.value,reason:reason.value.trim()};
    if(/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/.test(payload.reason))throw new Error('购买理由包含不支持的控制字符，请移除后重新提交。');
    let request=readPendingRequest();
    if(request&&(JSON.stringify(request.payload)!==JSON.stringify(payload)||request.scenario!==scenarioSelect.value))throw new Error('有一笔创建结果未确认的采购。请核对交易记录，或使用原内容重试；不能直接改成新订单。');
    if(!request){request={key:crypto.randomUUID(),payload,scenario:scenarioSelect.value};sessionStorage.setItem(pendingRequestStorageKey,JSON.stringify(request));}
    let data;
    try { data=await post('/api/orders',{...request.payload,idempotency_key:request.key}); }
    catch(error){
      // 仅确认取得订单后清除：即使重试遭拒，也不能推断更早的请求未创建订单。
      error.message+=' 尚未取得确认订单，已保留原请求编号；请核对记录并用原内容重试，勿新建重复采购。';
      throw error;
    }
    const resultOrder=data.order;
    if(!resultOrder||!resultOrder.id)throw new Error('申请响应没有确认订单，已保留原请求编号。请核对交易记录并用原内容重试。');
    forgetPendingRequest();
    delete drafts.purchase;
    // 同一请求重试取回原订单；只有仍为 approved 的订单可继续执行，未知支付必须先核验。
    if(resultOrder.status==='approved')await post(`/api/orders/${encodeURIComponent(resultOrder.id)}/execute`,{scenario:request.scenario});
  },'模拟采购已处理，请查看订单的实际状态。');});
  wireDraft(form,'purchase');
  updateProduct();return form;
}
function taskBadge(task) {const [label,tone]=taskStatusLabels[task.status]||['未知状态 · 禁止采购','red'];return badge(label,tone);}
function taskOrders(task) {taskFilter=task.id;orderFilter='all';orderSearch='';navigate('orders');}
function readPendingTask() {
  const raw=sessionStorage.getItem(pendingTaskStorageKey);if(!raw)return null;
  try{const value=JSON.parse(raw);if(!value.key||!value.payload)throw new Error();return value;}
  catch{throw new Error('未确认的新建任务请求无法读取。请先核对任务列表，不要重复新建。');}
}
function taskText(input,label,maxLength) {
  const value=input.value.trim();
  if(!value||value.length>maxLength||/[\u0000-\u001f\u007f]/.test(value))throw new Error(`${label}不能为空，最多 ${maxLength} 个字符，且不能包含控制字符。`);
  return value;
}
function taskCreationForm() {
  const form=el('form','panel-body guide-target');form.id='task-create-start';
  const title=el('input');title.required=true;title.maxLength=120;title.placeholder='例如：秋季产品宣传资料';
  const agent=el('input');agent.maxLength=100;agent.placeholder='留空自动分配';
  const budget=el('input');budget.required=true;budget.inputMode='decimal';budget.value='100.00';
  const advanced=el('details','task-advanced');append(advanced,el('summary','','高级：自定义演示标识（可选）'),field('模拟 AI 标识',agent,'留空由程序分配，不需要了解 AI 标识格式；不会创建或连接真实 AI。'));
  append(form,field('任务名称',title),advanced,field('任务累计预算（元）',budget,'任务全生命周期累计上限，不按月重置。退款不恢复额度；钱包每日、每月及单笔规则仍同时生效。'));
  const submit=el('button','button button-primary','创建模拟任务');submit.type='submit';form.append(submit);
  wireDraft(form,'task-create');
  let pending;
  try{pending=readPendingTask();}catch(error){form.prepend(notice(error.message,'error'));}
  if(pending){title.value=pending.payload.title;agent.value=pending.payload.agent_id;budget.value=(pending.payload.budget_cents/100).toFixed(2);form.prepend(notice('上一笔新建请求尚未确认。已恢复原内容，同内容重试会取回原任务，不会重复新建。','warning'));submit.textContent='核验并重试原任务';}
  form.addEventListener('submit',async event=>{
    event.preventDefault();if(busy||refreshing||!form.reportValidity())return;
    try{
      if(!agent.value.trim())agent.value=`demo-${crypto.randomUUID()}`;
      const payload={title:taskText(title,'任务名称',120),agent_id:taskText(agent,'模拟 AI 标识',100),budget_cents:centsFromInput(budget,'任务预算')};
      if(payload.budget_cents>100000000)throw new Error('任务预算最多为 1,000,000 元。');
      let pendingRequest=readPendingTask();
      if(pendingRequest&&JSON.stringify(pendingRequest.payload)!==JSON.stringify(payload))throw new Error('有一笔新建任务尚未确认，请用原内容重试并核对任务列表，不要直接改成另一个任务。');
      if(!await confirmAction(`为“${payload.title}”分配累计预算 ${money(payload.budget_cents)}。这只创建本地任务，不连接真实 AI、不转账，也不会自动采购。`,'创建模拟任务'))return;
      await act(async()=>{
        if(!pendingRequest){pendingRequest={key:crypto.randomUUID(),payload};sessionStorage.setItem(pendingTaskStorageKey,JSON.stringify(pendingRequest));}
        const data=await post('/api/tasks',{...pendingRequest.payload,idempotency_key:pendingRequest.key});
        if(!data.task?.id)throw new Error('尚未取得确认任务，已保留原请求编号，请用原内容重试。');
        sessionStorage.removeItem(pendingTaskStorageKey);delete drafts['task-create'];
      },'模拟任务已创建，可在任务详情查看和管理。');
    }catch(error){showToast(error.message,true);}
  });return form;
}
function renderTasks() {
  if(state.allocation)return renderAllocationTasks();
  main.append(heading('每个 AI 的任务，都有消费边界','创建任务、分配累计预算，随时暂停。AI 标识只是模拟主体名称，尚未连接真实 AI。','AI & TASKS'));
  const id=route().id;
  if(id){main.append(button('← 返回任务列表',()=>navigate('tasks'),'order-detail-back'));const task=state.tasks.find(t=>t.id===id);if(!task){main.append(empty('没有找到这个任务，请返回任务列表核对。'));return;}renderTaskDetail(task);return;}
  const list=el('div','panel-body task-management-list');
  state.tasks.forEach(task=>{
    const card=el('article','task-management-card');
    append(card,append(el('div','order-title-row'),el('h3','',task.title),taskBadge(task)),el('p','task-meta',`${task.agent_id} · 未连接真实 AI`),append(el('div','task-budget-line'),el('span','',`剩余 ${money(task.available_cents)}`),el('span','',`累计预算 ${money(task.budget_cents)}`)),progress(task.spent_cents+task.reserved_cents,task.budget_cents),el('p','field-help',`已消费 ${money(task.spent_cents)} · 预留 ${money(task.reserved_cents)}`),append(el('div','order-actions'),button('任务详情',()=>navigate('tasks',task.id),'button-small'),button('查看任务交易',()=>taskOrders(task),'button-ghost button-small')));list.append(card);
  });
  if(!state.tasks.length)list.append(empty('还没有任务。在右侧创建第一个模拟任务后，就能体验采购流程。'));
  main.append(append(el('div','task-management-grid'),panel('任务列表',`共 ${state.tasks.length} 个任务，${state.tasks.filter(t=>t.status==='active').length} 个可采购`,list),panel('新建任务','分配权限额度，不是充值或向 AI 转账',taskCreationForm())));
}
function renderTaskDetail(task) {
  const summary=el('div','panel-body');
  append(summary,append(el('div','order-title-row'),el('h2','',task.title),taskBadge(task)),el('p','field-help',`模拟主体 ${task.agent_id} · 任务 ${task.id} · 版本 ${task.revision}`),append(el('div','task-budget-line'),el('strong','',`剩余 ${money(task.available_cents)}`),el('span','',`累计预算 ${money(task.budget_cents)}`)),progress(task.spent_cents+task.reserved_cents,task.budget_cents),el('p','field-help',`已消费 ${money(task.spent_cents)} · 预留 ${money(task.reserved_cents)}。退款不恢复累计预算。`),button('查看此任务的全部交易 →',()=>taskOrders(task),'button-small'));
  main.append(panel('任务详情','仅本地模拟；标识不代表已经授权或连接外部 AI',summary));
  if(!['active','paused'].includes(task.status)){main.append(notice(task.status==='archived'?'任务已归档，不能恢复或继续采购。历史交易仍可查看、核验和处理。':'任务状态无法识别，不能修改或采购，请刷新并核对服务版本。','warning'));return;}
  const form=el('form','panel-body');
  const title=el('input');title.value=task.title;title.required=true;title.maxLength=120;
  const budget=el('input');budget.value=(task.budget_cents/100).toFixed(2);budget.required=true;budget.inputMode='decimal';
  append(form,append(el('div','field-grid'),field('任务名称',title),field('累计预算（元）',budget)),notice('修改名称或预算不会取消已有申请，也不会重置已消费金额。预算不能低于已消费与预留额度之和；已存在订单保留原任务名称快照。'));
  const key=`task-edit-${task.id}`;const revision=wireDraft(form,key,task.revision);
  if(revision!==task.revision)form.prepend(notice('任务已在其他页面修改。你的草稿已保留，提交前请放弃草稿并核对最新版本。','warning'));
  const save=el('button','button button-primary','保存任务修改');save.type='submit';
  append(form,append(el('div','order-actions'),save,button('放弃草稿，读取最新任务',async()=>{if(await confirmAction('放弃未保存的任务名称和预算，重新读取当前任务？','读取最新任务')){delete drafts[key];await refreshState(true);render();}})));
  form.addEventListener('submit',async event=>{
    event.preventDefault();if(busy||refreshing||!form.reportValidity())return;
    try{
      const body={title:taskText(title,'任务名称',120),budget_cents:centsFromInput(budget,'累计预算'),expected_revision:revision};
      if(body.budget_cents>100000000)throw new Error('累计预算最多为 1,000,000 元。');
      if(body.title===task.title&&body.budget_cents===task.budget_cents){showToast('任务没有变化，无需保存。');return;}
      if(!await fetchState())throw new Error('状态正在更新，请稍后重新保存。');updateShell();
      const current=state.tasks.find(t=>t.id===task.id);
      if(!current||current.revision!==revision)throw new Error('任务版本已变化，草稿已保留。请读取最新任务后重新核对。');
      if(body.budget_cents<current.spent_cents+current.reserved_cents)throw new Error('累计预算不能低于已消费金额与预留额度之和。');
      if(await confirmAction(`将任务名称保存为“${body.title}”，累计预算改为 ${money(body.budget_cents)}。已消费 ${money(current.spent_cents)} 和预留 ${money(current.reserved_cents)} 不会重置。修改名称和预算不取消现有申请；已有订单保留原任务名称快照。`,'核对任务修改'))await act(async()=>{await post(`/api/tasks/${encodeURIComponent(task.id)}`,body);delete drafts[key];},'任务修改已保存，请核对预算及关联订单。');
    }catch(error){showToast(error.message,true);}
  });
  const actions=el('div','panel-body');
  append(actions,el('p','field-help','暂停将阻止新采购和继续付款；恢复只允许后续符合规则的采购，不会自动执行。归档不可恢复，历史记录保留。'),append(el('div','order-actions'),button(task.status==='active'?'暂停此任务':'恢复此任务',()=>changeTaskStatus(task,task.status==='active'?'paused':'active'),'button-small'),button('归档任务',()=>changeTaskStatus(task,'archived'),'button-danger button-small')));
  const editPanel=panel('名称与累计预算','模拟主体标识创建后不能更换；需要其他主体时创建新任务',form);editPanel.classList.add('section-gap');
  const actionPanel=panel('任务权限','只影响此任务，不会暂停整个钱包',actions);actionPanel.classList.add('section-gap');main.append(editPanel,actionPanel);
}
async function changeTaskStatus(task,statusValue) {
  if(busy||refreshing)return;
  const names={paused:'暂停',active:'恢复',archived:'归档'};
  const note=statusValue==='archived'?'归档不可恢复，不能继续采购；历史交易仍可查询。':statusValue==='paused'?'暂停后不能新建采购或继续付款；恢复时需要重新申请已失效的订单。':'恢复后允许符合规则的新采购，不会自动执行订单。';
  if(!await confirmAction(`${names[statusValue]}“${task.title}”？${note}${statusValue!=='active'?'本任务待审批和已批准但未付款的申请会被取消，释放预留额度。':''}已付款记录保留；未知支付继续保留额度，仍需核验。`,`${names[statusValue]}任务`))return;
  await act(()=>post(`/api/tasks/${encodeURIComponent(task.id)}`,{status:statusValue,expected_revision:task.revision}),`任务状态已更新，请核对关联交易。`);
}
function orderCard(order) {
  const card=el('article','order-card');
  const title=append(el('div','order-title'),document.createTextNode(order.product_title),el('small','',order.merchant_name));
  append(card,append(el('div','order-title-row'),title,el('div','order-price',money(order.amount_cents))),el('p','order-reason',order.reason),status(order),el('div','order-info',`${order.task_title||taskName(order.task_id)} · ${date(order.created_at)}`));
  if(order.decision_reason)card.append(el('div','order-info',`处理说明：${order.decision_reason}`));
  if(order.expires_at&&['approved','pending_approval'].includes(order.status))card.append(el('div','order-info',`申请有效至 ${date(order.expires_at)}；跨日或更改授权后需重新申请。`));
  const records=el('details','order-more');records.open=route().id===order.id;
  append(records,el('summary','','订单编号与回执'),el('div','order-info',`订单 ${order.id} · 授权版本 ${order.policy_revision}`));
  if(order.receipt)records.append(el('div','order-info',`模拟回执：${order.receipt.id||'已记录'} · ${money(order.receipt.amount_cents)}${order.receipt.confirmed_at?` · ${date(order.receipt.confirmed_at)}`:''}`));
  if(order.refund_receipt)records.append(el('div','order-info',`模拟退款回执：${order.refund_receipt.id} · ${money(order.refund_receipt.amount_cents)} · ${date(order.refund_receipt.confirmed_at)}`));
  if(order.delivery)records.append(el('div','order-info',`模拟交付：${order.delivery.title||''}${order.delivery.content?` · ${order.delivery.content}`:''}`));
  card.append(records);
  const actions=el('div','order-actions');
  const path=`/api/orders/${encodeURIComponent(order.id)}`;
  const hostManaged=Boolean(order.checkout||order.dispatch);
  if(hostManaged){
    // 网站结账必须走原受控执行链路，不能借页面按钮调用固定目录的模拟结算。
    if(order.status==='pending_approval')append(actions,
      button('批准此笔采购',()=>act(()=>post(`${path}/approve`),'已提交批准，请核对原订单；付款仍需执行程序复核。'),'button-primary button-small'),
      button('拒绝',()=>act(()=>post(`${path}/reject`),'已拒绝此笔采购。'),'button-small'),
      el('p','field-help','这里只确认采购授权；不会直接提交网站付款。'));
    if(order.status==='approved')append(actions,
      el('p','field-help','等待付款执行程序核对原结账页面。当前页面不能代为提交；无需重新购买。'),
      button('取消采购',async()=>{if(await confirmAction(`取消这笔 ${money(order.amount_cents)} 的未付款采购？若已进入付款流程，程序会保留原单供核对。`,'取消未付款采购'))await act(()=>post(`${path}/reject`),'取消请求已处理，请核对原单状态。');},'button-small'));
    if(order.status==='unknown')actions.append(el('p','field-help','等待付款执行程序核对原网站订单和支付记录。额度继续保留；刷新只读取记录，不会再次付款，也不会自动判定成功。'));
    if(order.status==='paid'&&!order.delivery)actions.append(el('p','field-help','已记录付款，尚未确认素材交付。请保留原订单，等待核对成果；不要再次购买。'));
    if(['paid','delivery_failed'].includes(order.status))actions.append(el('p','field-help','需要退款或处理交付问题时，请在原购买网站处理并核对原单；这里尚不能提交网站退款。'));
    actions.append(button('刷新原单状态',()=>refreshState(true),'button-small'));
  } else {
  if(order.status==='pending_approval')append(actions,button('批准并模拟执行',()=>act(async()=>{
    const approved=await post(`${path}/approve`);
    if(approved.order?.id!==order.id||approved.order?.status!=='approved')throw new Error('尚未确认批准结果，请核对原订单；没有继续执行。');
    await post(`${path}/execute`,{scenario:'success'});
  },'批准和模拟执行已处理，请核对订单最终状态。'),'button-primary button-small'),button('拒绝',()=>act(()=>post(`${path}/reject`),'已拒绝此笔采购。'),'button-small'),el('p','field-help','点击批准会继续执行这笔模拟采购，不需再次确认；最终状态以执行结果为准。'));
  if(order.status==='approved') {const scenario=select(Object.entries(scenarioLabels),drafts[`scenario-${order.id}`]||'success');scenario.setAttribute('aria-label',`${order.product_title} 的支付模拟情景`);scenario.addEventListener('change',()=>drafts[`scenario-${order.id}`]=scenario.value);append(actions,scenario,button('模拟执行',()=>act(()=>post(`${path}/execute`,{scenario:scenario.value}),'执行结果已更新，请核对订单状态。'),'button-primary button-small'),button('取消采购',async()=>{if(await confirmAction(`取消这笔 ${money(order.amount_cents)} 的未付款采购？成功取消后释放预留额度。`,'取消未付款采购'))await act(()=>post(`${path}/reject`),'采购已取消，预留额度已释放。');},'button-small'));}
  if(order.status==='unknown')append(actions,button('核验模拟支付结果',()=>act(()=>post(`${path}/reconcile`),'本演示按支付成功恢复记录并交付；未再次扣款。'),'button-primary button-small'),el('div','field-help','结果未知时保留额度；核验前不会重复付款。本演示核验结果固定为已支付。'));
  if(['paid','delivery_failed'].includes(order.status))actions.append(button('模拟退款',async()=>{if(await confirmAction(`确认模拟退回这笔 ${money(order.amount_cents)} 的采购？这只更新本地演示记录，不会向真实商家退款。为防止循环消费，退款不恢复累计预算额度。`,'确认模拟退款'))await act(()=>post(`${path}/refund`),'模拟退款已记录；累计消费额度不恢复。');},'button-small'));
  }
  if(route().id!==order.id)actions.append(button('查看详情',()=>navigate('orders',order.id),'button-ghost button-small'));
  else actions.append(button('记录问题',()=>{supportOrderId=order.id;delete drafts['support-create'];navigate('operations');},'button-small'));
  if(actions.childNodes.length)card.append(actions);
  return card;
}
function renderInbox() {
  if(state.allocation){main.append(heading('确认任务预算，处理例外','预算确认后，AI 可以在任务额度和授权范围内采购；需要追加时再次向你申请。','ACTION CENTER'));const budgets=el('div','panel-body');budgetProposals().forEach(task=>budgets.append(budgetDecisionCard(task)));if(!budgetProposals().length)budgets.append(empty('没有待确认的任务预算。'));main.append(panel('待确认预算','确认只是划分消费权限，不是立即付款',budgets));const orders=el('div','panel-body');state.orders.filter(needsAttention).forEach(order=>orders.append(orderCard(order)));if(!orders.children.length)orders.append(empty('没有需要处理的采购。'));main.append(panel('采购例外与异常','如设置了高级单笔审批规则，相关申请也会出现在这里',orders));return;}
  main.append(heading('需要处理的事，都在这里','核对商品、理由和金额后，一次点击批准并模拟执行；异常仍会保留。','ACTION CENTER'));
  const list=el('div','panel order-page-list');
  const orders=state.orders.filter(needsAttention);
  orders.forEach(order=>list.append(orderCard(order)));
  if(!orders.length)list.append(empty('待办已清空。已完成、拒绝或支付失败的记录可在交易记录中查看。'));
  main.append(list);
}
function renderOrders() {
  main.append(heading('每笔支出，都有来处','查看授权、支付和交付结果；未知支付先核验，避免重复付款。','TRANSACTION HISTORY'));
  main.append(button('导出本地记录 JSON（可读数据文件）',exportRecords,'button-small order-detail-back'));
  const detailId=route().id;
  if(detailId){main.append(button('← 返回交易列表',()=>navigate('orders'),'order-detail-back'));const order=state.orders.find(o=>o.id===detailId);main.append(order?append(el('section','panel order-page-list'),orderCard(order)):empty('没有找到该订单。请检查链接或返回交易列表。'));return;}
  const search=el('input');search.type='search';search.placeholder='商品、商家或订单号';search.value=orderSearch;
  const tasks=select([['all','全部任务'],...state.tasks.map(t=>[t.id,t.title])],taskFilter);
  const searchForm=el('form','order-tools');
  const submit=el('button','button','查询');submit.type='submit';
  append(searchForm,field('搜索交易',search),field('关联任务',tasks),submit,button('清除筛选',()=>{orderSearch='';taskFilter='all';orderFilter='all';render();}));
  search.addEventListener('input',()=>orderSearch=search.value);tasks.addEventListener('change',()=>taskFilter=tasks.value);
  searchForm.addEventListener('submit',event=>{event.preventDefault();render();});main.append(searchForm);
  const filters=el('div','filter-row');
  [['all','全部'],['pending_approval','待审批'],['approved','待执行'],['attention','需要处理'],['paid','已付款'],['refunded','已退款'],['rejected','已拒绝 / 取消']].forEach(([id,label])=>{const b=button(label,()=>{orderFilter=id;render();});b.className=`filter-button${orderFilter===id?' selected':''}`;filters.append(b);});
  main.append(filters);
  const list=el('div','panel order-page-list');
  const query=orderSearch.trim().toLocaleLowerCase();
  const orders=state.orders.filter(o=>(orderFilter==='all'||(orderFilter==='attention'?(['unknown','delivery_failed','failed'].includes(o.status)||awaitingDelivery(o)):o.status===orderFilter))&&(taskFilter==='all'||o.task_id===taskFilter)&&(!query||[o.id,o.product_title,o.merchant_name,o.task_title,taskName(o.task_id)].join(' ').toLocaleLowerCase().includes(query)));
  orders.forEach(order=>list.append(orderCard(order)));if(!orders.length)list.append(empty('这个分类暂时没有交易记录。'));
  main.append(list,auditPanel());
}
function centsFromInput(input,label) {
  const raw=input.value.trim();
  if(!/^(0|[1-9]\d*)(\.\d{1,2})?$/.test(raw))throw new Error(`${label}请输入非负金额，最多保留两位小数。`);
  const [whole,fraction='']=raw.split('.');
  const cents=Number(whole)*100+Number(fraction.padEnd(2,'0'));
  if(!Number.isSafeInteger(cents))throw new Error(`${label}超出了可支持范围。`);
  return cents;
}
function renderPolicy() {
  main.append(heading('明确授权，放心交给 AI','规则由本地程序执行，AI 不能提高额度或绕过审批。','SPENDING POLICY'));
  if(state.allocation)main.append(panel('AI 可操控总额度','先确定允许 AI 使用多少，再按任务确认分配',allocationPoolForm()));
  main.append(notice('修改规则会使待处理申请失效，需要按新规则重新申请。已产生的支出、未知支付及其预留额度仍须核对。','warning'));
  const form=el('form','panel-body guide-target');form.id='policy-start';const fields={};
  const grid=el('div','field-grid');
  [['monthly_limit_cents','每月总上限'],['daily_limit_cents','每日总上限'],['per_payment_limit_cents','单笔绝对上限'],['approval_threshold_cents','单笔自动审批门槛']].forEach(([key,label])=>{const input=el('input');input.type='text';input.inputMode='decimal';input.required=true;input.value=(state.policy[key]/100).toFixed(2);fields[key]=[input,label];grid.append(field(`${label}（元）`,input));});
  if(state.allocation){const advanced=el('details','task-advanced');append(advanced,el('summary','','高级：单笔、每日与每月限制'),el('p','field-help','日常以总额度和任务预算为主。这些限制是额外保护，不会扩大任务预算；设置较低的自动审批门槛会增加逐笔待办。'),grid);form.append(advanced);}else form.append(grid);
  form.append(el('p','field-help','超过自动审批门槛需要你批准；超过单笔、任务、每日或每月总上限则拒绝，审批不会自动扩大预算。'));
  const categoryBox=el('div','check-list'); const categoryInputs={};
  Object.entries(categoryLabels).forEach(([id,name])=>{const input=el('input');input.type='checkbox';input.checked=state.policy.allowed_categories.includes(id);categoryInputs[id]=input;categoryBox.append(append(el('label','checkbox'),input,document.createTextNode(name)));});
  const merchantBox=el('div','check-list');const merchantInputs={};
  state.merchants.filter(merchant=>['studio','insight','compute'].includes(merchant.id)).forEach(merchant=>{const input=el('input');input.type='checkbox';input.checked=state.policy.allowed_merchant_ids.includes(merchant.id);merchantInputs[merchant.id]=input;merchantBox.append(append(el('label','checkbox'),input,document.createTextNode(merchant.name)));});
  merchantBox.append(el('p','field-help','外部演示商店仅用于验证拒绝规则，当前不支持授权。'));
  const allowed=el('div','field-grid section-gap');append(allowed,append(el('div','field'),el('span','field-label','允许的商品类别'),categoryBox),append(el('div','field'),el('span','field-label','允许的商家'),merchantBox));form.append(allowed);
  const save=el('button','button button-primary','保存授权规则');save.type='submit';
  append(form,notice('商品类别与商家必须同时获准。第一版不允许订阅，不向其他 AI 转授权限。'),append(el('div','form-footer'),el('span','field-help',`当前授权版本 ${state.policy.revision}`),button('放弃草稿，读取最新规则',async()=>{if(await confirmAction('放弃尚未保存的规则修改，并读取最新规则？','重新读取规则')){delete drafts.policy;await refreshState(true);render();}}),save));
  const policyRevision=wireDraft(form,'policy');
  form.addEventListener('submit',async event=>{
    event.preventDefault();if(busy||refreshing)return;
    try {
      const body={};Object.entries(fields).forEach(([key,[input,label]])=>body[key]=centsFromInput(input,label));
      if(body.approval_threshold_cents>body.per_payment_limit_cents||body.per_payment_limit_cents>body.daily_limit_cents||body.daily_limit_cents>body.monthly_limit_cents)throw new Error('金额关系应为：自动审批门槛 ≤ 单笔上限 ≤ 每日上限 ≤ 每月上限。');
      body.allowed_categories=Object.entries(categoryInputs).filter(([,input])=>input.checked).map(([id])=>id);
      body.allowed_merchant_ids=Object.entries(merchantInputs).filter(([,input])=>input.checked).map(([id])=>id);
      if(!await fetchState())throw new Error('状态正在更新，请稍后重新保存。');
      updateShell();
      if(state.policy.revision!==policyRevision)throw new Error('规则已在其他页面更新。草稿已保留，请先点击“放弃草稿，读取最新规则”并重新核对。');
      const changed=Object.keys(body).some(key=>JSON.stringify(body[key])!==JSON.stringify(state.policy[key]));
      if(!changed){delete drafts.policy;showToast('规则没有变化，无需保存。');return;}
      const affected=state.orders.filter(o=>['pending_approval','approved'].includes(o.status));
      const describe=affected.slice(0,5).map(o=>`${taskName(o.task_id)}：${o.product_title} ${money(o.amount_cents)}`).join('；');
      const message=`本次修改将使 ${affected.length} 笔未付款申请失效，释放预留额度 ${money(affected.reduce((sum,o)=>sum+o.amount_cents,0))}。${describe?`涉及：${describe}${affected.length>5?'等':''}。`:''}即使只提高预算，当前版本也会取消这些申请，需要重新申请。已付款和结果未知的订单保留。此为当前快照，最终以保存时账本为准。`;
      if(await confirmAction(message,'核对规则变更影响'))await act(async()=>{
        await fetchState();
        if(state.policy.revision!==policyRevision)throw new Error('确认期间规则已变化，本次未保存。请读取最新规则后重试。');
        // 订单集合变化后要求重新预览，避免按旧清单确认。
        const current=state.orders.filter(o=>['pending_approval','approved'].includes(o.status)).map(o=>o.id).sort().join(',');
        if(current!==affected.map(o=>o.id).sort().join(','))throw new Error('待付款订单已变化，本次未保存。请再次保存，核对新的影响清单。');
        await post('/api/policy/commit',{changes:body,expected_revision:policyRevision,expected_pending_ids:affected.map(o=>o.id)});delete drafts.policy;
      },'授权规则已保存，请在交易记录中核对失效申请。');
    }catch(error){showToast(error.message,true);}
  });
  const explanation=el('div','panel-body');const rules=el('ol','rule-list');
  [['先确认身份与用途','订单关联指定的演示 AI 和任务，价格从本地商品目录读取。'],['再检查全部消费边界','商家、类别、订阅限制和所有额度都要符合规则。'],['预留额度，避免同时超支','申请占用可用额度，支付失败或拒绝后释放；结果未知时继续保留。'],['审批和支付分别记录','批准只允许这笔订单继续执行，不修改任何总预算。']].forEach(([title,copy],index)=>rules.append(append(el('li'),el('span','step-number',index+1),append(el('div'),el('strong','',title),el('p','',copy)))));explanation.append(rules);
  main.append(append(el('div','policy-grid'),panel('消费授权','金额均为人民币；这是支出权限，不是充值余额',form),panel('一笔采购如何通过检查','全部规则同时生效',explanation)),auditPanel());
}
function renderOperations() {
  main.append(heading('使用帮助与问题处理','先找到原因，再处理原订单；不用反复尝试同一笔采购。','HELP & SUPPORT'));
  main.append(notice('当前只有本地模拟：没有真实扣款、素材下载或 AI 连接，也未开放付费服务。本地问题记录不会发送给客服。','warning'));
  const actions=append(el('div','help-actions'),button('从三步引导开始',()=>{guideExpanded=true;navigate('overview');document.querySelector('.start-guide')?.scrollIntoView({block:'start'});},'button-primary'),button('前往待办处理采购',()=>navigate('inbox')),button('查找已有交易',()=>navigate('orders')),button('前往AI 接入',()=>navigate('integration')));
  main.append(actions);
  // 从订单详情进入时先显示已关联的问题表单，避免帮助内容把原操作挤到页尾。
  if(supportOrderId)main.append(supportPanel());
  const body=el('div','panel-body');
  const search=el('input');search.type='search';search.maxLength=100;search.value=helpQuery;search.placeholder='例如：退款、暂停、下载、预算';search.setAttribute('aria-label','搜索帮助问题');
  const count=el('p','field-help');count.setAttribute('role','status');count.setAttribute('aria-live','polite');
  const list=el('div','help-topics');
  const draw=()=>{
    list.replaceChildren();const query=helpQuery.trim().toLocaleLowerCase('zh-CN');
    const matches=(state.allocation?allocationHelpTopics():helpTopics).filter(topic=>`${topic.title} ${topic.answer} ${topic.keywords}`.toLocaleLowerCase('zh-CN').includes(query));
    count.textContent=query?`找到 ${matches.length} 个相关问题`:`共 ${matches.length} 个常见问题，点击问题展开`;
    for(const topic of matches){
      const item=el('details','help-topic');item.open=expandedHelp.has(topic.id);
      item.addEventListener('toggle',()=>{if(item.isConnected){if(item.open)expandedHelp.add(topic.id);else expandedHelp.delete(topic.id);}});
      append(item,el('summary','',topic.title),el('p','',topic.answer));list.append(item);
    }
    if(!matches.length)append(list,empty('没有找到相关问题。试试“预算”“付款”或“退款”，也可以在下方记录原订单的问题。'),button('清空帮助搜索',()=>{helpQuery='';search.value='';draw();search.focus();},'button-small'));
  };
  search.addEventListener('input',()=>{helpQuery=search.value;draw();});
  append(body,field('搜索常见问题',search),count,list);draw();
  main.append(panel('常见问题','说明当前版本的实际行为，不代表真实平台的许可或退款承诺',body));
  if(!supportOrderId)main.append(supportPanel());
}

const helpTopics=[
  {id:'start',title:'第一次打开，应该先做什么？',keywords:'开始 新手 教程 创建任务',answer:'点击首页“新增任务”，填写用途和累计预算，再核对授权规则。日常在待办处理需要你决定的采购，在交易记录查结果，无需替AI选商品或填写技术标识。要体验采购流程，进入“AI 接入”。目前尚未连接真实 AI，预置任务和商家均为演示内容。'},
  {id:'balance',title:'剩余可用额度是我的钱吗？需要充值吗？',keywords:'余额 提现 预留 冻结',answer:'额度是你允许这个程序使用的支出上限，不是现金余额，不能充值或提现。待审批、已批准未付款和结果未知的订单会预留额度，避免其他订单把同一份预算用掉。你可以在待办和交易详情核对这些订单。'},
  {id:'rejected',title:'额度看起来够，为什么采购还是被拒绝？',keywords:'预算 超限 商家 类别 订阅 范围 权限',answer:'采购必须同时满足任务预算、单笔上限、每日和每月上限，商家与商品类别也必须同时获准。已有预留会占用额度；任务暂停、报价过期或订阅类商品也可能被拒绝。查看该订单的拒绝原因，再到任务或授权规则核对，不要仅凭首页剩余额度判断。'},
  {id:'approval',title:'批准后会怎样？为什么有时仍未付款？',keywords:'审批 执行 大额 小额',answer:'点击“批准并模拟执行”会先批准原单，再继续执行，用户只点一次；它不保证执行一定成功。如果中途断开，可能保留为已批准未执行或结果未知，应先核对原单。旧的已批准未付款订单仍有执行入口。小额申请符合全部规则可自动模拟执行；批准不能突破预算。'},
  {id:'unknown',title:'显示“结果未知”或请求超时，应该怎么办？',keywords:'重复 重试 网络失败 扣款',answer:'先在待办或交易记录找到原订单并核验，不要新建一笔相同采购。结果未知时继续保留额度，跨日或跨月也不会自动释放。如果创建申请还未确认，首页会保留原请求的核验入口。当前模拟核验会返回预设结果，不代表真实渠道已查账。'},
  {id:'delivery',title:'显示已付款但交付失败，如何处理？',keywords:'下载 文件 素材 没收到 售后',answer:'交付失败不能当作没付款，支出仍需保留。先打开原订单详情核对支付与交付记录，再使用“记录问题”保存情况。当前可演示退款，但没有真实素材文件或重新下载能力；真实平台的重新交付和退款条件还未确认。'},
  {id:'pause',title:'暂停会取消什么？恢复后会自动继续吗？',keywords:'撤销 停止 归档 恢复',answer:'暂停整个钱包会阻止新的采购和执行，并使未付款的待审批、已批准申请失效；已付款和结果未知的订单仍需核对。单独暂停任务只影响该任务。恢复不会恢复旧批准，也不会自动退款。此处暂停不能阻止你在其他网站直接消费。任务归档后当前版本不可恢复。'},
  {id:'refund',title:'退款后，为什么可用额度没有增加？',keywords:'退钱 累计 消费 限额',answer:'当前规则按累计消费限制支出，退款不会恢复任务、每日或每月的累计消费额度，避免反复购买和退款绕过上限。退款会单独记录回执；“已模拟退款”仅为演示结果。没有付款的申请被拒绝或取消时，其预留额度才会释放。'},
  {id:'scope',title:'能给不同任务设置不同的购买范围吗？',keywords:'AI 商店 图片 视频 独立权限',answer:'当前可以分别设置任务预算、暂停或归档任务，但允许的商家、商品类别和单笔审批门槛是全局规则，所有任务共用。当前还不能单独限定某个任务只买图片；图片与视频的真实采购能力也尚未接通。'},
  {id:'changes',title:'修改授权规则，会影响已有申请吗？',keywords:'保存 提高金额 取消 失效',answer:'当前版本只要修改全局授权规则，即使提高预算，也会使未付款的申请失效，需要重新申请。保存前会列出受影响订单和释放的预留额度。已付款、结果未知的订单保留。只修改任务名称或预算不会取消已有申请，但预算不能低于已消费和预留之和。'},
  {id:'support',title:'记录问题后，会有人联系我吗？',keywords:'客服 人工 处理进度 已解决',answer:'不会自动联系。当前入口只把问题和手动填写的处理进度保存在本机，没有发送客服工单或启动 AI 处理。把问题标为“已解决”不会改变支付或退款状态；处理资金仍应核对原订单。不要填写密码、银行卡或其他敏感信息。'},
  {id:'records',title:'在哪里查文件和消费记录？关闭页面会丢失吗？',keywords:'导出 账单 数据 保存 备份 JSON',answer:'在交易记录中按任务、状态或关键词查订单，点击详情查看已有回执。当前交付回执是模拟说明，不是真实素材文件或商业许可。已保存的订单和问题记录关闭页面后仍在本机；未提交的表单不保证在关页后保留。导出的 JSON 是可读记录文件，不是正式账单，也不是可直接恢复的备份。'},
];
function allocationHelpTopics(){
  const answers={start:'先设置 AI 可操控总额度。AI 提出任务和建议预算，你可以修改并确认金额，确认后才从尚可分配额度中划分给任务。AI 接入口目前提供本地预算管理，支付工具尚未接通；提交提案不会自行授权或付款。',balance:'总额度是允许 AI 使用的累计支出上限，不是钱包内存放的现金。总额度等于尚可分配、任务尚未消费的额度、累计已消费三部分之和。任务尚未消费的额度包括任务可用金额和付款占用，不能再分给其他任务。当前不连接账户、不能充值或提现。',approval:'任务预算确认是日常主要审批：预算内且符合授权范围的采购可以直接模拟执行。追加预算必须再次确认，AI 不能挪用其他任务预算。高级规则如设置了较低的单笔审批门槛，会额外产生逐笔待办；“批准并模拟执行”处理这类采购，仍不能突破任务预算。',pause:'暂停保留任务预算并阻止继续采购，未付款申请会取消；结果未知的付款继续占用。结束任务会释放未使用且可释放的额度，未知付款核验后再结算，已消费不退回。结束不可恢复；暂停任务可恢复，但不自动恢复旧申请或执行采购。',scope:'可以按任务确认独立预算并暂停或结束任务。商家、类别和高级单笔限制仍是所有任务共用的授权范围；当前未实现每个任务各自的商家名单，也未接通真实图片或视频采购。',changes:'调整 AI 总额度不会自动扩大任务预算。任务预算只能通过提案、用户确认后改变；新任务提案确认前不可采购，追加提案等待期间保留旧预算。确认总预算可以低于 AI 建议金额，但不能低于已确认预算或已消费和付款占用之和，也不能超出可分配额度。当前调减未用预算通过结束任务释放。修改全局商家、类别或高级规则会使未付款申请失效，保存前会显示影响。'};
  return helpTopics.map(topic=>({...topic,answer:answers[topic.id]||topic.answer}));
}

async function exportRecords() {
  if(busy)return;
  await act(async()=>{
    const {response,data}=await request('/api/export',{cache:'no-store'});
    if(response.status===401){showLogin();const error=new Error('请登录后导出本地记录。');error.code='login_required';throw error;}
    if(!response.ok)throw new Error(data.error||'导出失败，请重试。');
    const blob=new Blob([JSON.stringify(data,null,2)],{type:'application/json;charset=utf-8'});
    const url=URL.createObjectURL(blob);
    const link=el('a');link.href=url;link.download=`yuxing-demo-records-${new Date().toISOString().slice(0,10)}.json`;link.hidden=true;document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),5000);
  },'已生成本地 JSON 下载；内容为模拟记录，不是正式账单或可恢复备份。');
}
async function loadSupportTickets() {
  if(supportLoading||!state)return;
  const sequence=++supportSequence;
  supportLoading=true;
  try{
    const {response,data}=await request('/api/support/tickets',{cache:'no-store'});
    if(sequence!==supportSequence)return;
    if(response.status===401){showLogin();return;}
    if(!response.ok)throw new Error(data.error||'无法读取本地问题记录。');
    if(!state)return;
    supportTickets=data.tickets||[];supportLoaded=true;supportError='';
  }catch(error){if(sequence===supportSequence){supportError=error.message;supportLoaded=true;}}
  finally{if(sequence===supportSequence){supportLoading=false;if(state&&activePage==='operations'){deferredRender=true;flushDeferredRender();}}}
}
function readPendingTicket() {
  const raw=sessionStorage.getItem(pendingTicketStorageKey);if(!raw)return null;
  try{const value=JSON.parse(raw);if(!value.key||!value.payload)throw new Error();return value;}
  catch{throw new Error('未确认的问题记录无法读取，请先核对已有记录，不要重复提交。');}
}
function supportPanel() {
  const body=el('div','panel-body');
  body.append(notice('这些记录只保存到本机，未发送给客服或商家，也没有后台 AI 正在处理。状态和解决说明由你在本页面手动记录。'));
  if(!supportLoaded&&!supportLoading)loadSupportTickets();
  if(supportError)body.append(notice(supportError,'error'),button('重新读取问题记录',()=>{supportLoaded=false;supportError='';render();},'button-small'));
  if(state.orders.length){
    const form=el('form','support-create-form');
    const order=select(state.orders.map(o=>[o.id,`${o.product_title} · ${money(o.amount_cents)} · ${o.id}`]),supportOrderId||state.orders[0].id);order.required=true;
    const category=select([['payment','付款问题'],['delivery','交付问题'],['refund','退款问题'],['other','其他问题']]);
    const description=el('textarea');description.required=true;description.maxLength=1000;description.placeholder='描述遇到的问题和已做过的检查，请勿填写口令、密钥或其他敏感信息。';
    append(form,append(el('div','field-grid'),field('关联订单',order),field('问题类别',category)),field('问题说明',description));
    const submit=el('button','button button-primary','保存本地问题记录');submit.type='submit';form.append(submit);wireDraft(form,'support-create');
    let pending;
    try{pending=readPendingTicket();}catch(error){form.prepend(notice(error.message,'error'));}
    if(pending){order.value=pending.payload.order_id;category.value=pending.payload.category;description.value=pending.payload.description;form.prepend(notice('上次保存结果未确认，已恢复原内容。重试会复用原编号，避免重复记录。','warning'));submit.textContent='核验并重试原记录';}
    form.addEventListener('submit',async event=>{
      event.preventDefault();if(busy||refreshing||!form.reportValidity())return;
      await act(async()=>{
        const payload={order_id:order.value,category:category.value,description:description.value.trim()};
        if(!payload.description||/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/.test(payload.description))throw new Error('问题说明不能为空或包含不支持的控制字符。');
        let request=readPendingTicket();
        if(request&&JSON.stringify(request.payload)!==JSON.stringify(payload))throw new Error('上次保存尚未确认，请先用原内容重试并核对记录。');
        if(!request){request={key:crypto.randomUUID(),payload};sessionStorage.setItem(pendingTicketStorageKey,JSON.stringify(request));}
        const data=await post('/api/support/tickets',{...request.payload,idempotency_key:request.key});
        if(!data.ticket?.id)throw new Error('尚未取得确认记录，已保留原编号，请用原内容重试。');
        sessionStorage.removeItem(pendingTicketStorageKey);delete drafts['support-create'];supportLoaded=false;
      },'已保存本地问题记录；未发送给客服或商家。');
    });body.append(form);
  }else body.append(empty('有采购订单后，可以在这里记录对应的付款、交付或退款问题。'));
  const records=el('div','support-records section-gap');
  records.append(el('h3','',`本地处理记录 ${supportLoaded?supportTickets.length:''}`));
  if(!supportLoaded)records.append(el('p','field-help','正在读取本地问题记录…'));
  if(supportLoaded&&!supportError&&!supportTickets.length)records.append(empty('还没有本地问题记录。'));
  supportTickets.forEach(ticket=>{
    const form=el('form','support-ticket');
    const names={open:'待处理',in_progress:'处理中',resolved:'已解决'};
    append(form,append(el('div','order-title-row'),el('h3','',`${ticket.order_snapshot?.product_title||ticket.order_id} · ${ticket.id}`),badge(names[ticket.status]||'未知状态',ticket.status==='resolved'?'':'amber')),el('p','order-reason',ticket.description),el('p','field-help',`创建 ${date(ticket.created_at)} · 更新 ${date(ticket.updated_at)} · 版本 ${ticket.revision} · 仅本机记录`));
    const status=select([['open','待处理'],['in_progress','处理中'],['resolved','已解决']],ticket.status);
    const resolution=el('textarea');resolution.required=true;resolution.maxLength=1000;resolution.value=ticket.resolution||'';resolution.placeholder='记录你已完成的处理或核验；标为已解决前请写明结果。';
    append(form,field('手动记录进度',status),field('处理或解决说明',resolution));
    const key=`support-${ticket.id}`;const revision=wireDraft(form,key,ticket.revision);
    if(revision!==ticket.revision)form.append(notice('该记录已有更新，草稿保留。请放弃草稿后核对最新内容。','warning'));
    const save=el('button','button button-small','保存处理记录');save.type='submit';append(form,append(el('div','order-actions'),save,button('放弃此条草稿',async()=>{if(await confirmAction('放弃这条尚未保存的处理说明，读取当前记录？','放弃处理草稿')){delete drafts[key];supportLoaded=false;render();}},'button-ghost button-small')));
    form.addEventListener('submit',event=>{event.preventDefault();if(busy||refreshing||!form.reportValidity())return;act(async()=>{const text=resolution.value.trim();if(!text||/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/.test(text))throw new Error('请填写有效的处理说明。');await post(`/api/support/tickets/${encodeURIComponent(ticket.id)}`,{status:status.value,resolution:text,expected_revision:revision});delete drafts[key];supportLoaded=false;},'本地处理记录已更新；没有对外发送消息。');});records.append(form);
  });body.append(records);
  const result=panel('本地问题记录','保存问题和处理进度，供你自己核对；不是客服工单',body);result.classList.add('section-gap');return result;
}

function showLogin() {
  // 会话失效时清除页面和内存中的旧账目；未知请求编号保留，供重新登录后核对。
  ++fetchSequence;
  state=null;lastUpdated=null;syncError='';deferredRender=false;
  clearTimeout(toastTimer);document.querySelector('#toast').hidden=true;document.querySelector('#toast').textContent='';
  ++supportSequence;supportTickets=[];supportLoaded=false;supportLoading=false;supportError='';supportOrderId='';
  Object.keys(drafts).forEach(key=>delete drafts[key]);orderSearch='';taskFilter='all';orderFilter='all';
  document.querySelectorAll('dialog').forEach(dialog=>dialog.remove());
  document.querySelector('#inbox-count').hidden=true;
  document.querySelector('#wallet-status').textContent='本地工作空间已锁定';
  document.querySelector('#wallet-status').className='status-badge amber';
  document.querySelector('#pause-button').disabled=true;
  document.querySelector('#logout-button').hidden=true;
  document.querySelector('#sync-status').textContent='登录后读取本地账目';
  document.querySelector('#breadcrumb-page').textContent='解锁工作空间';
  main.replaceChildren(heading('解锁本地工作空间','此口令保护当前本机服务，不是面向公网的用户账号体系。采购仍为模拟，不会真实扣款。','LOCAL ACCESS'));
  const form=el('form','panel-body login-form');
  const password=el('input');password.type='password';password.autocomplete='current-password';password.required=true;password.name='password';
  const submit=el('button','button button-primary','解锁工作空间');submit.type='submit';
  append(form,field('本机服务口令',password,'使用启动受保护模式时设置的口令。本页面不会把口令保存到草稿或浏览器存储。'),submit);
  form.addEventListener('submit',async event=>{
    event.preventDefault();if(busy||!form.reportValidity())return;
    busy=true;submit.disabled=true;
    try{
      const {response,data}=await request('/api/session/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:password.value})});
      password.value='';
      if(!response.ok||data.ok===false)throw new Error(data.error||'无法解锁，请核对本机服务口令。');
      await fetchState();activePage=route().page;render();
    }catch(error){password.value='';showToast(error.message,true);}
    finally{busy=false;submit.disabled=false;updateShell();}
  });
  const loginPanel=panel('本机访问保护','口令只发送给当前本地服务',form);loginPanel.classList.add('login-panel');main.append(loginPanel);
}
async function logout() {
  if(!state?.owner_protected||busy)return;
  if(!await confirmAction('退出后清除当前页面的账目和未提交草稿。尚未确认的采购、新建任务请求编号会保留，便于重新登录后核对。','退出本地工作空间'))return;
  busy=true;++fetchSequence;document.querySelector('#logout-button').disabled=true;
  try{await post('/api/session/logout',{});showLogin();showToast('已退出，本地工作空间已锁定。');}
  catch(error){if(error.code!=='login_required')showToast('退出未确认，请重试。',true);}
  finally{busy=false;updateShell();}
}

document.querySelectorAll('[data-page]').forEach(node=>node.addEventListener('click',()=>navigate(node.dataset.page)));
document.querySelector('#pause-button').addEventListener('click',async()=>{if(!state)return;const enabled=state.policy.enabled;if(!enabled&&!await confirmAction('恢复后，符合规则的新采购可以继续模拟执行。已失效的申请需要重新提交。','恢复消费授权'))return;await act(()=>post(`/api/wallet/${enabled?'pause':'resume'}`),enabled?'已暂停消费授权。':'已恢复消费授权。');});
document.querySelector('#logout-button')?.addEventListener('click',logout);
document.querySelector('#refresh-button')?.addEventListener('click',()=>refreshState(true));
main.addEventListener('focusout',()=>setTimeout(flushDeferredRender,0));
// navigate 已同步渲染时不重复重建页面，避免引导定位后的焦点被 hashchange 清除。
window.addEventListener('hashchange',()=>{if(location.hash!==renderedHash){activePage=route().page;render();}});
window.addEventListener('focus',()=>refreshState());
document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible')refreshState();});
setInterval(()=>{if(document.visibilityState==='visible')refreshState();},15000);
async function init(){try{await fetchState();activePage=route().page;render();}catch(error){if(error.code==='login_required')return;main.replaceChildren(heading('本地钱包暂时无法连接',error.message,'CONNECTION'),button('重新连接',init,'button-primary'));showToast(error.message,true);}}
init();
