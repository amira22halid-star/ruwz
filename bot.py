#!/usr/bin/env python3
"""
Warzone Patch Notes Bot
Официальные патчноуты (callofduty.com/patchnotes) -> ПОЛНЫЙ перевод на русский (Groq) -> Discord.

Как это работает:
  1. Открывает официальную страницу патчноутов, находит страницы Warzone.
  2. На каждой странице ищет датированные разделы ("THURSDAY SEPTEMBER 24" и т.п.):
     хотфиксы дописываются в ту же страницу, поэтому каждый раздел отслеживается отдельно.
  3. Новый раздел режется на куски, каждый кусок переводится целиком (ничего не сокращается),
     в переводе проверяется, что все числа из оригинала на месте.
  4. Готовый текст уходит в Discord несколькими сообщениями-карточками подряд.
Прогресс (переведённые куски, что уже отправлено) хранится в state.json, поэтому если
закончился лимит Groq или время запуска, следующий запуск продолжит с того же места.
"""
import os
import re
import sys
import json
import time
import hashlib
from datetime import datetime, timezone, timedelta, date

import requests

# ============================ НАСТРОЙКИ ============================
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()


def _models(name, default):
    raw = os.environ.get(name) or default
    return [m.strip() for m in raw.split(",") if m.strip()]


# Модели пробуются по порядку: если у первой кончился лимит - берётся следующая.
# Актуальные названия и лимиты: https://console.groq.com/docs/rate-limits
MODELS = _models("MODELS", "openai/gpt-oss-120b,llama-3.3-70b-versatile,qwen/qwen3-32b")

BASE = "https://www.callofduty.com"
LIST_URL = BASE + "/patchnotes"
PAGES_TO_CHECK = int(os.environ.get("PAGES_TO_CHECK") or "3")       # сколько последних страниц Warzone проверять
MAX_AGE_DAYS = float(os.environ.get("MAX_AGE_DAYS") or "20")        # разделы старше этого не публикуем
REPOST_LATEST = (os.environ.get("REPOST_LATEST") or "").lower() in ("1", "true", "yes")  # тест: отправить свежий раздел заново
CHUNK_CHARS = int(os.environ.get("CHUNK_CHARS") or "3000")          # размер куска оригинала для перевода
CHUNK_DELAY = float(os.environ.get("CHUNK_DELAY") or "25")          # пауза между запросами (лимит токенов в минуту)
RUN_BUDGET_MIN = float(os.environ.get("RUN_BUDGET_MIN") or "40")    # после этого запуск останавливается и продолжится в следующий
EMBED_CHARS = int(os.environ.get("EMBED_CHARS") or "3800")          # размер одного сообщения в Discord (лимит карточки 4096)
COLOR = 0xF59E0B
STATE_FILE = os.environ.get("STATE_FILE") or "state.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

MONTHS = ["january", "february", "march", "april", "may", "june", "july",
          "august", "september", "october", "november", "december"]
MONTHS_RU = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
             "августа", "сентября", "октября", "ноября", "декабря"]
DATE_H = re.compile(
    r"^#{1,4}\s*(?:(?:mon|tues|wednes|thurs|fri|satur|sun)day,?\s+)?(" + "|".join(MONTHS) + r")\s+(\d{1,2})\b",
    re.I)


def log(*a):
    print(*a, flush=True)


# ============================ СОСТОЯНИЕ ============================
def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            s = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        s = {}
    s.setdefault("seen", {})   # "путь|раздел" -> время публикации
    s.setdefault("cache", {})  # незавершённые переводы/публикации
    return s


def save_state(state):
    now = datetime.now(timezone.utc)
    state["seen"] = {k: t for k, t in state["seen"].items()
                     if now - datetime.fromisoformat(t) < timedelta(days=180)}
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


