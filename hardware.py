"""
hardware.py — 雷霆戰機硬體驅動層（Raspberry Pi 4B）

元件：
  - LCD 1602 I2C (PCF8574, 地址 0x27 或 0x3F)
  - 紅色 LED × 3（生命值）
  - Keyes 無源蜂鳴器（pigpio 硬體 PWM，需先執行 sudo pigpiod）
  - 常開按鍵（射擊，上拉）
  - PS2 搖桿 via MCP3008 SPI ADC（VRx = CH0, VRy = CH1）

GPIO 腳位（BCM 編號，可依實際接線修改）：
  LED_PINS   = [17, 27, 22]
  BUZZER_PIN = 18          ← 必須為硬體 PWM 腳位（12/13/18/19 其中一個）
  BUTTON_PIN = 23
  MCP3008 SPI: CE0（預設 /dev/spidev0.0）

蜂鳴器說明：
  pigpio 硬體 PWM 精度遠優於 RPi.GPIO Software PWM，音色穩定不抖動。
  啟動前請先執行：sudo pigpiod
  安裝：pip install pigpio
"""

import time
import threading
import spidev
import smbus2
import pigpio
from RPi import GPIO

# ── GPIO 腳位設定 ───────────────────────────────────────────
LED_PINS   = [26, 19, 13]   # LED 1, 2, 3（對應生命值 3→1）
BUZZER_PIN = 18
BUTTON_PIN = 23

# ── 搖桿死區 ────────────────────────────────────────────────
JOYSTICK_CENTER = 512
JOYSTICK_DEAD   = 50         # ±50 視為靜止

# ── LCD I2C 設定 ────────────────────────────────────────────
LCD_I2C_ADDR = 0x27          # 若不通請改 0x3F
LCD_BUS      = 1             # /dev/i2c-1

# PCF8574 位元定義
LCD_BACKLIGHT = 0x08
ENABLE        = 0b00000100
RW            = 0b00000010
RS            = 0b00000001


# ══════════════════════════════════════════════════════════
# LCD 1602 I2C 驅動
# ══════════════════════════════════════════════════════════

class LCD1602:
    def __init__(self, addr=LCD_I2C_ADDR, bus=LCD_BUS):
        self.addr = addr
        self.bus  = smbus2.SMBus(bus)
        self._init_lcd()

    def _write_byte(self, data):
        self.bus.write_byte(self.addr, data | LCD_BACKLIGHT)

    def _strobe(self, data):
        self._write_byte(data | ENABLE)
        time.sleep(0.0005)
        self._write_byte(data & ~ENABLE)
        time.sleep(0.0001)

    def _write4bits(self, data):
        self._write_byte(data)
        self._strobe(data)

    def _send(self, data, mode):
        high = mode | (data & 0xF0)
        low  = mode | ((data << 4) & 0xF0)
        self._write4bits(high)
        self._write4bits(low)

    def _init_lcd(self):
        self._write4bits(0x30)
        time.sleep(0.005)
        self._write4bits(0x30)
        time.sleep(0.001)
        self._write4bits(0x30)
        self._write4bits(0x20)          # 4-bit mode
        self._send(0x28, 0)             # 2 lines, 5x8
        self._send(0x0C, 0)             # display on, cursor off
        self._send(0x06, 0)             # entry mode
        self.clear()

    def clear(self):
        self._send(0x01, 0)
        time.sleep(0.002)

    def set_cursor(self, row, col):
        offsets = [0x00, 0x40]
        self._send(0x80 | (offsets[row] + col), 0)

    def write_string(self, text):
        for ch in text:
            self._send(ord(ch), RS)

    def display(self, row0: str, row1: str):
        """一次更新兩行，自動截斷/補空至 16 字元。"""
        self.set_cursor(0, 0)
        self.write_string(f"{row0:<16}"[:16])
        self.set_cursor(1, 0)
        self.write_string(f"{row1:<16}"[:16])


# ══════════════════════════════════════════════════════════
# MCP3008 ADC（SPI，讀搖桿類比值）
# ══════════════════════════════════════════════════════════

class MCP3008:
    def __init__(self, bus=0, device=0):
        self.spi = spidev.SpiDev()
        self.spi.open(bus, device)
        self.spi.max_speed_hz = 1_350_000

    def read(self, channel: int) -> int:
        """讀取指定 channel（0–7），回傳 0–1023。"""
        assert 0 <= channel <= 7
        r = self.spi.xfer2([1, (8 + channel) << 4, 0])
        return ((r[1] & 3) << 8) | r[2]

    def close(self):
        self.spi.close()


# ══════════════════════════════════════════════════════════
# 硬體管理器（統一初始化 / 清理）
# ══════════════════════════════════════════════════════════

