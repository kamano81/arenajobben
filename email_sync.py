#!/usr/bin/env python3
"""
Arenajobben — Yahoo Mail Sync
Läser Tarantsec-mail från Yahoo och exporterar till shifts.json
Pushar automatiskt shifts.json till GitHub efter varje synk.
Kör: python3 email_sync.py
"""

import imaplib
import email
import json
import re
import os
import random
import string
import base64
import urllib.request
import urllib.error
from datetime import datetime
from email.header import decode_header

# ── Paths ──────────────────────────────────────────────────────────────
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, 'email_config.json')
STATE_FILE  = os.path.join(BASE_DIR, 'sync_state.json')
OUTPUT_FILE = os.path.join(BASE_DIR, 'shifts.json')
PDF_FOLDER  = os.path.join(BASE_DIR, 'pdfs')

# ── Yahoo IMAP ─────────────────────────────────────────────────────────
YAHOO_IMAP       = 'imap.mail.yahoo.com'
YAHOO_PORT       = 993
SENDER_DOMAIN    = 'tarantsec.se'
TARGET_FOLDER    = 'Tarantsec'   # mappen du skapade i Yahoo

# ── Helpers ────────────────────────────────────────────────────────────
def uid():
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=12))

MONTHS_SV = {
    'januari':1,'februari':2,'mars':3,'april':4,'maj':5,'juni':6,
    'juli':7,'augusti':8,'september':9,'oktober':10,'november':11,'december':12
}
def parse_swedish_date(text):
    """'28 mars 2026' → '2026-03-28'"""
    m = re.search(
        r'(\d{1,2})\s+(januari|februari|mars|april|maj|juni|juli|augusti|'
        r'september|oktober|november|december)\s+(\d{4})',
        text, re.IGNORECASE
    )
    if not m:
        return ''
    return f"{m.group(3)}-{MONTHS_SV[m.group(2).lower()]:02d}-{int(m.group(1)):02d}"

MONTHS_EN = {
    'january':1,'february':2,'march':3,'april':4,'may':5,'june':6,
    'july':7,'august':8,'september':9,'october':10,'november':11,'december':12
}
def parse_english_date(text):
    """'28 March, 2026' → '2026-03-28'"""
    m = re.search(
        r'(\d{1,2})\s+(January|February|March|April|May|June|July|August|'
        r'September|October|November|December),?\s+(\d{4})',
        text, re.IGNORECASE
    )
    if not m:
        return ''
    return f"{m.group(3)}-{MONTHS_EN[m.group(2).lower()]:02d}-{int(m.group(1)):02d}"

def parse_time(t):
    """HH:MM eller HH:MM:SS → HH:MM  (ogiltiga timmar ≥ 24 ger '')"""
    m = re.match(r'(\d{1,2}):(\d{2})(?::\d{2})?', t.strip())
    if not m:
        return ''
    h = int(m.group(1))
    if h >= 24:
        return ''
    return f"{h:02d}:{m.group(2)}"

# ── Config ─────────────────────────────────────────────────────────────
def load_config():
    if not os.path.exists(CONFIG_FILE):
        print(f"⚠️  Ingen konfigfil hittad. Skapar mall: {CONFIG_FILE}")
        print("   Fyll i din e-post och ditt Yahoo App-lösenord och kör igen.")
        exit(1)
    with open(CONFIG_FILE) as f:
        cfg = json.load(f)
    if 'your_email' in cfg.get('email', '') or 'your-16' in cfg.get('app_password', ''):
        print("⚠️  Fyll i email och app_password i email_config.json och kör igen.")
        exit(1)
    return cfg

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"processed_ids": [], "first_run_done": False}

def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)

def load_existing_shifts_from_github(config):
    """Hämtar aktuell shifts.json från GitHub — användarens manuella ändringar bevaras."""
    token = config.get('github_token', '')
    user  = config.get('github_user', '')
    if not token or not user:
        return None   # Inget GitHub-konto konfigurerat
    url = f'https://api.github.com/repos/{user}/arenajobben/contents/shifts.json'
    headers = {'Authorization': f'token {token}', 'Accept': 'application/vnd.github.v3+json'}
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req) as r:
            data = json.loads(r.read())
            shifts = json.loads(base64.b64decode(data['content']).decode())
            print(f"☁️  Hämtade {len(shifts)} pass från GitHub som bas")
            return shifts
    except Exception as e:
        print(f"⚠️  Kunde inte hämta shifts.json från GitHub: {e}")
        return None

