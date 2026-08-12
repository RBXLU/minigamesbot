'use strict';

const tg = window.Telegram && window.Telegram.WebApp;
const screenEl = document.getElementById('screen');
const titleEl = document.getElementById('title');
const coinsEl = document.getElementById('coins');
const backEl = document.getElementById('back');
const toastEl = document.getElementById('toast');
const layoutToggleEl = document.getElementById('layout-toggle');

let CFG = null;
let PROFILE = null;
let currentCleanup = null;
let currentScreen = null;

/* ---------- режим отображения ---------- */

const MOBILE_PLATFORMS = ['android', 'android_x', 'ios'];
const LAYOUT_KEY = 'minigames:layout';

/** Телефон определяем по платформе Telegram, типу указателя и ширине экрана. */
function detectDevice() {
  const platform = (tg && tg.platform) || 'unknown';
  if (MOBILE_PLATFORMS.includes(platform)) return 'mobile';
  if (platform === 'tdesktop' || platform === 'macos' || platform === 'linux') return 'desktop';
  const coarse = window.matchMedia('(pointer: coarse)').matches;
  return coarse || window.innerWidth < 560 ? 'mobile' : 'desktop';
}

function storedLayout() {
  try {
    return localStorage.getItem(LAYOUT_KEY);
  } catch (e) {
    return null;
  }
}

function setLayout(mode, remember) {
  document.documentElement.setAttribute('data-device', mode);
  layoutToggleEl.textContent = mode === 'mobile' ? '🖥' : '📱';
  layoutToggleEl.title = mode === 'mobile' ? 'Раскладка для широкого экрана' : 'Мобильная раскладка';
  if (remember) {
    try { localStorage.setItem(LAYOUT_KEY, mode); } catch (e) { /* приватный режим */ }
  }
}

function currentLayout() {
  return document.documentElement.getAttribute('data-device') || 'mobile';
}

layoutToggleEl.addEventListener('click', () => {
  const next = currentLayout() === 'mobile' ? 'desktop' : 'mobile';
  setLayout(next, true);
  haptic('tap');
  toast(next === 'mobile' ? 'Мобильная раскладка' : 'Раскладка для широкого экрана', 1400);
  if (currentScreen) currentScreen();
});

/** Подгоняет высоту под окно Telegram и учитывает вырезы экрана. */
function applyViewport() {
  const root = document.documentElement;
  const height = (tg && tg.viewportStableHeight) || window.innerHeight;
  root.style.setProperty('--app-height', height + 'px');

  const safe = (tg && (tg.contentSafeAreaInset || tg.safeAreaInset)) || {};
  root.style.setProperty('--safe-top', (safe.top || 0) + 'px');
  root.style.setProperty('--safe-bottom', (safe.bottom || 0) + 'px');
}

/* ---------- инфраструктура ---------- */

const REDUCED_MOTION = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function ripple(event, node) {
  if (REDUCED_MOTION) return;
  const rect = node.getBoundingClientRect();
  const size = Math.max(rect.width, rect.height);
  const wave = el('span', 'ripple');
  wave.style.width = wave.style.height = size + 'px';
  wave.style.left = ((event.clientX || rect.left + rect.width / 2) - rect.left - size / 2) + 'px';
  wave.style.top = ((event.clientY || rect.top + rect.height / 2) - rect.top - size / 2) + 'px';
  node.appendChild(wave);
  setTimeout(() => wave.remove(), 600);
}

function button(label, className, onClick) {
  const b = el('button', className || 'btn', label);
  b.addEventListener('click', event => {
    ripple(event, b);
    haptic('tap');
    onClick(event);
  });
  return b;
}

/** Короткая анимация-подсветка: снимает класс, чтобы её можно было повторить. */
function replay(node, className) {
  if (!node || REDUCED_MOTION) return;
  node.classList.remove(className);
  void node.offsetWidth;
  node.classList.add(className);
}

/** Плитки и клетки появляются волной, а не все разом. */
function stagger(nodes, step) {
  if (REDUCED_MOTION) return;
  Array.from(nodes).forEach((node, i) => {
    node.style.animationDelay = Math.min(i * (step || 22), 400) + 'ms';
  });
}

function toast(text, ms) {
  toastEl.textContent = text;
  toastEl.hidden = false;
  replay(toastEl, 'toast');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { toastEl.hidden = true; }, ms || 1800);
}

function haptic(type) {
  try {
    if (!tg || !tg.HapticFeedback) return;
    if (type === 'win') tg.HapticFeedback.notificationOccurred('success');
    else if (type === 'lose') tg.HapticFeedback.notificationOccurred('error');
    else tg.HapticFeedback.impactOccurred('light');
  } catch (e) { /* haptics недоступны в браузере */ }
}

function randInt(n) { return Math.floor(Math.random() * n); }
function pick(list) { return list[randInt(list.length)]; }
function newSession() { return Date.now().toString(36) + Math.random().toString(36).slice(2, 7); }

async function api(path, body) {
  const res = await fetch(path, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Telegram-Init-Data': (tg && tg.initData) || ''
    },
    body: JSON.stringify(body || {})
  });
  if (!res.ok) throw new Error('HTTP ' + res.status);
  return res.json();
}

/** Плавно докручивает число монет и подсвечивает прибыль/убыль. */
function setCoins(profile) {
  if (!profile) return;
  const previous = PROFILE ? PROFILE.coins : null;
  PROFILE = profile;

  if (previous === null || previous === profile.coins || REDUCED_MOTION) {
    coinsEl.textContent = '🪙 ' + profile.coins;
    return;
  }

  coinsEl.classList.toggle('up', profile.coins > previous);
  coinsEl.classList.toggle('down', profile.coins < previous);
  replay(coinsEl, 'bump');

  const start = performance.now();
  const delta = profile.coins - previous;
  const step = now => {
    const t = Math.min(1, (now - start) / 550);
    const eased = 1 - Math.pow(1 - t, 3);
    coinsEl.textContent = '🪙 ' + Math.round(previous + delta * eased);
    if (t < 1) requestAnimationFrame(step);
    else setTimeout(() => coinsEl.classList.remove('up', 'down'), 500);
  };
  requestAnimationFrame(step);
}

async function report(payload) {
  try {
    setCoins(await api('/api/report', payload));
  } catch (e) {
    toast('Не удалось сохранить результат');
  }
}

/* ---------- навигация ---------- */

function openScreen(title, showBack, builder) {
  currentScreen = () => openScreen(title, showBack, builder);
  if (typeof currentCleanup === 'function') currentCleanup();
  releaseKeys();
  currentCleanup = null;
  screenEl.innerHTML = '';
  titleEl.textContent = title;
  replay(titleEl, 'slide-in');
  backEl.hidden = !showBack;
  if (tg && tg.BackButton) showBack ? tg.BackButton.show() : tg.BackButton.hide();
  replay(screenEl, 'swap');
  currentCleanup = builder(screenEl) || null;
  window.scrollTo(0, 0);
}

/* ---------- управление с клавиатуры (широкий экран) ---------- */

let keyHandler = null;

/** Вешает горячие клавиши на текущий экран; снимаются при переходе.
 *  onChar получает любой одиночный символ, не описанный в map. */
function bindKeys(map, onChar) {
  releaseKeys();
  keyHandler = event => {
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    const action = map[event.key] || map[event.key.toLowerCase()];
    if (action) {
      event.preventDefault();
      action(event.key);
      return;
    }
    if (onChar && event.key.length === 1) {
      event.preventDefault();
      onChar(event.key.toLowerCase());
    }
  };
  window.addEventListener('keydown', keyHandler);
}

function releaseKeys() {
  if (keyHandler) window.removeEventListener('keydown', keyHandler);
  keyHandler = null;
}

/** Стрелки и WASD для игр с направлениями. */
function bindArrows(onMove, extra) {
  const map = {
    ArrowUp: () => onMove('up'), ArrowDown: () => onMove('down'),
    ArrowLeft: () => onMove('left'), ArrowRight: () => onMove('right'),
    w: () => onMove('up'), s: () => onMove('down'),
    a: () => onMove('left'), d: () => onMove('right'),
    ц: () => onMove('up'), ы: () => onMove('down'),
    ф: () => onMove('left'), в: () => onMove('right')
  };
  bindKeys(Object.assign(map, extra || {}));
}

function goHome() {
  openScreen('Мини-игры', false, buildHome);
}

backEl.addEventListener('click', goHome);

/* ---------- главный экран ---------- */

