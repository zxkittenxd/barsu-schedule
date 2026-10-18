// Смоук-тест страницы «Расписание БарГУ»: реальный рендер в jsdom.
//
//   node tools/smoke-test.js                                   -> http://127.0.0.1:8099/
//   node tools/smoke-test.js https://zxkittenxd.github.io/barsu-schedule/
//
// ВАЖНО: тест не содержит захардкоженных чисел групп и дат. Ожидания читаются из
// опубликованного data/version.json, то есть проверяется согласованность
// «страница == данные». Благодаря этому добавление университетом новых групп
// или смена недели больше не роняют тест (инцидент 17.10.2026: жёсткое «131»
// заблокировало публикацию, когда групп стало 134).
const { JSDOM, VirtualConsole } = require('jsdom');
const BASE = (process.argv[2] || 'http://127.0.0.1:8099/').replace(/\/?$/, '/');

const errors = [];
const vc = new VirtualConsole();
vc.on('jsdomError', e => errors.push('jsdomError: ' + (e.stack || e.message)));
vc.on('error', (...a) => errors.push('console.error: ' + a.join(' ')));
vc.on('warn', () => { /* предупреждения не интересуют */ });

const sleep = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  // --- ожидания из опубликованных данных, а не из констант
  let version = null;
  try {
    version = await fetch(new URL('data/version.json?_=' + Date.now(), BASE), { cache: 'no-store' })
      .then(r => { if (!r.ok) throw new Error('http ' + r.status); return r.json(); });
  } catch (e) {
    console.log('❌ не удалось прочитать data/version.json: ' + e.message);
    process.exit(1);
  }
  const expGroups = version.groups;
  const expPublished = version.publishedWeeks || [];

  const dom = await JSDOM.fromURL(BASE, {
    runScripts: 'dangerously',
    resources: 'usable',
    pretendToBeVisual: true,
    virtualConsole: vc,
    beforeParse(window) {
      // в jsdom нет fetch — проксируем в node-овский, разрешая относительные URL
      window.fetch = (u, o) => fetch(new URL(u, BASE).href, o);
      window.matchMedia = window.matchMedia || (() => ({
        matches: false, addListener(){}, removeListener(){}, addEventListener(){}, removeEventListener(){},
      }));
      window.addEventListener('error', e => errors.push('window.error: ' + ((e.error && e.error.stack) || e.message)));
      window.addEventListener('unhandledrejection', e => errors.push('unhandledrejection: ' + e.reason));
    },
  });
  const { window } = dom;
  const doc = window.document;

  await sleep(4000); // ждём selectGroup -> fetch data/gNNN.json -> render

  const txt = id => (doc.getElementById(id) || {}).textContent || '';
  // const/let верхнего уровня не попадают в window — читаем их eval-ом в глобальной области
  const ev = expr => { try { return window.eval(expr); } catch (e) { return '⟨' + e.message + '⟩'; } };

  const report = [];
  const check = (name, cond, extra = '') => {
    report.push(`${cond ? '✅' : '❌'} ${name}${extra ? ' — ' + extra : ''}`);
    if (!cond) process.exitCode = 1;
  };
  const skip = name => report.push('ℹ️ ' + name + ' — проверка пропущена (данные не требуют)');

  // --- каталог и состояние страницы согласованы с version.json
  check(`групп на странице = групп в version.json (${expGroups})`,
        ev('GROUPS.length') === expGroups, 'на странице: ' + ev('GROUPS.length'));
  check('выбрана группа по умолчанию', ev('state.group') === ev('DEFAULT_GROUP'), ev('state.group'));
  const wk = ev('state.weekISO');
  check('неделя выбрана', !!wk, String(wk));
  check('на выбранную неделю есть данные', ev('!!state.schedules[state.weekISO]') === true,
        wk + ' / недель в state: ' + ev('Object.keys(state.schedules).length'));

  // --- множество опубликованных недель на странице == множеству в version.json
  const pubPage = ev('[...PUBLISHED].sort().join()');
  const pubExp = [...expPublished].sort().join();
  check('опубликованные недели страницы совпадают с version.json',
        pubPage === pubExp, pubPage + ' | ожидалось: ' + pubExp);

  // --- занятия: требуем только если университет реально опубликовал их для этой группы/недели
  const defWeeks = ev('(GMAP[state.group] && GMAP[state.group].weeks) || []');
  const lessons = ev('(()=>{const s=state.schedules[state.weekISO];if(!s)return 0;' +
                     'return s.days.reduce((n,d)=>n+d.slots.reduce((m,x)=>m+x.lessons.length,0),0);})()');
  if (Array.isArray(defWeeks) && defWeeks.includes(wk)) {
    check('на выбранной неделе есть занятия', lessons > 0, lessons + ' занятий');
    check('отрисовано расписание дня', /para|пара/i.test(txt('dayContent')),
          txt('dayContent').slice(0, 80).replace(/\s+/g, ' '));
  } else {
    skip('занятия на выбранной неделе (каникулы или неделя без пар)');
    check('отрисована заглушка пустой недели', /не опубликовано|не загружены|Перечитать/i.test(txt('dayContent')),
          txt('dayContent').slice(0, 80).replace(/\s+/g, ' '));
  }
  check('дни отрисованы', txt('daySeg').length > 0, txt('daySeg').replace(/\s+/g, ' ').trim().slice(0, 60));
  check('статус-чип показывает время данных', /·/.test(txt('statusText')), txt('statusText'));
  check('в подвале есть дата сборки', /\d{4}-\d{2}-\d{2}/.test(txt('footMeta')), txt('footMeta'));
  check(`модальное окно: число групп (${expGroups})`,
        new RegExp(expGroups + ' групп').test(txt('modalFoot')), txt('modalFoot'));

  // --- кнопка «обновить»
  doc.getElementById('refreshBtn').click();
  await sleep(2500);
  check('кнопка «обновить» отработала', !doc.getElementById('refreshBtn').classList.contains('spinning'));
  check('после обновления данные на месте', ev('!!state.schedules[state.weekISO]') === true);
  check('после обновления статус не «офлайн-кэш»', !/офлайн/.test(txt('statusText')), txt('statusText'));

  // --- эмуляция «GitHub Actions опубликовал новый снимок»
  const before = ev('state.fetchedAt');
  const realFetch = window.fetch;
  window.fetch = (u, o) => {
    if (String(u).includes('version.json')) {
      return realFetch(new URL('data/version.json', BASE).href, o)
        .then(r => r.json())
        .then(v => ({ ok: true, status: 200, json: async () => ({ ...v, fetchedAt: '2099-01-01 00:00:00' }) }));
    }
    return realFetch(u, o);
  };
  await window.checkForUpdate();
  await sleep(1500);
  check('checkForUpdate подхватил новый снимок', ev('state.fetchedAt') !== before,
        before + ' -> ' + ev('state.fetchedAt'));
  check('данные после автообновления на месте', ev('!!state.schedules[state.weekISO]') === true);

  console.log('\n=== РЕЗУЛЬТАТ РЕНДЕР-ТЕСТА (' + BASE + ') ===');
  console.log(report.join('\n'));
  const real = errors.filter(e => !/Could not parse CSS|Not implemented/i.test(e));
  console.log('\n=== ОШИБКИ В КОНСОЛИ ===');
  console.log(real.length ? real.slice(0, 12).join('\n') : 'нет');
  if (real.length) process.exitCode = 1;
  window.close();
})();
