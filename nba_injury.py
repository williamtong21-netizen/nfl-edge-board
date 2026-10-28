"""The NBA's official injury report: teams must file it, and the league posts a new PDF every 15 minutes on game days
(official.nba.com). It is the first place a late scratch or a Questionable -> Out call shows up, usually 30-90 minutes
before ESPN's injury list catches up.
  latest()      {"at": "10/28/26 03:00 PM", "url": ..., "players": {norm name: {n, s, why, tm, game}}}  (None if unavailable)
Needs pdfplumber (pip install pdfplumber); without it this returns None and the board falls back to ESPN only.
Run:  python nba_injury.py      (prints the latest report)
"""
import io, json, re, sys
from urllib.request import urlopen, Request

PAGE = "https://official.nba.com/nba-injury-report-{}-{}-season/"
UA = {"User-Agent": "nfl-edge-board"}
STATUS = ("Out", "Doubtful", "Questionable", "Probable", "Available")
ROW = re.compile(r"^(.+?,\S+)\s+(Out|Doubtful|Questionable|Probable|Available)\b\s*(.*)$")
NICK = ("Hawks", "Celtics", "Nets", "Hornets", "Bulls", "Cavaliers", "Mavericks", "Nuggets", "Pistons", "Warriors", "Rockets", "Pacers",
        "Clippers", "Lakers", "Grizzlies", "Heat", "Bucks", "Timberwolves", "Pelicans", "Knicks", "Thunder", "Magic", "76ers", "Suns",
        "TrailBlazers", "Kings", "Spurs", "Raptors", "Jazz", "Wizards")
GAME = re.compile(r"\b([A-Z]{2,3}@[A-Z]{2,3})\b")
SUFFIX = re.compile(r"(jr|sr|iii|ii|iv)$")


def fix(raw):
    """'Udeh,Jr.,Ernest' (some suffixes get their own comma) -> 'UdehJr.,Ernest'."""
    return re.sub(r",(Jr\.?|Sr\.?|II|III|IV),", r"\1,", raw)


def norm(name):
    """'Porter Jr., Michael' / 'PorterJr.,Michael' / 'Michael Porter Jr.' -> 'michaelporter'."""
    s = re.sub(r"(?<=[a-z])(Jr\.?|Sr\.?|III|II|IV)(?=[\s,.]|$)", r" \1", str(name or ""))   # the PDF glues suffixes on
    if "," in s:
        last, first = s.split(",", 1); s = first + " " + last
    toks = [t for t in (re.sub(r"[^a-z]", "", t) for t in s.lower().split()) if t]
    while len(toks) > 2 and SUFFIX.fullmatch(toks[-1]): toks.pop()
    return "".join(toks)


def fetch(url):
    with urlopen(Request(url, headers=UA), timeout=25) as r: return r.read()


def parse(pdf_bytes):
    import pdfplumber
    rows, game, team = {}, None, None
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        text = "\n".join(p.extract_text() or "" for p in pdf.pages)
    at = (re.search(r"Injury Report:\s*([\d/]+\s+[\d:]+\s*[AP]M)", text) or [None, None])[1]
    for line in text.splitlines():
        m = GAME.search(line)
        if m: game = m.group(1); line = line[m.end():].strip()
        head = line.split(" ", 1)
        if len(head) == 2 and "," not in head[0] and head[0].endswith(NICK): team, line = head   # 'CharlotteHornets' (spaces dropped)
        r = ROW.search(line)
        if not r: continue
        raw = fix(r.group(1)); last, first = raw.split(",", 1)
        n = f"{first} {last}".strip()
        n = re.sub(r"(?<=[a-z])(Jr\.|Sr\.|III|II|IV)$", r" \1", n)
        rows[norm(raw)] = {"n": n, "s": r.group(2), "why": re.sub(r"(?<=[a-z])(?=[A-Z])", " ", re.sub(r"^Injury/Illness-", "", r.group(3))).strip()[:80], "tm": team, "game": game}
    return at, rows


def latest():
    try:
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc) - timedelta(hours=5); y = now.year if now.month >= 8 else now.year - 1   # US Eastern, near enough
        page = fetch(PAGE.format(y, str(y + 1)[-2:])).decode("utf-8", "replace")
        links = re.findall(r"https://[^\"']*Injury-Report_[\d_-]+[AP]M\.pdf", page)
        if not links: return None
        url = sorted(set(links), key=lambda u: re.sub(r".*Injury-Report_(\d{4}-\d\d-\d\d)_(\d\d)_(\d\d)([AP]M).*",
                                                        lambda m: m[1] + ("1" if m[4] == "PM" else "0") + str(int(m[2]) % 12).zfill(2) + m[3], u))[-1]
        if now.strftime("%Y-%m-%d") not in url: return None   # today's first report isn't up yet: yesterday's would be stale
        at, rows = parse(fetch(url))
        return {"at": at, "url": url, "players": rows}
    except ImportError:
        print("official injury report skipped: pip install pdfplumber", file=sys.stderr); return None
    except Exception as e:
        print("official injury report failed", e, file=sys.stderr); return None


if __name__ == "__main__":
    r = latest()
    if not r: sys.exit("no report")
    print(r["at"], r["url"], len(r["players"]), "players")
    for k, v in list(r["players"].items())[:25]: print(k, json.dumps(v))