def load_existing_shifts():
    if os.path.exists(OUTPUT_FILE):
        with open(OUTPUT_FILE) as f:
            return json.load(f)
    return []

def save_shifts(shifts):
    with open(OUTPUT_FILE, 'w') as f:
        json.dump(shifts, f, indent=2, ensure_ascii=False)
    print(f"💾 Sparade {len(shifts)} pass till {OUTPUT_FILE}")

def load_tombstones_from_github(config):
    """Hämtar tombstones.json från GitHub — pass som användaren tagit bort ska inte läggas tillbaka."""
    token = config.get('github_token', '')
    user  = config.get('github_user', '')
    if not token or not user:
        return set()
    url = f'https://api.github.com/repos/{user}/arenajobben/contents/tombstones.json'
    headers = {'Authorization': f'token {token}', 'Accept': 'application/vnd.github.v3+json'}
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req) as r:
            data = json.loads(r.read())
            keys = json.loads(base64.b64decode(data['content']).decode())
            return set(keys)
    except Exception:
        return set()

def tombstone_key(shift):
    return f"{shift.get('date','')}|{shift.get('event','').lower().strip()}"

def push_file_to_github(config, local_path, repo_path, commit_msg):
    """Generisk hjälpfunktion: pushar en lokal fil till GitHub-repot."""
    token = config.get('github_token', '')
    user  = config.get('github_user', '')
    if not token or not user:
        return
    repo    = 'arenajobben'
    api_url = f'https://api.github.com/repos/{user}/{repo}/contents/{repo_path}'
    headers = {
        'Authorization': f'token {token}',
        'Accept': 'application/vnd.github.v3+json',
        'Content-Type': 'application/json',
    }
    try:
        req  = urllib.request.Request(api_url, headers=headers)
        sha  = json.loads(urllib.request.urlopen(req).read()).get('sha', '')
    except urllib.error.HTTPError:
        sha = ''
    with open(local_path, 'rb') as f:
        content_b64 = base64.b64encode(f.read()).decode()
    payload = json.dumps({'message': commit_msg, 'content': content_b64, 'sha': sha}).encode()
    try:
        req = urllib.request.Request(api_url, data=payload, headers=headers, method='PUT')
        urllib.request.urlopen(req)
        print(f"☁️  {repo_path} pushad till GitHub ({user}/{repo})")
    except Exception as e:
        print(f"⚠️  Kunde inte pusha {repo_path} till GitHub: {e}")


def push_to_github(config):
    """Pushar shifts.json till GitHub så att webb-appen uppdateras."""
    push_file_to_github(
        config, OUTPUT_FILE, 'shifts.json',
        f'Synk {datetime.now().strftime("%Y-%m-%d %H:%M")}'
    )

# ── Email parsing ──────────────────────────────────────────────────────
def get_body(msg):
    """Extraherar och rensar e-postbodyn till ren text."""
    body = ''
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == 'text/plain':
                raw = part.get_payload(decode=True)
                charset = part.get_content_charset() or 'utf-8'
                if raw:
                    body += raw.decode(charset, errors='replace')
            elif ct == 'text/html' and not body:
                # Bara HTML om vi inte redan har plain text
                raw = part.get_payload(decode=True)
                charset = part.get_content_charset() or 'utf-8'
                if raw:
                    body += raw.decode(charset, errors='replace')
    else:
        raw = msg.get_payload(decode=True)
        charset = msg.get_content_charset() or 'utf-8'
        if raw:
            body = raw.decode(charset, errors='replace')

    # Ta bort <style> och <script>-block HELT (innehåll + tagg)
    body = re.sub(r'<style[^>]*>.*?</style>', '', body, flags=re.DOTALL | re.IGNORECASE)
    body = re.sub(r'<script[^>]*>.*?</script>', '', body, flags=re.DOTALL | re.IGNORECASE)

    # Normalisera HTML → text
    body = re.sub(r'<br\s*/?>', '\n', body, flags=re.IGNORECASE)
    body = re.sub(r'</(?:p|div|tr|li)>', '\n', body, flags=re.IGNORECASE)
    body = re.sub(r'<[^>]+>', ' ', body)
    for ent, ch in [('&nbsp;', ' '), ('&lt;', '<'), ('&gt;', '>'),
                    ('&amp;', '&'), ('&quot;', '"'), ('&#39;', "'")]:
        body = body.replace(ent, ch)
    body = re.sub(r'\r\n|\r', '\n', body)
    body = re.sub(r'[ \t]{2,}', ' ', body)
    body = re.sub(r'\n{3,}', '\n\n', body)
    return body.strip()

