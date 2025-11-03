#!/usr/bin/env python3
"""
Stepwise bot — add reminder scheduling (24h and 2h) with robust NotFound handling.

Replace the running bot.py with this file and restart the container.
"""
from __future__ import annotations

import os
import io
import sqlite3
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

# logging
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

# intents & bot
intents = discord.Intents.default()
intents.message_content = True
intents.guild_scheduled_events = True

bot = commands.Bot(command_prefix="!", intents=intents)

# config
DB_PATH = os.getenv("POLL_DB", "polls.sqlite")
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0")) if os.getenv("CHANNEL_ID") else None
EVENTS_CHANNEL_ID = int(os.getenv("EVENTS_CHANNEL_ID", "0")) if os.getenv("EVENTS_CHANNEL_ID") else None
POST_TIMEZONE = os.getenv("POST_TIMEZONE", "Europe/Berlin")

# DB helpers
def init_db():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
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

# scheduler
scheduler = AsyncIOScheduler(timezone=ZoneInfo(POST_TIMEZONE))

# placeholder view for reminders (we will later replace with RSVP view)
class EventViewPlaceholder(discord.ui.View):
    def __init__(self, discord_event_id: str):
        super().__init__(timeout=None)
        self.discord_event_id = discord_event_id

# Helper: build a simple embed for the event from DB
def build_event_embed_from_db(discord_event_id: str):
    rows = db_execute("SELECT discord_event_id, start_time FROM tracked_events WHERE discord_event_id = ?", (discord_event_id,), fetch=True) or []
    start_time = rows[0][1] if rows else None
    embed = discord.Embed(title="📣 Event", description="Details", color=discord.Color.blurple(), timestamp=datetime.now(timezone.utc))
    if start_time:
        try:
            dt = datetime.fromisoformat(start_time)
            embed.add_field(name="Start", value=dt.astimezone(ZoneInfo(POST_TIMEZONE)).strftime("%d.%m.%Y %H:%M %Z"), inline=False)
        except Exception:
            embed.add_field(name="Start", value=start_time, inline=False)
    # RSVP count (basic)
    r = db_execute("SELECT user_id FROM event_rsvps WHERE discord_event_id = ?", (discord_event_id,), fetch=True) or []
    names = [str(x[0]) for x in r]
    embed.add_field(name="Interessierte", value=", ".join(names) if names else "Keine", inline=False)
    return embed

# Reminder coroutine: robustly delete old tracked message if needed, post new reminder, update DB
async def reminder_coro(channel_id: int, discord_event_id: str, hours_before: int):
    ch = bot.get_channel(channel_id)
    if not ch:
        log.info("reminder_coro: channel %s not found", channel_id)
        return
    embed = build_event_embed_from_db(discord_event_id)
    embed.title = f"📣 Event — startet in ~{hours_before} Stunden"
    view = EventViewPlaceholder(discord_event_id)

    # Try to delete previously posted tracked message (if any). Handle NotFound quietly and clear DB refs.
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

    # Send the reminder/post and persist new posted message id
    try:
        sent = await ch.send(embed=embed, view=view)
        db_execute("UPDATE tracked_events SET posted_channel_id = ?, posted_message_id = ?, updated_at = ? WHERE discord_event_id = ?",
                   (ch.id, sent.id, datetime.now(timezone.utc).isoformat(), discord_event_id))
    except Exception:
        log.exception("Failed sending reminder message for %s", discord_event_id)

# Schedule or immediately run reminders for a given event start_time
def schedule_reminders_for_event(bot_inst, scheduler_inst, discord_event_id: str, start_time):
    # remove existing jobs (best-effort)
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

    # ensure tz-aware
    if start_time.tzinfo is None:
        start_time = start_time.replace(tzinfo=timezone.utc)

    t24 = start_time - timedelta(hours=24)
    t2 = start_time - timedelta(hours=2)
    now = datetime.now(timezone.utc)

    # schedule or fire for 24h
    if t24 > now:
        scheduler_inst.add_job(lambda: bot_inst.loop.create_task(reminder_coro(EVENTS_CHANNEL_ID, discord_event_id, 24)),
                               trigger=DateTrigger(run_date=t24), id=f"event_reminder_24_{discord_event_id}", replace_existing=True)
        log.info("Scheduled 24h reminder for %s at %s", discord_event_id, t24.isoformat())
    elif t24 <= now < start_time:
        bot_inst.loop.create_task(reminder_coro(EVENTS_CHANNEL_ID, discord_event_id, 24))
        log.info("Posted immediate 24h reminder for %s (start in <24h)", discord_event_id)

    # schedule or fire for 2h
    if t2 > now:
        scheduler_inst.add_job(lambda: bot_inst.loop.create_task(reminder_coro(EVENTS_CHANNEL_ID, discord_event_id, 2)),
                               trigger=DateTrigger(run_date=t2), id=f"event_reminder_2_{discord_event_id}", replace_existing=True)
        log.info("Scheduled 2h reminder for %s at %s", discord_event_id, t2.isoformat())
    elif t2 <= now < start_time:
        bot_inst.loop.create_task(reminder_coro(EVENTS_CHANNEL_ID, discord_event_id, 2))
        log.info("Posted immediate 2h reminder for %s (start in <2h)", discord_event_id)

# Expose bot_inst and scheduler_inst to lambdas used above
bot_inst = bot
scheduler_inst = scheduler

