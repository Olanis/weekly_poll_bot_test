#!/usr/bin/env python3
"""
Stepwise bot — Part A: Poll DB tables + simple post_poll_to_channel + !startpoll.

This file continues from the last working state (events, reminders, RSVP UI).
It adds:
- DB tables for polls/options/votes,
- A simple post_poll_to_channel function that creates a poll record, posts an embed
  with a PollView that contains only an "Add Idea" button for now,
- A modal to submit a new idea (stores into options table),
- A !startpoll command to post a new poll to the current channel,
- Persistent registration of PollView instances on startup (best-effort).

Replace your current bot.py with this file, restart the bot, then run:
- !startpoll in a channel to post a poll,
- click "📝 Idee hinzufügen" to add an idea (it will persist in DB).
Later steps will add voting buttons and availability UI.

I kept all previously added event/RSVP/reminder logic intact and only added the poll-related pieces.
"""
from __future__ import annotations

import os
import sqlite3
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
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# config
DB_PATH = os.getenv("POLL_DB", "polls.sqlite")
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0")) if os.getenv("CHANNEL_ID") else None
EVENTS_CHANNEL_ID = int(os.getenv("EVENTS_CHANNEL_ID", "0")) if os.getenv("EVENTS_CHANNEL_ID") else None
POST_TIMEZONE = os.getenv("POST_TIMEZONE", "Europe/Berlin")

# -------------------------
# DB helpers & init (extended with polls)
# -------------------------
def init_db():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    # tracked events & rsvps
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

    # polls: basic tables
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
        rows = cur.fetchall() if fetch else None
        con.commit()
        return rows
    finally:
        con.close()

# scheduler
scheduler = AsyncIOScheduler(timezone=ZoneInfo(POST_TIMEZONE))

# -------------------------
# Utilities
# -------------------------
def get_user_display(guild: discord.Guild | None, user_id: int) -> str:
    if guild:
        m = guild.get_member(user_id)
        if m:
            return m.display_name
    u = bot.get_user(user_id)
    return getattr(u, "name", str(user_id))

# -------------------------
# Poll: Modal + View (Part A: adding ideas)
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
            # best-effort: try to update the original poll message in this channel if exists
            try:
                if interaction.message:
                    # find the poll id's message by searching recent bot messages in this channel
                    async for msg in interaction.channel.history(limit=200):
                        if msg.author == bot.user and msg.embeds:
                            em = msg.embeds[0]
                            if em.title and "Worauf" in em.title:
                                # regenerate embed from DB and edit
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
        # persistent custom_id so view can be re-registered
        super().__init__(label="📝 Idee hinzufügen", style=discord.ButtonStyle.secondary, custom_id=f"poll:addidea:{poll_id}")
        self.poll_id = poll_id
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(SuggestIdeaModal(self.poll_id))

class PollView(discord.ui.View):
    def __init__(self, poll_id: str):
        super().__init__(timeout=None)
        self.poll_id = poll_id
        self.add_item(AddIdeaButton(poll_id))
        # voting buttons will be added in later steps (Part B/C)

# -------------------------
# Poll embed generation (simple)
# -------------------------
def generate_poll_embed_from_db(poll_id: str, guild: discord.Guild | None = None) -> discord.Embed:
    rows = db_execute("SELECT id, option_text, created_at, author_id FROM options WHERE poll_id = ? ORDER BY id ASC", (poll_id,), fetch=True) or []
    embed = discord.Embed(
        title="📋 Worauf hast du diese Woche Lust?",
        description="Gib eigene Ideen ein oder stimme ab (Buttons folgen).",
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc)
    )
    if not rows:
        embed.add_field(name="ℹ️ Keine Ideen", value="Sei der Erste und füge eine Idee hinzu!", inline=False)
    else:
        for opt_id, opt_text, created_at, author_id in rows:
            author = get_user_display(guild, author_id) if author_id else "Unbekannt"
            embed.add_field(name=opt_text or "(ohne Titel)", value=f"von {author}", inline=False)
    return embed

# -------------------------
# Post poll to channel (Part A)
# -------------------------
async def post_poll_to_channel(channel: discord.TextChannel):
    poll_id = datetime.now(tz=ZoneInfo(POST_TIMEZONE)).strftime("%Y%m%dT%H%M%S")
    created_at = datetime.now(timezone.utc).isoformat()
    # create poll record
    db_execute("INSERT OR REPLACE INTO polls(id, created_at) VALUES (?, ?)", (poll_id, created_at))
    embed = generate_poll_embed_from_db(poll_id, channel.guild)
    view = PollView(poll_id)
    try:
        bot.add_view(view)
    except Exception:
        pass
    sent = await channel.send(embed=embed, view=view)
    return poll_id, sent

# -------------------------
# Commands: startpoll
# -------------------------
@bot.command()
async def startpoll(ctx):
    """Post a new poll in this channel (Part A)."""
    poll_id, sent = await post_poll_to_channel(ctx.channel)
    await ctx.send(f"Poll gepostet: id={poll_id}", delete_after=10)

# -------------------------
# (Existing event/RSVP/reminder code kept unchanged)
# -------------------------
# For brevity this file retains the last working event/RSVP/reminder implementations.
# I'll reinsert them here unchanged (they were present in previous step files).
# To keep this file concise in the iterative process, I'm importing them from the current runtime state:
# However, because this environment is a single file, we need to include the existing logic inline.
# The event/RSVP/reminder handlers are preserved below — copied from the previous working step.

# Reminder & RSVP/Event handlers (copied from previous working version)
def build_event_embed_from_db(discord_event_id: str, guild: discord.Guild | None = None):
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

