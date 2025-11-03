#!/usr/bin/env python3
"""
Integrated bot.py
- Weekly poll bot with availability voting.
- Event watcher (Guild Scheduled Events) integrated directly in this file (no modules/ folder).
  - Posts event messages to EVENTS_CHANNEL_ID and schedules 24h & 2h reminders.
  - RSVP "Interessiert" button; stores RSVPs in SQLite and updates the message.
- Quarterly poll logic integrated directly in this file (no modules/ folder).
  - Schedules a check daily and posts a quarterly poll 1 week before quarter start to QUARTER_POLL_CHANNEL_ID.
  - Users can add ideas (title + description) and pick days (CSV modal).
- Daily summary wrapper added (post_daily_summary) and scheduled at 09:00 and 18:00 Europe/Berlin.
- All DB tables are created in init_db(). All code is in one file for direct copy.
"""
import os
import sqlite3
from datetime import datetime, timedelta, timezone, date
from zoneinfo import ZoneInfo
import discord
from discord.ext import commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

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
POST_TIMEZONE = "Europe/Berlin"

# New channel envs
EVENTS_CHANNEL_ID = int(os.getenv("EVENTS_CHANNEL_ID", "0")) if os.getenv("EVENTS_CHANNEL_ID") else None
QUARTER_POLL_CHANNEL_ID = int(os.getenv("QUARTER_POLL_CHANNEL_ID", "0")) if os.getenv("QUARTER_POLL_CHANNEL_ID") else None

