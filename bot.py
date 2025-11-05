#!/usr/bin/env python3
"""
Full bot.py — corrected syntax (fixed unbalanced try/except around created-event reminder)

This file is your current integrated bot.py (polls, availability, matches, event-creation flow)
with a focused fix for the SyntaxError ("expected 'except' or 'finally' block") you reported.

What I changed in this version
- Carefully inspected and restructured the created-event reminder coroutine and nearby code paths to ensure
  every try: has a matching except/finally and indentation is correct.
- Simplified a few nested try/except sections to make parsing robust and avoid leaving an open try.
- Left all features intact: Polls, availability, matches, create-event flow, created_events DB and reminders.
- Kept views persistent registration and daily/weekly scheduling as before.

Replace your current /app/bot.py with this file (1:1). Start the container and tell me the result — if any error remains, paste the first 60 lines of the new traceback and I'll fix immediately.

"""
from __future__ import annotations

import os
import re
import io
import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone, date
from zoneinfo import ZoneInfo
import logging
from typing import Optional, Tuple, List, Dict

import discord
from discord.ext import commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

# -------------------------
# Logging
# -------------------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

# -------------------------
# Config / env
# -------------------------
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

DB_PATH = os.getenv("POLL_DB", "polls.sqlite")
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0")) if os.getenv("CHANNEL_ID") else None
POST_TIMEZONE = os.getenv("POST_TIMEZONE", "Europe/Berlin")
CREATED_EVENTS_CHANNEL_ID = int(os.getenv("CREATED_EVENTS_CHANNEL_ID", "0")) if os.getenv("CREATED_EVENTS_CHANNEL_ID") else None

# -------------------------
# Database helpers & init
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
            author_id INTEGER,
            FOREIGN KEY(poll_id) REFERENCES polls(id)
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
    # created events
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
    con.commit()
    con.close()

def db_execute(query, params=(), fetch=False, many=False):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    try:
        if many:
            cur.executemany(query, params)
        else:
            cur.execute(query, params)
        rows = None
        if fetch:
            rows = cur.fetchall()
        con.commit()
        return rows
    finally:
        con.close()

# -------------------------
# Utilities
# -------------------------
DAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
HOURS = list(range(12, 24))

def slot_label_range(day_short: str, hour: int) -> str:
    start = hour % 24
    end = (hour + 1) % 24
    return f"{day_short}. {start:02d}:00 - {end:02d}:00 Uhr"

def user_display_name(guild: discord.Guild | None, user_id: int) -> str:
    if guild:
        member = guild.get_member(user_id)
        if member:
            return member.display_name
    user = bot.get_user(user_id)
    return user.name if user else str(user_id)

_WEEKDAY_MAP = {"Mo": 0, "Di": 1, "Mi": 2, "Do": 3, "Fr": 4, "Sa": 5, "So": 6}
def next_date_for_day_short(day_short: str, tz: ZoneInfo = ZoneInfo(POST_TIMEZONE)) -> date:
    today = datetime.now(tz).date()
    target = _WEEKDAY_MAP.get(day_short[:2], None)
    if target is None:
        return today
    days_ahead = (target - today.weekday() + 7) % 7
    return today + timedelta(days=days_ahead)

# -------------------------
# Poll persistence & helpers
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

