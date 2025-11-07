#!/usr/bin/env python3
"""
bot.py — Event creation: Single modal with flexible parsing, creates Bot Event only (no Discord Scheduled Event).
Embed layout adjusted: no confirmation on idea delete, no icons in event embed, matches back in poll embed.
Daily summary now shows only new matches since last post.
Added quarterly poll with day-based availability, improved navigation within one message, fixed view attribute access, added labels for sections, fixed PollView definition, fixed day selection persistence, updated week calculation to Monday-Sunday, removed checkmarks from weekly poll, added weekly summary for quarterly poll.

Replace your running bot.py with this file and restart the bot.
"""
from __future__ import annotations

import os
import io
import sqlite3
import asyncio
import logging
from datetime import datetime, timedelta, timezone, date, time as _time
from zoneinfo import ZoneInfo
from typing import Optional, List, Dict, Set, Tuple

import discord
from discord.ext import commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

# -------------------------
# Logging & config
# -------------------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

DB_PATH = os.getenv("POLL_DB", "polls.sqlite")
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0")) if os.getenv("CHANNEL_ID") else None
CREATED_EVENTS_CHANNEL_ID = int(os.getenv("CREATED_EVENTS_CHANNEL_ID", "0")) if os.getenv("CREATED_EVENTS_CHANNEL_ID") else None
QUARTERLY_CHANNEL_ID = int(os.getenv("QUARTERLY_CHANNEL_ID", "0")) if os.getenv("QUARTERLY_CHANNEL_ID") else None  # New variable
POST_TIMEZONE = os.getenv("POST_TIMEZONE", "Europe/Berlin")