# ============================ ЗАГРУЗКА СТРАНИЦ ============================
def fetch(url):
    """Возвращает (вид, текст): 'html' - напрямую с сайта, 'md' - через запасной путь (r.jina.ai)."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=40)
        if r.status_code == 200 and len(r.text) > 2000:
            return "html", r.text
        log(f"[!] {url}: HTTP {r.status_code}, пробую запасной путь")
    except requests.RequestException as ex:
        log(f"[!] {url}: {ex.__class__.__name__}, пробую запасной путь")
    try:
        r = requests.get("https://r.jina.ai/" + url, timeout=90,
                         headers={"User-Agent": HEADERS["User-Agent"], "Accept": "text/plain"})
        if r.status_code == 200 and len(r.text) > 2000:
            return "md", r.text
        log(f"[!] запасной путь для {url}: HTTP {r.status_code}")
    except requests.RequestException as ex:
        log(f"[!] запасной путь для {url}: {ex.__class__.__name__}")
    return None, None


def discover_pages():
    """Ссылки на страницы патчноутов Warzone, самые свежие первыми."""
    kind, text = fetch(LIST_URL)
    if not text:
        return []
    found = {}
    for y, m, slug in re.findall(r"/patchnotes/(\d{4})/(\d{2})/([a-z0-9\-]+)", text):
        if "warzone" in slug and "mobile" not in slug:
            found[(y, m, slug)] = f"{BASE}/patchnotes/{y}/{m}/{slug}"
    keys = sorted(found, reverse=True)[:PAGES_TO_CHECK]
    return [found[k] for k in keys]


# ============================ РАЗБОР СТРАНИЦЫ ============================
def to_markdown(kind, raw):
    if kind == "html":
        import html2text
        h = html2text.HTML2Text()
        h.body_width = 0
        h.ignore_images = True
        h.ignore_links = True
        h.ignore_emphasis = True
        h.unicode_snob = True
        return h.handle(raw)
    md = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", raw)
    md = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", md)
    md = re.sub(r"\*\*(.+?)\*\*", r"\1", md)
    md = re.sub(r"(?<![\w*])\*(?!\s)([^*\n]+?)\*(?![\w*])", r"\1", md)
    return md


def clean_lines(md):
    md = re.sub(r"\\([\\`*{}\[\]()#+\-.!_>~|])", r"\1", md)
    out = []
    for raw in md.splitlines():
        t = raw.replace("\xa0", " ").rstrip()
        if not t.strip() or re.fullmatch(r"[!\[\]()<>\s]+", t):
            continue
        m = re.match(r"^(\s*)[*+\-]\s+(.*)$", t)
        if m:
            t = "  " * (len(m.group(1)) // 2) + "- " + m.group(2).strip()
        else:
            t = t.strip()
        out.append(t)
    return out


def article_lines(lines):
    start = next((i for i, l in enumerate(lines) if re.match(r"^#\s+.*patch notes", l, re.I)), None)
    if start is None:
        start = next((i for i, l in enumerate(lines) if re.match(r"^#\s+\S", l)), 0)
    end = len(lines)
    for i in range(start, len(lines)):
        if re.search(r"For regular updates about all Call of Duty|^Back to Top", lines[i]):
            end = i
            break
    return lines[start:end]


def infer_date(url_y, url_m, month, day):
    try:
        d = date(url_y, month, day)
    except ValueError:
        return None
    if (d - date(url_y, url_m, 1)).days < -90:  # страница создана в декабре, раздел из января
        d = date(url_y + 1, month, day)
    return d


def load_page(url):
    kind, raw = fetch(url)
    if not raw:
        return None
    lines = article_lines(clean_lines(to_markdown(kind, raw)))
    if not lines:
        return None
    m = re.search(r"/patchnotes/(\d{4})/(\d{2})/", url)
    uy, um = int(m.group(1)), int(m.group(2))
    title = re.sub(r"^#\s*", "", lines[0])
    title = re.sub(r"^Call of Duty:?\s*", "", title, flags=re.I)
    title = re.sub(r"\s*Patch Notes\s*$", "", title, flags=re.I).strip() or "Warzone"

    heads = [(i, DATE_H.match(l)) for i, l in enumerate(lines)]
    heads = [(i, mm) for i, mm in heads if mm]
    sections = []
    if heads:
        for n, (i, mm) in enumerate(heads):
            j = heads[n + 1][0] if n + 1 < len(heads) else len(lines)
            month = MONTHS.index(mm.group(1).lower()) + 1
            day = int(mm.group(2))
            d = infer_date(uy, um, month, day)
            if d is None:
                continue
            sections.append({"id": f"{MONTHS[month - 1][:3]}{day}", "date": d, "lines": lines[i + 1:j]})
    else:  # страница без датированных разделов: берём всё после заголовка
        first = next((i for i, l in enumerate(lines) if l.startswith("## ")), 1)
        sections.append({"id": "full", "date": date(uy, um, 1), "lines": lines[first:]})
    return {"path": url.replace(BASE, ""), "url": url, "title": title, "sections": sections}


# ============================ ПОДГОТОВКА ТЕКСТА ============================
NOISE = {"Weapon", "Adjustments", "Weapon:", "Adjustments:", "Damage Range", "Pre-Patch", "Post-Patch"}
TERMINATORS = ("Weapon:", "Additional Adjustments", "Attachment Adjustments")


DASH = r"[—–\-]{1,3}"


def _grab(seg, label):
    """Из куска 'Pre-Patch'/'Post-Patch' достаёт значение и пометку режима (All Modes и т.п.)."""
    mm = re.search(DASH + r"\s*" + label + r"\s*" + DASH + r"\s*", seg, re.I)
    if not mm:
        return None, ""
    rest = seg[mm.end():]
    nxt = re.search(DASH + r"\s*(Damage|Range)\s*" + DASH, rest, re.I)
    if nxt:
        rest = rest[:nxt.start()]
    rest = rest.strip()
    w = re.search(r"[A-Za-z]{2,}", rest)  # единицы (m, ms) - одна буква, пометки режима - слова
    val, mod = (rest[:w.start()].strip(), rest[w.start():].strip()) if w else (rest, "")
    return re.sub(r"\s*-\s*(?=\d)", " - ", val), mod


def parse_band(text):
    """Разбирает одну строку таблицы урона в компактный вид. None, если формат не узнан."""
    pre_m = re.search(r"Pre-Patch:?", text, re.I)
    post_m = re.search(r"Post-Patch:?", text, re.I)
    if not (pre_m and post_m and pre_m.start() < post_m.start()):
        return None
    band = text[:pre_m.start()].strip(" :")
    if not band:
        return None
    pre, post = text[pre_m.end():post_m.start()], text[post_m.end():]
    parts = []
    for label in ("Damage", "Range"):
        a, am = _grab(pre, label)
        b, bm = _grab(post, label)
        if a is None and b is None:
            continue
        mod = bm or am
        parts.append(f"{label} {a or '-'} → {b or '-'}" + (f" [{mod}]" if mod else ""))
    return f"{band}: " + "; ".join(parts) if parts else None


def compact_tables(lines):
    """Таблицы урона оружия сплющены в десятки строк - сворачиваем в одну строку на диапазон."""
    out, i, n = [], 0, len(lines)
    while i < n:
        if lines[i].strip().startswith("Damage Range:"):
            j = i + 1
            blk = [lines[i].strip()[len("Damage Range:"):].strip()]
            while j < n:
                t = lines[j].strip()
                if (t.startswith("Damage Range:") or t.startswith("#") or t.startswith("- ")
                        or any(t.startswith(x) for x in TERMINATORS)):
                    break
                blk.append(t)
                j += 1
            parsed = parse_band(" ".join(x for x in blk if x))
            if parsed:
                out.append(f"- {parsed}")
                i = j
                continue
        out.append(lines[i])
        i += 1
    return out


NOISE_RE = re.compile(r"Weapon|Adjustments|Damage Range|Pre-Patch|Post-Patch|[:\s]+")
MODE_RE = re.compile(r"All Modes|BR/RES Only|[A-Za-z/ ]{2,20} Only", re.I)


def prepare(lines):
    lines = compact_tables(lines)
    res = []
    for l in lines:
        t = l.strip()
        if NOISE_RE.sub("", t) == "" or re.match(r"^#+\s.*Damage Adjustments\s*$", t):
            continue
        if t in ("Additional Adjustments", "Attachment Adjustments"):
            t = "#### " + t
        elif MODE_RE.fullmatch(t):
            t = "Applies to: " + t
        else:
            t = l
        res.append(t.replace("⇩", "↓").replace("⇧", "↑"))
    return res


def is_heading(l):
    return bool(re.match(r"^#{1,4}\s", l))


def make_chunks(lines, limit):
    """Режет на куски ~limit символов по границам заголовков. Возвращает [(текст, последний_заголовок_до_куска)]."""
    chunks, cur, size, last_head, start_head = [], [], 0, "", ""
    for l in lines:
        add = len(l) + 1
        if cur and (size + add > limit * 1.15 or (is_heading(l) and size > limit * 0.6)):
            tail = []
            while cur and is_heading(cur[-1]):
                tail.insert(0, cur.pop())
            if cur:
                chunks.append(("\n".join(cur), start_head))
                start_head = last_head
            else:
                cur, tail = tail, []
            cur, size = tail, sum(len(x) + 1 for x in tail)
        cur.append(l)
        size += add
        if is_heading(l):
            last_head = re.sub(r"^#+\s*", "", l)
    if cur:
        chunks.append(("\n".join(cur), start_head))
    return chunks


# ============================ GROQ ============================
def groq_chat(messages, max_tokens):
    """Пробует модели по очереди. Возвращает (текст, модель) или (None, None)."""
    for model in MODELS:
        for _ in range(3):
            body = {"model": model, "messages": messages, "temperature": 0.1, "max_tokens": max_tokens}
            if "gpt-oss" in model:
                body["reasoning_effort"] = "low"
            try:
                r = requests.post(GROQ_URL, json=body, timeout=180,
                                  headers={"Authorization": f"Bearer {GROQ_API_KEY}"})
            except requests.RequestException as ex:
                log(f"[!] Сеть/Groq ({model}): {ex.__class__.__name__}")
                time.sleep(5)
                continue
            if r.status_code == 200:
                try:
                    txt = r.json()["choices"][0]["message"]["content"] or ""
                except (KeyError, IndexError, ValueError):
                    break
                txt = re.sub(r"<think>.*?</think>", "", txt, flags=re.S).strip()
                txt = re.sub(r"^```\w*\n?|\n?```$", "", txt).strip()
                if txt:
                    return txt, model
                break
            if r.status_code == 429:
                try:
                    wait = float(r.headers.get("retry-after", 30))
                except ValueError:
                    wait = 30
                if wait > 120:
                    log(f"[!] {model}: лимит исчерпан (ждать {int(wait)} с), пробую другую модель")
                    break
                log(f"[..] {model}: лимит в минуту, жду {int(wait)} с")
                time.sleep(wait + 1)
                continue
            log(f"[!] {model}: HTTP {r.status_code} {r.text[:200]}")
            break
    return None, None


SYSTEM_PROMPT = """You are a professional translator of Call of Duty: Warzone patch notes from English to Russian for a Russian-speaking community.
Translate the given fragment COMPLETELY and FAITHFULLY. Never summarize, shorten, merge, reorder, skip or add anything.

