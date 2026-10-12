#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Скрапер rasp.barsu.by для сайта «Расписание БарГУ».

Зачем он нужен
--------------
rasp.barsu.by НЕ отдаёт CORS-заголовки и принимает параметры только через POST,
поэтому браузер физически не может получить свежие данные напрямую, а бесплатные
CORS-реле нестабильны. Единственный надёжный способ — собирать расписание
на стороне сервера и класть в репозиторий готовый JSON. Этим и занимается
данный скрипт: он запускается в GitHub Actions по расписанию, пересобирает
`data/gNNN.json` и BOOT-снимок внутри `index.html`, а Pages публикует результат.

Что делает
----------
1. GET  https://rasp.barsu.by/stud.php          -> каталог групп и список недель
2. POST https://rasp.barsu.by/stud.php (на группу и неделю) -> HTML-таблица
3. Парсит таблицы (порт клиентского парсера из index.html, формат 1:1)
4. Обновляет data/gNNN.json, BOOT-снимок в index.html и data/version.json

Файлы перезаписываются ТОЛЬКО если содержимое реально изменилось, поэтому
«пустые» прогоны не создают коммитов и не дёргают пересборку Pages.

Только стандартная библиотека — ничего устанавливать не нужно.

Запуск вручную:
    python3 tools/scrape.py                 # полный прогон, запись в репозиторий
    python3 tools/scrape.py --dry-run       # собрать и показать отчёт, ничего не писать
    python3 tools/scrape.py --only ПИТТ21   # одна группа (отладка)
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

# ----------------------------------------------------------------------------- config

SRC_URL = "https://rasp.barsu.by/stud.php"
MINSK = timezone(timedelta(hours=3))  # Беларусь не переводит часы: UTC+3 круглый год
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36 barsu-schedule/2.0 (+https://github.com/zxkittenxd/barsu-schedule)"
)
DAY_NAMES = {
    "ПН": "Понедельник", "ВТ": "Вторник", "СР": "Среда",
    "ЧТ": "Четверг", "ПТ": "Пятница", "СБ": "Суббота", "ВС": "Воскресенье",
}
# недель в BOOT-снимке для группы по умолчанию (остальные грузятся лениво из data/)
BOOT_WEEKS_FOR_DEFAULT = 6

# ----------------------------------------------------------------------------- time helpers


def now_minsk() -> datetime:
    return datetime.now(MINSK)


def stamp(dt: datetime | None = None) -> str:
    return (dt or now_minsk()).strftime("%Y-%m-%d %H:%M:%S")


def minsk_iso(dt: datetime | None = None) -> str:
    return (dt or now_minsk()).strftime("%Y-%m-%d")


def monday_of(iso: str) -> str:
    d = datetime.strptime(iso, "%Y-%m-%d")
    return (d - timedelta(days=d.weekday())).strftime("%Y-%m-%d")


def add_days(iso: str, n: int) -> str:
    d = datetime.strptime(iso, "%Y-%m-%d") + timedelta(days=n)
    return d.strftime("%Y-%m-%d")


# ----------------------------------------------------------------------------- http


def http(url: str, data: bytes | None = None, timeout: int = 30, tries: int = 3) -> str:
    """GET/POST с повторами. Возвращает текст страницы (utf-8, BOM убирается)."""
    last: Exception | None = None
    for attempt in range(tries):
        try:
            req = urlrequest.Request(
                url,
                data=data,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                    "Accept-Language": "ru,en;q=0.8",
                    "Referer": SRC_URL,
                    **({"Content-Type": "application/x-www-form-urlencoded"} if data else {}),
                },
            )
            with urlrequest.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            return raw.decode("utf-8-sig", "replace")
        except Exception as exc:  # noqa: BLE001 - сеть может ломаться по-разному
            last = exc
            if isinstance(exc, urlerror.HTTPError) and exc.code in (403, 404):
                break  # бессмысленно долбиться
            time.sleep(1.2 * (attempt + 1) + random.random())
    raise RuntimeError(f"HTTP-запрос не удался: {url} ({type(last).__name__}: {last})")


