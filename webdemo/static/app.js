/* BTBIZ voice demo — browser client.
 *
 * Before a call: details → WhatsApp code → pick a voicebot. The server enforces the gate
 * on /api/token; this page only guides people through it, so nothing here is trusted.
 *
 * During a call the visitor plays the customer. The left panel is the script for the
 * chosen bot (from /api/config → bots), ticking off as the bot captures each answer; the
 * right panel is what the bot has captured, live, from the worker's demo.state feed. On
 * narrow screens the two panels share a tab strip.
 *
 * Everything that arrives from a call came through speech recognition and an LLM, so it
 * is only ever written with textContent — never innerHTML.
 *
 * The gate's refusals: everything inline next to its field, EXCEPT the two a visitor
 * can do nothing about — a locked number and a used-up allowance. Those get the popup,
 * with the countdown the server sent, because a line of red text under a button someone
 * is about to press again is not an explanation.
 *
 * Optional URL params:
 *   ?bot=roof           preselect a voicebot (smarthealth | roof | finance)
 *   ?tts=cartesia:<id>  override the voice (whitelisted server-side)
 *   ?k=<code>           legacy passcode, only if DEMO_PASSCODE is set server-side
 */
(() => {
'use strict';

// Bump on every edit. Printed at load so "is the browser running my new code?" is a
// glance at the console rather than a guess.
const BUILD = 15;
console.info('[btbiz-demo] app.js build ' + BUILD);

const LK = window.LivekitClient;
const $  = (id) => document.getElementById(id);
const QS = new URLSearchParams(location.search);
const BOT_STORE = 'btbiz.demo.bot';
// Outcomes that mean "still talking" rather than a result worth colouring.
const LIVE_OUTCOMES = new Set(['', 'DROPPED', 'NO_OUTCOME']);

const el = {
  stage:$('stage'),
  brandCompany:$('brandCompany'), timerValue:$('timerValue'),
  statusPill:$('statusPill'), statusText:$('statusText'),
  orbwrap:$('orbwrap'), orb:$('orb'), core:document.querySelector('.core'),
  viewIdle:$('viewIdle'), viewLive:$('viewLive'), viewEnded:$('viewEnded'),
  stepDetails:$('stepDetails'), stepCode:$('stepCode'), stepReady:$('stepReady'),
  detailsForm:$('detailsForm'), fName:$('fName'), fCompany:$('fCompany'),
  fEmail:$('fEmail'), fCategory:$('fCategory'), fUsecase:$('fUsecase'),
  fCc:$('fCc'), fIso:$('fIso'), fPhone:$('fPhone'), fWebsite:$('fWebsite'),
  btnSend:$('btnSend'),
  ccWrap:$('ccWrap'), ccBtn:$('ccBtn'), ccIso:$('ccIso'), ccDial:$('ccDial'),
  ccPop:$('ccPop'), ccSearch:$('ccSearch'), ccList:$('ccList'), ccNone:$('ccNone'),
  lockModal:$('lockModal'), lockTitle:$('lockTitle'), lockBody:$('lockBody'),
  lockCount:$('lockCount'), lockLeft:$('lockLeft'), lockHelp:$('lockHelp'),
  lockClose:$('lockClose'), lockWhatsapp:$('lockWhatsapp'),
  codeForm:$('codeForm'), fCode:$('fCode'), codeTo:$('codeTo'), codeLen:$('codeLen'),
  codeHint:$('codeHint'),
  btnVerify:$('btnVerify'), btnResend:$('btnResend'), btnEdit:$('btnEdit'),
  readyTitle:$('readyTitle'), picker:$('picker'), fBot:$('fBot'), botCard:$('botCard'),
  bcTagline:$('bcTagline'), bcMarket:$('bcMarket'), bcVoice:$('bcVoice'),
  bcGoal:$('bcGoal'), bcPersona:$('bcPersona'),
  btnStart:$('btnStart'), callsLeft:$('callsLeft'),
  contactLink:$('contactLink'), contactSep:$('contactSep'), btnNotMe:$('btnNotMe'),
  idleError:$('idleError'),
  liveGrid:$('liveGrid'), liveCenter:$('liveCenter'), liveBot:$('liveBot'),
  panelScript:$('panelScript'), scriptProg:$('scriptProg'), livePersona:$('livePersona'),
  scriptSteps:$('scriptSteps'), tryBox:$('tryBox'), tryList:$('tryList'),
  panelData:$('panelData'), dataProg:$('dataProg'), dataList:$('dataList'),
  chipOutcome:$('chipOutcome'), chipConsent:$('chipConsent'),
  tabScript:$('tabScript'), tabData:$('tabData'), tabCount:$('tabCount'),
  orbState:$('orbState'),
  btnMute:$('btnMute'), muteLabel:$('muteLabel'), btnEnd:$('btnEnd'),
  liveError:$('liveError'), endLede:$('endLede'), btnAgain:$('btnAgain'),
  endSummary:$('endSummary'), endBotName:$('endBotName'), endOutcome:$('endOutcome'),
  endData:$('endData'),
};

const S = {
  cfg:{company:'BTBIZ', agent:'Sarah', signupRequired:true, codeLength:6, bots:[]},
  bots:[], bot:'',
  me:null, pendingOtp:null, lastForm:null, verifying:false, resendTimer:null,
  countries:[], cc:null, sendsLeft:null, lockUntil:0, lockTimer:null,
  room:null, roomName:null, callId:null,
  audioEl:null, ac:null, analyser:null, raf:null,
  t0:0, timer:null, outcome:'', ended:false, errorMsg:'',
  // Per call
  state:null, live:{}, steps:[], dataView:null, curStep:null,
};

/* ── Small helpers ──────────────────────────────────────────────────────── */
function store(key, val){
  try{
    if (val === undefined) return localStorage.getItem(key);
    localStorage.setItem(key, val);
  }catch(_){ /* private mode or blocked storage — a convenience, not a requirement */ }
  return null;
}
function make(tag, cls, text){
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}
const val = (fields, k) => String((fields && fields[k]) || '').trim();

/* ── Chrome ─────────────────────────────────────────────────────────────── */
function setStatus(kind, text){
  el.statusPill.className = 'pill pill-' + kind;
  el.statusText.textContent = text;
}
function placeOrb(where){
  // Moved, never rebuilt: the visualiser holds a reference to .core.
  if (where === 'live'){
    if (el.orbwrap.parentNode !== el.liveCenter) el.liveCenter.insertBefore(el.orbwrap, el.orbState);
  } else if (el.orbwrap.parentNode !== el.stage || el.stage.firstElementChild !== el.orbwrap){
    el.stage.insertBefore(el.orbwrap, el.stage.firstChild);
  }
}
function showView(name){
  for (const k of ['Idle','Live','Ended']) el['view'+k].hidden = (k.toLowerCase() !== name);
  // Top-aligned during a call. Vertically centred, the orb and controls would jump every
  // time the phone tab strip swaps a long panel for a short one.
  el.stage.classList.toggle('stage-live', name === 'live');
  placeOrb(name === 'live' ? 'live' : 'stage');
  el.orbwrap.classList.toggle('compact', name === 'live');
  el.orb.classList.toggle('idle', name !== 'live');
  if (name !== 'idle') el.orbwrap.classList.remove('mini');
}
function showStep(name){
  showView('idle');
  el.stepDetails.hidden = name !== 'details';
  el.stepCode.hidden    = name !== 'code';
  el.stepReady.hidden   = name !== 'ready';
  // Small on every pre-call step: the bot picker and its notes need the room, and the
  // Start button must stay above the fold. It grows to full size for the call itself.
  el.orbwrap.classList.add('mini');
  showErr(el.idleError, '');
}
function showErr(node, msg){
  if (!msg){ node.hidden = true; node.textContent = ''; return; }
  node.textContent = msg; node.hidden = false;
}
function setBusy(btn, busy, label){
  const lbl = btn.querySelector('.lbl');
  if (lbl){
    if (!btn.dataset.label) btn.dataset.label = lbl.textContent;
    lbl.textContent = busy ? label : btn.dataset.label;
  }
  btn.disabled = busy;
  btn.classList.toggle('busy', busy);
}
const fmtTime = (s) =>
  String(Math.floor(s/60)).padStart(2,'0') + ':' + String(Math.floor(s%60)).padStart(2,'0');
const firstName = (n) => String(n || '').trim().split(/\s+/)[0] || '';

async function getJSON(url, opts){
  const res = await fetch(url, Object.assign({credentials:'same-origin'}, opts || {}));
  const data = await res.json().catch(() => ({}));
  return {res, data};
}
async function postJSON(url, body){
  return getJSON(url, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(body || {}),
  });
}

/* ── Boot ───────────────────────────────────────────────────────────────── */
async function boot(){
  try{
    const {data} = await getJSON('/api/config');
    S.cfg = Object.assign(S.cfg, data);
  }catch(_){ /* defaults keep the page usable */ }

  el.brandCompany.textContent = S.cfg.company || 'BTBIZ';
  document.title = (S.cfg.company || 'BTBIZ') + ' · AI Voice Agent';

  initBots();
  initCountries(S.cfg.countries, S.cfg.defaultCountry);

  const len = Number(S.cfg.codeLength) || 6;
  el.codeLen.textContent = String(len);
  el.fCode.maxLength = len;

  if (S.cfg.contactWhatsapp){
    el.contactLink.href = 'https://wa.me/' + String(S.cfg.contactWhatsapp).replace(/\D/g,'');
  }

  if (S.cfg.signupRequired === false){ setReady(null); return; }

  try{
    const {data} = await getJSON('/api/me');
    if (data.verified){ setReady(data); return; }
  }catch(_){}
  showStep('details');
}

/* ── Bots ───────────────────────────────────────────────────────────────── */
function initBots(){
  S.bots = Array.isArray(S.cfg.bots) ? S.cfg.bots : [];
  el.fBot.textContent = '';
  for (const b of S.bots){
    const o = make('option', '', b.market ? `${b.name} · ${b.market}` : b.name);
    o.value = b.key;
    el.fBot.appendChild(o);
  }
  // ?bot= beats the last one used, which beats the server's first choice.
  const wanted = [QS.get('bot'), store(BOT_STORE), S.cfg.defaultBot]
    .map(k => String(k || '').toLowerCase());
  S.bot = wanted.find(k => S.bots.some(b => b.key === k)) || (S.bots[0] && S.bots[0].key) || '';
  el.fBot.value = S.bot;
  el.picker.hidden = S.bots.length === 0;
  renderBotCard();
}
const currentBot = () => S.bots.find(b => b.key === S.bot) || null;

function renderBotCard(){
  const bot = currentBot();
  el.botCard.hidden = !bot;
  if (!bot) return;
  el.bcTagline.textContent = bot.tagline || '';
  el.bcMarket.textContent  = bot.market || '';
  el.bcVoice.textContent   = bot.voice || '';
  el.bcGoal.textContent    = bot.goal || '';
  el.bcPersona.textContent = bot.persona || '';
}
el.fBot.addEventListener('change', () => {
  S.bot = el.fBot.value;
  store(BOT_STORE, S.bot);
  renderBotCard();
});

/* ── Country picker ─────────────────────────────────────────────────────────
 * Every country, so nobody is stuck because their code wasn't on our shortlist — which
 * means it has to be searchable. Matching is deliberately loose: "uk", "gb", "44",
 * "+44", "britain"… all reach the United Kingdom. Accents are stripped, so "reunion"
 * finds Réunion and "turkiye" finds Türkiye.
 * The value posted is the hidden fCc/fIso pair; the server re-derives the number from
 * it and never trusts this.
 */
const CC_STORE = 'btbiz.demo.cc';
const deaccent = (s) => String(s || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '');

function subsequence(hay, needle){
  if (needle.length < 3) return false;
  let i = 0;
  for (let k = 0; k < hay.length && i < needle.length; k++) if (hay[k] === needle[i]) i++;
  return i === needle.length;
}

// Lower is better; 99 = no match. Order within a score keeps the server's order, which
// puts the countries our prospects are actually in first.
function ccScore(c, q){
  if (!q) return 10;
  const name = deaccent(c.name).toLowerCase();
  const iso  = c.iso.toLowerCase();
  const dial = c.dial.replace('+', '');
  if (/^[+\d][\d\s+-]*$/.test(q)){
    const digits = q.replace(/\D/g, '');
    if (!digits) return 99;
    if (dial === digits) return 0;
    if (dial.startsWith(digits)) return 1;
    return digits.length >= 2 && dial.includes(digits) ? 4 : 99;
  }
  if (iso === q) return 0;
  if (name.startsWith(q)) return 1;
  if (name.split(/[\s-]+/).some(w => w.startsWith(q))) return 2;
  if (name.includes(q)) return 3;
  return subsequence(name, q) ? 5 : 99;
}

function pickCC(c, {focusPhone = true} = {}){
  if (!c) return;
  S.cc = c;
  el.fCc.value = c.dial;
  el.fIso.value = c.iso;
  el.ccIso.textContent = c.iso;
  el.ccDial.textContent = c.dial;
  el.ccBtn.setAttribute('aria-label', `Country code: ${c.name} ${c.dial}`);
  store(CC_STORE, c.iso);
  openCC(false);
  if (focusPhone) el.fPhone.focus();
}

function renderCC(){
  const q = deaccent(el.ccSearch.value.trim()).toLowerCase();
  const rows = S.countries
    .map((c, i) => ({c, s: ccScore(c, q), i}))
    .filter(r => r.s < 99)
    .sort((a, b) => a.s - b.s || a.i - b.i);
  el.ccList.textContent = '';
  for (const {c} of rows){
    const li = make('li', 'cc-opt');
    li.setAttribute('role', 'option');
    li.dataset.iso = c.iso;
    li.setAttribute('aria-selected', String(S.cc && c.iso === S.cc.iso));
    li.appendChild(make('span', 'cc-name', c.name));
    li.appendChild(make('span', 'cc-code', c.dial));
    li.addEventListener('click', () => pickCC(c));
    el.ccList.appendChild(li);
  }
  el.ccNone.hidden = rows.length > 0;
  const active = el.ccList.querySelector('[aria-selected="true"]') || el.ccList.firstElementChild;
  if (active) active.classList.add('on');
}

function openCC(open){
  el.ccPop.hidden = !open;
  el.ccBtn.setAttribute('aria-expanded', String(open));
  if (open){
    el.ccSearch.value = '';
    renderCC();
    el.ccSearch.focus();
    const on = el.ccList.querySelector('.on');
    if (on) on.scrollIntoView({block:'nearest'});
  }
}

function moveCC(step){
  const opts = [...el.ccList.children];
  if (!opts.length) return;
  const at = opts.findIndex(o => o.classList.contains('on'));
  const next = Math.min(Math.max((at < 0 ? 0 : at) + step, 0), opts.length - 1);
  opts.forEach(o => o.classList.remove('on'));
  opts[next].classList.add('on');
  opts[next].scrollIntoView({block:'nearest'});
}

function initCountries(list, defIso){
  S.countries = Array.isArray(list) && list.length ? list : [{iso:'IN', name:'India', dial:'+91'}];
  const want = store(CC_STORE) || defIso || 'IN';
  pickCC(S.countries.find(c => c.iso === want) || S.countries[0], {focusPhone:false});
}

el.ccBtn.addEventListener('click', () => openCC(el.ccPop.hidden));
el.ccSearch.addEventListener('input', renderCC);
el.ccSearch.addEventListener('keydown', (e) => {
  if (e.key === 'ArrowDown'){ e.preventDefault(); moveCC(1); }
  else if (e.key === 'ArrowUp'){ e.preventDefault(); moveCC(-1); }
  else if (e.key === 'Enter'){
    e.preventDefault();
    const on = el.ccList.querySelector('.on');
    if (on) pickCC(S.countries.find(c => c.iso === on.dataset.iso));
  } else if (e.key === 'Escape'){ openCC(false); el.ccBtn.focus(); }
});
document.addEventListener('click', (e) => {
  if (!el.ccPop.hidden && !el.ccWrap.contains(e.target)) openCC(false);
});

/* ── The lockout popup ──────────────────────────────────────────────────────
 * One dialog for both hard stops: a locked number and a spent allowance. It runs its
 * own countdown off the server's lockSeconds — never off the clock on this machine,
 * which can be wrong or set forward.
 */
function stopLockTimer(){
  if (S.lockTimer){ clearInterval(S.lockTimer); S.lockTimer = null; }
}

function mmss(sec){
  const m = Math.floor(sec / 60), s = sec % 60;
  return m + ':' + String(s).padStart(2, '0');
}

function lockTick(){
  const left = Math.max(Math.ceil((S.lockUntil - Date.now()) / 1000), 0);
  el.lockLeft.textContent = mmss(left);
  if (left <= 0){
    stopLockTimer();
    el.lockCount.hidden = true;
    el.lockBody.textContent = 'You can try again now.';
    el.btnSend.disabled = false;
    el.btnResend.disabled = false;
  }
}

function showLock({title, body, help, seconds, showContact}){
  stopLockTimer();
  el.lockTitle.textContent = title;
  el.lockBody.textContent = body;
  el.lockHelp.textContent = help || '';
  el.lockHelp.hidden = !help;
  const wa = S.cfg.contactWhatsapp;
  el.lockWhatsapp.hidden = !(showContact && wa);
  if (showContact && wa) el.lockWhatsapp.href = 'https://wa.me/' + wa;
  if (seconds > 0){
    S.lockUntil = Date.now() + seconds * 1000;
    el.lockCount.hidden = false;
    lockTick();
    S.lockTimer = setInterval(lockTick, 1000);
    // Nothing they can press will work until it expires; say so by not offering it.
    el.btnSend.disabled = true;
    el.btnResend.disabled = true;
  } else {
    S.lockUntil = 0;
    el.lockCount.hidden = true;
  }
  el.lockModal.hidden = false;
  el.lockClose.focus();
}

function hideLock(){
  el.lockModal.hidden = true;
}
el.lockClose.addEventListener('click', hideLock);
el.lockModal.addEventListener('click', (e) => { if (e.target === el.lockModal) hideLock(); });
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !el.lockModal.hidden) hideLock();
});