const CATEGORY_TITLES = {
  solo: 'Одиночные',
  luck: 'На удачу',
  cards: 'Карточные',
  vs_bot: 'Против бота',
  quiz: 'Вопросы'
};

function progressBar(label, value, total, good) {
  const wrap = el('div');
  const head = el('div', 'bar-label');
  head.appendChild(el('span', null, label));
  head.appendChild(el('span', null, value + ' / ' + total));
  const bar = el('div', 'bar' + (good ? ' good' : ''));
  const fill = el('i');
  bar.appendChild(fill);
  wrap.appendChild(head);
  wrap.appendChild(bar);
  // ширину задаём после вставки, чтобы полоса заполнялась анимацией
  requestAnimationFrame(() => {
    fill.style.width = Math.min(100, total ? (value / total) * 100 : 0) + '%';
  });
  return wrap;
}

function buildHome(root) {
  const panel = el('div', 'panel');

  const hero = el('div', 'hero');
  hero.appendChild(el('div', 'avatar', (PROFILE && PROFILE.avatar) || '🙂'));
  const who = el('div', 'who');
  who.appendChild(el('div', 'name', (PROFILE && PROFILE.name) || 'Игрок'));
  who.appendChild(el('div', 'sub', PROFILE && PROFILE.streak
    ? 'серия ' + PROFILE.streak + ' дн. подряд'
    : 'сыграйте партию, чтобы начать серию'));
  hero.appendChild(who);
  panel.appendChild(hero);

  const stats = el('div', 'stat-row');
  const values = [
    [PROFILE ? PROFILE.games_total : '—', 'партий'],
    [PROFILE ? PROFILE.coins : '—', 'монет'],
    [PROFILE ? PROFILE.streak : '—', 'серия'],
    [PROFILE ? PROFILE.achievements + '/' + PROFILE.achievements_total : '—', 'достижений']
  ];
  values.forEach(([value, label]) => {
    const box = el('div', 'stat');
    box.appendChild(el('b', null, String(value)));
    box.appendChild(el('span', null, label));
    stats.appendChild(box);
  });
  stagger(stats.children, 60);
  panel.appendChild(stats);

  if (PROFILE) {
    const bars = el('div', 'bars');
    if (PROFILE.achievements_total) {
      bars.appendChild(progressBar('Достижения', PROFILE.achievements, PROFILE.achievements_total));
    }
    if (PROFILE.quests_total) {
      bars.appendChild(progressBar('Квесты', PROFILE.quests_done, PROFILE.quests_total, true));
    }
    if (bars.children.length) panel.appendChild(bars);
  }

  root.appendChild(panel);

  const byCategory = {};
  CFG.games.forEach(([key, name, emoji, category]) => {
    (byCategory[category] = byCategory[category] || []).push([key, name, emoji]);
  });

  Object.keys(CATEGORY_TITLES).forEach(category => {
    const items = byCategory[category];
    if (!items) return;
    root.appendChild(el('div', 'section-title', CATEGORY_TITLES[category]));
    const grid = el('div', 'grid');
    items.forEach(([key, name, emoji]) => {
      const tile = el('button', 'tile');
      tile.appendChild(el('span', 'emoji', emoji));
      const label = el('span');
      label.appendChild(document.createTextNode(name));
      const played = PROFILE && PROFILE.game_stats[key] ? PROFILE.game_stats[key].played : 0;
      if (played) label.appendChild(el('span', 'played', 'сыграно: ' + played));
      tile.appendChild(label);
      tile.addEventListener('click', event => {
        ripple(event, tile);
        haptic('tap');
        openGame(key, name);
      });
      grid.appendChild(tile);
    });
    stagger(grid.children);
    root.appendChild(grid);
  });

  root.appendChild(el('div', 'section-title', 'Игры в чате'));
  const chatGrid = el('div', 'grid');
  CFG.chat_only.forEach(([key, name, emoji, query]) => {
    const tile = el('button', 'tile');
    tile.appendChild(el('span', 'emoji', emoji));
    tile.appendChild(el('span', null, name));
    tile.addEventListener('click', event => {
      ripple(event, tile);
      openChatGame(name, query);
    });
    chatGrid.appendChild(tile);
  });
  stagger(chatGrid.children);
  root.appendChild(chatGrid);
  root.appendChild(el('div', 'chat-note',
    'Этим играм нужен второй игрок — они запускаются в чате через инлайн-режим бота.'));
}

function openChatGame(name, query) {
  if (tg && typeof tg.switchInlineQuery === 'function' && query) {
    try {
      tg.switchInlineQuery(query, ['users', 'groups', 'channels']);
      return;
    } catch (e) { /* режим недоступен — покажем инструкцию */ }
  }
  toast('Напишите в чате @' + CFG.bot_username + ' ' + (query || name), 3200);
}

/* ---------- запуск игры ---------- */

const GAMES = {};

function openGame(key, name) {
  const builder = GAMES[key];
  if (!builder) {
    toast('Игра пока недоступна');
    return;
  }
  const session = newSession();
  let reported = false;
  const ctx = {
    session,
    cfg: CFG,
    // Итог партии: play засчитывается один раз, результат — опционально
    finish(result, extra) {
      const payload = Object.assign({ game: key, session, result: result || null }, extra || {});
      reported = true;
      haptic(result === 'wins' ? 'win' : result === 'losses' ? 'lose' : 'tap');
      report(payload);
    },
    started() {
      if (!reported) {
        reported = true;
        report({ game: key, session, result: null });
      }
    }
  };
  openScreen(name, true, root => builder(root, ctx));
}

/* ---------- вспомогательные элементы игр ---------- */

/** Строит клавиатуру рядами, как настоящую раскладку. */
function keyboard(container, rows, onKey, isUsed) {
  container.innerHTML = '';
  rows.forEach(row => {
    const line = el('div', 'key-row');
    row.forEach(item => {
      const [label, value, extraClass] = Array.isArray(item) ? item : [item.toUpperCase(), item, ''];
      const key = el('button', 'key' + (extraClass ? ' ' + extraClass : ''), label);
      if (isUsed && isUsed(value)) key.classList.add('used');
      key.addEventListener('click', () => { haptic('tap'); onKey(value); });
      line.appendChild(key);
    });
    container.appendChild(line);
  });
}

const RU_ROWS = ['йцукенгшщзх', 'фывапролджэ', 'ячсмитьбю'].map(r => r.split(''));
const RU_FULL_ROWS = ['абвгдеёжзийк', 'лмнопрстуфхц', 'чшщъыьэюя'].map(r => r.split(''));

function statusLine(root, text) {
  const node = el('div', 'status', text || '');
  root.appendChild(node);
  return node;
}

/** Показывает итог партии: текст + цвет + короткая вспышка. */
function showOutcome(node, text, result) {
  node.textContent = text;
  node.classList.toggle('win', result === 'wins');
  node.classList.toggle('lose', result === 'losses');
  replay(node, 'flash');
}

function hintLine(root, text) {
  root.appendChild(el('div', 'hint', text));
}

function makeBoard(root, cols, cellSize, gap) {
  const board = el('div', 'board');
  board.style.gridTemplateColumns = `repeat(${cols}, ${cellSize}px)`;
  if (gap) board.style.gap = gap + 'px';
  root.appendChild(board);
  return board;
}

function fitCell(cols, max, padding) {
  const available = (screenEl.clientWidth || 340) - (padding || 0);
  const limit = currentLayout() === 'desktop' ? Math.max(max || 0, 440) : 460;
  const width = Math.min(available, limit);
  return Math.max(20, Math.floor((width - (cols - 1) * 4) / cols));
}

function addSwipe(target, onSwipe) {
  let x0 = null, y0 = null;
  target.addEventListener('touchstart', e => {
    x0 = e.touches[0].clientX;
    y0 = e.touches[0].clientY;
  }, { passive: true });
  target.addEventListener('touchend', e => {
    if (x0 === null) return;
    const dx = e.changedTouches[0].clientX - x0;
    const dy = e.changedTouches[0].clientY - y0;
    x0 = null;
    if (Math.max(Math.abs(dx), Math.abs(dy)) < 24) return;
    onSwipe(Math.abs(dx) > Math.abs(dy) ? (dx > 0 ? 'right' : 'left') : (dy > 0 ? 'down' : 'up'));
  }, { passive: true });
}