def fetch_week(faculty: str, speciality: str, group: str, week: str, timeout: int) -> str:
    body = urlparse.urlencode(
        {
            "faculty": faculty,
            "speciality": speciality,
            "groups": group,
            "weekbegindate": week,
            "go": "Показать",
        }
    ).encode()
    return http(SRC_URL, data=body, timeout=timeout)


# ----------------------------------------------------------------------------- catalog


def _select_block(html: str, name: str) -> str:
    m = re.search(r'name=["\']%s["\'][\s\S]*?</select>' % re.escape(name), html)
    if not m:
        raise RuntimeError(f"Не найден <select name=\"{name}\"> — вёрстка rasp.barsu.by изменилась?")
    return m.group(0)


def _options(block: str) -> list[tuple[str, str]]:
    """(value, class) для всех <option> в блоке."""
    out = []
    for m in re.finditer(
        r"""<option\s+value=(?P<q>["'])(?P<v>.*?)(?P=q)(?:\s+class=(?P<q2>["'])(?P<c>.*?)(?P=q2))?""",
        block,
        re.I,
    ):
        out.append((m.group("v"), (m.group("c") or "").strip()))
    return out


def parse_catalog(html: str) -> list[dict]:
    """Список групп: код -> специальность -> факультет (связи лежат в class= у <option>)."""
    spec2fac = {
        _unescape(v): _unescape(c)
        for v, c in _options(_select_block(html, "speciality"))
        if v and v != "selectcard"
    }
    groups = []
    seen = set()
    for code, spec in _options(_select_block(html, "groups")):
        if not code or code == "selectcard" or code in seen:
            continue
        seen.add(code)
        spec = _unescape(spec)
        groups.append(
            {"code": code, "speciality": spec, "faculty": spec2fac.get(spec, "")}
        )
    if not groups:
        raise RuntimeError("Каталог групп пуст — rasp.barsu.by отдал что-то необычное")
    return groups


def parse_week_list(html: str) -> list[str]:
    """Недели из <select name="weekbegindate"> (порядок как на сайте — от новых к старым)."""
    try:
        block = _select_block(html, "weekbegindate")
    except RuntimeError:
        return []
    seen, out = set(), []
    for _, wk in re.findall(r"""value=(["'])(\d{4}-\d{2}-\d{2})\1""", block):
        if wk not in seen:
            seen.add(wk)
            out.append(wk)
    return out


def _unescape(s: str) -> str:
    return (
        s.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#039;", "'")
    )


# ----------------------------------------------------------------------------- parser
# Точный порт клиентского парсера из index.html (stripTags / parseContent /
# parseScheduleHTML), чтобы data/*.json оставались байт-в-байт совместимыми.

_RE_IMG_LEG = re.compile(r"<img[^>]*leg[^>]*>", re.I)
_RE_IMG = re.compile(r"<img[^>]*>", re.I)
_RE_BR = re.compile(r"<br\s*/?>", re.I)
_RE_TAG = re.compile(r"<[^>]+>")
_RE_TR = re.compile(r"<tr[\s\S]*?</tr>")
_RE_CELL = re.compile(r"<t[dh][^>]*>[\s\S]*?</t[dh]>")
_RE_TBODY_FIX = re.compile(r"(<tbody[^>]*>)\s*(<td[^>]*rowspan)", re.I)
_RE_START = re.compile(
    r"^(\[ЗАМЕНА\]\s*)?(.+?)\s+-\s+(Экзамен|Зачет|Зач|Экз|ЛК|ЛЗ|ПЗ|СЗ|ЛР|УЭК|КП|КР|СКР)(?=\s|$)\s*(.*)$",
    re.I,
)
_RE_ROOM = re.compile(r"^(\d+\s*/\s*\d+[а-яa-z]?)\s*(.*)$", re.I)
_RE_TIME = re.compile(r"^\s*(\d{1,2}[.,]\d{2})\s*-\s*(\d{1,2}[.,]\d{2})\s*$")
_RE_HEADING = re.compile(r"Расписание\s+на\s+неделю[^<]*")
_RE_UPDATED = re.compile(r"Последнее\s+обновление\s*([0-9][0-9\- :]*[0-9])")