/* Returns true when the popup took over and the caller should stop. */
function handleGate(res, data){
  if (res.status === 423 || data.code === 'locked'){
    const network = data.scope === 'network';
    showLock({
      title: network ? 'Too many attempts from your network' : 'This number is locked',
      body: data.detail || 'Too many failed attempts.',
      help: network
        ? 'The limit is per network, so someone else on this connection may have hit it.'
        : 'The lock is on the number, so requesting a new code won’t clear it.',
      seconds: Number(data.lockSeconds) || 0,
      showContact: true,
    });
    return true;
  }
  if (data.code === 'call_limit'){
    showLock({
      title: 'Demo limit reached',
      body: data.detail || 'You’ve used your demo calls.',
      help: data.scope === 'network'
        ? 'The limit counts calls from this network, not just this account.'
        : '',
      seconds: 0,
      showContact: true,
    });
    return true;
  }
  return false;
}

/* ── Step 1: details ────────────────────────────────────────────────────── */
const FIELD = {name:'fName', company:'fCompany', email:'fEmail', category:'fCategory',
               usecase:'fUsecase', phone:'fPhone'};

function toE164(cc, raw){
  let s = String(raw || '').replace(/[^\d+]/g, '');
  if (s.startsWith('+')) return s;                     // typed a full international number
  if (s.startsWith('00')) return '+' + s.slice(2);
  s = s.replace(/^0+/, '');                            // drop the local trunk prefix (07…, 04…)
  const ccDigits = cc.replace('+', '');
  // Typed the country code without the plus — don't prefix it twice.
  if (s.length > 10 && s.startsWith(ccDigits)) return '+' + s;
  return cc + s;
}

