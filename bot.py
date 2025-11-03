#!/usr/bin/env python3
"""
bot.py — Events, Reminders, RSVP, Polls, Availability & Matching, plus Daily Summary & Weekly Poll scheduler.

This is a complete, ready-to-run replacement file. It continues from the previous working
state and adds:

- Daily summary cron jobs (morning/evening) that post a short summary about:
  - recently added polls
  - top availability matches (across polls)
  - optionally other summary items later
- Weekly poll scheduler (posts a poll weekly on Sunday at 12:00 in POST_TIMEZONE)
- Persistence for polls includes posted_channel_id and posted_message_id (already present)
- Functions:
  - schedule_weekly_post()
  - schedule_daily_summary()
  - post_daily_summary_to_channel(channel)
  - job_post_weekly()
- Startup registers cron jobs and keeps previous behavior intact.

Environment variables:
- BOT_TOKEN (required)
- POLL_DB (optional; default polls.sqlite)
- EVENTS_CHANNEL_ID (optional)
- POST_TIMEZONE (optional; default Europe/Berlin)
"""
from __future__ import annotations

import os
import sqlite3
import logging
import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import re
from typing import Optional

import discord
from discord.ext import commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.cron import CronTrigger

# -------------------------
# Logging & config
# -------------------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

intents = discord.Intents.default()
intents.message_content = True
intents.guild_scheduled_events = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

DB_PATH = os.getenv("POLL_DB", "polls.sqlite")
BOT_TOKEN = os.getenv("BOT_TOKEN")
EVENTS_CHANNEL_ID = int(os.getenv("EVENTS_CHANNEL_ID", "0")) if os.getenv("EVENTS_CHANNEL_ID") else None
POST_TIMEZONE = os.getenv("POST_TIMEZONE", "Europe/Berlin")