RULES
1. Every sentence, bullet, number, unit, date, time, percentage, price and name must appear in the translation. Copy numbers EXACTLY (keep decimal points as in the source, do not round). Keep arrows ↑ ↓ and "→" as they are. Convert units: m -> м, ms -> мс, m/s -> м/с.
2. Names stay in original Latin spelling: weapons, attachments, operators, maps, locations, modes, perks, field upgrades, killstreaks, events, camos, blueprints, game titles, and quoted item names.
3. Translate all ordinary words. Glossary: Damage = урон; Range = дальность; Maximum/Medium/Minimum Damage Range = максимальная/средняя/минимальная дальность урона; Recoil = отдача; Bullet Velocity = скорость пули; Aim Down Sight (ADS) Speed = скорость прицеливания (ADS); Headshot multiplier = множитель урона в голову; Ranked Play = рейтинговая игра; Skill Rating (SR) = рейтинг (SR); Deployment Fee = плата за вход (Deployment Fee); Limited-Time Mode = временный режим; Bug Fixes = исправления ошибок; buff = усиление; nerf = ослабление; All Modes = все режимы; BR/RES Only = только BR/Resurgence; Pre-Patch = было; Post-Patch = стало.
4. Formatting for Discord (only these): a line starting with "## " -> bold uppercase line like **ГЕЙМПЛЕЙ**; a line starting with "###" or "####" -> bold line like **AN-94** or **Изменения (Adjusted)**; bullets: "- " -> "• ", nested (2 spaces) -> "  ◦ ", deeper -> "    ▪ " ; a line starting with "> " is a developer comment -> italic line: _Комментарий разработчиков: ..._ ; a line "Applies to: All Modes" -> italic line _Применяется: все режимы_ . Keep one fragment line per output line.
5. Lines like "Maximum Damage Range: Damage 41 → 38↓ [All Modes]; Range 0 - 45m → 0 - 38m↓ [All Modes]" become "• Максимальная дальность урона: урон 41 → 38↓ [все режимы]; дальность 0 - 45 м → 0 - 38 м↓ [все режимы]".
6. If you see a table flattened into label lines followed by value lines, rebuild it as one bullet per row, keeping every value.
7. Output ONLY the translation: no preface, no comments, no code fences."""


def norm_num(tok):
    if "." in tok:
        a, b = tok.split(".", 1)
        return str(int(a)) + "." + b
    return str(int(tok))


def number_set(s):
    s = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", s)           # 4,000 -> 4000
    s = re.sub(r"(?<=\d)[ \u00a0\u202f](?=\d{3}(?!\d))", "", s)  # 4 000 -> 4000
    s = re.sub(r"(?<=\d),(?=\d)", ".", s)                   # 13,5 -> 13.5
    return {norm_num(t) for t in re.findall(r"\d+(?:\.\d+)?", s)}


def translate_chunk(text, head, idx, n, section_name):
    src_nums = number_set(text)
    allowed = max(1, int(0.05 * len(src_nums)))
    best, best_missing, note = None, None, ""
    for attempt in range(2):
        user = (f"Document: {section_name}\nFragment {idx + 1} of {n}. "
                f"Section heading before this fragment: {head or '-'}\n\n{text}{note}")
        out, model = groq_chat([{"role": "system", "content": SYSTEM_PROMPT},
                                {"role": "user", "content": user}], 4000)
        if out is None:
            return best  # None, если вообще не удалось; иначе лучший вариант из попыток
        missing = src_nums - number_set(out)
        if best is None or len(missing) < len(best_missing):
            best, best_missing = out, missing
        if len(missing) <= allowed:
            break
        log(f"[check] фрагмент {idx + 1}: в переводе не хватает чисел {sorted(missing)[:12]}, повтор")
        note = ("\n\nIMPORTANT: your previous translation omitted these numbers from the source: "
                + ", ".join(sorted(missing)[:25]) + ". Translate again and include everything.")
        time.sleep(5)
    if best_missing:
        log(f"[warn] фрагмент {idx + 1}: возможно пропущены числа {sorted(best_missing)[:12]}")
    return best


# ============================ DISCORD ============================
def _send(payload):
    for _ in range(4):
        try:
            r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=30)
        except requests.RequestException as ex:
            log(f"[!] Discord: {ex.__class__.__name__}")
            time.sleep(3)
            continue
        if r.status_code in (200, 204):
            return True
        if r.status_code == 429:
            try:
                wait = float(r.json().get("retry_after", 2))
            except Exception:
                wait = 2
            time.sleep(wait + 0.5)
            continue
        log(f"[!] Discord HTTP {r.status_code}: {r.text[:200]}")
        return False
    return False


def split_parts(text, limit):
    """Делит текст на сообщения <= limit символов по границам строк, не оставляя заголовок в конце."""
    def heading_like(l):
        s = l.strip()
        return len(s) < 100 and s.startswith("**") and s.endswith("**")

    lines = []
    for l in text.splitlines():
        while len(l) > limit:
            cut = l.rfind(" ", 0, limit)
            cut = cut if cut > limit // 2 else limit
            lines.append(l[:cut])
            l = l[cut:].lstrip()
        lines.append(l)
    parts, cur, size = [], [], 0
    for l in lines:
        add = len(l) + 1
        if cur and size + add > limit:
            tail = []
            while cur and heading_like(cur[-1]):
                tail.insert(0, cur.pop())
            if cur:
                parts.append("\n".join(cur).strip())
                cur = tail
            size = sum(len(x) + 1 for x in cur)
        cur.append(l)
        size += add
    if cur:
        parts.append("\n".join(cur).strip())
    return [p for p in parts if p]


def post_part(title, desc, footer):
    embed = {"description": desc[:4090], "color": COLOR, "footer": {"text": footer}}
    if title:
        embed["title"] = title[:250]
    return _send({"embeds": [embed], "allowed_mentions": {"parse": []}})


# ============================ ОБРАБОТКА РАЗДЕЛА ============================
def process(state, item, started):
    """Переводит и публикует один раздел. Возвращает 'done' | 'budget' | 'fail'."""
    key, page, sec = item["key"], item["page"], item["sec"]
    src = prepare(sec["lines"])
    if not src:
        state["seen"][key] = datetime.now(timezone.utc).isoformat()
        return "done"
    h = hashlib.sha1("\n".join(src).encode("utf-8")).hexdigest()
    chunks = make_chunks(src, CHUNK_CHARS)
    cache = state["cache"].get(key)
    if not cache or cache.get("hash") != h:
        cache = {"hash": h, "chunks": [None] * len(chunks), "parts": None, "posted": 0}
        state["cache"][key] = cache

    d = sec["date"]
    doc_name = f"{page['title']}, update of {d.isoformat()}"
    log(f"=== {doc_name}: {len(src)} строк, {sum(len(c[0]) for c in chunks)} символов, {len(chunks)} кусков")

    if cache["parts"] is None:
        for i, (text, head) in enumerate(chunks):
            if cache["chunks"][i] is not None:
                continue
            if time.time() - started > RUN_BUDGET_MIN * 60:
                log("Лимит времени запуска, продолжу в следующий раз.")
                return "budget"
            out = translate_chunk(text, head, i, len(chunks), doc_name)
            if out is None:
                log("[!] Все модели недоступны/исчерпаны, продолжу в следующий раз.")
                return "fail"
            cache["chunks"][i] = out
            save_state(state)
            log(f"[ok] переведён кусок {i + 1}/{len(chunks)}")
            if i < len(chunks) - 1:
                time.sleep(CHUNK_DELAY)
        cache["parts"] = split_parts("\n\n".join(cache["chunks"]), EMBED_CHARS)
        cache["posted"] = 0
        save_state(state)

    parts = cache["parts"]
    title = f"Патчноуты {page['title']} · {d.day} {MONTHS_RU[d.month - 1]} {d.year}"
    for i in range(cache["posted"], len(parts)):
        footer = f"Часть {i + 1}/{len(parts)}" if len(parts) > 1 else "Патчноуты Warzone"
        if not post_part(title if i == 0 else None, parts[i], footer):
            log("[!] Не удалось отправить в Discord, продолжу в следующий раз.")
            return "fail"
        cache["posted"] = i + 1
        save_state(state)
        log(f"[posted] часть {i + 1}/{len(parts)}")
        time.sleep(1.5)
    state["seen"][key] = datetime.now(timezone.utc).isoformat()
    state["cache"].pop(key, None)
    return "done"


def run(state):
    started = time.time()
    today = datetime.now(timezone.utc).date()
    pages = discover_pages()
    if not pages:
        log("[!] Не удалось получить список страниц патчноутов.")
        return
    log(f"Страницы Warzone: {len(pages)}")
    todo, newest = [], None
    for n, url in enumerate(pages):
        page = load_page(url)
        if not page:
            log(f"[!] Не удалось разобрать страницу {url}")
            continue
        for sec in page["sections"]:
            key = f"{page['path']}|{sec['id']}"
            item = {"key": key, "page": page, "sec": sec}
            if newest is None or sec["date"] > newest["sec"]["date"]:
                newest = item
            if key in state["seen"] or key in state["cache"]:
                if key in state["cache"] and key not in state["seen"]:
                    todo.append(item)  # недоделанное с прошлого раза
                continue
            if (today - sec["date"]).days > MAX_AGE_DAYS:
                state["seen"][key] = datetime.now(timezone.utc).isoformat()  # старое не публикуем
                continue
            todo.append(item)
    if REPOST_LATEST and newest and all(i["key"] != newest["key"] for i in todo):
        state["seen"].pop(newest["key"], None)
        state["cache"].pop(newest["key"], None)
        todo.append(newest)
        log(f"[тест] повторная отправка свежего раздела {newest['key']}")
    todo.sort(key=lambda i: i["sec"]["date"])
    log(f"К публикации разделов: {len(todo)}")
    for item in todo:
        if process(state, item, started) != "done":
            break


def main():
    if not GROQ_API_KEY or not DISCORD_WEBHOOK_URL:
        sys.exit("Не заданы секреты GROQ_API_KEY и/или DISCORD_WEBHOOK_URL")
    state = load_state()
    try:
        run(state)
    finally:
        save_state(state)


if __name__ == "__main__":
    main()