def strip_tags(s: str) -> str:
    s = _RE_IMG_LEG.sub(" [ЗАМЕНА] ", s)
    s = _RE_IMG.sub(" ", s)
    s = _RE_BR.sub("\n", s)
    s = _RE_TAG.sub(" ", s)
    return _unescape(s)


def _fill_room(cur: dict, room: str, rest: str) -> None:
    cur["room"] = re.sub(r"\s+", "", room)
    pg = re.search(r"пг\s*:?\s*(\d+)", rest, re.I)
    if pg:
        cur["subgroup"] = pg.group(1)
        rest = rest.replace(pg.group(0), " ")
    parts = [p for p in re.sub(r"\s+", " ", rest).strip().split(" ") if p]
    if parts:
        cur["building"] = parts[0]
        extra = " ".join(parts[1:])
        if extra:
            cur["note"] = (cur["note"] + " " + extra) if cur["note"] else extra


def parse_content(html_cell: str) -> list[dict]:
    lines = [
        re.sub(r"\s+", " ", ln).strip()
        for ln in strip_tags(html_cell).split("\n")
    ]
    lines = [ln for ln in lines if ln]
    lessons: list[dict] = []
    cur: dict | None = None
    for line in lines:
        m = _RE_START.match(line)
        if m:
            cur = {
                "subjectRaw": m.group(2).strip(),
                "type": m.group(3).upper(),
                "teacher": "",
                "room": "",
                "building": "",
                "subgroup": "",
                "note": "",
                "replacement": bool(m.group(1)),
            }
            lessons.append(cur)
            tail = (m.group(4) or "").strip()
            if not tail:
                continue
            line = tail
        if cur is None:
            continue
        if "[ЗАМЕНА]" in line:
            cur["replacement"] = True
            line = line.replace("[ЗАМЕНА]", "").strip()
            if not line:
                continue
        rm = _RE_ROOM.match(line)
        if not cur["teacher"]:
            if rm:
                _fill_room(cur, rm.group(1), rm.group(2))
            else:
                cur["teacher"] = line
            continue
        if rm and not cur["room"]:
            _fill_room(cur, rm.group(1), rm.group(2))
        else:
            cur["note"] = (cur["note"] + " " + line) if cur["note"] else line
    return lessons


def _norm_time(t: str) -> str:
    return t.replace(",", ".", 1).replace(".", ":", 1)


def parse_schedule_html(page_html: str, monday_iso: str) -> dict | None:
    hm = _RE_HEADING.search(page_html)
    if not hm:
        return None
    t_start = page_html.find("<table", hm.start())
    t_end = page_html.find("</table>", t_start)
    if t_start < 0 or t_end < 0:
        return None
    table = _RE_TBODY_FIX.sub(r"\1<tr>\2", page_html[t_start:t_end])
    rows = _RE_TR.findall(table)
    um = _RE_UPDATED.search(page_html)
    out = {
        "weekBegin": monday_iso,
        "heading": hm.group(0).strip(),
        "lastUpdate": um.group(1).strip() if um else "",
        "days": [],
    }
    cur: dict | None = None
    slot_n = 0
    for row in rows:
        cells = _RE_CELL.findall(row)
        if not cells:
            continue
        idx = 0
        if "rowspan=9" in row:
            abbr = strip_tags(cells[0]).strip().upper()
            if abbr in DAY_NAMES:
                cur = {
                    "abbr": abbr,
                    "name": DAY_NAMES[abbr],
                    "date": strip_tags(cells[1] if len(cells) > 1 else "").strip(),
                    "slots": [],
                }
                out["days"].append(cur)
                slot_n = 0
            idx = 2
        rest = cells[idx:]
        if rest and cur is not None:
            tm = _RE_TIME.match(strip_tags(rest[0]))
            if tm:
                slot_n += 1
                cur["slots"].append(
                    {
                        "n": slot_n,
                        "time": f"{_norm_time(tm.group(1))}–{_norm_time(tm.group(2))}",
                        "lessons": parse_content(rest[1] if len(rest) > 1 else ""),
                    }
                )
    for k, d in enumerate(out["days"]):
        d["iso"] = add_days(monday_iso, k)
        d["dateFull"] = ".".join(reversed(d["iso"].split("-")))
    return out if out["days"] else None


