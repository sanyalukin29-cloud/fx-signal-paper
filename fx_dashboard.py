#!/usr/bin/env python3
"""
FX Dashboard Generator — EURJPY/GBPJPY
อ่าน paper_trades.csv + state.json + คำนวณ z-score history (yfinance)
แล้ว bake เป็นไฟล์เดียว fx_dashboard.html (เปิดบนมือถือได้ ไม่ต้องรัน server)

รัน:  py -3.11 fx_dashboard.py     (วางไว้โฟลเดอร์เดียวกับ fx_autotrader.py)
"""
import json, csv, html
from pathlib import Path
from datetime import datetime
import statsmodels.api as sm
import yfinance as yf

HERE       = Path(__file__).parent
STATE_FILE = HERE / "state.json"
PAPER_LOG  = HERE / "paper_trades.csv"
OUT        = HERE / "fx_dashboard.html"

LOOKBACK, ENTRY_Z, EXIT_Z, STOP_Z = 60, 2.0, 1.0, 3.0
RATIO_STOP, HEDGE_OK_LO = 0.83, 0.78

def zscore_history(days=180):
    df = yf.download(["EURJPY=X","GBPJPY=X"], period=f"{days}d", interval="1d",
                     progress=False, auto_adjust=True)["Close"]
    df.columns = ["EURJPY","GBPJPY"]; df = df.dropna()
    rows=[]
    for i in range(LOOKBACK, len(df)):
        seg = df.iloc[i-LOOKBACK:i+1]
        X = sm.add_constant(seg["GBPJPY"])
        h = sm.OLS(seg["EURJPY"], X).fit().params.iloc[1]
        sp = seg["EURJPY"] - h*seg["GBPJPY"]
        m,s = sp.mean(), sp.std()
        z = (sp.iloc[-1]-m)/s if s>0 else 0
        rows.append({"date": df.index[i].strftime("%Y-%m-%d"),
                     "z": round(float(z),3),
                     "eurjpy": round(float(df["EURJPY"].iloc[i]),3),
                     "gbpjpy": round(float(df["GBPJPY"].iloc[i]),3),
                     "hedge": round(float(h),3),
                     "corr": round(float(seg["EURJPY"].corr(seg["GBPJPY"])),3),
                     "ratio": round(float(df["EURJPY"].iloc[i]/df["GBPJPY"].iloc[i]),3)})
    return rows

def read_trades():
    if not PAPER_LOG.exists(): return []
    with open(PAPER_LOG, encoding="utf-8") as f:
        return list(csv.DictReader(f))

def read_state():
    if STATE_FILE.exists(): return json.loads(STATE_FILE.read_text())
    return {"position":"NONE"}

