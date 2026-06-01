"""
main.py — 雷霆戰機主程式（Raspberry Pi 4B）

執行方式：
  python main.py           # 正常執行（需硬體）
  python main.py --demo    # 純軟體 Demo 模式（不需硬體）

遊戲 loop 與 Flask Server 分別在不同執行緒執行。
"""

import argparse
import sys
import time
import threading

# ── 啟動 Flask Server（子執行緒）────────────────────────
def start_flask():
    from app import app, socketio, init_db
    init_db()
    socketio.run(app, host="0.0.0.0", port=5000, debug=False, use_reloader=False)


# ── 遊戲主迴圈 ───────────────────────────────────────────
TARGET_FPS = 30
FRAME_TIME = 1.0 / TARGET_FPS

def game_loop(demo: bool):
    hw = None

    if not demo:
        try:
            from hardware import HardwareManager
            hw = HardwareManager()
        except Exception as e:
            print(f"[WARN] 硬體初始化失敗，切換為 Demo 模式：{e}")
            demo = True

    from game import ThunderFighter, State
    game = ThunderFighter(hw=hw)

    # 等待開始
    if hw:
        hw.lcd_message("Press BTN Start", "Thunder Fighter")
    else:
        print("[DEMO] 按 Enter 開始遊戲…")
        input()

    game.start()
    game.periodic_push(interval=1.0)

    print(f"[INFO] 遊戲開始（{'Demo' if demo else 'Hardware'} 模式）")

    try:
        while True:
            t0 = time.time()

            # Demo 模式：用鍵盤模擬輸入（非阻塞，依平台而定）
            # 硬體模式：game.update() 內部讀取 GPIO
            game.update(dt=FRAME_TIME)

            if game.state == State.GAMEOVER:
                print(f"[INFO] 遊戲結束！分數：{game.score}  關卡：{game.level}")
                time.sleep(3)
                # 重新開始
                game.start()

            elapsed = time.time() - t0
            sleep   = FRAME_TIME - elapsed
            if sleep > 0:
                time.sleep(sleep)

    except KeyboardInterrupt:
        print("\n[INFO] 使用者中止遊戲。")
    finally:
        if hw:
            hw.cleanup()


# ── 入口 ────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="雷霆戰機")
    parser.add_argument("--demo", action="store_true",
                        help="Demo 模式（不需硬體）")
    args = parser.parse_args()

    # Flask Server（背景）
    flask_thread = threading.Thread(target=start_flask, daemon=True)
    flask_thread.start()
    time.sleep(1)   # 等 Flask 啟動

    print("=" * 40)
    print("  雷霆戰機 — Thunder Fighter")
    print("  Flask:  http://localhost:5000")
    print("=" * 40)

    game_loop(demo=args.demo)


if __name__ == "__main__":
    main()
