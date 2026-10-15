#!/usr/bin/env python3
"""
Синк расписания НГИЭУ (Schedulab) -> напрямую в календарь iCloud через CalDAV.
Общая логика разбора расписания - в schedule_core.py. Работает по тому же
принципу, что и schedule_sync_api.py для Google: сам создаёт/обновляет/удаляет
события в конкретном (отдельном) календаре iCloud.

Настройка перед первым запуском:
1. На iPhone/Mac/icloud.com создай отдельный календарь с названием, указанным
   в ICLOUD_CALENDAR_NAME ниже (по умолчанию "Расписание") - именно в него
   будут писаться события, остальные календари скрипт не трогает.
2. Заведи app-specific password для своего Apple ID: appleid.apple.com ->
   Sign-In and Security -> App-Specific Passwords.
3. Положи APPLE_ID и этот пароль в переменные окружения (в GitHub Actions -
   через secrets) - APPLE_ID и APPLE_APP_PASSWORD.
"""

import os
import datetime
import caldav
from icalendar import Calendar as ICalendar, Event as ICalEvent

import schedule_core as core

APPLE_ID = os.environ.get("APPLE_ID", "")
APPLE_APP_PASSWORD = os.environ.get("APPLE_APP_PASSWORD", "")
ICLOUD_CALENDAR_NAME = os.environ.get("ICLOUD_CALENDAR_NAME", "Расписание")

CALDAV_URL = "https://caldav.icloud.com/"

# Нижний Новгород = UTC+3 круглый год (без перевода часов)
LOCAL_UTC_OFFSET = datetime.timedelta(hours=3)


def to_utc(local_dt: datetime.datetime) -> datetime.datetime:
    return (local_dt - LOCAL_UTC_OFFSET).replace(tzinfo=datetime.timezone.utc)


def build_ics(uid: str, ev: dict) -> str:
    summary = ev["subject"]
    if ev["is_change"]:
        summary = f"[ИЗМЕНЕНО] {summary}"

    description_lines = []
    if ev["instructors"]:
        description_lines.append("Преподаватель: " + ", ".join(ev["instructors"]))
    if ev["note"]:
        description_lines.append(ev["note"])

    start_utc = to_utc(datetime.datetime.combine(ev["date"], ev["start"]))
    end_utc = to_utc(datetime.datetime.combine(ev["date"], ev["end"]))

    cal = ICalendar()
    cal.add("prodid", "-//schedule-sync//ru//")
    cal.add("version", "2.0")

    vevent = ICalEvent()
    vevent.add("uid", f"{uid}@schedule-sync")
    vevent.add("summary", summary)
    vevent.add("dtstart", start_utc)
    vevent.add("dtend", end_utc)
    vevent.add("location", ev["office"])
    vevent.add("description", "\n".join(description_lines))
    cal.add_component(vevent)

    return cal.to_ical().decode("utf-8")


def extract_fields(ical_text: str) -> dict:
    """Достаём summary/location/description/dtstart/dtend из ics-текста существующего события."""
    cal = ICalendar.from_ical(ical_text)
    for component in cal.walk("VEVENT"):
        return {
            "summary": str(component.get("summary", "")),
            "location": str(component.get("location", "")),
            "description": str(component.get("description", "")),
            "dtstart": component.get("dtstart").dt,
            "dtend": component.get("dtend").dt,
        }
    return {}


def event_needs_update(existing_fields: dict, uid: str, ev: dict) -> bool:
    desired_summary = ev["subject"]
    if ev["is_change"]:
        desired_summary = f"[ИЗМЕНЕНО] {desired_summary}"
    if existing_fields.get("summary", "") != desired_summary:
        return True
    if existing_fields.get("location", "") != ev["office"]:
        return True
    desired_start = to_utc(datetime.datetime.combine(ev["date"], ev["start"]))
    desired_end = to_utc(datetime.datetime.combine(ev["date"], ev["end"]))
    if existing_fields.get("dtstart") != desired_start:
        return True
    if existing_fields.get("dtend") != desired_end:
        return True
    return False


def get_target_calendar():
    client = caldav.DAVClient(url=CALDAV_URL, username=APPLE_ID, password=APPLE_APP_PASSWORD)
    principal = client.principal()
    for cal in principal.calendars():
        if cal.name == ICLOUD_CALENDAR_NAME:
            return cal
    raise RuntimeError(
        f"Календарь '{ICLOUD_CALENDAR_NAME}' не найден в iCloud. "
        f"Создай его вручную в приложении Календарь (Mac/iPhone) или на icloud.com/calendar."
    )


def sync_to_icloud(desired_events: dict):
    calendar = get_target_calendar()

    # Так как календарь целиком выделен под расписание - все события в нём наши,
    # отдельная метка не нужна (в отличие от Google, где календарь мог бы быть общий).
    existing = {}
    for caldav_event in calendar.events():
        caldav_event.load()
        cal = ICalendar.from_ical(caldav_event.data)
        for component in cal.walk("VEVENT"):
            raw_uid = str(component.get("uid", ""))
            uid = raw_uid.split("@")[0]
            existing[uid] = caldav_event

    created = updated = deleted = unchanged = 0

    for uid, ev in desired_events.items():
        ics_text = build_ics(uid, ev)
        if uid not in existing:
            calendar.save_event(ics_text)
            created += 1
        else:
            existing_fields = extract_fields(existing[uid].data)
            if event_needs_update(existing_fields, uid, ev):
                existing[uid].data = ics_text
                existing[uid].save()
                updated += 1
            else:
                unchanged += 1

    stale_uids = set(existing.keys()) - set(desired_events.keys())
    for uid in stale_uids:
        existing[uid].delete()
        deleted += 1

    print(f"Готово (iCloud): создано {created}, обновлено {updated}, без изменений {unchanged}, удалено {deleted}")


def main():
    entries = core.fetch_schedule()
    desired_events = core.build_events(entries)
    sync_to_icloud(desired_events)


if __name__ == "__main__":
    main()