def decode_subject(msg):
    parts = decode_header(msg.get('Subject', ''))
    result = ''
    for part, enc in parts:
        if isinstance(part, bytes):
            result += part.decode(enc or 'utf-8', errors='replace')
        else:
            result += part
    return result

# ── Ämnesrader att skippa helt ─────────────────────────────────────────
SKIP_SUBJECT_PATTERNS = re.compile(
    r'^(sv:|re:|fw:|fwd:|återställning|registrering mottagen|välkommen på intervju|'
    r'rekryteringsdag|profilfoto|personalentrén:|inför ditt pass|inför ert arbetspass|'
    r'mer information om|uppdaterade tider|dubbelpass|byta pass|byte av pass|'
    r'avbokat pass|avbokning|tyvärr sjuk|vinterkonferensen|arenagenomgång|'
    r'varmt välkommen till tarantsec|tarantsec registrering|tarantsec\.se registrering|'
    r'inget arbetstilfälle|tidigare start)',
    re.IGNORECASE
)

# Skräpord som INTE är evenemangsnamn
GARBAGE_EVENT = re.compile(
    r'^(hej|vänliga hälsningar|tack|super|jättekul|med vänlig|mvh|ok|okej|'
    r'ja|nej|tyvärr|självklart|absolut|givetvis|precis|noted)',
    re.IGNORECASE
)

