"""
demo_server.py — 雷霆戰機 電腦 Demo 版本
安裝: pip install flask flask-socketio eventlet
啟動: python demo_server.py  -> http://localhost:5000

操作:
  WASD / 方向鍵  移動
  空白鍵          連續射擊
  E               導彈清場（非 Boss）
  P               暫停 / 繼續
  Enter           開始 / 重新開始
"""

import math, random, sqlite3, threading, time, os
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Dict
from flask import Flask, render_template, jsonify
from flask_socketio import SocketIO, emit

# ── 常數 ──────────────────────────────────────────────────
W, H   = 320, 480
FPS    = 30
FRAME  = 1.0 / FPS

INIT_LIVES        = 5
INIT_BULLET_COUNT = 1
MISSILE_MAX       = 3
MISSILE_REGEN     = 15.0
INVINCIBLE_TIME   = 2.0
BUFF_DURATION     = 10.0

DB = os.path.join(os.path.dirname(__file__), "game.db")

# ── 資料庫 ─────────────────────────────────────────────────
def init_db():
    with sqlite3.connect(DB) as c:
        c.execute("""CREATE TABLE IF NOT EXISTS game_records(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            score INTEGER, level INTEGER, datetime TEXT)""")

def save_record(score, level):
    with sqlite3.connect(DB) as c:
        c.execute("INSERT INTO game_records VALUES(NULL,?,?,?)",
                  (score, level, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))

def get_records(n=50):
    with sqlite3.connect(DB) as c:
        rows = c.execute(
            "SELECT id,score,level,datetime FROM game_records "
            "ORDER BY score DESC LIMIT ?", (n,)).fetchall()
    return [{"id":r[0],"score":r[1],"level":r[2],"datetime":r[3]} for r in rows]

# ══════════════════════════════════════════════════════════
# 蜂鳴器（Keyes 無源蜂鳴器，pigpio 硬體 PWM）
# 在樹莓派上接了蜂鳴器（BUZZER_PIN=18，需先執行 sudo pigpiod）才會發聲。
# 在電腦上執行時 pigpio 不存在或連線失敗，自動停用，不影響遊戲。
# ══════════════════════════════════════════════════════════
BUZZER_PIN   = 18
_BUZZER_DUTY = 500_000   # 50% duty cycle

try:
    import pigpio
    _pi = pigpio.pi()
    if not _pi.connected:
        _pi = None
        print("[HW] pigpio 未連線（請先執行 sudo pigpiod），蜂鳴器停用")
    else:
        _pi.hardware_PWM(BUZZER_PIN, 0, 0)
        print("[HW] 蜂鳴器已啟用")
except Exception as e:
    _pi = None
    print(f"[HW] 蜂鳴器不可用，停用音效：{e}")

def _tone(freq: int, duration_ms: int):
    if not _pi: return
    def _run():
        _pi.hardware_PWM(BUZZER_PIN, freq, _BUZZER_DUTY)
        time.sleep(duration_ms / 1000)
        _pi.hardware_PWM(BUZZER_PIN, 0, 0)
    threading.Thread(target=_run, daemon=True).start()

