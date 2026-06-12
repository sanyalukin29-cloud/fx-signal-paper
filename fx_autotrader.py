#!/usr/bin/env python3
"""
FX Pairs AUTO TRADER — EURJPY/GBPJPY
ส่งคำสั่งจริงผ่าน MT5 Python API (Exness)

ติดตั้ง (Windows เท่านั้น สำหรับ live):
  py -3.11 -m pip install MetaTrader5 yfinance statsmodels httpx

เงื่อนไข live:
  - MT5 ต้องเปิดและ login Exness ไว้ + เปิด Allow automated trading
  - รันบน Windows เท่านั้น (MetaTrader5 lib เป็น Windows-only)

โครงสร้างราคา:
  - สัญญาณ (z-score) ใช้ yfinance  → ตรงกับ backtest 10 ปี
  - ส่งออเดอร์จริง ใช้ Exness ผ่าน MT5
  - basis-check: ก่อนเข้าไม้ เทียบราคา Exness กับ yfinance ถ้าต่างเกิน BASIS_MAX_PCT = ไม่เทรด

PAPER_MODE = True : รันได้ทุกที่ ไม่ต้องมี MT5 (ใช้ราคา yfinance จำลอง fill, ข้าม basis-check)
"""

import os, json, sys, csv
from datetime import datetime
from pathlib import Path

import numpy as np
import statsmodels.api as sm
import httpx
import yfinance as yf

try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False

# ══════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════
BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID", "")
STATE_FILE = Path(__file__).parent / "state.json"
PAPER_LOG  = Path(__file__).parent / "paper_trades.csv"

SYMBOL_A      = "EURJPYm"   # ชื่อใน Exness MT5 (เช็คด้วย check-symbol ก่อน)
SYMBOL_B      = "GBPJPYm"
LOT_A         = 0.30
LOT_B         = 0.24
LOOKBACK      = 60
ENTRY_Z       = 2.0
EXIT_Z        = 1.0
STOP_Z        = 3.0
MAX_DAYS      = 20
RATIO_STOP    = 0.83    # ถ้า EURJPY/GBPJPY ต่ำกว่านี้ = regime เปลี่ยน → exit
BASIS_MAX_PCT = 0.30    # yfinance vs Exness ต่างเกิน % นี้ = skip ไม้ (กัน feed หลุด)
PAPER_MODE    = True    # True = จำลอง (ข้าม basis-check) / False = live ส่ง MT5

# ══════════════════════════════════════
#  TELEGRAM
# ══════════════════════════════════════
def telegram(text: str) -> bool:
    try:
        r = httpx.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        return r.status_code == 200
    except Exception:
        return False

# ══════════════════════════════════════
#  MT5 HELPERS (live only)
# ══════════════════════════════════════
def mt5_connect() -> bool:
    if not MT5_AVAILABLE:
        telegram("❌ MetaTrader5 library ไม่ได้ติดตั้ง (Windows-only)")
        return False
    if not mt5.initialize():
        telegram(f"❌ MT5 เชื่อมไม่ได้: {mt5.last_error()}")
        return False
    info = mt5.account_info()
    if info is None:
        telegram("❌ MT5 ยังไม่ได้ login")
        return False
    print(f"  MT5: {info.login} | Balance: ${info.balance:.2f} | Server: {info.server}")
    return True

def check_symbol(symbol: str) -> str:
    for s in [symbol, symbol.replace("m", ""), symbol + "m", symbol + ".r"]:
        info = mt5.symbol_info(s)
        if info and info.visible:
            return s
    raise ValueError(f"Symbol {symbol} ไม่พบใน broker")

def get_exness_mid(symbol: str):
    """ราคากลาง Exness ปัจจุบัน (live เท่านั้น). คืน None ถ้าดึงไม่ได้/paper."""
    if PAPER_MODE or not MT5_AVAILABLE:
        return None
    try:
        sym  = check_symbol(symbol)
        tick = mt5.symbol_info_tick(sym)
        if tick is None:
            return None
        return (tick.bid + tick.ask) / 2.0
    except Exception:
        return None