function dpad(root, onMove, extraLabel, onExtra) {
  const pad = el('div', 'dpad');
  bindArrows(onMove, extraLabel ? { ' ': onExtra, Enter: onExtra } : null);
  root.appendChild(el('div', 'hint hint-keys',
    extraLabel ? 'Клавиши: стрелки или WASD, пробел — сбросить' : 'Клавиши: стрелки или WASD'));
  const mk = (label, dir) => button(label, 'btn', () => onMove(dir));
  pad.appendChild(el('div', 'spacer'));
  pad.appendChild(mk('▲', 'up'));
  pad.appendChild(el('div', 'spacer'));
  pad.appendChild(mk('◀', 'left'));
  pad.appendChild(extraLabel ? button(extraLabel, 'btn', onExtra) : mk('▼', 'down'));
  pad.appendChild(mk('▶', 'right'));
  root.appendChild(pad);
  return pad;
}

/* ================= ИГРЫ ================= */

/* Камень-ножницы-бумага */
GAMES.rps = function (root, ctx) {
  const MOVES = { rock: '🪨 Камень', paper: '📄 Бумага', scissors: '✂️ Ножницы' };
  const BEATS = { rock: 'scissors', paper: 'rock', scissors: 'paper' };
  const status = statusLine(root, 'Выберите ход');
  hintLine(root, 'Три варианта, один бросок.');

  const row = el('div', 'row');
  Object.keys(MOVES).forEach(move => {
    row.appendChild(button(MOVES[move], 'btn', () => {
      const botMove = pick(Object.keys(MOVES));
      let result;
      if (move === botMove) result = 'draws';
      else if (BEATS[move] === botMove) result = 'wins';
      else result = 'losses';
      status.style.whiteSpace = 'pre-line';
      showOutcome(status, `Вы: ${MOVES[move]}\nБот: ${MOVES[botMove]}\n` +
        (result === 'wins' ? '🎉 Победа!' : result === 'draws' ? '🤝 Ничья' : '😢 Поражение'), result);
      ctx.finish(result, { opponent: 'бот' });
    }));
  });
  root.appendChild(row);
};

/* Крестики-нолики против бота */
GAMES.ttt = function (root, ctx) {
  const WINS = [[0,1,2],[3,4,5],[6,7,8],[0,3,6],[1,4,7],[2,5,8],[0,4,8],[2,4,6]];
  let board, over;
  const status = statusLine(root, 'Ваш ход — ❌');
  const size = Math.min(88, fitCell(3, 260, 40));
  const boardEl = makeBoard(root, 3, size);

  function winner(b, s) { return WINS.some(line => line.every(i => b[i] === s)); }

  function botMove() {
    const free = board.map((v, i) => v ? null : i).filter(i => i !== null);
    for (const symbol of ['⭕', '❌']) {
      for (const i of free) {
        const copy = board.slice();
        copy[i] = symbol;
        if (winner(copy, symbol)) return i;
      }
    }
    if (board[4] === '') return 4;
    return pick(free);
  }

  function render(justPlaced) {
    boardEl.innerHTML = '';
    board.forEach((value, index) => {
      const cell = el('button', 'cell', value || '');
      cell.style.height = size + 'px';
      cell.style.fontSize = Math.round(size * 0.5) + 'px';
      if (value && (justPlaced === undefined || justPlaced === index)) cell.classList.add('pop');
      cell.addEventListener('click', () => play(index));
      boardEl.appendChild(cell);
    });
  }

  function finish(result) {
    over = true;
    showOutcome(status, result === 'wins' ? '🎉 Вы победили!'
      : result === 'losses' ? '💀 Бот победил' : '🤝 Ничья', result);
    ctx.finish(result, { opponent: 'бот' });
  }

  function play(index) {
    if (over || board[index]) return;
    board[index] = '❌';
    render(index);
    if (winner(board, '❌')) return finish('wins');
    if (board.every(Boolean)) return finish('draws');

    const botIndex = botMove();
    board[botIndex] = '⭕';
    render(botIndex);
    if (winner(board, '⭕')) return finish('losses');
    if (board.every(Boolean)) return finish('draws');
    status.textContent = 'Ваш ход — ❌';
  }

  function reset() {
    board = Array(9).fill('');
    over = false;
    status.textContent = 'Ваш ход — ❌';
    render();
  }

  root.appendChild(button('🔁 Новая партия', 'btn wide secondary', reset));
  reset();
};

/* Миллионер */
GAMES.millionaire = function (root, ctx) {
  const question = pick(ctx.cfg.millionaire_questions);
  let attempts = 3;
  const status = statusLine(root, question.question);
  const left = el('div', 'hint', 'Осталось попыток: 3');
  root.appendChild(left);
  const box = el('div');
  root.appendChild(box);

  question.options.forEach(option => {
    const b = button(option, 'btn wide secondary', () => {
      if (attempts <= 0) return;
      if (option === question.answer) {
        showOutcome(status, '🎉 Правильно: ' + option, 'wins');
        box.querySelectorAll('button').forEach(x => { x.disabled = true; });
        attempts = 0;
        ctx.finish('wins');
        return;
      }
      attempts -= 1;
      replay(b, 'shake');
      b.disabled = true;
      left.textContent = 'Осталось попыток: ' + attempts;
      if (attempts <= 0) {
        showOutcome(status, '💀 Правильный ответ: ' + question.answer, 'losses');
        box.querySelectorAll('button').forEach(x => { x.disabled = true; });
        ctx.finish('losses');
      }
    });
    box.appendChild(b);
  });
};

/* Орёл или решка */
GAMES.coin = function (root, ctx) {
  const status = statusLine(root, 'Загадайте сторону');
  hintLine(root, 'Чистая удача — 50 на 50.');
  const row = el('div', 'row');
  [['Орёл', 'Орёл'], ['Решка', 'Решка']].forEach(([label, side]) => {
    row.appendChild(button('🪙 ' + label, 'btn', () => {
      const got = Math.random() < 0.5 ? 'Орёл' : 'Решка';
      const win = got === side;
      showOutcome(status, `Выпало: ${got} — ${win ? '🎉 угадали!' : '😢 мимо'}`, win ? 'wins' : 'losses');
      ctx.finish(win ? 'wins' : 'losses');
    }));
  });
  root.appendChild(row);
};

/* Угадай число */
GAMES.guess = function (root, ctx) {
  let target = 1 + randInt(10);
  let attempts = 3;
  const status = statusLine(root, 'Загадано число от 1 до 10');
  const left = el('div', 'hint', 'Попыток: 3');
  root.appendChild(left);

  const grid = makeBoard(root, 5, fitCell(5, 300, 40));
  const buttons = [];
  for (let i = 1; i <= 10; i++) {
    const cell = el('button', 'cell', String(i));
    cell.style.height = '46px';
    cell.addEventListener('click', () => guess(i, cell));
    buttons.push(cell);
    grid.appendChild(cell);
  }

  function stop(text, result) {
    showOutcome(status, text, result);
    buttons.forEach(b => { b.disabled = true; });
    ctx.finish(result);
  }

  function guess(value, cell) {
    if (attempts <= 0) return;
    if (value === target) {
      replay(cell, 'pop');
      return stop('🎉 Верно! Это ' + target, 'wins');
    }
    replay(cell, 'shake');
    cell.disabled = true;
    attempts -= 1;
    left.textContent = 'Попыток: ' + attempts;
    if (attempts <= 0) return stop('💀 Было загадано ' + target, 'losses');
    status.textContent = value > target ? 'Загаданное меньше' : 'Загаданное больше';
  }
};

/* Блиц-реакция */
GAMES.reaction = function (root, ctx) {
  let armed = false, startAt = 0, timer = null;
  const status = statusLine(root, 'Нажмите «Старт» и ждите сигнала');
  const pad = button('▶️ Старт', 'btn wide', start);
  root.appendChild(pad);

  function start() {
    if (timer) return;
    armed = false;
    pad.textContent = '⏳ Ждите...';
    status.textContent = 'Не спешите — сигнал будет внезапно';
    timer = setTimeout(() => {
      timer = null;
      armed = true;
      startAt = Date.now();
      pad.textContent = '⚡ ЖМИ!';
      status.textContent = 'Сигнал!';
      haptic('tap');
    }, 1500 + randInt(3500));
  }

  pad.addEventListener('click', () => {
    if (timer) {
      clearTimeout(timer);
      timer = null;
      pad.textContent = '▶️ Старт';
      status.textContent = '😅 Рано! Попробуйте снова';
      ctx.finish('losses');
      return;
    }
    if (!armed) return;
    armed = false;
    const ms = Date.now() - startAt;
    showOutcome(status, `⚡ ${ms} мс`, ms < 400 ? 'wins' : null);
    pad.textContent = '🔁 Ещё раз';
    ctx.finish(ms < 400 ? 'wins' : 'draws', { score: ms + ' мс' });
  });

  bindKeys({ ' ': () => pad.click(), Enter: () => pad.click() });
  return () => { if (timer) clearTimeout(timer); };
};

