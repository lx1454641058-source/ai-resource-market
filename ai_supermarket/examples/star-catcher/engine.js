/* 来源：本项目原创。独立游戏规则，浏览器与 Node 共用；不请求网络。 */
(function (root) {
  'use strict';
  const WIDTH = 640, HEIGHT = 560, PLAYER_Y = 505;
  const clamp = (n, min, max) => Math.max(min, Math.min(max, n));
  class Game {
    constructor(random = Math.random) { this.random = random; this.reset(); this.mode = 'ready'; }
    reset() {
      this.mode = 'playing'; this.x = WIDTH / 2; this.score = 0; this.lives = 3;
      this.elapsed = 0; this.chain = 0; this.caught = 0; this.invulnerable = 0;
      this.items = []; this.spawnIn = 0.45; this.events = [];
    }
    get remaining() { return Math.max(0, 60 - this.elapsed); }
    get multiplier() { return Math.min(5, 1 + Math.floor(this.chain / 5)); }
    pause() { if (this.mode === 'playing') this.mode = 'paused'; }
    resume() { if (this.mode === 'paused') this.mode = 'playing'; }
    step(seconds, direction = 0, target = null) {
      this.events = [];
      if (this.mode !== 'playing' || !Number.isFinite(seconds) || seconds <= 0) return;
      // Cap a stalled frame so returning from a suspended tab never teleports hazards.
      const dt = Math.min(seconds, 0.05, this.remaining);
      this.elapsed = Math.min(60, this.elapsed + dt);
      this.invulnerable = Math.max(0, this.invulnerable - dt);
      if (Number.isFinite(target)) this.x += clamp(target - this.x, -560 * dt, 560 * dt);
      else this.x += clamp(direction, -1, 1) * 390 * dt;
      this.x = clamp(this.x, 25, WIDTH - 25);
      this.spawnIn -= dt;
      if (this.spawnIn <= 0) {
        const rock = this.random() < 0.22 + this.elapsed / 650;
        this.items.push({kind: rock ? 'rock' : 'star', x: 28 + this.random() * (WIDTH - 56),
          y: -30, radius: rock ? 19 : 13, speed: 125 + this.elapsed * 2.6 + this.random() * 35,
          angle: this.random() * Math.PI * 2});
        this.spawnIn = Math.max(0.28, 0.65 - this.elapsed * 0.006);
      }
      const survivors = [];
      for (const item of this.items) {
        item.y += item.speed * dt;
        item.angle += dt * (item.kind === 'star' ? 0.9 : -0.6);
        if (Math.hypot(item.x - this.x, item.y - PLAYER_Y) < item.radius + 19) {
          if (item.kind === 'star') {
            this.chain++; this.caught++;
            const points = 10 * this.multiplier;
            this.score += points;
            this.events.push({kind: 'catch', x: item.x, y: item.y, points});
          } else if (this.invulnerable <= 0) {
            this.lives--; this.chain = 0; this.invulnerable = 1.25;
            this.events.push({kind: 'hit', x: this.x, y: PLAYER_Y});
            if (!this.lives) { this.mode = 'ended'; break; }
          }
        } else if (item.y < HEIGHT + 35) survivors.push(item);
        else if (item.kind === 'star') this.chain = 0;
      }
      this.items = survivors;
      if (this.remaining <= 0) this.mode = 'ended';
    }
  }
  const api = {Game, WIDTH, HEIGHT, PLAYER_Y};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.StarCatcher = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