def extract_shift(body, subject):
    """
    Tolkar ett Tarantsec-mail och returnerar ett pass-dict eller None.

    Stödda format:
      NY:  "EventNamn [TAG] YYYY-MM-DD på Arena"  (i bodyn)
      NY:  "Information om EventNamn YYYY-MM-DD på Arena"
      GAMMAL: Ämne = "Arbetserbjudande från Tarantsec: EventNamn [TAG]"
      GAMMAL: Ämne = "Arbetsbekräftelse  EventNamn [TAG] Arena"
      GAMMAL: Ämne = "Reservlistan  EventNamn [TAG] Arena"
    """

    # ── Skippa irrelevanta mail direkt ──
    if SKIP_SUBJECT_PATTERNS.match(subject.strip()):
        return None

    # ── Status ──
    is_confirmed = bool(
        re.search(r'du jobbar!', body, re.IGNORECASE) or
        re.search(r'arbetsbekräftelse', subject, re.IGNORECASE) or
        re.match(r'^du jobbar på\b', subject.strip(), re.IGNORECASE)
    )
    is_reserve = bool(
        re.match(r'^reservlistan\b', subject.strip(), re.IGNORECASE) or
        re.match(r'^du har blivit flyttad till reservlistan\b', subject.strip(), re.IGNORECASE)
    )
    status = 'confirmed' if is_confirmed else ('reserve' if is_reserve else 'pending')

    raw_name = ''
    date_str = ''
    venue    = ''

    # ── Format 1 (nytt): body innehåller "EventNamn [TAG] YYYY-MM-DD på Arena" ──
    used_body_format = False
    event_line_re = re.compile(
        r'(.{3,80}?)\s+(\d{4}-\d{2}-\d{2})\s+på\s+([^\n]{3,50}?)(?:\s*\n|$)',
        re.MULTILINE
    )
    m = event_line_re.search(body)
    if m:
        raw_name = m.group(1).strip()
        date_str = m.group(2).strip()
        venue    = m.group(3).strip().split('\n')[0].strip()
        used_body_format = True

        # Rensa bort boilerplate-prefix (var som helst i strängen, inte bara i början)
        for prefix in [
            r'Hej!\s+Här\s+kommer\s+ett\s+arbetserbjudande\s+för\s+',
            r'Här\s+kommer\s+ett\s+arbetserbjudande\s+för\s+',
            r'ett\s+arbetserbjudande\s+för\s+',
            r'Information\s+om\s+',
            r'Välkommen\s+att\s+jobba\s+på\s+',
            r'Du\s+är\s+nu\s+på\s+reservlistan\s+för\s+',
            r'Du\s+har\s+fått\s+arbetspasset\s+för\s+',
            r'^för\s+',
        ]:
            raw_name = re.sub(prefix, '', raw_name, flags=re.IGNORECASE).strip()

        # Rensa trailing "den" som ibland hänger kvar
        raw_name = re.sub(r'\s+den\s*$', '', raw_name, flags=re.IGNORECASE).strip()

        # Sanitetskoll på venue — Tarantsec skickar ibland evenemang-info som venue
        # Ogiltigt om venue innehåller ISO-datum, taggar, eller känd e-posttext
        if re.search(r'\d{4}-\d{2}-\d{2}|\[PREMIUM\]|\[SERVICE\]|mejl att|arbetspasset', venue, re.IGNORECASE):
            venue = ''

    # ── Format 2b (nytt): ämne "Information om EventNamn [TAG] YYYY-MM-DD på Arena" ──
    # Dessa mejl skickas när tider uppdateras eller som bekräftelse-info.
    if not raw_name or not date_str:
        used_body_format = False
        info_m = re.match(
            r'Information\s+om\s+(.+?)\s+(\d{4}-\d{2}-\d{2})\s+på\s+(.+)',
            subject.strip(), re.IGNORECASE
        )
        if info_m:
            raw_name = info_m.group(1).strip()
            date_str = info_m.group(2)
            venue    = info_m.group(3).strip()

    # ── Format 2 (gammalt): ämnesbaserat ──
    if not raw_name or not date_str:
        used_body_format = False
        subj_m = re.match(
            r'(?:Arbetserbjudande från Tarantsec:\s*|'
            r'Arbetsbekräftelse\s+|'
            r'Reservlistan\s+|'
            r'Du jobbar på\s+|'
            r'Du har blivit flyttad till reservlistan på\s+)'
            r'(.+)',
            subject, re.IGNORECASE
        )
        if subj_m:
            raw_name = subj_m.group(1).strip()
            # Datum i bodyn — ISO → svenska → engelska
            dm = re.search(r'(\d{4}-\d{2}-\d{2})', body)
            if dm:
                date_str = dm.group(1)
            else:
                date_str = parse_swedish_date(body) or parse_english_date(body)
            # Arena i bodyn
            vm = re.search(r'(?:på|at)\s+([A-ZÅÄÖ\w][^\n,]{3,40}?)(?=\s*\n)', body, re.MULTILINE)
            if vm:
                venue = vm.group(1).strip()
        else:
            return None  # okänt format

    if not date_str:
        return None

    # ── Sanitetskoll på venue (gäller alla format) ───────────────────────
    # Ogiltigt om venue innehåller ISO-datum, taggar, eller känd e-posttext
    if re.search(r'\d{4}-\d{2}-\d{2}|\[PREMIUM\]|\[SERVICE\]|\[PREMUM\]|mejl att|arbetspasset',
                 venue, re.IGNORECASE):
        venue = ''

    # ── Extrahera [TAG] och rensa evenemangsnamn ──
    source_for_tag = raw_name or subject
    tag_m = re.search(r'\[([^\]]+)\]', source_for_tag)
    tag = tag_m.group(1) if tag_m else ''
    if not used_body_format and tag_m:
        # Gammalt ämnesformat: "EventNamn [TAG] Arenans Namn" — arenans namn
        # hamnar efter taggen, ta bara det som är FÖRE taggen
        event_name = raw_name[:tag_m.start()].strip()
    else:
        # Nytt bodyformat: taggen kan sitta mitt i namnet, t.ex. "MAX [LIVE] - KVÄLL"
        event_name = re.sub(r'\s*\[.*?\]\s*', ' ', raw_name).strip()
    event_name = re.sub(r'\s+', ' ', event_name).strip()
    premium = 'PREMIUM' if re.search(r'premium', tag, re.IGNORECASE) else ''

    # ── Sanitetskoll — filtrera skräp ──
    if not event_name or len(event_name) < 3:
        return None
    if re.search(r'[{}@]|!important|media only|text-align|font-size', event_name):
        return None   # CSS läckte igenom
    if GARBAGE_EVENT.match(event_name):
        return None   # e-postsvar, hälsningsfraser

    # ── Tider ──
    start_time = ''
    end_time   = ''

    # "Preliminära tider: HH:MM" ELLER "Bekräftade tider: HH:MM"
    # "Preliminära tider (cirkatider) HH:MM:SS"
    # OBS: [^0-9]* används (ej [^:]*) för att inte svälja timciffren
    prelim_m = re.search(
        r'(?:Preliminära|Bekräftade)\s+tider[^0-9]*(\d{1,2}:\d{2}(?::\d{2})?)\s*[-–]\s*(\d{1,2}:\d{2}(?::\d{2})?)',
        body, re.IGNORECASE
    )
    if prelim_m:
        s_raw = parse_time(prelim_m.group(1))
        e_raw = parse_time(prelim_m.group(2))
        # Skippa 00:00-00:00 som är Tarantsecs platshållare för "okänd tid"
        if not (s_raw == '00:00' and e_raw == '00:00'):
            start_time = s_raw
            end_time   = e_raw
    else:
        pairs = re.findall(
            r'(\d{1,2}:\d{2}(?::\d{2})?)\s*[-–]\s*(\d{1,2}:\d{2}(?::\d{2})?)',
            body
        )
        if pairs:
            starts = [parse_time(s) for s, _ in pairs if parse_time(s)]
            ends   = [parse_time(e) for _, e in pairs if parse_time(e)]
            if starts: start_time = min(starts)
            if ends:   end_time   = max(ends)

    return {
        'id':     uid(),
        'event':  event_name,
        'venue':  venue,
        'date':   date_str,
        'start':  start_time,
        'end':    end_time,
        'rate':   0,
        'status': status,
        'tag':    premium,
        'source': 'email',
    }