/* Казино */
GAMES.slot = function (root, ctx) {
  const SYMBOLS = ['🍒', '🍋', '🍉', '⭐', '💎', '7️⃣'];
  const BET = 10;
  const reels = el('div', 'reels', '🎰 🎰 🎰');
  root.appendChild(reels);
  const status = statusLine(root, `Ставка ${BET} 🪙`);
  let spinning = false;

  root.appendChild(button('🎰 Крутить', 'btn wide', () => {
    if (spinning) return;
    spinning = true;
    reels.classList.add('spin');
    let ticks = 0;
    const anim = setInterval(() => {
      reels.textContent = [0, 1, 2].map(() => pick(SYMBOLS)).join(' ');
      if (++ticks < 12) return;
      clearInterval(anim);
      spinning = false;
      reels.classList.remove('spin');
      const roll = [pick(SYMBOLS), pick(SYMBOLS), pick(SYMBOLS)];
      reels.textContent = roll.join(' ');
      const unique = new Set(roll).size;
      if (unique === 1) {
        showOutcome(status, '🎉 Джекпот!', 'wins');
        ctx.finish('wins', { bet: BET });
      } else if (unique === 2) {
        showOutcome(status, '✨ Почти!', 'draws');
        ctx.finish('draws', { bet: BET });
      } else {
        showOutcome(status, '😢 Мимо', 'losses');
        ctx.finish('losses', { bet: BET });
      }
    }, 70);
  }));
};

/* Wordle */
GAMES.wordle = function (root, ctx) {
  const words = ctx.cfg.wordle_words;
  const target = pick(words);
  const attempts = [];
  let current = '';
  let over = false;

  const status = statusLine(root, 'Угадайте слово из 5 букв');
  const gridEl = makeBoard(root, 5, fitCell(5, 300, 40));
  const keysEl = el('div', 'keys');
  root.appendChild(keysEl);

  function evaluate(guess) {
    const marks = Array(5).fill('miss');
    const rest = {};
    for (let i = 0; i < 5; i++) {
      if (guess[i] === target[i]) marks[i] = 'hit';
      else rest[target[i]] = (rest[target[i]] || 0) + 1;
    }
    for (let i = 0; i < 5; i++) {
      if (marks[i] === 'hit') continue;
      if (rest[guess[i]] > 0) { marks[i] = 'near'; rest[guess[i]] -= 1; }
    }
    return marks;
  }

  const COLORS = { hit: '#4caf7d', near: '#c9a227', miss: '#3a4653' };

  function render() {
    gridEl.innerHTML = '';
    for (let row = 0; row < 6; row++) {
      const attempt = attempts[row];
      const text = attempt ? attempt.guess : (row === attempts.length ? current.padEnd(5) : '     ');
      for (let i = 0; i < 5; i++) {
        const cell = el('div', 'cell', (text[i] || '').toUpperCase());
        cell.style.height = '46px';
        if (attempt) {
          cell.style.background = COLORS[attempt.marks[i]];
          if (row === attempts.length - 1) {
            cell.classList.add('pop');
            cell.style.animationDelay = i * 80 + 'ms';
          }
        }
        gridEl.appendChild(cell);
      }
    }

    const rows = RU_ROWS.concat([[['Стереть', '\b', 'wide'], ['Готово', '\n', 'wide']]]);
    keyboard(keysEl, rows, value => {
      if (over) return;
      if (value === '\n') return submit();
      if (value === '\b') { current = current.slice(0, -1); return render(); }
      if (current.length >= 5) return;
      current += value;
      render();
    }, ch => ch.length === 1 && attempts.some(a => a.guess.includes(ch)));
  }

  function submit() {
    if (over) return;
    if (current.length !== 5) return toast('Нужно 5 букв');
    if (words.indexOf(current) === -1) return toast('Слова нет в словаре');
    attempts.push({ guess: current, marks: evaluate(current) });
    const won = current === target;
    current = '';
    if (won) {
      over = true;
      status.textContent = '🎉 Победа!';
      ctx.finish('wins', { score: attempts.length + '/6' });
    } else if (attempts.length >= 6) {
      over = true;
      status.textContent = '💀 Слово было: ' + target.toUpperCase();
      ctx.finish('losses');
    }
    render();
    if (over) showOutcome(status, status.textContent, won ? 'wins' : 'losses');
  }

  render();
  bindKeys({
    Enter: submit,
    Backspace: () => { if (!over) { current = current.slice(0, -1); render(); } }
  }, ch => {
    if (over || current.length >= 5) return;
    if (!'йцукенгшщзхфывапролджэячсмитьбю'.includes(ch)) return;
    current += ch;
    render();
  });
};

/* Виселица */
GAMES.hangman = function (root, ctx) {
  const entries = Object.keys(ctx.cfg.hangman_words);
  const word = pick(entries);
  const hint = ctx.cfg.hangman_words[word];
  const stages = ctx.cfg.hangman_stages;
  const guessed = new Set();
  const wrong = new Set();
  const maxWrong = 6;
  let over = false;

  const art = el('div', 'mono');
  root.appendChild(art);
  const status = statusLine(root, '');
  const keysEl = el('div', 'keys');
  root.appendChild(keysEl);
  let hintUsed = false;
  const hintBtn = button('💡 Подсказка', 'btn wide secondary', () => {
    hintUsed = true;
    hintBtn.disabled = true;
    toast(hint, 3000);
    render();
  });
  root.appendChild(hintBtn);

  function solved() { return word.split('').every(ch => guessed.has(ch)); }

  function render() {
    art.textContent = stages[Math.min(wrong.size, stages.length - 1)];
    const shown = word.split('').map(ch => (guessed.has(ch) ? ch.toUpperCase() : '_')).join(' ');
    status.textContent = shown + '\nОшибок: ' + wrong.size + '/' + maxWrong + (hintUsed ? '\n💡 ' + hint : '');
    status.style.whiteSpace = 'pre-line';

    const letters = ctx.cfg.hangman_alphabet.split('');
    const size = Math.ceil(letters.length / 3);
    const rows = [letters.slice(0, size), letters.slice(size, size * 2), letters.slice(size * 2)];
    keyboard(keysEl, rows, guess, ch => guessed.has(ch) || wrong.has(ch));
  }

  function guess(ch) {
    if (over || guessed.has(ch) || wrong.has(ch)) return;
    const correct = word.indexOf(ch) >= 0;
    if (correct) guessed.add(ch); else wrong.add(ch);
    if (!correct) replay(art, 'shake');
    if (solved()) {
      over = true;
      render();
      showOutcome(status, '🎉 Слово: ' + word.toUpperCase(), 'wins');
      ctx.finish('wins');
      return;
    }
    if (wrong.size >= maxWrong) {
      over = true;
      render();
      showOutcome(status, '💀 Слово было: ' + word.toUpperCase(), 'losses');
      ctx.finish('losses');
      return;
    }
    render();
  }

  render();
  bindKeys({}, guess);
};

