"""
116 學年度碩士班甄試名單監看：中山資管、中央資管
- 從各校教務處／招生組／資管系的公告頁出發，找出「116 + 甄試/碩士 + 面試/複試/名單/榜單/錄取」的公告
- 新公告 → 寄信通知
- 打開公告（網頁、PDF）搜尋應考證號碼 → 找到就寄信通知
狀態存在 state.json（由 workflow commit 回 repo），避免重複通知。
"""
import io
import json
import os
import re
import smtplib
import sys
import time
from email.mime.text import MIMEText
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

try:
    from pypdf import PdfReader
except ImportError:  # pragma: no cover
    PdfReader = None

STATE_FILE = "state.json" if os.environ.get("TARGET_YEAR", "116") == "116" else "state_sim.json"
DEBUG = False
UA = {"User-Agent": "Mozilla/5.0 (admission-watch; personal use)"}
TIMEOUT = 25

SCHOOLS = {
    "中山": {
        "id_env": "NSYSU_ID",
        "domains": ["nsysu.edu.tw"],
        "seeds": [
            "https://oaa.nsysu.edu.tw/p/403-1003-867-1.php?Lang=zh-tw",
            "https://oaa.nsysu.edu.tw/p/403-1003-878.php?Lang=zh-tw",
            "https://oaa.nsysu.edu.tw/",
            "https://exam-oaa.nsysu.edu.tw/p/412-1065-2038.php?Lang=zh-tw",
            "https://exam-acad.nsysu.edu.tw/exam_netlist/netlist_main.php?ExmNo=11",
            "https://exam-acad.nsysu.edu.tw/",
            "https://www.mis.nsysu.edu.tw/",
            "https://exam-acad.nsysu.edu.tw/exam_netlist/chk_dpt_prt.php?str=TVRVeE1URXhOall3TURnME1qQmhZMkU9",
        ],
    },
    "中央": {
        "id_env": "NCU_ID",
        "domains": ["ncu.edu.tw"],
        "seeds": [
            "https://im.mgt.ncu.edu.tw/enrollment",
            "https://im.mgt.ncu.edu.tw/enrollment/MS/MSselect",
            "https://im.mgt.ncu.edu.tw/",
            "https://admission.ncu.edu.tw/p/412-1024-2359.php",
            "https://admission.ncu.edu.tw/",
        ],
    },
}

# 公告標題要符合的條件
RESULT_WORDS = re.compile(r"(面試|複試|口試|名單|榜單|放榜|錄取|初審|甄試結果|成績)")
EXAM_WORDS = re.compile(r"(甄試|碩士|推甄)")
YEAR = os.environ.get("TARGET_YEAR", "116")
# 值得點進去繼續找的「列表頁」連結
NAV_WORDS = re.compile(r"(最新消息|公告|招生|甄試|碩士班|入學|news|admission)", re.I)


def log(*a):
    print(*a, flush=True)


def fetch(session, url):
    try:
        r = session.get(url, headers=UA, timeout=TIMEOUT, allow_redirects=True)
        if r.status_code != 200:
            log(f"  [{r.status_code}] {url}")
            return None
        return r
    except Exception as e:  # noqa: BLE001
        log(f"  [ERR] {url} -> {type(e).__name__}: {e}")
        return None


def in_domain(url, domains):
    host = urlparse(url).hostname or ""
    return any(host == d or host.endswith("." + d) for d in domains)


def page_links(resp):
    ctype = resp.headers.get("Content-Type", "")
    if "html" not in ctype and not resp.text.lstrip().startswith("<"):
        return []
    resp.encoding = resp.apparent_encoding or resp.encoding
    soup = BeautifulSoup(resp.text, "html.parser")
    out = []
    for a in soup.find_all("a", href=True):
        text = " ".join(a.get_text(" ", strip=True).split())
        title = a.get("title") or ""
        href = urljoin(resp.url, a["href"])
        if href.startswith("http"):
            out.append((f"{text} {title}".strip(), href))
    return out