class HardwareManager:
    def __init__(self):
        # GPIO 初始化
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)

        # LED
        for pin in LED_PINS:
            GPIO.setup(pin, GPIO.OUT, initial=GPIO.LOW)

        # 無源蜂鳴器（Keyes）— 使用 pigpio 硬體 PWM
        # pigpio 直接控制 GPIO，不需要 GPIO.setup
        self._pi = pigpio.pi()
        if not self._pi.connected:
            raise RuntimeError("pigpio 連線失敗，請先執行：sudo pigpiod")
        # 確保蜂鳴器靜音（duty cycle = 0）
        self._pi.hardware_PWM(BUZZER_PIN, 0, 0)

        # 按鍵（上拉）
        GPIO.setup(BUTTON_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)

        # LCD
        self.lcd = LCD1602()

        # ADC（搖桿）
        self.adc = MCP3008()

        # 防彈跳計時
        self._last_btn_time = 0
        self._btn_debounce  = 0.02      # 20ms

    # ── LED 生命值 ──────────────────────────────────────────
    def set_lives(self, lives: int):
        """依剩餘生命值（0–3）點亮 LED。"""
        lives = max(0, min(3, lives))
        for i, pin in enumerate(LED_PINS):
            GPIO.output(pin, GPIO.HIGH if i < lives else GPIO.LOW)

    # ── 無源蜂鳴器（Keyes，pigpio 硬體 PWM）───────────────
    #
    #   pigpio hardware_PWM(pin, freq, dutycycle)
    #     freq      : Hz，0 = 關閉
    #     dutycycle : 0–1_000_000，500_000 = 50%（方波）
    #
    #   音效頻率對照：
    #     射擊       1200 Hz  短促高音
    #     擊中敵機    900 Hz  中音
    #     玩家被擊中  600 Hz  低沉三連音
    #     遊戲結束    400→200 Hz 滑降長鳴

    _DUTY = 500_000   # 50% duty cycle

    def _tone(self, freq: int, duration_ms: int):
        """同步：發音 → 等待 → 靜音"""
        self._pi.hardware_PWM(BUZZER_PIN, freq, self._DUTY)
        time.sleep(duration_ms / 1000)
        self._pi.hardware_PWM(BUZZER_PIN, 0, 0)

    def _tone_async(self, freq: int, duration_ms: int,
                    repeat: int = 1, gap_ms: int = 80,
                    slide_to: int = 0):
        """
        非同步發音（背景執行緒）。
        slide_to: 若非 0，在最後一段音效中逐步降頻至此值（滑音效果）。
        """
        def _run():
            for i in range(repeat):
                if slide_to and i == repeat - 1:
                    # 滑音：每 40ms 降一次頻率
                    steps     = max(1, duration_ms // 40)
                    step_size = max(1, (freq - slide_to) // steps)
                    f = freq
                    self._pi.hardware_PWM(BUZZER_PIN, f, self._DUTY)
                    for _ in range(steps):
                        f = max(slide_to, f - step_size)
                        self._pi.hardware_PWM(BUZZER_PIN, f, self._DUTY)
                        time.sleep(0.04)
                    self._pi.hardware_PWM(BUZZER_PIN, 0, 0)
                else:
                    self._tone(freq, duration_ms)
                if i < repeat - 1:
                    time.sleep(gap_ms / 1000)
        threading.Thread(target=_run, daemon=True).start()

    def beep_shoot(self):
        """射擊：1200 Hz，50ms 短促高音"""
        self._tone_async(1200, 50)

    def beep_hit_enemy(self):
        """擊中敵機：900 Hz，150ms"""
        self._tone_async(900, 150)

    def beep_player_hit(self):
        """玩家被擊中：600 Hz，三連短音"""
        self._tone_async(600, 100, repeat=3, gap_ms=80)

    def beep_game_over(self):
        """遊戲結束：400 → 200 Hz 滑降長鳴"""
        self._tone_async(400, 1000, slide_to=200)

    # ── 按鍵讀取（防彈跳）──────────────────────────────────
    def is_button_pressed(self) -> bool:
        """回傳 True 代表這次呼叫偵測到有效按下（邊緣觸發 + 防彈跳）。"""
        now = time.time()
        if GPIO.input(BUTTON_PIN) == GPIO.LOW:
            if now - self._last_btn_time > self._btn_debounce:
                self._last_btn_time = now
                return True
        return False

    # ── 搖桿讀取 ────────────────────────────────────────────
    def read_joystick(self) -> dict:
        """
        回傳 {'x': int, 'y': int, 'dx': int, 'dy': int}
        dx/dy 為方向分量：-1 / 0 / +1
        """
        x = self.adc.read(0)    # VRx → CH0
        y = self.adc.read(1)    # VRy → CH1

        dx = 0
        if x < JOYSTICK_CENTER - JOYSTICK_DEAD:
            dx = -1
        elif x > JOYSTICK_CENTER + JOYSTICK_DEAD:
            dx = 1

        dy = 0
        if y < JOYSTICK_CENTER - JOYSTICK_DEAD:
            dy = -1
        elif y > JOYSTICK_CENTER + JOYSTICK_DEAD:
            dy = 1

        return {'x': x, 'y': y, 'dx': dx, 'dy': dy}

    # ── LCD 更新 ─────────────────────────────────────────────
    def update_lcd(self, score: int, lives: int, level: int):
        row0 = f"SCORE:{score:08d}"
        stars = '*' * lives + ' ' * (3 - lives)
        row1 = f"LIFE:{stars} LV:{level:02d}"
        self.lcd.display(row0, row1)

    def lcd_message(self, line0: str, line1: str = ""):
        self.lcd.display(line0, line1)

    # ── 清理資源 ─────────────────────────────────────────────
    def cleanup(self):
        # 關閉蜂鳴器並釋放 pigpio
        try:
            self._pi.hardware_PWM(BUZZER_PIN, 0, 0)
            self._pi.stop()
        except Exception:
            pass
        self.adc.close()
        self.lcd.clear()
        GPIO.cleanup()