/* Сапёр */
GAMES.minesweeper = function (root, ctx) {
  const SIZE = 6, MINES = 6;
  const board = [];
  const mines = new Set();
  const revealed = new Set();
  const flags = new Set();
  let over = false;

  for (let i = 0; i < SIZE; i++) board.push(new Array(SIZE).fill(0));
  while (mines.size < MINES) mines.add(randInt(SIZE * SIZE));
  mines.forEach(index => {
    const r = Math.floor(index / SIZE), c = index % SIZE;
    board[r][c] = -1;
    for (let dr = -1; dr <= 1; dr++) {
      for (let dc = -1; dc <= 1; dc++) {
        const nr = r + dr, nc = c + dc;
        if (nr >= 0 && nr < SIZE && nc >= 0 && nc < SIZE && board[nr][nc] !== -1) board[nr][nc] += 1;
      }
    }
  });

  const status = statusLine(root, `Мин: ${MINES}. Долгое нажатие — флажок`);
  const cellSize = fitCell(SIZE, 320, 40);
  const gridEl = makeBoard(root, SIZE, cellSize);

  function open(index) {
    if (over || revealed.has(index) || flags.has(index)) return;
    const r = Math.floor(index / SIZE), c = index % SIZE;
    if (board[r][c] === -1) {
      over = true;
      mines.forEach(m => revealed.add(m));
      showOutcome(status, '💥 Мина!', 'losses');
      render();
      replay(gridEl, 'shake');
      ctx.finish('losses');
      return;
    }
    flood(r, c);
    if (revealed.size === SIZE * SIZE - MINES) {
      over = true;
      showOutcome(status, '🎉 Поле разминировано!', 'wins');
      render();
      ctx.finish('wins');
      return;
    }
    render();
  }

  function flood(r, c) {
    const index = r * SIZE + c;
    if (r < 0 || r >= SIZE || c < 0 || c >= SIZE || revealed.has(index)) return;
    revealed.add(index);
    if (board[r][c] !== 0) return;
    for (let dr = -1; dr <= 1; dr++) {
      for (let dc = -1; dc <= 1; dc++) if (dr || dc) flood(r + dr, c + dc);
    }
  }

  function render() {
    gridEl.innerHTML = '';
    for (let index = 0; index < SIZE * SIZE; index++) {
      const r = Math.floor(index / SIZE), c = index % SIZE;
      const cell = el('button', 'cell');
      cell.style.height = cellSize + 'px';
      cell.style.fontSize = Math.round(cellSize * 0.45) + 'px';
      if (revealed.has(index)) {
        cell.style.background = 'rgba(255,255,255,.08)';
        cell.textContent = board[r][c] === -1 ? '💣' : (board[r][c] || '');
        cell.classList.add('pop');
      } else if (flags.has(index)) {
        cell.textContent = '🚩';
      }

      let hold = null;
      cell.addEventListener('pointerdown', () => {
        hold = setTimeout(() => {
          hold = null;
          if (over || revealed.has(index)) return;
          flags.has(index) ? flags.delete(index) : flags.add(index);
          haptic('tap');
          render();
        }, 400);
      });
      const cancel = () => { if (hold) { clearTimeout(hold); hold = null; } };
      cell.addEventListener('pointerup', () => { if (hold) { cancel(); open(index); } });
      cell.addEventListener('pointerleave', cancel);
      cell.addEventListener('pointercancel', cancel);
      gridEl.appendChild(cell);
    }
  }

  render();
};

/* 2048 */
GAMES.g2048 = function (root, ctx) {
  const COLORS = {
    0: '#2b3945', 2: '#5c4b3a', 4: '#6b543c', 8: '#8a5a2b', 16: '#a2622a',
    32: '#b45f28', 64: '#c9a227', 128: '#c9a227', 256: '#2f6f9f',
    512: '#2f6f9f', 1024: '#6f4b9f', 2048: '#4caf7d'
  };
  let board = [[0,0,0,0],[0,0,0,0],[0,0,0,0],[0,0,0,0]];
  let score = 0, over = false, previous = new Array(16).fill(0);

  const status = statusLine(root, 'Очки: 0');
  const cellSize = fitCell(4, 300, 40);
  const gridEl = makeBoard(root, 4, cellSize);

  function spawn() {
    const free = [];
    board.forEach((row, y) => row.forEach((v, x) => { if (!v) free.push([y, x]); }));
    if (!free.length) return;
    const [y, x] = pick(free);
    board[y][x] = Math.random() < 0.9 ? 2 : 4;
  }

  function slide(row) {
    const values = row.filter(Boolean);
    const out = [];
    for (let i = 0; i < values.length; i++) {
      if (values[i] === values[i + 1]) {
        out.push(values[i] * 2);
        score += values[i] * 2;
        i++;
      } else out.push(values[i]);
    }
    while (out.length < 4) out.push(0);
    return out;
  }

  function move(dir) {
    if (over) return;
    const before = JSON.stringify(board);
    if (dir === 'left' || dir === 'right') {
      board = board.map(row => {
        const source = dir === 'right' ? row.slice().reverse() : row;
        const moved = slide(source);
        return dir === 'right' ? moved.reverse() : moved;
      });
    } else {
      for (let x = 0; x < 4; x++) {
        let column = [0, 1, 2, 3].map(y => board[y][x]);
        if (dir === 'down') column.reverse();
        column = slide(column);
        if (dir === 'down') column.reverse();
        for (let y = 0; y < 4; y++) board[y][x] = column[y];
      }
    }
    if (JSON.stringify(board) === before) return;
    spawn();
    render();

    const flat = board.flat();
    if (flat.includes(2048)) {
      over = true;
      showOutcome(status, '🎉 2048! Очки: ' + score, 'wins');
      ctx.finish('wins', { score: String(score) });
      return;
    }
    if (!movesLeft()) {
      over = true;
      showOutcome(status, '💀 Ходов нет. Очки: ' + score, 'losses');
      ctx.finish('losses', { score: String(score) });
    }
  }

  function movesLeft() {
    for (let y = 0; y < 4; y++) {
      for (let x = 0; x < 4; x++) {
        if (!board[y][x]) return true;
        if (x < 3 && board[y][x] === board[y][x + 1]) return true;
        if (y < 3 && board[y][x] === board[y + 1][x]) return true;
      }
    }
    return false;
  }

  function render() {
    status.textContent = 'Очки: ' + score;
    gridEl.innerHTML = '';
    board.flat().forEach((value, i) => {
      const cell = el('div', 'cell', value || '');
      cell.style.height = cellSize + 'px';
      cell.style.fontSize = Math.round(cellSize * (value > 999 ? 0.28 : 0.36)) + 'px';
      cell.style.background = COLORS[value] || '#6f4b9f';
      if (value && value !== previous[i]) cell.classList.add('pop');
      gridEl.appendChild(cell);
    });
    previous = board.flat();
  }

  addSwipe(gridEl, move);
  dpad(root, move);
  spawn(); spawn(); render();
  ctx.started();
};

/* Змейка */
GAMES.snake = function (root, ctx) {
  const W = 12, H = 12;
  let snake = [[6, 6], [5, 6], [4, 6]];
  let dir = 'right', food = null, score = 0, timer = null, over = false;

  const status = statusLine(root, 'Очки: 0');
  const cellSize = fitCell(W, 320, 30);
  const gridEl = makeBoard(root, W, cellSize, 2);

  function placeFood() {
    do { food = [randInt(W), randInt(H)]; }
    while (snake.some(([x, y]) => x === food[0] && y === food[1]));
  }

  function render() {
    gridEl.innerHTML = '';
    for (let y = 0; y < H; y++) {
      for (let x = 0; x < W; x++) {
        const cell = el('div', 'cell');
        cell.style.height = cellSize + 'px';
        const headIndex = snake.findIndex(([sx, sy]) => sx === x && sy === y);
        if (headIndex === 0) cell.style.background = '#4caf7d';
        else if (headIndex > 0) cell.style.background = '#3d8b5f';
        else if (food && food[0] === x && food[1] === y) cell.style.background = '#e5695b';
        else cell.style.background = '#232f3b';
        gridEl.appendChild(cell);
      }
    }
  }

  function step() {
    const deltas = { up: [0, -1], down: [0, 1], left: [-1, 0], right: [1, 0] };
    const [dx, dy] = deltas[dir];
    const head = [snake[0][0] + dx, snake[0][1] + dy];
    const hitWall = head[0] < 0 || head[0] >= W || head[1] < 0 || head[1] >= H;
    const hitSelf = snake.some(([x, y]) => x === head[0] && y === head[1]);
    if (hitWall || hitSelf) return stop();

    snake.unshift(head);
    if (food && head[0] === food[0] && head[1] === food[1]) {
      score += 1;
      status.textContent = 'Очки: ' + score;
      placeFood();
    } else snake.pop();
    render();
  }

  function stop() {
    if (over) return;
    over = true;
    clearInterval(timer);
    timer = null;
    const result = score >= 5 ? 'wins' : 'losses';
    showOutcome(status, '💥 Игра окончена. Очки: ' + score, result);
    ctx.finish(result, { score: String(score) });
  }

  function turn(next) {
    const opposite = { up: 'down', down: 'up', left: 'right', right: 'left' };
    if (over || opposite[next] === dir) return;
    dir = next;
  }

  addSwipe(gridEl, turn);
  dpad(root, turn);
  placeFood();
  render();
  timer = setInterval(step, 260);
  ctx.started();
  return () => clearInterval(timer);
};

