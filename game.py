"""
game.py — 雷霆戰機遊戲邏輯（Raspberry Pi 4B）

與 demo_server.py 保持同步的完整遊戲邏輯。
畫面輸出：
  - LCD 1602 顯示分數 / 生命 / 關卡
  - Flask API 推送即時狀態供網頁顯示

座標系統（邏輯像素）：320 × 480
"""

import math
import random
import threading
import time
import requests
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Dict, Optional

# ── 畫布尺寸 ───────────────────────────────────────────────
W, H = 320, 480

# ── 玩家常數 ───────────────────────────────────────────────
INIT_LIVES        = 5
INIT_BULLET_COUNT = 1
MISSILE_MAX       = 3
MISSILE_REGEN     = 15.0   # 秒，自動補充一枚
INVINCIBLE_TIME   = 2.0    # 被擊中後無敵秒數
BUFF_DURATION     = 10.0   # Buff 持續秒數

# ── Flask API ─────────────────────────────────────────────
FLASK_URL = "http://localhost:5000/api/update"

# ── 遊戲狀態 ───────────────────────────────────────────────
class State:
    WAITING  = "waiting"
    PLAYING  = "playing"
    PAUSED   = "paused"
    GAMEOVER = "gameover"

# ── 各型敵機碰撞半寬/半高 ──────────────────────────────────
ETYPE_SIZE = {
    "scout":   (14, 14),
    "fighter": (20, 16),
    "bomber":  (28, 22),
    "zigzag":  (16, 16),
    "boss":    (36, 32),
}

# ── 擊殺分數 ───────────────────────────────────────────────
ETYPE_SCORE = {
    "scout": 50, "fighter": 100, "bomber": 200,
    "zigzag": 150, "boss": 500,
}


# ══════════════════════════════════════════════════════════
# 資料類別
# ══════════════════════════════════════════════════════════

@dataclass
class Bullet:
    x: float; y: float
    dx: float = 0.0
    dy: float = -12.0
    owner: str = "player"
    # 敵機子彈種類: normal | spread | laser | homing
    # 玩家子彈種類: normal
    kind:  str = "normal"
    dmg:   int = 1
    active: bool = True
    homing: bool = False
    HOMING_RANGE: float = 180.0   # 超出此距離停止追蹤

    def move(self, px: float = 0, py: float = 0):
        if self.homing:
            ddx = px - self.x; ddy = py - self.y
            dist = math.hypot(ddx, ddy) or 1
            if dist <= self.HOMING_RANGE:
                spd = math.hypot(self.dx, self.dy)
                self.dx += (ddx/dist*spd - self.dx) * 0.18
                self.dy += (ddy/dist*spd - self.dy) * 0.18
                cur = math.hypot(self.dx, self.dy) or 1
                self.dx = self.dx/cur * spd
                self.dy = self.dy/cur * spd
        self.x += self.dx
        self.y += self.dy
        if self.y < -40 or self.y > H+40 or self.x < -40 or self.x > W+40:
            self.active = False

    @property
    def hw(self): return {"laser":3,"homing":8,"spread":4}.get(self.kind, 5)
    @property
    def hh(self): return {"laser":14,"homing":8,"spread":6}.get(self.kind, 10)


@dataclass
class Buff:
    x: float; y: float
    kind: str   # dmg_reduce | hp | atk_boost | bullet_up | dmg_boost | missile
    active: bool = True
    vy: float = 1.5

    def move(self):
        self.y += self.vy
        if self.y > H + 20: self.active = False


@dataclass
class Enemy:
    x: float; y: float
    hp: int; max_hp: int
    speed: float
    move_type: str   # sweep | dive | zigzag_move
    etype: str = "fighter"
    is_boss: bool = False
    active: bool = True
    _dir:  int   = field(default=1,   repr=False)
    _tick: float = field(default=0.0, repr=False)
    _next_shoot: float = field(default=0.0, repr=False)

    def move(self):
        if self.move_type == "sweep":
            self.x += self.speed * self._dir
            self.y += self.speed * 0.25
            if self.x >= W - self.hw: self._dir = -1
            if self.x <= self.hw:     self._dir =  1
        elif self.move_type == "zigzag_move":
            self._tick += 0.08
            self.x = max(self.hw, min(W - self.hw,
                         self.x + math.sin(self._tick) * self.speed * 1.8))
            self.y += self.speed * 0.7
        else:  # dive
            self.y += self.speed
        if self.y > H + 60: self.active = False

    def should_shoot(self, now: float, lo: float, hi: float) -> bool:
        if now >= self._next_shoot:
            self._next_shoot = now + random.uniform(lo, hi)
            return True
        return False

    @property
    def hw(self): return ETYPE_SIZE.get(self.etype, (20,16))[0]
    @property
    def hh(self): return ETYPE_SIZE.get(self.etype, (20,16))[1]