# Reminder scheduling and handlers are left as in prior step; minimal stubs if needed
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
                        db_execute("UPDATE tracked_events SET posted_channel_id = NULL, posted_message_id = NULL WHERE discord_event_id = ?", (discord_event_id,))
                        old_msg = None
                    except Exception:
                        log.exception("Error fetching old event message during reminder for %s", discord_event_id)
                        old_msg = None
                    if old_msg:
                        try:
                            await old_msg.delete()
                        except discord.NotFound:
                            pass
                        except Exception:
                            log.exception("Failed deleting old event message during reminder for %s", discord_event_id)
    try:
        sent = await ch.send(embed=embed, view=view)
        db_execute("UPDATE tracked_events SET posted_channel_id = ?, posted_message_id = ?, updated_at = ? WHERE discord_event_id = ?",
                   (ch.id, sent.id, datetime.now(timezone.utc).isoformat(), discord_event_id))
    except Exception:
        log.exception("Failed sending reminder message for %s", discord_event_id)

def schedule_reminders_for_event(bot_inst, scheduler_inst, discord_event_id: str, start_time):
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
        start_time = start_time.replace(tzinfo=timezone.utc)
    t24 = start_time - timedelta(hours=24)
    t2 = start_time - timedelta(hours=2)
    now = datetime.now(timezone.utc)
    if t24 > now:
        scheduler_inst.add_job(lambda: bot_inst.loop.create_task(reminder_coro(EVENTS_CHANNEL_ID, discord_event_id, 24)),
                               trigger=DateTrigger(run_date=t24), id=f"event_reminder_24_{discord_event_id}", replace_existing=True)
    elif t24 <= now < start_time:
        bot_inst.loop.create_task(reminder_coro(EVENTS_CHANNEL_ID, discord_event_id, 24))
    if t2 > now:
        scheduler_inst.add_job(lambda: bot_inst.loop.create_task(reminder_coro(EVENTS_CHANNEL_ID, discord_event_id, 2)),
                               trigger=DateTrigger(run_date=t2), id=f"event_reminder_2_{discord_event_id}", replace_existing=True)
    elif t2 <= now < start_time:
        bot_inst.loop.create_task(reminder_coro(EVENTS_CHANNEL_ID, discord_event_id, 2))

bot_inst = bot
scheduler_inst = scheduler

# Event handlers (create/update/delete) kept from prior step (unchanged)
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
        db_execute("INSERT OR REPLACE INTO tracked_events(guild_id, discord_event_id, start_time, updated_at) VALUES (?, ?, ?, ?)",
                   (guild_id, discord_event_id, start_iso, now_iso))
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
                            try:
                                schedule_reminders_for_event(bot_inst, scheduler_inst, discord_event_id, event.start_time)
                            except Exception:
                                log.exception("Failed scheduling reminders (create)")
                            return
                        except discord.NotFound:
                            db_execute("UPDATE tracked_events SET posted_channel_id = NULL, posted_message_id = NULL WHERE discord_event_id = ?", (discord_event_id,))
                        except Exception:
                            log.exception("Error verifying existing posted message")
        ch = bot.get_channel(EVENTS_CHANNEL_ID)
        if not ch:
            log.warning("Events channel %s not found", EVENTS_CHANNEL_ID)
            return
        embed = build_event_embed_from_db(discord_event_id, event.guild)
        try:
            bot.add_view(EventRSVPView(discord_event_id, event.guild))
        except Exception:
            pass
        sent = await ch.send(embed=embed, view=EventRSVPView(discord_event_id, event.guild))
        db_execute("UPDATE tracked_events SET posted_channel_id = ?, posted_message_id = ?, updated_at = ? WHERE discord_event_id = ?",
                   (ch.id, sent.id, now_iso, discord_event_id))
        try:
            schedule_reminders_for_event(bot_inst, scheduler_inst, discord_event_id, event.start_time)
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
# Startup: register PollViews persistently & re-register RSVP views
# -------------------------
@bot.event
async def on_ready():
    log.info("Bot ready: %s (id=%s)", bot.user, bot.user.id)
    init_db()
    if not scheduler.running:
        scheduler.start()
    # re-register RSVP views
    try:
        rows = db_execute("SELECT discord_event_id FROM tracked_events", fetch=True) or []
        for (did,) in rows:
            try:
                bot.add_view(EventRSVPView(did, None))
            except Exception:
                pass
    except Exception:
        log.exception("Failed to re-register RSVP views on startup")
    # register PollView instances for existing polls
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
# Debug commands for polls
# -------------------------
@bot.command()
async def listpolls(ctx):
    rows = db_execute("SELECT id, created_at FROM polls ORDER BY created_at DESC", fetch=True) or []
    await ctx.send(f"polls: {rows}")

@bot.command()
async def listoptions(ctx, poll_id: str):
    rows = db_execute("SELECT id, option_text, created_at, author_id FROM options WHERE poll_id = ? ORDER BY id ASC", (poll_id,), fetch=True) or []
    pretty = [(r[0], r[1], r[2], get_user_display(ctx.guild, r[3]) if r[3] else None) for r in rows]
    await ctx.send(f"options for {poll_id}: {pretty}")

@bot.command()
async def ping(ctx):
    await ctx.send("pong")

if __name__ == "__main__":
    if not BOT_TOKEN:
        print("Bitte BOT_TOKEN als Umgebungsvariable setzen.")
        raise SystemExit(1)
    init_db()
    bot.run(BOT_TOKEN)
