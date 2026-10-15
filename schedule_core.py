#!/usr/bin/env python3
"""
Общая логика синка расписания НГИЭУ (новый бэкенд api.schedule.ngieu.ru).
Используется и schedule_sync_api.py (Google Calendar), и icloud_sync.py (Apple/iCloud).

Новый API сам резолвит шаблон + изменения в итоговый список на конкретные даты
(day_date), отменённые пары просто отсутствуют в ответе - никакой ручной логики
чётности недель или разбора isChange/"Нет пар" больше не нужно.
"""

import os
import json
import hashlib
import datetime
import requests

# ======================= НАСТРОЙКИ (поменять под себя) =======================

API_URL = "https://api.schedule.ngieu.ru/api/v1/public/service/schedule"
GROUP_ID = 14  # Б-23ИвО

# Храним как секрет (переменная окружения), а не хардкодим в код - на случай,
# если вуз когда-нибудь будет ротировать такие клиентские credentials.
CLIENT_CREDENTIAL = os.environ.get(
    "SCHEDULE_CLIENT_CREDENTIAL",
    "slinst_EBHNZBmoYOJI_Fc-PfzaYA.QI0OFCqalYQry_mmjMbZA12kdSZCCIsW20rtsfpfh7Y",
)

SEMESTER_START = datetime.date(2026, 9, 1)
WEEKS_AHEAD = 20

TIMEZONE = "Europe/Moscow"  # Нижний Новгород = тот же часовой пояс, что и Москва

CHUNK_DAYS = 30  # на всякий случай запрашиваем диапазон порциями, а не целиком

# Если True - берём локальный файл (список entries) вместо похода в сеть
USE_LOCAL_SAMPLE = False
LOCAL_SAMPLE_PATH = "sample_schedule.json"

# ==============================================================================


def fetch_schedule():
    """Возвращает плоский список entries (как в поле "entries" ответа API)."""
    if USE_LOCAL_SAMPLE:
        with open(LOCAL_SAMPLE_PATH, encoding="utf-8") as f:
            return json.load(f)

    all_entries = []
    cur = SEMESTER_START
    end_total = SEMESTER_START + datetime.timedelta(weeks=WEEKS_AHEAD)
    headers = {"X-Schedulab-Client-Credential": CLIENT_CREDENTIAL}

    while cur < end_total:
        chunk_end = min(cur + datetime.timedelta(days=CHUNK_DAYS - 1), end_total)
        params = {
            "groupId": GROUP_ID,
            "dateFrom": cur.isoformat(),
            "dateTo": chunk_end.isoformat(),
            "includeChanges": "true",
        }
        resp = requests.get(API_URL, params=params, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        all_entries.extend(data.get("entries", []))
        cur = chunk_end + datetime.timedelta(days=1)

    return all_entries


def parse_hhmm(s: str) -> datetime.time:
    h, m = s.split(":")
    return datetime.time(int(h), int(m))


def make_uid(group: str, date_: datetime.date, start_time: datetime.time, subject: str) -> str:
    raw = f"{group}|{date_.isoformat()}|{start_time.isoformat()}|{subject}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def build_events(entries):
    """Превращает плоский список entries от API в {uid: словарь с полями события}."""
    events = {}
    for e in entries:
        event_date = datetime.date.fromisoformat(e["day_date"])
        start_t = parse_hhmm(e["time_start"])
        end_t = parse_hhmm(e["time_end"])
        group = e["group_name"]
        # Обычная пара - subject_name; "Кураторский час" и подобное - activity_type_name
        subject = e.get("subject_name") or e.get("activity_type_name") or ""
        instructors = [e["instructor_display"]] if e.get("instructor_display") else []
        office = e.get("classroom_name") or ""
        note = e.get("subject_type_name") or ""
        is_change = e.get("source_kind") == "change"

        uid = make_uid(group, event_date, start_t, subject)
        events[uid] = {
            "date": event_date, "start": start_t, "end": end_t, "group": group,
            "subject": subject,
            "instructors": instructors,
            "office": office,
            "note": note,
            "is_change": is_change,
        }
    return events