@dataclass
class Player:
    x: float = W / 2
    y: float = H - 50
    speed: float = 4.0
    lives: int = INIT_LIVES
    bullet_count: int = INIT_BULLET_COUNT
    missiles: int = MISSILE_MAX
    score: int = 0
    _last_shoot: float         = field(default=0.0, repr=False)
    _invincible_until: float   = field(default=0.0, repr=False)
    _last_missile_regen: float = field(default=0.0, repr=False)
    buffs: Dict[str, float]    = field(default_factory=dict)
    SHOOT_CD: float = 0.12

    def is_invincible(self, now): return now < self._invincible_until
    def can_shoot(self, now):     return now - self._last_shoot >= self.SHOOT_CD

    def get_dmg(self):
        base = 2 if "dmg_boost" in self.buffs else 1
        if "atk_boost" in self.buffs: base *= 2
        return base

    def take_hit(self, now):
        if self.is_invincible(now): return
        if "dmg_reduce" in self.buffs:
            del self.buffs["dmg_reduce"]
        else:
            self.lives -= 1
        self._invincible_until = now + INVINCIBLE_TIME

    def move(self, dx: int, dy: int):
        self.x = max(16, min(W-16, self.x + dx * self.speed))
        self.y = max(16, min(H-16, self.y + dy * self.speed))

    def shoot(self, now) -> List[Bullet]:
        self._last_shoot = now
        dmg = self.get_dmg()
        offsets = {1:[0], 2:[-10,10], 3:[-18,0,18]}
        return [Bullet(self.x+ox, self.y-20, dy=-12, dmg=dmg)
                for ox in offsets.get(self.bullet_count, [0])]

    def tick_buffs(self, now):
        expired = [k for k,v in self.buffs.items() if v != -1 and now > v]
        for k in expired: del self.buffs[k]

    def tick_missile_regen(self, now):
        if self.missiles < MISSILE_MAX and \
                now - self._last_missile_regen >= MISSILE_REGEN:
            self.missiles += 1
            self._last_missile_regen = now

    def apply_buff(self, kind: str, now: float):
        if kind == "hp":
            self.lives = min(self.lives + 1, INIT_LIVES)
        elif kind == "bullet_up":
            self.bullet_count = min(self.bullet_count + 1, 3)
        elif kind == "missile":
            self.missiles = min(self.missiles + 1, MISSILE_MAX)
        else:
            self.buffs[kind] = now + BUFF_DURATION


def overlap(ax, ay, aw, ah, bx, by, bw, bh) -> bool:
    return ax < bx+bw and ax+aw > bx and ay < by+bh and ay+ah > by


