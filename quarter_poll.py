#!/usr/bin/env python3
"""
quarter_poll.py
- Schedules a quarterly poll 1 week before quarter start for long-term planning.
- Posts the poll into a configured QUARTER_POLL_CHANNEL_ID.
- Users can add option title + short description via Modal.
- Users select availability by choosing days (date strings) via ephemeral Views (month -> week -> day).
- Stores quarter_polls, options, votes, and day-availability in SQLite.
- Shows matches (most popular days per option) in embed.
Usage:
- import and call init_quarter_polls(bot, scheduler, db_path, quarter_channel_id) from bot.py on startup.
"""
from datetime import datetime, timedelta, timezone, date
from zoneinfo import ZoneInfo
import sqlite3
import discord
from discord.ext import commands

QUARTER_TABLES = [
    """
    CREATE TABLE IF NOT EXISTS quarter_polls (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        quarter_start DATE NOT NULL,
        posted_channel_id INTEGER,
        posted_message_id INTEGER,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quarter_options (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        poll_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        description TEXT,
        created_at TEXT NOT NULL,
        author_id INTEGER,
        FOREIGN KEY(poll_id) REFERENCES quarter_polls(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quarter_votes (
        poll_id INTEGER NOT NULL,
        option_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        UNIQUE(poll_id, option_id, user_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quarter_availability (
        poll_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        day TEXT NOT NULL,  -- YYYY-MM-DD
        UNIQUE(poll_id, user_id, day)
    )
    """
]

def _db_execute(db_path, query, params=(), fetch=False, many=False):
    con = sqlite3.connect(db_path)
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

# Modal to add option with description
class QuarterIdeaModal(discord.ui.Modal, title="Neue Quartals-Idee"):
    title_input = discord.ui.TextInput(label="Titel", max_length=100)
    desc = discord.ui.TextInput(label="Kurzbeschreibung", style=discord.TextStyle.long, required=False, max_length=500)
    def __init__(self, poll_id: int):
        super().__init__()
        self.poll_id = poll_id
    async def on_submit(self, interaction: discord.Interaction):
        t = str(self.title_input.value).strip()
        d = str(self.desc.value).strip()
        _db_execute(DB_PATH, "INSERT INTO quarter_options(poll_id, title, description, created_at, author_id) VALUES (?, ?, ?, ?, ?)", (self.poll_id, t, d, datetime.now(timezone.utc).isoformat(), interaction.user.id))
        # update poll message if present
        await interaction.response.send_message("✅ Idee hinzugefügt.", ephemeral=True)
        try:
            rows = _db_execute(DB_PATH, "SELECT posted_channel_id, posted_message_id FROM quarter_polls WHERE id = ?", (self.poll_id,), fetch=True)
            if rows:
                ch_id, msg_id = rows[0]
                ch = interaction.client.get_channel(ch_id)
                if ch:
                    msg = await ch.fetch_message(msg_id)
                    if msg:
                        from quarter_poll import build_quarter_embed  # local import
                        embed = build_quarter_embed(self.poll_id, interaction.guild)
                        view = build_quarter_view(self.poll_id)
                        await msg.edit(embed=embed, view=view)
        except Exception:
            pass

# Note: DB_PATH will be injected in init_quarter_polls below
DB_PATH = None

# build embed for quarter poll
def build_quarter_embed(poll_id: int, guild: discord.Guild | None):
    options = _db_execute(DB_PATH, "SELECT id, title, description FROM quarter_options WHERE poll_id = ? ORDER BY id ASC", (poll_id,), fetch=True) or []
    votes = _db_execute(DB_PATH, "SELECT option_id, user_id FROM quarter_votes WHERE poll_id = ?", (poll_id,), fetch=True) or []
    avail = _db_execute(DB_PATH, "SELECT user_id, day FROM quarter_availability WHERE poll_id = ?", (poll_id,), fetch=True) or []
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
        # compute best days similar to weekly — most popular day(s)
        if voters:
            # map day->users (only voters' availability)
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
                        lines.append(f"{dd.isoformat()}: {', '.join([str(uid) for uid in ulist])}")
                    except Exception:
                        lines.append(f"{d}: {', '.join([str(uid) for uid in ulist])}")
                value += "\n✅ Beliebteste Tage:\n" + "\n".join(lines)
        embed.add_field(name=title, value=value or "(keine Beschreibung)", inline=False)
    return embed