# ----------------------------------------------------------------------------- repo io


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def read_json(path: str):
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return None


def write_if_changed(path: str, text: str) -> bool:
    old = None
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            old = fh.read()
    if old == text:
        return False
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    return True


SNAPSHOT_RE = re.compile(
    r'(<script id="snapshot" type="application/json">)([\s\S]*?)(</script>)'
)


def read_boot(index_html: str) -> dict:
    m = SNAPSHOT_RE.search(index_html)
    if not m:
        raise RuntimeError('В index.html нет <script id="snapshot" type="application/json">')
    return json.loads(m.group(2))


def write_boot(index_html: str, boot: dict) -> str:
    return SNAPSHOT_RE.sub(
        lambda m: m.group(1) + dumps(boot) + m.group(3), index_html, count=1
    )


def weeks_with_lessons(schedules: dict) -> list[str]:
    out = []
    for wk, sch in schedules.items():
        if any(sl["lessons"] for d in sch.get("days", []) for sl in d.get("slots", [])):
            out.append(wk)
    out.sort(reverse=True)
    return out


# ----------------------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(description="Скрапер rasp.barsu.by -> data/*.json + BOOT")
    ap.add_argument("--root", default=".", help="корень репозитория (по умолчанию текущий каталог)")
    ap.add_argument("--dry-run", action="store_true", help="не записывать файлы")
    ap.add_argument("--only", help="собрать только указанную группу (отладка)")
    ap.add_argument("--workers", type=int, default=4, help="параллельных запросов (по умолчанию 4)")
    ap.add_argument("--timeout", type=int, default=30, help="таймаут запроса, сек")
    ap.add_argument("--max-weeks", type=int, default=8,
                    help="сколько недель на группу максимум тянуть (по умолчанию 8)")
    ap.add_argument("--forward-weeks", type=int, default=2,
                    help="сколько недель вперёд от текущей всегда обновлять (по умолчанию 2: "
                         "текущая + следующая; именно их и публикует университет)")
    ap.add_argument("--backfill-days", type=int, default=21,
                    help="насколько назад добирать незаполненные недели (по умолчанию 21)")
    ap.add_argument("--report", help="куда положить отчёт для текста коммита")
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    index_path = os.path.join(root, "index.html")
    data_dir = os.path.join(root, "data")
    if not os.path.exists(index_path):
        print(f"!! не найден {index_path}", file=sys.stderr)
        return 2

    with open(index_path, encoding="utf-8") as fh:
        index_html = fh.read()
    boot = read_boot(index_html)
    default_group = boot.get("defaultGroup") or "ПИТТ21"

    t0 = time.time()
    print(":: Запрашиваю каталог групп и список недель…", flush=True)
    catalog_html = http(SRC_URL, timeout=args.timeout)
    catalog = parse_catalog(catalog_html)
    weeks_all = parse_week_list(catalog_html)
    if args.only:
        catalog = [g for g in catalog if g["code"] == args.only]
        if not catalog:
            print(f"!! группа {args.only} не найдена в каталоге", file=sys.stderr)
            return 2
    if not weeks_all:
        print("!! список недель пуст — не буду ничего перезаписывать", file=sys.stderr)
        return 1

    today = minsk_iso()
    current_week = monday_of(today)
    print(f":: групп: {len(catalog)} · недель у университета: {len(weeks_all)} "
          f"· сегодня {today} (неделя {current_week})", flush=True)

    # --- нумерация файлов: сохраняем существующую, новые группы дописываем в конец
    file_of = {g["code"]: g["file"] for g in boot.get("groups", []) if g.get("file")}
    next_idx = 0
    for f in file_of.values():
        m = re.search(r"g(\d+)\.json$", f)
        if m:
            next_idx = max(next_idx, int(m.group(1)) + 1)
    order = [g["code"] for g in boot.get("groups", [])]  # прежний порядок
    for g in catalog:  # новые группы — в алфавитном порядке в хвост
        if g["code"] not in file_of:
            file_of[g["code"]] = f"data/g{next_idx:03d}.json"
            next_idx += 1
            order.append(g["code"])

    catalog_by_code = {g["code"]: g for g in catalog}
    horizon = add_days(current_week, -args.backfill_days)

    # --- какие недели тянуть для каждой группы
    tasks: list[tuple[str, str]] = []
    existing: dict[str, dict] = {}
    asked: dict[str, list[str]] = {}
    for g in catalog:
        code = g["code"]
        path = os.path.join(root, file_of[code])
        old = read_json(path) or {}
        old_schedules = old.get("schedules") or {}
        existing[code] = {"meta": old.get("meta") or {}, "schedules": old_schedules}
        forward_limit = add_days(current_week, 7 * max(0, args.forward_weeks - 1))
        # Прошлые недели университет уже не правит, поэтому пустой ответ запоминаем
        # в meta.probed и больше эту неделю не запрашиваем — иначе каждый прогон
        # заново дёргал бы сервер по заведомо пустым неделям.
        probed = set(old.get("meta", {}).get("probed") or [])
        want = []
        for wk in weeks_all:
            if current_week <= wk <= forward_limit:
                want.append(wk)          # текущая и ближайшие: там бывают замены
            elif wk < current_week and wk >= horizon and wk not in old_schedules and wk not in probed:
                want.append(wk)          # дыра в истории — добираем один раз
        want = want[: args.max_weeks]
        for wk in want:
            tasks.append((code, wk))
        asked[code] = want

    print(f":: запросов к rasp.barsu.by: {len(tasks)} (workers={args.workers})", flush=True)

    # --- качаем
    results: dict[tuple[str, str], dict | None] = {}
    errors: list[str] = []
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futs = {}
        for code, wk in tasks:
            g = catalog_by_code[code]
            futs[pool.submit(fetch_week, g["faculty"], g["speciality"], code, wk, args.timeout)] = (code, wk)
            time.sleep(0.05)  # не долбим сервер университета очередью
        for fut in as_completed(futs):
            code, wk = futs[fut]
            done += 1
            try:
                html = fut.result()
                results[(code, wk)] = parse_schedule_html(html, wk)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{code} {wk}: {exc}")
                results[(code, wk)] = None
            if done % 50 == 0 or done == len(tasks):
                print(f"   … {done}/{len(tasks)}", flush=True)

    ok = sum(1 for v in results.values() if v)
    hard_fail = len(errors)
    if tasks and hard_fail / len(tasks) > 0.2:
        print(f"!! слишком много ошибок сети ({hard_fail}/{len(tasks)}) — данные не трогаю",
              file=sys.stderr)
        for e in errors[:10]:
            print("   " + e, file=sys.stderr)
        return 1

    # --- собираем новые файлы
    written, changed_groups, new_weeks = [], [], set()
    groups_out = []
    per_group: dict[str, dict] = {}
    for code in order:
        if code not in catalog_by_code:
            # группа исчезла из каталога университета — оставляем как есть
            rec = next((g for g in boot.get("groups", []) if g["code"] == code), None)
            if rec:
                groups_out.append(rec)
                per_group[code] = existing.get(code, {"meta": {}, "schedules": {}})
            continue
        g = catalog_by_code[code]
        old = existing.get(code, {"meta": {}, "schedules": {}})
        old_meta = old.get("meta") or {}
        schedules = dict(old["schedules"])
        for (c, wk), parsed in results.items():
            if c == code and parsed:
                schedules[wk] = parsed
        # порядок недель: от новых к старым, как в исходных файлах
        schedules = {k: schedules[k] for k in sorted(schedules, reverse=True)}

        probed = {w for w in (old_meta.get("probed") or []) if w < current_week}
        probed |= {w for w in asked.get(code, []) if w < current_week}
        probed_list = sorted(probed, reverse=True)

        data_changed = schedules != old["schedules"]
        fetched_at = stamp() if data_changed else (old_meta.get("fetchedAt") or stamp())
        meta = {
            "group": code,
            "faculty": g["faculty"],
            "speciality": g["speciality"],
            "source": SRC_URL,
            "fetchedAt": fetched_at,
            "weeksAvailable": weeks_all,
            "currentWeek": current_week,
            "probed": probed_list,
        }
        obj = {"meta": meta, "schedules": schedules}
        per_group[code] = obj
        has_weeks = weeks_with_lessons(schedules)
        groups_out.append({
            "code": code,
            "speciality": g["speciality"],
            "faculty": g["faculty"],
            "file": file_of[code],
            "has": bool(has_weeks),
            "weeks": has_weeks,
        })
        # пишем файл, если изменилось хоть что-то (данные или служебный probed),
        # но в отчёт и в BOOT-время попадают только реальные изменения расписания
        path = os.path.join(root, file_of[code])
        text = dumps(obj)
        if data_changed:
            written.append(file_of[code])
            changed_groups.append(code)
            for wk, parsed in schedules.items():
                if old["schedules"].get(wk) != parsed:
                    new_weeks.add(wk)
        if not args.dry_run:
            write_if_changed(path, text)

    # --- BOOT-снимок
    dg = default_group if default_group in per_group else (order[0] if order else default_group)
    dg_obj = per_group.get(dg, {"meta": {}, "schedules": {}})
    dg_sched = dict(list(dg_obj["schedules"].items())[:BOOT_WEEKS_FOR_DEFAULT])
    # Время сборки ставим в снимок только если что-то реально поменялось: иначе каждый
    # прогон давал бы новый коммит и лишнюю пересборку Pages при нулевых изменениях.
    old_boot_text = SNAPSHOT_RE.search(index_html).group(2)
    old_fetched = boot.get("fetchedAt") or stamp()

    def make_boot(fetched_at: str) -> dict:
        return {
            "groups": groups_out,
            "currentWeek": current_week,
            "weeksAvailable": weeks_all,
            "fetchedAt": fetched_at,
            "defaultGroup": dg,
            "defaultData": {
                "meta": {k: v for k, v in dict(dg_obj.get("meta") or {}, group=dg,
                         weeksAvailable=weeks_all, currentWeek=current_week,
                         fetchedAt=fetched_at).items() if k != "probed"},
                "schedules": dg_sched,
            },
        }

    boot_changed = dumps(make_boot(old_fetched)) != old_boot_text
    fetched_at = stamp() if (written or boot_changed) else old_fetched
    new_boot = make_boot(fetched_at)
    new_index = write_boot(index_html, new_boot)
    index_changed = new_index != index_html
    if index_changed and not args.dry_run:
        write_if_changed(index_path, new_index)

    # --- version.json: маленький файл, по которому открытый сайт понимает, что данные обновились
    version = {
        "fetchedAt": new_boot["fetchedAt"],
        "currentWeek": current_week,
        "weeks": weeks_all,
        "groups": len(groups_out),
        "publishedWeeks": sorted({w for g in groups_out for w in g["weeks"]}, reverse=True),
        "source": SRC_URL,
    }
    version_changed = write_if_changed(
        os.path.join(data_dir, "version.json"), dumps(version)
    ) if not args.dry_run else True

    # --- отчёт
    elapsed = time.time() - t0
    lines = [
        f"групп: {len(groups_out)}",
        f"запросов: {len(tasks)} (успешно распарсено {ok}, сетевых ошибок {hard_fail})",
        f"изменилось файлов групп: {len(written)}",
        f"новых/обновлённых недель: {', '.join(sorted(new_weeks, reverse=True)) or '—'}",
        f"index.html (BOOT): {'обновлён' if index_changed else 'без изменений'}",
        f"data/version.json: {'обновлён' if version_changed else 'без изменений'}",
        f"время: {elapsed:.0f} c",
    ]
    report = "\n".join(lines)
    print("\n=== ИТОГ ===\n" + report, flush=True)
    if changed_groups:
        print("изменённые группы: " + ", ".join(changed_groups[:40])
              + ("…" if len(changed_groups) > 40 else ""), flush=True)

    changed_anything = bool(written) or index_changed
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            fh.write(report + "\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write("### Обновление расписания\n\n```\n" + report + "\n```\n")

    if not changed_anything:
        print(":: изменений нет — коммит не нужен", flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