def basis_check(sig: dict):
    """
    เทียบราคา yfinance (สัญญาณ) กับ Exness (เทรดจริง).
    คืน (ok: bool, detail: str). paper/no-mt5 = ผ่านอัตโนมัติ.
    """
    if PAPER_MODE or not MT5_AVAILABLE:
        return True, "paper/no-mt5 — ข้าม basis-check"
    ex_a = get_exness_mid(SYMBOL_A)
    ex_b = get_exness_mid(SYMBOL_B)
    if ex_a is None or ex_b is None:
        return False, "ดึงราคา Exness ไม่ได้ (symbol/feed?)"
    da = abs(ex_a - sig["eurjpy"]) / sig["eurjpy"] * 100
    db = abs(ex_b - sig["gbpjpy"]) / sig["gbpjpy"] * 100
    detail = (f"EURJPY yf={sig['eurjpy']} ex={ex_a:.3f} Δ{da:.2f}% | "
              f"GBPJPY yf={sig['gbpjpy']} ex={ex_b:.3f} Δ{db:.2f}% (เกณฑ์ {BASIS_MAX_PCT}%)")
    return (da <= BASIS_MAX_PCT and db <= BASIS_MAX_PCT), detail

def send_order(symbol: str, lot: float, order_type: str, comment: str, paper_px: float) -> dict:
    if PAPER_MODE:
        print(f"  [PAPER] {order_type} {lot} {symbol} @ {paper_px}")
        return {"ticket": 0, "price": paper_px, "paper": True}
    sym = check_symbol(symbol)
    tick = mt5.symbol_info_tick(sym)
    price = tick.ask if order_type == "BUY" else tick.bid
    mt5_type = mt5.ORDER_TYPE_BUY if order_type == "BUY" else mt5.ORDER_TYPE_SELL
    request = {
        "action": mt5.TRADE_ACTION_DEAL, "symbol": sym, "volume": lot,
        "type": mt5_type, "price": price, "deviation": 20, "magic": 20260613,
        "comment": comment[:31], "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.symbol_info(sym).filling_mode,
    }
    result = mt5.order_send(request)
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        return {"error": f"retcode {result.retcode}: {result.comment}"}
    return {"ticket": result.order, "price": result.price}

def close_position(ticket: int, symbol: str, lot: float, open_type: str, paper_px: float) -> dict:
    if PAPER_MODE:
        print(f"  [PAPER] CLOSE {lot} {symbol} @ {paper_px}")
        return {"ticket": 0, "price": paper_px, "paper": True}
    sym = check_symbol(symbol)
    tick = mt5.symbol_info_tick(sym)
    close_type = mt5.ORDER_TYPE_SELL if open_type == "BUY" else mt5.ORDER_TYPE_BUY
    price = tick.bid if open_type == "BUY" else tick.ask
    request = {
        "action": mt5.TRADE_ACTION_DEAL, "symbol": sym, "volume": lot,
        "type": close_type, "position": ticket, "price": price,
        "deviation": 20, "magic": 20260613, "comment": "pairs_exit",
        "type_filling": mt5.symbol_info(sym).filling_mode,
    }
    result = mt5.order_send(request)
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        return {"error": f"retcode {result.retcode}: {result.comment}"}
    return {"ticket": result.order, "price": result.price}

def log_paper(row: dict):
    new = not PAPER_LOG.exists()
    with open(PAPER_LOG, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["ts","event","direction","z","eurjpy","gbpjpy","hedge","held","pnl_pct"])
        if new: w.writeheader()
        w.writerow(row)

# ══════════════════════════════════════
#  PRICE (yfinance daily) — สัญญาณ
# ══════════════════════════════════════
def get_daily_data():
    df = yf.download(["EURJPY=X", "GBPJPY=X"], period="120d", interval="1d",
                     progress=False, auto_adjust=True)["Close"]
    df.columns = ["EURJPY", "GBPJPY"]
    return df.dropna().tail(120)

def calc_zscore(df):
    seg = df.tail(LOOKBACK)
    X = sm.add_constant(seg["GBPJPY"])
    h = sm.OLS(seg["EURJPY"], X).fit().params.iloc[1]
    sp = df["EURJPY"] - h * df["GBPJPY"]
    m, s = sp.tail(LOOKBACK).mean(), sp.tail(LOOKBACK).std()
    z = (sp.iloc[-1] - m) / s if s > 0 else 0
    corr = seg["EURJPY"].corr(seg["GBPJPY"])
    return {"z": round(float(z),3), "hedge": round(float(h),3), "corr": round(float(corr),3),
            "eurjpy": round(float(df["EURJPY"].iloc[-1]),3), "gbpjpy": round(float(df["GBPJPY"].iloc[-1]),3),
            "ratio": round(float(df["EURJPY"].iloc[-1]/df["GBPJPY"].iloc[-1]),3),
            "date": df.index[-1].strftime("%Y-%m-%d")}