const squash = (s) => String(s || '').replace(/\s+/g, ' ').trim();

function readForm(){
  return {
    name:     el.fName.value.trim(),
    company:  el.fCompany.value.trim(),
    email:    el.fEmail.value.trim(),
    category: squash(el.fCategory.value),
    usecase:  squash(el.fUsecase.value),
    // Both: the raw number AND what the picker says. The server re-derives the E.164
    // from them (countries.py), which is what makes a typed +44, a leading 0 or a
    // doubled country code all land on the same number.
    phone:    el.fPhone.value.trim(),
    cc:       el.fCc.value,
    iso:      el.fIso.value,
    website:  el.fWebsite.value,
  };
}

function validate(f){
  const e = {};
  if (f.name.length < 2)     e.name = 'Enter your full name.';
  if (f.company.length < 2)  e.company = 'Enter your company name.';
  if (!/^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$/.test(f.email)) e.email = 'Enter a valid email address.';
  if (f.category.length < 2) e.category = 'Enter your business category.';
  if (f.usecase.length < 5)  e.usecase = 'Tell us briefly what you’d like a voicebot to do.';
  else if (f.usecase.length > 500) e.usecase = 'Please keep this under 500 characters.';
  // Only a shape check here; the server decides. toE164 mirrors its normaliser closely
  // enough to catch a typo before we spend a WhatsApp message on it.
  if (!/^\+[1-9]\d{7,14}$/.test(toE164(f.cc, f.phone))) e.phone = 'Enter a valid mobile number.';
  return e;
}