def is_result_notice(text):
    if not RESULT_WORDS.search(text):
        return False
    has_year = YEAR in text
    if not (EXAM_WORDS.search(text) or has_year):
        return False
    # 標題寫了別的學年度（例如 115 學年度）就不是這次的
    other = [y for y in re.findall(r"(1[01]\d)\s*學年", text) if y != YEAR]
    if other and not has_year:
        return False
    # 考試入學（筆試）、在職專班、博士班、轉學等不相干的排除
    if re.search(r"(考試入學|入學考試|在職|博士|轉學|學士|學分班|EMBA)", text) and "甄試" not in text:
        return False
    if re.search(r"(在職|博士|轉學|學士班|學分班|EMBA|抵免|聲明書|切結書|工讀|招募|簡章|報名人數|專班|特殊選才|學士|海納百川)", text):
        return False
    return has_year or "甄試" in text


def is_mis(text):
    return "資管" in text or "資訊管理" in text


def doc_text(resp):
    ctype = resp.headers.get("Content-Type", "").lower()
    url = resp.url.lower()
    if "pdf" in ctype or url.endswith(".pdf"):
        if PdfReader is None:
            return ""
        try:
            reader = PdfReader(io.BytesIO(resp.content))
            return "\n".join((p.extract_text() or "") for p in reader.pages)
        except Exception as e:  # noqa: BLE001
            log(f"  PDF 讀取失敗 {resp.url}: {e}")
            return ""
    resp.encoding = resp.apparent_encoding or resp.encoding
    return BeautifulSoup(resp.text, "html.parser").get_text(" ", strip=True)


def id_in(text, sid):
    if not sid:
        return False
    norm = re.sub(r"\s+", "", text).upper()
    return sid.upper() in norm


def crawl_school(session, name, cfg, sid):
    """回傳 (notices: dict url->title, hits: list of (title,url))."""
    notices = {}
    hits = []
    seen = set()
    queue = [(u, 0) for u in cfg["seeds"]]
    max_pages = 40
    ok_seeds = 0
    while queue and len(seen) < max_pages:
        url, depth = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        resp = fetch(session, url)
        if resp is None:
            continue
        if depth == 0:
            ok_seeds += 1
        for text, href in page_links(resp):
            if not in_domain(href, cfg["domains"]):
                continue
            if DEBUG and EXAM_WORDS.search(text):
                log(f"    debug d{depth} {resp.url[:60]} -> {text[:70]} | {href}")
            if is_result_notice(text):
                notices.setdefault(href, text[:120])
            elif depth < 1 and NAV_WORDS.search(text) and href not in seen:
                queue.append((href, depth + 1))
    log(f"[{name}] 可連線起始頁 {ok_seeds}/{len(cfg['seeds'])}，掃描 {len(seen)} 頁，找到候選公告 {len(notices)} 則")

    # 打開候選公告（以及公告裡的附件）搜尋應考證號
    for url, title in list(notices.items())[:25]:
        hits.extend(check_notice(session, url, title, sid))
    return notices, hits


def check_notice(session, url, title, sid, show=False):
    hits = []
    resp = fetch(session, url)
    if resp is None:
        return hits
    docs = [(title, resp)]
    for text, href in page_links(resp):
        if re.search(r"\.(pdf)(\?|$)", href, re.I) or is_result_notice(text) or "附件" in text or "下載" in text:
            r2 = fetch(session, href)
            if r2 is not None:
                docs.append((f"{title} / {text[:60]}", r2))
    for t, r in docs:
        txt = doc_text(r)
        if show:
            log(f"--- {r.url} ({len(txt)} 字)\n{txt[:1500]}")
        if id_in(txt, sid):
            hits.append((t, r.url))
    return hits


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"notices": {}, "hits": [], "initialized": False}


def save_state(st):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1, sort_keys=True)


