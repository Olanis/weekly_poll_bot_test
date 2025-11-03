#!/usr/bin/env python3
"""
Complete bot.py with Poll + Quarter Poll functions and event handling fixes.

- persistent Views (custom_id) and bot.add_view registration
- scheduled event posting with duplicate prevention and REST sync (!sync_events)
- raw socket fallback for scheduled event create
- robust reminder deletion (handles 404 NotFound)
- poll UI, persistence, availability, daily summary, quarter polls
- debug commands: !listevents, !checkevents, !pyver

Environment variables:
- BOT_TOKEN (required)
- POLL_DB (optional; default polls.sqlite)
- CHANNEL_ID (optional)
- EVENTS_CHANNEL_ID (optional; channel to post events)
- QUARTER_POLL_CHANNEL_ID (optional)
- POST_TIMEZONE (optional; default Europe/Berlin)
"""
from __future__ import annotations

import os
import io
import sqlite3
import asyncio
import logging
from datetime import datetime, timedelta, timezone, date
from zoneinfo import ZoneInfo

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
# Config / environment
# -------------------------
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.guild_scheduled_events = True  # ensure scheduled event gateway dispatches are enabled

bot = commands.Bot(command_prefix="!", intents=intents)

DB_PATH = os.getenv("POLL_DB", "polls.sqlite")
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0")) if os.getenv("CHANNEL_ID") else None
POST_TIMEZONE = os.getenv("POST_TIMEZONE", "Europe/Berlin")

EVENTS_CHANNEL_ID = int(os.getenv("EVENTS_CHANNEL_ID", "0")) if os.getenv("EVENTS_CHANNEL_ID") else None
QUARTER_POLL_CHANNEL_ID = int(os.getenv("QUARTER_POLL_CHANNEL_ID", "0")) if os.getenv("QUARTER_POLL_CHANNEL_ID") else None

