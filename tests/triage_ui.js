// Проверка логики окна разбора без браузера: кусок скрипта страницы + заглушка DOM + заглушка сервера.
// Запуск: node tests/triage_ui.js путь/к/index.html  (см. test_triage_ui.py). Раскладку и вид это не проверяет.
const fs = require('fs'), assert = require('assert');
const html = fs.readFileSync(process.argv[2], 'utf8');
const a = html.indexOf('// Разбор реестра Life Reset: один пункт'), b = html.indexOf('// Окно начала фокуса.');
assert(a > 0 && b > a, 'блок разбора не найден в странице');
const code = html.slice(a, b);

class Node_ { constructor(t) { this.tagName = t; this.children = []; this._text = ''; this.className = ''; this.hidden = false; this.attrs = {}; this.style = {}; this.dataset = {}; this.l = {}; this.onclick = null; this.value = ''; }
  set textContent(v) { this.children = []; this._text = String(v); }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(''); }
  append(...n) { n.forEach(x => this.children.push(typeof x === 'string' ? Object.assign(new Node_('#text'), { _text: x }) : x)); }
  setAttribute(k, v) { this.attrs[k] = String(v); } getAttribute(k) { return this.attrs[k]; }
  addEventListener(e, f) { (this.l[e] = this.l[e] || []).push(f); }
  querySelectorAll(sel) { const out = []; (function walk(n) { n.children.forEach(c => { if (sel === 'button' && c.tagName === 'BUTTON') out.push(c); walk(c); }); })(this); return out; }
  focus() {} click() { return this.onclick && this.onclick(); } remove() {} }
const ids = {};
const document = { getElementById: id => ids[id] || (ids[id] = new Node_('DIV')), createElement: t => new Node_(t.toUpperCase()), addEventListener(e, f) { (document.l[e] = document.l[e] || []).push(f); }, l: {} };
const walkAll = (n, f) => { f(n); n.children.forEach(c => walkAll(c, f)); };
const btn = (text) => { let hit = null; walkAll(document.getElementById('triBody'), n => { if (!hit && n.tagName === 'BUTTON' && n.textContent.includes(text)) hit = n; }); assert(hit, 'нет кнопки: ' + text + '\n' + document.getElementById('triBody').textContent); return hit; };
const body = () => document.getElementById('triBody').textContent;
const key = k => document.l.keydown.forEach(f => f({ key: k, target: { tagName: 'DIV' }, preventDefault() {} }));
const tick = () => new Promise(r => setTimeout(r, 5));