function clearFieldError(input){
  input.removeAttribute('aria-invalid');
  const host = input.closest('.fld');
  const n = host && host.querySelector('.ferr');
  if (n) n.remove();
}
function clearFieldErrors(){
  document.querySelectorAll('.ferr').forEach(n => n.remove());
  document.querySelectorAll('[aria-invalid]').forEach(n => n.removeAttribute('aria-invalid'));
}
function showFieldErrors(errs, focus = true){
  let first = null;
  for (const [key, msg] of Object.entries(errs || {})){
    const input = $(FIELD[key]);
    if (!input) continue;
    input.setAttribute('aria-invalid', 'true');
    const host = input.closest('.fld');
    let n = host.querySelector('.ferr');
    if (!n){ n = make('small', 'ferr'); host.appendChild(n); }
    n.textContent = msg;
    first = first || input;
  }
  if (first && focus) first.focus();
}
for (const id of Object.values(FIELD)){
  const input = $(id);
  input.addEventListener('input', () => clearFieldError(input));
  input.addEventListener('change', () => clearFieldError(input));
}
el.fCc.addEventListener('change', () => clearFieldError(el.fPhone));

/* Company-email check when the visitor leaves the field, so a Gmail address is flagged
   before they fill in the phone and trigger a WhatsApp code. The server applies the same
   rule on send (email_policy.py) — this is early feedback, not enforcement. */
let emailCheckSeq = 0;
async function checkEmail(){
  const email = el.fEmail.value.trim();
  if (!/^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$/.test(email)) return;   // format errors show on submit
  const seq = ++emailCheckSeq;
  try{
    const {res, data} = await postJSON('/api/email/check', {email});
    // Ignore a late answer if they've already edited the field since.
    if (seq !== emailCheckSeq || el.fEmail.value.trim() !== email) return;
    // No focus change: they've moved on to the next field and yanking them back is hostile.
    if (res.ok && data.ok === false) showFieldErrors({email: data.message}, false);
  }catch(_){ /* the server re-checks on send */ }
}
el.fEmail.addEventListener('blur', checkEmail);

el.detailsForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  clearFieldErrors();
  const form = readForm();
  const errs = validate(form);
  if (Object.keys(errs).length){ showFieldErrors(errs); return; }
  await sendCode(form);
});

async function sendCode(form){
  showErr(el.idleError, '');
  setBusy(el.btnSend, true, 'Sending…');
  setBusy(el.btnResend, true);
  try{
    const {res, data} = await postJSON('/api/otp/send', form);
    if (!res.ok){
      if (handleGate(res, data)) return;
      // A rejected field belongs back on the form, next to the field.
      if (data.fields){
        showStep('details');
        showFieldErrors(data.fields);
      }
      // A cooldown is not an error to argue with — just restart the countdown.
      if (data.code === 'cooldown' && Number(data.retryAfter) > 0){
        startResendTimer(new Date(Date.now() + data.retryAfter * 1000).toISOString());
      }
      throw new Error(data.detail || 'We couldn’t send your code. Please try again.');
    }
    S.lastForm = form;
    S.pendingOtp = data;
    S.sendsLeft = Number.isInteger(data.sendsRemaining) ? data.sendsRemaining : null;
    el.codeTo.textContent = data.phoneMasked || form.phone;
    el.fCode.value = '';
    setCodeHint(Number(data.attemptsAllowed) || Number(S.cfg.otpMaxAttempts) || 0, true);
    showStep('code');
    el.fCode.focus();
    startResendTimer(data.resendAvailableAt);
  }catch(err){
    showErr(el.idleError, err.message);
  }finally{
    setBusy(el.btnSend, false);
    el.btnResend.classList.remove('busy');
  }
}