# ── Duplicate / update logic ───────────────────────────────────────────
def normalize_event(name):
    """Normalisera för jämförelse: lowercase + alla separatorer (–//_) → ' - '"""
    name = name.lower().strip()
    name = re.sub(r'\s*[–—/\\|_]\s*', ' - ', name)
    return re.sub(r'\s+', ' ', name).strip()

def find_existing(shift, existing, strict_tag=False):
    """Returnerar index om passet redan finns (samma datum + evenemang), annars -1.
    strict_tag=True: PREMIUM och SERVICE räknas som separata roller (används för email-import).
    strict_tag=False: ignorerar tag-skillnader (används för PDF-import och dedup)."""
    new_key      = normalize_event(shift.get('event', ''))
    new_premium  = (shift.get('tag', '') == 'PREMIUM')
    for i, s in enumerate(existing):
        if s.get('date') != shift['date']:
            continue
        if normalize_event(s.get('event', '')) != new_key:
            continue
        if strict_tag:
            ex_premium = (s.get('tag', '') == 'PREMIUM')
            if new_premium != ex_premium:
                continue   # Olika roller → separata pass
        return i
    return -1

# ── PDF parsing ────────────────────────────────────────────────────────
def parse_pdf_shift(pdf_path):
    """
    Tolkar ett Tarantsec-anställningsavtal (PDF).
    Källprioritering:
      - datum, tider, arena, lön  → ur PDF-texten (strukturerade fält)
      - evenemangsnamn, tag        → ur filnamnet (PDF-texten kan ha felrendat namn)
    Status:
      - 'worked'    om digitalt utcheckning finns i avtalet
      - 'confirmed' annars
    """
    filename = os.path.basename(pdf_path)

    # ── Läs PDF-text ──────────────────────────────────────────────────
    text = ''
    try:
        import pdfplumber
        with pdfplumber.open(pdf_path) as pdf:
            text = '\n'.join(page.extract_text() or '' for page in pdf.pages)
    except Exception as e:
        print(f"  ⚠️  PDF-text gick inte att läsa ({filename}): {e}")

    # ── Datum och tider ur PDF-fälten ─────────────────────────────────
    date_str   = ''
    start_time = ''
    end_time   = ''
    venue      = ''
    rate       = 0

    if text:
        # Schemalagda tider (fallback om faktiska saknas)
        sm = re.search(
            r'Anst[äa]llningstid,\s*start\s+(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2})',
            text, re.IGNORECASE
        )
        if sm:
            date_str   = sm.group(1)
            start_time = sm.group(2)

        em = re.search(
            r'Anst[äa]llningstid,\s*slut\s+\d{4}-\d{2}-\d{2}\s+(\d{1,2}:\d{2})',
            text, re.IGNORECASE
        )
        if em:
            end_time = em.group(1)

        # Faktisk sluttid från utcheckning — starttiden är alltid schemalagd
        # "2026-04-19 16:44  Arbetstagare digitalt utcheckad"
        checkout_m = re.search(
            r'\d{4}-\d{2}-\d{2}\s+(\d{1,2}:\d{2})\s+Arbetstagare digitalt utcheckad',
            text, re.IGNORECASE
        )
        if checkout_m:
            end_time = checkout_m.group(1)

        # "Arbetsplats   Strawberry Arena"
        vm = re.search(r'Arbetsplats\s+(.+)', text)
        if vm:
            venue = vm.group(1).strip()

        # "Lön, SEK/timme, brutto  150"
        rm = re.search(r'L[öo]n,\s*SEK/timme,\s*brutto\s+(\d+)', text, re.IGNORECASE)
        if rm:
            rate = int(rm.group(1))

    # ── Evenemangsnamn + tag ur filnamnet ─────────────────────────────
    # PDF-texten renderar ibland "AIK ? Kalmar FF" — filnamnet är tillförlitligare
    event_name = ''
    tag        = ''
    fn_date_m  = re.search(r'(\d{4}-\d{2}-\d{2})(?:\s*\(\d+\))?\.pdf$', filename, re.IGNORECASE)
    prefix_m   = re.match(r'.+?-[^-]+-', filename)   # "Anstä...avtal-Namn-"
    if fn_date_m and prefix_m:
        chunk = filename[prefix_m.end() : fn_date_m.start()].rstrip('-').strip()
        tag_m = re.search(r'\[([^\]]+)\]', chunk)
        tag   = tag_m.group(1) if tag_m else ''
        raw   = re.sub(r'\s*\[[^\]]+\]\s*', ' ', chunk).strip()
        raw   = re.sub(r'\s+_\s+', ' / ', raw)
        raw   = re.sub(r'\s+\d+[/_]\d+$', '', raw)   # strip trailing "10_1" / "10/1" date artifacts
        event_name = re.sub(r'\s+', ' ', raw).strip()

    # Fallback: datum ur filnamnet om PDF saknade det
    if not date_str and fn_date_m:
        date_str = fn_date_m.group(1)

    if not date_str or not event_name:
        return None

    premium = 'PREMIUM' if 'premium' in tag.lower() else ''

    # ── Status ────────────────────────────────────────────────────────
    # Utcheckning i avtalet → passet är redan utfört
    status = 'worked' if text and re.search(r'utcheckad', text, re.IGNORECASE) else 'confirmed'

    return {
        'id':     uid(),
        'event':  event_name,
        'venue':  venue,
        'date':   date_str,
        'start':  start_time,
        'end':    end_time,
        'rate':   rate,
        'status': status,
        'tag':    premium,
        'source': 'pdf',
    }