# -------------------------
# DB helpers & init
# -------------------------
def init_db():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS polls (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS options (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            poll_id TEXT NOT NULL,
            option_text TEXT NOT NULL,
            created_at TEXT NOT NULL,
            author_id INTEGER
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS votes (
            poll_id TEXT NOT NULL,
            option_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            UNIQUE(poll_id, option_id, user_id)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS availability (
            poll_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            slot TEXT NOT NULL,
            UNIQUE(poll_id, user_id, slot)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS daily_summaries (
            channel_id INTEGER PRIMARY KEY,
            message_id INTEGER,
            created_at TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS weekly_summaries (
            channel_id INTEGER PRIMARY KEY,
            message_id INTEGER,
            created_at TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS created_events (
            id TEXT PRIMARY KEY,
            poll_id TEXT,
            title TEXT,
            description TEXT,
            start_time TEXT,
            end_time TEXT,
            participants TEXT,
            location TEXT,
            posted_channel_id INTEGER,
            posted_message_id INTEGER,
            created_at TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS created_event_rsvps (
            event_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            UNIQUE(event_id, user_id)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS last_posted_matches (
            poll_id TEXT PRIMARY KEY,
            matches TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS last_posted_weekly_matches (
            poll_id TEXT PRIMARY KEY,
            matches TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    con.commit()
    con.close()

def db_execute(query: str, params=(), fetch=False, many=False):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    try:
        if many:
            cur.executemany(query, params)
        else:
            cur.execute(query, params)
        rows = cur.fetchall() if fetch else None
        con.commit()
        return rows
    finally:
        con.close()

# -------------------------
# Utilities
# -------------------------
DAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
MONTHS = ["Jan", "Feb", "Mär", "Apr", "Mai", "Jun", "Jul", "Aug", "Sep", "Okt", "Nov", "Dez"]
HOURS = list(range(12, 24))

def slot_label_range(day_short: str, hour: int) -> str:
    start = hour % 24
    end = (hour + 1) % 24
    return f"{day_short}. {start:02d}:00 - {end:02d}:00 Uhr"

def format_slot_range(slot: str) -> str:
    try:
        day, hour_s = slot.split("-")
        return slot_label_range(day, int(hour_s))
    except Exception:
        return slot

def user_display_name(guild: Optional[discord.Guild], user_id: int) -> str:
    try:
        if guild:
            member = guild.get_member(user_id)
            if member:
                return member.display_name
        u = bot.get_user(user_id)
        return getattr(u, "name", str(user_id)) if u else str(user_id)
    except Exception:
        return str(user_id)

_WEEKDAY_MAP = {"Mo": 0, "Di": 1, "Mi": 2, "Do": 3, "Fr": 4, "Sa": 5, "So": 6}
def next_date_for_day_short(day_short: str, tz: ZoneInfo = ZoneInfo(POST_TIMEZONE)) -> date:
    today = datetime.now(tz).date()
    target = _WEEKDAY_MAP.get(day_short[:2], None)
    if target is None:
        return today
    days_ahead = (target - today.weekday() + 7) % 7
    return today + timedelta(days=days_ahead)

def date_to_ddmmyyyy(d: date) -> str:
    return d.strftime("%d.%m.%Y")

def parse_date_ddmmyyyy(s: str) -> Optional[date]:
    s = s.strip()
    try:
        parts = s.split(".")
        if len(parts) == 3:
            d, m, y = map(int, parts)
            return date(y, m, d)
        # try ISO fallback
        return date.fromisoformat(s)
    except Exception:
        return None

def parse_time_hhmm(s: str) -> Optional[_time]:
    s = s.strip()
    try:
        hh, mm = map(int, s.split(":"))
        return _time(hh, mm)
    except Exception:
        return None

def parse_date_range(date_range_str: str) -> Tuple[Optional[date], Optional[date]]:
    """
    Parse a date range string like "01.01.2026 - 02.01.2026" or shorthand "01.05." -> "01.05.2025 - 01.05.2025".
    Returns (start_date, end_date) or (None, None) on error.
    """
    date_range_str = date_range_str.strip()
    parts = [p.strip() for p in date_range_str.split("-")]
    if len(parts) == 1:
        # Single date: use as start and end
        single_date_str = parts[0]
        if not single_date_str:
            return None, None
        # If no year, add current year
        if single_date_str.count(".") == 1:  # e.g., "01.05"
            current_year = datetime.now().year
            single_date_str += f".{current_year}"
        start_date = parse_date_ddmmyyyy(single_date_str)
        end_date = start_date
    elif len(parts) == 2:
        start_str, end_str = parts
        # If no year, add current year
        if start_str.count(".") == 1:
            start_str += f".{datetime.now().year}"
        if end_str.count(".") == 1:
            end_str += f".{datetime.now().year}"
        start_date = parse_date_ddmmyyyy(start_str)
        end_date = parse_date_ddmmyyyy(end_str)
    else:
        return None, None
    return start_date, end_date

def parse_time_range(time_range_str: str) -> Tuple[Optional[_time], Optional[_time]]:
    """
    Parse a time range string like "18:00 - 20:00" or shorthand "9-10" -> "09:00 - 10:00".
    Returns (start_time, end_time) or (None, None) on error.
    """
    time_range_str = time_range_str.strip()
    parts = [p.strip() for p in time_range_str.split("-")]
    if len(parts) != 2:
        return None, None
    start_str, end_str = parts
    # If only numbers, add :00
    if start_str.isdigit():
        start_str += ":00"
    if end_str.isdigit():
        end_str += ":00"
    start_time = parse_time_hhmm(start_str)
    end_time = parse_time_hhmm(end_str)
    return start_time, end_time

# New utilities for quarterly
def get_current_quarter_start() -> date:
    now = datetime.now(ZoneInfo(POST_TIMEZONE)).date()
    year = now.year
    if now.month <= 3:
        start_month = 1
    elif now.month <= 6:
        start_month = 4
    elif now.month <= 9:
        start_month = 7
    else:
        start_month = 10
    return date(year, start_month, 1)

def get_quarter_months(start_date: date) -> List[str]:
    months = []
    for i in range(3):
        m = (start_date.month + i - 1) % 12 + 1
        y = start_date.year + ((start_date.month + i - 1) // 12)
        months.append(f"{MONTHS[m-1]}. {y}")
    return months

def get_month_weeks(month_str: str) -> List[Tuple[str, date, date]]:
    month_name, year_s = month_str.split(". ")
    year = int(year_s)
    month = MONTHS.index(month_name) + 1
    first_day = date(year, month, 1)
    last_day = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year+1, 1, 1) - timedelta(days=1)
    weeks = []
    current = first_day
    week_num = 1
    while current <= last_day:
        # Find the Monday of the week containing current
        monday = current - timedelta(days=current.weekday())  # weekday() 0=Mon, 6=Sun
        sunday = monday + timedelta(days=6)
        # Clip to the month
        start = max(monday, first_day)
        end = min(sunday, last_day)
        if start <= end:
            label = f"Woche {week_num}"
            weeks.append((label, start, end))
            week_num += 1
        current = sunday + timedelta(days=1)
    return weeks

def get_week_days(week_start: date, week_end: date) -> List[str]:
    days = []
    current = week_start
    while current <= week_end:
        days.append(f"{DAYS[current.weekday()]}. {current.strftime('%d.%m.')}")
        current += timedelta(days=1)
    return days

# -------------------------
# Poll persistence helpers
# -------------------------
def create_poll_record(poll_id: str):
    db_execute("INSERT OR REPLACE INTO polls(id, created_at) VALUES (?, ?)", (poll_id, datetime.now(timezone.utc).isoformat()))

def add_option(poll_id: str, option_text: str, author_id: int = None):
    created_at = datetime.now(timezone.utc).isoformat()
    db_execute("INSERT INTO options(poll_id, option_text, created_at, author_id) VALUES (?, ?, ?, ?)",
               (poll_id, option_text, created_at, author_id))
    rows = db_execute("SELECT id FROM options WHERE poll_id = ? AND option_text = ? ORDER BY id DESC LIMIT 1",
                      (poll_id, option_text), fetch=True)
    return rows[-1][0] if rows else None

def get_options(poll_id: str):
    return db_execute("SELECT id, option_text, created_at, author_id FROM options WHERE poll_id = ? ORDER BY id ASC",
                      (poll_id,), fetch=True) or []

def get_user_options(poll_id: str, user_id: int):
    return db_execute("SELECT id, option_text, created_at FROM options WHERE poll_id = ? AND author_id = ? ORDER BY id ASC",
                      (poll_id, user_id), fetch=True) or []

def add_vote(poll_id: str, option_id: int, user_id: int):
    try:
        db_execute("INSERT OR IGNORE INTO votes(poll_id, option_id, user_id) VALUES (?, ?, ?)",
                   (poll_id, option_id, user_id))
    except Exception:
        log.exception("add_vote failed")

def remove_vote(poll_id: str, option_id: int, user_id: int):
    db_execute("DELETE FROM votes WHERE poll_id = ? AND option_id = ? AND user_id = ?",
               (poll_id, option_id, user_id))

def get_votes_for_poll(poll_id: str):
    return db_execute("SELECT option_id, user_id FROM votes WHERE poll_id = ?", (poll_id,), fetch=True) or []

def persist_availability(poll_id: str, user_id: int, slots: list):
    db_execute("DELETE FROM availability WHERE poll_id = ? AND user_id = ?", (poll_id, user_id))
    if slots:
        db_execute("INSERT OR IGNORE INTO availability(poll_id, user_id, slot) VALUES (?, ?, ?)",
                   [(poll_id, user_id, s) for s in slots], many=True)

def get_availability_for_poll(poll_id: str):
    return db_execute("SELECT user_id, slot FROM availability WHERE poll_id = ?", (poll_id,), fetch=True) or []

def get_options_since(poll_id: str, since_dt: datetime):
    rows = db_execute("SELECT option_text, created_at FROM options WHERE poll_id = ? AND created_at >= ? ORDER BY created_at ASC",
                      (poll_id, since_dt.isoformat()), fetch=True)
    return rows or []

# -------------------------
# Matching & embed generation
# -------------------------
def compute_matches_for_poll_from_db(poll_id: str):
    options = get_options(poll_id)
    votes = get_votes_for_poll(poll_id)
    votes_map = {}
    for opt_id, uid in votes:
        votes_map.setdefault(opt_id, []).append(uid)
    availability_rows = get_availability_for_poll(poll_id)
    avail_map = {}
    for uid, slot in availability_rows:
        avail_map.setdefault(uid, set()).add(slot)
    results = {}
    for opt_id, opt_text, _created, _author in options:
        voters = votes_map.get(opt_id, [])
        if len(voters) < 2:
            continue
        slot_to_users = {}
        for u in voters:
            for s in avail_map.get(u, set()):
                slot_to_users.setdefault(s, []).append(u)
        common_slots = []
        for s, users in slot_to_users.items():
            if len(users) >= 2:
                common_slots.append({"slot": s, "users": users})
        if common_slots:
            max_count = max(len(info["users"]) for info in common_slots)
            best = [info for info in common_slots if len(info["users"]) == max_count]
            results[opt_text] = best
    return results

def get_last_posted_matches(poll_id: str):
    rows = db_execute("SELECT matches FROM last_posted_matches WHERE poll_id = ?", (poll_id,), fetch=True)
    if rows:
        import json
        return json.loads(rows[0][0])
    return {}

def set_last_posted_matches(poll_id: str, matches: dict):
    import json
    matches_str = json.dumps(matches)
    now = datetime.now(timezone.utc).isoformat()
    db_execute("INSERT OR REPLACE INTO last_posted_matches(poll_id, matches, updated_at) VALUES (?, ?, ?)",
               (poll_id, matches_str, now))

def get_last_posted_weekly_matches(poll_id: str):
    rows = db_execute("SELECT matches FROM last_posted_weekly_matches WHERE poll_id = ?", (poll_id,), fetch=True)
    if rows:
        import json
        return json.loads(rows[0][0])
    return {}

def set_last_posted_weekly_matches(poll_id: str, matches: dict):
    import json
    matches_str = json.dumps(matches)
    now = datetime.now(timezone.utc).isoformat()
    db_execute("INSERT OR REPLACE INTO last_posted_weekly_matches(poll_id, matches, updated_at) VALUES (?, ?, ?)",
               (poll_id, matches_str, now))

def generate_poll_embed_from_db(poll_id: str, guild: Optional[discord.Guild] = None):
    options = get_options(poll_id)
    votes = get_votes_for_poll(poll_id)
    votes_map = {}
    for opt_id, uid in votes:
        votes_map.setdefault(opt_id, []).append(uid)
    embed = discord.Embed(
        title="📋 Worauf hast du diese Woche Lust?",
        description="Gib eigene Ideen ein, stimme ab oder trage deine Zeiten ein!"
        # Removed timestamp
    )
    for opt_id, opt_text, _created, author_id in options:
        voters = votes_map.get(opt_id, [])
        count = len(voters)
        header = f"🗳️ {count} Stimme" if count == 1 else f"🗳️ {count} Stimmen"
        if voters:
            names = [user_display_name(guild, uid) for uid in voters]
            if len(names) > 10:
                shown = names[:10]
                remaining = len(names) - 10
                names_line = ", ".join(shown) + f", und {remaining} weitere..."
            else:
                names_line = ", ".join(names)
            value = header + "\n👥 " + names_line
        else:
            value = header + "\n👥 Keine Stimmen"
        if len(voters) >= 2:
            avail_rows = get_availability_for_poll(poll_id)
            slot_map = {}
            for uid, slot in avail_rows:
                if uid in voters:
                    slot_map.setdefault(slot, []).append(uid)
            common = [(s, ulist) for s, ulist in slot_map.items() if len(ulist) >= 2]
            if common:
                max_count = max(len(ulist) for (_, ulist) in common)
                best = [(s, ulist) for (s, ulist) in common if len(ulist) == max_count]
                lines = []
                for s, ulist in best:
                    try:
                        day, hour_s = s.split("-")
                        hour = int(hour_s)
                        timestr = slot_label_range(day, hour)
                    except Exception:
                        timestr = s
                    names = [user_display_name(guild, u) for u in ulist]
                    lines.append(f"{timestr}: {', '.join(names)}")
                value += "\n✅ Gemeinsame Zeit (beliebteste):\n" + "\n".join(lines)
        embed.add_field(name=opt_text or "(ohne Titel)", value=value, inline=False)
    return embed

def generate_quarterly_poll_embed_from_db(poll_id: str, guild: Optional[discord.Guild] = None):
    # Similar to weekly, but adjusted for quarterly
    options = get_options(poll_id)
    votes = get_votes_for_poll(poll_id)
    votes_map = {}
    for opt_id, uid in votes:
        votes_map.setdefault(opt_id, []).append(uid)
    quarter_start = get_current_quarter_start()
    embed = discord.Embed(
        title=f"📋 Quartalsumfrage Q{(quarter_start.month-1)//3 + 1} {quarter_start.year}",
        description="Gib eigene Ideen ein, stimme ab oder trage deine verfügbaren Tage ein!"
    )
    for opt_id, opt_text, _created, author_id in options:
        voters = votes_map.get(opt_id, [])
        count = len(voters)
        header = f"🗳️ {count} Stimme" if count == 1 else f"🗳️ {count} Stimmen"
        if voters:
            names = [user_display_name(guild, uid) for uid in voters]
            if len(names) > 10:
                shown = names[:10]
                remaining = len(names) - 10
                names_line = ", ".join(shown) + f", und {remaining} weitere..."
            else:
                names_line = ", ".join(names)
            value = header + "\n👥 " + names_line
        else:
            value = header + "\n👥 Keine Stimmen"
        # For quarterly, matches based on days
        if len(voters) >= 2:
            avail_rows = get_availability_for_poll(poll_id)
            slot_map = {}
            for uid, slot in avail_rows:
                if uid in voters:
                    slot_map.setdefault(slot, []).append(uid)
            common = [(s, ulist) for s, ulist in slot_map.items() if len(ulist) >= 2]
            if common:
                max_count = max(len(ulist) for (_, ulist) in common)
                best = [(s, ulist) for (s, ulist) in common if len(ulist) == max_count]
                lines = []
                for s, ulist in best:
                    names = [user_display_name(guild, u) for u in ulist]
                    lines.append(f"{s}: {', '.join(names)}")
                value += "\n✅ Gemeinsame Tage (beliebteste):\n" + "\n".join(lines)
        embed.add_field(name=opt_text or "(ohne Titel)", value=value, inline=False)
    return embed

# -------------------------
# In-memory temporary storages
# -------------------------
temp_selections: Dict[str, Dict[int, Set[str]]] = {}
create_event_temp_storage: Dict[str, Dict] = {}

# -------------------------
# UI classes (defined before use)
# -------------------------

# Suggest / AddOption
class SuggestModal(discord.ui.Modal, title="Neue Idee hinzufügen"):
    idea = discord.ui.TextInput(label="Deine Idee", placeholder="z. B. Minecraft zocken", max_length=100)
    def __init__(self, poll_id: str):
        super().__init__(title="Neue Idee hinzufügen")
        self.poll_id = poll_id
    async def on_submit(self, interaction: discord.Interaction):
        text = str(self.idea.value).strip()
        if not text:
            try:
                await interaction.response.send_message("Leere Idee verworfen.", ephemeral=True)
            except Exception:
                pass
            return
        add_option(self.poll_id, text, author_id=interaction.user.id)
        try:
            # best-effort update nearby poll message
            if interaction.channel:
                async for msg in interaction.channel.history(limit=200):
                    if msg.author == bot.user and msg.embeds:
                        em = msg.embeds[0]
                        if "Worauf" in em.title or "Quartalsumfrage" in em.title:
                            embed = generate_poll_embed_from_db(self.poll_id, interaction.guild) if "Worauf" in em.title else generate_quarterly_poll_embed_from_db(self.poll_id, interaction.guild)
                            try:
                                bot.add_view(PollView(self.poll_id) if "Worauf" in em.title else QuarterlyPollView(self.poll_id))
                            except Exception:
                                pass
                            await msg.edit(embed=embed, view=PollView(self.poll_id) if "Worauf" in em.title else QuarterlyPollView(self.poll_id))
                            break
        except Exception:
            log.exception("Best-effort update failed")
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            pass

class AddOptionButton(discord.ui.Button):
    def __init__(self, poll_id: str):
        super().__init__(label="📝 Idee hinzufügen", style=discord.ButtonStyle.success, custom_id=f"addopt:{poll_id}")
        self.poll_id = poll_id
    async def callback(self, interaction: discord.Interaction):
        try:
            await interaction.response.send_modal(SuggestModal(self.poll_id))
        except Exception:
            log.exception("Failed to open SuggestModal")

# Availability grid and controls
class DaySelectButton(discord.ui.Button):
    def __init__(self, poll_id: str, day_index: int, selected: bool = False):
        label = f"{DAYS[day_index]}."
        style = discord.ButtonStyle.success if selected else discord.ButtonStyle.secondary
        custom_id = f"day:{poll_id}:{day_index}"
        super().__init__(label=label, style=style, custom_id=custom_id)
        self.poll_id = poll_id
        self.day_index = day_index
    async def callback(self, interaction: discord.Interaction):
        new_view = AvailabilityDayView(self.poll_id, day_index=self.day_index, for_user=interaction.user.id)
        try:
            await interaction.response.edit_message(view=new_view)
        except Exception:
            pass

class HourButton(discord.ui.Button):
    def __init__(self, poll_id: str, day: str, hour: int):
        label = slot_label_range(day, hour)
        custom_id = f"hour:{poll_id}:{day}:{hour}"
        super().__init__(label=label, style=discord.ButtonStyle.secondary, custom_id=custom_id)
        self.poll_id = poll_id
        self.day = day
        self.hour = hour
        self.slot = f"{day}-{hour}"
    async def callback(self, interaction: discord.Interaction):
        uid = interaction.user.id
        _tmp = temp_selections.setdefault(self.poll_id, {})
        user_tmp = _tmp.setdefault(uid, set())
        if self.slot in user_tmp:
            user_tmp.remove(self.slot)
        else:
            user_tmp.add(self.slot)
        day_index = getattr(self.view, "day_index", 0)
        new_view = AvailabilityDayView(self.poll_id, day_index=day_index, for_user=uid)
        try:
            await interaction.response.edit_message(view=new_view)
        except Exception:
            pass

class SubmitButton(discord.ui.Button):
    def __init__(self, poll_id: str):
        super().__init__(label="✅ Absenden", style=discord.ButtonStyle.success, custom_id=f"submit:{poll_id}")
        self.poll_id = poll_id
    async def callback(self, interaction: discord.Interaction):
        uid = interaction.user.id
        user_tmp = temp_selections.get(self.poll_id, {}).get(uid, set())
        persist_availability(self.poll_id, uid, list(user_tmp))
        if self.poll_id in temp_selections and uid in temp_selections[self.poll_id]:
            temp_selections[self.poll_id].pop(uid, None)
        try:
            await interaction.response.edit_message(view=AvailabilityDayView(self.poll_id, day_index=getattr(self.view, "day_index", 0), for_user=uid))
        except Exception:
            try:
                await interaction.response.defer(ephemeral=True)
            except Exception:
                pass
        # Add confirmation message
        try:
            await interaction.followup.send("✅ Zeiten gespeichert!", ephemeral=True)
        except Exception:
            pass

class RemovePersistedButton(discord.ui.Button):
    def __init__(self, poll_id: str):
        super().__init__(label="🗑️ Gespeicherte Zeit löschen", style=discord.ButtonStyle.danger, custom_id=f"removepersist:{poll_id}")
        self.poll_id = poll_id
    async def callback(self, interaction: discord.Interaction):
        uid = interaction.user.id
        db_execute("DELETE FROM availability WHERE poll_id = ? AND user_id = ?", (self.poll_id, uid))
        if self.poll_id in temp_selections:
            temp_selections[self.poll_id].pop(uid, None)
        try:
            await interaction.response.edit_message(view=AvailabilityDayView(self.poll_id, day_index=getattr(self.view, "day_index", 0), for_user=uid))
        except Exception:
            try:
                await interaction.response.defer(ephemeral=True)
            except Exception:
                pass

class AvailabilityDayView(discord.ui.View):
    def __init__(self, poll_id: str, day_index: int = 0, for_user: int = None):
        super().__init__(timeout=None)
        self.poll_id = poll_id
        self.day_index = day_index
        self.for_user = for_user
        if for_user is not None:
            pst = temp_selections.setdefault(poll_id, {})
            if for_user not in pst:
                persisted = db_execute("SELECT slot FROM availability WHERE poll_id = ? AND user_id = ?", (poll_id, for_user), fetch=True)
                pst[for_user] = set(r[0] for r in persisted)
        day_rows = (len(DAYS) + 5 - 1) // 5
        for idx in range(len(DAYS)):
            btn = DaySelectButton(poll_id, idx, selected=(idx == day_index))
            btn.row = idx // 5
            self.add_item(btn)
        day = DAYS[day_index]
        uid = for_user
        user_temp = temp_selections.get(poll_id, {}).get(uid, set())
        for i, hour in enumerate(HOURS):
            btn = HourButton(poll_id, day, hour)
            btn.row = day_rows + (i // 5)
            slot = f"{day}-{hour}"
            selected = (slot in user_temp)
            if selected:
                btn.style = discord.ButtonStyle.success
            else:
                btn.style = discord.ButtonStyle.secondary
            self.add_item(btn)
        last_hour_row = day_rows + ((len(HOURS) - 1) // 5)
        controls_row = min(4, last_hour_row + 1)
        submit = SubmitButton(poll_id)
        submit.row = controls_row
        remove = RemovePersistedButton(poll_id)
        remove.row = controls_row
        self.add_item(submit)
        self.add_item(remove)

# Quarterly availability - updated for single message navigation
class MonthSelectButton(discord.ui.Button):
    def __init__(self, poll_id: str, month_index: int, months: list):
        label = months[month_index]
        custom_id = f"month:{poll_id}:{month_index}"
        super().__init__(label=label, style=discord.ButtonStyle.secondary, custom_id=custom_id)
        self.poll_id = poll_id
        self.month_index = month_index
        self.months = months
    async def callback(self, interaction: discord.Interaction):
        # Edit message to add weeks for selected month
        month_str = self.months[self.month_index]
        weeks = get_month_weeks(month_str)
        new_view = QuarterlyAvailabilityView(self.poll_id, selected_month=self.month_index, months=self.months, weeks=weeks)
        embed = discord.Embed(
            title="🗓️ Quartals-Verfügbarkeit auswählen",
            description="Wähle Monate und Wochen des Quartals aus.",
            color=discord.Color.green()
        )
        try:
            await interaction.response.edit_message(embed=embed, view=new_view)
        except Exception:
            pass

class WeekSelectButton(discord.ui.Button):
    def __init__(self, poll_id: str, week_index: int, weeks: list):
        label = weeks[week_index][0]
        custom_id = f"week:{poll_id}:{week_index}"
        super().__init__(label=label, style=discord.ButtonStyle.secondary, custom_id=custom_id)
        self.poll_id = poll_id
        self.week_index = week_index
        self.weeks = weeks
    async def callback(self, interaction: discord.Interaction):
        # Edit message to add days for selected week
        months = self.view.months if hasattr(self.view, 'months') else []
        selected_month = self.view.selected_month if hasattr(self.view, 'selected_month') else None
        weeks = self.view.weeks if hasattr(self.view, 'weeks') else []
        _, week_start, week_end = self.weeks[self.week_index]
        days = get_week_days(week_start, week_end)
        new_view = QuarterlyAvailabilityView(self.poll_id, selected_month=selected_month, months=months, weeks=weeks, selected_week=self.week_index, days=days)
        # Set styles for day buttons based on user selections
        uid = interaction.user.id
        user_tmp = temp_selections.get(self.poll_id, {}).get(uid, set())
        if not user_tmp:
            persisted = db_execute("SELECT slot FROM availability WHERE poll_id = ? AND user_id = ?", (self.poll_id, uid), fetch=True)
            user_tmp = set(r[0] for r in persisted) if persisted else set()
        for item in new_view.children:
            if isinstance(item, DayAvailButton):
                if item.day in user_tmp:
                    item.style = discord.ButtonStyle.success
                else:
                    item.style = discord.ButtonStyle.secondary
        embed = discord.Embed(
            title="🗓️ Quartals-Verfügbarkeit auswählen",
            description="Wähle Tage der Woche aus.",
            color=discord.Color.green()
        )
        try:
            await interaction.response.edit_message(embed=embed, view=new_view)
        except Exception:
            pass

class DayAvailButton(discord.ui.Button):
    def __init__(self, poll_id: str, day: str):
        super().__init__(label=day, style=discord.ButtonStyle.secondary)
        self.poll_id = poll_id
        self.day = day
    async def callback(self, interaction: discord.Interaction):
        uid = interaction.user.id
        _tmp = temp_selections.setdefault(self.poll_id, {})
        user_tmp = _tmp.setdefault(uid, set())
        if self.day in user_tmp:
            user_tmp.remove(self.day)
        else:
            user_tmp.add(self.day)
        # Rebuild view with updated state
        months = self.view.months if hasattr(self.view, 'months') else []
        selected_month = self.view.selected_month if hasattr(self.view, 'selected_month') else None
        weeks = self.view.weeks if hasattr(self.view, 'weeks') else []
        selected_week = self.view.selected_week if hasattr(self.view, 'selected_week') else None
        days = self.view.days if hasattr(self.view, 'days') else []
        new_view = QuarterlyAvailabilityView(self.poll_id, selected_month=selected_month, months=months, weeks=weeks, selected_week=selected_week, days=days)
        # Update the styles for all day buttons based on user_tmp
        for item in new_view.children:
            if isinstance(item, DayAvailButton):
                if item.day in user_tmp:
                    item.style = discord.ButtonStyle.success
                else:
                    item.style = discord.ButtonStyle.secondary
        embed = discord.Embed(
            title="🗓️ Quartals-Verfügbarkeit auswählen",
            description="Wähle Tage der Woche aus.",
            color=discord.Color.green()
        )
        try:
            await interaction.response.edit_message(embed=embed, view=new_view)
        except Exception:
            pass

class QuarterlySubmitButton(discord.ui.Button):
    def __init__(self, poll_id: str):
        super().__init__(label="✅ Absenden", style=discord.ButtonStyle.success)
        self.poll_id = poll_id
    async def callback(self, interaction: discord.Interaction):
        uid = interaction.user.id
        user_tmp = temp_selections.get(self.poll_id, {}).get(uid, set())
        persist_availability(self.poll_id, uid, list(user_tmp))
        if self.poll_id in temp_selections and uid in temp_selections[self.poll_id]:
            temp_selections[self.poll_id].pop(uid, None)
        try:
            await interaction.response.send_message("✅ Tage gespeichert!", ephemeral=True)
        except Exception:
            pass

class QuarterlyAvailabilityView(discord.ui.View):
    def __init__(self, poll_id: str, selected_month: int = None, months: list = None, weeks: list = None, selected_week: int = None, days: list = None):
        super().__init__(timeout=None)
        self.poll_id = poll_id
        self.selected_month = selected_month
        self.months = months or get_quarter_months(get_current_quarter_start())
        self.weeks = weeks or []
        self.selected_week = selected_week
        self.days = days or []
        # Add month label and buttons
        if self.months:
            self.add_item(discord.ui.Button(label="Monate", style=discord.ButtonStyle.secondary, disabled=True))
            for i in range(3):
                btn = MonthSelectButton(poll_id, i, self.months)
                if selected_month is not None and i == selected_month:
                    btn.style = discord.ButtonStyle.success
                self.add_item(btn)
        # Add week label and buttons if month selected
        if weeks:
            self.add_item(discord.ui.Button(label="Wochen", style=discord.ButtonStyle.secondary, disabled=True))
            for i, (label, _, _) in enumerate(weeks):
                btn = WeekSelectButton(poll_id, i, weeks)
                if selected_week is not None and i == selected_week:
                    btn.style = discord.ButtonStyle.success
                self.add_item(btn)
        # Add day label and buttons if week selected
        if days:
            self.add_item(discord.ui.Button(label="Tage", style=discord.ButtonStyle.secondary, disabled=True))
            for day in days:
                btn = DayAvailButton(poll_id, day)
                uid = None  # Can't get uid here, handle in callback
                self.add_item(btn)
        # Add submit button
        submit = QuarterlySubmitButton(poll_id)
        self.add_item(submit)

class PollView(discord.ui.View):
    def __init__(self, poll_id: str):
        super().__init__(timeout=None)
        self.poll_id = poll_id
        options = get_options(poll_id)
        for opt_id, opt_text, _created, author_id in options:
            try:
                self.add_item(PollButton(poll_id, opt_id, opt_text))
            except Exception:
                pass
        try:
            self.add_item(AddOptionButton(poll_id))
        except Exception:
            pass
        try:
            self.add_item(AddAvailabilityButton(poll_id))
        except Exception:
            pass
        try:
            self.add_item(CreateEventButton(poll_id))
        except Exception:
            pass
        try:
            self.add_item(OpenEditOwnIdeasButton(poll_id))
        except Exception:
            pass

class PollButton(discord.ui.Button):
    def __init__(self, poll_id: str, option_id: int, option_text: str):
        super().__init__(label=option_text, style=discord.ButtonStyle.primary, custom_id=f"poll:{poll_id}:{option_id}")
        self.poll_id = poll_id
        self.option_id = option_id
    async def callback(self, interaction: discord.Interaction):
        uid = interaction.user.id
        rows = db_execute("SELECT 1 FROM votes WHERE poll_id = ? AND option_id = ? AND user_id = ?", (self.poll_id, self.option_id, uid), fetch=True)
        if rows:
            remove_vote(self.poll_id, self.option_id, uid)
        else:
            add_vote(self.poll_id, self.option_id, uid)
        embed = generate_poll_embed_from_db(self.poll_id, interaction.guild)
        try:
            new_view = PollView(self.poll_id)
            bot.add_view(new_view)
            await interaction.response.edit_message(embed=embed, view=new_view)
        except Exception:
            try:
                await interaction.response.edit_message(embed=embed)
            except Exception:
                pass

class AddAvailabilityButton(discord.ui.Button):
    def __init__(self, poll_id: str):
        super().__init__(label="🗓️ Verfügbarkeit hinzufügen", style=discord.ButtonStyle.success, custom_id=f"avail:{poll_id}")
        self.poll_id = poll_id
    async def callback(self, interaction: discord.Interaction):
        try:
            view = AvailabilityDayView(self.poll_id, for_user=interaction.user.id)
            embed = discord.Embed(
                title="🗓️ Verfügbarkeit auswählen",
                description="Wähle Tage und Zeiten aus.",
                color=discord.Color.green()
            )
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        except Exception:
            log.exception("Failed opening AvailabilityDayView")

# Edit-own-ideas UI
class DeleteOwnOptionButtonEphemeral(discord.ui.Button):
    def __init__(self, poll_id: str, option_id: int, option_text: str, user_id: int):
        super().__init__(label="✖️", style=discord.ButtonStyle.danger)
        self.poll_id = poll_id
        self.option_id = option_id
        self.option_text = option_text
        self.user_id = user_id
    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            try:
                await interaction.response.send_message("❌ Nur du kannst diese Idee hier löschen.", ephemeral=True)
            except Exception:
                pass
            return
        db_execute("DELETE FROM options WHERE id = ?", (self.option_id,))
        db_execute("DELETE FROM votes WHERE option_id = ?", (self.option_id,))
        # Removed confirmation message
        try:
            if interaction.channel:
                async for msg in interaction.channel.history(limit=200):
                    if msg.author == bot.user and msg.embeds:
                        em = msg.embeds[0]
                        if "Worauf" in em.title or "Quartalsumfrage" in em.title:
                            embed = generate_poll_embed_from_db(self.poll_id, interaction.guild) if "Worauf" in em.title else generate_quarterly_poll_embed_from_db(self.poll_id, interaction.guild)
                            try:
                                bot.add_view(PollView(self.poll_id) if "Worauf" in em.title else QuarterlyPollView(self.poll_id))
                            except Exception:
                                pass
                            await msg.edit(embed=embed, view=PollView(self.poll_id) if "Worauf" in em.title else QuarterlyPollView(self.poll_id))
                            break
        except Exception:
            log.exception("Failed best-effort poll update on delete")

class EditOwnIdeasView(discord.ui.View):
    def __init__(self, poll_id: str, user_id: int):
        super().__init__(timeout=None)
        self.poll_id = poll_id
        self.user_id = user_id
        user_opts = get_user_options(poll_id, user_id)
        if not user_opts:
            info = discord.ui.Button(label="Du hast noch keine eigenen Ideen.", style=discord.ButtonStyle.secondary, disabled=True)
            self.add_item(info)
        else:
            for opt_id, opt_text, created in user_opts:
                label = opt_text if len(opt_text) <= 80 else opt_text[:77] + "..."
                display_btn = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary, disabled=True)
                self.add_item(display_btn)
                del_btn = DeleteOwnOptionButtonEphemeral(poll_id, opt_id, opt_text, user_id)
                self.add_item(del_btn)

class OpenEditOwnIdeasButton(discord.ui.Button):
    def __init__(self, poll_id: str):
        super().__init__(label="⚙️", style=discord.ButtonStyle.secondary, custom_id=f"edit:{poll_id}")
        self.poll_id = poll_id
    async def callback(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        user_opts = get_user_options(self.poll_id, user_id)
        if not user_opts:
            try:
                await interaction.response.send_message("ℹ️ Du hast noch keine eigenen Ideen in dieser Umfrage.", ephemeral=True)
            except Exception:
                pass
            return
        view = EditOwnIdeasView(self.poll_id, user_id)
        try:
            await interaction.response.send_message("⚙️ Deine eigenen Ideen (nur für dich sichtbar):", view=view, ephemeral=True)
        except Exception:
            pass

# Match selection and event creation
class MatchSelect(discord.ui.Select):
    def __init__(self, poll_id: str, matches: dict):
        options = []
        self.poll_id = poll_id
        self.matches = matches
        for option_text, infos in matches.items():
            for info in infos:
                slot = info["slot"]
                users = info["users"]
                day, hour_s = slot.split("-")
                hour = int(hour_s)
                time_str = slot_label_range(day, hour)
                user_names = " ".join([user_display_name(None, u) for u in users])
                label = f"{option_text[:50]} | {time_str} | {user_names[:50]}"
                value = f"{option_text}|{slot}"
                options.append(discord.SelectOption(label=label, value=value))
        if options:
            super().__init__(placeholder="Wähle ein Match aus...", options=options)
        else:
            super().__init__(placeholder="Keine Matches verfügbar", options=[], disabled=True)
        self.callback = self.select_match

    async def select_match(self, interaction: discord.Interaction):
        selected = self.values[0] if self.values else None
        if not selected:
            return
        option_text, slot = selected.split("|", 1)
        day, hour_s = slot.split("-")
        hour = int(hour_s)
        date_next = next_date_for_day_short(day)
        start_dt = datetime.combine(date_next, _time(hour, 0))
        end_dt = start_dt + timedelta(hours=1)
        date_str = start_dt.strftime("%d.%m.%Y")
        time_str = f"{hour:02d}:00 - {(hour+1)%24:02d}:00"
        modal = CreateEventModal(self.poll_id, prefill_title=option_text, prefill_date=date_str, prefill_time=time_str)
        try:
            await interaction.response.send_modal(modal)
        except Exception:
            log.exception("Failed to send prefilled CreateEventModal")

class NewEventButton(discord.ui.Button):
    def __init__(self, poll_id: str):
        super().__init__(label="📅 Neues Event erstellen", style=discord.ButtonStyle.primary)
        self.poll_id = poll_id
    async def callback(self, interaction: discord.Interaction):
        modal = CreateEventModal(self.poll_id)
        try:
            await interaction.response.send_modal(modal)
        except Exception:
            log.exception("Failed to send CreateEventModal")

class SelectMatchView(discord.ui.View):
    def __init__(self, poll_id: str, matches: dict):
        super().__init__(timeout=None)
        select = MatchSelect(poll_id, matches)
        self.add_item(select)
        new_btn = NewEventButton(poll_id)
        self.add_item(new_btn)

# Event creation: Single modal with flexible parsing, creates Bot Event only (no Discord Scheduled Event).
class CreateEventModal(discord.ui.Modal, title="Event erstellen"):
    title_field = discord.ui.TextInput(label="Titel", style=discord.TextStyle.short, max_length=100)
    description_field = discord.ui.TextInput(label="Beschreibung", style=discord.TextStyle.long, required=False, max_length=2000)
    date_range_field = discord.ui.TextInput(label="Datumsbereich", style=discord.TextStyle.short, placeholder="01.01.2026 - 02.01.2026", max_length=40)
    time_range_field = discord.ui.TextInput(label="Zeitbereich", style=discord.TextStyle.short, placeholder="18:00 - 20:00", max_length=20)
    location_field = discord.ui.TextInput(label="Ort", style=discord.TextStyle.short, placeholder="#channelname oder Text", max_length=200)

    def __init__(self, poll_id: str, prefill_title: str = "", prefill_date: str = "", prefill_time: str = ""):
        super().__init__(title="Event erstellen")
        self.poll_id = poll_id
        if prefill_title:
            self.title_field.default = prefill_title
        if prefill_date:
            self.date_range_field.default = prefill_date
        if prefill_time:
            self.time_range_field.default = prefill_time

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()  # Close modal
        try:
            title = str(self.title_field.value).strip()
            description = str(self.description_field.value).strip()
            date_range_str = str(self.date_range_field.value).strip()
            time_range_str = str(self.time_range_field.value).strip()
            location = str(self.location_field.value).strip()

            if not title or not date_range_str or not time_range_str:
                try:
                    await interaction.followup.send("Titel, Datumsbereich und Zeitbereich sind erforderlich.", ephemeral=True)
                except Exception:
                    log.exception("Failed to send required fields message")
                return

            start_date, end_date = parse_date_range(date_range_str)
            start_time, end_time = parse_time_range(time_range_str)

            if not start_date or not end_date or not start_time or not end_time:
                try:
                    await interaction.followup.send("Datums-/Zeitbereich konnte nicht geparst werden. Verwende DD.MM.YYYY für Datum und HH:MM für Zeit.", ephemeral=True)
                except Exception:
                    log.exception("Failed to send parsing error")
                return

            tz = ZoneInfo(POST_TIMEZONE)
            start_dt = datetime(start_date.year, start_date.month, start_date.day, start_time.hour, start_time.minute, tzinfo=tz)
            end_dt = datetime(end_date.year, end_date.month, end_date.day, end_time.hour, end_time.minute, tzinfo=tz)

            event_id = datetime.now(tz=ZoneInfo(POST_TIMEZONE)).strftime("%Y%m%dT%H%M%S") + "-" + str(interaction.user.id)
            created_at = datetime.now(timezone.utc).isoformat()
            try:
                db_execute("INSERT INTO created_events(id, poll_id, title, description, start_time, end_time, participants, location, posted_channel_id, posted_message_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           (event_id, self.poll_id, title, description, start_dt.isoformat(), end_dt.isoformat(), "", location, None, None, created_at))
            except Exception:
                log.exception("Failed inserting created_event")
                try:
                    await interaction.followup.send("Fehler beim Speichern des Events.", ephemeral=True)
                except Exception:
                    pass
                return

            # automatically add creator as interested
            try:
                creator_uid = interaction.user.id
                db_execute("INSERT OR IGNORE INTO created_event_rsvps(event_id, user_id) VALUES (?, ?)", (event_id, creator_uid))
            except Exception:
                log.exception("Failed adding creator to RSVPs")

            target_channel = None
            if CREATED_EVENTS_CHANNEL_ID:
                target_channel = bot.get_channel(CREATED_EVENTS_CHANNEL_ID)
            if not target_channel and CHANNEL_ID:
                target_channel = bot.get_channel(CHANNEL_ID)
            if not target_channel and isinstance(interaction.channel, discord.TextChannel):
                target_channel = interaction.channel
            if not target_channel:
                try:
                    await interaction.followup.send("Kein Zielkanal gefunden, um das Event zu posten.", ephemeral=True)
                except Exception:
                    pass
                return

            # Create Embed resembling official Discord Server Event
            embed = discord.Embed(
                title=title,  # Removed calendar icon
                description=description if description else None,  # No description if empty
                color=discord.Color.blue()
                # No timestamp
            )
            embed.set_thumbnail(url=interaction.guild.icon.url if interaction.guild and interaction.guild.icon else None)  # Server logo

            # Normal spacing (removed extra spacer)

            # Grouped Date/Time
            start_str = start_dt.strftime("%d.%m.%y %H:%M")
            end_str = end_dt.strftime("%d.%m.%y %H:%M")
            if start_date == end_date:
                # Same date: "01.01.26 16:00 - 18:00 Uhr"
                date_part = start_dt.strftime("%d.%m.%y")
                time_part_start = start_dt.strftime("%H:%M")
                time_part_end = end_dt.strftime("%H:%M")
                wann_value = f"{date_part} {time_part_start} – {time_part_end} Uhr"
            else:
                wann_value = f"{start_str} – {end_str}"
            embed.add_field(name="Wann", value=wann_value, inline=False)  # Removed clock icon

            # Location
            embed.add_field(name="Ort", value=location or "Nicht angegeben", inline=False)  # Removed location icon

            # RSVP Info
            rows2 = db_execute("SELECT user_id FROM created_event_rsvps WHERE event_id = ?", (event_id,), fetch=True) or []
            user_ids = [r[0] for r in rows2]
            if user_ids:
                names = [user_display_name(interaction.guild, uid) for uid in user_ids]
                embed.add_field(name="✅ Interessiert", value=", ".join(names[:10]) + (f" und {len(names)-10} weitere..." if len(names)>10 else ""), inline=False)
            else:
                embed.add_field(name="✅ Interessiert", value="Noch niemand", inline=False)

            # No footer

            view = EventSignupView(event_id, interaction.user.id)
            try:
                bot.add_view(view)
            except Exception:
                pass
            try:
                sent = await target_channel.send(embed=embed, view=view)
                db_execute("UPDATE created_events SET posted_channel_id = ?, posted_message_id = ? WHERE id = ?", (target_channel.id, sent.id, event_id))
            except Exception:
                log.exception("Failed posting created event to channel")
                try:
                    await interaction.followup.send("Fehler beim Posten des Events.", ephemeral=True)
                except Exception:
                    pass
                return
            if start_dt:
                schedule_reminders_for_created_event(event_id, start_dt, target_channel.id)
        except Exception:
            log.exception("Unhandled error in CreateEventModal.on_submit")
            try:
                await interaction.followup.send("Interner Fehler beim Verarbeiten des Formulars.", ephemeral=True)
            except Exception:
                pass

# Simplified event creation buttons
class CreateEventButton(discord.ui.Button):
    def __init__(self, poll_id: str):
        super().__init__(label="📅 Event erstellen", style=discord.ButtonStyle.success, custom_id=f"createevent:{poll_id}")
        self.poll_id = poll_id
    async def callback(self, interaction: discord.Interaction):
        matches = compute_matches_for_poll_from_db(self.poll_id)
        if matches:
            view = SelectMatchView(self.poll_id, matches)
            embed = discord.Embed(
                title="🎯 Event aus Match erstellen",
                description="Wähle ein bestehendes Match aus, um ein Event vorzubefüllt zu erhalten, oder erstelle ein neues.",
                color=discord.Color.blue()
            )
            try:
                await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
            except Exception:
                log.exception("Failed to send SelectMatchView")
        else:
            # No matches, directly open modal
            modal = CreateEventModal(self.poll_id)
            try:
                await interaction.response.send_modal(modal)
            except Exception:
                log.exception("Failed to send CreateEventModal")

# Quarterly poll view
class QuarterlyPollView(discord.ui.View):
    def __init__(self, poll_id: str):
        super().__init__(timeout=None)
        self.poll_id = poll_id
        options = get_options(poll_id)
        for opt_id, opt_text, _created, author_id in options:
            try:
                self.add_item(QuarterlyPollButton(poll_id, opt_id, opt_text))
            except Exception:
                pass
        try:
            self.add_item(AddOptionButton(poll_id))
        except Exception:
            pass
        try:
            self.add_item(QuarterlyAddAvailabilityButton(poll_id))
        except Exception:
            pass
        try:
            self.add_item(CreateEventButton(poll_id))
        except Exception:
            pass
        try:
            self.add_item(OpenEditOwnIdeasButton(poll_id))
        except Exception:
            pass

class QuarterlyPollButton(discord.ui.Button):
    def __init__(self, poll_id: str, option_id: int, option_text: str):
        super().__init__(label=option_text, style=discord.ButtonStyle.primary, custom_id=f"qpoll:{poll_id}:{option_id}")
        self.poll_id = poll_id
        self.option_id = option_id
    async def callback(self, interaction: discord.Interaction):
        uid = interaction.user.id
        rows = db_execute("SELECT 1 FROM votes WHERE poll_id = ? AND option_id = ? AND user_id = ?", (self.poll_id, self.option_id, uid), fetch=True)
        if rows:
            remove_vote(self.poll_id, self.option_id, uid)
        else:
            add_vote(self.poll_id, self.option_id, uid)
        embed = generate_quarterly_poll_embed_from_db(self.poll_id, interaction.guild)
        try:
            new_view = QuarterlyPollView(self.poll_id)
            bot.add_view(new_view)
            await interaction.response.edit_message(embed=embed, view=new_view)
        except Exception:
            try:
                await interaction.response.edit_message(embed=embed)
            except Exception:
                pass

class QuarterlyAddAvailabilityButton(discord.ui.Button):
    def __init__(self, poll_id: str):
        super().__init__(label="🗓️ Verfügbarkeit hinzufügen", style=discord.ButtonStyle.success, custom_id=f"qavail:{poll_id}")
        self.poll_id = poll_id
    async def callback(self, interaction: discord.Interaction):
        try:
            view = QuarterlyAvailabilityView(self.poll_id)
            embed = discord.Embed(
                title="🗓️ Quartals-Verfügbarkeit auswählen",
                description="Wähle Monate des Quartals aus.",
                color=discord.Color.green()
            )
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        except Exception:
            log.exception("Failed opening QuarterlyAvailabilityView")

# -------------------------
# Removed: All step1/step2, Match/New buttons, create_step2_modal_instance, EditParticipantsModal, EditDescriptionLocationModal, FinalizeEventView etc.
# -------------------------

async def build_created_event_embed(event_id: str, guild: Optional[discord.Guild] = None) -> discord.Embed:
    rows = db_execute("SELECT title, description, start_time, end_time, participants, location FROM created_events WHERE id = ?", (event_id,), fetch=True) or []
    if not rows:
        return discord.Embed(title="Event", description="(Details fehlen)", color=discord.Color.dark_grey())
    title, description, start_iso, end_iso, participants_text, location = rows[0]
    embed = discord.Embed(
        title=title,  # Removed calendar icon
        description=description if description else None,
        color=discord.Color.blue()
    )
    embed.set_thumbnail(url=guild.icon.url if guild and guild.icon else None)
    # Normal spacing
    if start_iso:
        try:
            start_dt = datetime.fromisoformat(start_iso)
            end_dt = datetime.fromisoformat(end_iso) if end_iso else None
            if end_dt and start_dt.date() == end_dt.date():
                date_part = start_dt.strftime("%d.%m.%y")
                time_part_start = start_dt.strftime("%H:%M")
                time_part_end = end_dt.strftime("%H:%M")
                wann_value = f"{date_part} {time_part_start} – {time_part_end} Uhr"
            else:
                start_str = start_dt.strftime("%d.%m.%y %H:%M")
                end_str = end_dt.strftime("%d.%m.%y %H:%M") if end_dt else ""
                wann_value = f"{start_str} – {end_str}" if end_str else start_str
            embed.add_field(name="Wann", value=wann_value, inline=False)  # Removed clock icon
        except Exception:
            embed.add_field(name="Wann", value=start_iso, inline=False)
    if location:
        embed.add_field(name="Ort", value=location, inline=False)  # Removed location icon
    rows2 = db_execute("SELECT user_id FROM created_event_rsvps WHERE event_id = ?", (event_id,), fetch=True) or []
    user_ids = [r[0] for r in rows2]
    if user_ids:
        names = [user_display_name(guild, uid) for uid in user_ids]
        embed.add_field(name="✅ Interessiert", value=", ".join(names[:20]) + (f", und {len(names)-20} weitere..." if len(names)>20 else ""), inline=False)
    else:
        embed.add_field(name="✅ Interessiert", value="Keine", inline=False)
    return embed

class EventSignupView(discord.ui.View):
    def __init__(self, event_id: str, user_id: int = None):
        super().__init__(timeout=None)
        self.event_id = event_id
        self.user_id = user_id
        # Check if user is interested
        existing = db_execute("SELECT 1 FROM created_event_rsvps WHERE event_id = ? AND user_id = ?", (event_id, user_id), fetch=True) if user_id else []
        is_interested = bool(existing)
        if is_interested:
            btn = discord.ui.Button(label="✅ Interessiert", style=discord.ButtonStyle.success, custom_id=f"rsvp:{event_id}")
        else:
            btn = discord.ui.Button(label="🔔 Interessiert", style=discord.ButtonStyle.secondary, custom_id=f"rsvp:{event_id}")
        btn.callback = self.toggle_interested
        self.add_item(btn)

    async def toggle_interested(self, interaction: discord.Interaction):
        await interaction.response.defer()  # Defer to avoid "interaction failed"
        uid = interaction.user.id
        try:
            existing = db_execute("SELECT 1 FROM created_event_rsvps WHERE event_id = ? AND user_id = ?", (self.event_id, uid), fetch=True)
            if existing:
                db_execute("DELETE FROM created_event_rsvps WHERE event_id = ? AND user_id = ?", (self.event_id, uid))
            else:
                db_execute("INSERT OR IGNORE INTO created_event_rsvps(event_id, user_id) VALUES (?, ?)", (self.event_id, uid))
        except Exception:
            log.exception("Error toggling RSVP")
        # Update the view and message (no confirmation message)
        try:
            embed = await build_created_event_embed(self.event_id, interaction.guild)
            new_view = EventSignupView(self.event_id, interaction.user.id)
            await interaction.message.edit(embed=embed, view=new_view)
        except Exception:
            log.exception("Failed to update event message after RSVP")

# -------------------------
# Reminders, posting, scheduling, commands (unchanged flow)
# -------------------------
scheduler = AsyncIOScheduler(timezone=ZoneInfo(POST_TIMEZONE))

def _remove_created_event_jobs(event_id: str):
    try:
        scheduler.remove_job(f"created_event_reminder_24_{event_id}")
    except Exception:
        pass
    try:
        scheduler.remove_job(f"created_event_reminder_1_{event_id}")
    except Exception:
        pass

def schedule_reminders_for_created_event(event_id: str, start_dt: datetime, channel_id: int):
    _remove_created_event_jobs(event_id)
    if not start_dt:
        return
    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=ZoneInfo(POST_TIMEZONE))
    t24 = start_dt - timedelta(hours=24)
    t1 = start_dt - timedelta(hours=1)
    now = datetime.now(timezone.utc)
    if t24 > now:
        scheduler.add_job(lambda: bot.loop.create_task(_created_event_reminder_coro(event_id, channel_id, 24)),
                          trigger=DateTrigger(run_date=t24), id=f"created_event_reminder_24_{event_id}", replace_existing=True)
        log.info("Scheduled created-event 24h reminder for %s at %s", event_id, t24.isoformat())
    elif t24 <= now < start_dt:
        bot.loop.create_task(_created_event_reminder_coro(event_id, channel_id, 24))
    if t1 > now:
        scheduler.add_job(lambda: bot.loop.create_task(_created_event_reminder_coro(event_id, channel_id, 1)),
                          trigger=DateTrigger(run_date=t1), id=f"created_event_reminder_1_{event_id}", replace_existing=True)
        log.info("Scheduled created-event 1h reminder for %s at %s", event_id, t1.isoformat())
    elif t1 <= now < start_dt:
        bot.loop.create_task(_created_event_reminder_coro(event_id, channel_id, 1))

async def _created_event_reminder_coro(event_id: str, channel_id: int, hours_before: int):
    ch = bot.get_channel(channel_id)
    if not ch:
        log.info("Reminder: channel %s not found for event %s", channel_id, event_id)
        return
    start_iso = None
    try:
        rows = db_execute("SELECT posted_channel_id, posted_message_id, start_time FROM created_events WHERE id = ?", (event_id,), fetch=True) or []
    except Exception:
        rows = []
        log.exception("DB error fetching created_events for reminder")
    if rows:
        old_ch_id, old_msg_id, start_iso = rows[0]
        if old_ch_id and old_msg_id:
            try:
                old_ch = bot.get_channel(old_ch_id)
                if old_ch:
                    try:
                        old_msg = await old_ch.fetch_message(old_msg_id)
                        try:
                            await old_msg.delete()
                        except discord.NotFound:
                            pass
                        except Exception:
                            log.exception("Failed deleting old created event message during reminder")
                    except discord.NotFound:
                        try:
                            db_execute("UPDATE created_events SET posted_channel_id = NULL, posted_message_id = NULL WHERE id = ?", (event_id,))
                        except Exception:
                            log.exception("Failed clearing posted refs during reminder")
                    except Exception:
                        log.exception("Error fetching old created event message during reminder")
            except Exception:
                log.exception("Failed while handling old created event message during reminder")
    try:
        embed = await build_created_event_embed(event_id, None)
    except Exception:
        log.exception("Failed building created event embed")
        embed = discord.Embed(title="📣 Event", description="Details", color=discord.Color.orange())
    if start_iso:
        try:
            sdt = datetime.fromisoformat(start_iso)
            now_local = datetime.now(sdt.tzinfo or timezone.utc)
            delta = sdt - now_local
            hours_left = int(delta.total_seconds() // 3600)
            new_title = embed.title or "Event"
            embed.title = f"📣 starts in ~{hours_left}h — {new_title}"
        except Exception:
            pass
    view = EventSignupView(event_id)
    try:
        bot.add_view(view)
    except Exception:
        pass
    try:
        sent = await ch.send(embed=embed, view=view)
        try:
            db_execute("UPDATE created_events SET posted_channel_id = ?, posted_message_id = ? WHERE id = ?", (ch.id, sent.id, event_id))
        except Exception:
            log.exception("Failed to persist created event posted ids during reminder")
    except Exception:
        log.exception("Failed to send reminder for created event %s", event_id)

# Posting polls & commands
async def post_poll_to_channel(channel: discord.abc.Messageable):
    poll_id = datetime.now(tz=ZoneInfo(POST_TIMEZONE)).strftime("%Y%m%dT%H%M%S")
    create_poll_record(poll_id)
    embed = generate_poll_embed_from_db(poll_id, channel.guild if isinstance(channel, discord.TextChannel) else None)
    view = PollView(poll_id)
    try:
        bot.add_view(view)
    except Exception:
        pass
    await channel.send(embed=embed, view=view)
    return poll_id

async def post_quarterly_poll_to_channel(channel: discord.abc.Messageable):
    poll_id = datetime.now(tz=ZoneInfo(POST_TIMEZONE)).strftime("%Y%m%dT%H%M%S") + "_quarterly"
    create_poll_record(poll_id)
    embed = generate_quarterly_poll_embed_from_db(poll_id, channel.guild if isinstance(channel, discord.TextChannel) else None)
    view = QuarterlyPollView(poll_id)
    try:
        bot.add_view(view)
    except Exception:
        pass
    await channel.send(embed=embed, view=view)
    return poll_id

@bot.command()
async def startpoll(ctx):
    try:
        poll_id = await post_poll_to_channel(ctx.channel)
        # Removed confirmation message
    except Exception as e:
        log.exception("startpoll failed")
        await ctx.send(f"Fehler beim Erstellen der Umfrage: {e}")

@bot.command()
async def startquarterlypoll(ctx):
    try:
        poll_id = await post_quarterly_poll_to_channel(ctx.channel)
        # Optional: Bestätigungsnachricht, falls gewünscht
    except Exception as e:
        log.exception("startquarterlypoll failed")
        await ctx.send(f"Fehler beim Erstellen der Quartalsumfrage: {e}")

@bot.command()
async def weeklysummary(ctx):
    try:
        await post_weekly_summary_to(ctx.channel)
    except Exception as e:
        log.exception("weeklysummary failed")
        await ctx.send(f"Fehler beim Erstellen der wöchentlichen Zusammenfassung: {e}")

@bot.command()
async def listpolls(ctx, limit: int = 50):
    rows = db_execute("SELECT id, created_at FROM polls ORDER BY created_at DESC LIMIT ?", (limit,), fetch=True)
    if not rows:
        await ctx.send("Keine Polls in der DB gefunden.")
        return
    lines = [f"- {r[0]}  (erstellt: {r[1]})" for r in rows]
    text = "\n".join(lines)
    if len(text) > 1900:
        await ctx.send(file=discord.File(io.BytesIO(text.encode()), filename="polls.txt"))
    else:
        await ctx.send(f"Polls:\n{text}")

# Daily summary & scheduler helpers
def get_last_daily_summary(channel_id: int):
    rows = db_execute("SELECT message_id FROM daily_summaries WHERE channel_id = ?", (channel_id,), fetch=True)
    return rows[0][0] if rows and rows[0][0] is not None else None

def set_last_daily_summary(channel_id: int, message_id: int):
    now = datetime.now(timezone.utc).isoformat()
    db_execute("INSERT OR REPLACE INTO daily_summaries(channel_id, message_id, created_at) VALUES (?, ?, ?)",
               (channel_id, message_id, now))

def get_last_weekly_summary(channel_id: int):
    rows = db_execute("SELECT message_id FROM weekly_summaries WHERE channel_id = ?", (channel_id,), fetch=True)
    return rows[0][0] if rows and rows[0][0] is not None else None

def set_last_weekly_summary(channel_id: int, message_id: int):
    now = datetime.now(timezone.utc).isoformat()
    db_execute("INSERT OR REPLACE INTO weekly_summaries(channel_id, message_id, created_at) VALUES (?, ?, ?)",
               (channel_id, message_id, now))

async def post_daily_summary():
    await bot.wait_until_ready()
    channel = None
    if CHANNEL_ID:
        channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        for g in bot.guilds:
            for ch in g.text_channels:
                try:
                    perms = ch.permissions_for(g.me)
                    if perms.send_messages:
                        channel = ch
                        break
                except Exception:
                    continue
            if channel:
                break
    if not channel:
        log.info("Kein Kanal gefunden für Daily Summary.")
        return
    await post_daily_summary_to(channel)

async def post_daily_summary_to(channel: discord.TextChannel):
    rows = db_execute("SELECT id, created_at FROM polls WHERE id NOT LIKE '%_quarterly' ORDER BY created_at DESC LIMIT 1", fetch=True)
    if not rows:
        return
    poll_id, poll_created = rows[0]
    tz = ZoneInfo(POST_TIMEZONE)
    since = datetime.now(tz=tz) - timedelta(days=1)
    new_options = get_options_since(poll_id, since)
    current_matches = compute_matches_for_poll_from_db(poll_id)
    last_matches = get_last_posted_matches(poll_id)
    new_matches = {}
    for key, infos in current_matches.items():
        if key not in last_matches:
            new_matches[key] = infos
        else:
            # Check if new infos
            last_infos = last_matches[key]
            for info in infos:
                if info not in last_infos:
                    if key not in new_matches:
                        new_matches[key] = []
                    new_matches[key].append(info)
    if (not new_options) and (not new_matches):
        return
    embed = discord.Embed(title="🗓️ Tages-Update: Matches & neue Ideen", color=discord.Color.green(), timestamp=datetime.now(tz=tz))
    if new_options:
        lines = []
        for opt_text, created_at in new_options:
            try:
                t = datetime.fromisoformat(created_at).astimezone(tz)
                tstr = t.strftime("%d.%m. %H:%M")
            except Exception:
                tstr = created_at
            lines.append(f"- {opt_text} (hinzugefügt {tstr})")
        embed.add_field(name="🆕 Neue Ideen", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="🆕 Neue Ideen", value="Keine", inline=False)
    if new_matches:
        for opt_text, infos in new_matches.items():
            lines = []
            for info in infos:
                slot = info["slot"]
                day, hour_s = slot.split("-")
                hour = int(hour_s)
                timestr = slot_label_range(day, hour)
                names = [user_display_name(channel.guild if isinstance(channel, discord.TextChannel) else None, u) for u in info["users"]]
                lines.append(f"{timestr}: {', '.join(names)}")
            embed.add_field(name=f"🤝 Neue Matches — {opt_text}", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="🤝 Neue Matches", value="Keine neuen gemeinsamen Zeiten seit dem letzten Update.", inline=False)
    voter_rows = db_execute("SELECT DISTINCT user_id FROM votes WHERE poll_id = ?", (poll_id,), fetch=True)
    voters = [r[0] for r in voter_rows] if voter_rows else []
    avail_rows = db_execute("SELECT DISTINCT user_id FROM availability WHERE poll_id = ?", (poll_id,), fetch=True)
    has_avail = {r[0] for r in avail_rows} if avail_rows else set()
    voters_no_avail = [uid for uid in voters if uid not in has_avail]
    if voters_no_avail:
        names = [user_display_name(channel.guild if isinstance(channel, discord.TextChannel) else None, uid) for uid in voters_no_avail]
        if len(names) > 30:
            shown = names[:30]
            remaining = len(names) - 30
            names_line = ", ".join(shown) + f", und {remaining} weitere..."
        else:
            names_line = ", ".join(names)
        embed.add_field(name="ℹ️ Abstimmende ohne eingetragene Zeiten", value=names_line, inline=False)
    else:
        embed.add_field(name="ℹ️ Abstimmende ohne eingetragene Zeiten", value="Alle Abstimmenden haben Zeiten eingetragen.", inline=False)
    last_msg_id = get_last_daily_summary(channel.id)
    if last_msg_id:
        try:
            prev = await channel.fetch_message(last_msg_id)
            if prev:
                await prev.delete()
        except discord.NotFound:
            pass
        except Exception:
            log.exception("Failed deleting previous daily summary")
    sent = await channel.send(embed=embed)
    try:
        set_last_daily_summary(channel.id, sent.id)
        set_last_posted_matches(poll_id, current_matches)
    except Exception:
        log.exception("Failed saving daily summary id or last matches")

async def post_weekly_summary():
    await bot.wait_until_ready()
    channel = None
    if QUARTERLY_CHANNEL_ID:
        channel = bot.get_channel(QUARTERLY_CHANNEL_ID)
    if not channel:
        log.info("Kein Quartals-Kanal gefunden für Weekly Summary.")
        return
    await post_weekly_summary_to(channel)

async def post_weekly_summary_to(channel: discord.TextChannel):
    rows = db_execute("SELECT id, created_at FROM polls WHERE id LIKE '%_quarterly' ORDER BY created_at DESC LIMIT 1", fetch=True)
    if not rows:
        return
    poll_id, poll_created = rows[0]
    tz = ZoneInfo(POST_TIMEZONE)
    since = datetime.now(tz=tz) - timedelta(weeks=1)
    new_options = get_options_since(poll_id, since)
    current_matches = compute_matches_for_poll_from_db(poll_id)
    last_matches = get_last_posted_weekly_matches(poll_id)
    new_matches = {}
    for key, infos in current_matches.items():
        if key not in last_matches:
            new_matches[key] = infos
        else:
            # Check if new infos
            last_infos = last_matches[key]
            for info in infos:
                if info not in last_infos:
                    if key not in new_matches:
                        new_matches[key] = []
                    new_matches[key].append(info)
    if (not new_options) and (not new_matches):
        return
    embed = discord.Embed(title="🗓️ Wöchentliches Update: Matches & neue Ideen", color=discord.Color.blue(), timestamp=datetime.now(tz=tz))
    if new_options:
        lines = []
        for opt_text, created_at in new_options:
            try:
                t = datetime.fromisoformat(created_at).astimezone(tz)
                tstr = t.strftime("%d.%m. %H:%M")
            except Exception:
                tstr = created_at
            lines.append(f"- {opt_text} (hinzugefügt {tstr})")
        embed.add_field(name="🆕 Neue Ideen", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="🆕 Neue Ideen", value="Keine", inline=False)
    if new_matches:
        for opt_text, infos in new_matches.items():
            lines = []
            for info in infos:
                slot = info["slot"]
                names = [user_display_name(channel.guild if isinstance(channel, discord.TextChannel) else None, u) for u in info["users"]]
                lines.append(f"{slot}: {', '.join(names)}")
            embed.add_field(name=f"🤝 Neue Matches — {opt_text}", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="🤝 Neue Matches", value="Keine neuen gemeinsamen Tage seit dem letzten Update.", inline=False)
    voter_rows = db_execute("SELECT DISTINCT user_id FROM votes WHERE poll_id = ?", (poll_id,), fetch=True)
    voters = [r[0] for r in voter_rows] if voter_rows else []
    avail_rows = db_execute("SELECT DISTINCT user_id FROM availability WHERE poll_id = ?", (poll_id,), fetch=True)
    has_avail = {r[0] for r in avail_rows} if avail_rows else set()
    voters_no_avail = [uid for uid in voters if uid not in has_avail]
    if voters_no_avail:
        names = [user_display_name(channel.guild if isinstance(channel, discord.TextChannel) else None, uid) for uid in voters_no_avail]
        if len(names) > 30:
            shown = names[:30]
            remaining = len(names) - 30
            names_line = ", ".join(shown) + f", und {remaining} weitere..."
        else:
            names_line = ", ".join(names)
        embed.add_field(name="ℹ️ Abstimmende ohne eingetragene Tage", value=names_line, inline=False)
    else:
        embed.add_field(name="ℹ️ Abstimmende ohne eingetragene Tage", value="Alle Abstimmenden haben Tage eingetragen.", inline=False)
    last_msg_id = get_last_weekly_summary(channel.id)
    if last_msg_id:
        try:
            prev = await channel.fetch_message(last_msg_id)
            if prev:
                await prev.delete()
        except discord.NotFound:
            pass
        except Exception:
            log.exception("Failed deleting previous weekly summary")
    sent = await channel.send(embed=embed)
    try:
        set_last_weekly_summary(channel.id, sent.id)
        set_last_posted_weekly_matches(poll_id, current_matches)
    except Exception:
        log.exception("Failed saving weekly summary id or last matches")

# Scheduler helpers & startup
def job_post_weekly():
    asyncio.create_task(job_post_weekly_coro())

async def job_post_weekly_coro():
    await bot.wait_until_ready()
    channel = None
    if CHANNEL_ID:
        channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        for g in bot.guilds:
            for ch in g.text_channels:
                try:
                    perms = ch.permissions_for(g.me)
                    if perms.send_messages:
                        channel = ch
                        break
                except Exception:
                    continue
            if channel:
                break
    if not channel:
        log.info("Kein Kanal gefunden: bitte CHANNEL_ID setzen oder verwende !startpoll in einem Kanal.")
        return
    try:
        poll_id = await post_poll_to_channel(channel)
        log.info(f"Posted weekly poll {poll_id} to {channel} at {datetime.now(tz=ZoneInfo(POST_TIMEZONE))}")
    except Exception:
        log.exception("Failed posting weekly poll job")

def job_post_quarterly():
    asyncio.create_task(job_post_quarterly_coro())

async def job_post_quarterly_coro():
    await bot.wait_until_ready()
    channel = None
    if QUARTERLY_CHANNEL_ID:
        channel = bot.get_channel(QUARTERLY_CHANNEL_ID)
    if not channel:
        log.info("Kein Quartals-Kanal gefunden.")
        return
    try:
        poll_id = await post_quarterly_poll_to_channel(channel)
        log.info(f"Posted quarterly poll {poll_id} to {channel} at {datetime.now(tz=ZoneInfo(POST_TIMEZONE))}")
    except Exception:
        log.exception("Failed posting quarterly poll job")

def schedule_weekly_post():
    trigger = CronTrigger(day_of_week="sun", hour=12, minute=0, timezone=ZoneInfo(POST_TIMEZONE))
    scheduler.add_job(job_post_weekly, trigger=trigger, id="weekly_poll", replace_existing=True)

def schedule_quarterly_post():
    # Am 1. des Vormonats, z.B. 01.11.2025 für Q1 2026
    now = datetime.now(ZoneInfo(POST_TIMEZONE))
    prev_month = (now.month - 2) % 12 + 1
    year = now.year if now.month > 1 else now.year - 1
    trigger = CronTrigger(day=1, month=prev_month, year=year, hour=12, minute=0, timezone=ZoneInfo(POST_TIMEZONE))
    scheduler.add_job(job_post_quarterly, trigger=trigger, id="quarterly_poll", replace_existing=True)

def schedule_weekly_summary():
    trigger = CronTrigger(day_of_week="mon", hour=9, minute=0, timezone=ZoneInfo(POST_TIMEZONE))
    scheduler.add_job(post_weekly_summary, trigger=trigger, id="weekly_summary", replace_existing=True)

def schedule_daily_summary():
    trigger_morning = CronTrigger(day_of_week="*", hour=9, minute=0, timezone=ZoneInfo(POST_TIMEZONE))
    scheduler.add_job(post_daily_summary, trigger=trigger_morning, id="daily_summary_morning", replace_existing=True)
    trigger_evening = CronTrigger(day_of_week="*", hour=18, minute=0, timezone=ZoneInfo(POST_TIMEZONE))
    scheduler.add_job(post_daily_summary, trigger=trigger_evening, id="daily_summary_evening", replace_existing=True)

async def register_persistent_poll_views_async(batch_delay: float = 0.02):
    rows = db_execute("SELECT id FROM polls", fetch=True) or []
    if not rows:
        return
    await asyncio.sleep(0.5)
    for (poll_id,) in rows:
        try:
            if "_quarterly" in poll_id:
                view = QuarterlyPollView(poll_id)
            else:
                view = PollView(poll_id)
            bot.add_view(view)
        except Exception:
            log.exception("Failed to add persistent view for poll %s", poll_id)
        await asyncio.sleep(batch_delay)

@bot.event
async def on_ready():
    log.info(f"✅ Eingeloggt als {bot.user} (ID: {bot.user.id})")
    init_db()
    if not scheduler.running:
        scheduler.start()
    schedule_weekly_post()
    schedule_quarterly_post()
    schedule_weekly_summary()
    schedule_daily_summary()
    try:
        bot.loop.create_task(register_persistent_poll_views_async(batch_delay=0.02))
        log.info("Scheduled async registration of PollView instances for existing polls.")
    except Exception:
        log.exception("Failed to schedule persistent view registration on startup.")

if __name__ == "__main__":
    if not BOT_TOKEN:
        print("Bitte BOT_TOKEN als Umgebungsvariable setzen.")
        raise SystemExit(1)
    init_db()
    bot.run(BOT_TOKEN)