/* Тетрис */
GAMES.tetris = function (root, ctx) {
  const W = 10, H = 16;
  const SHAPES = [
    [[1, 1, 1, 1]], [[1, 1], [1, 1]], [[1, 1, 1], [0, 1, 0]],
    [[1, 1, 1], [1, 0, 0]], [[1, 1, 1], [0, 0, 1]],
    [[1, 1, 0], [0, 1, 1]], [[0, 1, 1], [1, 1, 0]]
  ];
  const COLORS = ['#e5695b', '#d98b3a', '#c9a227', '#4caf7d', '#3d8bcd', '#8b5cc9', '#a0725a'];
  let board = Array.from({ length: H }, () => new Array(W).fill(0));
  let piece = null, score = 0, timer = null, over = false;

  const status = statusLine(root, 'Очки: 0');
  const cellSize = fitCell(W, 300, 30);
  const gridEl = makeBoard(root, W, cellSize, 2);

  function spawn() {
    const shapeIndex = randInt(SHAPES.length);
    piece = { shape: SHAPES[shapeIndex], color: shapeIndex + 1, x: Math.floor((W - SHAPES[shapeIndex][0].length) / 2), y: 0 };
    if (!fits(piece.shape, piece.x, piece.y)) stop();
  }

  function fits(shape, px, py) {
    return shape.every((row, dy) => row.every((v, dx) => {
      if (!v) return true;
      const x = px + dx, y = py + dy;
      return x >= 0 && x < W && y >= 0 && y < H && !board[y][x];
    }));
  }

  function lock() {
    piece.shape.forEach((row, dy) => row.forEach((v, dx) => {
      if (v) board[piece.y + dy][piece.x + dx] = piece.color;
    }));
    const kept = board.filter(row => !row.every(Boolean));
    const cleared = H - kept.length;
    if (cleared) {
      score += cleared * 100;
      while (kept.length < H) kept.unshift(new Array(W).fill(0));
      board = kept;
      status.textContent = 'Очки: ' + score;
    }
    spawn();
  }

  function tick() {
    if (over || !piece) return;
    if (fits(piece.shape, piece.x, piece.y + 1)) piece.y += 1;
    else lock();
    render();
  }

  function rotate() {
    if (over || !piece) return;
    const rows = piece.shape.length, cols = piece.shape[0].length;
    const rotated = Array.from({ length: cols }, (_, y) =>
      Array.from({ length: rows }, (_, x) => piece.shape[rows - 1 - x][y]));
    if (fits(rotated, piece.x, piece.y)) piece.shape = rotated;
    render();
  }

  function move(dir) {
    if (over || !piece) return;
    if (dir === 'up') return rotate();
    if (dir === 'down') {
      while (fits(piece.shape, piece.x, piece.y + 1)) piece.y += 1;
      lock();
      return render();
    }
    const dx = dir === 'left' ? -1 : 1;
    if (fits(piece.shape, piece.x + dx, piece.y)) piece.x += dx;
    render();
  }

  function stop() {
    if (over) return;
    over = true;
    clearInterval(timer);
    timer = null;
    piece = null;
    const result = score >= 300 ? 'wins' : 'losses';
    showOutcome(status, '💀 Игра окончена. Очки: ' + score, result);
    ctx.finish(result, { score: String(score) });
  }

  function render() {
    const view = board.map(row => row.slice());
    if (piece) {
      piece.shape.forEach((row, dy) => row.forEach((v, dx) => {
        const y = piece.y + dy, x = piece.x + dx;
        if (v && y >= 0 && y < H && x >= 0 && x < W) view[y][x] = piece.color;
      }));
    }
    gridEl.innerHTML = '';
    view.flat().forEach(value => {
      const cell = el('div', 'cell');
      cell.style.height = cellSize + 'px';
      cell.style.background = value ? COLORS[value - 1] : '#232f3b';
      gridEl.appendChild(cell);
    });
  }

  addSwipe(gridEl, move);
  dpad(root, move, '⤓', () => move('down'));
  bindArrows(move, { ' ': () => move('down'), Enter: () => move('down') });
  spawn();
  render();
  timer = setInterval(tick, 620);
  ctx.started();
  return () => clearInterval(timer);
};

/* Flappy Bird */
GAMES.flappy = function (root, ctx) {
  const width = Math.min(screenEl.clientWidth - 28, 360);
  const height = 420;
  const canvas = el('canvas');
  canvas.width = width;
  canvas.height = height;
  root.appendChild(canvas);
  const g = canvas.getContext('2d');

  const status = statusLine(root, 'Нажмите по полю, чтобы взлететь');
  let bird = height / 2, velocity = 0, pipes = [], score = 0, running = false, over = false, raf = null;

  function reset() {
    bird = height / 2; velocity = 0; score = 0; pipes = [];
    for (let i = 0; i < 3; i++) pipes.push({ x: width + i * 160, gap: 90 + randInt(160) });
    over = false; running = true;
    status.textContent = 'Очки: 0';
    loop();
  }

  function flap() {
    if (over) return reset();
    if (!running) return reset();
    velocity = -5.6;
    haptic('tap');
  }

  function stop() {
    over = true; running = false;
    if (raf) cancelAnimationFrame(raf);
    raf = null;
    const result = score >= 5 ? 'wins' : 'losses';
    showOutcome(status, '💥 Очки: ' + score + '. Нажмите, чтобы начать заново', result);
    ctx.finish(result, { score: String(score) });
  }

  function loop() {
    velocity += 0.32;
    bird += velocity;

    pipes.forEach(p => { p.x -= 2.2; });
    if (pipes[0].x < -60) { pipes.shift(); pipes.push({ x: pipes[pipes.length - 1].x + 160, gap: 90 + randInt(160) }); }

    g.fillStyle = '#0f1720';
    g.fillRect(0, 0, width, height);
    g.fillStyle = '#4caf7d';
    pipes.forEach(p => {
      g.fillRect(p.x, 0, 46, p.gap - 60);
      g.fillRect(p.x, p.gap + 60, 46, height);
      if (!p.passed && p.x + 46 < 60) { p.passed = true; score += 1; status.textContent = 'Очки: ' + score; }
    });
    g.fillStyle = '#ffd166';
    g.beginPath();
    g.arc(60, bird, 11, 0, Math.PI * 2);
    g.fill();

    const crashed = bird < 10 || bird > height - 10 || pipes.some(p =>
      p.x < 71 && p.x + 46 > 49 && (bird < p.gap - 60 || bird > p.gap + 60));
    if (crashed) return stop();
    raf = requestAnimationFrame(loop);
  }

  canvas.addEventListener('pointerdown', flap);
  bindKeys({ ' ': flap, Enter: flap, ArrowUp: flap, w: flap, ц: flap });
  root.appendChild(button('⬆️ Взлёт', 'btn wide', flap));
  reset();
  ctx.started();
  return () => { if (raf) cancelAnimationFrame(raf); };
};

/* Пинг-понг против бота */
GAMES.pong = function (root, ctx) {
  const width = Math.min(screenEl.clientWidth - 28, 360);
  const height = 380;
  const canvas = el('canvas');
  canvas.width = width;
  canvas.height = height;
  root.appendChild(canvas);
  const g = canvas.getContext('2d');
  const status = statusLine(root, 'До 5 очков');

  const PADDLE = 62, THICK = 9;
  let playerX = width / 2 - PADDLE / 2;
  let botX = playerX;
  let ball = { x: width / 2, y: height / 2, dx: 2.6, dy: -3.2 };
  let score = [0, 0], raf = null, over = false;

  function move(clientX) {
    const rect = canvas.getBoundingClientRect();
    playerX = Math.max(0, Math.min(width - PADDLE, clientX - rect.left - PADDLE / 2));
  }

  canvas.addEventListener('pointermove', e => { e.preventDefault(); move(e.clientX); });
  canvas.addEventListener('pointerdown', e => move(e.clientX));

  function reset(direction) {
    ball = { x: width / 2, y: height / 2, dx: (Math.random() < 0.5 ? -1 : 1) * 2.6, dy: direction * 3.2 };
  }

  function stop(playerWon) {
    over = true;
    if (raf) cancelAnimationFrame(raf);
    raf = null;
    showOutcome(status, (playerWon ? '🎉 Вы победили ' : '💀 Бот победил ') + score[0] + ':' + score[1], playerWon ? 'wins' : 'losses');
    ctx.finish(playerWon ? 'wins' : 'losses', { score: score[0] + ':' + score[1], opponent: 'бот' });
  }

  function loop() {
    botX += Math.max(-3.1, Math.min(3.1, (ball.x - PADDLE / 2) - botX));
    botX = Math.max(0, Math.min(width - PADDLE, botX));

    ball.x += ball.dx;
    ball.y += ball.dy;
    if (ball.x < 6 || ball.x > width - 6) ball.dx *= -1;

    if (ball.y > height - 24 && ball.x > playerX && ball.x < playerX + PADDLE && ball.dy > 0) ball.dy *= -1;
    if (ball.y < 24 && ball.x > botX && ball.x < botX + PADDLE && ball.dy < 0) ball.dy *= -1;

    if (ball.y > height) { score[1] += 1; reset(-1); }
    if (ball.y < 0) { score[0] += 1; reset(1); }
    status.textContent = 'Вы ' + score[0] + ' : ' + score[1] + ' Бот';

    g.fillStyle = '#0f1720';
    g.fillRect(0, 0, width, height);
    g.fillStyle = '#5aa9e6';
    g.fillRect(playerX, height - 20, PADDLE, THICK);
    g.fillStyle = '#e5695b';
    g.fillRect(botX, 14, PADDLE, THICK);
    g.fillStyle = '#f5f7fa';
    g.beginPath();
    g.arc(ball.x, ball.y, 6, 0, Math.PI * 2);
    g.fill();

    if (score[0] >= 5) return stop(true);
    if (score[1] >= 5) return stop(false);
    if (!over) raf = requestAnimationFrame(loop);
  }

  bindKeys({
    ArrowLeft: () => { playerX = Math.max(0, playerX - 26); },
    ArrowRight: () => { playerX = Math.min(width - PADDLE, playerX + 26); },
    a: () => { playerX = Math.max(0, playerX - 26); },
    d: () => { playerX = Math.min(width - PADDLE, playerX + 26); }
  });
  hintLine(root, currentLayout() === 'desktop'
    ? 'Двигайте ракетку стрелками ← → или мышью.'
    : 'Ведите пальцем по полю, чтобы двигать ракетку.');
  loop();
  ctx.started();
  return () => { if (raf) cancelAnimationFrame(raf); };
};

