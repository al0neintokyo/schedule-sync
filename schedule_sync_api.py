#!/usr/bin/env python3
"""
Синк расписания НГИЭУ (Schedulab) -> напрямую в Google Calendar через API.
Общая логика разбора расписания - в schedule_core.py.
"""

import os
import datetime
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

import schedule_core as core

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REFRESH_TOKEN = os.environ.get("GOOGLE_REFRESH_TOKEN", "")
GOOGLE_CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID", "")

MANAGED_TAG = "schedule_sync"  # метка, по которой узнаём "свои" события


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
    # только на поле timeZone - так надёжнее, без риска, что Google
    # интерпретирует "голое" время как UTC.
    start_dt = datetime.datetime.combine(ev["date"], ev["start"])
    end_dt = datetime.datetime.combine(ev["date"], ev["end"])
    start_iso = start_dt.isoformat() + "+03:00"
    end_iso = end_dt.isoformat() + "+03:00"

    return {
        "summary": summary,
        "location": ev["office"],
        "description": "\n".join(description_lines),
        "start": {"dateTime": start_iso, "timeZone": core.TIMEZONE},
        "end": {"dateTime": end_iso, "timeZone": core.TIMEZONE},
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

    stale_uids = set(existing.keys()) - set(desired_events.keys())
    for uid in stale_uids:
        service.events().delete(calendarId=GOOGLE_CALENDAR_ID, eventId=existing[uid]["id"]).execute()
        deleted += 1

    print(f"Готово (Google): создано {created}, обновлено {updated}, без изменений {unchanged}, удалено {deleted}")


def main():
    entries = core.fetch_schedule()
    desired_events = core.build_events(entries)
    sync_to_google_calendar(desired_events)


if __name__ == "__main__":
    main()