/* ── Step 2: code ───────────────────────────────────────────────────────── */
/* Say the budget up front and count it down. Being told "2 attempts left before this
 * number is locked" is what stops the third guess; being told afterwards is not. */
function setCodeHint(n, fresh){
  if (!n){ el.codeHint.hidden = true; return; }
  el.codeHint.hidden = false;
  el.codeHint.classList.toggle('warn', !fresh && n <= 1);
  const attempts = `${n} attempt${n === 1 ? '' : 's'}`;
  el.codeHint.textContent = fresh
    ? `You have ${attempts} before this number is locked.`
    : `${attempts} left before this number is locked.`;
}

function startResendTimer(iso){
  stopResendTimer();
  let until = iso ? Date.parse(iso) : NaN;
  if (Number.isNaN(until)) until = Date.now() + 30000;
  const tick = () => {
    const left = Math.ceil((until - Date.now()) / 1000);
    if (left > 0){
      el.btnResend.disabled = true;
      el.btnResend.textContent = `Resend code in ${left}s`;
    } else if (S.sendsLeft === 0){
      // The server will refuse another code for this number; don't offer the button.
      el.btnResend.disabled = true;
      el.btnResend.textContent = 'No more codes for now';
      stopResendTimer();
    } else {
      el.btnResend.disabled = false;
      el.btnResend.textContent = S.sendsLeft > 0
        ? `Resend code (${S.sendsLeft} left)` : 'Resend code';
      stopResendTimer();
    }
  };
  tick();
  S.resendTimer = setInterval(tick, 1000);
}
function stopResendTimer(){
  if (S.resendTimer){ clearInterval(S.resendTimer); S.resendTimer = null; }
}

el.fCode.addEventListener('input', () => {
  const len = Number(S.cfg.codeLength) || 6;
  el.fCode.value = el.fCode.value.replace(/\D/g, '').slice(0, len);
  showErr(el.idleError, '');
  // Submit the moment the last digit lands — pasted or autofilled codes included.
  if (el.fCode.value.length === len) verifyCode();
});
el.codeForm.addEventListener('submit', (e) => { e.preventDefault(); verifyCode(); });

async function verifyCode(){
  if (S.verifying || !S.pendingOtp) return;
  const len = Number(S.cfg.codeLength) || 6;
  const code = el.fCode.value.replace(/\D/g, '');
  if (code.length !== len){
    showErr(el.idleError, `Enter the ${len}-digit code from WhatsApp.`);
    el.fCode.focus();
    return;
  }
  S.verifying = true;
  setBusy(el.btnVerify, true, 'Verifying…');
  showErr(el.idleError, '');
  try{
    const {res, data} = await postJSON('/api/otp/verify', {otpId: S.pendingOtp.otpId, code});
    if (!res.ok){
      if (handleGate(res, data)){
        S.pendingOtp = null;
        stopResendTimer();
        el.fCode.value = '';
        setCodeHint(0);
        return;
      }
      if (Number.isInteger(data.attemptsRemaining)) setCodeHint(data.attemptsRemaining, false);
      if (data.restart){
        // This code is dead — offer a fresh one straight away rather than a countdown.
        S.pendingOtp = null;
        stopResendTimer();
        el.btnResend.disabled = false;
        el.btnResend.textContent = 'Send a new code';
        el.fCode.value = '';
      } else {
        el.fCode.select();
      }
      throw new Error(data.detail || 'That code didn’t work. Please try again.');
    }
    stopResendTimer();
    S.pendingOtp = null;
    setReady(data);
  }catch(err){
    showErr(el.idleError, err.message);
  }finally{
    S.verifying = false;
    setBusy(el.btnVerify, false);
  }
}

el.btnResend.addEventListener('click', () => { if (S.lastForm) sendCode(S.lastForm); });
el.btnEdit.addEventListener('click', () => {
  stopResendTimer();
  S.pendingOtp = null;
  showStep('details');
  el.fName.focus();
});

/* ── Step 3: pick a bot ─────────────────────────────────────────────────── */
function setReady(me){
  S.me = me;
  if (me){
    const fn = firstName(me.name);
    el.readyTitle.textContent = fn ? `You're all set, ${fn}.` : 'You’re all set.';
    const left = Number(me.callsRemaining) || 0;
    if (left > 0){
      el.callsLeft.textContent =
        `${left} of ${me.callsMax} demo call${me.callsMax === 1 ? '' : 's'} remaining. ` +
        'You’ll be asked to allow your microphone.';
    } else {
      el.callsLeft.textContent = `You've used all ${me.callsMax} demo calls. Message us and we'll set up a full trial.`;
    }
    el.btnStart.disabled = left <= 0;
    el.btnNotMe.hidden = false;
  } else {
    el.readyTitle.textContent = 'Have a conversation with an AI voice agent.';
    el.callsLeft.textContent = 'You’ll be asked to allow your microphone.';
    el.btnStart.disabled = false;
    el.btnNotMe.hidden = true;
  }
  const showContact = Boolean(S.cfg.contactWhatsapp);
  el.contactLink.hidden = !showContact;
  el.contactSep.hidden = !(showContact && !el.btnNotMe.hidden);
  showStep('ready');
  setStatus('idle', 'Ready');
}

el.btnNotMe.addEventListener('click', async () => {
  try{ await postJSON('/api/signout'); }catch(_){}
  S.me = null;
  el.detailsForm.reset();
  clearFieldErrors();
  showStep('details');
  el.fName.focus();
});

async function refreshMe(){
  try{
    const {data} = await getJSON('/api/me');
    if (data.verified){ setReady(data); return true; }
  }catch(_){}
  return false;
}

/* ── Start the call ─────────────────────────────────────────────────────── */
el.btnStart.addEventListener('click', start);

