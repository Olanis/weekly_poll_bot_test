#!/usr/bin/env python3
"""
Full bot.py — poll id hidden in embeds

This is your integrated bot.py with one change you requested:
- The poll_id is no longer shown in the public poll embed title.
  You can still obtain poll IDs via the !listpolls command (server-side DB).
All other functionality (persistent custom_ids, repair/recover commands, daily summaries,
scheduling, rate-safe registration) is kept as in the prior version.
"""
from __future__ import annotations

import os
import re
import io
import asyncio
import sqlite3
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import logging

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
    con.commit()

    # Ensure daily_summaries has a column to store last_matches (JSON).
    # This allows us to detect which matches are new since last summary.
    cur.execute("PRAGMA table_info(daily_summaries)")
    cols = [r[1] for r in cur.fetchall()]
    if "last_matches" not in cols:
        try:
            cur.execute("ALTER TABLE daily_summaries ADD COLUMN last_matches TEXT")
            con.commit()
        except Exception:
            # If ALTER fails for any reason, ignore — we'll handle missing column gracefully elsewhere.
            log.exception("Failed to add last_matches column to daily_summaries")
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
            # normalize users to ints (they already are) and sort for determinism when serializing
            for b in best:
                b["users"] = sorted(b["users"])
            results[opt_text] = best
    return results

def generate_poll_embed_from_db(poll_id: str, guild: discord.Guild | None = None):
    # NOTE: poll_id intentionally not displayed in the title per user's request.
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
# UI: Views & Buttons with persistent custom_ids
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
                    if em.title and "Worauf" in em.title:
                        rows = db_execute("SELECT id FROM polls ORDER BY created_at DESC LIMIT 1", fetch=True)
                        if rows:
                            poll_id = rows[0][0]
                            new_embed = generate_poll_embed_from_db(poll_id, interaction.guild)
                            new_view = PollView(poll_id)
                            try:
                                bot.add_view(new_view)
                                await msg.edit(embed=new_embed, view=new_view)
                            except Exception:
                                log.exception("Failed to update public poll after delete")
                        break
        except Exception:
            log.exception("Error while best-effort updating public message")
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
# Posting polls
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
# Repair & Recover commands
# -------------------------
@bot.command()
async def repairpoll(ctx, channel_id: int, message_id: int, poll_id: str = None):
    """
    Repair a poll message that was created with an older bot instance.
    Usage: !repairpoll <channel_id> <message_id> [poll_id]
    Note: because poll IDs are no longer visible in the embed, it's recommended to call !listpolls first
    and pass the poll_id explicitly when using repairpoll.
    """
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
    """
    Try to reconstruct a poll from an existing embed message and attach a working view.
    Usage: !recoverpollfrommessage <channel_id> <message_id>
    """
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
# List polls command
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
# Daily summary helpers
# -------------------------
def get_last_daily_summary(channel_id: int):
    rows = db_execute("SELECT message_id FROM daily_summaries WHERE channel_id = ?", (channel_id,), fetch=True)
    return rows[0][0] if rows and rows[0][0] is not None else None

def get_last_daily_summary_info(channel_id: int):
    """
    Returns dict with keys: message_id (int or None), created_at (datetime or None), last_matches (dict or None)
    """
    rows = db_execute("SELECT message_id, created_at, last_matches FROM daily_summaries WHERE channel_id = ?", (channel_id,), fetch=True)
    if not rows:
        return None
    msg_id, created_at, last_matches = rows[0]
    created_dt = None
    if created_at:
        try:
            created_dt = datetime.fromisoformat(created_at)
            # created_at is stored in UTC (see set_last_daily_summary), make it timezone-aware UTC
            if created_dt.tzinfo is None:
                created_dt = created_dt.replace(tzinfo=timezone.utc)
        except Exception:
            created_dt = None
    parsed_matches = None
    if last_matches:
        try:
            parsed_matches = json.loads(last_matches)
        except Exception:
            parsed_matches = None
    return {"message_id": msg_id, "created_at": created_dt, "last_matches": parsed_matches}

def set_last_daily_summary(channel_id: int, message_id: int, matches: dict | None = None):
    now = datetime.now(timezone.utc).isoformat()
    matches_json = json.dumps(matches) if matches is not None else None
    # Use INSERT OR REPLACE so we update the row for this channel
    db_execute("INSERT OR REPLACE INTO daily_summaries(channel_id, message_id, created_at, last_matches) VALUES (?, ?, ?, ?)",
               (channel_id, message_id, now, matches_json))

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
    last_info = get_last_daily_summary_info(channel.id)
    if last_info and last_info.get("created_at"):
        # Use last summary time as since
        since_dt_utc = last_info["created_at"].astimezone(timezone.utc)
    else:
        # fallback: last 24 hours
        since_dt_utc = datetime.now(timezone.utc) - timedelta(days=1)

    # find new options since last summary
    new_options = get_options_since(poll_id, since_dt_utc)

    # compute current matches (based on current DB state)
    current_matches = compute_matches_for_poll_from_db(poll_id)
    # normalize current_matches for comparison (ensure sorted user lists)
    norm_current = {}
    for opt_text, infos in current_matches.items():
        norm_infos = []
        for info in infos:
            norm_infos.append({"slot": info["slot"], "users": sorted(info["users"])})
        norm_current[opt_text] = norm_infos

    # load last matches snapshot (if any) and compute which matches are new
    last_matches = last_info["last_matches"] if last_info else None

    def matches_to_set(match_map):
        """Convert match mapping to set of tuples (opt_text, slot, tuple(users)) for comparison."""
        s = set()
        if not match_map:
            return s
        for opt_text, infos in match_map.items():
            for info in infos:
                users_tuple = tuple(sorted(info.get("users", [])))
                s.add((opt_text, info.get("slot"), users_tuple))
        return s

    set_current = matches_to_set(norm_current)
    set_last = matches_to_set(last_matches) if last_matches else set()

    new_matches_set = set_current - set_last

    # Build new_matches mapping for embed (only include matches that are new)
    new_matches = {}
    if new_matches_set:
        for (opt_text, slot, users_tuple) in new_matches_set:
            new_matches.setdefault(opt_text, []).append({"slot": slot, "users": list(users_tuple)})

    # Only post if there are either new options or new matches since last summary
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
        embed.add_field(name="🤝 Matches", value="Keine neuen gemeinsamen Zeiten seit der letzten Zusammenfassung.", inline=False)

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

    last_msg_info = get_last_daily_summary_info(channel.id)
    if last_msg_info and last_msg_info.get("message_id"):
        try:
            prev = await channel.fetch_message(last_msg_info["message_id"])
            if prev:
                await prev.delete()
        except discord.NotFound:
            pass
        except Exception:
            log.exception("Failed deleting previous daily summary")

    sent = await channel.send(embed=embed)
    try:
        # store the full current matches as snapshot for next comparison
        set_last_daily_summary(channel.id, sent.id, norm_current)
    except Exception:
        log.exception("Failed saving daily summary id and snapshot")

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
# Commands: startpoll & dailysummary
# -------------------------
@bot.command()
async def startpoll(ctx):
    """Manually post a poll in the current channel."""
    try:
        poll_id = await post_poll_to_channel(ctx.channel)
        await ctx.send(f"Poll gepostet (id via !listpolls)", delete_after=8)
    except Exception as e:
        log.exception("startpoll failed")
        await ctx.send(f"Fehler beim Erstellen der Umfrage: {e}")

@bot.command()
async def dailysummary(ctx):
    """Manually post/update the daily summary in the current channel."""
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
