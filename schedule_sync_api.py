#!/usr/bin/env python3
"""
Синк расписания НГИЭУ (Schedulab) -> напрямую в Google Calendar через API.

В отличие от .ics-подхода, тут скрипт при каждом запуске сам создаёт/обновляет/
удаляет события в конкретном календаре - без задержки на то, когда Google
решит перечитать файл. Событие считается "нашим", если у него в
extendedProperties.private проставлен managed=schedule_sync - по этому
признаку скрипт находит свои же старые события, чтобы их обновить или снести,
и не трогает ничего постороннего в календаре.

Настройка перед первым запуском (см. auth_setup.py и инструкцию к нему):
- нужен отдельный календарь под расписание (его id) - GOOGLE_CALENDAR_ID
- нужны данные от OAuth-приложения - GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN
Их можно положить в переменные окружения (так и будет в GitHub Actions) либо
прямо в константы ниже для локального теста.
"""

import os
import json
import hashlib
import datetime
import requests

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# ======================= НАСТРОЙКИ (поменять под себя) =======================

ACTOR_ID = "a11ce001-0000-0001-0000-000e00000000"
API_URL = "https://230352-2.vm.clodo.ru/api/v2/Schedule/Get"

SEMESTER_START = datetime.date(2026, 9, 1)
FIRST_WEEK_IS_UPPER = False
WEEKS_AHEAD = 20

TIMEZONE = "Europe/Moscow"  # Нижний Новгород = тот же часовой пояс, что и Москва

# Если True - берём локальный файл вместо похода в сеть (для теста/отладки)
USE_LOCAL_SAMPLE = False
LOCAL_SAMPLE_PATH = "sample_schedule.json"

# Данные для Google Calendar API. В GitHub Actions придут из secrets через env,
# для локального теста можно временно вписать значения прямо тут.
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REFRESH_TOKEN = os.environ.get("GOOGLE_REFRESH_TOKEN", "")
GOOGLE_CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID", "")

MANAGED_TAG = "schedule_sync"  # метка, по которой узнаём "свои" события

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
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def build_events(entries):
    """Возвращает список словарей с готовыми полями события, ключ - uid."""
    events = {}

    # --- 1. Разовые изменения (у записи проставлена конкретная дата) ---
    # Если предмет "Нет пар" (или пусто) - это отмена: пару нужно просто убрать,
    # а не создавать событие "Нет пар". change_keys всё равно заполняем, чтобы
    # подавить соответствующую пару из шаблона на эту дату.
    CANCELLED_SUBJECTS = {"Нет пар", "Нет пары", ""}
    change_keys = set()
    for entry in entries:
        if entry.get("date"):
            event_date = datetime.date.fromisoformat(entry["date"])
            start_t, end_t = parse_time_range(entry["classTime"])
            subject = entry["subjects"][0] if entry["subjects"] else ""
            is_cancelled = subject.strip() in CANCELLED_SUBJECTS
            for group in entry["groups"]:
                change_keys.add((group, event_date, start_t))
                if is_cancelled:
                    continue  # пару отменили - просто не создаём событие
                uid = make_uid(group, event_date, start_t, subject)
                events[uid] = {
                    "date": event_date, "start": start_t, "end": end_t, "group": group,
                    "subject": subject,
                    "instructors": entry.get("instructors", []),
                    "office": entry["offices"][0] if entry.get("offices") else "",
                    "note": entry["notes"][0] if entry.get("notes") else "",
                    "is_change": True,
                }

    # --- 2. Обычный повторяющийся шаблон, разворачиваем на WEEKS_AHEAD недель ---
    base_monday = monday_of_week(SEMESTER_START)
    for week_index in range(WEEKS_AHEAD):
        week_monday = base_monday + datetime.timedelta(weeks=week_index)
        upper = week_is_upper(week_monday)

        for entry in entries:
            if entry.get("date"):
                continue
            weekday_idx = WEEKDAY_RU_TO_INDEX.get(entry["dayName"])
            if weekday_idx is None:
                continue

            entry_upper = entry.get("isUpperWeek")
            if entry_upper is not None and entry_upper != upper:
                continue

            event_date = week_monday + datetime.timedelta(days=weekday_idx)
            if event_date < SEMESTER_START:
                continue

            start_t, end_t = parse_time_range(entry["classTime"])

            for group in entry["groups"]:
                if (group, event_date, start_t) in change_keys:
                    continue

                subject = entry["subjects"][0] if entry["subjects"] else ""
                uid = make_uid(group, event_date, start_t, subject)
                events[uid] = {
                    "date": event_date, "start": start_t, "end": end_t, "group": group,
                    "subject": subject,
                    "instructors": entry.get("instructors", []),
                    "office": entry["offices"][0] if entry.get("offices") else "",
                    "note": entry["notes"][0] if entry.get("notes") else "",
                    "is_change": False,
                }

    return events