async function start(){
  showErr(el.idleError, '');
  setBusy(el.btnStart, true, 'Connecting…');
  setStatus('wait', 'Connecting…');

  let probe = null;
  try{
    // Ask for the mic explicitly so a denied permission gives a clear message instead
    // of surfacing as a vague publish failure ten seconds later.
    probe = await navigator.mediaDevices.getUserMedia({audio:true});
  }catch(_){
    setBusy(el.btnStart, false); setStatus('idle', 'Ready');
    showErr(el.idleError, 'Microphone access was blocked. Allow it from the icon in ' +
                          'your browser’s address bar, then try again.');
    return;
  }finally{
    if (probe) probe.getTracks().forEach(t => t.stop());
  }

  let data;
  try{
    const r = await postJSON('/api/token', {
      bot:      S.bot,
      tts:      QS.get('tts') || '',
      passcode: QS.get('k') || '',
    });
    data = r.data;
    if (!r.res.ok){
      if (data.code === 'signup_required'){
        // Pass expired or was cleared — send them back through verification.
        setBusy(el.btnStart, false); setStatus('idle', 'Ready');
        S.me = null;
        showStep('details');
        showErr(el.idleError, 'Your session has expired. Please verify your details again.');
        return;
      }
      if (data.code === 'call_limit'){
        setBusy(el.btnStart, false); setStatus('idle', 'Ready');
        handleGate(r.res, data);
        await refreshMe();
        return;
      }
      if (handleGate(r.res, data)){
        setBusy(el.btnStart, false); setStatus('idle', 'Ready');
        return;
      }
      throw new Error(data.detail || ('HTTP ' + r.res.status));
    }
  }catch(err){
    setBusy(el.btnStart, false); setStatus('idle', 'Ready');
    showErr(el.idleError, String(err.message || err));
    return;
  }

  setBusy(el.btnStart, false);
  S.roomName = data.room; S.callId = data.callId;
  if (data.bot && S.bots.some(b => b.key === data.bot)) S.bot = data.bot;
  console.debug('[btbiz-demo] room=%s bot=%s callsRemaining=%s',
                data.room, data.bot, data.callsRemaining);
  resetCall();
  buildLive();
  showView('live');

  try{
    await connect(data.url, data.token);
  }catch(err){
    showErr(el.liveError, 'Could not connect: ' + (err.message || err));
    setStatus('bad', 'Connection failed');
  }
}

async function connect(url, token){
  const room = new LK.Room({
    adaptiveStream:true, dynacast:true,
    audioCaptureDefaults:{echoCancellation:true, noiseSuppression:true, autoGainControl:true},
  });
  S.room = room; S.ended = false;

  room.on(LK.RoomEvent.TrackSubscribed, onTrackSubscribed);
  room.on(LK.RoomEvent.DataReceived, onData);
  room.on(LK.RoomEvent.ParticipantAttributesChanged, (_c, p) => readAgentState(p));
  room.on(LK.RoomEvent.Disconnected, () => finish('The agent ended the call.'));
  room.on(LK.RoomEvent.ParticipantDisconnected, () => finish('The agent ended the call.'));

  await room.connect(url, token);
  await room.localParticipant.setMicrophoneEnabled(true);
  try{ await room.startAudio(); }catch(_){}   // the click is the required gesture

  setStatus('live', 'Live');
  el.timerValue.hidden = false;
  S.t0 = Date.now();
  S.timer = setInterval(() => {
    el.timerValue.textContent = fmtTime((Date.now() - S.t0) / 1000);
  }, 500);
}

/* ── Script + captured data ─────────────────────────────────────────────── */
/* A `when` condition is 'yes', 'no', or 'unknown' — unknown until the field it depends on
   has been captured. It lets one script hold both the business and personal loan paths
   and show the right one once the caller has said which. */
function whenState(cond, fields){
  if (!cond) return 'yes';
  let unknown = false;
  for (const [k, want] of Object.entries(cond)){
    const have = val(fields, k).toLowerCase();
    if (!have){ unknown = true; continue; }
    if (!have.includes(String(want).toLowerCase())) return 'no';
  }
  return unknown ? 'unknown' : 'yes';
}

function agentNames(bot){
  return {
    agent:   S.live.agent   || bot.agent   || 'Agent',
    company: S.live.company || bot.company || '',
  };
}
const fill = (text, n) => String(text || '').replace(/\{agent\}/g, n.agent).replace(/\{company\}/g, n.company);

function buildLive(){
  S.state = null; S.live = {}; S.curStep = null; S.errorMsg = '';
  const bot = currentBot();
  el.liveBot.textContent = bot ? (bot.market ? `${bot.name} · ${bot.market}` : bot.name) : '';
  el.liveGrid.hidden = !bot;
  if (!bot) return;

  el.livePersona.textContent = bot.persona || '';
  el.scriptSteps.textContent = '';
  S.steps = (bot.script || []).map((step) => {
    const li = make('li', 'stp');
    const n = make('span', 'n');
    const body = make('div');
    const say = make('p', 'say');
    const who = make('b');
    const line = make('span');
    say.append(who, ' ', line);
    const you = make('p', 'you');
    you.append(make('span', 'tag', 'You'), make('span', '', `“${step.you}”`));
    body.append(say, you);
    if (step.tip) body.append(make('p', 'tip', step.tip));
    li.append(n, body);
    el.scriptSteps.appendChild(li);
    return {li, n, who, line, step};
  });

  el.tryList.textContent = '';
  for (const t of (bot.try || [])){
    const li = make('li');
    li.append(make('p', 't-say', `“${t.say}”`), make('p', 't-exp', t.expect));
    el.tryList.appendChild(li);
  }
  el.tryBox.hidden = !(bot.try || []).length;
  el.tryBox.open = false;

  S.dataView = buildData(el.dataList, bot);
  refreshNames();
  setTab('script');
  el.panelScript.scrollTop = 0;
  el.panelData.scrollTop = 0;
  renderLive();
}

function refreshNames(){
  const bot = currentBot();
  if (!bot) return;
  const n = agentNames(bot);
  for (const s of S.steps){
    s.who.textContent = n.agent + ':';
    s.line.textContent = ' ' + fill(s.step.agent, n);
  }
}