# ── 關卡設定 ───────────────────────────────────────────────
def level_cfg(lv: int) -> dict:
    return dict(
        base_hp       = min(1 + lv // 2, 5),
        speed         = min(1.0 + lv * 0.3, 5.0),
        max_enemies   = min(3 + lv, 12),
        shoot_interval= (max(0.5, 2.5 - lv*0.15), max(1.0, 4.0 - lv*0.2)),
        spawn_prob    = min(0.02 + lv*0.005, 0.06),
        boss_threshold= lv >= 3 and lv % 3 == 0,
    )


# ══════════════════════════════════════════════════════════
# 遊戲主類別
# ══════════════════════════════════════════════════════════

class ThunderFighter:
    LEVEL_UP_SCORE = 1500

    def __init__(self, hw=None):
        """
        hw: HardwareManager 實例（可為 None，僅跑邏輯測試）
        """
        self.hw      = hw
        self.state   = State.WAITING
        self.player  = Player()
        self.enemies: List[Enemy]  = []
        self.bullets: List[Bullet] = []
        self.buffs:   List[Buff]   = []
        self.level   = 1
        self._lock   = threading.Lock()
        self._boss_spawned = False

    # ── 狀態控制 ───────────────────────────────────────────
    def start(self):
        self.player  = Player()
        self.enemies.clear(); self.bullets.clear(); self.buffs.clear()
        self.level   = 1
        self._boss_spawned = False
        self.state   = State.PLAYING
        if self.hw:
            self.hw.set_lives(INIT_LIVES)
            self.hw.lcd_message("Thunder Fighter", "  GET READY!    ")
            time.sleep(1.5)

    def pause(self):
        if   self.state == State.PLAYING: self.state = State.PAUSED
        elif self.state == State.PAUSED:  self.state = State.PLAYING

    def game_over(self):
        self.state = State.GAMEOVER
        if self.hw:
            self.hw.beep_game_over()
            self.hw.set_lives(0)
            self.hw.lcd_message("  GAME  OVER    ",
                                f"SCORE:{self.player.score:08d}")
        self._post_to_flask("gameover")

    # ── 主更新（每幀呼叫）─────────────────────────────────
    def update(self, dt: float):
        if self.state != State.PLAYING:
            return

        now = time.time()
        cfg = level_cfg(self.level)
        p   = self.player

        p.tick_buffs(now)
        p.tick_missile_regen(now)

        # ── 硬體輸入 ─────────────────────────────────────
        if self.hw:
            joy = self.hw.read_joystick()
            p.move(joy['dx'], joy['dy'])

            if self.hw.is_button_pressed() and p.can_shoot(now):
                self.bullets.extend(p.shoot(now))
                self.hw.beep_shoot()

            # 搖桿按下 = 導彈（SW LOW）
            if joy.get('sw') and p.missiles > 0:
                p.missiles -= 1
                p._last_missile_regen = now
                for e in self.enemies:
                    if not e.is_boss: e.active = False

        # ── 生成敵機 ─────────────────────────────────────
        active_e = sum(1 for e in self.enemies if e.active)
        if active_e < cfg['max_enemies'] and random.random() < cfg['spawn_prob']:
            self._spawn_enemy(cfg, now)

        # ── Boss ─────────────────────────────────────────
        if cfg['boss_threshold'] and not self._boss_spawned:
            if not any(e.is_boss for e in self.enemies):
                self._spawn_boss()
                self._boss_spawned = True

        # ── 敵機更新 ─────────────────────────────────────
        for e in self.enemies:
            if not e.active: continue
            e.move()
            if e.etype == "scout": continue
            if e.should_shoot(now, *cfg['shoot_interval']):
                self.bullets.extend(self._enemy_shoot(e, p.x, p.y))

        # ── 子彈移動 ─────────────────────────────────────
        for b in self.bullets:
            if b.active: b.move(p.x, p.y)

        # ── Buff 移動 ─────────────────────────────────────
        for bf in self.buffs:
            if bf.active: bf.move()

        # ── 碰撞偵測 ─────────────────────────────────────
        self._collide(now)

        # ── 清理 ─────────────────────────────────────────
        self.enemies = [e  for e  in self.enemies if e.active]
        self.bullets = [b  for b  in self.bullets if b.active]
        self.buffs   = [bf for bf in self.buffs   if bf.active]

        # ── 升關 ─────────────────────────────────────────
        new_lv = max(1, p.score // self.LEVEL_UP_SCORE + 1)
        if new_lv > self.level:
            self.level = new_lv
            self._boss_spawned = False
            if self.hw:
                self.hw.lcd_message(f"  LEVEL UP! {self.level:02d}  ", "")
                time.sleep(0.8)

        # ── LCD 更新 ─────────────────────────────────────
        if self.hw:
            self.hw.update_lcd(p.score, p.lives, self.level)

        # ── 遊戲結束 ─────────────────────────────────────
        if p.lives <= 0:
            self.game_over()

    # ── 敵機生成 ───────────────────────────────────────────
    def _spawn_enemy(self, cfg: dict, now: float):
        lv  = self.level
        ex  = random.randint(20, W - 20)
        spd = cfg['speed'] * random.uniform(0.8, 1.2)
        lo, hi = cfg['shoot_interval']

        if lv <= 1:
            pool = ["scout", "fighter", "fighter"]
        elif lv <= 3:
            pool = ["scout", "fighter", "fighter", "bomber", "zigzag"]
        else:
            pool = ["scout", "fighter", "bomber", "zigzag", "zigzag", "bomber"]

        etype = random.choice(pool)
        hp_map    = {"scout":1, "fighter":max(1, cfg['base_hp']-1),
                     "bomber":cfg['base_hp']+2, "zigzag":max(1, cfg['base_hp'])}
        mtype_map = {"scout":"dive", "fighter":"sweep",
                     "bomber":"sweep", "zigzag":"zigzag_move"}

        hp = hp_map[etype]
        self.enemies.append(Enemy(
            x=ex, y=-20, hp=hp, max_hp=hp,
            speed=spd, move_type=mtype_map[etype], etype=etype,
            _next_shoot=now + random.uniform(lo, hi)
        ))

    def _spawn_boss(self):
        hp = 20 + self.level * 5
        self.enemies.append(Enemy(
            x=W//2, y=-40, hp=hp, max_hp=hp,
            speed=1.2, move_type="sweep", etype="boss", is_boss=True,
        ))

    # ── 敵機射擊 ───────────────────────────────────────────
    def _enemy_shoot(self, e: Enemy, px: float, py: float) -> List[Bullet]:
        bx, by = e.x, e.y + e.hh + 2
        dx0 = px - bx; dy0 = py - by
        dist = math.hypot(dx0, dy0) or 1

        if e.etype == "fighter":
            spd = 5
            return [Bullet(bx, by, dx=dx0/dist*spd, dy=dy0/dist*spd,
                           owner="enemy", kind="normal", dmg=1)]

        elif e.etype == "bomber":
            base = math.atan2(dy0, dx0)
            return [Bullet(bx, by,
                           dx=math.cos(base + math.radians(off))*4,
                           dy=math.sin(base + math.radians(off))*4,
                           owner="enemy", kind="spread", dmg=1)
                    for off in [-25, 0, 25]]

        elif e.etype == "zigzag":
            return [Bullet(bx, by, dx=0, dy=14,
                           owner="enemy", kind="laser", dmg=1)]

        elif e.etype == "boss":
            spd = 3.5
            return [Bullet(bx, by, dx=dx0/dist*spd, dy=dy0/dist*spd,
                           owner="enemy", kind="homing", dmg=2, homing=True)]
        return []

    # ── 碰撞 ───────────────────────────────────────────────
    def _collide(self, now: float):
        p = self.player
        for b in self.bullets:
            if not b.active: continue
            if b.owner == "player":
                for e in self.enemies:
                    if not e.active: continue
                    if overlap(b.x-b.hw, b.y-b.hh, b.hw*2, b.hh*2,
                               e.x-e.hw, e.y-e.hh, e.hw*2, e.hh*2):
                        e.hp -= b.dmg
                        b.active = False
                        if e.hp <= 0:
                            e.active = False
                            p.score += ETYPE_SCORE.get(e.etype, 100)
                            self._drop_buff(e.x, e.y)
                            if self.hw:
                                self.hw.beep_hit_enemy()
                        break
            else:
                if overlap(b.x-b.hw, b.y-b.hh, b.hw*2, b.hh*2,
                           p.x-16, p.y-16, 32, 32):
                    b.active = False
                    was_hit = not p.is_invincible(now)
                    p.take_hit(now)
                    if was_hit and self.hw:
                        self.hw.beep_player_hit()
                        self.hw.set_lives(p.lives)

        # Buff 撿取
        for bf in self.buffs:
            if not bf.active: continue
            if overlap(bf.x-12, bf.y-12, 24, 24, p.x-16, p.y-16, 32, 32):
                bf.active = False
                p.apply_buff(bf.kind, now)

    def _drop_buff(self, x: float, y: float):
        if random.random() < 0.25:
            kinds   = ["dmg_reduce","hp","atk_boost","bullet_up","dmg_boost","missile"]
            weights = [20, 20, 15, 15, 15, 15]
            self.buffs.append(Buff(x=x, y=y,
                                   kind=random.choices(kinds, weights=weights)[0]))

    # ── 狀態序列化（供 Flask 使用）─────────────────────────
    def get_state_dict(self) -> dict:
        p = self.player
        now = time.time()
        return {
            "score":        p.score,
            "level":        self.level,
            "lives":        p.lives,
            "missiles":     p.missiles,
            "bullet_count": p.bullet_count,
            "active_buffs": list(p.buffs.keys()),
            "invincible":   p.is_invincible(now),
            "status":       self.state,
            "player":  {"x": p.x, "y": p.y},
            "enemies": [{"x":e.x,"y":e.y,"hp":e.hp,"max_hp":e.max_hp,
                         "etype":e.etype,"is_boss":e.is_boss}
                        for e in self.enemies if e.active],
            "bullets": [{"x":b.x,"y":b.y,"dx":b.dx,"dy":b.dy,
                         "owner":b.owner,"kind":b.kind}
                        for b in self.bullets if b.active],
            "buffs":   [{"x":bf.x,"y":bf.y,"kind":bf.kind}
                        for bf in self.buffs if bf.active],
        }

    # ── 非同步推送至 Flask ─────────────────────────────────
    def _post_to_flask(self, status_override: Optional[str] = None):
        payload = {
            "score":  self.player.score,
            "level":  self.level,
            "lives":  self.player.lives,
            "status": status_override or self.state,
        }
        def _send():
            try:
                requests.post(FLASK_URL, json=payload, timeout=2)
            except Exception:
                pass
        threading.Thread(target=_send, daemon=True).start()

    def periodic_push(self, interval: float = 1.0):
        """每隔 interval 秒推一次完整狀態給 Flask。"""
        if self.state == State.PLAYING:
            # 推送完整快照（含敵機/子彈位置，供網頁即時畫面使用）
            payload = self.get_state_dict()
            def _send():
                try:
                    requests.post(FLASK_URL, json=payload, timeout=2)
                except Exception:
                    pass
            threading.Thread(target=_send, daemon=True).start()
        t = threading.Timer(interval, self.periodic_push, args=(interval,))
        t.daemon = True
        t.start()