def scan_pdfs(existing, tombstones, processed):
    """Skannar ~/arenajobben/pdfs/ och lägger till nya bekräftade pass."""
    if not os.path.isdir(PDF_FOLDER):
        return 0

    STATUS_RANK = {'worked': 3, 'confirmed': 2, 'reserve': 1, 'pending': 0}
    added = 0
    for fname in sorted(os.listdir(PDF_FOLDER)):
        if not fname.lower().endswith('.pdf'):
            continue

        full_path = os.path.join(PDF_FOLDER, fname)
        shift = parse_pdf_shift(full_path)

        if not shift:
            print(f'  ❓ Kunde inte tolka PDF: {fname}')
            continue
        if tombstone_key(shift) in tombstones:
            print(f'  🪦 SKIPPAD (tombstone): {shift["event"]} {shift["date"]}')
            continue

        idx = find_existing(shift, existing)
        if idx == -1:
            existing.append(shift)
            added += 1
            print(f'  📄 PDF NYTT: {shift["event"]} @ {shift["venue"]} {shift["date"]} {shift["start"]}–{shift["end"]}')
        else:
            changed = False
            ex = existing[idx]
            # PDF är alltid auktoritativ för tider, venue och lön — skriv alltid över
            if shift['start'] and ex.get('start') != shift['start']:
                ex['start'] = shift['start']
                ex['end']   = shift['end']
                changed = True
            if shift['venue'] and ex.get('venue') != shift['venue']:
                ex['venue'] = shift['venue']
                changed = True
            if shift['rate'] and ex.get('rate') != shift['rate']:
                ex['rate'] = shift['rate']
                changed = True
            # Status uppgraderas men nedgraderas inte manuellt
            if STATUS_RANK.get(shift['status'], 0) > STATUS_RANK.get(ex.get('status'), 0):
                ex['status'] = shift['status']
                changed = True
            if changed:
                print(f'  📄 PDF UPPDATERAD: {shift["event"]} {shift["date"]} {shift["start"]}–{shift["end"]}')
            else:
                print(f'  ⏭  PDF OK: {shift["event"]} {shift["date"]}')

    return added


