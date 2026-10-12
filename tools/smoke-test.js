// Смоук-тест сайта: рендер в jsdom + проверка, что данные подхватились и обновление работает.
// jsdom берётся из NODE_PATH (в CI ставится во внешний каталог, чтобы не мусорить в репозитории)
const { JSDOM, VirtualConsole } = require('jsdom');
// Аргументом можно передать любой адрес: локальный сервер или уже опубликованный сайт.
//   node tools/smoke-test.js                                  -> http://127.0.0.1:8099/
//   node tools/smoke-test.js https://zxkittenxd.github.io/barsu-schedule/
const BASE = (process.argv[2] || 'http://127.0.0.1:8099/').replace(/\/?$/, '/');

const errors = [];
const vc = new VirtualConsole();
vc.on('jsdomError', e => errors.push('jsdomError: ' + (e.stack || e.message)));
vc.on('error', (...a) => errors.push('console.error: ' + a.join(' ')));
vc.on('warn', (...a) => { /* игнорируем предупреждения */ });

(async () => {
  const dom = await JSDOM.fromURL(BASE, {
    runScripts: 'dangerously',
    resources: 'usable',
    pretendToBeVisual: true,
    virtualConsole: vc,
    beforeParse(window) {
      // в jsdom нет fetch — проксируем в node-овский, разрешая относительные URL
      window.fetch = (u, o) => fetch(new URL(u, BASE).href, o);
      window.matchMedia = window.matchMedia || (() => ({ matches: false, addListener(){}, removeListener(){}, addEventListener(){}, removeEventListener(){} }));
      window.addEventListener('error', e => errors.push('window.error: ' + (e.error && e.error.stack || e.message)));
      window.addEventListener('unhandledrejection', e => errors.push('unhandledrejection: ' + e.reason));
    },
  });
  const { window } = dom;
  const doc = window.document;
  const sleep = ms => new Promise(r => setTimeout(r, ms));

  await sleep(4000); // ждём selectGroup -> fetch data/gNNN.json -> render

  const txt = id => (doc.getElementById(id) || {}).textContent || '';
  // const/let верхнего уровня не попадают в window — читаем их через eval в глобальной области
  const ev = expr => { try { return window.eval(expr); } catch (e) { return '⟨' + e.message + '⟩'; } };
  const report = [];
  const check = (name, cond, extra = '') => {
    report.push(`${cond ? '✅' : '❌'} ${name}${extra ? ' — ' + extra : ''}`);
    if (!cond) process.exitCode = 1;
  };

  check('групп в каталоге = 131', ev('GROUPS.length') === 131, ev('GROUPS.length') + '');
  check('выбрана группа по умолчанию', ev('state.group') === 'ПИТТ21', ev('state.group'));
  const wk = ev('state.weekISO');
  check('неделя выбрана', !!wk, wk || 'нет');
  check('есть данные на выбранную неделю', ev('!!state.schedules[state.weekISO]') === true,
        wk + ' / недель в state: ' + ev('Object.keys(state.schedules).length'));
  check('текущая неделя опубликована (PUBLISHED)', ev("PUBLISHED.has('2026-10-12')") === true, ev('[...PUBLISHED].slice(0,4).join()'));
  const lessons = ev(`(()=>{const s=state.schedules[state.weekISO];if(!s)return 0;
    return s.days.reduce((n,d)=>n+d.slots.reduce((m,x)=>m+x.lessons.length,0),0);})()`);
  check('на неделе есть занятия', lessons > 0, lessons + ' занятий');
  check('отрисовано расписание дня', /para|пара|–/i.test(txt('dayContent')), txt('dayContent').slice(0, 90).replace(/\s+/g, ' '));
  check('дни отрисованы', txt('daySeg').length > 0, txt('daySeg').replace(/\s+/g, ' ').trim().slice(0, 60));
  check('статус-чип показывает время снимка', /данные ·/.test(txt('statusText')), txt('statusText'));
  check('в подвале есть время сборки', /2026-/.test(txt('footMeta')), txt('footMeta'));
  check('модальное окно: число групп', /131 групп/.test(txt('modalFoot')), txt('modalFoot'));

  // ---- эмуляция нажатия «обновить»
  window.document.getElementById('refreshBtn').click();
  await sleep(2500);
  check('кнопка «обновить» отработала без ошибок', !window.document.getElementById('refreshBtn').classList.contains('spinning'));
  check('после обновления данные на месте', ev('!!state.schedules[state.weekISO]') === true);
  check('после обновления status не «офлайн-кэш»', !/офлайн/.test(txt('statusText')), txt('statusText'));

  // ---- эмуляция «опубликован новый снимок» -> checkForUpdate
  const before = ev('state.fetchedAt');
  const realFetch = window.fetch;
  window.fetch = (u, o) => {
    if (String(u).includes('version.json')) {
      return realFetch(new URL('data/version.json', BASE).href, o).then(r => r.json())
        .then(v => ({ ok: true, status: 200, json: async () => ({ ...v, fetchedAt: '2099-01-01 00:00:00' }) }));
    }
    return realFetch(u, o);
  };
  await window.checkForUpdate();
  await sleep(1500);
  check('checkForUpdate подхватил новый снимок', ev('state.fetchedAt') !== before, before + ' -> ' + ev('state.fetchedAt'));
  check('данные после автообновления на месте',
        ev('!!state.schedules[state.weekISO]') === true);

  console.log('\n=== РЕЗУЛЬТАТ РЕНДЕР-ТЕСТА ===');
  console.log(report.join('\n'));
  const real = errors.filter(e => !/Could not parse CSS|Not implemented: navigation|Error: Not implemented/i.test(e));
  console.log('\n=== ОШИБКИ В КОНСОЛИ ===');
  console.log(real.length ? real.slice(0, 12).join('\n') : 'нет');
  if (real.length) process.exitCode = 1;
  window.close();
})();