# -------------------------
# DB helpers & init
# -------------------------
def init_db():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    # tracked events and RSVPs
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
    # polls
    cur.execute("""
        CREATE TABLE IF NOT EXISTS polls (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            posted_channel_id INTEGER,
            posted_message_id INTEGER
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
    # availability table: one row per poll,user,day,hour
    cur.execute("""
        CREATE TABLE IF NOT EXISTS availability (
            poll_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            day TEXT NOT NULL,
            hour INTEGER NOT NULL,
            UNIQUE(poll_id, user_id, day, hour)
        )
    """)
    # daily summaries (persist last summary message per channel)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS daily_summaries (
            channel_id INTEGER PRIMARY KEY,
            message_id INTEGER,
            last_run TEXT NOT NULL
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
# Scheduler
# -------------------------
scheduler = AsyncIOScheduler(timezone=ZoneInfo(POST_TIMEZONE))
bot_inst = bot
scheduler_inst = scheduler

# -------------------------
# Utilities
# -------------------------
DAY_NAMES = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]

def get_user_display(guild: discord.Guild | None, user_id: int) -> str:
    if guild:
        m = guild.get_member(user_id)
        if m:
            return m.display_name
    u = bot.get_user(user_id)
    return getattr(u, "name", str(user_id))

# parse slot inputs like "Mo18,Di19" or "Mo-18" or "Mo 18"
_slot_re = re.compile(r"^(?P<day>[A-Za-zÄÖÜäöü]{2,9})\s*[-]?\s*(?P<hour>\d{1,2})$")

def parse_slots_text(text: str) -> list[tuple[str,int]]:
    res = []
    parts = re.split(r"[,\n;]+", text)
    lookup = {
        "mo":"Mo","montag":"Mo",
        "di":"Di","dienstag":"Di",
        "mi":"Mi","mittwoch":"Mi",
        "do":"Do","donnerstag":"Do",
        "fr":"Fr","freitag":"Fr",
        "sa":"Sa","samstag":"Sa",
        "so":"So","sonntag":"So"
    }
    for p in parts:
        p = p.strip()
        if not p:
            continue
        m = _slot_re.match(p)
        if m:
            rawday = m.group("day").lower()
            day_norm = lookup.get(rawday[:len(rawday)], lookup.get(rawday, rawday[:2].capitalize()))
            try:
                hour = int(m.group("hour"))
            except Exception:
                continue
            if 0 <= hour <= 23 and day_norm in DAY_NAMES:
                res.append((day_norm, hour))
    return res

# -------------------------
# Event embed / RSVP helpers
# -------------------------
def build_event_embed_from_db(discord_event_id: str, guild: discord.Guild | None = None) -> discord.Embed:
    rows = db_execute("SELECT discord_event_id, start_time FROM tracked_events WHERE discord_event_id = ?", (discord_event_id,), fetch=True) or []
    start_time = rows[0][1] if rows else None
    r = db_execute("SELECT user_id, status FROM event_rsvps WHERE discord_event_id = ?", (discord_event_id,), fetch=True) or []
    by_status: dict[str, list[int]] = {}
    for uid, status in r:
        by_status.setdefault(status, []).append(uid)
    interested = by_status.get("interested", [])
    going = by_status.get("going", [])
    embed = discord.Embed(title="📣 Event", description="Details", color=discord.Color.blurple(), timestamp=datetime.now(timezone.utc))
    if start_time:
        try:
            dt = datetime.fromisoformat(start_time)
            embed.add_field(name="Start", value=dt.astimezone(ZoneInfo(POST_TIMEZONE)).strftime("%d.%m.%Y %H:%M %Z"), inline=False)
        except Exception:
            embed.add_field(name="Start", value=start_time, inline=False)
    def names_list(uids: list[int]) -> str:
        if not uids:
            return "Keine"
        names = [get_user_display(guild, uid) for uid in uids]
        if len(names) > 20:
            return ", ".join(names[:20]) + f", und {len(names)-20} weitere..."
        return ", ".join(names)
    embed.add_field(name="🔔 Interessiert", value=names_list(interested), inline=False)
    embed.add_field(name="✅ Nehme teil", value=names_list(going), inline=False)
    return embed

# -------------------------
# RSVP View
# -------------------------
class EventRSVPView(discord.ui.View):
    def __init__(self, discord_event_id: str, guild: discord.Guild | None):
        super().__init__(timeout=None)
        self.discord_event_id = discord_event_id
        self.guild = guild

    @discord.ui.button(label="🔔 Interessiert", style=discord.ButtonStyle.secondary, custom_id=None)
    async def btn_interested(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_rsvp(interaction, "interested")

    @discord.ui.button(label="✅ Nehme teil", style=discord.ButtonStyle.success, custom_id=None)
    async def btn_going(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_rsvp(interaction, "going")

    async def _handle_rsvp(self, interaction: discord.Interaction, status: str):
        uid = interaction.user.id
        did = self.discord_event_id
        try:
            existing = db_execute("SELECT status FROM event_rsvps WHERE discord_event_id = ? AND user_id = ?", (did, uid), fetch=True)
            if existing and existing[0][0] == status:
                db_execute("DELETE FROM event_rsvps WHERE discord_event_id = ? AND user_id = ?", (did, uid))
                await interaction.response.send_message(f"Dein RSVP ({status}) wurde entfernt.", ephemeral=True)
            else:
                db_execute("INSERT OR REPLACE INTO event_rsvps(discord_event_id, user_id, status) VALUES (?, ?, ?)", (did, uid, status))
                await interaction.response.send_message(f"Dein RSVP wurde gesetzt: {status}.", ephemeral=True)
            # update posted message if present
            tracked = db_execute("SELECT posted_channel_id, posted_message_id FROM tracked_events WHERE discord_event_id = ?", (did,), fetch=True)
            if tracked:
                ch_id, msg_id = tracked[0]
                ch = bot.get_channel(ch_id) if ch_id else None
                if ch and msg_id:
                    try:
                        msg = await ch.fetch_message(msg_id)
                        if msg:
                            embed = build_event_embed_from_db(did, self.guild)
                            try:
                                bot.add_view(EventRSVPView(did, self.guild))
                            except Exception:
                                pass
                            await msg.edit(embed=embed, view=EventRSVPView(did, self.guild))
                    except discord.NotFound:
                        db_execute("UPDATE tracked_events SET posted_channel_id = NULL, posted_message_id = NULL WHERE discord_event_id = ?", (did,))
                    except Exception:
                        log.exception("Failed to update event message after RSVP")
        except Exception:
            log.exception("Error handling RSVP interaction")
            try:
                await interaction.response.send_message("Fehler beim Verarbeiten deines RSVP.", ephemeral=True)
            except Exception:
                pass

# -------------------------
# Reminder scheduling & coroutine
# -------------------------
async def reminder_coro(channel_id: int, discord_event_id: str, hours_before: int):
    ch = bot.get_channel(channel_id)
    if not ch:
        log.info("reminder_coro: channel %s not found", channel_id)
        return
    embed = build_event_embed_from_db(discord_event_id, None)
    embed.title = f"📣 Event — startet in ~{hours_before} Stunden"
    view = EventRSVPView(discord_event_id, None)
    tracked = db_execute("SELECT posted_channel_id, posted_message_id FROM tracked_events WHERE discord_event_id = ?", (discord_event_id,), fetch=True)
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
                        db_execute("UPDATE tracked_events SET posted_channel_id = NULL, posted_message_id = NULL WHERE discord_event_id = ?", (discord_event_id,))
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
        db_execute("UPDATE tracked_events SET posted_channel_id = ?, posted_message_id = ?, updated_at = ? WHERE discord_event_id = ?",
                   (ch.id, sent.id, datetime.now(timezone.utc).isoformat(), discord_event_id))
    except Exception:
        log.exception("Failed sending reminder message for %s", discord_event_id)

def schedule_reminders_for_event(bot_inst_local, scheduler_inst_local, discord_event_id: str, start_time):
    try:
        scheduler_inst_local.remove_job(f"event_reminder_24_{discord_event_id}")
    except Exception:
        pass
    try:
        scheduler_inst_local.remove_job(f"event_reminder_2_{discord_event_id}")
    except Exception:
        pass

    if not start_time:
        return

    if start_time.tzinfo is None:
        start_time = start_time.replace(tzinfo=timezone.utc)

    t24 = start_time - timedelta(hours=24)
    t2 = start_time - timedelta(hours=2)
    now = datetime.now(timezone.utc)

    if t24 > now:
        scheduler_inst_local.add_job(lambda: bot_inst.loop.create_task(reminder_coro(EVENTS_CHANNEL_ID, discord_event_id, 24)),
                                     trigger=DateTrigger(run_date=t24), id=f"event_reminder_24_{discord_event_id}", replace_existing=True)
        log.info("Scheduled 24h reminder for %s at %s", discord_event_id, t24.isoformat())
    elif t24 <= now < start_time:
        bot_inst.loop.create_task(reminder_coro(EVENTS_CHANNEL_ID, discord_event_id, 24))
        log.info("Posted immediate 24h reminder for %s (start in <24h)", discord_event_id)

    if t2 > now:
        scheduler_inst_local.add_job(lambda: bot_inst.loop.create_task(reminder_coro(EVENTS_CHANNEL_ID, discord_event_id, 2)),
                                     trigger=DateTrigger(run_date=t2), id=f"event_reminder_2_{discord_event_id}", replace_existing=True)
        log.info("Scheduled 2h reminder for %s at %s", discord_event_id, t2.isoformat())
    elif t2 <= now < start_time:
        bot_inst.loop.create_task(reminder_coro(EVENTS_CHANNEL_ID, discord_event_id, 2))
        log.info("Posted immediate 2h reminder for %s (start in <2h)", discord_event_id)

# -------------------------
# Event handlers
# -------------------------
@bot.event
async def on_guild_scheduled_event_create(event: discord.ScheduledEvent):
    log.info(
        "EVENT_CREATE id=%s name=%s",
        getattr(event, "id", None),
        getattr(event, "name", None),
    )

    # If no target channel is configured, nothing to do
    if not EVENTS_CHANNEL_ID:
        log.info("EVENTS_CHANNEL_ID not set; ignoring scheduled event create")
        return

    discord_event_id = str(event.id)
    guild_id = event.guild.id if getattr(event, "guild", None) else None
    start_iso = event.start_time.isoformat() if event.start_time else None
    now_iso = datetime.now(timezone.utc).isoformat()

    # Ensure we persist (insert or replace) the tracked_events row first.
    try:
        db_execute(
            "INSERT OR REPLACE INTO tracked_events(guild_id, discord_event_id, start_time, updated_at) VALUES (?, ?, ?, ?)",
            (guild_id, discord_event_id, start_iso, now_iso),
        )
    except Exception:
        log.exception("Failed to insert/replace tracked_events row for %s", discord_event_id)

    # Check whether we already have a posted message recorded and if it still exists.
    try:
        tracked = db_execute(
            "SELECT posted_channel_id, posted_message_id FROM tracked_events WHERE discord_event_id = ?",
            (discord_event_id,),
            fetch=True,
        ) or []
    except Exception:
        log.exception("DB error when reading tracked_events for %s", discord_event_id)
        tracked = []

    if tracked:
        posted_ch_id, posted_msg_id = tracked[0]
        if posted_msg_id:
            ch_check = bot.get_channel(posted_ch_id) if posted_ch_id else None
            if ch_check:
                try:
                    # If the message still exists, we skip posting a duplicate.
                    await ch_check.fetch_message(posted_msg_id)
                    log.info("Event %s already posted as message %s — skipping", discord_event_id, posted_msg_id)
                    # Still ensure reminders are scheduled / rescheduled
                    try:
                        schedule_reminders_for_event(bot_inst, scheduler_inst, discord_event_id, event.start_time)
                    except Exception:
                        log.exception("Failed scheduling reminders for existing posted event %s", discord_event_id)
                    return
                except discord.NotFound:
                    # The recorded message is gone — clear DB refs and continue to post anew.
                    try:
                        db_execute(
                            "UPDATE tracked_events SET posted_channel_id = NULL, posted_message_id = NULL WHERE discord_event_id = ?",
                            (discord_event_id,),
                        )
                        log.info("Cleared stale posted refs for event %s", discord_event_id)
                    except Exception:
                        log.exception("Failed clearing stale posted refs for %s", discord_event_id)
                except Exception:
                    log.exception("Error while verifying existing posted message for %s", discord_event_id)

    # At this point either there was no tracked posted message, or it was removed; attempt to post.
    ch = bot.get_channel(EVENTS_CHANNEL_ID)
    if not ch:
        log.warning("Events channel %s not found or inaccessible; cannot post event %s", EVENTS_CHANNEL_ID, discord_event_id)
        return

    try:
        embed = discord.Embed(
            title=event.name or "Event",
            description=event.description or "",
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc),
        )
        if event.start_time:
            try:
                embed.add_field(
                    name="Start",
                    value=event.start_time.astimezone(ZoneInfo(POST_TIMEZONE)).strftime("%d.%m.%Y %H:%M %Z"),
                    inline=False,
                )
            except Exception:
                embed.add_field(name="Start", value=str(event.start_time), inline=False)

        # register persistent view instance before sending (best-effort)
        try:
            bot.add_view(EventRSVPView(discord_event_id, event.guild))
        except Exception:
            log.debug("bot.add_view(EventRSVPView) failed (non-fatal)")

        sent = await ch.send(embed=embed, view=EventRSVPView(discord_event_id, event.guild))
        try:
            db_execute(
                "UPDATE tracked_events SET posted_channel_id = ?, posted_message_id = ?, updated_at = ? WHERE discord_event_id = ?",
                (ch.id, sent.id, datetime.now(timezone.utc).isoformat(), discord_event_id),
            )
        except Exception:
            log.exception("Failed to update tracked_events after posting event %s", discord_event_id)

        # schedule reminders for this event (best-effort)
        try:
            schedule_reminders_for_event(bot_inst, scheduler_inst, discord_event_id, event.start_time)
        except Exception:
            log.exception("Failed scheduling reminders after posting event %s", discord_event_id)

    except Exception:
        log.exception("Failed to post event message for %s", discord_event_id)

@bot.event
async def on_guild_scheduled_event_update(event: discord.ScheduledEvent):
    log.info("EVENT_UPDATE id=%s name=%s", getattr(event, "id", None), getattr(event, "name", None))
    if not EVENTS_CHANNEL_ID:
        log.info("EVENTS_CHANNEL_ID not set; ignoring scheduled event update")
        return

    try:
        discord_event_id = str(event.id)
        start_iso = event.start_time.isoformat() if event.start_time else None
        now_iso = datetime.now(timezone.utc).isoformat()

        db_execute("UPDATE tracked_events SET start_time = ?, updated_at = ? WHERE discord_event_id = ?", (start_iso, now_iso, discord_event_id))

        try:
            schedule_reminders_for_event(bot_inst, scheduler_inst, discord_event_id, event.start_time)
        except Exception:
            log.exception("Failed to reschedule reminders (update)")

        tracked = db_execute("SELECT posted_channel_id, posted_message_id FROM tracked_events WHERE discord_event_id = ?", (discord_event_id,), fetch=True)
        if tracked:
            ch_id, msg_id = tracked[0]
            ch = bot.get_channel(ch_id) if ch_id else None
            if ch and msg_id:
                try:
                    msg = await ch.fetch_message(msg_id)
                    embed = build_event_embed_from_db(discord_event_id, event.guild)
                    try:
                        bot.add_view(EventRSVPView(discord_event_id, event.guild))
                    except Exception:
                        pass
                    await msg.edit(embed=embed, view=EventRSVPView(discord_event_id, event.guild))
                except discord.NotFound:
                    db_execute("UPDATE tracked_events SET posted_channel_id = NULL, posted_message_id = NULL WHERE discord_event_id = ?", (discord_event_id,))
                except Exception:
                    log.exception("Failed to update event message on event update")
    except Exception:
        log.exception("Error in on_guild_scheduled_event_update")

@bot.event
async def on_guild_scheduled_event_delete(event: discord.ScheduledEvent):
    log.info("EVENT_DELETE id=%s", getattr(event, "id", None))
    try:
        discord_event_id = str(event.id)
        tracked = db_execute("SELECT posted_channel_id, posted_message_id FROM tracked_events WHERE discord_event_id = ?", (discord_event_id,), fetch=True)
        if tracked:
            ch_id, msg_id = tracked[0]
            ch = bot.get_channel(ch_id) if ch_id else None
            if ch and msg_id:
                try:
                    msg = await ch.fetch_message(msg_id)
                    await msg.delete()
                except discord.NotFound:
                    log.info("Tracked event message already deleted for %s", discord_event_id)
                except Exception:
                    log.exception("Failed to delete tracked message on event delete")
        db_execute("DELETE FROM tracked_events WHERE discord_event_id = ?", (discord_event_id,))
        db_execute("DELETE FROM event_rsvps WHERE discord_event_id = ?", (discord_event_id,))
    except Exception:
        log.exception("Error in on_guild_scheduled_event_delete")

# -------------------------
# Polls: AddIdea modal, voting, availability, views
# -------------------------
class SuggestIdeaModal(discord.ui.Modal, title="Neue Idee hinzufügen"):
    idea = discord.ui.TextInput(label="Deine Idee", placeholder="z. B. Minecraft zocken", max_length=200)
    def __init__(self, poll_id: str):
        super().__init__()
        self.poll_id = poll_id

    async def on_submit(self, interaction: discord.Interaction):
        text = str(self.idea.value).strip()
        if not text:
            await interaction.response.send_message("Leere Idee verworfen.", ephemeral=True)
            return
        created_at = datetime.now(timezone.utc).isoformat()
        try:
            db_execute("INSERT INTO options(poll_id, option_text, created_at, author_id) VALUES (?, ?, ?, ?)",
                       (self.poll_id, text, created_at, interaction.user.id))
            # best-effort update of poll message in this channel
            try:
                if interaction.message:
                    async for msg in interaction.channel.history(limit=200):
                        if msg.author == bot.user and msg.embeds:
                            em = msg.embeds[0]
                            if em.title and "Worauf" in em.title:
                                embed = generate_poll_embed_from_db(self.poll_id, interaction.guild)
                                try:
                                    bot.add_view(PollView(self.poll_id))
                                except Exception:
                                    pass
                                await msg.edit(embed=embed, view=PollView(self.poll_id))
                                break
            except Exception:
                log.exception("Best-effort update of poll message failed")
            await interaction.response.send_message("✅ Idee hinzugefügt.", ephemeral=True)
        except Exception:
            log.exception("Failed to insert option")
            await interaction.response.send_message("Fehler beim Speichern der Idee.", ephemeral=True)

class AddIdeaButton(discord.ui.Button):
    def __init__(self, poll_id: str):
        super().__init__(label="📝 Idee hinzufügen", style=discord.ButtonStyle.secondary, custom_id=f"poll:addidea:{poll_id}")
        self.poll_id = poll_id
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(SuggestIdeaModal(self.poll_id))

class AvailabilityModal(discord.ui.Modal, title="Verfügbarkeit eintragen (z. B. Mo18, Di19)"):
    slots = discord.ui.TextInput(label="Slots", placeholder="Mo18, Di19, Fr20", style=discord.TextStyle.long, max_length=400)
    def __init__(self, poll_id: str):
        super().__init__()
        self.poll_id = poll_id

    async def on_submit(self, interaction: discord.Interaction):
        text = str(self.slots.value).strip()
        slots = parse_slots_text(text)
        if not slots:
            await interaction.response.send_message("Keine gültigen Slots erkannt. Benutze z.B. 'Mo18,Di19'.", ephemeral=True)
            return
        uid = interaction.user.id
        poll_id = self.poll_id
        try:
            db_execute("DELETE FROM availability WHERE poll_id = ? AND user_id = ?", (poll_id, uid))
            to_insert = [(poll_id, uid, day, hour) for (day, hour) in slots]
            db_execute("INSERT OR REPLACE INTO availability(poll_id, user_id, day, hour) VALUES (?, ?, ?, ?)", to_insert, many=True)
            await interaction.response.send_message(f"Verfügbarkeit gespeichert: {len(slots)} Slots.", ephemeral=True)
            # Try to update poll message in channel (best-effort)
            try:
                if interaction.message:
                    async for msg in interaction.channel.history(limit=200):
                        if msg.author == bot.user and msg.embeds:
                            em = msg.embeds[0]
                            if em.title and "Worauf" in em.title:
                                embed = generate_poll_embed_from_db(poll_id, interaction.guild)
                                try:
                                    bot.add_view(PollView(poll_id))
                                except Exception:
                                    pass
                                await msg.edit(embed=embed, view=PollView(poll_id))
                                break
            except Exception:
                log.exception("Best-effort poll message update after availability failed")
        except Exception:
            log.exception("Failed storing availability")
            await interaction.response.send_message("Fehler beim Speichern deiner Verfügbarkeit.", ephemeral=True)

class AvailabilityButton(discord.ui.Button):
    def __init__(self, poll_id: str):
        super().__init__(label="📆 Verfügbarkeit", style=discord.ButtonStyle.primary, custom_id=f"poll:availability:{poll_id}")
        self.poll_id = poll_id
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(AvailabilityModal(self.poll_id))

# Votes helpers
def add_vote_to_db(poll_id: str, option_id: int, user_id: int):
    try:
        db_execute("INSERT OR IGNORE INTO votes(poll_id, option_id, user_id) VALUES (?, ?, ?)",
                   (poll_id, option_id, user_id))
    except Exception:
        log.exception("add_vote_to_db failed for poll %s option %s user %s", poll_id, option_id, user_id)

def remove_vote_from_db(poll_id: str, option_id: int, user_id: int):
    try:
        db_execute("DELETE FROM votes WHERE poll_id = ? AND option_id = ? AND user_id = ?", (poll_id, option_id, user_id))
    except Exception:
        log.exception("remove_vote_from_db failed for poll %s option %s user %s", poll_id, option_id, user_id)

def get_votes_map_for_poll(poll_id: str) -> dict[int, list[int]]:
    rows = db_execute("SELECT option_id, user_id FROM votes WHERE poll_id = ?", (poll_id,), fetch=True) or []
    ret: dict[int, list[int]] = {}
    for opt_id, uid in rows:
        ret.setdefault(opt_id, []).append(uid)
    return ret

# compute slot participants for a poll
def compute_slot_participants_for_poll(poll_id: str) -> dict[tuple[str,int], set]:
    rows = db_execute("SELECT day, hour, user_id FROM availability WHERE poll_id = ?", (poll_id,), fetch=True) or []
    slot_map: dict[tuple[str,int], set] = {}
    for day, hour, uid in rows:
        key = (day, hour)
        slot_map.setdefault(key, set()).add(uid)
    return slot_map

# Generate poll embed including top slots summary (based on availability)
def generate_poll_embed_from_db(poll_id: str, guild: discord.Guild | None = None) -> discord.Embed:
    options = db_execute("SELECT id, option_text, created_at, author_id FROM options WHERE poll_id = ? ORDER BY id ASC", (poll_id,), fetch=True) or []
    votes_map = get_votes_map_for_poll(poll_id)
    embed = discord.Embed(
        title="📋 Worauf hast du diese Woche Lust?",
        description="Gib eigene Ideen ein, stimme ab oder trage deine Zeiten ein!",
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc)
    )
    # availability summary
    slot_map = compute_slot_participants_for_poll(poll_id)
    if slot_map:
        ranked = sorted(slot_map.items(), key=lambda kv: len(kv[1]), reverse=True)
        topn = ranked[:3]
        lines = []
        for (day,hour), users in topn:
            names = [get_user_display(guild, uid) for uid in list(users)[:6]]
            more = len(users) - len(names)
            names_str = ", ".join(names) + (f", und {more} weitere" if more>0 else "")
            lines.append(f"{day} {hour:02d}:00 — {len(users)} Personen ({names_str})")
        embed.add_field(name="📆 Top gemeinsame Slots", value="\n".join(lines), inline=False)
    # options + votes
    if not options:
        embed.add_field(name="ℹ️ Keine Ideen", value="Sei der Erste und füge eine Idee hinzu!", inline=False)
    else:
        for opt_id, opt_text, created_at, author_id in options:
            voters = votes_map.get(opt_id, [])
            count = len(voters)
            top_slot_score = 0
            if slot_map and voters:
                overlaps = []
                for slot, users in slot_map.items():
                    overlaps.append(len(set(voters) & set(users)))
                top_slot_score = max(overlaps) if overlaps else 0
            if voters:
                names = [get_user_display(guild, uid) for uid in voters]
                voters_line = ", ".join(names[:8]) + (f", und {len(names)-8} weitere..." if len(names)>8 else "")
                value = f"🗳️ {count} Stimmen\n👥 {voters_line}\n🔎 Top-Slot‑Übereinstimmung: {top_slot_score}"
            else:
                value = f"🗳️ {count} Stimmen\n👥 Keine Stimmen\n🔎 Top-Slot‑Übereinstimmung: {top_slot_score}"
            embed.add_field(name=opt_text or "(ohne Titel)", value=value, inline=False)
    return embed

# PollVoteButton and PollView include AvailabilityButton
class PollVoteButton(discord.ui.Button):
    def __init__(self, poll_id: str, option_id: int, option_text: str):
        custom = f"poll:vote:{poll_id}:{option_id}"
        label = option_text if len(option_text) <= 80 else option_text[:77] + "..."
        super().__init__(label=label, style=discord.ButtonStyle.primary, custom_id=custom)
        self.poll_id = poll_id
        self.option_id = option_id

    async def callback(self, interaction: discord.Interaction):
        uid = interaction.user.id
        existing = db_execute("SELECT 1 FROM votes WHERE poll_id = ? AND option_id = ? AND user_id = ?", (self.poll_id, self.option_id, uid), fetch=True)
        if existing:
            remove_vote_from_db(self.poll_id, self.option_id, uid)
            try:
                await interaction.response.send_message("Deine Stimme wurde entfernt.", ephemeral=True)
            except Exception:
                pass
        else:
            add_vote_to_db(self.poll_id, self.option_id, uid)
            try:
                await interaction.response.send_message("Deine Stimme wurde gespeichert.", ephemeral=True)
            except Exception:
                pass
        try:
            if interaction.message:
                embed = generate_poll_embed_from_db(self.poll_id, interaction.guild)
                view = PollView(self.poll_id)
                try:
                    bot.add_view(view)
                except Exception:
                    pass
                await interaction.message.edit(embed=embed, view=view)
        except Exception:
            log.exception("Failed to refresh poll message after vote")

class PollView(discord.ui.View):
    def __init__(self, poll_id: str):
        super().__init__(timeout=None)
        self.poll_id = poll_id
        options = db_execute("SELECT id, option_text FROM options WHERE poll_id = ? ORDER BY id ASC", (poll_id,), fetch=True) or []
        for opt_id, opt_text in options:
            try:
                self.add_item(PollVoteButton(poll_id, opt_id, opt_text))
            except Exception:
                log.exception("Failed to add PollVoteButton for poll %s option %s", poll_id, opt_id)
        try:
            self.add_item(AvailabilityButton(poll_id))
        except Exception:
            pass
        try:
            self.add_item(AddIdeaButton(poll_id))
        except Exception:
            pass

async def post_poll_to_channel(channel: discord.TextChannel):
    poll_id = datetime.now(tz=ZoneInfo(POST_TIMEZONE)).strftime("%Y%m%dT%H%M%S")
    created_at = datetime.now(timezone.utc).isoformat()
    db_execute("INSERT OR REPLACE INTO polls(id, created_at, posted_channel_id, posted_message_id) VALUES (?, ?, ?, ?)",
               (poll_id, created_at, channel.id, None))
    embed = generate_poll_embed_from_db(poll_id, channel.guild)
    view = PollView(poll_id)
    try:
        bot.add_view(view)
    except Exception:
        pass
    sent = await channel.send(embed=embed, view=view)
    # persist posted message id
    try:
        db_execute("UPDATE polls SET posted_message_id = ? WHERE id = ?", (sent.id, poll_id))
    except Exception:
        log.exception("Failed to persist poll posted_message_id")
    return poll_id, sent

@bot.command()
async def startpoll(ctx):
    poll_id, sent = await post_poll_to_channel(ctx.channel)
    await ctx.send(f"Poll gepostet: id={poll_id}", delete_after=10)

@bot.command()
async def pollresults(ctx, poll_id: str):
    embed = generate_poll_embed_from_db(poll_id, ctx.guild)
    await ctx.send(embed=embed)

@bot.command()
async def listpolls(ctx):
    rows = db_execute("SELECT id, created_at FROM polls ORDER BY created_at DESC", fetch=True) or []
    await ctx.send(f"polls: {rows}")

@bot.command()
async def listoptions(ctx, poll_id: str):
    rows = db_execute("SELECT id, option_text, created_at, author_id FROM options WHERE poll_id = ? ORDER BY id ASC", (poll_id,), fetch=True) or []
    pretty = [(r[0], r[1], r[2], get_user_display(ctx.guild, r[3]) if r[3] else None) for r in rows]
    await ctx.send(f"options for {poll_id}: {pretty}")

# -------------------------
# Matches command: summarize top slots and per-option scores
# -------------------------
@bot.command()
async def matches(ctx, poll_id: str):
    slot_map = compute_slot_participants_for_poll(poll_id)
    if not slot_map:
        await ctx.send("Keine Verfügbarkeitsdaten für diesen Poll gefunden.")
        return
    ranked = sorted(slot_map.items(), key=lambda kv: len(kv[1]), reverse=True)
    lines = []
    for (day,hour), users in ranked[:10]:
        names = [get_user_display(ctx.guild, uid) for uid in list(users)[:8]]
        more = len(users) - len(names)
        names_str = ", ".join(names) + (f", und {more} weitere" if more>0 else "")
        lines.append(f"{day} {hour:02d}:00 — {len(users)}: {names_str}")
    votes_map = get_votes_map_for_poll(poll_id)
    top_slot = ranked[0][0] if ranked else None
    per_option_lines = []
    if top_slot:
        top_users = ranked[0][1]
        for opt_id, voters in votes_map.items():
            overlap = len(set(voters) & set(top_users))
            per_option_lines.append(f"Option {opt_id}: {len(voters)} Stimmen, {overlap} Stimmen in Top-Slot")
    await ctx.send(f"Top Slots:\n" + "\n".join(lines[:5]) + ("\n\nPer-Option:\n" + "\n".join(per_option_lines) if per_option_lines else ""))

# -------------------------
# Daily Summary and Weekly Poll scheduling
# -------------------------
def schedule_weekly_post():
    try:
        trigger = CronTrigger(day_of_week="sun", hour=12, minute=0, timezone=ZoneInfo(POST_TIMEZONE))
        scheduler.add_job(job_post_weekly, trigger=trigger, id="weekly_poll", replace_existing=True)
        log.info("Scheduled weekly poll job (sun 12:00 %s)", POST_TIMEZONE)
    except Exception:
        log.exception("Failed to schedule weekly post")

def schedule_daily_summary():
    try:
        trigger_morning = CronTrigger(day_of_week="*", hour=9, minute=0, timezone=ZoneInfo(POST_TIMEZONE))
        scheduler.add_job(lambda: asyncio.create_task(post_daily_summary_to_all()), trigger=trigger_morning, id="daily_summary_morning", replace_existing=True)
        trigger_evening = CronTrigger(day_of_week="*", hour=18, minute=0, timezone=ZoneInfo(POST_TIMEZONE))
        scheduler.add_job(lambda: asyncio.create_task(post_daily_summary_to_all()), trigger=trigger_evening, id="daily_summary_evening", replace_existing=True)
        log.info("Scheduled daily summary jobs (09:00 and 18:00 %s)", POST_TIMEZONE)
    except Exception:
        log.exception("Failed to schedule daily summary jobs")

async def job_post_weekly():
    await bot.wait_until_ready()
    # Post weekly poll to CHANNEL_ID if set, otherwise skip
    channel_id = None
    # prefer polls.posted_channel_id if set (not applicable here) — else fallback to EVENTS_CHANNEL_ID
    if EVENTS_CHANNEL_ID:
        channel_id = EVENTS_CHANNEL_ID
    if not channel_id:
        log.info("No channel configured for weekly poll; skipping job_post_weekly")
        return
    ch = bot.get_channel(channel_id)
    if not ch:
        log.warning("Weekly poll channel %s not found", channel_id)
        return
    try:
        await post_poll_to_channel(ch)
        log.info("Posted weekly poll to channel %s", channel_id)
    except Exception:
        log.exception("Failed to post weekly poll")

async def post_daily_summary_to_channel(channel: discord.TextChannel):
    """
    Post a concise summary to a specific channel:
    - Recently created polls (last 24h)
    - Top availability slots across all polls (aggregated)
    - Optionally other metrics
    """
    # gather polls created in last 24h
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    rows = db_execute("SELECT id, created_at FROM polls WHERE created_at >= ? ORDER BY created_at ASC", (since.isoformat(),), fetch=True) or []
    poll_lines = []
    for pid, created_at in rows:
        poll_lines.append(f"- Poll {pid} (created {created_at})")
    # aggregate availability across polls (simple top slots overall)
    all_slots = db_execute("SELECT day, hour, COUNT(DISTINCT user_id) as cnt FROM availability GROUP BY day, hour ORDER BY cnt DESC LIMIT 5", fetch=True) or []
    slot_lines = []
    for day, hour, cnt in all_slots:
        slot_lines.append(f"- {day} {int(hour):02d}:00 — {cnt} Personen")
    parts = []
    if poll_lines:
        parts.append("Neue Polls (letzte 24h):\n" + "\n".join(poll_lines))
    if slot_lines:
        parts.append("Top gemeinsame Slots (gesamt):\n" + "\n".join(slot_lines))
    if not parts:
        # nothing to post
        return
    summary = "\n\n".join(parts)
    embed = discord.Embed(title="📣 Tägliche Zusammenfassung", description=summary, color=discord.Color.green(), timestamp=datetime.now(timezone.utc))
    # Try to replace previous summary message in this channel (if any)
    try:
        existing = db_execute("SELECT message_id FROM daily_summaries WHERE channel_id = ?", (channel.id,), fetch=True) or []
        if existing and existing[0][0]:
            try:
                msg = await channel.fetch_message(existing[0][0])
                if msg:
                    sent = await msg.edit(embed=embed)
                    db_execute("UPDATE daily_summaries SET last_run = ?, message_id = ? WHERE channel_id = ?", (datetime.now(timezone.utc).isoformat(), sent.id, channel.id))
                    return
            except discord.NotFound:
                # fallthrough to post new
                pass
            except Exception:
                log.exception("Failed updating existing daily summary message")
        sent = await channel.send(embed=embed)
        try:
            db_execute("INSERT OR REPLACE INTO daily_summaries(channel_id, message_id, last_run) VALUES (?, ?, ?)", (channel.id, sent.id, datetime.now(timezone.utc).isoformat()))
        except Exception:
            log.exception("Failed to persist daily summary message id")
    except Exception:
        log.exception("Failed to post daily summary to channel %s", channel.id)

async def post_daily_summary_to_all():
    await bot.wait_until_ready()
    # Determine channels to post summaries to: for now, use EVENTS_CHANNEL_ID if set
    if not EVENTS_CHANNEL_ID:
        log.info("EVENTS_CHANNEL_ID not set; skipping daily summaries")
        return
    ch = bot.get_channel(EVENTS_CHANNEL_ID)
    if not ch:
        log.warning("Daily summary channel %s not found", EVENTS_CHANNEL_ID)
        return
    await post_daily_summary_to_channel(ch)

# -------------------------
# Remaining Poll/Event code (unchanged from previous step)
# -------------------------
def add_vote_to_db(poll_id: str, option_id: int, user_id: int):
    try:
        db_execute("INSERT OR IGNORE INTO votes(poll_id, option_id, user_id) VALUES (?, ?, ?)",
                   (poll_id, option_id, user_id))
    except Exception:
        log.exception("add_vote_to_db failed for poll %s option %s user %s", poll_id, option_id, user_id)

def remove_vote_from_db(poll_id: str, option_id: int, user_id: int):
    try:
        db_execute("DELETE FROM votes WHERE poll_id = ? AND option_id = ? AND user_id = ?", (poll_id, option_id, user_id))
    except Exception:
        log.exception("remove_vote_from_db failed for poll %s option %s user %s", poll_id, option_id, user_id)

def get_votes_map_for_poll(poll_id: str) -> dict[int, list[int]]:
    rows = db_execute("SELECT option_id, user_id FROM votes WHERE poll_id = ?", (poll_id,), fetch=True) or []
    ret: dict[int, list[int]] = {}
    for opt_id, uid in rows:
        ret.setdefault(opt_id, []).append(uid)
    return ret

def compute_slot_participants_for_poll(poll_id: str) -> dict[tuple[str,int], set]:
    rows = db_execute("SELECT day, hour, user_id FROM availability WHERE poll_id = ?", (poll_id,), fetch=True) or []
    slot_map: dict[tuple[str,int], set] = {}
    for day, hour, uid in rows:
        key = (day, hour)
        slot_map.setdefault(key, set()).add(uid)
    return slot_map

def generate_poll_embed_from_db(poll_id: str, guild: discord.Guild | None = None) -> discord.Embed:
    options = db_execute("SELECT id, option_text, created_at, author_id FROM options WHERE poll_id = ? ORDER BY id ASC", (poll_id,), fetch=True) or []
    votes_map = get_votes_map_for_poll(poll_id)
    embed = discord.Embed(
        title="📋 Worauf hast du diese Woche Lust?",
        description="Gib eigene Ideen ein, stimme ab oder trage deine Zeiten ein!",
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc)
    )
    slot_map = compute_slot_participants_for_poll(poll_id)
    if slot_map:
        ranked = sorted(slot_map.items(), key=lambda kv: len(kv[1]), reverse=True)
        topn = ranked[:3]
        lines = []
        for (day,hour), users in topn:
            names = [get_user_display(guild, uid) for uid in list(users)[:6]]
            more = len(users) - len(names)
            names_str = ", ".join(names) + (f", und {more} weitere" if more>0 else "")
            lines.append(f"{day} {hour:02d}:00 — {len(users)} Personen ({names_str})")
        embed.add_field(name="📆 Top gemeinsame Slots", value="\n".join(lines), inline=False)
    if not options:
        embed.add_field(name="ℹ️ Keine Ideen", value="Sei der Erste und füge eine Idee hinzu!", inline=False)
    else:
        for opt_id, opt_text, created_at, author_id in options:
            voters = votes_map.get(opt_id, [])
            count = len(voters)
            top_slot_score = 0
            if slot_map and voters:
                overlaps = []
                for slot, users in slot_map.items():
                    overlaps.append(len(set(voters) & set(users)))
                top_slot_score = max(overlaps) if overlaps else 0
            if voters:
                names = [get_user_display(guild, uid) for uid in voters]
                voters_line = ", ".join(names[:8]) + (f", und {len(names)-8} weitere..." if len(names)>8 else "")
                value = f"🗳️ {count} Stimmen\n👥 {voters_line}\n🔎 Top-Slot‑Übereinstimmung: {top_slot_score}"
            else:
                value = f"🗳️ {count} Stimmen\n👥 Keine Stimmen\n🔎 Top-Slot‑Übereinstimmung: {top_slot_score}"
            embed.add_field(name=opt_text or "(ohne Titel)", value=value, inline=False)
    return embed

class PollVoteButton(discord.ui.Button):
    def __init__(self, poll_id: str, option_id: int, option_text: str):
        custom = f"poll:vote:{poll_id}:{option_id}"
        label = option_text if len(option_text) <= 80 else option_text[:77] + "..."
        super().__init__(label=label, style=discord.ButtonStyle.primary, custom_id=custom)
        self.poll_id = poll_id
        self.option_id = option_id

    async def callback(self, interaction: discord.Interaction):
        uid = interaction.user.id
        existing = db_execute("SELECT 1 FROM votes WHERE poll_id = ? AND option_id = ? AND user_id = ?", (self.poll_id, self.option_id, uid), fetch=True)
        if existing:
            remove_vote_from_db(self.poll_id, self.option_id, uid)
            try:
                await interaction.response.send_message("Deine Stimme wurde entfernt.", ephemeral=True)
            except Exception:
                pass
        else:
            add_vote_to_db(self.poll_id, self.option_id, uid)
            try:
                await interaction.response.send_message("Deine Stimme wurde gespeichert.", ephemeral=True)
            except Exception:
                pass
        try:
            if interaction.message:
                embed = generate_poll_embed_from_db(self.poll_id, interaction.guild)
                view = PollView(self.poll_id)
                try:
                    bot.add_view(view)
                except Exception:
                    pass
                await interaction.message.edit(embed=embed, view=view)
        except Exception:
            log.exception("Failed to refresh poll message after vote")

class PollView(discord.ui.View):
    def __init__(self, poll_id: str):
        super().__init__(timeout=None)
        self.poll_id = poll_id
        options = db_execute("SELECT id, option_text FROM options WHERE poll_id = ? ORDER BY id ASC", (poll_id,), fetch=True) or []
        for opt_id, opt_text in options:
            try:
                self.add_item(PollVoteButton(poll_id, opt_id, opt_text))
            except Exception:
                log.exception("Failed to add PollVoteButton for poll %s option %s", poll_id, opt_id)
        try:
            self.add_item(AvailabilityButton(poll_id))
        except Exception:
            pass
        try:
            self.add_item(AddIdeaButton(poll_id))
        except Exception:
            pass

# SuggestIdeaModal and AvailabilityModal already defined above

# -------------------------
# Debug commands
# -------------------------
@bot.command()
async def checkevents(ctx):
    rows = db_execute("SELECT discord_event_id, start_time, posted_channel_id, posted_message_id FROM tracked_events", fetch=True)
    await ctx.send(f"tracked_events: {rows}")

@bot.command()
async def rsvpstatus(ctx, discord_event_id: str):
    rows = db_execute("SELECT user_id, status FROM event_rsvps WHERE discord_event_id = ?", (discord_event_id,), fetch=True) or []
    guild = ctx.guild
    pretty = [(get_user_display(guild, r[0]), r[1]) for r in rows]
    await ctx.send(f"rsvps for {discord_event_id}: {pretty}")

@bot.command()
async def ping(ctx):
    await ctx.send("pong")

# -------------------------
# Startup
# -------------------------
@bot.event
async def on_ready():
    log.info("Bot ready: %s (id=%s)", bot.user, bot.user.id)
    init_db()
    if not scheduler.running:
        scheduler.start()
    # schedule cron jobs (weekly poll & daily summary)
    schedule_weekly_post()
    schedule_daily_summary()
    # re-register RSVP views for tracked events
    try:
        rows = db_execute("SELECT discord_event_id FROM tracked_events", fetch=True) or []
        for (did,) in rows:
            try:
                bot.add_view(EventRSVPView(did, None))
            except Exception:
                pass
    except Exception:
        log.exception("Failed to re-register RSVP views on startup")
    # re-register PollView instances
    try:
        rows = db_execute("SELECT id FROM polls", fetch=True) or []
        for (pid,) in rows:
            try:
                bot.add_view(PollView(pid))
            except Exception:
                pass
    except Exception:
        log.exception("Failed to register PollView instances on startup")
    # reschedule reminders
    if EVENTS_CHANNEL_ID:
        try:
            rows = db_execute("SELECT discord_event_id, start_time FROM tracked_events", fetch=True) or []
            for discord_event_id, start_iso in rows:
                try:
                    start_dt = datetime.fromisoformat(start_iso) if start_iso else None
                except Exception:
                    continue
                schedule_reminders_for_event(bot_inst, scheduler_inst, discord_event_id, start_dt)
        except Exception:
            log.exception("Failed to reschedule events on startup")

# -------------------------
# Entrypoint
# -------------------------
if __name__ == "__main__":
    if not BOT_TOKEN:
        print("Bitte BOT_TOKEN als Umgebungsvariable setzen.")
        raise SystemExit(1)
    init_db()
    bot.run(BOT_TOKEN)
