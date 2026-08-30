#!/usr/bin/env python3
"""
Синк расписания НГИЭУ (Schedulab) -> единый .ics файл.
.ics можно подписать и в Google Calendar, и в Apple Calendar (iPhone/Mac) -
оба сами подтягивают обновления по URL, отдельный код под каждую платформу не нужен.

Как это работает:
1. Тянем JSON с /api/v2/Schedule/Get?actorId=... (или берём заглушку локально для теста).
2. Разворачиваем шаблон "день недели + чётность недели" в реальные даты на весь семестр.
3. Отдельно накладываем сверху разовые изменения (записи с непустым "date").
4. Пишем всё в один .ics файл.

Дальше файл нужно куда-то выложить со стабильным URL (GitHub Gist/Pages, любой
хостинг) и подписаться на него в календаре - тогда обновления идут сами.
"""

import json
import hashlib
import datetime
import requests
from icalendar import Calendar, Event

# ======================= НАСТРОЙКИ (поменять под себя) =======================

ACTOR_ID = "a11ce001-0000-0001-0000-000e00000000"
API_URL = "https://230352-2.vm.clodo.ru/api/v2/Schedule/Get"

# Дата начала семестра (дата первой реальной пары)
SEMESTER_START = datetime.date(2026, 9, 1)

# Какая по факту неделя (верхняя/нижняя) идёт в ту неделю, где лежит SEMESTER_START
FIRST_WEEK_IS_UPPER = True

# На сколько недель вперёд строим календарь за один прогон
WEEKS_AHEAD = 20

OUTPUT_ICS_PATH = "schedule.ics"

# Если True - берём локальный файл вместо похода в сеть (для теста/отладки)
USE_LOCAL_SAMPLE = False
LOCAL_SAMPLE_PATH = "sample_schedule.json"

# ==============================================================================

WEEKDAY_RU_TO_INDEX = {
    "Понедельник": 0,
    "Вторник": 1,
    "Среда": 2,
    "Четверг": 3,
    "Пятница": 4,
    "Суббота": 5,
    "Воскресенье": 6,
}


def fetch_schedule():
    if USE_LOCAL_SAMPLE:
        with open(LOCAL_SAMPLE_PATH, encoding="utf-8") as f:
            return json.load(f)
    resp = requests.get(API_URL, params={"actorId": ACTOR_ID}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def parse_time_range(class_time: str):
    """'13-45 / 15-15' -> (datetime.time(13,45), datetime.time(15,15))"""
    start_str, end_str = [p.strip() for p in class_time.split("/")]
    def to_time(s):
        h, m = s.split("-")
        return datetime.time(int(h), int(m))
    return to_time(start_str), to_time(end_str)


def monday_of_week(d: datetime.date) -> datetime.date:
    return d - datetime.timedelta(days=d.weekday())


def week_is_upper(week_monday: datetime.date) -> bool:
    base_monday = monday_of_week(SEMESTER_START)
    weeks_diff = (week_monday - base_monday).days // 7
    if weeks_diff % 2 == 0:
        return FIRST_WEEK_IS_UPPER
    return not FIRST_WEEK_IS_UPPER


def make_uid(group: str, date_: datetime.date, start_time: datetime.time, subject: str) -> str:
    raw = f"{group}|{date_.isoformat()}|{start_time.isoformat()}|{subject}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest() + "@schedule-sync"


def build_events(entries):
    """Возвращает список словарей с готовыми полями события."""
    events = []

    # --- 1. Разовые изменения (у записи проставлена конкретная дата) ---
    # ВНИМАНИЕ: реальный формат замены пока не видели живьём - как только
    # появится пример, эту секцию нужно будет поправить под точную структуру.
    change_keys = set()  # (group, date, start_time) - чтобы не задвоить с шаблоном
    for entry in entries:
        if entry.get("date"):
            event_date = datetime.date.fromisoformat(entry["date"])
            start_t, end_t = parse_time_range(entry["classTime"])
            for group in entry["groups"]:
                change_keys.add((group, event_date, start_t))
                events.append({
                    "date": event_date,
                    "start": start_t,
                    "end": end_t,
                    "group": group,
                    "subject": entry["subjects"][0] if entry["subjects"] else "",
                    "instructors": entry.get("instructors", []),
                    "office": entry["offices"][0] if entry.get("offices") else "",
                    "note": entry["notes"][0] if entry.get("notes") else "",
                    "is_change": True,
                })

    # --- 2. Обычный повторяющийся шаблон, разворачиваем на WEEKS_AHEAD недель ---
    base_monday = monday_of_week(SEMESTER_START)
    for week_index in range(WEEKS_AHEAD):
        week_monday = base_monday + datetime.timedelta(weeks=week_index)
        upper = week_is_upper(week_monday)

        for entry in entries:
            if entry.get("date"):
                continue  # это разовое изменение, уже обработано выше
            weekday_idx = WEEKDAY_RU_TO_INDEX.get(entry["dayName"])
            if weekday_idx is None:
                continue

            entry_upper = entry.get("isUpperWeek")
            if entry_upper is not None and entry_upper != upper:
                continue  # эта пара не в эту чётность недели

            event_date = week_monday + datetime.timedelta(days=weekday_idx)
            if event_date < SEMESTER_START:
                continue  # семестр ещё не начался

            start_t, end_t = parse_time_range(entry["classTime"])

            for group in entry["groups"]:
                if (group, event_date, start_t) in change_keys:
                    continue  # на этот день/пару/группу уже есть разовое изменение - оно приоритетнее

                events.append({
                    "date": event_date,
                    "start": start_t,
                    "end": end_t,
                    "group": group,
                    "subject": entry["subjects"][0] if entry["subjects"] else "",
                    "instructors": entry.get("instructors", []),
                    "office": entry["offices"][0] if entry.get("offices") else "",
                    "note": entry["notes"][0] if entry.get("notes") else "",
                    "is_change": False,
                })

    return events


def build_ics(events) -> Calendar:
    cal = Calendar()
    cal.add("prodid", "-//schedule-sync//ru//")
    cal.add("version", "2.0")
    cal.add("x-wr-calname", "Расписание")

    for ev in events:
        vevent = Event()
        summary = ev["subject"]
        if ev["is_change"]:
            summary = f"[ИЗМЕНЕНО] {summary}"
        vevent.add("summary", summary)
        vevent.add("dtstart", datetime.datetime.combine(ev["date"], ev["start"]))
        vevent.add("dtend", datetime.datetime.combine(ev["date"], ev["end"]))
        vevent.add("location", ev["office"])
        description_lines = []
        if ev["instructors"]:
            description_lines.append("Преподаватель: " + ", ".join(ev["instructors"]))
        if ev["note"]:
            description_lines.append(ev["note"])
        vevent.add("description", "\n".join(description_lines))
        vevent["uid"] = make_uid(ev["group"], ev["date"], ev["start"], ev["subject"])
        cal.add_component(vevent)

    return cal


def main():
    entries = fetch_schedule()
    events = build_events(entries)
    cal = build_ics(events)

    with open(OUTPUT_ICS_PATH, "wb") as f:
        f.write(cal.to_ical())

    print(f"Готово: {len(events)} событий записано в {OUTPUT_ICS_PATH}")


if __name__ == "__main__":
    main()