/* Викторина */
GAMES.quizgame = function (root, ctx) {
  const question = pick(ctx.cfg.quiz_questions);
  let answer = '';
  let over = false;

  const status = statusLine(root, question.q);
  const input = el('div', 'cards', '—');
  root.appendChild(input);
  const keysEl = el('div', 'keys');
  root.appendChild(keysEl);

  function render() {
    input.textContent = answer || '—';
    const rows = RU_FULL_ROWS.concat([
      '0123456789'.split(''),
      [['Пробел', ' ', 'wide'], ['Стереть', '\b', 'wide']]
    ]);
    keyboard(keysEl, rows, value => {
      if (over) return;
      if (value === '\b') { answer = answer.slice(0, -1); return render(); }
      if (answer.length >= 32) return;
      answer += value;
      render();
    });
  }

  root.appendChild(button('✅ Ответить', 'btn wide', () => {
    if (over) return;
    if (!answer.trim()) return toast('Введите ответ');
    over = true;
    const correct = answer.trim().toLowerCase() === String(question.a).toLowerCase();
    showOutcome(status, correct ? '🎉 Верно!' : '😢 Правильный ответ: ' + question.a, correct ? 'wins' : 'losses');
    ctx.finish(correct ? 'wins' : 'losses');
  }));

  render();
};

/* Комбо-битва */
GAMES.combogame = function (root, ctx) {
  const CHOICES = ctx.cfg.combo_choices;
  const BEATS = ctx.cfg.combo_beats;
  let round = 1, mine = 0, theirs = 0, over = false;

  const status = statusLine(root, 'Раунд 1 из 3');
  const log = el('div', 'hint', 'Счёт 0 : 0');
  root.appendChild(log);
  const row = el('div', 'row');
  root.appendChild(row);

  Object.keys(CHOICES).forEach(key => {
    row.appendChild(button(CHOICES[key], 'btn', () => play(key)));
  });

  function play(key) {
    if (over) return;
    const botKey = pick(Object.keys(CHOICES));
    let line;
    if (key === botKey) line = '🤝 Ничья в раунде';
    else if (BEATS[key] === botKey) { mine += 1; line = '🎉 Раунд ваш'; }
    else { theirs += 1; line = '😢 Раунд за ботом'; }

    status.textContent = `${CHOICES[key]} против ${CHOICES[botKey]}\n${line}`;
    status.style.whiteSpace = 'pre-line';
    log.textContent = `Счёт ${mine} : ${theirs}`;

    if (round >= 3) {
      over = true;
      const result = mine > theirs ? 'wins' : mine < theirs ? 'losses' : 'draws';
      showOutcome(status, status.textContent + '\n' + (result === 'wins' ? '🏆 Вы выиграли битву!' : result === 'losses' ? '💀 Бот выиграл битву' : '🤝 Полная ничья'), result);
      ctx.finish(result, { rounds: '3', score: mine + ':' + theirs, opponent: 'бот' });
      return;
    }
    round += 1;
    log.textContent += ' · раунд ' + round + ' из 3';
  }
};

/* ---------- карты ---------- */

function cardLabel(card) { return card[0] + card[1]; }
function isRed(card) { return card[1] === '♥' || card[1] === '♦'; }

function renderCards(node, cards, hiddenFrom) {
  node.innerHTML = '';
  cards.forEach((card, index) => {
    const hidden = hiddenFrom !== undefined && index >= hiddenFrom;
    const cls = 'card-in' + (!hidden && isRed(card) ? ' red' : '');
    const span = el('span', cls, hidden ? '🂠 ' : cardLabel(card) + ' ');
    span.style.animationDelay = Math.min(index * 70, 350) + 'ms';
    node.appendChild(span);
  });
}

/* Блэкджек */
GAMES.blackjack = function (root, ctx) {
  const SUITS = ['♠', '♥', '♦', '♣'];
  const RANKS = ['A', '2', '3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K'];
  const BET = 10;
  let deck, player, dealer, over;

  const dealerEl = el('div', 'cards');
  const playerEl = el('div', 'cards');
  root.appendChild(el('div', 'hint', 'Дилер'));
  root.appendChild(dealerEl);
  root.appendChild(el('div', 'hint', 'Вы'));
  root.appendChild(playerEl);
  const status = statusLine(root, `Ставка ${BET} 🪙`);
  const actions = el('div', 'row');
  root.appendChild(actions);

  function value(hand) {
    let total = 0, aces = 0;
    hand.forEach(([rank]) => {
      if (rank === 'A') { total += 11; aces += 1; }
      else if (['J', 'Q', 'K'].includes(rank)) total += 10;
      else total += parseInt(rank, 10);
    });
    while (total > 21 && aces > 0) { total -= 10; aces -= 1; }
    return total;
  }

  function render(revealDealer) {
    renderCards(dealerEl, dealer, revealDealer ? undefined : 1);
    renderCards(playerEl, player);
    if (revealDealer) dealerEl.appendChild(el('span', null, ' (' + value(dealer) + ')'));
    playerEl.appendChild(el('span', null, ' (' + value(player) + ')'));
  }

  function finish(result) {
    over = true;
    render(true);
    showOutcome(status, result === 'wins' ? '🎉 Вы выиграли' : result === 'losses' ? '💀 Дилер выиграл' : '🤝 Ничья', result);
    ctx.finish(result, { bet: BET, score: value(player) + ':' + value(dealer) });
    buildActions();
  }

  function buildActions() {
    actions.innerHTML = '';
    if (over) {
      actions.appendChild(button('🔁 Новая партия', 'btn', deal));
      return;
    }
    actions.appendChild(button('➕ Ещё', 'btn', () => {
      player.push(deck.pop());
      if (value(player) > 21) return finish('losses');
      render(false);
    }));
    actions.appendChild(button('🛑 Хватит', 'btn secondary', () => {
      while (value(dealer) < 17 && deck.length) dealer.push(deck.pop());
      const p = value(player), d = value(dealer);
      finish(d > 21 || p > d ? 'wins' : p < d ? 'losses' : 'draws');
    }));
  }

  function deal() {
    deck = [];
    SUITS.forEach(s => RANKS.forEach(r => deck.push([r, s])));
    deck.sort(() => Math.random() - 0.5);
    player = [deck.pop(), deck.pop()];
    dealer = [deck.pop(), deck.pop()];
    over = false;
    status.textContent = `Ставка ${BET} 🪙`;
    if (value(player) === 21) return finish(value(dealer) === 21 ? 'draws' : 'wins');
    render(false);
    buildActions();
  }

  deal();
};