# -------------------------
# Event handlers (create/update/delete) - use schedule_reminders_for_event
# -------------------------
@bot.event
async def on_guild_scheduled_event_create(event: discord.ScheduledEvent):
    log.info("EVENT_CREATE id=%s name=%s", getattr(event, "id", None), getattr(event, "name", None))
    if not EVENTS_CHANNEL_ID:
        log.info("EVENTS_CHANNEL_ID not set; ignoring scheduled event create")
        return
    try:
        discord_event_id = str(event.id)
        start_iso = event.start_time.isoformat() if event.start_time else None
        guild_id = event.guild.id if getattr(event, "guild", None) else None
        now_iso = datetime.now(timezone.utc).isoformat()

        # persist tracked_events row
        db_execute("INSERT OR REPLACE INTO tracked_events(guild_id, discord_event_id, start_time, updated_at) VALUES (?, ?, ?, ?)",
                   (guild_id, discord_event_id, start_iso, now_iso))

        # check if a posted message is already recorded and exists
        tracked = db_execute("SELECT posted_channel_id, posted_message_id FROM tracked_events WHERE discord_event_id = ?", (discord_event_id,), fetch=True)
        if tracked:
            posted_ch_id, posted_msg_id = tracked[0]
            if posted_msg_id:
                try:
                    ch_check = bot.get_channel(posted_ch_id) if posted_ch_id else None
                    if ch_check:
                        try:
                            _ = await ch_check.fetch_message(posted_msg_id)
                            log.info("Event %s already posted as message %s — skipping", discord_event_id, posted_msg_id)
                            # schedule reminders based on event.start_time
                            try:
                                schedule_reminders_for_event(bot_inst, scheduler_inst, discord_event_id, event.start_time)
                            except Exception:
                                log.exception("Failed scheduling reminders (create)")
                            return
                        except discord.NotFound:
                            db_execute("UPDATE tracked_events SET posted_channel_id = NULL, posted_message_id = NULL WHERE discord_event_id = ?", (discord_event_id,))
                        except Exception:
                            log.exception("Error verifying existing posted message")
        # Post message
        ch = bot.get_channel(EVENTS_CHANNEL_ID)
        if not ch:
            log.warning("Events channel %s not found", EVENTS_CHANNEL_ID)
            return
        embed = discord.Embed(title=event.name or "Event", description=event.description or "", color=discord.Color.blue(), timestamp=datetime.now(timezone.utc))
        if event.start_time:
            try:
                embed.add_field(name="Start", value=event.start_time.astimezone(ZoneInfo(POST_TIMEZONE)).strftime("%d.%m.%Y %H:%M %Z"), inline=False)
            except Exception:
                embed.add_field(name="Start", value=str(event.start_time), inline=False)
        view = EventViewPlaceholder(discord_event_id)
        sent = await ch.send(embed=embed, view=view)
        db_execute("UPDATE tracked_events SET posted_channel_id = ?, posted_message_id = ?, updated_at = ? WHERE discord_event_id = ?",
                   (ch.id, sent.id, now_iso, discord_event_id))
        # schedule reminders
        try:
            start_dt = event.start_time
            schedule_reminders_for_event(bot_inst, scheduler_inst, discord_event_id, start_dt)
        except Exception:
            log.exception("Failed scheduling reminders after post")
    except Exception:
        log.exception("Error in on_guild_scheduled_event_create")

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
        # reschedule reminders
        try:
            start_dt = event.start_time
            schedule_reminders_for_event(bot_inst, scheduler_inst, discord_event_id, start_dt)
        except Exception:
            log.exception("Failed to reschedule reminders (update)")
        # update posted message embed if exists
        tracked = db_execute("SELECT posted_channel_id, posted_message_id FROM tracked_events WHERE discord_event_id = ?", (discord_event_id,), fetch=True)
        if tracked:
            ch_id, msg_id = tracked[0]
            ch = bot.get_channel(ch_id) if ch_id else None
            if ch and msg_id:
                try:
                    msg = await ch.fetch_message(msg_id)
                    embed = discord.Embed(title=event.name or "Event", description=event.description or "", color=discord.Color.blue(), timestamp=datetime.now(timezone.utc))
                    if event.start_time:
                        try:
                            embed.add_field(name="Start", value=event.start_time.astimezone(ZoneInfo(POST_TIMEZONE)).strftime("%d.%m.%Y %H:%M %Z"), inline=False)
                        except Exception:
                            embed.add_field(name="Start", value=str(event.start_time), inline=False)
                    await msg.edit(embed=embed, view=EventViewPlaceholder(discord_event_id))
                except discord.NotFound:
                    log.info("Tracked event message missing during update for %s; clearing posted refs", discord_event_id)
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

# Debug commands
@bot.command()
async def checkevents(ctx):
    rows = db_execute("SELECT discord_event_id, start_time, posted_channel_id, posted_message_id FROM tracked_events", fetch=True)
    await ctx.send(f"tracked_events: {rows}")

@bot.command()
async def ping(ctx):
    await ctx.send("pong")

# startup
@bot.event
async def on_ready():
    log.info("Bot ready: %s (id=%s)", bot.user, bot.user.id)
    init_db()
    if not scheduler.running:
        scheduler.start()
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

if __name__ == "__main__":
    if not BOT_TOKEN:
        print("Bitte BOT_TOKEN als Umgebungsvariable setzen.")
        raise SystemExit(1)
    init_db()
    bot.run(BOT_TOKEN)