function buildData(root, bot){
  root.textContent = '';
  // Worked-out values — a quote, the premium check, blocked card numbers — go FIRST.
  // They're the headline, and at the end of a long field list they'd be below the fold.
  const derived = make('div', 'kv-derived');
  root.appendChild(derived);
  const rows = {};
  for (const f of (bot.fields || [])){
    const row = make('div', 'kvr empty');
    const dt = make('dt', '', f.label);
    if (f.required){
      const r = make('i', 'req');
      r.title = 'Needed for the handoff';
      dt.appendChild(r);
    }
    const dd = make('dd', '', '—');
    row.append(dt, dd);
    root.appendChild(row);
    rows[f.key] = {row, dd, f, v:''};
  }
  return {rows, derived, derivedKey:''};
}

function updateData(view, st, animate){
  const fields = (st && st.fields) || {};
  let captured = 0, required = 0, count = 0;
  for (const key in view.rows){
    const r = view.rows[key];
    const v = val(fields, key);
    // A value always shows. An empty row shows unless it's optional or belongs to a
    // branch the caller hasn't taken.
    r.row.hidden = !(v || (!r.f.hideEmpty && whenState(r.f.when, fields) === 'yes'));
    if (r.f.required){ required++; if (v) captured++; }
    if (v) count++;
    if (v !== r.v){
      r.dd.textContent = v || '—';
      r.row.classList.toggle('empty', !v);
      if (v && animate){
        r.row.classList.remove('flash');
        void r.row.offsetWidth;               // restart the animation
        r.row.classList.add('flash');
      }
      r.v = v;
    }
  }
  const derived = (st && st.derived) || [];
  const dk = JSON.stringify(derived);
  if (dk !== view.derivedKey){
    view.derived.textContent = '';
    for (const d of derived){
      const row = make('div', 'kvr derived' + (d.tone ? ' ' + d.tone : ''));
      row.append(make('dt', '', d.label), make('dd', '', d.value));
      view.derived.appendChild(row);
    }
    view.derivedKey = dk;
  }
  return {captured, required, count};
}

function updateScript(st){
  const fields = (st && st.fields) || {};
  const handedOff = !!(st && st.done);
  const flags = {consent: !!(st && st.consent) || handedOff, done: handedOff};
  const visible = S.steps.map(s => {
    const w = whenState(s.step.when, fields);
    return w === 'yes' || (w === 'unknown' && !!s.step.suggested);
  });
  // A step is done if its own evidence is in, OR any later visible step's is. Bots don't
  // save every answer they hear, and the tracker must never sit stuck on one they skipped.
  let lastDone = -1;
  S.steps.forEach((s, i) => {
    if (!visible[i]) return;
    const own = s.step.flag
      ? !!flags[s.step.flag]
      : (s.step.fields || []).length > 0 && s.step.fields.every(k => val(fields, k));
    if (own) lastDone = i;
  });
  const finished = !!(st && !LIVE_OUTCOMES.has(st.outcome || ''));
  let num = 0, total = 0, done = 0, current = -1;
  S.steps.forEach((s, i) => {
    s.li.hidden = !visible[i];
    if (!visible[i]) return;
    total++; num++;
    const isDone = i <= lastDone;
    if (isDone) done++;
    const isNow = !isDone && current === -1 && !finished;
    if (isNow) current = i;
    s.li.classList.toggle('done', isDone);
    s.li.classList.toggle('now', isNow);
    s.n.textContent = isDone ? '✓' : String(num);
  });
  el.scriptProg.textContent = total ? `${done} of ${total}` : '';
  if (current !== S.curStep){
    S.curStep = current;
    if (current >= 0) keepInView(el.panelScript, S.steps[current].li);
  }
}

/* Scroll inside the panel only — never the page, which would yank a mobile visitor away
   from the controls mid-call. */
function keepInView(panel, node){
  if (panel.scrollHeight <= panel.clientHeight + 2) return;
  const top = node.offsetTop - 70;
  const bottom = node.offsetTop + node.offsetHeight - panel.clientHeight + 24;
  if (panel.scrollTop > top) panel.scrollTo({top: Math.max(top, 0), behavior:'smooth'});
  else if (panel.scrollTop < bottom) panel.scrollTo({top: bottom, behavior:'smooth'});
}

function outcomeInfo(bot, code){
  if (!bot || LIVE_OUTCOMES.has(code || '')) return null;
  return (bot.outcomes || {})[code] || {label: code, tone: ''};
}

function renderLive(){
  const bot = currentBot();
  if (!bot || !S.dataView) return;
  const st = S.state;
  updateScript(st);
  const c = updateData(S.dataView, st, true);
  el.dataProg.textContent = c.required ? `${c.captured} of ${c.required} required` : '';
  el.tabCount.textContent = String(c.count);

  const info = outcomeInfo(bot, st && st.outcome);
  el.chipOutcome.className = 'chip' + (info && info.tone ? ' ' + info.tone : '');
  el.chipOutcome.textContent = info ? info.label : 'In progress';
  // Per bot: some have nothing to consent to (null = no chip), some name it.
  const labels = ('consent_chip' in bot) ? bot.consent_chip : ['Consent given', 'No consent yet'];
  el.chipConsent.hidden = !labels;
  if (labels){
    const consent = !!(st && st.consent);
    el.chipConsent.className = 'chip' + (consent ? ' good' : '');
    el.chipConsent.textContent = consent ? labels[0] : labels[1];
  }
}

function setTab(tab){
  el.liveGrid.dataset.tab = tab;
  el.tabScript.setAttribute('aria-selected', String(tab === 'script'));
  el.tabData.setAttribute('aria-selected', String(tab === 'data'));
}
el.tabScript.addEventListener('click', () => setTab('script'));
el.tabData.addEventListener('click', () => setTab('data'));

/* ── Media ──────────────────────────────────────────────────────────────── */
function onTrackSubscribed(track, pub, participant){
  if (track.kind !== 'audio') return;
  S.audioEl = track.attach();
  S.audioEl.autoplay = true;
  document.body.appendChild(S.audioEl);
  startVisualizer(track.mediaStreamTrack);
  readAgentState(participant);
}

/* Drives the orb's scale from the agent's actual audio, so it pulses with real speech
   rather than a canned animation. */