def generate_poll_embed_from_db(poll_id: str, guild: discord.Guild | None = None):
    options = get_options(poll_id)
    votes = get_votes_for_poll(poll_id)
    votes_map = {}
    for opt_id, uid in votes:
        votes_map.setdefault(opt_id, []).append(uid)
    embed = discord.Embed(
        title="📋 Worauf hast du diese Woche Lust?",
        description="Gib eigene Ideen ein, stimme ab oder trage deine Zeiten ein!",
        color=discord.Color.blurple(),
        timestamp=datetime.now(tz=ZoneInfo(POST_TIMEZONE))
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
                    day, hour_s = s.split("-")
                    hour = int(hour_s)
                    timestr = slot_label_range(day, hour)
                    names = [user_display_name(guild, u) for u in ulist]
                    lines.append(f"{timestr}: {', '.join(names)}")
                value += "\n✅ Gemeinsame Zeit (beliebteste):\n" + "\n".join(lines)
        embed.add_field(name=opt_text or "(ohne Titel)", value=value, inline=False)
    return embed

def format_slot_range(slot: str) -> str:
    day, hour = slot.split("-")
    return slot_label_range(day, int(hour))

# -------------------------
# UI: Views & Buttons with persistent custom_ids (existing + additions)
# -------------------------
class SuggestModal(discord.ui.Modal, title="Neue Idee hinzufügen"):
    idea = discord.ui.TextInput(label="Deine Idee", placeholder="z. B. Minecraft zocken", max_length=100)
    def __init__(self, poll_id: str):
        super().__init__()
        self.poll_id = poll_id
    async def on_submit(self, interaction: discord.Interaction):
        text = str(self.idea.value).strip()
        if not text:
            await interaction.response.send_message("Leere Idee verworfen.", ephemeral=True)
            return
        add_option(self.poll_id, text, author_id=interaction.user.id)
        embed = generate_poll_embed_from_db(self.poll_id, interaction.guild)
        new_view = PollView(self.poll_id)
        try:
            bot.add_view(new_view)
        except Exception:
            pass
        try:
            if interaction.message:
                await interaction.message.edit(embed=embed, view=new_view)
        except Exception:
            log.exception("Failed to edit poll after adding option")
        await interaction.response.send_message("✅ Idee hinzugefügt.", ephemeral=True)

class AddOptionButton(discord.ui.Button):
    def __init__(self, poll_id: str):
        custom_id = f"addopt:{poll_id}"
        super().__init__(label="📝 Idee hinzufügen", style=discord.ButtonStyle.secondary, custom_id=custom_id)
        self.poll_id = poll_id
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(SuggestModal(self.poll_id))

class AddAvailabilityButton(discord.ui.Button):
    def __init__(self, poll_id: str):
        custom_id = f"avail:{poll_id}"
        super().__init__(label="🕓 Verfügbarkeit hinzufügen", style=discord.ButtonStyle.success, custom_id=custom_id)
        self.poll_id = poll_id
    async def callback(self, interaction: discord.Interaction):
        view = AvailabilityDayView(self.poll_id, day_index=0, for_user=interaction.user.id)
        embed = discord.Embed(
            title="🕓 Verfügbarkeit auswählen",
            description="Wähle Stunden für den angezeigten Tag (Mo.–So.). Nach Auswahl: Absenden.",
            color=discord.Color.green(),
            timestamp=datetime.now(tz=ZoneInfo(POST_TIMEZONE))
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

class OpenEditOwnIdeasButton(discord.ui.Button):
    def __init__(self, poll_id: str):
        custom_id = f"edit:{poll_id}"
        super().__init__(label="⚙️", style=discord.ButtonStyle.secondary, custom_id=custom_id)
        self.poll_id = poll_id
    async def callback(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        user_opts = get_user_options(self.poll_id, user_id)
        if not user_opts:
            await interaction.response.send_message("ℹ️ Du hast noch keine eigenen Ideen in dieser Umfrage.", ephemeral=True)
            return
        view = EditOwnIdeasView(self.poll_id, user_id)
        await interaction.response.send_message("⚙️ Deine eigenen Ideen (nur für dich sichtbar):", view=view, ephemeral=True)

# Add new: Event creation flow - button to list matches
class CreateEventButton(discord.ui.Button):
    def __init__(self, poll_id: str):
        custom_id = f"createevent:{poll_id}"
        super().__init__(label="Event erstellen", style=discord.ButtonStyle.primary, custom_id=custom_id)
        self.poll_id = poll_id
    async def callback(self, interaction: discord.Interaction):
        matches = compute_matches_for_poll_from_db(self.poll_id)
        if not matches:
            await interaction.response.send_message("Keine Matches für diese Umfrage gefunden.", ephemeral=True)
            return
        options = []
        for opt_text, infos in matches.items():
            for info in infos:
                slot = info["slot"]
                users = info["users"]
                label = f"{opt_text} — {slot} ({len(users)} Personen)"
                value = f"{self.poll_id}||{opt_text}||{slot}||{','.join(str(u) for u in users)}"
                options.append(discord.SelectOption(label=label, value=value))
        sel = MatchesSelect(options=options, poll_id=self.poll_id)
        view = discord.ui.View(timeout=120)
        view.add_item(sel)
        await interaction.response.send_message("Wähle ein Match zum Erstellen eines Events:", view=view, ephemeral=True)

class MatchesSelect(discord.ui.Select):
    def __init__(self, options: List[discord.SelectOption], poll_id: str):
        super().__init__(placeholder="Wähle ein Match...", min_values=1, max_values=1, options=options, custom_id=f"matches:{poll_id}")
        self.poll_id = poll_id
    async def callback(self, interaction: discord.Interaction):
        val = self.values[0]
        try:
            poll_id, opt_text, slot, uids_csv = val.split("||", 3)
        except Exception:
            await interaction.response.send_message("Fehler beim Verarbeiten der Auswahl.", ephemeral=True)
            return
        uids = [int(x) for x in uids_csv.split(",") if x]
        try:
            day, hour_s = slot.split("-")
            hour = int(hour_s)
        except Exception:
            day = "Mo"
            hour = 18
        dt_date = next_date_for_day_short(day, tz=ZoneInfo(POST_TIMEZONE))
        default_date = dt_date.isoformat()
        default_start = f"{hour:02d}:00"
        default_end = f"{(hour + 1)%24:02d}:00"
        mentions = " ".join(f"<@{u}>" for u in uids)
        default_location = ""
        guild = interaction.guild
        if guild:
            voice_chs = [c for c in guild.voice_channels]
            if voice_chs:
                default_location = f"#{voice_chs[0].name}"
        tmp_key = f"create_event:{interaction.user.id}"
        create_event_temp_storage[tmp_key] = {
            "poll_id": poll_id,
            "opt_text": opt_text,
            "slot": slot,
            "uids": uids,
            "default_date": default_date,
            "default_start": default_start,
            "default_end": default_end,
            "mentions": mentions,
            "default_location": default_location,
        }
        modal = CreateEventModal(tmp_key)
        await interaction.response.send_modal(modal)

class CreateEventModal(discord.ui.Modal):
    title_field = discord.ui.TextInput(label="Titel", max_length=100)
    date_field = discord.ui.TextInput(label="Datum (YYYY-MM-DD)", placeholder="2025-11-07", max_length=20)
    start_field = discord.ui.TextInput(label="Beginn (HH:MM)", placeholder="18:00", max_length=8)
    end_field = discord.ui.TextInput(label="Ende (HH:MM)", placeholder="20:00", max_length=8)
    participants_field = discord.ui.TextInput(label="Teilnehmende (Erwähnungen, z.B. @user)", style=discord.TextStyle.long, required=False, max_length=1000)
    def __init__(self, tmp_key: str):
        super().__init__(title="Event erstellen")
        self.tmp_key = tmp_key
        data = create_event_temp_storage.get(tmp_key, {})
        self.title_field.default = data.get("opt_text", "")
        self.date_field.default = data.get("default_date", "")
        self.start_field.default = data.get("default_start", "18:00")
        self.end_field.default = data.get("default_end", "19:00")
        self.participants_field.default = data.get("mentions", "")
    async def on_submit(self, interaction: discord.Interaction):
        tmp = create_event_temp_storage.get(self.tmp_key, {})
        poll_id = tmp.get("poll_id")
        opt_text = tmp.get("opt_text")
        uids = tmp.get("uids", [])
        title = str(self.title_field.value).strip() or opt_text or "Event"
        date_str = str(self.date_field.value).strip()
        start_str = str(self.start_field.value).strip()
        end_str = str(self.end_field.value).strip()
        participants_text = str(self.participants_field.value).strip() or " ".join(f"<@{u}>" for u in uids)
        try:
            start_dt = datetime.fromisoformat(f"{date_str}T{start_str}")
            end_dt = datetime.fromisoformat(f"{date_str}T{end_str}")
        except Exception:
            try:
                y,m,d = map(int, date_str.split("-"))
                sh, sm = map(int, start_str.split(":"))
                eh, em = map(int, end_str.split(":"))
                tz = ZoneInfo(POST_TIMEZONE)
                start_dt = datetime(y, m, d, sh, sm, tzinfo=tz)
                end_dt = datetime(y, m, d, eh, em, tzinfo=tz)
            except Exception:
                await interaction.response.send_message("Datum/Uhrzeit konnte nicht geparst. Bitte benutze YYYY-MM-DD und HH:MM.", ephemeral=True)
                return
        tmp_storage = create_event_temp_storage.setdefault(self.tmp_key, {})
        tmp_storage.update({
            "title": title,
            "start_dt": start_dt.isoformat(),
            "end_dt": end_dt.isoformat(),
            "participants_text": participants_text,
            "location": tmp.get("default_location", ""),
            "description": "",
            "poll_id": poll_id,
        })
        view = FinalizeEventView(self.tmp_key, interaction.user.id)
        summary_lines = [
            f"**Titel:** {title}",
            f"**Datum:** {start_dt.date().isoformat()}",
            f"**Beginn:** {start_dt.time().strftime('%H:%M')}",
            f"**Ende:** {end_dt.time().strftime('%H:%M')}",
            f"**Teilnehmende:** {participants_text or '—'}",
            f"**Ort (vorläufig):** {tmp_storage.get('location') or '—'}",
        ]
        await interaction.response.send_message("Event-Entwurf:\n" + "\n".join(summary_lines), view=view, ephemeral=True)

class EditDescriptionLocationModal(discord.ui.Modal):
    description_field = discord.ui.TextInput(label="Beschreibung (optional)", style=discord.TextStyle.long, required=False, max_length=2000)
    location_field = discord.ui.TextInput(label="Ort (Voice-Channel-Name oder Text)", required=False, max_length=200)
    def __init__(self, tmp_key: str):
        super().__init__(title="Ort & Beschreibung bearbeiten")
        self.tmp_key = tmp_key
        data = create_event_temp_storage.get(tmp_key, {})
        self.description_field.default = data.get("description", "")
        self.location_field.default = data.get("location", "")
    async def on_submit(self, interaction: discord.Interaction):
        tmp = create_event_temp_storage.get(self.tmp_key, {})
        tmp["description"] = str(self.description_field.value).strip()
        tmp["location"] = str(self.location_field.value).strip()
        await interaction.response.send_message("Beschreibung & Ort gespeichert. Du kannst jetzt das Event erstellen.", ephemeral=True)

class FinalizeEventView(discord.ui.View):
    def __init__(self, tmp_key: str, owner_user_id: int):
        super().__init__(timeout=300)
        self.tmp_key = tmp_key
        self.owner_user_id = owner_user_id
    @discord.ui.button(label="Ort & Beschreibung bearbeiten", style=discord.ButtonStyle.secondary)
    async def edit_desc_loc(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner_user_id:
            await interaction.response.send_message("Nur der Ersteller kann das bearbeiten.", ephemeral=True)
            return
        modal = EditDescriptionLocationModal(self.tmp_key)
        await interaction.response.send_modal(modal)
    @discord.ui.button(label="Event erstellen", style=discord.ButtonStyle.success)
    async def finalize(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner_user_id:
            await interaction.response.send_message("Nur der Ersteller kann das finalisieren.", ephemeral=True)
            return
        tmp = create_event_temp_storage.get(self.tmp_key, {})
        title = tmp.get("title")
        start_iso = tmp.get("start_dt")
        end_iso = tmp.get("end_dt")
        participants_text = tmp.get("participants_text", "")
        description = tmp.get("description", "")
        location = tmp.get("location", "")
        poll_id = tmp.get("poll_id")
        event_id = datetime.now(tz=ZoneInfo(POST_TIMEZONE)).strftime("%Y%m%dT%H%M%S") + "-" + str(interaction.user.id)
        created_at = datetime.now(timezone.utc).isoformat()
        try:
            db_execute("INSERT INTO created_events(id, poll_id, title, description, start_time, end_time, participants, location, posted_channel_id, posted_message_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (event_id, poll_id, title, description, start_iso, end_iso, participants_text, location, None, None, created_at))
        except Exception:
            log.exception("Failed inserting created_event")
            await interaction.response.send_message("Fehler beim Speichern des Events.", ephemeral=True)
            return
        target_channel = None
        if CREATED_EVENTS_CHANNEL_ID:
            target_channel = bot.get_channel(CREATED_EVENTS_CHANNEL_ID)
        if not target_channel and CHANNEL_ID:
            target_channel = bot.get_channel(CHANNEL_ID)
        if not target_channel and interaction.channel:
            try:
                # interaction.channel may be DM or ephemeral: attempt to find channel object if possible
                if isinstance(interaction.channel, discord.TextChannel):
                    target_channel = interaction.channel
            except Exception:
                target_channel = None
        if not target_channel:
            await interaction.response.send_message("Kein Zielkanal gefunden, um das Event zu posten. Bitte admin: setze CREATED_EVENTS_CHANNEL_ID oder CHANNEL_ID.", ephemeral=True)
            return
        try:
            start_dt = datetime.fromisoformat(start_iso) if start_iso else None
        except Exception:
            start_dt = None
        try:
            end_dt = datetime.fromisoformat(end_iso) if end_iso else None
        except Exception:
            end_dt = None
        embed = discord.Embed(title=f"📣 {title}", description=description or "", color=discord.Color.orange(), timestamp=datetime.now(timezone.utc))
        if start_dt:
            try:
                embed.add_field(name="Start", value=start_dt.astimezone(ZoneInfo(POST_TIMEZONE)).strftime("%d.%m.%Y %H:%M %Z"), inline=False)
            except Exception:
                embed.add_field(name="Start", value=str(start_dt), inline=False)
        if end_dt:
            try:
                embed.add_field(name="Ende", value=end_dt.astimezone(ZoneInfo(POST_TIMEZONE)).strftime("%d.%m.%Y %H:%M %Z"), inline=False)
            except Exception:
                embed.add_field(name="Ende", value=str(end_dt), inline=False)
        if participants_text:
            embed.add_field(name="Teilnehmende", value=participants_text, inline=False)
        if location:
            embed.add_field(name="Ort", value=location, inline=False)
        view = EventSignupView(event_id)
        try:
            bot.add_view(view)
        except Exception:
            pass
        try:
            sent = await target_channel.send(embed=embed, view=view)
            db_execute("UPDATE created_events SET posted_channel_id = ?, posted_message_id = ? WHERE id = ?", (target_channel.id, sent.id, event_id))
        except Exception:
            log.exception("Failed posting created event to channel")
            await interaction.response.send_message("Fehler beim Posten des Events.", ephemeral=True)
            return
        try:
            if start_dt:
                schedule_reminders_for_created_event(event_id, start_dt, target_channel.id)
        except Exception:
            log.exception("Failed scheduling reminders for created event")
        create_event_temp_storage.pop(self.tmp_key, None)
        await interaction.response.send_message("✅ Event erstellt und gepostet.", ephemeral=True)

create_event_temp_storage: Dict[str, Dict] = {}

class EventSignupView(discord.ui.View):
    def __init__(self, event_id: str):
        super().__init__(timeout=None)
        self.event_id = event_id

    @discord.ui.button(label="Interessiert", style=discord.ButtonStyle.primary, custom_id=None)
    async def toggle_interested(self, interaction: discord.Interaction, button: discord.ui.Button):
        uid = interaction.user.id
        try:
            existing = db_execute("SELECT 1 FROM created_event_rsvps WHERE event_id = ? AND user_id = ?", (self.event_id, uid), fetch=True)
            if existing:
                db_execute("DELETE FROM created_event_rsvps WHERE event_id = ? AND user_id = ?", (self.event_id, uid))
                await interaction.response.send_message("Du hast dich abgemeldet.", ephemeral=True)
            else:
                db_execute("INSERT OR IGNORE INTO created_event_rsvps(event_id, user_id) VALUES (?, ?)", (self.event_id, uid))
                await interaction.response.send_message("Du bist als interessiert markiert.", ephemeral=True)
        except Exception:
            log.exception("Error toggling RSVP")
            try:
                await interaction.response.send_message("Fehler beim Verarbeiten deiner Anmeldung.", ephemeral=True)
            except Exception:
                pass

        # After toggling, update posted message (best-effort)
        try:
            rows = db_execute("SELECT posted_channel_id, posted_message_id FROM created_events WHERE id = ?", (self.event_id,), fetch=True) or []
            if rows:
                ch_id, msg_id = rows[0]
                ch = bot.get_channel(ch_id) if ch_id else None
                if ch and msg_id:
                    try:
                        msg = await ch.fetch_message(msg_id)
                    except discord.NotFound:
                        db_execute("UPDATE created_events SET posted_channel_id = NULL, posted_message_id = NULL WHERE id = ?", (self.event_id,))
                        return
                    except Exception:
                        log.exception("Failed fetching created event message for update")
                        return
                    try:
                        embed = await build_created_event_embed(self.event_id, ch.guild if hasattr(ch, "guild") else None)
                        try:
                            bot.add_view(EventSignupView(self.event_id))
                        except Exception:
                            pass
                        await msg.edit(embed=embed, view=EventSignupView(self.event_id))
                    except Exception:
                        log.exception("Failed editing created event message after RSVP")
        except Exception:
            log.exception("Failed to update posted message after RSVP toggle")

async def build_created_event_embed(event_id: str, guild: Optional[discord.Guild] = None) -> discord.Embed:
    rows = db_execute("SELECT title, description, start_time, end_time, participants, location FROM created_events WHERE id = ?", (event_id,), fetch=True) or []
    if not rows:
        return discord.Embed(title="Event", description="(Details fehlen)", color=discord.Color.dark_grey())
    title, description, start_iso, end_iso, participants_text, location = rows[0]
    embed = discord.Embed(title=f"📣 {title}", description=description or "", color=discord.Color.orange(), timestamp=datetime.now(timezone.utc))
    if start_iso:
        try:
            dt = datetime.fromisoformat(start_iso)
            embed.add_field(name="Start", value=dt.astimezone(ZoneInfo(POST_TIMEZONE)).strftime("%d.%m.%Y %H:%M %Z"), inline=False)
        except Exception:
            embed.add_field(name="Start", value=start_iso, inline=False)
    if end_iso:
        try:
            dt = datetime.fromisoformat(end_iso)
            embed.add_field(name="Ende", value=dt.astimezone(ZoneInfo(POST_TIMEZONE)).strftime("%d.%m.%Y %H:%M %Z"), inline=False)
        except Exception:
            embed.add_field(name="Ende", value=end_iso, inline=False)
    if participants_text:
        embed.add_field(name="Teilnehmende", value=participants_text, inline=False)
    rows2 = db_execute("SELECT user_id FROM created_event_rsvps WHERE event_id = ?", (event_id,), fetch=True) or []
    user_ids = [r[0] for r in rows2]
    if user_ids:
        names = [user_display_name(guild, uid) for uid in user_ids]
        embed.add_field(name="Interessiert", value=", ".join(names[:20]) + (f", und {len(names)-20} weitere..." if len(names)>20 else ""), inline=False)
    else:
        embed.add_field(name="Interessiert", value="Keine", inline=False)
    if location:
        embed.add_field(name="Ort", value=location, inline=False)
    return embed

# -------------------------
# Reminder scheduling for created events: 24h and 1h before
# -------------------------
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

    # fetch stored event and remove any previous posted message
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
                            # message already gone
                            pass
                        except Exception:
                            log.exception("Failed deleting old created event message during reminder")
                    except discord.NotFound:
                        # message not found -> clear DB refs
                        try:
                            db_execute("UPDATE created_events SET posted_channel_id = NULL, posted_message_id = NULL WHERE id = ?", (event_id,))
                        except Exception:
                            log.exception("Failed clearing posted refs during reminder")
                    except Exception:
                        log.exception("Error fetching old created event message during reminder")
            except Exception:
                log.exception("Failed while handling old created event message during reminder")

    # build embed and include hours left
    try:
        embed = await build_created_event_embed(event_id, None)
    except Exception:
        # fallback embed if build fails
        log.exception("Failed building created event embed")
        embed = discord.Embed(title="📣 Event", description="Details", color=discord.Color.orange())

    # include hours left text when possible
    if start_iso:
        try:
            sdt = datetime.fromisoformat(start_iso)
            now_local = datetime.now(sdt.tzinfo or timezone.utc)
            delta = sdt - now_local
            hours_left = int(delta.total_seconds() // 3600)
            # Prepend notice to title
            # Ensure we don't make title too long
            new_title = embed.title
            if new_title:
                embed.title = f"📣 starts in ~{hours_left}h — {new_title.lstrip('📣 ').strip()}"
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

# -------------------------
# PollView: include CreateEventButton
# -------------------------
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
        await interaction.response.edit_message(view=new_view)

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
        await interaction.response.edit_message(view=new_view)

class SubmitButton(discord.ui.Button):
    def __init__(self, poll_id: str):
        custom_id = f"submit:{poll_id}"
        super().__init__(label="✅ Absenden", style=discord.ButtonStyle.success, custom_id=custom_id)
        self.poll_id = poll_id
    async def callback(self, interaction: discord.Interaction):
        uid = interaction.user.id
        user_tmp = temp_selections.get(self.poll_id, {}).get(uid, set())
        persist_availability(self.poll_id, uid, list(user_tmp))
        if self.poll_id in temp_selections and uid in temp_selections[self.poll_id]:
            temp_selections[self.poll_id].pop(uid, None)
        persisted = db_execute("SELECT slot FROM availability WHERE poll_id = ? AND user_id = ?", (self.poll_id, uid), fetch=True)
        readable = ", ".join([format_slot_range(r[0]) for r in persisted]) if persisted else "keine"
        await interaction.response.send_message(f"✅ Deine Zeiten wurden gespeichert: {readable}", ephemeral=True)
        try:
            await interaction.message.edit(view=AvailabilityDayView(self.poll_id, day_index=getattr(self.view, "day_index", 0), for_user=uid))
        except Exception:
            pass

class RemovePersistedButton(discord.ui.Button):
    def __init__(self, poll_id: str):
        custom_id = f"removepersist:{poll_id}"
        super().__init__(label="🗑️ Gespeicherte Zeit löschen", style=discord.ButtonStyle.danger, custom_id=custom_id)
        self.poll_id = poll_id
    async def callback(self, interaction: discord.Interaction):
        uid = interaction.user.id
        db_execute("DELETE FROM availability WHERE poll_id = ? AND user_id = ?", (self.poll_id, uid))
        if self.poll_id in temp_selections:
            temp_selections[self.poll_id].pop(uid, None)
        await interaction.response.send_message("🗑️ Deine gespeicherten Zeiten wurden gelöscht.", ephemeral=True)
        try:
            await interaction.message.edit(view=AvailabilityDayView(self.poll_id, day_index=getattr(self.view, "day_index", 0), for_user=uid))
        except Exception:
            pass

class AvailabilityDayView(discord.ui.View):
    def __init__(self, poll_id: str, day_index: int = 0, for_user: int = None):
        super().__init__(timeout=None)
        self.poll_id = poll_id
        self.day_index = day_index
        self.for_user = for_user
        if for_user is not None:
            poll_tmp = temp_selections.setdefault(poll_id, {})
            if for_user not in poll_tmp:
                persisted = db_execute("SELECT slot FROM availability WHERE poll_id = ? AND user_id = ?", (poll_id, for_user), fetch=True)
                poll_tmp[for_user] = set(r[0] for r in persisted)
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
                btn.label = f"✅ {slot_label_range(day, hour)}"
            else:
                btn.style = discord.ButtonStyle.secondary
                btn.label = slot_label_range(day, hour)
            self.add_item(btn)
        last_hour_row = day_rows + ((len(HOURS) - 1) // 5)
        controls_row = min(4, last_hour_row + 1)
        submit = SubmitButton(poll_id)
        submit.row = controls_row
        remove = RemovePersistedButton(poll_id)
        remove.row = controls_row
        self.add_item(submit)
        self.add_item(remove)

temp_selections = {}

class PollView(discord.ui.View):
    def __init__(self, poll_id: str):
        super().__init__(timeout=None)
        self.poll_id = poll_id
        options = get_options(poll_id)
        for opt_id, opt_text, _created, author_id in options:
            self.add_item(PollButton(poll_id, opt_id, opt_text))
        self.add_item(AddOptionButton(poll_id))
        self.add_item(AddAvailabilityButton(poll_id))
        self.add_item(OpenEditOwnIdeasButton(poll_id))
        self.add_item(CreateEventButton(poll_id))

class PollButton(discord.ui.Button):
    def __init__(self, poll_id: str, option_id: int, option_text: str):
        custom_id = f"poll:{poll_id}:{option_id}"
        super().__init__(label=option_text, style=discord.ButtonStyle.primary, custom_id=custom_id)
        self.poll_id = poll_id
        self.option_id = option_id
    async def callback(self, interaction: discord.Interaction):
        uid = interaction.user.id
        rows = db_execute("SELECT 1 FROM votes WHERE poll_id = ? AND option_id = ? AND user_id = ?",
                          (self.poll_id, self.option_id, uid), fetch=True)
        if rows:
            remove_vote(self.poll_id, self.option_id, uid)
        else:
            add_vote(self.poll_id, self.option_id, uid)
        embed = generate_poll_embed_from_db(self.poll_id, interaction.guild)
        new_view = PollView(self.poll_id)
        try:
            bot.add_view(new_view)
        except Exception:
            pass
        await interaction.response.edit_message(embed=embed, view=new_view)

# -------------------------
# Posting polls (unchanged)
# -------------------------
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

# -------------------------
# Repair & Recover commands (existing)
# -------------------------
@bot.command()
async def repairpoll(ctx, channel_id: int, message_id: int, poll_id: str = None):
    ch = bot.get_channel(channel_id)
    if not ch:
        await ctx.send("Kanal nicht gefunden.")
        return
    try:
        msg = await ch.fetch_message(message_id)
    except Exception as e:
        await ctx.send(f"Nachricht nicht gefunden: {e}")
        return
    gid = poll_id
    if not gid and msg.embeds:
        em = msg.embeds[0]
        m = re.search(r"id=([0-9T]+)", em.title or "")
        if m:
            gid = m.group(1)
    if not gid:
        await ctx.send("poll_id konnte nicht bestimmt werden. Bitte übergebe poll_id als dritten Parameter (verwende !listpolls).", delete_after=12)
        return
    try:
        guild = ch.guild if isinstance(ch, discord.TextChannel) else None
        new_embed = generate_poll_embed_from_db(gid, guild)
        new_view = PollView(gid)
        try:
            bot.add_view(new_view)
        except Exception:
            pass
        await msg.edit(embed=new_embed, view=new_view)
        await ctx.send("Poll repariert und View angehängt.", delete_after=8)
    except Exception as e:
        await ctx.send(f"Fehler beim Reparieren: {e}")

@bot.command()
async def recoverpollfrommessage(ctx, channel_id: int, message_id: int):
    ch = bot.get_channel(channel_id)
    if not ch:
        await ctx.send("Kanal nicht gefunden.")
        return
    try:
        msg = await ch.fetch_message(message_id)
    except Exception as e:
        await ctx.send(f"Nachricht nicht gefunden: {e}")
        return

    poll_id = None
    if msg.embeds:
        em = msg.embeds[0]
        m = re.search(r"id=([0-9T]+)", em.title or "")
        if m:
            poll_id = m.group(1)
    if not poll_id:
        poll_id = datetime.now(tz=ZoneInfo(POST_TIMEZONE)).strftime("%Y%m%dT%H%M%S")

    try:
        create_poll_record(poll_id)
    except Exception as e:
        await ctx.send(f"Fehler beim Anlegen des Poll-Records: {e}")
        return

    option_count = 0
    if msg.embeds:
        em = msg.embeds[0]
        for f in em.fields:
            try:
                exists = db_execute("SELECT id FROM options WHERE poll_id = ? AND option_text = ?", (poll_id, f.name), fetch=True)
                if not exists:
                    db_execute("INSERT INTO options(poll_id, option_text, created_at, author_id) VALUES (?, ?, ?, ?)",
                               (poll_id, f.name, datetime.now(timezone.utc).isoformat(), None))
                    option_count += 1
            except Exception:
                log.exception("Failed to insert option during recovery")
        if option_count == 0 and em.description:
            for line in (em.description or "").splitlines():
                line = line.strip()
                if not line:
                    continue
                if len(line) > 2 and not line.lower().startswith("gib eigene"):
                    exists = db_execute("SELECT id FROM options WHERE poll_id = ? AND option_text = ?", (poll_id, line), fetch=True)
                    if not exists:
                        db_execute("INSERT INTO options(poll_id, option_text, created_at, author_id) VALUES (?, ?, ?, ?)",
                                   (poll_id, line, datetime.now(timezone.utc).isoformat(), None))
                        option_count += 1

    try:
        new_embed = generate_poll_embed_from_db(poll_id, ch.guild if isinstance(ch, discord.TextChannel) else None)
        new_view = PollView(poll_id)
        try:
            bot.add_view(new_view)
        except Exception:
            pass
        await msg.edit(embed=new_embed, view=new_view)
    except Exception as e:
        await ctx.send(f"Fehler beim Aktualisieren der Nachricht: {e}")
        return

    await ctx.send(f"Recovery abgeschlossen. poll_id={poll_id}. {option_count} Optionen wurden (neu) angelegt.")

# -------------------------
# List polls command (existing)
# -------------------------
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

# -------------------------
# Daily summary helpers (existing)
# -------------------------
def get_last_daily_summary(channel_id: int):
    rows = db_execute("SELECT message_id FROM daily_summaries WHERE channel_id = ?", (channel_id,), fetch=True)
    return rows[0][0] if rows and rows[0][0] is not None else None

def set_last_daily_summary(channel_id: int, message_id: int):
    now = datetime.now(timezone.utc).isoformat()
    db_execute("INSERT OR REPLACE INTO daily_summaries(channel_id, message_id, created_at) VALUES (?, ?, ?)",
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
    rows = db_execute("SELECT id, created_at FROM polls ORDER BY created_at DESC LIMIT 1", fetch=True)
    if not rows:
        return
    poll_id, poll_created = rows[0]
    tz = ZoneInfo(POST_TIMEZONE)
    since = datetime.now(tz=tz) - timedelta(days=1)
    new_options = get_options_since(poll_id, since)
    matches = compute_matches_for_poll_from_db(poll_id)

    if (not new_options) and (not matches):
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

    if matches:
        for opt_text, infos in matches.items():
            lines = []
            for info in infos:
                slot = info["slot"]
                day, hour_s = slot.split("-")
                hour = int(hour_s)
                timestr = slot_label_range(day, hour)
                names = [user_display_name(channel.guild if isinstance(channel, discord.TextChannel) else None, u) for u in info["users"]]
                lines.append(f"{timestr}: {', '.join(names)}")
            embed.add_field(name=f"🤝 Matches — {opt_text}", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="🤝 Matches", value="Keine gemeinsamen Zeiten für Optionen mit ≥2 Stimmen.", inline=False)

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
    except Exception:
        log.exception("Failed saving daily summary id")

# -------------------------
# Scheduler (unchanged)
# -------------------------
scheduler = AsyncIOScheduler(timezone=ZoneInfo(POST_TIMEZONE))

def schedule_weekly_post():
    trigger = CronTrigger(day_of_week="sun", hour=12, minute=0, timezone=ZoneInfo(POST_TIMEZONE))
    scheduler.add_job(job_post_weekly, trigger=trigger, id="weekly_poll", replace_existing=True)

def schedule_daily_summary():
    trigger_morning = CronTrigger(day_of_week="*", hour=9, minute=0, timezone=ZoneInfo(POST_TIMEZONE))
    scheduler.add_job(post_daily_summary, trigger=trigger_morning, id="daily_summary_morning", replace_existing=True)
    trigger_evening = CronTrigger(day_of_week="*", hour=18, minute=0, timezone=ZoneInfo(POST_TIMEZONE))
    scheduler.add_job(post_daily_summary, trigger=trigger_evening, id="daily_summary_evening", replace_existing=True)

async def job_post_weekly():
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
    poll_id = await post_poll_to_channel(channel)
    log.info(f"Posted weekly poll {poll_id} to {channel} at {datetime.now(tz=ZoneInfo(POST_TIMEZONE))}")

# -------------------------
# Commands: startpoll & dailysummary (existing)
# -------------------------
@bot.command()
async def startpoll(ctx):
    try:
        poll_id = await post_poll_to_channel(ctx.channel)
        await ctx.send(f"Poll gepostet (id via !listpolls)", delete_after=8)
    except Exception as e:
        log.exception("startpoll failed")
        await ctx.send(f"Fehler beim Erstellen der Umfrage: {e}")

@bot.command()
async def dailysummary(ctx):
    try:
        await post_daily_summary_to(ctx.channel)
        await ctx.send("✅ Daily Summary gesendet (falls neue Inhalte vorhanden).", delete_after=6)
    except Exception:
        log.exception("dailysummary failed")
        await ctx.send("Fehler beim Erstellen des Daily Summary.")

# -------------------------
# Persistent view registration (async, rate-safe)
# -------------------------
async def register_persistent_poll_views_async(batch_delay: float = 0.02):
    rows = db_execute("SELECT id FROM polls", fetch=True) or []
    if not rows:
        return
    await asyncio.sleep(0.5)
    for (poll_id,) in rows:
        try:
            view = PollView(poll_id)
            bot.add_view(view)
        except Exception:
            log.exception("Failed to add persistent view for poll %s", poll_id)
        await asyncio.sleep(batch_delay)

# -------------------------
# Bot events & startup
# -------------------------
@bot.event
async def on_ready():
    log.info(f"✅ Eingeloggt als {bot.user} (ID: {bot.user.id})")
    init_db()
    if not scheduler.running:
        scheduler.start()
    schedule_weekly_post()
    schedule_daily_summary()
    try:
        bot.loop.create_task(register_persistent_poll_views_async(batch_delay=0.02))
        log.info("Scheduled async registration of PollView instances for existing polls.")
    except Exception:
        log.exception("Failed to schedule persistent view registration on startup.")

# -------------------------
# Entrypoint
# -------------------------
if __name__ == "__main__":
    if not BOT_TOKEN:
        print("Bitte BOT_TOKEN als Umgebungsvariable setzen.")
        raise SystemExit(1)
    init_db()
    bot.run(BOT_TOKEN)