# ── Main sync ──────────────────────────────────────────────────────────
def sync():
    config   = load_config()
    state    = load_state()
    # Använd GitHub som källan — inte lokal kopia — så att manuella ändringar bevaras
    existing = load_existing_shifts_from_github(config)
    if existing is None:
        existing = load_existing_shifts()   # Fallback om GitHub inte går att nå
    processed = set(state.get('processed_ids', []))

    # Hämta tombstones och rensa bort redan borttagna pass
    tombstones = load_tombstones_from_github(config)
    if tombstones:
        before   = len(existing)
        existing = [s for s in existing if tombstone_key(s) not in tombstones]
        removed  = before - len(existing)
        if removed:
            print(f"🪦  Tog bort {removed} tombstone-pass ur listan")

    print(f"\n🔌 Ansluter till Yahoo Mail ({YAHOO_IMAP})…")
    mail = imaplib.IMAP4_SSL(YAHOO_IMAP, YAHOO_PORT)
    mail.login(config['email'], config['app_password'])
    print(f"✅ Inloggad som {config['email']}\n")

    added   = 0
    updated = 0

    # Sök alltid i båda mapparna — scriptet flyttar INBOX-mail till Tarantsec-mappen
    folders = [TARGET_FOLDER, 'INBOX']
    inbox_to_move = []  # mail-IDs i INBOX som ska flyttas efter bearbetning

    for folder in folders:
        try:
            # INBOX behöver skrivrättighet för att kunna flytta mail
            readonly = (folder != 'INBOX')
            status, _ = mail.select(f'"{folder}"', readonly=readonly)
            if status != 'OK':
                print(f"⚠️  Kunde inte öppna mappen: {folder}")
                continue
        except Exception as e:
            print(f"⚠️  Mappfel ({folder}): {e}")
            continue

        _, msg_ids = mail.uid('search', None, f'FROM "{SENDER_DOMAIN}"')
        ids = msg_ids[0].split()
        print(f"📬 {len(ids)} mail från {SENDER_DOMAIN} i mappen '{folder}'")

        for uid in ids:
            key = f"{folder}:{uid.decode()}"
            if key in processed:
                if folder == 'INBOX':
                    inbox_to_move.append(uid)
                continue

            _, data = mail.uid('fetch', uid, '(RFC822)')
            raw_msg = data[0][1]
            msg     = email.message_from_bytes(raw_msg)
            subject = decode_subject(msg)
            body    = get_body(msg)
            shift   = extract_shift(body, subject)

            if shift:
                idx = find_existing(shift, existing, strict_tag=True)
                if idx == -1:
                    if tombstone_key(shift) in tombstones:
                        print(f"  🪦 SKIPPAD (tombstone): {shift['event']} {shift['date']}")
                    else:
                        existing.append(shift)
                        added += 1
                        print(f"  ➕ NYTT ({shift['status']}): {shift['event']} @ {shift['venue']} {shift['date']}")
                else:
                    # Statusrangordning: worked > confirmed > reserve > pending
                    # E-post uppgraderar ALDRIG status nedåt (reserve skriver ej över confirmed)
                    EMAIL_RANK = {'worked': 3, 'confirmed': 2, 'reserve': 1, 'pending': 0}
                    new_rank = EMAIL_RANK.get(shift['status'], 0)
                    old_rank = EMAIL_RANK.get(existing[idx].get('status'), 0)
                    changed = False
                    if new_rank > old_rank:
                        existing[idx]['status'] = shift['status']
                        if shift['start']:
                            existing[idx]['start'] = shift['start']
                        if shift['end']:
                            existing[idx]['end'] = shift['end']
                        updated += 1
                        status_sv = {'confirmed': 'bekräftad', 'reserve': 'reserv', 'pending': 'erbjudande', 'worked': 'jobbat'}
                        print(f"  🔄 UPPDATERAD → {status_sv.get(shift['status'], shift['status'])}: {shift['event']} {shift['date']}")
                        changed = True
                    # Uppdatera tider: lägg till om de saknas, eller skriv över om de ändrats
                    # (bekräftade tider från "Information om"-mejl kan ersätta preliminära)
                    if shift.get('start'):
                        old_start = existing[idx].get('start', '')
                        old_end   = existing[idx].get('end', '')
                        if not old_start:
                            existing[idx]['start'] = shift['start']
                            existing[idx]['end']   = shift['end']
                            updated += 1
                            print(f"  🕐 TIDER TILLAGDA: {shift['event']} {shift['date']} {shift['start']}–{shift['end']}")
                            changed = True
                        elif shift['start'] != old_start or shift['end'] != old_end:
                            existing[idx]['start'] = shift['start']
                            existing[idx]['end']   = shift['end']
                            updated += 1
                            print(f"  🕐 TIDER UPPDATERADE: {shift['event']} {shift['date']} {old_start}–{old_end} → {shift['start']}–{shift['end']}")
                            changed = True
                    if not changed:
                        print(f"  ⏭  Finns redan: {shift.get('event','?')} {shift.get('date','?')}")
            else:
                print(f"  ❓ Kunde inte tolka mail: {subject[:60]}")

            processed.add(key)
            if folder == 'INBOX':
                inbox_to_move.append(uid)

    # ── Flytta alla INBOX-mail till Tarantsec-mappen (via UID) ────────
    if inbox_to_move:
        try:
            mail.select('"INBOX"', readonly=False)
            moved = 0
            for uid in inbox_to_move:
                res = mail.uid('copy', uid, TARGET_FOLDER)
                if res[0] == 'OK':
                    mail.uid('store', uid, '+FLAGS', '\\Deleted')
                    moved += 1
            mail.expunge()
            print(f"\n📁 Flyttade {moved} mail från Inkorgen → {TARGET_FOLDER}")
        except Exception as e:
            print(f"⚠️  Kunde inte flytta mail: {e}")

    mail.logout()

    # ── Scanna PDF-mappen ──────────────────────────────────────────────
    pdf_count = scan_pdfs(existing, tombstones, processed)
    if pdf_count:
        print(f"\n📄 Lade till {pdf_count} pass från PDF-avtal")

    save_shifts(existing)
    push_to_github(config)
    state['processed_ids'] = list(processed)
    state['first_run_done'] = True
    save_state(state)
    push_file_to_github(
        config, STATE_FILE, 'sync_state.json',
        f'Synk-tillstånd {datetime.now().strftime("%Y-%m-%d %H:%M")}'
    )

    print(f"\n✅ Klar! Tillagda: {added + pdf_count}  Uppdaterade: {updated}  Totalt: {len(existing)} pass")

if __name__ == '__main__':
    sync()