// --- заглушка сервера ---
let items, decided, capacity, calls, merged0; const areaCalls = [];
const reset = () => { items = [[1, 'Изучение японского языка'], [2, 'Балкон'], [3, 'Список книг']].map(([id, title]) => ({ id, code: 'LR-' + id, title, type: 'wish', type_title: 'желание', area: 'finance', area_title: 'Финансы', status: 'inbox', status_title: 'не разобрано', resurfaced: false, where: '2019-2024' })); decided = []; capacity = 0; calls = []; merged0 = 0; };
reset();
global.fetch = async (path, opts) => { const body = opts && opts.body ? JSON.parse(opts.body) : {}; calls.push([path, body]); const json = o => ({ status: 200, json: async () => o }); const u = new URL('http://x' + path);
  if (u.pathname === '/api/triage') { const ex = (u.searchParams.get('exclude') || '').split(',').filter(Boolean).map(Number); const rest = items.filter(i => !ex.includes(i.id)); return json({ total: items.length, area_total: items.length, area: u.searchParams.get('area') || '', areas: [{ key: 'finance', title: 'Финансы', n: items.length }], items: rest.slice(0, 2), more: rest.length > 2, capacity, active: 0, bulk: 0 }); }
  if (u.pathname === '/api/triage/card') { const it = [...items, ...decided].find(i => i.id === +u.searchParams.get('id')); return json(it ? { ...it, summary: 'Суть ' + it.title, want: '', hint: '', next_step: '', sources: [{ claim: 'Хочу', where: '«Заметка», 2020-05', quote: 'хочу начать' }], related: [], areas_all: [{ key: 'finance', title: 'Финансы' }, { key: 'hobbies_interests', title: 'Хобби и интересы' }], last_decision: null } : { error: 'gone' }); }
  if (u.pathname === '/api/triage/dedupe') return json(merged0 ? { merged: merged0, undo: 'm1', left: items.length } : { merged: 0 });
  if (u.pathname === '/api/triage/capacity') { capacity = body.n; return json({ ok: true }); }
  if (u.pathname === '/api/triage/decide') { if (body.status === 'active' && !capacity && !body.skip_capacity) return json({ need: 'capacity' });
    const k = items.findIndex(i => i.id === body.id); decided.push(items.splice(k, 1)[0]); return json({ ok: true, status: body.status, label: body.status, review_on: body.months ? '2027-02-14' : '', left: items.length, undo: 'd' + body.id }); }
  if (u.pathname === '/api/triage/undo' && body.undo[0] === 'a') { const c = areaCalls.pop(); const it = items.find(i => i.id === c[0]); it.area = c[1]; it.area_title = 'Финансы'; return json({ ok: true, left: items.length, restored: 1 }); }
  if (u.pathname === '/api/triage/undo') { const id = +body.undo.slice(1); items.unshift(decided.splice(decided.findIndex(i => i.id === id), 1)[0]); return json({ ok: true, left: items.length }); }
  if (u.pathname === '/api/triage/area') { const it = items.find(i => i.id === body.id); if (!it) return json({ error: 'gone' }); if (it.area === body.area) return json({ ok: true, same: true, area: it.area, area_title: it.area_title });
    const titles = { finance: 'Финансы', hobbies_interests: 'Хобби и интересы' }; const prev = it.area_title; areaCalls.push([it.id, it.area]); it.area = body.area; it.area_title = titles[body.area]; return json({ ok: true, area: body.area, area_title: it.area_title, prev_title: prev, left: items.length, undo: 'a' + body.id }); }
  if (u.pathname === '/api/triage/find') return json({ items: items.filter(i => i.title.toLowerCase().includes(u.searchParams.get('q').toLowerCase())).map(i => ({ ...i })) });
  if (u.pathname === '/api/triage/merge') { const k = items.findIndex(i => i.id === body.id); decided.push(items.splice(k, 1)[0]); return json({ ok: true, into: 'LR-' + body.into, left: items.length, undo: 'm9' }); }
  throw new Error('нет заглушки: ' + path); };
const post = (path, body) => fetch(path, { body: JSON.stringify(body) });
const showLogin = () => { throw new Error('showLogin'); }, refresh = () => {};
eval(code + '; globalThis.T = T; globalThis.openTriage = openTriage;');