def send_mail(subject, body):
    user = os.environ.get("GMAIL_USER")
    pw = os.environ.get("GMAIL_APP_PASSWORD")
    to = os.environ.get("MAIL_TO") or user
    if not user or not pw:
        log("（未設定 GMAIL_USER / GMAIL_APP_PASSWORD，略過寄信）")
        log(subject)
        log(body)
        return
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(user, pw.replace(" ", ""))
        s.sendmail(user, [a.strip() for a in to.split(",")], msg.as_string())
    log(f"已寄信到 {to}：{subject}")


def main():
    global DEBUG
    DEBUG = "--debug" in sys.argv
    selftest = os.environ.get("SELFTEST_URL")
    if selftest:
        sid = os.environ.get("SELFTEST_ID", "")
        hits = check_notice(requests.Session(), selftest, "自我測試名單", sid, show=not sid)
        log(f"自我測試：號碼 {sid or '(未提供)'} 找到 {len(hits)} 處")
        if sid:
            body = ("這是完整流程測試（拿中央資管去年的名單當範例）。\n\n" +
                    ("\n".join(f"✅ 在「{t}」找到測試號碼\n   {u}" for t, u in hits) if hits else "❌ 沒找到測試號碼"))
            send_mail("🧪 名單比對測試：" + ("成功" if hits else "失敗"), body)
        return
    dump = os.environ.get("DUMP_URL")
    if dump:
        r = fetch(requests.Session(), dump)
        if r is not None:
            for t, h in page_links(r):
                log(f"LINK {t[:90]} | {h}")
        return
    test_mail = "--test-mail" in sys.argv
    st = load_state()
    session = requests.Session()
    new_notices = []
    new_hits = []
    for name, cfg in SCHOOLS.items():
        sid = (os.environ.get(cfg["id_env"]) or "").strip()
        notices, hits = crawl_school(session, name, cfg, sid)
        for url, title in notices.items():
            key = f"{name}|{url}"
            if key not in st["notices"]:
                st["notices"][key] = title
                new_notices.append((name, title, url))
        for title, url in hits:
            key = f"{name}|{url}"
            if key not in st["hits"]:
                st["hits"].append(key)
                new_hits.append((name, title, url))

    first_run = not st.get("initialized")
    st["initialized"] = True
    st["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()) + " UTC"
    save_state(st)

    # 第一次執行只建立基準（舊公告不通知），但找到應考證號一定通知
    lines = []
    if new_hits:
        for name, title, url in new_hits:
            lines.append(f"✅ 你的{name}應考證號出現在：{title}\n   {url}")
    notify_notices = [] if first_run else new_notices
    mis_first = sorted(notify_notices, key=lambda x: not is_mis(x[1]))
    hit_schools = {k.split("|", 1)[0] for k in st["hits"]}
    for name, title, url in mis_first:
        tag = "【資管】" if is_mis(title) else ""
        note = ("（名單裡已找到你的應考證號，見上方 ✅）" if name in hit_schools
                else "（程式在這則公告與附件裡沒找到你的應考證號；若名單是圖片檔可能比對不到，請點進去再確認）")
        lines.append(f"📢 {name}新公告{tag}：{title}\n   {url}\n   {note}")

    if first_run:
        log(f"第一次執行：記錄現有 {len(new_notices)} 則公告為基準，不寄通知。")
        for name, title, url in new_notices:
            log(f"  - {name} | {title} | {url}")

    if lines:
        if new_hits:
            subject = "✅ 甄試名單有你！" + "、".join(sorted({n for n, _, _ in new_hits}))
        else:
            subject = "📢 甄試新公告：" + "、".join(sorted({n for n, _, _ in notify_notices}))
        body = "\n\n".join(lines) + "\n\n（請點連結自行再確認一次。名單若是掃描圖檔，程式可能比對不到號碼。）"
        send_mail(subject, body)
    elif test_mail:
        send_mail("🧪 甄試監看測試信", "這是測試信，收到代表寄信設定成功。\n"
                  f"目前記錄的公告數：{len(st['notices'])}")
    else:
        log("沒有新消息。")


if __name__ == "__main__":
    main()
