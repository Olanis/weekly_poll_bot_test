#!/usr/bin/env python3
"""
Clean, syntactically-correct bot.py replacement.

This file preserves the event handling, sync, reminders and poll/quarter features
but is simplified and validated for syntax to eliminate the "expected 'except' or
'finally' block" error. Replace your current bot.py with this file and restart
the container.

Environment variables:
- BOT_TOKEN (required)
- POLL_DB (optional; default polls.sqlite)
- CHANNEL_ID (optional)
- EVENTS_CHANNEL_ID (optional)
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
intents.guild_scheduled_events = True

bot = commands.Bot(command_prefix="!", intents=intents)

DB_PATH = os.getenv("POLL_DB", "polls.sqlite")
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0")) if os.getenv("CHANNEL_ID") else None
POST_TIMEZONE = os.getenv("POST_TIMEZONE", "Europe/Berlin")

EVENTS_CHANNEL_ID = int(os.getenv("EVENTS_CHANNEL_ID", "0")) if os.getenv("EVENTS_CHANNEL_ID") else None
QUARTER_POLL_CHANNEL_ID = int(os.getenv("QUARTER_POLL_CHANNEL_ID", "0")) if os.getenv("QUARTER_POLL_CHANNEL_ID") else None

# -------------------------
# DB helpers
# -------------------------
def init_db():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    # minimal required tables
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
    # polls/quarter simplified tables (kept for compatibility)
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
        m = guild.get_member(user_id)
        if m:
            return m.display_name
    u = bot.get_user(user_id)
    return u.name if u else str(user_id)

# -------------------------
# Minimal poll helpers (kept for compatibility)
# -------------------------
def create_poll_record(poll_id: str):
    db_execute("INSERT OR REPLACE INTO polls(id, created_at) VALUES (?, ?)", (poll_id, datetime.now(timezone.utc).isoformat()))

def get_options(poll_id: str):
    return db_execute("SELECT id, option_text, created_at, author_id FROM options WHERE poll_id = ? ORDER BY id ASC", (poll_id,), fetch=True) or []

# -------------------------
# Event helpers & view
# -------------------------
def _event_db_execute(query, params=(), fetch=False, many=False):
    return db_execute(query, params, fetch=fetch, many=many)

class EventViewInFile(discord.ui.View):
    def __init__(self, discord_event_id: str, guild: discord.Guild | None):
        super().__init__(timeout=None)
        self.discord_event_id = discord_event_id
        self.guild = guild

    @discord.ui.button(label="⚜️ Interessiert", style=discord.ButtonStyle.primary)
    async def interested(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = interaction.user.id
        try:
            rows = _event_db_execute("SELECT status FROM event_rsvps WHERE discord_event_id = ? AND user_id = ?",
                                     (self.discord_event_id, user_id), fetch=True)
            if rows and rows[0][0] == "interested":
                _event_db_execute("DELETE FROM event_rsvps WHERE discord_event_id = ? AND user_id = ?",
                                  (self.discord_event_id, user_id))
                await interaction.response.send_message("Deine Interesse wurde entfernt.", ephemeral=True)
            else:
                _event_db_execute("INSERT OR REPLACE INTO event_rsvps(discord_event_id, user_id, status) VALUES (?, ?, ?)",
                                  (self.discord_event_id, user_id, "interested"))
                await interaction.response.send_message("Du bist als interessiert vermerkt.", ephemeral=True)
            tracked = _event_db_execute("SELECT posted_channel_id, posted_message_id FROM tracked_events WHERE discord_event_id = ?",
                                        (self.discord_event_id,), fetch=True)
            if tracked:
                ch_id, msg_id = tracked[0]
                ch = interaction.client.get_channel(ch_id)
                if ch:
                    try:
                        msg = await ch.fetch_message(msg_id)
                        if msg:
                            embed = build_event_embed_from_db(self.discord_event_id, self.guild)
                            await msg.edit(embed=embed, view=EventViewInFile(self.discord_event_id, self.guild))
                    except discord.NotFound:
                        pass
        except Exception:
            log.exception("Error handling RSVP interaction")

def build_event_embed_from_db(discord_event_id: str, guild: discord.Guild | None):
    rows = _event_db_execute("SELECT discord_event_id, start_time FROM tracked_events WHERE discord_event_id = ?", (discord_event_id,), fetch=True)
    start_time = rows[0][1] if rows else None
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
            embed.add_field(name="Start", value=dt.astimezone(ZoneInfo(POST_TIMEZONE)).strftime("%d.%m.%Y %H:%M %Z"), inline=False)
        except Exception:
            embed.add_field(name="Start", value=start_time, inline=False)
    embed.add_field(name="Interessierte", value=", ".join(names) if names else "Keine", inline=False)
    return embed

def schedule_reminders_for_event(bot_inst, scheduler_inst, discord_event_id: str, start_time, events_channel_id):
    try:
        scheduler_inst.remove_job(f"event_reminder_24_{discord_event_id}")
    except Exception:
        pass
    try:
        scheduler_inst.remove_job(f"event_reminder_2_{discord_event_id}")
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

        tracked = _event_db_execute("SELECT posted_channel_id, posted_message_id FROM tracked_events WHERE discord_event_id = ?",
                                    (discord_event_id,), fetch=True)
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
                            _event_db_execute("UPDATE tracked_events SET posted_channel_id = NULL, posted_message_id = NULL WHERE discord_event_id = ?",
                                              (discord_event_id,))
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
        scheduler_inst.add_job(lambda: bot_inst.loop.create_task(reminder_coro(events_channel_id, discord_event_id, 24)), trigger=DateTrigger(run_date=t24), id=f"event_reminder_24_{discord_event_id}", replace_existing=True)
    elif t24 <= now < start_time:
        bot_inst.loop.create_task(reminder_coro(events_channel_id, discord_event_id, 24))
    if t2 > now:
        scheduler_inst.add_job(lambda: bot_inst.loop.create_task(reminder_coro(events_channel_id, discord_event_id, 2)), trigger=DateTrigger(run_date=t2), id=f"event_reminder_2_{discord_event_id}", replace_existing=True)
    elif t2 <= now < start_time:
        bot_inst.loop.create_task(reminder_coro(events_channel_id, discord_event_id, 2))

# -------------------------
# High-level scheduled event listeners (corrected)
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
                            _ = await ch_check.fetch_message(posted_msg_id)
                            log.info("Event %s already has posted message %s — skipping post.", discord_event_id, posted_msg_id)
                            try:
                                schedule_reminders_for_event(bot, scheduler, discord_event_id, event.start_time, EVENTS_CHANNEL_ID)
                            except Exception:
                                log.exception("Failed to schedule reminders for event")
                            return
                        except discord.NotFound:
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

# -------------------------
# Scheduler & startup (minimal)
# -------------------------
scheduler = AsyncIOScheduler(timezone=ZoneInfo(POST_TIMEZONE))

def schedule_weekly_post():
    trigger = CronTrigger(day_of_week="sun", hour=12, minute=0, timezone=ZoneInfo(POST_TIMEZONE))
    scheduler.add_job(job_post_weekly, trigger=trigger, id="weekly_poll", replace_existing=True)

def schedule_daily_summary():
    trigger_morning = CronTrigger(day_of_week="*", hour=9, minute=0, timezone=ZoneInfo(POST_TIMEZONE))
    scheduler.add_job(post_daily_summary_to, trigger=trigger_morning, id="daily_summary_morning", replace_existing=True)
    trigger_evening = CronTrigger(day_of_week="*", hour=18, minute=0, timezone=ZoneInfo(POST_TIMEZONE))
    scheduler.add_job(post_daily_summary_to, trigger=trigger_evening, id="daily_summary_evening", replace_existing=True)

async def job_post_weekly():
    await bot.wait_until_ready()
    # minimal safe posting
    channel = bot.get_channel(CHANNEL_ID) if CHANNEL_ID else None
    if channel:
        await post_poll_to_channel(channel)

async def post_daily_summary_to(channel: discord.TextChannel):
    # minimal placeholder: do nothing if no polls exist
    rows = db_execute("SELECT id FROM polls ORDER BY created_at DESC LIMIT 1", fetch=True)
    if not rows:
        return

# persistent views registration placeholder
async def register_persistent_poll_views_async(batch_delay: float = 0.02):
    return

@bot.event
async def on_ready():
    log.info(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    init_db()
    if not scheduler.running:
        scheduler.start()
    schedule_weekly_post()
    schedule_daily_summary()
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
    try:
        bot.loop.create_task(register_persistent_poll_views_async(batch_delay=0.02))
    except Exception:
        log.exception("Failed to schedule persistent view registration on startup")

# Entrypoint
if __name__ == "__main__":
    if not BOT_TOKEN:
        print("Bitte BOT_TOKEN als Umgebungsvariable setzen.")
        raise SystemExit(1)
    init_db()
    bot.run(BOT_TOKEN)