/* Покер (Техасский холдем против бота) */
GAMES.poker = function (root, ctx) {
  const RANKS = ctx.cfg.poker_ranks;
  const SUITS = ctx.cfg.poker_suits;
  const NAMES = ctx.cfg.poker_hand_names;
  const STAGES = ['preflop', 'flop', 'turn', 'river', 'showdown'];
  const VISIBLE = { preflop: 0, flop: 3, turn: 4, river: 5, showdown: 5 };
  const STAGE_LABEL = { preflop: 'Префлоп', flop: 'Флоп', turn: 'Тёрн', river: 'Ривер', showdown: 'Вскрытие' };
  let bet = 10;
  let deck, player, botHand, community, stage, over;

  const info = el('div', 'hint', '');
  root.appendChild(info);
  root.appendChild(el('div', 'hint', 'Общие карты'));
  const communityEl = el('div', 'cards');
  root.appendChild(communityEl);
  root.appendChild(el('div', 'hint', 'Бот'));
  const botEl = el('div', 'cards');
  root.appendChild(botEl);
  root.appendChild(el('div', 'hint', 'Вы'));
  const playerEl = el('div', 'cards');
  root.appendChild(playerEl);
  const status = statusLine(root, '');
  const actions = el('div', 'row');
  root.appendChild(actions);
  const betRow = el('div', 'row');
  root.appendChild(betRow);

  function handRank(cards) {
    const values = cards.map(c => RANKS.indexOf(c[0])).sort((a, b) => b - a);
    const flush = new Set(cards.map(c => c[1])).size === 1;
    const unique = new Set(values);
    let straight = unique.size === 5 && values[0] - values[4] === 4;
    let ordered = values.slice();
    if (unique.size === 5 && values[0] === 12 && values[1] === 3) { straight = true; ordered = [3, 2, 1, 0, -1]; }

    const counts = [...unique].map(v => [values.filter(x => x === v).length, v]).sort((a, b) => b[0] - a[0] || b[1] - a[1]);
    const shape = counts.map(c => c[0]);
    const byCount = counts.map(c => c[1]);

    if (straight && flush) return [8, ordered];
    if (shape[0] === 4) return [7, byCount];
    if (shape[0] === 3 && shape[1] === 2) return [6, byCount];
    if (flush) return [5, ordered];
    if (straight) return [4, ordered];
    if (shape[0] === 3) return [3, byCount];
    if (shape[0] === 2 && shape[1] === 2) return [2, byCount];
    if (shape[0] === 2) return [1, byCount];
    return [0, ordered];
  }

  function compare(a, b) {
    if (a[0] !== b[0]) return a[0] - b[0];
    for (let i = 0; i < Math.max(a[1].length, b[1].length); i++) {
      const diff = (a[1][i] || 0) - (b[1][i] || 0);
      if (diff) return diff;
    }
    return 0;
  }

  function best(cards) {
    let top = null;
    const n = cards.length;
    for (let a = 0; a < n - 4; a++)
      for (let b = a + 1; b < n - 3; b++)
        for (let c = b + 1; c < n - 2; c++)
          for (let d = c + 1; d < n - 1; d++)
            for (let e = d + 1; e < n; e++) {
              const rank = handRank([cards[a], cards[b], cards[c], cards[d], cards[e]]);
              if (!top || compare(rank, top) > 0) top = rank;
            }
    return top;
  }

  function render() {
    info.textContent = `Ставка ${bet} 🪙 · Стадия: ${STAGE_LABEL[stage]}`;
    renderCards(communityEl, community.slice(0, VISIBLE[stage]));
    if (!community.slice(0, VISIBLE[stage]).length) communityEl.textContent = '—';
    renderCards(botEl, botHand, over ? undefined : 0);
    renderCards(playerEl, player);
    buildActions();
  }

  function buildActions() {
    actions.innerHTML = '';
    betRow.innerHTML = '';
    if (over) {
      [10, 50, 100].forEach(v => betRow.appendChild(button(v + ' 🪙', 'btn secondary', () => { bet = v; deal(); })));
      return;
    }
    if (stage !== 'showdown') {
      actions.appendChild(button('➡️ Дальше', 'btn', () => {
        stage = STAGES[STAGES.indexOf(stage) + 1];
        render();
      }));
      actions.appendChild(button('🏳️ Сдаться', 'btn secondary', () => finish('losses')));
    } else {
      actions.appendChild(button('🔍 Вскрыть карты', 'btn', () => {
        const mine = best(player.concat(community));
        const theirs = best(botHand.concat(community));
        const cmp = compare(mine, theirs);
        finish(cmp > 0 ? 'wins' : cmp < 0 ? 'losses' : 'draws', mine, theirs);
      }));
    }
  }

  function finish(result, mine, theirs) {
    over = true;
    stage = 'showdown';
    const text = result === 'wins' ? '🏆 Вы победили!' : result === 'losses' ? '💀 Бот победил' : '🤝 Ничья';
    status.style.whiteSpace = 'pre-line';
    showOutcome(status, mine ? `${text}\nУ вас: ${NAMES[mine[0]]}\nУ бота: ${NAMES[theirs[0]]}` : text, result);
    ctx.finish(result, { bet });
    render();
  }

  function deal() {
    deck = [];
    SUITS.forEach(s => RANKS.forEach(r => deck.push([r, s])));
    deck.sort(() => Math.random() - 0.5);
    player = [deck.pop(), deck.pop()];
    botHand = [deck.pop(), deck.pop()];
    community = [deck.pop(), deck.pop(), deck.pop(), deck.pop(), deck.pop()];
    stage = 'preflop';
    over = false;
    status.textContent = '';
    render();
  }

  deal();
};

/* ---------- запуск ---------- */

/** Подставляет цвета Telegram и достраивает недостающие оттенки под схему. */
function applyTheme() {
  const params = (tg && tg.themeParams) || {};
  const root = document.documentElement;
  const light = (tg && tg.colorScheme) === 'light';
  root.setAttribute('data-forced-dark', light ? 'no' : 'yes');

  const map = {
    '--bg': params.bg_color,
    '--card': params.secondary_bg_color,
    '--text': params.text_color,
    '--muted': params.hint_color,
    '--accent': params.button_color
  };
  Object.entries(map).forEach(([name, value]) => {
    if (value) root.style.setProperty(name, value);
  });

  if (params.secondary_bg_color) {
    // «Приподнятый» слой и разделители выводим из основной подложки
    root.style.setProperty('--card-2', mix(params.secondary_bg_color, light ? '#000000' : '#ffffff', light ? 0.05 : 0.06));
    root.style.setProperty('--line', light ? 'rgba(16,32,48,.1)' : 'rgba(255,255,255,.08)');
  }
  if (params.button_color) {
    root.style.setProperty('--accent-2', mix(params.button_color, light ? '#7b5cf0' : '#7b5cf0', 0.45));
  }

  try {
    if (tg.setHeaderColor && params.bg_color) tg.setHeaderColor(params.bg_color);
    if (tg.setBackgroundColor && params.bg_color) tg.setBackgroundColor(params.bg_color);
  } catch (e) { /* старые клиенты не умеют менять цвета */ }
}

function mix(hex, target, amount) {
  const parse = value => {
    const clean = String(value).replace('#', '');
    const full = clean.length === 3 ? clean.split('').map(c => c + c).join('') : clean;
    return [0, 2, 4].map(i => parseInt(full.slice(i, i + 2), 16));
  };
  try {
    const a = parse(hex), b = parse(target);
    const out = a.map((v, i) => Math.round(v + (b[i] - v) * amount));
    return '#' + out.map(v => v.toString(16).padStart(2, '0')).join('');
  } catch (e) {
    return hex;
  }
}

async function boot() {
  try {
    setLayout(storedLayout() || detectDevice(), false);
    applyViewport();
    window.addEventListener('resize', applyViewport);

    if (tg) {
      tg.ready();
      tg.expand();
      if (tg.BackButton) tg.BackButton.onClick(goHome);
      applyTheme();
      if (tg.onEvent) {
        tg.onEvent('themeChanged', applyTheme);
        tg.onEvent('viewportChanged', applyViewport);
        tg.onEvent('safeAreaChanged', applyViewport);
      }
      // Свайпы в играх не должны закрывать приложение
      try { if (tg.disableVerticalSwipes) tg.disableVerticalSwipes(); } catch (e) { /* старый клиент */ }
    }

    CFG = await (await fetch('/api/config')).json();
    try {
      setCoins(await api('/api/profile', {}));
    } catch (e) {
      coinsEl.textContent = '🪙 —';
      toast('Открой приложение через бота, чтобы сохранялся прогресс', 3000);
    }

    goHome();

    const requested = new URLSearchParams(location.search).get('game');
    if (requested && GAMES[requested]) {
      const meta = CFG.games.find(g => g[0] === requested);
      openGame(requested, meta ? meta[1] : requested);
    }
  } catch (e) {
    screenEl.innerHTML = '';
    screenEl.appendChild(el('div', 'panel', 'Не удалось загрузить приложение: ' + e.message));
  }
}

boot();
