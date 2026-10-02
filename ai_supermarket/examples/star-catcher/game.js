/* 来源：代码及默认图形为本项目原创；素材版使用本地Kenney CC0 PNG，来源见credits.html。 */
(() => {
  'use strict';
  const {Game, WIDTH, HEIGHT, PLAYER_Y} = window.StarCatcher;
  const game = new Game(), canvas = document.getElementById('sky'), ctx = canvas.getContext('2d');
  const $ = id => document.getElementById(id);
  const keys = new Set(), held = new Map();
  const storageKey = 'star-catcher-best-v1';
  let best = 0, target = null, dragging = false, previous = 0, effects = [], visualTime = 0;
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const art = {}, useMarketArt = new URLSearchParams(location.search).get('art') === 'market';
  if (useMarketArt) {
    $('art-note').textContent = '正在载入星际素材…';
    let settled = 0, loaded = 0;
    // Paths map to the verified delivery manifest. They are fixed local files,
    // never seller-provided URLs or metadata interpreted as instructions.
    for (const [name, path] of [['ship', 'resource-01.png'], ['meteor', 'resource-02.png'], ['star', 'resource-03.png']]) {
      const picture = new Image();
      picture.onload = () => { art[name] = picture; loaded++; done(); };
      picture.onerror = done;
      picture.src = `market-assets/${path}`;
    }
    function done() {
      settled++;
      if (settled === 3) {
        $('art-note').textContent = loaded === 3 ? '素材：Kenney · CC0 · 可离线游玩' : '部分素材不可用，已用原创图形补齐';
        $('art-note').dataset.loadedAssets = String(loaded);
      }
    }
  }
  try { const value = Number(localStorage.getItem(storageKey)); if (Number.isFinite(value) && value >= 0) best = Math.floor(value); }
  catch { $('save-note').textContent = '当前浏览器不能保存记录'; }
  $('best').textContent = String(best).padStart(3, '0');
  const stars = Array.from({length: 64}, (_, i) => ({x: (i * 131.7 + 17) % WIDTH, y: (i * 87.1 + 31) % HEIGHT, r: i % 3 === 0 ? 1.5 : 0.8}));
  function clearInput() { keys.clear(); held.clear(); target = null; dragging = false; }
  function updateHUD() {
    $('score').textContent = String(game.score).padStart(3, '0');
    $('time').textContent = Math.ceil(game.remaining);
    $('lives').textContent = Array.from({length: 3}, (_, i) => i < game.lives ? '●' : '○').join(' ');
    $('lives').setAttribute('aria-label', `剩余${game.lives}次护盾`);
    $('combo').hidden = game.chain < 2;
    $('combo').textContent = `${game.chain} 连击 · ×${game.multiplier}`;
  }
  function overlay(label, title, text, button, hint) {
    $('overlay-label').textContent = label; $('overlay-title').textContent = title;
    $('overlay-text').textContent = text; $('start').textContent = button;
    $('shortcut').textContent = hint; $('overlay').hidden = false;
  }
  function start() {
    clearInput();
    if (game.mode === 'paused') game.resume();
    else { game.reset(); effects = []; }
    previous = performance.now(); $('overlay').hidden = true; $('pause').disabled = false;
    $('pause').innerHTML = '暂停 <kbd>P</kbd>';
    canvas.scrollIntoView({block: 'center', behavior: 'instant'}); canvas.focus({preventScroll: true});
    $('announcement').textContent = '航行开始。左右移动接星星，P键暂停。'; updateHUD();
  }
  function pause() {
    if (game.mode !== 'playing') return;
    game.pause(); clearInput();
    overlay('TAKE A BREATH', '星光会等你。', '航行已暂停，分数和剩余时间会为你保留。', '继续航行 ↗', '按 P 或空格键继续');
    $('pause').innerHTML = '继续 <kbd>P</kbd>'; $('announcement').textContent = '游戏已暂停';
  }
  function finish() {
    clearInput(); const isRecord = game.score > best;
    if (isRecord) {
      best = game.score; $('best').textContent = String(best).padStart(3, '0');
      try { localStorage.setItem(storageKey, String(best)); }
      catch { $('save-note').textContent = '记录仅在本次页面保留'; }
    }
    overlay(isRecord ? 'A NEW PERSONAL BEST' : 'JOURNEY COMPLETE', isRecord ? '你的新纪录！' : '带着星光返航。',
      `本次 ${game.score} 分 · 接住 ${game.caught} 颗星星。${game.lives ? '一分钟的银河旅程完成。' : '护盾耗尽，补充能量再出发。'}`,
      '再来一局 ↗', '按空格键再次出发');
    $('pause').disabled = true; $('start').focus({preventScroll: true});
    $('announcement').textContent = `航行结束，得分${game.score}，收集${game.caught}颗星星。`;
  }
  function toggle() { if (game.mode === 'playing') pause(); else if (game.mode === 'paused') start(); }
  $('start').addEventListener('click', start); $('pause').addEventListener('click', toggle);
  document.addEventListener('keydown', e => {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const key = e.key.toLowerCase();
    if (['arrowleft', 'arrowright', 'a', 'd'].includes(key)) {
      if (game.mode === 'playing') { e.preventDefault(); keys.add(key); target = null; }
    } else if ((key === 'p' || key === 'escape') && !e.repeat) { e.preventDefault(); toggle(); }
    else if (e.code === 'Space' && e.target.tagName !== 'BUTTON' && !e.repeat) {
      e.preventDefault(); if (game.mode === 'playing') pause(); else start();
    }
  });
  document.addEventListener('keyup', e => keys.delete(e.key.toLowerCase()));
  window.addEventListener('blur', () => { clearInput(); pause(); });
  document.addEventListener('visibilitychange', () => { if (document.hidden) pause(); });
  function point(e) { const rect = canvas.getBoundingClientRect(); target = (e.clientX - rect.left) * WIDTH / rect.width; }
  canvas.addEventListener('pointerdown', e => {
    if (game.mode !== 'playing') return;
    e.preventDefault(); dragging = true; canvas.setPointerCapture(e.pointerId); point(e);
  });
  canvas.addEventListener('pointermove', e => { if (dragging) point(e); });
  for (const name of ['pointerup', 'pointercancel', 'lostpointercapture']) canvas.addEventListener(name, () => { dragging = false; target = null; });
  for (const [id, direction] of [['left', -1], ['right', 1]]) {
    const button = $(id);
    button.addEventListener('pointerdown', e => {
      if (game.mode !== 'playing') return;
      e.preventDefault(); target = null; held.set(e.pointerId, direction); button.setPointerCapture(e.pointerId);
    });
    for (const name of ['pointerup', 'pointercancel', 'lostpointercapture']) button.addEventListener(name, e => held.delete(e.pointerId));
  }
  function star(x, y, radius, angle, color) {
    if (art.star) {
      ctx.save(); ctx.translate(x, y); ctx.rotate(angle);
      ctx.drawImage(art.star, -radius, -radius, radius * 2, radius * 2); ctx.restore(); return;
    }
    ctx.save(); ctx.translate(x, y); ctx.rotate(angle); ctx.beginPath();
    for (let i = 0; i < 10; i++) { const r = i % 2 ? radius * 0.44 : radius, a = i * Math.PI / 5 - Math.PI / 2; ctx.lineTo(Math.cos(a) * r, Math.sin(a) * r); }
    ctx.closePath(); ctx.fillStyle = color; ctx.fill(); ctx.restore();
  }
  function ship(x, y) {
    ctx.save(); ctx.translate(x, y);
    if (game.invulnerable > 0) { ctx.strokeStyle = '#b0f0c8'; ctx.globalAlpha = 0.55 + Math.sin(visualTime * 24) * 0.15; ctx.beginPath(); ctx.arc(0, 0, 32, 0, Math.PI * 2); ctx.stroke(); }
    ctx.fillStyle = '#f7c989'; ctx.beginPath(); ctx.moveTo(-6, 14); ctx.lineTo(0, 30 + (reducedMotion ? 0 : Math.sin(visualTime * 20) * 5)); ctx.lineTo(6, 14); ctx.fill();
    if (art.ship) { ctx.globalAlpha = 1; ctx.drawImage(art.ship, -29, -22, 58, 44); ctx.restore(); return; }
    ctx.globalAlpha = 1; ctx.fillStyle = '#74a6a6'; ctx.beginPath(); ctx.moveTo(0, -8); ctx.lineTo(-26, 18); ctx.lineTo(-14, 21); ctx.lineTo(0, 11); ctx.lineTo(14, 21); ctx.lineTo(26, 18); ctx.closePath(); ctx.fill();
    ctx.fillStyle = '#c9f7dc'; ctx.beginPath(); ctx.moveTo(0, -26); ctx.quadraticCurveTo(16, -8, 12, 17); ctx.lineTo(-12, 17); ctx.quadraticCurveTo(-16, -8, 0, -26); ctx.fill();
    ctx.fillStyle = '#24465b'; ctx.beginPath(); ctx.ellipse(0, -5, 6, 9, 0, 0, Math.PI * 2); ctx.fill(); ctx.restore();
  }
  function draw(dt) {
    const rect = canvas.getBoundingClientRect(), dpr = Math.min(window.devicePixelRatio || 1, 2);
    const w = Math.max(1, Math.round(rect.width * dpr)), h = Math.max(1, Math.round(rect.height * dpr));
    if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
    ctx.setTransform(w / WIDTH, 0, 0, h / HEIGHT, 0, 0); ctx.clearRect(0, 0, WIDTH, HEIGHT);
    const bg = ctx.createLinearGradient(0, 0, WIDTH, HEIGHT); bg.addColorStop(0, '#10243a'); bg.addColorStop(0.6, '#0c1829'); bg.addColorStop(1, '#173039'); ctx.fillStyle = bg; ctx.fillRect(0, 0, WIDTH, HEIGHT);
    ctx.strokeStyle = '#b0f0c80b'; ctx.lineWidth = 1;
    for (let x = 0; x < WIDTH; x += 64) { ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, HEIGHT); ctx.stroke(); }
    for (let y = 0; y < HEIGHT; y += 64) { ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(WIDTH, y); ctx.stroke(); }
    for (const s of stars) { ctx.globalAlpha = 0.3 + (s.x % 5) / 10; ctx.fillStyle = '#d4e6ec'; ctx.beginPath(); ctx.arc(s.x, (s.y + (reducedMotion ? 0 : visualTime * 5)) % HEIGHT, s.r, 0, Math.PI * 2); ctx.fill(); }
    ctx.globalAlpha = 1; ctx.fillStyle = '#22404b'; ctx.beginPath(); ctx.arc(548, 134, 48, 0, Math.PI * 2); ctx.fill();
    ctx.strokeStyle = '#639f9955'; ctx.beginPath(); ctx.ellipse(548, 134, 79, 17, -0.4, 0, Math.PI * 2); ctx.stroke();
    ctx.strokeStyle = '#b0f0c829'; ctx.setLineDash([4, 7]); ctx.beginPath(); ctx.moveTo(20, PLAYER_Y + 30); ctx.lineTo(WIDTH - 20, PLAYER_Y + 30); ctx.stroke(); ctx.setLineDash([]);
    if (game.mode === 'ready') { star(145, 146, 18, -0.2, '#ffe096'); star(404, 306, 12, 0.3, '#ffe096'); }
    for (const item of game.items) {
      if (item.kind === 'star') { ctx.shadowColor = '#ffe096'; ctx.shadowBlur = reducedMotion ? 0 : 14; star(item.x, item.y, item.radius, item.angle, '#ffe096'); ctx.shadowBlur = 0; }
      else {
        if (art.meteor) {
          ctx.save(); ctx.translate(item.x, item.y); ctx.rotate(item.angle);
          ctx.drawImage(art.meteor, -item.radius, -item.radius, item.radius * 2, item.radius * 2); ctx.restore(); continue;
        }
        ctx.save(); ctx.translate(item.x, item.y); ctx.rotate(item.angle); ctx.fillStyle = '#aa7b79'; ctx.strokeStyle = '#deb098'; ctx.lineWidth = 2; ctx.beginPath();
        for (let i = 0; i < 7; i++) { const a = i * Math.PI * 2 / 7, r = item.radius * (i % 2 ? 0.83 : 1); ctx.lineTo(Math.cos(a) * r, Math.sin(a) * r); }
        ctx.closePath(); ctx.fill(); ctx.stroke(); ctx.fillStyle = '#73575c'; ctx.beginPath(); ctx.arc(-4, -3, 5, 0, 7); ctx.fill(); ctx.restore();
      }
    }
    ship(game.x, PLAYER_Y);
    for (const effect of effects) {
      if (game.mode === 'playing') effect.age += dt;
      ctx.globalAlpha = Math.max(0, 1 - effect.age / 0.7); ctx.fillStyle = effect.kind === 'catch' ? '#ffe096' : '#ffb69e';
      ctx.font = 'bold 18px "Segoe UI",sans-serif'; ctx.textAlign = 'center';
      ctx.fillText(effect.kind === 'catch' ? `+${effect.points}` : '−1 护盾', effect.x, effect.y - 30 - effect.age * 50);
    }
    ctx.globalAlpha = 1; effects = effects.filter(e => e.age < 0.7);
  }
  function frame(now) {
    const dt = Math.min(0.05, Math.max(0, (now - previous) / 1000)); previous = now;
    if (game.mode === 'playing' || game.mode === 'ready') visualTime += dt;
    if (game.mode === 'playing') {
      const direction = Number(keys.has('arrowright') || keys.has('d')) - Number(keys.has('arrowleft') || keys.has('a')) + [...held.values()].reduce((a, b) => a + b, 0);
      game.step(dt, direction, target); effects.push(...game.events.map(e => ({...e, age: 0}))); updateHUD();
      if (game.mode === 'ended') finish();
    }
    draw(dt); requestAnimationFrame(frame);
  }
  updateHUD(); requestAnimationFrame(frame);
})();