def _tone_slide(freq_from: int, freq_to: int, duration_ms: int):
    if not _pi: return
    def _run():
        steps     = max(1, duration_ms // 40)
        step_size = max(1, (freq_from - freq_to) // steps)
        f = freq_from
        _pi.hardware_PWM(BUZZER_PIN, f, _BUZZER_DUTY)
        for _ in range(steps):
            f = max(freq_to, f - step_size)
            _pi.hardware_PWM(BUZZER_PIN, f, _BUZZER_DUTY)
            time.sleep(0.04)
        _pi.hardware_PWM(BUZZER_PIN, 0, 0)
    threading.Thread(target=_run, daemon=True).start()

def beep_shoot():
    """射擊：1200 Hz，50ms 短促高音"""
    _tone(1200, 50)

def beep_hit_enemy():
    """擊中敵機：900 Hz，150ms"""
    _tone(900, 150)

def beep_player_hit():
    """玩家被擊中：600 Hz，三連短音"""
    def _run():
        for i in range(3):
            _tone(600, 100)
            time.sleep(0.18)
    threading.Thread(target=_run, daemon=True).start()

def beep_game_over():
    """遊戲結束：400 → 200 Hz 滑降長鳴"""
    _tone_slide(400, 200, 1000)

# ══════════════════════════════════════════════════════════
# 遊戲物件
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
    homing: bool = False   # 追蹤彈每幀轉向玩家
    HOMING_RANGE: float = 90.0  # 超出此距離停止追蹤

    def move(self, px: float = 0, py: float = 0):
        if self.homing:
            ddx = px - self.x; ddy = py - self.y
            dist = math.hypot(ddx, ddy) or 1
            if dist <= self.HOMING_RANGE:   # 在範圍內才轉向
                spd  = math.hypot(self.dx, self.dy)
                self.dx += (ddx/dist*spd - self.dx) * 0.18
                self.dy += (ddy/dist*spd - self.dy) * 0.18
                cur = math.hypot(self.dx, self.dy) or 1
                self.dx = self.dx/cur * spd
                self.dy = self.dy/cur * spd
            # 超出範圍：維持目前方向直飛，不做任何修正
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
    kind: str
    active: bool = True
    vy: float = 1.5

    def move(self):
        self.y += self.vy
        if self.y > H + 20: self.active = False


# 各型敵機碰撞半寬/半高（邏輯像素）
ETYPE_SIZE = {
    "scout":   (14, 14),
    "fighter": (20, 16),
    "bomber":  (28, 22),
    "zigzag":  (16, 16),
    "boss":    (36, 32),
}

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

    def should_shoot(self, now, lo, hi):
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
    lives: int = INIT_LIVES
    bullet_count: int = INIT_BULLET_COUNT
    missiles: int = MISSILE_MAX
    score: int = 0
    _last_shoot: float           = field(default=0.0, repr=False)
    _invincible_until: float     = field(default=0.0, repr=False)
    _last_missile_regen: float   = field(default=0.0, repr=False)
    buffs: Dict[str, float]      = field(default_factory=dict)
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

    def shoot(self, now):
        self._last_shoot = now
        dmg = self.get_dmg()
        offsets = {1:[0], 2:[-10,10], 3:[-18,0,18]}
        return [Bullet(self.x+ox, self.y-20, dy=-12, dmg=dmg)
                for ox in offsets.get(self.bullet_count, [0])]

    def tick_buffs(self, now):
        expired = [k for k,v in self.buffs.items() if v != -1 and now > v]
        for k in expired: del self.buffs[k]

    def tick_missile_regen(self, now):
        if self.missiles < MISSILE_MAX and now - self._last_missile_regen >= MISSILE_REGEN:
            self.missiles += 1
            self._last_missile_regen = now

    def apply_buff(self, kind, now):
        if kind == "hp":
            self.lives = min(self.lives + 1, INIT_LIVES)
        elif kind == "bullet_up":
            self.bullet_count = min(self.bullet_count + 1, 3)
        elif kind == "missile":
            self.missiles = min(self.missiles + 1, MISSILE_MAX)
        else:
            self.buffs[kind] = now + BUFF_DURATION


def overlap(ax, ay, aw, ah, bx, by, bw, bh):
    return ax < bx+bw and ax+aw > bx and ay < by+bh and ay+ah > by


# ── 關卡設定 ───────────────────────────────────────────────
def level_cfg(lv):
    return dict(
        base_hp    = min(1 + lv // 2, 5),
        speed      = min(1.0 + lv * 0.3, 5.0),
        max_enemies= min(3 + lv, 12),
        shoot_interval = (max(0.5, 2.5 - lv*0.15), max(1.0, 4.0 - lv*0.2)),
        spawn_prob = min(0.02 + lv*0.005, 0.06),
        boss_threshold = lv >= 3 and lv % 3 == 0,
    )


# ══════════════════════════════════════════════════════════
# 遊戲主類
# ══════════════════════════════════════════════════════════
class Game:
    SPEED = 4

    def __init__(self):
        self.state   = "menu"
        self.player  = Player()
        self.enemies: List[Enemy]  = []
        self.bullets: List[Bullet] = []
        self.buffs:   List[Buff]   = []
        self.level   = 1
        self.keys    = set()
        self._lock   = threading.Lock()
        self._boss_spawned = False

    def start(self):
        with self._lock:
            self.player = Player()
            self.enemies.clear(); self.bullets.clear(); self.buffs.clear()
            self.level = 1; self.keys.clear()
            self._boss_spawned = False
            self.state = "playing"

    def toggle_pause(self):
        if   self.state == "playing": self.state = "paused"
        elif self.state == "paused":  self.state = "playing"

    def key_down(self, key): self.keys.add(key)
    def key_up(self,   key): self.keys.discard(key)

    def update(self):
        if self.state != "playing": return
        now = time.time()
        cfg = level_cfg(self.level)
        p   = self.player

        with self._lock:
            p.tick_buffs(now)
            p.tick_missile_regen(now)

            # 移動
            if "ArrowLeft"  in self.keys or "a" in self.keys: p.x -= self.SPEED
            if "ArrowRight" in self.keys or "d" in self.keys: p.x += self.SPEED
            if "ArrowUp"    in self.keys or "w" in self.keys: p.y -= self.SPEED
            if "ArrowDown"  in self.keys or "s" in self.keys: p.y += self.SPEED
            p.x = max(16, min(W-16, p.x))
            p.y = max(16, min(H-16, p.y))

            # 射擊
            if " " in self.keys and p.can_shoot(now):
                self.bullets.extend(p.shoot(now))
                beep_shoot()

            # 導彈
            if "e" in self.keys and p.missiles > 0:
                self.keys.discard("e")
                p.missiles -= 1
                p._last_missile_regen = now
                for e in self.enemies:
                    if not e.is_boss: e.active = False

            # 生成敵機
            if sum(1 for e in self.enemies if e.active) < cfg['max_enemies']:
                if random.random() < cfg['spawn_prob']:
                    self._spawn_enemy(cfg, now)

            # Boss
            if cfg['boss_threshold'] and not self._boss_spawned:
                if not any(e.is_boss for e in self.enemies):
                    self._spawn_boss()
                    self._boss_spawned = True

            # 敵機更新
            for e in self.enemies:
                if not e.active: continue
                e.move()
                if e.etype == "scout": continue
                if e.should_shoot(now, *cfg['shoot_interval']):
                    self.bullets.extend(self._enemy_shoot(e, p.x, p.y))

            # 子彈移動（追蹤彈需要玩家座標）
            for b in self.bullets:
                if b.active: b.move(p.x, p.y)

            # Buff 移動
            for bf in self.buffs:
                if bf.active: bf.move()

            # 碰撞
            self._collide(now)

            # 清理
            self.enemies = [e  for e  in self.enemies if e.active]
            self.bullets = [b  for b  in self.bullets if b.active]
            self.buffs   = [bf for bf in self.buffs   if bf.active]

            # 升關
            new_lv = max(1, p.score // 1500 + 1)
            if new_lv > self.level:
                self.level = new_lv
                self._boss_spawned = False

            # 結束
            if p.lives <= 0:
                self.state = "gameover"
                save_record(p.score, self.level)
                beep_game_over()

    def _spawn_enemy(self, cfg, now):
        lv  = self.level
        ex  = random.randint(20, W - 20)
        spd = cfg['speed'] * random.uniform(0.8, 1.2)
        lo, hi = cfg['shoot_interval']

        if lv <= 1:
            pool = ["scout","fighter","fighter"]
        elif lv <= 3:
            pool = ["scout","fighter","fighter","bomber","zigzag"]
        else:
            pool = ["scout","fighter","bomber","zigzag","zigzag","bomber"]

        etype = random.choice(pool)
        hp_map   = {"scout":1, "fighter":max(1,cfg['base_hp']-1),
                    "bomber":cfg['base_hp']+2, "zigzag":max(1,cfg['base_hp'])}
        mtype_map = {"scout":"dive","fighter":"sweep",
                     "bomber":"sweep","zigzag":"zigzag_move"}

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

    def _enemy_shoot(self, e, px, py):
        bx, by = e.x, e.y + e.hh + 2
        dx0 = px - bx; dy0 = py - by
        dist = math.hypot(dx0, dy0) or 1

        if e.etype == "fighter":
            spd = 5
            return [Bullet(bx, by, dx=dx0/dist*spd, dy=dy0/dist*spd,
                           owner="enemy", kind="normal", dmg=1)]

        elif e.etype == "bomber":
            bullets = []
            base_angle = math.atan2(dy0, dx0)
            for off in [-25, 0, 25]:
                rad = base_angle + math.radians(off)
                spd = 4
                bullets.append(Bullet(bx, by,
                    dx=math.cos(rad)*spd, dy=math.sin(rad)*spd,
                    owner="enemy", kind="spread", dmg=1))
            return bullets

        elif e.etype == "zigzag":
            return [Bullet(bx, by, dx=0, dy=14,
                           owner="enemy", kind="laser", dmg=1)]

        elif e.etype == "boss":
            spd = 3.5
            return [Bullet(bx, by, dx=dx0/dist*spd, dy=dy0/dist*spd,
                           owner="enemy", kind="homing", dmg=2, homing=True)]
        return []

    def _collide(self, now):
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
                        beep_hit_enemy()
                        if e.hp <= 0:
                            e.active = False
                            pts = {"scout":50,"fighter":100,"bomber":200,
                                   "zigzag":150,"boss":500}.get(e.etype, 100)
                            p.score += pts
                            self._drop_buff(e.x, e.y)
                        break
            else:
                if overlap(b.x-b.hw, b.y-b.hh, b.hw*2, b.hh*2,
                           p.x-16, p.y-16, 32, 32):
                    b.active = False
                    if not p.is_invincible(now):
                        beep_player_hit()
                    p.take_hit(now)

        for bf in self.buffs:
            if not bf.active: continue
            if overlap(bf.x-12, bf.y-12, 24, 24, p.x-16, p.y-16, 32, 32):
                bf.active = False
                p.apply_buff(bf.kind, now)

    def _drop_buff(self, x, y):
        if random.random() < 0.25:
            kinds   = ["dmg_reduce","hp","atk_boost","bullet_up","dmg_boost","missile"]
            weights = [20, 20, 15, 15, 15, 15]
            self.buffs.append(Buff(x=x, y=y,
                                   kind=random.choices(kinds, weights=weights)[0]))

    def snapshot(self):
        p = self.player; now = time.time()
        return {
            "state":    self.state,
            "score":    p.score,
            "level":    self.level,
            "lives":    p.lives,
            "missiles": p.missiles,
            "missile_max": MISSILE_MAX,
            "bullet_count": p.bullet_count,
            "invincible": p.is_invincible(now),
            "active_buffs": list(p.buffs.keys()),
            "player":  {"x": p.x, "y": p.y},
            "enemies": [{"x":e.x,"y":e.y,"hp":e.hp,"max_hp":e.max_hp,
                         "etype":e.etype,"is_boss":e.is_boss}
                        for e in self.enemies],
            "bullets": [{"x":b.x,"y":b.y,"dx":b.dx,"dy":b.dy,
                         "owner":b.owner,"kind":b.kind}
                        for b in self.bullets],
            "buffs":   [{"x":bf.x,"y":bf.y,"kind":bf.kind} for bf in self.buffs],
            "records": get_records(10) if self.state == "gameover" else [],
            "W": W, "H": H,
        }


# ══════════════════════════════════════════════════════════
# Flask + SocketIO
# ══════════════════════════════════════════════════════════
app  = Flask(__name__)
app.config["SECRET_KEY"] = "thunder-fighter-secret"
sio  = SocketIO(app, cors_allowed_origins="*", async_mode="threading")
game = Game()

def game_loop():
    while True:
        t0 = time.time()
        game.update()
        sio.emit("state", game.snapshot())
        elapsed = time.time() - t0
        rest = FRAME - elapsed
        if rest > 0: time.sleep(rest)


# ══════════════════════════════════════════════════════════
# 實體搖桿輸入（MCP3008 + 按鍵，在 Raspberry Pi 上使用）
# 電腦上執行時硬體不存在，自動跳過不影響鍵盤操作
# ══════════════════════════════════════════════════════════

# 搖桿方向 → 對應虛擬按鍵名
_JOY_KEYS = {
    "left":  "ArrowLeft",
    "right": "ArrowRight",
    "up":    "ArrowUp",
    "down":  "ArrowDown",
}

def _joystick_loop():
    """
    讀取 MCP3008 搖桿（CH0=VRx, CH1=VRy）與按鍵，
    模擬 keydown / keyup 注入 game.keys。

    腳位：
      MCP3008 SPI0 CE0（BCM 8）
      射擊按鍵      BCM 23（上拉，按下為 LOW）
      搖桿 SW 按鈕  BCM 24（上拉，按下為 LOW → 導彈）

    校正流程：
      啟動時自動校正，請將搖桿放在自然靜止位置。
      採樣 50 次（約 1 秒）取平均值作為中心點。
      死區 = 校正中心 ± dead_zone（預設 60）。
    """
    try:
        import spidev
        from RPi import GPIO

        BUTTON_PIN  = 23
        MISSILE_PIN = 24
        DEBOUNCE    = 0.02
        DEAD_ZONE   = 60    # 校正後死區大小（可依搖桿品質調整）
        SAMPLES     = 50    # 校正採樣次數

        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        GPIO.setup(BUTTON_PIN,  GPIO.IN, pull_up_down=GPIO.PUD_UP)
        GPIO.setup(MISSILE_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)

        spi = spidev.SpiDev()
        spi.open(0, 0)
        spi.max_speed_hz = 1_350_000

        def read_adc(ch):
            r = spi.xfer2([1, (8 + ch) << 4, 0])
            return ((r[1] & 3) << 8) | r[2]

        # ── 自動校正：採樣靜止中心點 ─────────────────────
        print("[HW] 搖桿校正中，請勿移動搖桿...")
        sio.emit("calibrating", {"msg": "搖桿校正中，請勿移動搖桿..."})

        cx_sum = cy_sum = 0
        for _ in range(SAMPLES):
            cx_sum += read_adc(0)
            cy_sum += read_adc(1)
            time.sleep(0.02)

        center_x = cx_sum // SAMPLES
        center_y = cy_sum // SAMPLES

        print(f"[HW] 校正完成：中心 X={center_x}, Y={center_y}，死區=±{DEAD_ZONE}")
        sio.emit("calibrating", {"msg": f"校正完成！中心({center_x},{center_y})"})
        time.sleep(0.5)
        sio.emit("calibrating", {"msg": ""})

        last_btn_time     = 0.0
        last_missile_time = 0.0
        active_joy_keys   = set()

        print("[HW] 搖桿輸入已啟動")

        while True:
            now = time.time()
            vx  = read_adc(0)
            vy  = read_adc(1)

            # 以校正中心為基準判斷方向
            wanted = set()
            if vx < center_x - DEAD_ZONE: wanted.add("left")
            if vx > center_x + DEAD_ZONE: wanted.add("right")
            if vy < center_y - DEAD_ZONE: wanted.add("up")
            if vy > center_y + DEAD_ZONE: wanted.add("down")

            for d in wanted - active_joy_keys:
                game.key_down(_JOY_KEYS[d])
            for d in active_joy_keys - wanted:
                game.key_up(_JOY_KEYS[d])
            active_joy_keys = wanted

            # 射擊按鍵（防彈跳）
            if GPIO.input(BUTTON_PIN) == GPIO.LOW:
                if now - last_btn_time > DEBOUNCE:
                    last_btn_time = now
                    game.key_down(" ")
            else:
                game.key_up(" ")

            # 導彈（搖桿 SW）
            if GPIO.input(MISSILE_PIN) == GPIO.LOW:
                if now - last_missile_time > 0.5:
                    last_missile_time = now
                    game.key_down("e")
                    time.sleep(0.05)
                    game.key_up("e")

            time.sleep(0.02)   # 50Hz 輪詢

    except Exception as e:
        print(f"[HW] 搖桿不可用，使用鍵盤操作：{e}")

@app.route("/")
def index(): return render_template("game.html")

@app.route("/api/records")
def api_records(): return jsonify({"records": get_records()})

@sio.on("keydown")
def on_kd(key):  game.key_down(key)
@sio.on("keyup")
def on_ku(key):  game.key_up(key)
@sio.on("start")
def on_start():  game.start()
@sio.on("pause")
def on_pause():  game.toggle_pause()
@sio.on("connect")
def on_conn():   emit("state", game.snapshot())


if __name__ == "__main__":
    init_db()
    threading.Thread(target=game_loop,      daemon=True).start()
    threading.Thread(target=_joystick_loop, daemon=True).start()
    print("=" * 44)
    print("  雷霆戰機  ->  http://0.0.0.0:5000")
    print("  Ctrl+C 停止")
    print("=" * 44)
    sio.run(app, host="0.0.0.0", port=5000, debug=False)