# -------------------------
# Database helpers & init
# -------------------------
def init_db():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    # polls
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS polls (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS options (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            poll_id TEXT NOT NULL,
            option_text TEXT NOT NULL,
            created_at TEXT NOT NULL,
            author_id INTEGER
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS votes (
            poll_id TEXT NOT NULL,
            option_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            UNIQUE(poll_id, option_id, user_id)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS availability (
            poll_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            slot TEXT NOT NULL,
            UNIQUE(poll_id, user_id, slot)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_summaries (
            channel_id INTEGER PRIMARY KEY,
            message_id INTEGER,
            created_at TEXT NOT NULL
        )
        """
    )
    # events tables
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS tracked_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            discord_event_id TEXT NOT NULL UNIQUE,
            posted_channel_id INTEGER,
            posted_message_id INTEGER,
            start_time TEXT,
            updated_at TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS event_rsvps (
            discord_event_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            UNIQUE(discord_event_id, user_id)
        )
        """
    )
    # quarter poll tables
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS quarter_polls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            quarter_start DATE NOT NULL,
            posted_channel_id INTEGER,
            posted_message_id INTEGER,
            created_at TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS quarter_options (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            poll_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            created_at TEXT NOT NULL,
            author_id INTEGER
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS quarter_votes (
            poll_id INTEGER NOT NULL,
            option_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            UNIQUE(poll_id, option_id, user_id)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS quarter_availability (
            poll_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            day TEXT NOT NULL,
            UNIQUE(poll_id, user_id, day)
        )
        """
    )
    con.commit()
    con.close()

def db_execute(query, params=(), fetch=False, many=False):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    if many:
        cur.executemany(query, params)
    else:
        cur.execute(query, params)
    rows = None
    if fetch:
        rows = cur.fetchall()
    con.commit()
    con.close()
    return rows

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

def get_votes_for_poll(poll_id: str):
    return db_execute("SELECT option_id, user_id FROM votes WHERE poll_id = ?", (poll_id,), fetch=True) or []

def persist_availability(poll_id: str, user_id: int, slots: list):
    db_execute("DELETE FROM availability WHERE poll_id = ? AND user_id = ?", (poll_id, user_id))
    if slots:
        db_execute("INSERT OR IGNORE INTO availability(poll_id, user_id, slot) VALUES (?, ?, ?)", [(poll_id, user_id, s) for s in slots], many=True)

def get_availability_for_poll(poll_id: str):
    return db_execute("SELECT user_id, slot FROM availability WHERE poll_id = ?", (poll_id,), fetch=True) or []

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
        description="Gib eigene Ideen ein, stimme ab oder trage deine Zeiten ein!",
        color=discord.Color.blurple(),
        timestamp=datetime.now()
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

# -------------------------
# UI: Poll UI (buttons, modals)
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
            if interaction.message:
                await interaction.message.edit(embed=embed, view=new_view)
        except Exception:
            pass
        await interaction.response.send_message("✅ Idee hinzugefügt.", ephemeral=True)

class AddOptionButton(discord.ui.Button):
    def __init__(self, poll_id: str):
        super().__init__(label="📝 Idee hinzufügen", style=discord.ButtonStyle.secondary)
        self.poll_id = poll_id
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(SuggestModal(self.poll_id))

class AddAvailabilityButton(discord.ui.Button):
    def __init__(self, poll_id: str):
        super().__init__(label="🕓 Verfügbarkeit hinzufügen", style=discord.ButtonStyle.success)
        self.poll_id = poll_id
    async def callback(self, interaction: discord.Interaction):
        view = AvailabilityDayView(self.poll_id, day_index=0, for_user=interaction.user.id)
        embed = discord.Embed(title="🕓 Verfügbarkeit auswählen", description="Wähle Stunden für den angezeigten Tag (Mo.–So.). Nach Auswahl: Absenden.", color=discord.Color.green(), timestamp=datetime.now())
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

# Open-edit button (gear icon)
class OpenEditOwnIdeasButton(discord.ui.Button):
    def __init__(self, poll_id: str):
        super().__init__(label="⚙️", style=discord.ButtonStyle.secondary)
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
                                await msg.edit(embed=new_embed, view=new_view)
                            except Exception:
                                pass
                        break
        except Exception:
            pass
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

# Availability UI
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
        super().__init__(label="✅ Absenden", style=discord.ButtonStyle.success)
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
        super().__init__(label="🗑️ Gespeicherte Zeit löschen", style=discord.ButtonStyle.danger)
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

class PollButton(discord.ui.Button):
    def __init__(self, poll_id: str, option_id: int, option_text: str):
        super().__init__(label=option_text, style=discord.ButtonStyle.primary)
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
        await interaction.response.edit_message(embed=embed, view=new_view)

# -------------------------
# Posting polls & daily summaries
# -------------------------
async def post_poll_to_channel(channel: discord.abc.Messageable):
    poll_id = datetime.now(ZoneInfo(POST_TIMEZONE)).strftime("%Y%m%dT%H%M%S")
    create_poll_record(poll_id)
    embed = generate_poll_embed_from_db(poll_id, channel.guild if isinstance(channel, discord.TextChannel) else None)
    view = PollView(poll_id)
    await channel.send(embed=embed, view=view)
    return poll_id

# Wrapper for scheduler
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
        print("Kein Kanal gefunden für Daily Summary.")
        return
    await post_daily_summary_to(channel)

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
    since = datetime.now(tz) - timedelta(days=1)
    new_options = get_options_since(poll_id, since)
    matches = compute_matches_for_poll_from_db(poll_id)
    if (not new_options) and (not matches):
        return
    embed = discord.Embed(title="🗓️ Tages-Update: Matches & neue Ideen", color=discord.Color.green(), timestamp=datetime.now())
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
            pass
    sent = await channel.send(embed=embed)
    try:
        set_last_daily_summary(channel.id, sent.id)
    except Exception:
        pass

# -------------------------
# Event integration (in-file)
# -------------------------
def _event_db_execute(query, params=(), fetch=False, many=False):
    return db_execute(query, params, fetch=fetch, many=many)

class EventView(discord.ui.View):
    def __init__(self, discord_event_id: str, guild: discord.Guild | None):
        super().__init__(timeout=None)
        self.discord_event_id = discord_event_id
        self.guild = guild

    @discord.ui.button(label="⚜️ Interessiert", style=discord.ButtonStyle.primary)
    async def interested(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = interaction.user.id
        rows = _event_db_execute("SELECT status FROM event_rsvps WHERE discord_event_id = ? AND user_id = ?", (self.discord_event_id, user_id), fetch=True)
        if rows and rows[0][0] == "interested":
            _event_db_execute("DELETE FROM event_rsvps WHERE discord_event_id = ? AND user_id = ?", (self.discord_event_id, user_id))
            await interaction.response.send_message("Deine Interesse wurde entfernt.", ephemeral=True)
        else:
            _event_db_execute("INSERT OR REPLACE INTO event_rsvps(discord_event_id, user_id, status) VALUES (?, ?, ?)", (self.discord_event_id, user_id, "interested"))
            await interaction.response.send_message("Du bist als interessiert vermerkt.", ephemeral=True)
        try:
            tracked = _event_db_execute("SELECT posted_channel_id, posted_message_id FROM tracked_events WHERE discord_event_id = ?", (self.discord_event_id,), fetch=True)
            if tracked:
                channel_id, message_id = tracked[0]
                ch = interaction.client.get_channel(channel_id)
                if ch:
                    msg = await ch.fetch_message(message_id)
                    if msg:
                        embed = build_event_embed_from_db(self.discord_event_id, self.guild)
                        await msg.edit(embed=embed, view=EventView(self.discord_event_id, self.guild))
        except Exception:
            pass

def build_event_embed_from_db(discord_event_id: str, guild: discord.Guild | None):
    rows = _event_db_execute("SELECT discord_event_id, start_time FROM tracked_events WHERE discord_event_id = ?", (discord_event_id,), fetch=True)
    start_time = None
    if rows:
        start_time = rows[0][1]
    r = _event_db_execute("SELECT user_id FROM event_rsvps WHERE discord_event_id = ?", (discord_event_id,), fetch=True) or []
    user_ids = [x[0] for x in r]
    names = []
    for uid in user_ids:
        if guild:
            m = guild.get_member(uid)
            if m:
                names.append(m.display_name)
                continue
        names.append(str(uid))
    embed = discord.Embed(title="📣 Event", description="Details", color=discord.Color.blurple(), timestamp=datetime.now(timezone.utc))
    if start_time:
        try:
            dt = datetime.fromisoformat(start_time)
            embed.add_field(name="Start", value=dt.astimezone(ZoneInfo("Europe/Berlin")).strftime("%d.%m.%Y %H:%M %Z"), inline=False)
        except Exception:
            embed.add_field(name="Start", value=start_time, inline=False)
    embed.add_field(name="Interessierte", value=", ".join(names) if names else "Keine", inline=False)
    return embed

def schedule_reminders_for_event(bot, scheduler, discord_event_id: str, start_time, events_channel_id):
    # remove existing jobs
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
            return
        embed = build_event_embed_from_db(discord_event_id, None)
        embed.title = f"📣 Event — startet in ~{hours_before} Stunden"
        view = EventView(discord_event_id, None)
        tracked = _event_db_execute("SELECT posted_channel_id, posted_message_id FROM tracked_events WHERE discord_event_id = ?", (discord_event_id,), fetch=True)
        if tracked:
            old_ch_id, old_msg_id = tracked[0]
            try:
                old_ch = bot.get_channel(old_ch_id)
                if old_ch:
                    old_msg = await old_ch.fetch_message(old_msg_id)
                    if old_msg:
                        await old_msg.delete()
            except Exception:
                pass
        sent = await ch.send(embed=embed, view=view)
        _event_db_execute("UPDATE tracked_events SET posted_channel_id = ?, posted_message_id = ?, updated_at = ? WHERE discord_event_id = ?", (ch.id, sent.id, datetime.now(timezone.utc).isoformat(), discord_event_id))
    now = datetime.now(timezone.utc)
    if t24 > now:
        scheduler.add_job(lambda: bot.loop.create_task(reminder_coro(events_channel_id, discord_event_id, 24)), trigger=DateTrigger(run_date=t24), id=f"event_reminder_24_{discord_event_id}", replace_existing=True)
    elif t24 <= now < start_time:
        bot.loop.create_task(reminder_coro(events_channel_id, discord_event_id, 24))
    if t2 > now:
        scheduler.add_job(lambda: bot.loop.create_task(reminder_coro(events_channel_id, discord_event_id, 2)), trigger=DateTrigger(run_date=t2), id=f"event_reminder_2_{discord_event_id}", replace_existing=True)
    elif t2 <= now < start_time:
        bot.loop.create_task(reminder_coro(events_channel_id, discord_event_id, 2))

# Event listeners
@bot.event
async def on_guild_scheduled_event_create(event: discord.GuildScheduledEvent):
    if not EVENTS_CHANNEL_ID:
        return
    guild = event.guild
    discord_event_id = str(event.id)
    start_iso = event.start_time.isoformat() if event.start_time else None
    db_execute("INSERT OR REPLACE INTO tracked_events(guild_id, discord_event_id, start_time, updated_at) VALUES (?, ?, ?, ?)", (guild.id, discord_event_id, start_iso, datetime.now(timezone.utc).isoformat()))
    ch = bot.get_channel(EVENTS_CHANNEL_ID)
    if ch:
        embed = discord.Embed(title=event.name or "Event", description=event.description or "", color=discord.Color.blue(), timestamp=datetime.now(timezone.utc))
        if event.start_time:
            embed.add_field(name="Start", value=event.start_time.astimezone(ZoneInfo("Europe/Berlin")).strftime("%d.%m.%Y %H:%M %Z"), inline=False)
        view = EventView(discord_event_id, guild)
        msg = await ch.send(embed=embed, view=view)
        db_execute("UPDATE tracked_events SET posted_channel_id = ?, posted_message_id = ?, updated_at = ? WHERE discord_event_id = ?", (ch.id, msg.id, datetime.now(timezone.utc).isoformat(), discord_event_id))
        try:
            schedule_reminders_for_event(bot, scheduler, discord_event_id, event.start_time, EVENTS_CHANNEL_ID)
        except Exception:
            pass

@bot.event
async def on_guild_scheduled_event_update(event: discord.GuildScheduledEvent):
    if not EVENTS_CHANNEL_ID:
        return
    discord_event_id = str(event.id)
    start_iso = event.start_time.isoformat() if event.start_time else None
    db_execute("UPDATE tracked_events SET start_time = ?, updated_at = ? WHERE discord_event_id = ?", (start_iso, datetime.now(timezone.utc).isoformat(), discord_event_id))
    try:
        schedule_reminders_for_event(bot, scheduler, discord_event_id, event.start_time, EVENTS_CHANNEL_ID)
    except Exception:
        pass
    tracked = db_execute("SELECT posted_channel_id, posted_message_id FROM tracked_events WHERE discord_event_id = ?", (discord_event_id,), fetch=True)
    if tracked:
        ch_id, msg_id = tracked[0]
        ch = bot.get_channel(ch_id)
        if ch:
            try:
                msg = await ch.fetch_message(msg_id)
                embed = build_event_embed_from_db(discord_event_id, event.guild)
                await msg.edit(embed=embed, view=EventView(discord_event_id, event.guild))
            except Exception:
                pass

@bot.event
async def on_guild_scheduled_event_delete(event: discord.GuildScheduledEvent):
    discord_event_id = str(event.id)
    tracked = db_execute("SELECT posted_channel_id, posted_message_id FROM tracked_events WHERE discord_event_id = ?", (discord_event_id,), fetch=True)
    if tracked:
        ch_id, msg_id = tracked[0]
        try:
            ch = bot.get_channel(ch_id)
            if ch:
                msg = await ch.fetch_message(msg_id)
                await msg.delete()
        except Exception:
            pass
    db_execute("DELETE FROM tracked_events WHERE discord_event_id = ?", (discord_event_id,))
    db_execute("DELETE FROM event_rsvps WHERE discord_event_id = ?", (discord_event_id,))

# reschedule on startup
def reschedule_all_events():
    rows = db_execute("SELECT discord_event_id, start_time FROM tracked_events", fetch=True) or []
    for discord_event_id, start_iso in rows:
        try:
            start_dt = datetime.fromisoformat(start_iso)
        except Exception:
            continue
        schedule_reminders_for_event(bot, scheduler, discord_event_id, start_dt, EVENTS_CHANNEL_ID)

# -------------------------
# Quarterly poll integration (in-file)
# -------------------------
# Quarter helper functions
def build_quarter_embed(poll_id: int, guild: discord.Guild | None):
    options = db_execute("SELECT id, title, description FROM quarter_options WHERE poll_id = ? ORDER BY id ASC", (poll_id,), fetch=True) or []
    votes = db_execute("SELECT option_id, user_id FROM quarter_votes WHERE poll_id = ?", (poll_id,), fetch=True) or []
    avail = db_execute("SELECT user_id, day FROM quarter_availability WHERE poll_id = ?", (poll_id,), fetch=True) or []
    avail_map = {}
    for uid, day in avail:
        avail_map.setdefault(uid, set()).add(day)
    votes_map = {}
    for opt_id, uid in votes:
        votes_map.setdefault(opt_id, []).append(uid)
    embed = discord.Embed(title="🗓️ Quartals‑Planung (Long-term)", color=discord.Color.blurple(), timestamp=datetime.now(timezone.utc))
    for opt_id, title, desc in options:
        voters = votes_map.get(opt_id, [])
        header = f"🗳️ {len(voters)} Stimmen"
        value = header
        if desc:
            value += f"\n{desc}"
        if voters:
            day_map = {}
            for uid in voters:
                for d in avail_map.get(uid, set()):
                    day_map.setdefault(d, []).append(uid)
            common = [(d, ulist) for d, ulist in day_map.items() if len(ulist) >= 1]
            if common:
                max_count = max(len(ulist) for (_, ulist) in common)
                best = [(d, ulist) for (d, ulist) in common if len(ulist) == max_count]
                lines = []
                for d, ulist in best:
                    try:
                        dd = datetime.fromisoformat(d).date()
                        names = [user_display_name(guild, u) for u in ulist]
                        lines.append(f"{dd.isoformat()}: {', '.join(names)}")
                    except Exception:
                        lines.append(f"{d}: {', '.join([str(uid) for uid in ulist])}")
                value += "\n✅ Beliebteste Tage:\n" + "\n".join(lines)
        embed.add_field(name=title, value=value or "(keine Beschreibung)", inline=False)
    return embed

class QuarterIdeaModal(discord.ui.Modal, title="Neue Quartals-Idee"):
    title_input = discord.ui.TextInput(label="Titel", max_length=100)
    desc = discord.ui.TextInput(label="Kurzbeschreibung", style=discord.TextStyle.long, required=False, max_length=500)
    def __init__(self, poll_id: int):
        super().__init__()
        self.poll_id = poll_id
    async def on_submit(self, interaction: discord.Interaction):
        t = str(self.title_input.value).strip()
        d = str(self.desc.value).strip()
        db_execute("INSERT INTO quarter_options(poll_id, title, description, created_at, author_id) VALUES (?, ?, ?, ?, ?)", (self.poll_id, t, d, datetime.now(timezone.utc).isoformat(), interaction.user.id))
        await interaction.response.send_message("✅ Idee hinzugefügt.", ephemeral=True)
        try:
            rows = db_execute("SELECT posted_channel_id, posted_message_id FROM quarter_polls WHERE id = ?", (self.poll_id,), fetch=True)
            if rows:
                ch_id, msg_id = rows[0]
                ch = interaction.client.get_channel(ch_id)
                if ch:
                    msg = await ch.fetch_message(msg_id)
                    if msg:
                        embed = build_quarter_embed(self.poll_id, interaction.guild)
                        view = build_quarter_view(self.poll_id)
                        await msg.edit(embed=embed, view=view)
        except Exception:
            pass

class QuarterAddIdeaButton(discord.ui.Button):
    def __init__(self, poll_id: int):
        super().__init__(label="➕ Neue Idee (mit Beschreibung)", style=discord.ButtonStyle.secondary)
        self.poll_id = poll_id
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(QuarterIdeaModal(self.poll_id))

class QuarterVoteButton(discord.ui.Button):
    def __init__(self, poll_id: int, option_id: int, title: str):
        super().__init__(label=title[:80], style=discord.ButtonStyle.primary)
        self.poll_id = poll_id
        self.option_id = option_id
    async def callback(self, interaction: discord.Interaction):
        uid = interaction.user.id
        rows = db_execute("SELECT 1 FROM quarter_votes WHERE poll_id = ? AND option_id = ? AND user_id = ?", (self.poll_id, self.option_id, uid), fetch=True)
        if rows:
            db_execute("DELETE FROM quarter_votes WHERE poll_id = ? AND option_id = ? AND user_id = ?", (self.poll_id, self.option_id, uid))
            await interaction.response.send_message("Stimme entfernt.", ephemeral=True)
        else:
            db_execute("INSERT OR IGNORE INTO quarter_votes(poll_id, option_id, user_id) VALUES (?, ?, ?)", (self.poll_id, self.option_id, uid))
            await interaction.response.send_message("Stimme gespeichert.", ephemeral=True)
        try:
            rows = db_execute("SELECT posted_channel_id, posted_message_id FROM quarter_polls WHERE id = ?", (self.poll_id,), fetch=True)
            if rows:
                ch_id, msg_id = rows[0]
                ch = interaction.client.get_channel(ch_id)
                if ch:
                    msg = await ch.fetch_message(msg_id)
                    if msg:
                        embed = build_quarter_embed(self.poll_id, interaction.guild)
                        view = build_quarter_view(self.poll_id)
                        await msg.edit(embed=embed, view=view)
        except Exception:
            pass

class QuarterPickDaysModal(discord.ui.Modal, title="Tage auswählen (CSV)"):
    dates = discord.ui.TextInput(label="Tage (z. B. 2026-01-05,2026-01-12)", style=discord.TextStyle.long)
    def __init__(self, poll_id: int):
        super().__init__()
        self.poll_id = poll_id
    async def on_submit(self, interaction: discord.Interaction):
        raw = str(self.dates.value).strip()
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        saved = 0
        for p in parts:
            try:
                d = datetime.fromisoformat(p).date()
                db_execute("INSERT OR IGNORE INTO quarter_availability(poll_id, user_id, day) VALUES (?, ?, ?)", (self.poll_id, interaction.user.id, d.isoformat()))
                saved += 1
            except Exception:
                pass
        await interaction.response.send_message(f"{saved} Tage gespeichert.", ephemeral=True)
        try:
            rows = db_execute("SELECT posted_channel_id, posted_message_id FROM quarter_polls WHERE id = ?", (self.poll_id,), fetch=True)
            if rows:
                ch_id, msg_id = rows[0]
                ch = interaction.client.get_channel(ch_id)
                if ch:
                    msg = await ch.fetch_message(msg_id)
                    if msg:
                        embed = build_quarter_embed(self.poll_id, interaction.guild)
                        view = build_quarter_view(self.poll_id)
                        await msg.edit(embed=embed, view=view)
        except Exception:
            pass

class QuarterPickDaysButton(discord.ui.Button):
    def __init__(self, poll_id: int):
        super().__init__(label="📅 Tage wählen (CSV YYYY-MM-DD)", style=discord.ButtonStyle.secondary)
        self.poll_id = poll_id
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(QuarterPickDaysModal(self.poll_id))

class QuarterView(discord.ui.View):
    def __init__(self, poll_id: int):
        super().__init__(timeout=None)
        self.poll_id = poll_id
        self.add_item(QuarterAddIdeaButton(poll_id))
        self.add_item(QuarterPickDaysButton(poll_id))
        options = db_execute("SELECT id, title FROM quarter_options WHERE poll_id = ? ORDER BY id ASC", (poll_id,), fetch=True) or []
        for opt_id, title in options:
            self.add_item(QuarterVoteButton(poll_id, opt_id, title))

def build_quarter_view(poll_id: int):
    return QuarterView(poll_id)

def check_and_post_quarter_polls():
    today = datetime.now(timezone.utc).date()
    candidates = []
    for year in [today.year, today.year + 1]:
        for m in (1,4,7,10):
            qstart = date(year, m, 1)
            candidates.append(qstart)
    for qstart in candidates:
        post_date = qstart - timedelta(weeks=1)
        if post_date == today:
            existing = db_execute("SELECT id FROM quarter_polls WHERE quarter_start = ?", (qstart.isoformat(),), fetch=True)
            if not existing:
                created_at = datetime.now(timezone.utc).isoformat()
                db_execute("INSERT INTO quarter_polls(quarter_start, created_at) VALUES (?, ?)", (qstart.isoformat(), created_at))
                poll_id = db_execute("SELECT id FROM quarter_polls WHERE quarter_start = ? ORDER BY id DESC LIMIT 1", (qstart.isoformat(),), fetch=True)[0][0]
                async def _post():
                    ch = bot.get_channel(QUARTER_POLL_CHANNEL_ID)
                    if not ch:
                        return
                    embed = build_quarter_embed(poll_id, None)
                    view = build_quarter_view(poll_id)
                    sent = await ch.send(embed=embed, view=view)
                    db_execute("UPDATE quarter_polls SET posted_channel_id = ?, posted_message_id = ? WHERE id = ?", (ch.id, sent.id, poll_id))
                bot.loop.create_task(_post())

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
    from apscheduler.triggers.cron import CronTrigger
    scheduler.add_job(check_and_post_quarter_polls, CronTrigger(hour=8, minute=0, timezone=ZoneInfo("Europe/Berlin")), id="quarterly_check", replace_existing=True)

# -------------------------
# Weekly post helper
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
        print("Kein Kanal gefunden: bitte CHANNEL_ID setzen oder verwenden Sie !startpoll in einem Kanal.")
        return
    poll_id = await post_poll_to_channel(channel)
    print(f"Posted weekly poll {poll_id} to {channel} at {datetime.now()}")

# -------------------------
# Commands
# -------------------------
@bot.command()
async def startpoll(ctx):
    poll_id = await post_poll_to_channel(ctx.channel)
    await ctx.send(f"Poll gepostet (id={poll_id})", delete_after=8)

@bot.command()
async def dailysummary(ctx):
    await post_daily_summary_to(ctx.channel)
    await ctx.send("✅ Daily Summary gesendet (falls neue Inhalte vorhanden).", delete_after=6)

# -------------------------
# Bot events & startup
# -------------------------
@bot.event
async def on_ready():
    print(f"✅ Eingeloggt als {bot.user} (ID: {bot.user.id})")
    init_db()
    if not scheduler.running:
        scheduler.start()
    schedule_weekly_post()
    schedule_daily_summary()
    schedule_quarter_check()
    # reschedule events reminders
    if EVENTS_CHANNEL_ID:
        try:
            reschedule_all_events()
        except Exception:
            pass
    else:
        print("EVENTS_CHANNEL_ID not set; event reminders will not be scheduled.")
    if not QUARTER_POLL_CHANNEL_ID:
        print("QUARTER_POLL_CHANNEL_ID not set; quarterly polls disabled.")

# -------------------------
# Entrypoint
# -------------------------
if __name__ == "__main__":
    if not BOT_TOKEN:
        print("Bitte BOT_TOKEN als Umgebungsvariable setzen.")
        raise SystemExit(1)
    init_db()
    bot.run(BOT_TOKEN)