# -------------------------
# Database helpers & init
# -------------------------
def init_db():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    # polls
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

    # events
    cur.execute("""
        CREATE TABLE IF NOT EXISTS tracked_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            discord_event_id TEXT NOT NULL UNIQUE,
            posted_channel_id INTEGER,
            posted_message_id INTEGER,
            start_time TEXT,
            updated_at TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS event_rsvps (
            discord_event_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            UNIQUE(discord_event_id, user_id)
        )
    """)

    # quarter polls
    cur.execute("""
        CREATE TABLE IF NOT EXISTS quarter_polls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            quarter_start DATE NOT NULL,
            posted_channel_id INTEGER,
            posted_message_id INTEGER,
            created_at TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS quarter_options (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            poll_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            created_at TEXT NOT NULL,
            author_id INTEGER
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS quarter_votes (
            poll_id INTEGER NOT NULL,
            option_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            UNIQUE(poll_id, option_id, user_id)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS quarter_availability (
            poll_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            day TEXT NOT NULL,
            UNIQUE(poll_id, user_id, day)
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
HOURS = list(range(12, 24))  # 12..23

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

# -------------------------
# Poll persistence & helpers
# -------------------------
def create_poll_record(poll_id: str):
    db_execute("INSERT OR REPLACE INTO polls(id, created_at) VALUES (?, ?)", (poll_id, datetime.now(timezone.utc).isoformat()))

def add_option(poll_id: str, option_text: str, author_id: int = None):
    created_at = datetime.now(timezone.utc).isoformat()
    db_execute("INSERT INTO options(poll_id, option_text, created_at, author_id) VALUES (?, ?, ?, ?)", (poll_id, option_text, created_at, author_id))
    rows = db_execute("SELECT id FROM options WHERE poll_id = ? AND option_text = ? ORDER BY id DESC LIMIT 1", (poll_id, option_text), fetch=True)
    return rows[-1][0] if rows else None

def get_options(poll_id: str):
    return db_execute("SELECT id, option_text, created_at, author_id FROM options WHERE poll_id = ? ORDER BY id ASC", (poll_id,), fetch=True) or []

def get_user_options(poll_id: str, user_id: int):
    return db_execute("SELECT id, option_text, created_at FROM options WHERE poll_id = ? AND author_id = ? ORDER BY id ASC", (poll_id, user_id), fetch=True) or []

def add_vote(poll_id: str, option_id: int, user_id: int):
    try:
        db_execute("INSERT OR IGNORE INTO votes(poll_id, option_id, user_id) VALUES (?, ?, ?)", (poll_id, option_id, user_id))
    except Exception:
        pass

def remove_vote(poll_id: str, option_id: int, user_id: int):
    db_execute("DELETE FROM votes WHERE poll_id = ? AND option_id = ? AND user_id = ?", (poll_id, option_id, user_id))

def remove_votes_for_user_poll(poll_id: str, user_id: int):
    db_execute("DELETE FROM votes WHERE poll_id = ? AND user_id = ?", (poll_id, user_id))

def get_votes_for_poll(poll_id: str):
    return db_execute("SELECT option_id, user_id FROM votes WHERE poll_id = ?", (poll_id,), fetch=True) or []

def persist_availability(poll_id: str, user_id: int, slots: list):
    db_execute("DELETE FROM availability WHERE poll_id = ? AND user_id = ?", (poll_id, user_id))
    if slots:
        db_execute("INSERT OR IGNORE INTO availability(poll_id, user_id, slot) VALUES (?, ?, ?)", [(poll_id, user_id, s) for s in slots], many=True)

def get_availability_for_poll(poll_id: str):
    return db_execute("SELECT user_id, slot FROM availability WHERE poll_id = ? ORDER BY user_id", (poll_id,), fetch=True) or []

def get_options_since(poll_id: str, since_dt: datetime):
    rows = db_execute("SELECT option_text, created_at FROM options WHERE poll_id = ? AND created_at >= ? ORDER BY created_at ASC", (poll_id, since_dt.isoformat()), fetch=True)
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
        description="Gib eigene Ideen ein, stimm ab oder trage deine Zeiten ein!",
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

        # compute matches and format: only show the slot(s) with the maximum number of users
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
# UI: Views, Buttons, Modals (with persistent custom_id where needed)
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
            log.exception("Failed to edit poll message after adding option")
        await interaction.response.send_message("✅ Idee hinzugefügt.", ephemeral=True)

class AddOptionButton(discord.ui.Button):
    def __init__(self, poll_id: str):
        super().__init__(label="📝 Idee hinzufügen", style=discord.ButtonStyle.secondary, custom_id=f"addopt:{poll_id}")
        self.poll_id = poll_id
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(SuggestModal(self.poll_id))

class AddAvailabilityButton(discord.ui.Button):
    def __init__(self, poll_id: str):
        super().__init__(label="🕓 Verfügbarkeit hinzufügen", style=discord.ButtonStyle.success, custom_id=f"avail:{poll_id}")
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
        super().__init__(label="⚙️", style=discord.ButtonStyle.secondary, custom_id=f"edit:{poll_id}")
        self.poll_id = poll_id
    async def callback(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        user_opts = get_user_options(self.poll_id, user_id)
        if not user_opts:
            await interaction.response.send_message("ℹ️ Du hast noch keine eigenen Ideen in dieser Umfrage.", ephemeral=True)
            return
        view = EditOwnIdeasView(self.poll_id, user_id)
        await interaction.response.send_message("⚙️ Deine eigenen Ideen (nur für dich sichtbar):", view=view, ephemeral=True)

class DeleteOwnOptionButtonEphemeral(discord.ui.Button):
    def __init__(self, poll_id: str, option_id: int, option_text: str, user_id: int):
        super().__init__(label="✖️", style=discord.ButtonStyle.danger)
        self.poll_id = poll_id
        self.option_id = option_id
        self.option_text = option_text
        self.user_id = user_id
    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Nur du kannst diese Idee hier löschen.", ephemeral=True)
            return
        db_execute("DELETE FROM options WHERE id = ?", (self.option_id,))
        db_execute("DELETE FROM votes WHERE option_id = ?", (self.option_id,))
        await interaction.response.send_message(f"✅ Idee gelöscht: {self.option_text}", ephemeral=True)
        # best-effort update public poll message in this channel
        try:
            channel = interaction.channel
            async for msg in channel.history(limit=200):
                if msg.author == bot.user and msg.embeds:
                    em = msg.embeds[0]
                    if em.title and em.title.startswith("📋 Worauf"):
                        rows = db_execute("SELECT id FROM polls ORDER BY created_at DESC LIMIT 1", fetch=True)
                        if rows:
                            poll_id = rows[0][0]
                            new_embed = generate_poll_embed_from_db(poll_id, interaction.guild)
                            new_view = PollView(poll_id)
                            try:
                                bot.add_view(new_view)
                                await msg.edit(embed=new_embed, view=new_view)
                            except Exception:
                                log.exception("Failed to update poll message after deleting option")
                        break
        except Exception:
            log.exception("Failed to search channel history to update poll message")
        try:
            refreshed = EditOwnIdeasView(self.poll_id, self.user_id)
            await interaction.followup.send("🔄 Aktualisierte Liste deiner Ideen:", view=refreshed, ephemeral=True)
        except Exception:
            pass

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

class DaySelectButton(discord.ui.Button):
    def __init__(self, poll_id: str, day_index: int, selected: bool = False):
        label = f"{DAYS[day_index]}."
        style = discord.ButtonStyle.success if selected else discord.ButtonStyle.secondary
        super().__init__(label=label, style=style, custom_id=f"day:{poll_id}:{day_index}")
        self.poll_id = poll_id
        self.day_index = day_index
    async def callback(self, interaction: discord.Interaction):
        new_view = AvailabilityDayView(self.poll_id, day_index=self.day_index, for_user=interaction.user.id)
        await interaction.response.edit_message(view=new_view)

class HourButton(discord.ui.Button):
    def __init__(self, poll_id: str, day: str, hour: int):
        label = slot_label_range(day, hour)
        super().__init__(label=label, style=discord.ButtonStyle.secondary, custom_id=f"hour:{poll_id}:{day}:{hour}")
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
        super().__init__(label="✅ Absenden", style=discord.ButtonStyle.success, custom_id=f"submit:{poll_id}")
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
        super().__init__(label="🗑️ Gespeicherte Zeit löschen", style=discord.ButtonStyle.danger, custom_id=f"removepersist:{poll_id}")
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

# in-memory temporary selections
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
        new_view = PollView(self.poll_id)
        try:
            bot.add_view(new_view)
        except Exception:
            pass
        await interaction.response.edit_message(embed=embed, view=new_view)

# -------------------------
# Posting polls & daily summary
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
# Event integration
# -------------------------
def _event_db_execute(query, params=(), fetch=False, many=False):
    return db_execute(query, params, fetch=fetch, many=many)

def schedule_reminders_for_event(bot, scheduler, discord_event_id: str, start_time, events_channel_id):
    try:
        scheduler.remove_job(f"event_reminder_24_{discord_event_id}")
    except Exception:
        pass
    try:
        scheduler.remove_job(f"event_reminder_2_{discord_event_id}")
    except Exception:
        pass
    if not start_time:
        return
    if start_time.tzinfo is None:
        start_time = start_time.replace(tzinfo=ZoneInfo("UTC"))
    t24 = start_time - timedelta(hours=24)
    t2 = start_time - timedelta(hours=2)
    async def reminder_coro(channel_id: int, discord_event_id: str, hours_before: int):
        ch = bot.get_channel(channel_id)
        if not ch:
            log.info(f"reminder_coro: channel {channel_id} not found")
            return
        embed = build_event_embed_from_db(discord_event_id, None)
        embed.title = f"📣 Event — startet in ~{hours_before} Stunden"
        view = EventViewInFile(discord_event_id, None)
        tracked = _event_db_execute("SELECT posted_channel_id, posted_message_id FROM tracked_events WHERE discord_event_id = ?", (discord_event_id,), fetch=True)
        if tracked:
            old_ch_id, old_msg_id = tracked[0]
            if old_ch_id and old_msg_id:
                try:
                    old_ch = bot.get_channel(old_ch_id)
                    if old_ch:
                        try:
                            old_msg = await old_ch.fetch_message(old_msg_id)
                        except discord.NotFound:
                            log.info("Old event message not found for %s; clearing DB posted refs.", discord_event_id)
                            _event_db_execute("UPDATE tracked_events SET posted_channel_id = NULL, posted_message_id = NULL WHERE discord_event_id = ?", (discord_event_id,))
                            old_msg = None
                        except Exception:
                            log.exception("Error fetching old event message during reminder for %s", discord_event_id)
                            old_msg = None
                        if old_msg:
                            try:
                                await old_msg.delete()
                            except discord.NotFound:
                                log.info("Old event message disappeared before deletion for %s; ignoring.", discord_event_id)
                            except Exception:
                                log.exception("Failed deleting old event message during reminder for %s", discord_event_id)
                except Exception:
                    log.exception("Failed while handling old event message during reminder for %s", discord_event_id)
        try:
            sent = await ch.send(embed=embed, view=view)
            _event_db_execute("UPDATE tracked_events SET posted_channel_id = ?, posted_message_id = ?, updated_at = ? WHERE discord_event_id = ?",
                              (ch.id, sent.id, datetime.now(timezone.utc).isoformat(), discord_event_id))
        except Exception:
            log.exception("Failed sending reminder message for %s", discord_event_id)
    now = datetime.now(timezone.utc)
    if t24 > now:
        scheduler.add_job(lambda: bot.loop.create_task(reminder_coro(events_channel_id, discord_event_id, 24)), trigger=DateTrigger(run_date=t24), id=f"event_reminder_24_{discord_event_id}", replace_existing=True)
    elif t24 <= now < start_time:
        bot.loop.create_task(reminder_coro(events_channel_id, discord_event_id, 24))
    if t2 > now:
        scheduler.add_job(lambda: bot.loop.create_task(reminder_coro(events_channel_id, discord_event_id, 2)), trigger=DateTrigger(run_date=t2), id=f"event_reminder_2_{discord_event_id}", replace_existing=True)
    elif t2 <= now < start_time:
        bot.loop.create_task(reminder_coro(events_channel_id, discord_event_id, 2))

# -------------------------
# High-level scheduled event listeners
# -------------------------
@bot.event
async def on_guild_scheduled_event_create(event: discord.ScheduledEvent):
    log.info(
        f"DEBUG: Received guild_scheduled_event_create id={getattr(event, 'id', None)} "
        f"name={getattr(event, 'name', None)}"
    )
    if not EVENTS_CHANNEL_ID:
        log.info("EVENTS_CHANNEL_ID not set; ignoring scheduled event create")
        return

    try:
        guild = event.guild
        discord_event_id = str(event.id)
        start_iso = event.start_time.isoformat() if event.start_time else None

        # ensure tracked row exists
        db_execute(
            "INSERT OR REPLACE INTO tracked_events(guild_id, discord_event_id, start_time, updated_at) VALUES (?, ?, ?, ?)",
            (guild.id if guild else None, discord_event_id, start_iso, datetime.now(timezone.utc).isoformat())
        )

        # check if there's already a posted message recorded
        tracked = db_execute(
            "SELECT posted_channel_id, posted_message_id FROM tracked_events WHERE discord_event_id = ?",
            (discord_event_id,), fetch=True
        )
        if tracked:
            posted_ch_id, posted_msg_id = tracked[0]
            if posted_msg_id:
                try:
                    ch_check = bot.get_channel(posted_ch_id) if posted_ch_id else None
                    if ch_check:
                        try:
                            # verify message still exists
                            _ = await ch_check.fetch_message(posted_msg_id)
                            log.info("Event %s already has posted message %s — skipping post.", discord_event_id, posted_msg_id)
                            # still (re)create reminders
                            try:
                                schedule_reminders_for_event(bot, scheduler, discord_event_id, event.start_time, EVENTS_CHANNEL_ID)
                            except Exception:
                                log.exception("Failed to schedule reminders for event")
                            return
                        except discord.NotFound:
                            # message gone -> clear DB refs and continue to post
                            db_execute(
                                "UPDATE tracked_events SET posted_channel_id = NULL, posted_message_id = NULL WHERE discord_event_id = ?",
                                (discord_event_id,)
                            )
                        except Exception:
                            log.exception("Error checking existing posted message for event %s", discord_event_id)

        # Post the event message
        ch = bot.get_channel(EVENTS_CHANNEL_ID)
        if ch:
            embed = discord.Embed(
                title=event.name or "Event",
                description=event.description or "",
                color=discord.Color.blue(),
                timestamp=datetime.now(timezone.utc)
            )
            if event.start_time:
                embed.add_field(
                    name="Start",
                    value=event.start_time.astimezone(ZoneInfo(POST_TIMEZONE)).strftime("%d.%m.%Y %H:%M %Z"),
                    inline=False
                )
            view = EventViewInFile(discord_event_id, guild)
            msg = await ch.send(embed=embed, view=view)
            db_execute(
                "UPDATE tracked_events SET posted_channel_id = ?, posted_message_id = ?, updated_at = ? WHERE discord_event_id = ?",
                (ch.id, msg.id, datetime.now(timezone.utc).isoformat(), discord_event_id)
            )
            try:
                schedule_reminders_for_event(bot, scheduler, discord_event_id, event.start_time, EVENTS_CHANNEL_ID)
            except Exception:
                log.exception("Failed to schedule reminders for event")
        else:
            log.info(f"Channel {EVENTS_CHANNEL_ID} not found or inaccessible")
    except Exception:
        log.exception("Error in on_guild_scheduled_event_create")


@bot.event
async def on_guild_scheduled_event_update(event: discord.ScheduledEvent):
    log.info(
        f"DEBUG: Received guild_scheduled_event_update id={getattr(event, 'id', None)} "
        f"name={getattr(event, 'name', None)}"
    )
    if not EVENTS_CHANNEL_ID:
        log.info("EVENTS_CHANNEL_ID not set; ignoring scheduled event update")
        return

    try:
        discord_event_id = str(event.id)
        start_iso = event.start_time.isoformat() if event.start_time else None

        db_execute(
            "UPDATE tracked_events SET start_time = ?, updated_at = ? WHERE discord_event_id = ?",
            (start_iso, datetime.now(timezone.utc).isoformat(), discord_event_id)
        )

        try:
            schedule_reminders_for_event(bot, scheduler, discord_event_id, event.start_time, EVENTS_CHANNEL_ID)
        except Exception:
            log.exception("Failed to reschedule reminders for event update")

        tracked = db_execute(
            "SELECT posted_channel_id, posted_message_id FROM tracked_events WHERE discord_event_id = ?",
            (discord_event_id,), fetch=True
        )
        if tracked:
            ch_id, msg_id = tracked[0]
            ch = bot.get_channel(ch_id) if ch_id else None
            if ch and msg_id:
                try:
                    msg = await ch.fetch_message(msg_id)
                    embed = build_event_embed_from_db(discord_event_id, event.guild)
                    await msg.edit(embed=embed, view=EventViewInFile(discord_event_id, event.guild))
                except discord.NotFound:
                    log.info("Tracked event message missing during update for %s; clearing posted reference.", discord_event_id)
                    db_execute("UPDATE tracked_events SET posted_channel_id = NULL, posted_message_id = NULL WHERE discord_event_id = ?", (discord_event_id,))
                except Exception:
                    log.exception("Failed to update event message on event update")
    except Exception:
        log.exception("Error in on_guild_scheduled_event_update")

# -------------------------
# Raw socket fallback logging and handler
# -------------------------
@bot.event
async def on_socket_response(payload):
    try:
        t = payload.get("t")
        if t and t.startswith("GUILD_SCHEDULED_EVENT"):
            log.info(f"RAW DISPATCH: {t} payload keys: {list(payload.get('d',{}).keys())}")
            if t == "GUILD_SCHEDULED_EVENT_CREATE":
                d = payload.get("d", {})
                bot.loop.create_task(_handle_scheduled_event_create_from_payload(d))
    except Exception:
        log.exception("on_socket_response error")

async def _handle_scheduled_event_create_from_payload(d):
    try:
        discord_event_id = str(d.get("id"))
        name = d.get("name")
        description = d.get("description")
        start_raw = d.get("scheduled_start_time") or d.get("start_time") or d.get("scheduled_start_time_iso")
        start_dt = None
        if start_raw:
            try:
                start_dt = datetime.fromisoformat(start_raw)
            except Exception:
                try:
                    start_dt = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
                except Exception:
                    start_dt = None
        existing = db_execute("SELECT posted_channel_id, posted_message_id FROM tracked_events WHERE discord_event_id = ?", (discord_event_id,), fetch=True)
        now_iso = datetime.now(timezone.utc).isoformat()
        if not existing:
            try:
                db_execute("INSERT INTO tracked_events(guild_id, discord_event_id, start_time, updated_at) VALUES (?, ?, ?, ?)",
                           (None, discord_event_id, start_dt.isoformat() if start_dt else None, now_iso))
            except Exception:
                db_execute("INSERT OR REPLACE INTO tracked_events(guild_id, discord_event_id, start_time, updated_at) VALUES (?, ?, ?, ?)",
                           (None, discord_event_id, start_dt.isoformat() if start_dt else None, now_iso))
        else:
            posted_ch_id, posted_msg_id = existing[0]
            if posted_msg_id:
                try:
                    ch_check = bot.get_channel(posted_ch_id) if posted_ch_id else None
                    if ch_check:
                        try:
                            _ = await ch_check.fetch_message(posted_msg_id)
                            log.info("Fallback: Event %s already has posted message %s — skipping post.", discord_event_id, posted_msg_id)
                            return
                        except discord.NotFound:
                            db_execute("UPDATE tracked_events SET posted_channel_id = NULL, posted_message_id = NULL WHERE discord_event_id = ?", (discord_event_id,))
                        except Exception:
                            log.exception("Fallback: error checking existing posted message for event %s", discord_event_id)
        ch = bot.get_channel(EVENTS_CHANNEL_ID) if EVENTS_CHANNEL_ID else None
        if ch:
            embed = discord.Embed(title=name or "Event", description=description or "", color=discord.Color.blue(), timestamp=datetime.now(timezone.utc))
            if start_dt:
                try:
                    embed.add_field(name="Start", value=start_dt.astimezone(ZoneInfo(POST_TIMEZONE)).strftime("%d.%m.%Y %H:%M %Z"), inline=False)
                except Exception:
                    embed.add_field(name="Start", value=str(start_dt), inline=False)
            view = EventViewInFile(discord_event_id, None)
            sent = await ch.send(embed=embed, view=view)
            db_execute("UPDATE tracked_events SET posted_channel_id = ?, posted_message_id = ?, updated_at = ? WHERE discord_event_id = ?",
                       (ch.id, sent.id, datetime.now(timezone.utc).isoformat(), discord_event_id))
            try:
                schedule_reminders_for_event(bot, scheduler, discord_event_id, start_dt, EVENTS_CHANNEL_ID)
            except Exception:
                log.exception("Failed to schedule reminders in fallback handler")
    except Exception:
        log.exception("Fallback scheduled event create handler failed")

# -------------------------
# Debug commands
# -------------------------
@bot.command()
async def checkevents(ctx):
    await ctx.send(f"EVENTS_CHANNEL_ID={EVENTS_CHANNEL_ID}")
    ch = bot.get_channel(EVENTS_CHANNEL_ID) if EVENTS_CHANNEL_ID else None
    await ctx.send(f"get_channel -> {ch}")
    rows = db_execute("SELECT discord_event_id, start_time, posted_channel_id, posted_message_id FROM tracked_events", fetch=True)
    await ctx.send(f"tracked_events rows: {rows}")

@bot.command()
async def listevents(ctx):
    guild = ctx.guild
    if not guild:
        await ctx.send("Kein Guild-Kontext (bitte im Server-Kanal ausführen).")
        return
    try:
        events = await guild.fetch_scheduled_events()
        if not events:
            await ctx.send("Keine scheduled events in diesem Server gefunden.")
            return
        lines = []
        for e in events:
            entity_type = getattr(e, "entity_type", None)
            channel_id = getattr(e, "channel_id", None)
            start_time = getattr(e, "start_time", None)
            location = getattr(e, "location", None) if hasattr(e, "location") else getattr(e, "entity_metadata", None)
            lines.append(f"- id={e.id} name={e.name!r} entity_type={entity_type} channel_id={channel_id} location={location} start={start_time}")
        text = "\n".join(lines)
        if len(text) > 1900:
            await ctx.send(file=discord.File(io.BytesIO(text.encode()), filename="events.txt"))
        else:
            await ctx.send(f"Scheduled events:\n{text}")
    except Exception:
        log.exception("Failed to fetch scheduled events")
        await ctx.send("Fehler beim Abrufen der scheduled events")

@bot.command()
async def sync_events(ctx, post_channel_id: int = None):
    guild = ctx.guild
    if not guild:
        await ctx.send("Dieses Kommando muss in einem Server (Guild) ausgeführt werden.")
        return
    target_channel_id = post_channel_id if post_channel_id else EVENTS_CHANNEL_ID
    if not target_channel_id:
        await ctx.send("Kein EVENTS_CHANNEL_ID gesetzt und kein channel_id als Parameter übergeben.")
        return
    ch = bot.get_channel(target_channel_id)
    if not ch:
        await ctx.send(f"Kanal {target_channel_id} nicht gefunden oder Bot hat keine Zugriffsrechte.")
        return
    try:
        events = await guild.fetch_scheduled_events()
    except Exception as exc:
        await ctx.send(f"Fehler beim Abrufen der scheduled events: {exc}")
        return
    created = 0
    for ev in events:
        discord_event_id = str(ev.id)
        existing = db_execute("SELECT posted_channel_id, posted_message_id FROM tracked_events WHERE discord_event_id = ?", (discord_event_id,), fetch=True)
        if existing:
            posted_ch_id, posted_msg_id = existing[0]
            if posted_msg_id:
                try:
                    ch_check = bot.get_channel(posted_ch_id) if posted_ch_id else None
                    if ch_check:
                        try:
                            _ = await ch_check.fetch_message(posted_msg_id)
                            continue
                        except discord.NotFound:
                            db_execute("UPDATE tracked_events SET posted_channel_id = NULL, posted_message_id = NULL WHERE discord_event_id = ?", (discord_event_id,))
                        except Exception:
                            log.exception("Error checking existing posted message for event %s", discord_event_id)
        start_iso = None
        try:
            if getattr(ev, "start_time", None):
                start_iso = ev.start_time.isoformat()
        except Exception:
            start_iso = None
        now_iso = datetime.now(timezone.utc).isoformat()
        db_execute("INSERT OR REPLACE INTO tracked_events(guild_id, discord_event_id, start_time, updated_at) VALUES (?, ?, ?, ?)",
                   (guild.id if guild else None, discord_event_id, start_iso, now_iso))
        try:
            embed = discord.Embed(title=ev.name or "Event", description=ev.description or "", color=discord.Color.blue(), timestamp=datetime.now(timezone.utc))
            if getattr(ev, "start_time", None):
                try:
                    embed.add_field(name="Start", value=ev.start_time.astimezone(ZoneInfo(POST_TIMEZONE)).strftime("%d.%m.%Y %H:%M %Z"), inline=False)
                except Exception:
                    embed.add_field(name="Start", value=str(ev.start_time), inline=False)
            view = EventViewInFile(discord_event_id, guild)
            sent = await ch.send(embed=embed, view=view)
            db_execute("UPDATE tracked_events SET posted_channel_id = ?, posted_message_id = ?, updated_at = ? WHERE discord_event_id = ?", (ch.id, sent.id, datetime.now(timezone.utc).isoformat(), discord_event_id))
            try:
                start_dt = datetime.fromisoformat(start_iso) if start_iso else None
                schedule_reminders_for_event(bot, scheduler, discord_event_id, start_dt, target_channel_id)
            except Exception:
                log.exception("Failed to schedule reminders during sync for event %s", discord_event_id)
            created += 1
        except Exception:
            log.exception("Failed to post/track event %s during sync", discord_event_id)
    await ctx.send(f"Sync abgeschlossen: {created} neue Events gepostet/registriert (wenn welche fehlten).")

@bot.command()
async def pyver(ctx):
    import discord as _d
    ver = getattr(_d, "__version__", None) or getattr(_d, "version", "unknown")
    await ctx.send(f"discord.py version: {ver}")

# -------------------------
# Scheduler
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

def schedule_quarter_check():
    scheduler.add_job(check_and_post_quarter_polls, CronTrigger(hour=8, minute=0, timezone=ZoneInfo("Europe/Berlin")), id="quarterly_check", replace_existing=True)

# -------------------------
# Weekly posting helper & daily summary implementation
# -------------------------
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
        log.info("Kein Kanal gefunden: bitte CHANNEL_ID setzen oder verwenden Sie !startpoll in einem Kanal.")
        return
    poll_id = await post_poll_to_channel(channel)
    log.info(f"Posted weekly poll {poll_id} to {channel} at {datetime.now(tz=ZoneInfo(POST_TIMEZONE))}")

def get_last_daily_summary(channel_id: int):
    rows = db_execute("SELECT message_id FROM daily_summaries WHERE channel_id = ?", (channel_id,), fetch=True)
    return rows[0][0] if rows and rows[0][0] is not None else None

def set_last_daily_summary(channel_id: int, message_id: int):
    now = datetime.now(timezone.utc).isoformat()
    db_execute("INSERT OR REPLACE INTO daily_summaries(channel_id, message_id, created_at) VALUES (?, ?, ?)", (channel_id, message_id, now))

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
            log.exception("Failed to delete previous daily summary")
    sent = await channel.send(embed=embed)
    try:
        set_last_daily_summary(channel.id, sent.id)
    except Exception:
        log.exception("Failed to store daily summary message id")

# -------------------------
# Persistent view registration (async)
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
# Commands
# -------------------------
@bot.command()
async def startpoll(ctx):
    poll_id = await post_poll_to_channel(ctx.channel)
    await ctx.send(f"Poll gepostet (id via !listpolls)", delete_after=8)

@bot.command()
async def dailysummary(ctx):
    await post_daily_summary_to(ctx.channel)
    await ctx.send("✅ Daily Summary gesendet (falls neue Inhalte vorhanden).", delete_after=6)

@bot.command()
async def pyver(ctx):
    import discord as _d
    ver = getattr(_d, "__version__", None) or getattr(_d, "version", "unknown")
    await ctx.send(f"discord.py version: {ver}")

# -------------------------
# on_ready & startup
# -------------------------
@bot.event
async def on_ready():
    log.info(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    init_db()
    if not scheduler.running:
        scheduler.start()
    schedule_weekly_post()
    schedule_daily_summary()
    schedule_quarter_check()
    if EVENTS_CHANNEL_ID:
        try:
            rows = db_execute("SELECT discord_event_id, start_time FROM tracked_events", fetch=True) or []
            for discord_event_id, start_iso in rows:
                try:
                    start_dt = datetime.fromisoformat(start_iso) if start_iso else None
                except Exception:
                    continue
                schedule_reminders_for_event(bot, scheduler, discord_event_id, start_dt, EVENTS_CHANNEL_ID)
        except Exception:
            log.exception("Failed to reschedule events on startup")
    else:
        log.info("EVENTS_CHANNEL_ID not set; event reminders will not be scheduled.")
    if not QUARTER_POLL_CHANNEL_ID:
        log.info("QUARTER_POLL_CHANNEL_ID not set; quarterly polls disabled.")
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