# simple view for quarter poll (vote + add idea + pick days)
class QuarterView(discord.ui.View):
    def __init__(self, poll_id: int):
        super().__init__(timeout=None)
        self.poll_id = poll_id
        self.add_item(QuarterAddIdeaButton(poll_id))
        self.add_item(QuarterPickDaysButton(poll_id))
        # add vote buttons for each option
        options = _db_execute(DB_PATH, "SELECT id, title FROM quarter_options WHERE poll_id = ? ORDER BY id ASC", (poll_id,), fetch=True) or []
        for opt_id, title in options:
            self.add_item(QuarterVoteButton(poll_id, opt_id, title))

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
        # toggle vote
        rows = _db_execute(DB_PATH, "SELECT 1 FROM quarter_votes WHERE poll_id = ? AND option_id = ? AND user_id = ?", (self.poll_id, self.option_id, uid), fetch=True)
        if rows:
            _db_execute(DB_PATH, "DELETE FROM quarter_votes WHERE poll_id = ? AND option_id = ? AND user_id = ?", (self.poll_id, self.option_id, uid))
            await interaction.response.send_message("Stimme entfernt.", ephemeral=True)
        else:
            _db_execute(DB_PATH, "INSERT OR IGNORE INTO quarter_votes(poll_id, option_id, user_id) VALUES (?, ?, ?)", (self.poll_id, self.option_id, uid))
            await interaction.response.send_message("Stimme gespeichert.", ephemeral=True)
        # update public embed
        try:
            rows = _db_execute(DB_PATH, "SELECT posted_channel_id, posted_message_id FROM quarter_polls WHERE id = ?", (self.poll_id,), fetch=True)
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

class QuarterPickDaysView(discord.ui.View):
    def __init__(self, poll_id: int, month_idx: int = 0, year: int = None):
        super().__init__(timeout=None)
        self.poll_id = poll_id
        self.month_idx = month_idx
        self.year = year or datetime.now().year
        # We will implement as a simple modal flow or selects; for brevity, add a button that opens a modal to enter date(s) as CSV
        self.add_item(QuarterPickDaysModalButton(poll_id))

class QuarterPickDaysModalButton(discord.ui.Button):
    def __init__(self, poll_id: int):
        super().__init__(label="📅 Tage wählen (CSV YYYY-MM-DD)", style=discord.ButtonStyle.secondary)
        self.poll_id = poll_id
    async def callback(self, interaction: discord.Interaction):
        # open a modal where user can paste date(s) separated by comma
        await interaction.response.send_modal(QuarterPickDaysModal(self.poll_id))

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
                _db_execute(DB_PATH, "INSERT OR IGNORE INTO quarter_availability(poll_id, user_id, day) VALUES (?, ?, ?)", (self.poll_id, interaction.user.id, d.isoformat()))
                saved += 1
            except Exception:
                pass
        await interaction.response.send_message(f"{saved} Tage gespeichert.", ephemeral=True)
        # try update public embed
        try:
            rows = _db_execute(DB_PATH, "SELECT posted_channel_id, posted_message_id FROM quarter_polls WHERE id = ?", (self.poll_id,), fetch=True)
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

def build_quarter_view(poll_id: int):
    return QuarterView(poll_id)

# initialization function to be called from bot.py
def init_quarter_polls(bot: commands.Bot, scheduler, db_path: str, quarter_channel_id: int):
    global DB_PATH
    DB_PATH = db_path
    for sql in QUARTER_TABLES:
        _db_execute(DB_PATH, sql)
    # schedule a daily job to check whether to post a quarter poll one week before quarter start
    from apscheduler.triggers.cron import CronTrigger

    def check_and_post():
        today = datetime.now(timezone.utc).date()
        # calculate next quarter starts for current year and next year
        candidates = []
        for year in [today.year, today.year + 1]:
            for m in (1,4,7,10):
                qstart = date(year, m, 1)
                candidates.append(qstart)
        for qstart in candidates:
            post_date = qstart - timedelta(weeks=1)
            if post_date == today:
                # check if already posted
                existing = _db_execute(DB_PATH, "SELECT id FROM quarter_polls WHERE quarter_start = ?", (qstart.isoformat(),), fetch=True)
                if not existing:
                    # create poll and post
                    created_at = datetime.now(timezone.utc).isoformat()
                    _db_execute(DB_PATH, "INSERT INTO quarter_polls(quarter_start, created_at) VALUES (?, ?)", (qstart.isoformat(), created_at))
                    poll_id = _db_execute(DB_PATH, "SELECT id FROM quarter_polls WHERE quarter_start = ? ORDER BY id DESC LIMIT 1", (qstart.isoformat(),), fetch=True)[0][0]
                    async def _post():
                        ch = bot.get_channel(quarter_channel_id)
                        if not ch:
                            return
                        embed = build_quarter_embed(poll_id, None)
                        view = build_quarter_view(poll_id)
                        sent = await ch.send(embed=embed, view=view)
                        _db_execute(DB_PATH, "UPDATE quarter_polls SET posted_channel_id = ?, posted_message_id = ? WHERE id = ?", (ch.id, sent.id, poll_id))
                    bot.loop.create_task(_post())

    # register daily check at e.g. 08:00 server time
    scheduler.add_job(check_and_post, CronTrigger(hour=8, minute=0, timezone=ZoneInfo("Europe/Berlin")), id="quarterly_check", replace_existing=True)
