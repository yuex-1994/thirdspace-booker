#!/usr/bin/env python3
"""
Third Space Gym Auto-Booker
Runs at the exact moment booking opens (48h before class start) and books instantly.
"""

import json
import os
import sys
import requests
from datetime import datetime, timedelta
import pytz

# ─── CONFIG ───────────────────────────────────────────────────────────────────

BASE_URL    = "https://api.thirdspace-london.app"
TOKEN_FILE  = os.path.expanduser("~/.thirdspace_token.json")
LONDON_TZ   = pytz.timezone("Europe/London")

CLUBS = {
    "canary_wharf":    39,
    "wood_wharf":     200,
    "performance_lab": 145,
}

# weekday() → (search string, HH:MM, club_id)
# Script runs exactly 48h before class, so target = today + 2 days
SCHEDULE = {
    0: ("swim - skills & drills", "07:00", CLUBS["wood_wharf"]),    # Monday
    1: ("Dynamic Reformer",  "06:10", CLUBS["wood_wharf"]),    # Tuesday
    2: ("Hot Vinyasa",       "06:45", CLUBS["wood_wharf"]),    # Wednesday
    3: ("Speed Fiends",      "06:20", CLUBS["canary_wharf"]),  # Thursday
    4: ("Yard WOD",          "06:00", CLUBS["canary_wharf"]),  # Friday
        5: ("Dynamic Reformer",  "08:10", CLUBS["wood_wharf"]),    # Saturday
    6: ("Hyrox",             "08:15", CLUBS["canary_wharf"]),  # Sunday
}

HEADERS = {
    "accept":          "*/*",
    "x-app-platform":  "ios",
    "x-app-version":   "6.5.1",
    "user-agent":      "ThirdSpace/62 CFNetwork/3860.500.112 Darwin/25.4.0",
    "accept-encoding": "gzip, deflate, br",
    "accept-language": "en-GB,en;q=0.9",
    "content-type":    "application/json",
}

# ─── TOKEN MANAGEMENT ─────────────────────────────────────────────────────────

CLOUD_MODE = bool(os.environ.get("THIRDSPACE_TOKEN"))

def load_token():
    if CLOUD_MODE:
        return {
            "token":      os.environ["THIRDSPACE_TOKEN"],
            "expires_at": os.environ["THIRDSPACE_EXPIRES_AT"],
        }
    if not os.path.exists(TOKEN_FILE):
        raise Exception(f"No token file at {TOKEN_FILE}. Run setup.py first.")
    with open(TOKEN_FILE) as f:
        return json.load(f)

def save_token(data):
    if CLOUD_MODE:
        print("⚠️  Token refreshed but running in cloud mode — cannot persist.")
        print("   Update THIRDSPACE_TOKEN and THIRDSPACE_EXPIRES_AT in GitHub Secrets manually:")
        print(f"   Token:      {data['token']}")
        print(f"   Expires at: {data['expires_at']}")
        return
    with open(TOKEN_FILE, "w") as f:
        json.dump(data, f, indent=2)

def auth_headers(token):
    return {**HEADERS, "x-fisikal-token": token}

def refresh_token(current_token):
    print("Token expiring soon — refreshing...")
    resp = requests.post(
        f"{BASE_URL}/api/v2/member/auth/token",
        params={"api-version": "2.0"},
        headers=auth_headers(current_token),
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    return {
        "token":      data["response"]["token"],
        "expires_at": data["expiresAt"],
    }

def get_valid_token():
    td = load_token()
    expires_at = datetime.fromisoformat(td["expires_at"])
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=pytz.UTC)
    days_left = (expires_at - datetime.now(pytz.UTC)).days
    if days_left < 3:
        td = refresh_token(td["token"])
        save_token(td)
    return td["token"]

# ─── CLASS LOOKUP ─────────────────────────────────────────────────────────────

def get_classes(token, target_date, location_id):
    start = LONDON_TZ.localize(datetime.combine(target_date, datetime.min.time()))
    end   = LONDON_TZ.localize(datetime.combine(target_date, datetime.max.time()))
    resp = requests.get(
        f"{BASE_URL}/api/v2/member/classes/get_classes",
        params={
            "api-version": "2.0",
            "Limits.Count": "500",
            "StartDate":    start.astimezone(pytz.UTC).strftime("%Y-%m-%dT%H:%M:%S"),
            "EndDate":      end.astimezone(pytz.UTC).strftime("%Y-%m-%dT%H:%M:%S"),
            "LocationId":   location_id,
        },
        headers=auth_headers(token),
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()

def extract_classes(data):
    return (
        data.get("classes")
        or data.get("response", {}).get("classes")
        or []
    )

def find_class(data, class_name, class_time, location_id):
    classes = extract_classes(data)
    matched_by_name = []

    for cls in classes:
        name      = cls.get("name", "")
        start     = cls.get("startDate", "")   # confirmed field name
        club_id   = cls.get("clubId", -1)       # confirmed field name

        name_match = class_name.lower() in name.lower()
        time_match = f"T{class_time}:" in start
        loc_match  = int(club_id) == int(location_id)

        if name_match and loc_match:
            matched_by_name.append((name, start, cls))

        if name_match and time_match and loc_match:
            return cls

    # Helpful debug if not found
    if matched_by_name:
        print(f"Found '{class_name}' at this club but not at {class_time}. Available times:")
        for n, s, _ in matched_by_name:
            print(f"  - {n} at {s}")
    else:
        print(f"No '{class_name}' found at club ID {location_id} on this date.")

    return None

# ─── BOOKING ──────────────────────────────────────────────────────────────────

def book_class(token, class_id):
    resp = requests.post(
        f"{BASE_URL}/api/v2/member/classes/book_class",
        params={"api-version": "2.0"},
        headers=auth_headers(token),
        json={"classId": class_id},
        timeout=10,
    )

    if resp.status_code == 400:
        print(f"⚠️  400 from booking API. Response body: {resp.text}")
        print("This usually means the class is already booked — treating as success.")
        return {"status": "already_booked", "raw": resp.text}

    resp.raise_for_status()
    return resp.json()

# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    now     = datetime.now(LONDON_TZ)
    if os.environ.get("TARGET_DATE"):
        target = datetime.strptime(os.environ["TARGET_DATE"], "%Y-%m-%d").date()
    else:
        target  = (now + timedelta(days=2)).date()
    weekday = target.weekday()

    print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] Running booker for {target} ({target.strftime('%A')})")

    if weekday not in SCHEDULE:
        print("No class scheduled. Exiting.")
        return

    class_name, class_time, club_id = SCHEDULE[weekday]
    print(f"Target: {class_name} at {class_time}")

    token       = get_valid_token()
    classes_data = get_classes(token, target, club_id)
    target_class = find_class(classes_data, class_name, class_time, club_id)

    if not target_class:
        print(f"ERROR: '{class_name}' at {class_time} not found on {target}.")
        print("Available classes at this club:")
        for c in extract_classes(classes_data):
            if int(c.get("clubId", -1)) == int(club_id):
                print(f"  - {c.get('name','?')} at {c.get('startDate','?')}")
        sys.exit(1)

    class_id = target_class.get("id", target_class.get("classId"))
    print(f"Found class ID {class_id} — booking now...")

    result = book_class(token, class_id)
    print(f"Booked! Response: {result}")

if __name__ == "__main__":
    main()