# ══════════════════════════════════════
#  STATE
# ══════════════════════════════════════
def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"position": "NONE"}

def save_state(s):
    STATE_FILE.write_text(json.dumps(s, indent=2, ensure_ascii=False))

def days_held(state):
    if state.get("entry_date"):
        d = datetime.strptime(state["entry_date"], "%Y-%m-%d")
        return (datetime.now() - d).days
    return 0

# ══════════════════════════════════════
#  MAIN
# ══════════════════════════════════════
def main():
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    mode = "📄 PAPER" if PAPER_MODE else "💰 LIVE"
    print(f"[{now}] FX AutoTrader — Mode: {mode}")

    try:
        df = get_daily_data()
        sig = calc_zscore(df)
    except Exception as e:
        telegram(f"❌ ดึงข้อมูลไม่ได้: {e}")
        sys.exit(1)
    print(f"  Z: {sig['z']:+.3f} | Corr: {sig['corr']:.3f} | Ratio: {sig['ratio']:.3f} | "
          f"EURJPY: {sig['eurjpy']} | GBPJPY: {sig['gbpjpy']}")

    if not PAPER_MODE:
        if not mt5_connect():
            sys.exit(1)

    state = load_state()
    pos = state.get("position", "NONE")
    held = days_held(state)
    px_a, px_b = sig["eurjpy"], sig["gbpjpy"]   # ราคา fill (paper). live ใช้ Exness ใน send/close

    # ── EXIT (ออกได้เสมอ ไม่ติด basis แต่เตือนถ้า basis เพี้ยน) ──
    if pos != "NONE":
        reason = None
        if abs(sig["z"]) < EXIT_Z:        reason = "z-score กลับเข้า"
        elif abs(sig["z"]) > STOP_Z:      reason = "🛑 Stop Loss"
        elif held >= MAX_DAYS:            reason = f"⏰ ครบ {MAX_DAYS} วัน"
        elif sig["ratio"] < RATIO_STOP:   reason = "⚠️ Regime เปลี่ยน (ratio)"
        if reason:
            print(f"  → EXIT: {reason}")
            b_ok, b_detail = basis_check(sig)
            errors = []
            r_a = close_position(state.get("ticket_a",0), SYMBOL_A, LOT_A, "BUY" if pos=="LONG" else "SELL", px_a)
            if "error" in r_a: errors.append(f"EURJPY: {r_a['error']}")
            r_b = close_position(state.get("ticket_b",0), SYMBOL_B, LOT_B, "SELL" if pos=="LONG" else "BUY", px_b)
            if "error" in r_b: errors.append(f"GBPJPY: {r_b['error']}")
            h = state.get("hedge", sig["hedge"])
            sp_entry = state.get("entry_eurjpy", sig["eurjpy"]) - h*state.get("entry_gbpjpy", sig["gbpjpy"])
            sp_now = sig["eurjpy"] - h*sig["gbpjpy"]
            pnl_pct = (sp_now - sp_entry)/abs(state.get("entry_eurjpy", sig["eurjpy"]))*100
            if pos == "SHORT": pnl_pct = -pnl_pct
            emoji = "✅" if pnl_pct > 0 else "❌"
            warn = "" if b_ok else f"\n⚠️ basis เพี้ยนตอนปิด: {b_detail}"
            telegram(f"🏁 <b>FX PAIRS — EXIT {emoji}</b>\n━━━━━━━━━━━━━━━━━━━━\n"
                     f"เหตุผล: {reason}\nถือ: {held} วัน | Mode: {mode}\n\n"
                     f"Z entry: <code>{state.get('entry_z',0):+.2f}</code> → Z now: <code>{sig['z']:+.2f}</code>\n"
                     f"PnL ≈ <b>{pnl_pct:+.2f}%</b>\n\n"
                     + (f"⚠️ Error: {', '.join(errors)}" if errors else "✅ ออเดอร์ปิดสำเร็จ") + warn)
            if PAPER_MODE:
                log_paper({"ts":now,"event":"EXIT","direction":pos,"z":sig["z"],"eurjpy":px_a,
                           "gbpjpy":px_b,"hedge":h,"held":held,"pnl_pct":round(pnl_pct,3)})
            save_state({"position":"NONE"})
            if not PAPER_MODE and MT5_AVAILABLE: mt5.shutdown()
            return

    # ── ENTRY (มี basis-check gate) ──
    if pos == "NONE":
        direction = "SHORT" if sig["z"] > ENTRY_Z else ("LONG" if sig["z"] < -ENTRY_Z else None)
        if direction:
            b_ok, b_detail = basis_check(sig)
            if not b_ok:
                print(f"  → ENTRY SKIP (basis): {b_detail}")
                telegram(f"⚠️ <b>FX ENTRY SKIP</b> — basis เพี้ยน\n"
                         f"{b_detail}\nไม่เทรดไม้นี้ (กัน 2 feed หลุดกัน) | Mode: {mode}")
                if not PAPER_MODE and MT5_AVAILABLE: mt5.shutdown()
                return
            print(f"  → ENTRY {direction}  (basis: {b_detail})")
            errors = []
            if direction == "LONG":
                r_a = send_order(SYMBOL_A, LOT_A, "BUY",  "pairs_long_A",  px_a)
                r_b = send_order(SYMBOL_B, LOT_B, "SELL", "pairs_long_B",  px_b)
            else:
                r_a = send_order(SYMBOL_A, LOT_A, "SELL", "pairs_short_A", px_a)
                r_b = send_order(SYMBOL_B, LOT_B, "BUY",  "pairs_short_B", px_b)
            if "error" in r_a: errors.append(f"EURJPY: {r_a['error']}")
            if "error" in r_b: errors.append(f"GBPJPY: {r_b['error']}")
            arrow = "🟢" if direction == "LONG" else "🔴"
            action_a = "BUY" if direction == "LONG" else "SELL"
            action_b = "SELL" if direction == "LONG" else "BUY"
            telegram(f"📡 <b>FX PAIRS — ENTRY</b>\n━━━━━━━━━━━━━━━━━━━━\n"
                     f"{arrow} <b>{direction} SPREAD</b>\nMode: {mode}\n\n"
                     f"EURJPY: <code>{sig['eurjpy']}</code>  ({action_a} {LOT_A} lot)\n"
                     f"GBPJPY: <code>{sig['gbpjpy']}</code>  ({action_b} {LOT_B} lot)\n\n"
                     f"Z-Score: <b>{sig['z']:+.2f}</b>\nHedge: <code>{sig['hedge']:.3f}</code>  Corr: <code>{sig['corr']:.3f}</code>\n\n"
                     f"🎯 Exit: |z| < {EXIT_Z}  🛑 Stop: |z| > {STOP_Z}  ⏰ Max: {MAX_DAYS}d\n\n"
                     + (f"⚠️ Error: {', '.join(errors)}" if errors else
                        f"✅ ออเดอร์เปิดสำเร็จ (tickets: A={r_a.get('ticket','?')} B={r_b.get('ticket','?')})"))
            if not errors:
                save_state({"position":direction,"entry_date":sig["date"],"entry_z":sig["z"],
                            "entry_eurjpy":sig["eurjpy"],"entry_gbpjpy":sig["gbpjpy"],"hedge":sig["hedge"],
                            "ticket_a":r_a.get("ticket",0),"ticket_b":r_b.get("ticket",0)})
                if PAPER_MODE:
                    log_paper({"ts":now,"event":"ENTRY","direction":direction,"z":sig["z"],"eurjpy":px_a,
                               "gbpjpy":px_b,"hedge":sig["hedge"],"held":0,"pnl_pct":0})
            if not PAPER_MODE and MT5_AVAILABLE: mt5.shutdown()
            return

    # ── NO SIGNAL ──
    telegram(f"📊 <b>FX Daily — {sig['date']}</b>\n"
             f"Z: <b>{sig['z']:+.2f}</b>  Corr: <code>{sig['corr']:.3f}</code>  Ratio: <code>{sig['ratio']:.3f}</code>\n"
             f"EURJPY: <code>{sig['eurjpy']}</code>  GBPJPY: <code>{sig['gbpjpy']}</code>\n"
             f"Position: <b>{pos}</b>" + (f" ({held} วัน)" if pos!="NONE" else "") +
             f"\n⏳ รอ |z| > {ENTRY_Z}  | Mode: {mode}")
    if not PAPER_MODE and MT5_AVAILABLE: mt5.shutdown()
    print("  → Daily summary sent")

if __name__ == "__main__":
    main()