def build():
    hist = zscore_history()
    trades = read_trades()
    state = read_state()
    cur = hist[-1] if hist else {"z":0,"corr":0,"ratio":0,"hedge":0,"eurjpy":0,"gbpjpy":0,"date":"-"}

    # equity curve จาก EXIT pnl
    eq=[]; cum=0.0
    for t in trades:
        if t.get("event")=="EXIT":
            try: cum += float(t.get("pnl_pct") or 0)
            except: pass
            eq.append({"date":t.get("ts","")[:10], "cum": round(cum,3)})
    n_exit=len(eq)
    wins=sum(1 for t in trades if t.get("event")=="EXIT" and float(t.get("pnl_pct") or 0)>0)
    wr = round(wins/n_exit*100,1) if n_exit else 0

    # regime flags
    warn=[]
    if cur["ratio"]<RATIO_STOP: warn.append(f"ratio {cur['ratio']} &lt; {RATIO_STOP} (regime stop)")
    if cur["hedge"]<HEDGE_OK_LO: warn.append(f"hedge {cur['hedge']} &lt; {HEDGE_OK_LO} (โซนไม่ ideal)")
    if cur["corr"]<0.90: warn.append(f"corr {cur['corr']} &lt; 0.90")

    pos = state.get("position","NONE")
    pos_color = {"LONG":"#34d399","SHORT":"#f87171","NONE":"#9ca3af"}.get(pos,"#9ca3af")

    data = {"hist":hist,"eq":eq,"trades":trades,"cur":cur,"pos":pos,
            "wr":wr,"n_exit":n_exit,
            "entry":ENTRY_Z,"exit":EXIT_Z,"stop":STOP_Z,
            "gen": datetime.now().strftime("%Y-%m-%d %H:%M")}

    warn_html = ("".join(f"<span class='chip warn'>⚠️ {w}</span>" for w in warn)
                 or "<span class='chip ok'>✅ regime ปกติ</span>")

    rows_html=""
    for t in reversed(trades[-40:]):
        ev=t.get("event",""); col = "#34d399" if ev=="ENTRY" else "#fbbf24"
        pnl=t.get("pnl_pct","")
        try: pnlf=float(pnl); pnl_disp=f"{pnlf:+.2f}%"; pcol="#34d399" if pnlf>0 else ("#f87171" if pnlf<0 else "#9ca3af")
        except: pnl_disp="-"; pcol="#9ca3af"
        rows_html+=(f"<tr><td>{html.escape(t.get('ts',''))}</td>"
                    f"<td style='color:{col}'>{html.escape(ev)}</td>"
                    f"<td>{html.escape(t.get('direction',''))}</td>"
                    f"<td>{html.escape(t.get('z',''))}</td>"
                    f"<td>{html.escape(t.get('held',''))}</td>"
                    f"<td style='color:{pcol}'>{pnl_disp}</td></tr>")
    if not rows_html:
        rows_html="<tr><td colspan='6' style='text-align:center;color:#6b7280'>ยังไม่มีไม้ — รัน fx_autotrader.py (paper) เก็บสถิติก่อน</td></tr>"

    tpl = """<!doctype html><html lang="th"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>FX Pairs — EURJPY/GBPJPY</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
:root{--bg:#0b0d12;--card:#151821;--line:#252a36;--gold:#d4af37;--mut:#9ca3af;--txt:#e5e7eb}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);
font-family:-apple-system,Segoe UI,Roboto,sans-serif;padding:14px;max-width:860px;margin:auto}
h1{font-size:18px;margin:4px 0 2px}.sub{color:var(--mut);font-size:12px;margin-bottom:14px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;margin-bottom:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:12px}
.card .k{color:var(--mut);font-size:11px;text-transform:uppercase;letter-spacing:.5px}
.card .v{font-size:22px;font-weight:700;margin-top:4px}
.gold{color:var(--gold)}
.chip{display:inline-block;font-size:12px;padding:4px 10px;border-radius:999px;margin:2px 4px 2px 0}
.chip.warn{background:#3b2410;color:#fbbf24;border:1px solid #5a3a16}
.chip.ok{background:#10241a;color:#34d399;border:1px solid #1c4030}
.panel{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px;margin-bottom:14px}
.panel h2{font-size:13px;color:var(--mut);margin:0 0 10px;text-transform:uppercase;letter-spacing:.5px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:7px 6px;border-bottom:1px solid var(--line);text-align:left}
th{color:var(--mut);font-weight:500;font-size:11px;text-transform:uppercase}
.foot{color:#4b5563;font-size:11px;text-align:center;margin-top:10px}
</style></head><body>
<h1>FX Pairs — <span class="gold">EURJPY / GBPJPY</span></h1>
<div class="sub">Daily swing · z-score mean reversion · อัปเดต __GEN__</div>

<div style="margin-bottom:14px">__WARN__</div>

<div class="grid">
  <div class="card"><div class="k">Z-Score</div><div class="v gold">__Z__</div></div>
  <div class="card"><div class="k">Position</div><div class="v" style="color:__POSCOL__">__POS__</div></div>
  <div class="card"><div class="k">Corr 60d</div><div class="v">__CORR__</div></div>
  <div class="card"><div class="k">Ratio</div><div class="v">__RATIO__</div></div>
  <div class="card"><div class="k">Hedge β</div><div class="v">__HEDGE__</div></div>
  <div class="card"><div class="k">Win / Trades</div><div class="v">__WR__% <span style="font-size:12px;color:var(--mut)">(__NEXIT__)</span></div></div>
</div>

<div class="panel"><h2>Z-Score 120 วัน (เส้น: entry ±__ENTRY__ · exit ±__EXIT__ · stop ±__STOP__)</h2>
<canvas id="zc" height="150"></canvas></div>

<div class="panel"><h2>Equity Curve (paper, สะสม %)</h2>
<canvas id="eqc" height="120"></canvas></div>

<div class="panel"><h2>Trade Log (ล่าสุด 40)</h2>
<table><thead><tr><th>เวลา</th><th>Event</th><th>Dir</th><th>Z</th><th>วัน</th><th>PnL</th></tr></thead>
<tbody>__ROWS__</tbody></table></div>

<div class="foot">EURJPY __EURJPY__ · GBPJPY __GBPJPY__ · regenerate: py -3.11 fx_dashboard.py</div>

<script>
const D=__DATA__;
const labels=D.hist.map(r=>r.date), zs=D.hist.map(r=>r.z);
const line=(v,c)=>({label:'',data:labels.map(()=>v),borderColor:c,borderWidth:1,borderDash:[5,4],pointRadius:0,fill:false});
new Chart(document.getElementById('zc'),{type:'line',
 data:{labels,datasets:[
   {label:'z',data:zs,borderColor:'#d4af37',borderWidth:2,pointRadius:0,tension:.2},
   line(D.entry,'#f87171'),line(-D.entry,'#f87171'),
   line(D.exit,'#34d399'),line(-D.exit,'#34d399'),
   line(D.stop,'#7c3aed'),line(-D.stop,'#7c3aed')]},
 options:{plugins:{legend:{display:false}},scales:{
   x:{ticks:{color:'#6b7280',maxTicksLimit:6},grid:{color:'#1b1f29'}},
   y:{ticks:{color:'#6b7280'},grid:{color:'#1b1f29'}}}}});
const eqL=D.eq.map(r=>r.date), eqV=D.eq.map(r=>r.cum);
new Chart(document.getElementById('eqc'),{type:'line',
 data:{labels:eqL,datasets:[{label:'cum%',data:eqV,borderColor:'#34d399',
   backgroundColor:'rgba(52,211,153,.12)',borderWidth:2,pointRadius:3,fill:true,tension:.2}]},
 options:{plugins:{legend:{display:false}},scales:{
   x:{ticks:{color:'#6b7280',maxTicksLimit:6},grid:{color:'#1b1f29'}},
   y:{ticks:{color:'#6b7280'},grid:{color:'#1b1f29'}}}}});
</script></body></html>"""

    out = (tpl.replace("__GEN__",data["gen"]).replace("__WARN__",warn_html)
        .replace("__Z__",f"{cur['z']:+.2f}").replace("__POS__",pos).replace("__POSCOL__",pos_color)
        .replace("__CORR__",f"{cur['corr']:.3f}").replace("__RATIO__",f"{cur['ratio']:.3f}")
        .replace("__HEDGE__",f"{cur['hedge']:.3f}").replace("__WR__",str(wr)).replace("__NEXIT__",str(n_exit))
        .replace("__ENTRY__",str(ENTRY_Z)).replace("__EXIT__",str(EXIT_Z)).replace("__STOP__",str(STOP_Z))
        .replace("__ROWS__",rows_html).replace("__EURJPY__",str(cur["eurjpy"])).replace("__GBPJPY__",str(cur["gbpjpy"]))
        .replace("__DATA__",json.dumps(data,ensure_ascii=False)))
    OUT.write_text(out, encoding="utf-8")
    print(f"  wrote {OUT.name}  | z-hist {len(hist)} วัน | trades {len(trades)} | exits {n_exit}")

if __name__=="__main__":
    build()