def to_google_event_body(uid: str, ev: dict) -> dict:
    summary = ev["subject"]
    if ev["is_change"]:
        summary = f"[ИЗМЕНЕНО] {summary}"

    description_lines = []
    if ev["instructors"]:
        description_lines.append("Преподаватель: " + ", ".join(ev["instructors"]))
    if ev["note"]:
        description_lines.append(ev["note"])

    # Явно прописываем смещение +03:00 в самой строке времени, а не полагаемся
    # только на поле timeZone — так надёжнее, без риска, что Google
    # интерпретирует "голое" время как UTC.
    start_dt = datetime.datetime.combine(ev["date"], ev["start"])
    end_dt = datetime.datetime.combine(ev["date"], ev["end"])
    start_iso = start_dt.isoformat() + "+03:00"
    end_iso = end_dt.isoformat() + "+03:00"

    return {
        "summary": summary,
        "location": ev["office"],
        "description": "\n".join(description_lines),
        "start": {"dateTime": start_iso, "timeZone": TIMEZONE},
        "end": {"dateTime": end_iso, "timeZone": TIMEZONE},
        "extendedProperties": {
            "private": {
                "managed": MANAGED_TAG,
                "schedule_uid": uid,
            }
        },
    }


def event_needs_update(existing: dict, desired_body: dict) -> bool:
    for field in ("summary", "location", "description"):
        if existing.get(field, "") != desired_body.get(field, ""):
            return True
    if existing.get("start", {}).get("dateTime") != desired_body["start"]["dateTime"]:
        return True
    if existing.get("end", {}).get("dateTime") != desired_body["end"]["dateTime"]:
        return True
    return False


def get_calendar_service():
    creds = Credentials(
        token=None,
        refresh_token=GOOGLE_REFRESH_TOKEN,
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/calendar"],
    )
    return build("calendar", "v3", credentials=creds)


def fetch_existing_managed_events(service) -> dict:
    """Возвращает {schedule_uid: event} для всех событий, помеченных нашей меткой."""
    existing = {}
    page_token = None
    while True:
        resp = service.events().list(
            calendarId=GOOGLE_CALENDAR_ID,
            privateExtendedProperty=f"managed={MANAGED_TAG}",
            pageToken=page_token,
            maxResults=2500,
            singleEvents=True,
        ).execute()
        for item in resp.get("items", []):
            uid = item.get("extendedProperties", {}).get("private", {}).get("schedule_uid")
            if uid:
                existing[uid] = item
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return existing


def sync_to_google_calendar(desired_events: dict):
    service = get_calendar_service()
    existing = fetch_existing_managed_events(service)

    created = updated = deleted = unchanged = 0

    for uid, ev in desired_events.items():
        body = to_google_event_body(uid, ev)
        if uid not in existing:
            service.events().insert(calendarId=GOOGLE_CALENDAR_ID, body=body).execute()
            created += 1
        elif event_needs_update(existing[uid], body):
            service.events().update(
                calendarId=GOOGLE_CALENDAR_ID, eventId=existing[uid]["id"], body=body
            ).execute()
            updated += 1
        else:
            unchanged += 1

    # Событий, которые раньше были нашими, а теперь пропали из desired - удаляем
    # (например, пару отменили целиком или скорректировали шаблон)
    stale_uids = set(existing.keys()) - set(desired_events.keys())
    for uid in stale_uids:
        service.events().delete(calendarId=GOOGLE_CALENDAR_ID, eventId=existing[uid]["id"]).execute()
        deleted += 1

    print(f"Готово: создано {created}, обновлено {updated}, без изменений {unchanged}, удалено {deleted}")


def main():
    entries = fetch_schedule()
    desired_events = build_events(entries)
    sync_to_google_calendar(desired_events)


if __name__ == "__main__":
    main()