function startVisualizer(mediaStreamTrack){
  try{
    S.ac = new (window.AudioContext || window.webkitAudioContext)();
    const src = S.ac.createMediaStreamSource(new MediaStream([mediaStreamTrack]));
    S.analyser = S.ac.createAnalyser();
    S.analyser.fftSize = 256; S.analyser.smoothingTimeConstant = 0.78;
    src.connect(S.analyser);   // analyser only — connecting to destination would play
                               // the agent twice, once here and once via the <audio>
    const buf = new Uint8Array(S.analyser.frequencyBinCount);
    const tick = () => {
      S.analyser.getByteFrequencyData(buf);
      let sum = 0;
      for (let i = 0; i < 40; i++) sum += buf[i];
      const amp = 1 + Math.min(sum / 40 / 255, 1) * 0.22;
      if (el.core) el.core.style.setProperty('--amp', amp.toFixed(3));
      S.raf = requestAnimationFrame(tick);
    };
    tick();
  }catch(_){ /* the orb is cosmetic — never let it break a call */ }
}

function stopVisualizer(){
  if (S.raf) cancelAnimationFrame(S.raf);
  S.raf = null;
  if (S.ac){ try{ S.ac.close(); }catch(_){} S.ac = null; }
  if (el.core) el.core.style.setProperty('--amp', '1');
}

/* Agent lifecycle comes from LiveKit itself as a participant attribute, so the orb
   reflects real state rather than a guess. */
function readAgentState(participant){
  const st = participant && participant.attributes && participant.attributes['lk.agent.state'];
  if (!st) return;
  el.orbState.textContent = {
    initializing:'getting ready', idle:'ready', listening:'listening',
    thinking:'thinking', speaking:'speaking',
  }[st] || st;
  const speaking = st === 'speaking';
  el.orb.classList.toggle('on', speaking);
  el.orb.classList.toggle('thinking', st === 'thinking');
  // Drift and amplitude both animate transform; only one may own it at a time.
  el.orb.classList.toggle('idle', !speaking && st !== 'thinking');
}

/* ── Data ───────────────────────────────────────────────────────────────── */
function onData(payload, _p, _kind, topic){
  let msg;
  try{ msg = JSON.parse(new TextDecoder().decode(payload)); }catch(_){ return; }
  if (topic === 'demo.state'){
    S.state = msg;
    S.outcome = msg.outcome || S.outcome;
    const b = msg.bot || {};
    if ((b.agent || '') !== (S.live.agent || '') || (b.company || '') !== (S.live.company || '')){
      S.live = {agent: b.agent || '', company: b.company || ''};
      refreshNames();
    }
    renderLive();
  } else if (topic === 'demo.error'){
    S.errorMsg = msg.message || 'This voicebot couldn’t start.';
    showErr(el.liveError, S.errorMsg);
    setStatus('bad', 'Unavailable');
  } else if (topic === 'demo.transcript'){
    if (msg.src !== 'stt') console.debug('[btbiz-demo] %s: %s', msg.role, msg.text);
  } else if (topic === 'demo.event'){
    console.debug('[btbiz-demo] event', msg.label);
  } else if (topic === 'demo.metrics'){
    console.debug('[btbiz-demo] %s %dms', msg.kind, msg.ms);
  }
}

/* ── Controls ───────────────────────────────────────────────────────────── */
el.btnMute.addEventListener('click', async () => {
  if (!S.room) return;
  const muted = el.btnMute.getAttribute('aria-pressed') === 'true';
  await S.room.localParticipant.setMicrophoneEnabled(muted);
  el.btnMute.setAttribute('aria-pressed', String(!muted));
  el.muteLabel.textContent = muted ? 'Mute' : 'Unmute';
  el.orb.classList.toggle('muted', !muted);
});
el.btnEnd.addEventListener('click', () => finish('You ended the call.'));
el.btnAgain.addEventListener('click', async () => {
  el.timerValue.hidden = true; el.timerValue.textContent = '00:00';
  if (S.cfg.signupRequired === false){ setReady(null); return; }
  // Re-read the pass so the remaining-calls count is current, and so an expired pass
  // lands on the form instead of failing on the next click.
  if (!(await refreshMe())) showStep('details');
});

/* ── Reset + teardown ───────────────────────────────────────────────────── */
function resetCall(){
  S.ended = false; S.outcome = ''; S.errorMsg = '';
  el.btnMute.setAttribute('aria-pressed', 'false');
  el.muteLabel.textContent = 'Mute';
  el.orb.classList.remove('muted', 'on', 'thinking');
  el.orbState.textContent = 'connecting…';
  showErr(el.liveError, '');
}

function renderSummary(){
  const bot = currentBot();
  const st = S.state;
  if (!bot || !st){ el.endSummary.hidden = true; return; }
  el.endBotName.textContent = bot.name;
  const view = buildData(el.endData, bot);
  const c = updateData(view, st, false);
  // After the call, only what was actually captured.
  for (const k in view.rows) if (!view.rows[k].v) view.rows[k].row.hidden = true;
  const info = outcomeInfo(bot, st.outcome);
  el.endOutcome.hidden = !info;
  if (info){
    el.endOutcome.className = 'chip' + (info.tone ? ' ' + info.tone : '');
    el.endOutcome.textContent = info.label;
  }
  el.endSummary.hidden = c.count === 0 && !(st.derived || []).length;
}

async function finish(reason){
  if (S.ended) return;
  S.ended = true;
  if (S.timer){ clearInterval(S.timer); S.timer = null; }
  const secs = S.t0 ? (Date.now() - S.t0) / 1000 : 0;
  stopVisualizer();
  el.orb.classList.remove('on', 'thinking', 'muted');
  if (S.audioEl){ S.audioEl.remove(); S.audioEl = null; }

  try{ if (S.room) await S.room.disconnect(); }catch(_){}
  // Guarantees the worker subprocess is reclaimed even if the tab was force-closed.
  try{
    if (S.roomName) await postJSON('/api/end', {room: S.roomName});
  }catch(_){}
  S.room = null;

  setStatus('idle', 'Ended');
  const info = outcomeInfo(currentBot(), S.outcome);
  const why = S.errorMsg || reason;
  el.endLede.textContent = `${why} Duration ${fmtTime(secs)}.` + (info ? ` ${info.label}.` : '');
  renderSummary();
  showView('ended');
}

window.addEventListener('beforeunload', () => { if (S.room) S.room.disconnect(); });

boot();
})();