(async () => {
  // 1. открытие: первый пункт, пять вариантов с пояснениями, счётчик
  await openTriage(''); await tick();
  assert(body().includes('Изучение японского языка') && body().includes('Что с этим делать?'), body());
  assert.strictEqual(T.opts.length, 5);
  assert(body().includes('Справка, список или просто сведения'), 'у «Не проект» нет пояснения');
  assert(document.getElementById('triCount').textContent.includes('ждут решения: 3'));
  // 2. «Не проект» на клавишу 4 – решение записано, показан следующий
  key('4'); await tick();
  const dec = calls.filter(c => c[0] === '/api/triage/decide').pop()[1];
  assert.strictEqual(dec.status, 'dismissed'); assert(body().includes('Балкон'), body());
  assert(document.getElementById('triMsg').textContent.includes('Записал'));
  // 3. «Делаю сейчас»: ёмкость спрашивается, можно «пока без лимита»
  key('1'); await tick(); btn('Делаю сейчас').click(); await tick();
  assert(body().includes('Сколько направлений'), body());
  btn('Пока без лимита').click(); await tick();
  assert(calls.filter(c => c[0] === '/api/triage/decide').pop()[1].skip_capacity === true);
  assert(body().includes('Список книг'), body());
  // 4. «Не уверен» → вопросы кнопками → «Не сейчас» → срок 3 мес. → «Когда-нибудь»
  key('5'); await tick(); assert(body().includes('всё ещё интересно'), body());
  key('1'); await tick(); assert(body().includes('Готов заняться'), body());
  key('2'); await tick(); assert(body().includes('Отложить: как именно?'), body());
  btn('3 мес.').click(); key('1'); await tick();
  const last = calls.filter(c => c[0] === '/api/triage/decide').pop()[1];
  assert.strictEqual(last.status, 'someday'); assert.strictEqual(last.months, 3);
  assert(last.answer.includes('Ещё интересно: да') && last.answer.includes('Готов сейчас: нет'), last.answer);
  // 5. очередь пуста
  assert(body().includes('Здесь всё разобрано'), body());
  // 6. отмена возвращает пункт и показывает его первым
  document.getElementById('triUndo').click(); await tick();
  assert(body().includes('Список книг') && document.getElementById('triMsg').textContent === 'Отменил.', body());
  // 7. пропуск: пункт уходит в конец, а не теряется
  reset(); await openTriage(''); await tick();
  btn('Пропустить').click(); await tick();
  assert(body().includes('Балкон'), body());
  btn('Пропустить').click(); await tick(); btn('Пропустить').click(); await tick();
  assert(body().includes('Изучение японского языка'), 'после круга пропусков вернулись к первому: ' + body());
  // 8. склейка через поиск
  btn('Это то же, что другой пункт').click(); await tick();
  const inp = (() => { let f = null; walkAll(document.getElementById('triBody'), n => { if (!f && n.tagName === 'INPUT') f = n; }); return f; })();
  inp.value = 'балк'; inp.oninput(); await new Promise(r => setTimeout(r, 300));
  btn('Балкон').click(); await tick();
  assert(calls.filter(c => c[0] === '/api/triage/merge').length === 1 && document.getElementById('triMsg').textContent.includes('Склеил'));
  // 9. Esc: из шага назад, из главного – закрыть
  reset(); await openTriage(''); await tick(); key('2'); await tick(); assert(body().includes('Отложить: как именно?'));
  document.l.keydown.forEach(f => f({ key: 'Escape', target: { tagName: 'DIV' }, preventDefault() {} })); await tick();
  assert(body().includes('Что с этим делать?'), 'Esc вернул на главный экран');
  // 10. автослияние при открытии показывает отмену
  reset(); merged0 = 2; await openTriage(''); await tick();
  assert(document.getElementById('triMsg').textContent.includes('Склеил пункты с одинаковым названием: 2') && !document.getElementById('triUndo').hidden);
  // 11. смена области выпадающим списком: пункт остаётся тем же, область обновляется, есть отмена, счётчик «разобрано» не меняется
  reset(); await openTriage(''); await tick();
  const sel = (() => { let f = null; walkAll(document.getElementById('triBody'), n => { if (!f && n.tagName === 'SELECT') f = n; }); return f; })();
  assert(sel, 'нет выпадающего списка области'); assert.strictEqual(sel.value, 'finance');
  assert.deepStrictEqual(sel.children.map(o => o.value), ['finance', 'hobbies_interests']);
  assert(!document.getElementById('triBody').textContent.split('Область:')[0].includes('Финансы'), 'область не дублируется в строке мета');
  sel.value = 'hobbies_interests'; await sel.onchange(); await tick();
  assert.strictEqual(calls.filter(c => c[0] === '/api/triage/area').pop()[1].area, 'hobbies_interests');
  assert(document.getElementById('triMsg').textContent.includes('«Финансы» → «Хобби и интересы»') && !document.getElementById('triUndo').hidden);
  assert(body().includes('Изучение японского языка'), 'после смены области пункт остался на экране: ' + body());
  assert.strictEqual(T.done, 0, 'смена области не считается разобранным');
  document.getElementById('triUndo').click(); await tick();
  assert.strictEqual(T.done, 0); assert.strictEqual(items[0].area, 'finance'); assert(document.getElementById('triMsg').textContent === 'Отменил.');
  console.log('ok');
})().catch(e => { console.error(e); process.exit(1); });
