# ============================================================
# HOW THIS FILE IS ORGANIZED
# ============================================================
# 1. Imports & gspread setup       -> UNCHANGED, as requested
# 2. Sheet layout config           -> column numbers, all in one place
# 3. Embed / style config          -> colors, footer branding
# 4. Generic helpers                -> sheet I/O, lookups, embeds, bars
# 5. Permissions                    -> role check + reusable decorator
# 6. Business logic                 -> promotion & points calculations
# 7. Bot setup + central error handler
# 8. COMMAND TEMPLATE (commented)   -> copy this for any new command
# 9. Commands: test, register, orbat, progress, addpoints, promote
#
# The idea: if you ever need to add a new command, steps 2-6 already give
# you every building block (column constants, lookup helpers, embed
# builders, the permission decorator). You mostly just write the command
# body following the template in step 8.

# DISCORD PY IMPORTS
import discord
from discord import app_commands
from discord.ext import commands

# GSPREAD IMPORTS
import gspread
from google.oauth2.service_account import Credentials
from gspread.cell import Cell

# MISC IMPORTS
import requests
import asyncio
import aiohttp
import re

from datetime import datetime, timezone, timedelta

import os
import json
from dotenv import load_dotenv

load_dotenv()

def require_env(name: str) -> str:
    """Reads a required secret from the environment and fails LOUD and
    EARLY (at startup) if it's missing, instead of the bot silently
    crashing later the first time something tries to use it."""
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            f"Set it in your .env file or your host's environment settings."
        )
    return value


# GLOBAL STUFF
ranks = ["Cadet", "Private", "Private First Class", "Specialist", "Lance Corporal", "Corporal", "Sergeant", "Staff Sergeant", "Sergeant Major", "Warrant Officer", "Second Lieutenant", "Lieutenant", "Captain", "Senior Warrant Officer", "Major", "Colonel", "Battalion Commander"]
CMD_PERMS_ROLES = {1549719657420693514, 1423287377111023679}
cell_list = [] # remember to reset after each command.
command_lock = asyncio.Lock()

# SECRETS
DISCORD_BOT_TOKEN = require_env("DISCORD_BOT_TOKEN")
APPS_SCRIPT_URL = require_env("APPS_SCRIPT_URL")
APPS_SCRIPT_SECRET = require_env("APPS_SCRIPT_SECRET")
BLOXLINK_API_KEY = os.environ.get("BLOXLINK_API_KEY")


# ONLY FOR DEVELOPMENT
GUILD_ID = 1007656642038472754

# GSPREAD INIT
scopes = ["https://www.googleapis.com/auth/spreadsheets"]

_creds_json = os.environ.get("GOOGLE_CREDS_JSON")
if _creds_json:
    creds = Credentials.from_service_account_info(json.loads(_creds_json), scopes=scopes)
else:
    GOOGLE_CREDS_PATH = os.environ.get("GOOGLE_CREDS_PATH", "creds.json")
    creds = Credentials.from_service_account_file(GOOGLE_CREDS_PATH, scopes=scopes)
 
client = gspread.authorize(creds)
sheet_id = require_env("SHEET_ID")
sh = client.open_by_key(sheet_id)
sheet = sh.worksheet("Data")


# Rank display order for /battalion stats: most senior -> most junior
DISPLAY_RANK_ORDER = [r for r in reversed(ranks) if r != "Cadet"]

# Roblox group IDs tracked by /bgcheck. Add or remove entries here to track
# more (or fewer) groups — nothing else in the code needs to change.
ROBLOX_GROUPS = {
    "442nd Battalion": 990610899,
    "GAR": 1092793046,
}
 
ROBLOX_API_TIMEOUT = 10  # seconds, per individual Roblox API request

# ============================================================
# SHEET LAYOUT CONFIG
# ============================================================
# Everything here describes WHERE things live in the "Data" sheet. If the
# sheet layout ever changes (columns added/moved), this is the only place
# you should need to update — every command reads through these constants
# instead of hardcoding numbers.
#
# NOTE: these are 0-indexed, matching the position of each value inside a
# row returned by sheet.get_all_values() (e.g. row[COL_USERNAME]).
# When writing a Cell(), gspread wants a 1-indexed column, so we do
# `COL_X + 1` at the point of writing.

DATA_START_ROW = 11   # first sheet row (1-indexed) containing a player
DATA_END_ROW = 978    # last sheet row (1-indexed) reserved for players

COL_USERNAME    = 3   # D - Roblox username
COL_RANK        = 4   # E - in-game rank
COL_DESIGNATION = 5   # F - e.g. CT-5142 "Henry"
COL_TIMEZONE    = 6   # G - timezone
COL_POINTS      = 7   # H - TOTAL points (sheet formula = I+J+K, never write to this)
COL_TRAINING    = 8   # I - Battalion Event (BE) points
COL_PD          = 9   # J - Permadeath (PD) points
COL_HOSTED      = 10  # K - Hosted points
COL_PROMOTION   = 11  # L - promotion checkbox
COL_LAST_EVENT  = 12  # M - date of last event
COL_JOINED      = 13  # N - date joined
COL_RETURN      = 14  # O - return / WIA date

# Maps the /addpoints "points_type" choice value to the column it edits.
POINTS_TYPE_COLUMNS = {
    "BE": COL_TRAINING,
    "PD": COL_PD,
    "Hosted": COL_HOSTED,
}

# Promotion thresholds. Only ranks listed here are auto-promotable via BE/PD
# points; anything else (NCO/Officer tier) is handled manually by
# leadership. The NEXT rank is looked up dynamically from the `ranks` list
# above, so you only need to keep one source of truth for rank order.
POINT_PROMOTION_REQUIREMENTS = {
    "Private":              {"be": 4,  "pd": 2},
    "Private First Class":  {"be": 8,  "pd": 4},
    "Specialist":           {"be": 12, "pd": 8},
}

# The highest rank whose points are still tracked in the sheet, but which
# isn't itself auto-promotable — promotion beyond this is a manual
# leadership decision, not a BE/PD threshold. /progress shows a distinct
# "sheet ceiling reached" message for this rank instead of the generic
# NCO/Officer tier one.
SHEET_CEILING_RANK = "Lance Corporal"


# ============================================================
# EMBED / STYLE CONFIG
# ============================================================
EMBED_COLOR_SUCCESS = 0x57F287  # green  - things went well
EMBED_COLOR_ERROR   = 0xED4245  # red    - validation / lookup failures
EMBED_COLOR_GOLD    = 0xF1C40F  # gold   - "not yet", informational
EMBED_COLOR_INFO    = 0x5865F2  # blurple - neutral default
EMBED_COLOR_EVENT   = 0x2ECC71  # green  - /event embeds

FOOTER_BRAND = "442nd ORBAT System"

# ============================================================
# EVENT SYSTEM CONFIG  (used by /event)
# ============================================================
# Everything /event needs to know about the battalion's companies /
# detachments lives here. To wire this up for the real server:
#   1. Replace each "emoji" below with the real custom emote, pasted
#      straight from Discord (type \:emojiname: in a message and copy the
#      result, e.g. "<:horn:123456789012345678>"). Plain unicode emoji
#      (the keycap numbers below) and custom emoji strings both work —
#      see the _as_emoji() helper further down.
#   2. Optionally set "logo_url" to a direct image link. It's used as the
#      default thumbnail on a company-specific event for that unit (only
#      if the event creator doesn't upload their own thumbnail).
# Nothing else in the code needs to change — every event command reads
# through this dict instead of hardcoding company names.
EVENT_TYPE_GENERAL = "general"   # one event for the whole battalion; one reaction emoji per company
EVENT_TYPE_COMPANY = "company"   # one event for a single company; Accepted/Declined/Tentative buttons
 
BATTALION_COMPANIES = {
    # key                  label                            emoji    logo_url
    "horn_company":      {"label": "Horn Company",             "emoji": "1️⃣", "logo_url": None},
    "manticore_company": {"label": "Manticore Company",        "emoji": "2️⃣", "logo_url": None},
    "doom_company":      {"label": "Doom Company",             "emoji": "3️⃣", "logo_url": None},
    "battalion_command": {"label": "Battalion Command",        "emoji": "4️⃣", "logo_url": None},
    "arc_detachment":    {"label": "ARC Detachment",           "emoji": "5️⃣", "logo_url": None},
    "viper_company":     {"label": "Viper Company",            "emoji": "6️⃣", "logo_url": None},
    "jedi":              {"label": "Jedi",                     "emoji": "7️⃣", "logo_url": None},
    "arc_rc_liaison":    {"label": "ARC Liaison / RC Liaison", "emoji": "8️⃣", "logo_url": None},
    "guests":            {"label": "Guests",                   "emoji": "9️⃣", "logo_url": None},
}
 
EVENT_REMINDER_LEAD_MINUTES     = 20    # how long before start the reminder thread + ping fires
EVENT_REFRESH_DEBOUNCE_SECONDS  = 1.5   # coalesces a burst of reactions into a single embed edit
EVENT_RSVP_MUTUALLY_EXCLUSIVE   = True  # company events: can a member only hold ONE of Accepted/Declined/Tentative at a time?
EVENT_FIELD_CHAR_LIMIT          = 300   # per-category name list cap, keeps embeds under Discord's 6000-char total limit
EVENT_DESCRIPTION_CHAR_LIMIT    = 500   # same reason
EVENTS_FILE = "events_data.json"        # where tracked events + RSVP lists persist across bot restarts
 
# Accepted formats for the start/end time prompts, tried in order. Add or
# remove strptime patterns here — nothing else needs to change. Times are
# entered in the user's OWN local time; their UTC offset (asked for in a
# separate step) is what lets the bot convert it to a real UTC instant.
DATETIME_FORMATS = [
    "%d/%m/%Y %H:%M",
    "%d-%m-%Y %H:%M",
    "%Y-%m-%d %H:%M",
]
DATETIME_FORMAT_EXAMPLES = [
    "26/09/2026 15:00",
    "26-09-2026 15:00",
    "2026-09-26 15:00",
]



# ============================================================
# GENERIC HELPERS
# ============================================================

def safe_int(value, default=0):
    """Converts a sheet cell to int, tolerating blanks/garbage instead of
    crashing the whole command on one bad row."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def get_cell(row, col_index, default=""):
    """Safely reads row[col_index]. Google Sheets (via gspread) trims
    trailing empty cells from each row, so a short row is normal, not a
    bug — this just returns `default` instead of raising IndexError."""
    if col_index < len(row):
        value = row[col_index]
        return value if value != "" else default
    return default


def truncate_field(text, limit=1024):
    """Discord embed field values are capped at 1024 characters. This
    keeps a huge /addpoints or /promote batch from crashing the send."""
    text = str(text) if text not in (None, "") else "N/A"
    if len(text) <= limit:
        return text
    return text[: limit - 20] + "\n… (truncated)"


def parse_username_list(raw: str):
    """Turns 'user1, user2,user1' into ['user1', 'user2'] — trims
    whitespace and removes case-insensitive duplicates, preserving order."""
    names = []
    for part in raw.split(","):
        name = part.strip()
        if not name:
            continue
        if not any(existing.lower() == name.lower() for existing in names):
            names.append(name)
    return names


def build_username_index(list_of_lists):
    """
    Builds a {lowercase_username: row_index} dict in one pass over the
    player range, so every command can look up any number of users in O(1)
    each instead of re-scanning the sheet per user (this replaces the old
    substring-based `find()` generator, which also risked false-positive
    matches against other columns).
    """
    index = {}
    start = DATA_START_ROW - 1  # to 0-based
    end = min(DATA_END_ROW, len(list_of_lists))
    for i in range(start, end):
        username = get_cell(list_of_lists[i], COL_USERNAME)
        if username:
            index[username.lower()] = i
    return index


def find_first_empty_row(list_of_lists):
    """Returns the 0-based index of the first row (within the player
    range) with no username set, for /register. If the sheet's fetched
    data ends before DATA_END_ROW (Sheets trims trailing empty rows),
    the first row past what was fetched counts as empty too."""
    start = DATA_START_ROW - 1
    for i in range(start, DATA_END_ROW):
        if i >= len(list_of_lists) or not get_cell(list_of_lists[i], COL_USERNAME):
            return i
    return None  # sheet is full


async def get_sheet_data():
    """
    The ONE read call every command needs. Runs sheet.get_all_values() in
    a background thread via asyncio.to_thread — gspread is a blocking/sync
    library, and calling it directly would freeze the bot's whole event
    loop (heartbeats, other interactions, etc.) while waiting on Google.
    """
    return await asyncio.to_thread(sheet.get_all_values)


async def update_cells(cells):
    """
    The ONE write call every command needs. Flushes every queued Cell in
    a single batched request (also off the event loop), then clears the
    shared cell_list so the next command starts fresh.
    """
    if not cells:
        return
    await asyncio.to_thread(sheet.update_cells, cells)
    cell_list.clear()


def make_progress_bar(current, required, length=15):
    """Cosmetic text progress bar for /progress, e.g. '███████░░░░░░░░ 8/12 (67%)'.
    Change `length` or the block characters to restyle it."""
    pct = 100 if required <= 0 else min(100, int((current / required) * 100))
    filled = round(length * pct / 100)
    bar = "█" * filled + "░" * (length - filled)
    return f"{bar} **{current}/{required}** ({pct}%)"


def format_requirement_line(current, required, label):
    """One requirement's full line for /progress: bar + met/not-met status."""
    bar = make_progress_bar(current, required)
    if current >= required:
        return f"{bar}\n✅ Requirement met"
    return f"{bar}\nStill needed: **{required - current}** {label}"


def make_embed(interaction, title, description=None, color=EMBED_COLOR_INFO, fields=None, verb="Requested"):
    """
    Standard embed builder used by every command's successful response:
    consistent title/color/fields, a branded footer ("<brand> • <verb> by
    <display name>"), the bot's avatar as the footer icon, and a timestamp
    (Discord renders this as "Today at HH:MM" automatically).

    `fields` is a list of (name, value, inline) tuples.
    `verb` is "Requested" for read-only commands (/orbat, /progress) or
    "Updated" for commands that write to the sheet (/addpoints, /promote,
    /register) — purely cosmetic, tweak freely.
    """
    embed = discord.Embed(title=title, color=color)
    if description:
        embed.description = description
    for name, value, inline in (fields or []):
        embed.add_field(name=name, value=truncate_field(value), inline=inline)

    footer = f"{FOOTER_BRAND} • {verb} by {interaction.user.display_name}"
    icon_url = interaction.user.display_avatar.url if interaction.user else None
    embed.set_footer(text=footer, icon_url=icon_url)
    embed.timestamp = discord.utils.utcnow()
    return embed


def make_error_embed(title, description):
    """Standalone error embed — doesn't need interaction context, so it
    can be used even before responding/deferring."""
    embed = discord.Embed(title=title, description=description, color=EMBED_COLOR_ERROR)
    embed.set_footer(text=FOOTER_BRAND)
    embed.timestamp = discord.utils.utcnow()
    return embed


def make_busy_embed():
    return make_error_embed(
        "Bot Is Busy",
        "Another command is currently being processed. Please wait a moment and try again.",
    )

# ============================================================
# ROBLOX API HELPERS  (used by /bgcheck)
# ============================================================
# These call Roblox's public REST APIs directly — no auth/API key needed
# for any of them except the optional Discord-verification lookup. Every
# helper is written to FAIL SAFE: one flaky/rate-limited endpoint just
# makes that one field show "Unknown" in the embed, instead of taking down
# the whole command (same philosophy as safe_int/get_cell above).
 
async def roblox_get_json(session, url, method="GET", json_body=None):
    """Thin wrapper every call below goes through, so the one try/except
    for 'this endpoint misbehaved' lives in a single place instead of
    being copy-pasted for every Roblox API call."""
    try:
        async with session.request(
            method, url, json=json_body, timeout=aiohttp.ClientTimeout(total=ROBLOX_API_TIMEOUT)
        ) as resp:
            if resp.status != 200:
                return None
            return await resp.json()
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return None
 
 
async def roblox_get_user_id(session, username):
    """Resolves a Roblox username -> (user_id, canonical_name) via the
    bulk username-lookup endpoint (case-insensitive, exact match only).
    Returns (None, None) if no such account exists."""
    data = await roblox_get_json(
        session,
        "https://users.roblox.com/v1/usernames/users",
        method="POST",
        json_body={"usernames": [username], "excludeBannedUsers": False},
    )
    if not data or not data.get("data"):
        return None, None
    match = data["data"][0]
    return match["id"], match["name"]
 
 
async def roblox_get_group_ranks(session, user_id, group_ids):
    """Returns {group_id: (role_name, rank_number)} for whichever of
    `group_ids` the user actually belongs to. A group_id missing from the
    result just means 'not a member of that group' — handled by the caller."""
    data = await roblox_get_json(session, f"https://groups.roblox.com/v1/users/{user_id}/groups/roles")
    result = {}
    if not data:
        return result
    for entry in data.get("data", []):
        gid = entry["group"]["id"]
        if gid in group_ids:
            result[gid] = (entry["role"]["name"], entry["role"]["rank"])
    return result
 
 
async def get_verified_discord_status(session, roblox_user_id):
    """
    Roblox itself has no concept of "verified Discord" — bots that show
    this field (like the one in the reference screenshot) are querying a
    third-party account-linking service such as Bloxlink or RoVer, which
    requires YOUR OWN API key from that service.
 
    This is intentionally left as a stub so /bgcheck works out of the box
    without one: set BLOXLINK_API_KEY above and fill in the real request
    below once you have a key. Until then it always reports "Not Configured".
    """
    if not BLOXLINK_API_KEY:
        return "Not Configured"
    # Example shape once you have a Bloxlink API key (check their current
    # docs before relying on this — it's a third-party, versioned API, not
    # a Roblox one, so the exact endpoint/auth format can change):
    #
    data = await roblox_get_json(
        session,
        f"https://api.blox.link/v4/public/guilds/{GUILD_ID}/roblox-to-discord/{roblox_user_id}",
    )  # would need an Authorization header with BLOXLINK_API_KEY
    return "Yes" if data and data.get("discordIDs") else "No"
 
 
async def fetch_roblox_profile(session, user_id, group_ids):
    """
    Fires every profile-detail request CONCURRENTLY via asyncio.gather —
    Roblox splits this data across several separate services (users/,
    friends/, groups/, premiumfeatures/, thumbnails/), so there's no single
    call that returns it all. Gathering them means /bgcheck's total wait is
    roughly the SLOWEST single call, not the sum of all seven of them.
    """
    details, history, friends_count, followers_count, group_ranks, premium_raw, thumbnail = await asyncio.gather(
        roblox_get_json(session, f"https://users.roblox.com/v1/users/{user_id}"),
        roblox_get_json(session, f"https://users.roblox.com/v1/users/{user_id}/username-history?limit=10&sortOrder=Desc"),
        roblox_get_json(session, f"https://friends.roblox.com/v1/users/{user_id}/friends/count"),
        roblox_get_json(session, f"https://friends.roblox.com/v1/users/{user_id}/followers/count"),
        roblox_get_group_ranks(session, user_id, group_ids),
        roblox_get_json(session, f"https://premiumfeatures.roblox.com/v1/users/{user_id}/validate-membership"),
        roblox_get_json(
            session,
            f"https://thumbnails.roblox.com/v1/users/avatar-headshot?userIds={user_id}&size=420x420&format=Png&isCircular=false",
        ),
    )
 
    past_names = [entry["name"] for entry in (history or {}).get("data", [])]
 
    created_str = (details or {}).get("created")
    if created_str:
        # Roblox timestamps look like "2019-05-06T12:34:56.789Z" — slice to
        # the whole-second part and mark it UTC explicitly (avoids the
        # deprecated naive-utcnow() pattern and keeps the day-count correct).
        created_date = datetime.strptime(created_str[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - created_date).days
        age_text = f"{age_days} ({created_date.day} {created_date.strftime('%b %Y')})"
    else:
        age_text = "Unknown"
 
    thumbnail_url = None
    if thumbnail and thumbnail.get("data"):
        thumbnail_url = thumbnail["data"][0].get("imageUrl")
 
    return {
        "name": (details or {}).get("name", "Unknown"),
        "past_names": ", ".join(past_names) if past_names else "-",
        "premium": premium_raw if isinstance(premium_raw, bool) else "Unknown",
        "friends": friends_count.get("count", "Unknown") if friends_count else "Unknown",
        "followers": followers_count.get("count", "Unknown") if followers_count else "Unknown",
        "age_text": age_text,
        "group_ranks": group_ranks,
        "thumbnail_url": thumbnail_url,
    }
 
 
def make_bgcheck_embed(interaction, roblox_id, profile):
    """Builds the /bgcheck embed. The title links straight to the Roblox
    profile (click the header — same as the reference screenshot); the
    'Profile URL' field repeats the same link as visible clickable text."""
    profile_url = f"https://www.roblox.com/users/{roblox_id}/profile"
 
    if profile["premium"] is True:
        premium_text = "Yes"
    elif profile["premium"] is False:
        premium_text = "No"
    else:
        premium_text = "Unknown"
 
    fields = [
        ("Name", profile["name"], True),
        ("Past Names", profile["past_names"], True),
        ("ID", str(roblox_id), True),
        ("Premium", premium_text, True),
        ("Profile URL", f"[Click Here]({profile_url})", True),
        ("Verified Discord", profile["verified_discord"], True),
        (
            "Statistics",
            f"Friends: {profile['friends']}\nFollowers: {profile['followers']}\nAge: {profile['age_text']}",
            False,
        ),
        ("Community Group Memberships", "\u200b", False),  # section header; \u200b = zero-width space (Discord fields can't have an empty value)
    ]
 
    for group_name, group_id in ROBLOX_GROUPS.items():
        if group_id in profile["group_ranks"]:
            role_name, rank_num = profile["group_ranks"][group_id]
            fields.append((group_name, f"{role_name} ({rank_num})", True))
        else:
            fields.append((group_name, "Not in group", True))
 
    embed = make_embed(
        interaction,
        title=f"Background Check | {profile['name']}",
        color=EMBED_COLOR_INFO,
        fields=fields,
        verb="Requested",
    )
    embed.url = profile_url  # makes the embed TITLE itself clickable
    if profile["thumbnail_url"]:
        embed.set_thumbnail(url=profile["thumbnail_url"])
    return embed



# ============================================================
# PERMISSIONS
# ============================================================

def has_allowed_role(interaction: discord.Interaction) -> bool:
    member = interaction.user
    if not isinstance(member, discord.Member):
        return False  # e.g. used in DMs, where roles don't apply
    return any(role.id in CMD_PERMS_ROLES for role in member.roles)


def require_role():
    """
    Reusable permission gate. Add `@require_role()` right under
    `@bot.tree.command(...)` on any command that should be restricted to
    CMD_PERMS_ROLES. Failures are caught centrally by
    on_app_command_error below, so nothing else is needed in the command
    body — leave this decorator off for a command anyone can use (like
    /orbat and /progress).
    """
    async def predicate(interaction: discord.Interaction) -> bool:
        return has_allowed_role(interaction)
    return app_commands.check(predicate)


# ============================================================
# BUSINESS LOGIC
# ============================================================

def get_promotion_status(row):
    """
    Pure, side-effect-free evaluation of a single row's promotion
    eligibility. Used by BOTH /progress (read-only display) and /promote
    (which additionally queues the sheet writes if eligible) — keeping
    the eligibility math in one place avoids the two commands drifting
    out of sync.
    """
    current_rank = get_cell(row, COL_RANK)
    requirement = POINT_PROMOTION_REQUIREMENTS.get(current_rank)
    be = safe_int(get_cell(row, COL_TRAINING, "0"))
    pd = safe_int(get_cell(row, COL_PD, "0"))
 
    if requirement is None:
        return {"trackable": False, "rank": current_rank, "be": be, "pd": pd}
 
    try:
        next_rank = ranks[ranks.index(current_rank) + 1]
    except (ValueError, IndexError):
        next_rank = "N/A"
 
    return {
        "trackable": True,
        "rank": current_rank,
        "next_rank": next_rank,
        "be": be, "req_be": requirement["be"],
        "pd": pd, "req_pd": requirement["pd"],
        "eligible": be >= requirement["be"] and pd >= requirement["pd"],
    }



def apply_points(list_of_lists, username_index, usernames, amount, column):
    """
    Queues +amount Cell updates for `column` for every matched username.
    Returns (updated, not_found):
      updated   -> list of (display_name, old_value, new_value)
      not_found -> list of usernames with no matching row
    """
    updated, not_found = [], []
    for name in usernames:
        row_idx = username_index.get(name.lower())
        if row_idx is None:
            not_found.append(name)
            continue
        row = list_of_lists[row_idx]
        display_name = get_cell(row, COL_USERNAME, name)
        current = safe_int(get_cell(row, column, "0"))
        new_value = current + amount
        cell_list.append(Cell(row=row_idx + 1, col=column + 1, value=new_value))
        cell_list.append(Cell(row=row_idx + 1, col=COL_LAST_EVENT + 1, value=datetime.now().strftime("%d/%m/%Y")))
        updated.append((display_name, current, new_value))
    return updated, not_found


def apply_promotions(list_of_lists, username_index, usernames):
    """
    Evaluates every username with get_promotion_status() and queues the
    rank-change + BE-reset Cells for anyone eligible. Returns a dict
    bucketed by outcome, ready to drop straight into embed fields.
    """
    result = {"promoted": [], "not_eligible": [], "not_trackable": [], "not_found": []}

    for name in usernames:
        row_idx = username_index.get(name.lower())
        if row_idx is None:
            result["not_found"].append(name)
            continue

        row = list_of_lists[row_idx]
        display_name = get_cell(row, COL_USERNAME, name)
        status = get_promotion_status(row)

        if not status["trackable"]:
            result["not_trackable"].append((display_name, status["rank"]))
            continue

        if status["eligible"]:
            cell_list.append(Cell(row=row_idx + 1, col=COL_RANK + 1, value=status["next_rank"]))
            cell_list.append(Cell(row=row_idx + 1, col=COL_TRAINING + 1, value=0))  # only BE resets, matches original behavior
            result["promoted"].append((display_name, status["rank"], status["next_rank"]))
        else:
            result["not_eligible"].append((display_name, status))

    return result


def sort_users():
    """Triggers the Apps Script that re-sorts the sheet by rank. Only
    called after a promotion actually changes someone's rank."""
    response = requests.post(
        APPS_SCRIPT_URL,
        json={"token": APPS_SCRIPT_SECRET},
        timeout=30,
    )
    response.raise_for_status()

    try:
        result = response.json()
    except ValueError:
        raise RuntimeError("Apps Script returned a non-JSON response: " + response.text[:500])

    if not result.get("success"):
        raise Exception(f"Sorting failed: {result.get('error')}")

    print("Users sorted correctly.")


# ============================================================
# DISCORD BOT SETUP
# ============================================================
intents = discord.Intents.default()
intents.message_content = True


class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
        self.http_session = None  # created below in setup_hook, closed in close()

    async def setup_hook(self):

        # One shared aiohttp session, reused across every /bgcheck call
        # instead of opening a fresh connection per request — aiohttp pools
        # and keeps-alive connections under the hood, so this alone
        # meaningfully cuts down Roblox API latency on repeat lookups.
        self.http_session = aiohttp.ClientSession()


        # Reload any /event events that were still active before the last
        # restart (their RSVP lists and all), and re-register the
        # persistent RSVP button view so company-event buttons keep
        # working without the message needing to be resent. Must happen
        # before tree.sync() so commands and components come up together.

        global active_events
        active_events = load_events()
        self.add_view(EventRSVPView())
        for event in active_events.values():
            schedule_event_reminder(event)
        print(f"Loaded {len(active_events)} tracked event(s) from disk.")


        guild = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        print("Slash commands synced to guild.")

        # USA QUESTO QUANDO LO VUOI AGGIUNGERE AD ALTRI SERVER
        # await self.tree.sync()
        # print("Slash commands synced.")

    async def close(self):
        # Make sure the aiohttp session's connections are torn down
        # cleanly on shutdown instead of leaking sockets.
        if self.http_session:
            await self.http_session.close()
        await super().close()


bot = MyBot()


@bot.event
async def on_ready():
    print(f"Logged on as {bot.user}.")


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    """
    Central error handler for every slash command. A `@require_role()`
    failure lands here automatically as CheckFailure — this is what lets
    every command skip writing its own "you don't have permission" logic.
    """
    if isinstance(error, app_commands.CheckFailure):
        embed = make_error_embed("Permission Denied", "You do not have permission to use this command.")
    else:
        print(f"Unhandled app command error: {error}")
        embed = make_error_embed("Something Went Wrong", "An unexpected error occurred while processing the command.")

    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ============================================================
# COMMAND TEMPLATE — copy this pattern for any new command
# ============================================================
# @bot.tree.command(name="template", description="What this command does.")
# @app_commands.describe(some_field="Explain this parameter to the user.")
# @require_role()                       # remove this line for a public/read-only command
# async def template(interaction: discord.Interaction, some_field: str):
#
#     # 1) VALIDATE input synchronously, before touching the network or the
#     #    lock. Bad input should never cost an API call.
#     some_field = some_field.strip()
#     if not some_field:
#         await interaction.response.send_message(
#             embed=make_error_embed("Invalid Input", "Explain what's wrong."),
#             ephemeral=True,
#         )
#         return
#
#     # 2) If this command WRITES to the sheet, reject immediately when the
#     #    bot is already mid-command instead of silently queueing up —
#     #    this is what guarantees one command fully finishes before the
#     #    next one can touch the sheet. Read-only commands can skip this.
#     if command_lock.locked():
#         await interaction.response.send_message(embed=make_busy_embed(), ephemeral=True)
#         return
#
#     # 3) Do the actual work under the lock.
#     async with command_lock:
#         await interaction.response.defer()  # Sheets calls can take > 3s
#         try:
#             list_of_lists = await get_sheet_data()
#             username_index = build_username_index(list_of_lists)
#
#             # ... business logic here, queueing Cell(...) into cell_list ...
#
#             if cell_list:
#                 await update_cells(cell_list)
#
#             embed = make_embed(
#                 interaction,
#                 title="Template Result",
#                 color=EMBED_COLOR_SUCCESS,
#                 fields=[("Field Name", "Field Value", True)],
#                 verb="Updated",
#             )
#             await interaction.followup.send(embed=embed)
#         except Exception as e:
#             print(f"Error in template: {e}")
#             await interaction.followup.send(
#                 embed=make_error_embed("Something Went Wrong", "An error occurred while processing the command.")
#             )


# ============================================================
# COMMANDS
# ============================================================

@bot.tree.command(name="test", description="Test command.")
async def test(interaction: discord.Interaction):
    await interaction.response.send_message(
        embed=make_embed(interaction, title="It works!", color=EMBED_COLOR_SUCCESS)
    )


# ------------------------------------------------------------
# /register
# ------------------------------------------------------------
@bot.tree.command(name="register", description="Register a new recruit in the battalion sheet.")
@app_commands.describe(
    roblox_username="The recruit's Roblox username.",
    ct_number="The recruit's 4-digit CT number (e.g. 5142).",
    nickname="The recruit's nickname (e.g. Henry).",
    timezone="The recruit's timezone (e.g. CEST, or N/A if none).",
)
@require_role()
async def register(
    interaction: discord.Interaction,
    roblox_username: str,
    ct_number: app_commands.Range[str, 4, 4],
    nickname: str,
    timezone: str,
):
    # 1) Validate
    roblox_username = roblox_username.strip()
    ct_number = ct_number.strip()
    nickname = nickname.strip()
    timezone = timezone.strip()

    if not roblox_username:
        await interaction.response.send_message(
            embed=make_error_embed("Invalid Username", "You must provide a Roblox username."), ephemeral=True
        )
        return
    if not ct_number.isdigit():
        await interaction.response.send_message(
            embed=make_error_embed("Invalid CT Number", "The CT number must be exactly 4 digits, e.g. `5142`."),
            ephemeral=True,
        )
        return
    if not nickname:
        await interaction.response.send_message(
            embed=make_error_embed("Invalid Nickname", "You must provide a nickname."), ephemeral=True
        )
        return
    if not timezone:
        await interaction.response.send_message(
            embed=make_error_embed("Invalid Timezone", "You must provide a timezone."), ephemeral=True
        )
        return

    # 2) Busy check
    if command_lock.locked():
        await interaction.response.send_message(embed=make_busy_embed(), ephemeral=True)
        return

    # 3) Work
    async with command_lock:
        await interaction.response.defer()
        try:
            list_of_lists = await get_sheet_data()
            username_index = build_username_index(list_of_lists)

            if roblox_username.lower() in username_index:
                await interaction.followup.send(
                    embed=make_error_embed(
                        "Already Registered", f"**{roblox_username}** is already in the battalion sheet."
                    )
                )
                return

            empty_row = find_first_empty_row(list_of_lists)
            if empty_row is None:
                await interaction.followup.send(
                    embed=make_error_embed("Sheet Full", "No free rows are available in the Data sheet.")
                )
                return

            designation = f'CT-{ct_number} "{nickname}"'
            join_date = datetime.now().strftime("%d/%m/%Y")
            row_number = empty_row + 1  # back to 1-indexed for gspread

            cell_list.append(Cell(row=row_number, col=COL_USERNAME + 1, value=roblox_username))
            cell_list.append(Cell(row=row_number, col=COL_RANK + 1, value="Private"))
            cell_list.append(Cell(row=row_number, col=COL_DESIGNATION + 1, value=designation))
            cell_list.append(Cell(row=row_number, col=COL_TIMEZONE + 1, value=timezone))
            cell_list.append(Cell(row=row_number, col=COL_TRAINING + 1, value=0))
            cell_list.append(Cell(row=row_number, col=COL_PD + 1, value=0))
            cell_list.append(Cell(row=row_number, col=COL_HOSTED + 1, value=0))
            cell_list.append(Cell(row=row_number, col=COL_JOINED + 1, value=join_date))

            await update_cells(cell_list)

            embed = make_embed(
                interaction,
                title="New Recruit Registered",
                description=f"**{roblox_username}** has been added to the roster.",
                color=EMBED_COLOR_SUCCESS,
                fields=[
                    ("Username", roblox_username, True),
                    ("Rank", "Private", True),
                    ("Designation", designation, True),
                    ("Timezone", timezone, True),
                    ("Joined", join_date, True),
                ],
                verb="Updated",
            )
            await interaction.followup.send(embed=embed)
            print(f"Registered {roblox_username} at row {row_number}.")

        except Exception as e:
            print(f"Error in register: {e}")
            await interaction.followup.send(
                embed=make_error_embed("Something Went Wrong", "An error occurred while processing the command.")
            )


# ------------------------------------------------------------
# /orbat  (read-only, open to everyone)
# ------------------------------------------------------------
@bot.tree.command(name="orbat", description="Display a member's ORBAT record.")
@app_commands.describe(roblox_username="The Roblox username to look up.")
async def orbat(interaction: discord.Interaction, roblox_username: str):
    roblox_username = roblox_username.strip()
    if not roblox_username:
        await interaction.response.send_message(
            embed=make_error_embed("Invalid Username", "You must provide a Roblox username."), ephemeral=True
        )
        return

    # Read-only: no lock needed, just defer since the Sheets call can be slow.
    await interaction.response.defer()
    try:
        list_of_lists = await get_sheet_data()
        username_index = build_username_index(list_of_lists)
        row_idx = username_index.get(roblox_username.lower())

        if row_idx is None:
            await interaction.followup.send(
                embed=make_error_embed(
                    "Record Not Found", f"No record found for **{roblox_username}** in the battalion sheet."
                )
            )
            return

        row = list_of_lists[row_idx]
        embed = make_embed(
            interaction,
            title=f"ORBAT Record | {get_cell(row, COL_USERNAME)}",
            color=EMBED_COLOR_SUCCESS,
            fields=[
                ("Username", get_cell(row, COL_USERNAME, "N/A"), True),
                ("Rank", get_cell(row, COL_RANK, "N/A"), True),
                ("Designation", get_cell(row, COL_DESIGNATION, "N/A"), True),
                ("Timezone", get_cell(row, COL_TIMEZONE, "N/A"), True),
                ("Points", get_cell(row, COL_POINTS, "0"), True),
                ("Training", get_cell(row, COL_TRAINING, "0"), True),
                ("PD", get_cell(row, COL_PD, "0"), True),
                ("Hosted", get_cell(row, COL_HOSTED, "0"), True),
                ("Last Event", get_cell(row, COL_LAST_EVENT, "N/A"), True),
                ("Joined", get_cell(row, COL_JOINED, "N/A"), True),
            ],
            verb="Requested",
        )
        await interaction.followup.send(embed=embed)

    except Exception as e:
        print(f"Error in orbat: {e}")
        await interaction.followup.send(
            embed=make_error_embed("Something Went Wrong", "An error occurred while processing the command.")
        )


# ------------------------------------------------------------
# /progress  (read-only, open to everyone)
# ------------------------------------------------------------
@bot.tree.command(name="progress", description="Display a member's promotion progress toward their next rank.")
@app_commands.describe(username="The username to check.")
async def progress(interaction: discord.Interaction, username: str):
    username = username.strip()
    if not username:
        await interaction.response.send_message(
            embed=make_error_embed("Invalid Username", "You must provide a username."), ephemeral=True
        )
        return
 
    await interaction.response.defer()
    try:
        list_of_lists = await get_sheet_data()
        username_index = build_username_index(list_of_lists)
        row_idx = username_index.get(username.lower())
 
        if row_idx is None:
            await interaction.followup.send(
                embed=make_error_embed(
                    "Progress Lookup Failed", f"No record found for **{username}** in the battalion sheet."
                )
            )
            return
 
        row = list_of_lists[row_idx]
        display_name = get_cell(row, COL_USERNAME)
        status = get_promotion_status(row)
 
        if not status["trackable"] and status["rank"] == SHEET_CEILING_RANK:
            # Lance Corporal: points are still tracked, but there's no BE/PD
            # threshold to promote past it — it's a manual leadership call.
            embed = make_embed(
                interaction,
                title=f"Promotion Progress | {display_name}",
                color=EMBED_COLOR_GOLD,
                fields=[
                    ("Current Rank", status["rank"], True),
                    ("Next Rank", "🎖️ NCO Tier (Leadership Decision)", True),
                    ("BE", str(status["be"]), True),
                    ("PD", str(status["pd"]), True),
                    (
                        "Promotion Status",
                        f"🎖️ **{status['rank']} — sheet ceiling reached.**\n"
                        "Further promotions are handled by your superiors and are not "
                        "tracked through the event system.",
                        False,
                    ),
                ],
                verb="Requested",
            )
            await interaction.followup.send(embed=embed)
            return
 
        if not status["trackable"]:
            embed = make_embed(
                interaction,
                title=f"Promotion Progress | {display_name}",
                description=(
                    f"🎖️ **{status['rank']}**\n\n"
                    "This member holds an NCO or Officer rank. Promotions at this "
                    "level are handled by leadership and are not tracked through "
                    "the battalion event system."
                ),
                color=EMBED_COLOR_GOLD,
                fields=[
                    ("Current Rank", status["rank"], True),
                    ("Promotion Tracking", "⭐ Max sheet rank reached — NCO/Officer tier", False),
                ],
                verb="Requested",
            )
            await interaction.followup.send(embed=embed)
            return
 
        fields = [
            ("Current Rank", status["rank"], True),
            ("Next Rank", status["next_rank"], True),
            ("BE", str(status["be"]), True),
            ("PD", str(status["pd"]), True),
            ("Battalion Events (BE)", format_requirement_line(status["be"], status["req_be"], "BE"), False),
            ("Permadeath (PD)", format_requirement_line(status["pd"], status["req_pd"], "PD"), False),
            (
                "Promotion Status",
                "✅ **Eligible for promotion!** Use `/promote` to apply it."
                if status["eligible"]
                else "❌ **Not yet eligible**",
                False,
            ),
        ]
 
        embed = make_embed(
            interaction,
            title=f"Promotion Progress | {display_name}",
            color=EMBED_COLOR_SUCCESS if status["eligible"] else EMBED_COLOR_GOLD,
            fields=fields,
            verb="Requested",
        )
        await interaction.followup.send(embed=embed)
 
    except Exception as e:
        print(f"Error in progress: {e}")
        await interaction.followup.send(
            embed=make_error_embed("Something Went Wrong", "An error occurred while processing the command.")
        )



# ------------------------------------------------------------
# /addpoints
# ------------------------------------------------------------
@bot.tree.command(name="addpoints", description="Add points to one or more users.")
@app_commands.describe(
    points_type="Type of points to add.",
    usernames="Username or usernames separated by commas (e.g. user1,user2,user3).",
    points_amount="Amount of points to add (must be greater than 0).",
)
@app_commands.choices(
    points_type=[
        app_commands.Choice(name="Battalion Event (BE)", value="BE"),
        app_commands.Choice(name="Permadeath (PD)", value="PD"),
        app_commands.Choice(name="Hosted", value="Hosted"),
    ]
)
@require_role()
async def addpoints(
    interaction: discord.Interaction,
    points_type: app_commands.Choice[str],
    usernames: str,
    points_amount: app_commands.Range[int, 1, 100000],
):
    # 1) Validate
    names_list = parse_username_list(usernames)
    if not names_list:
        await interaction.response.send_message(
            embed=make_error_embed("No Usernames Provided", "You must provide at least one username."),
            ephemeral=True,
        )
        return
    if points_amount <= 0:  # belt-and-suspenders alongside the Range[] constraint above
        await interaction.response.send_message(
            embed=make_error_embed("Invalid Points Amount", "Points amount must be greater than 0."),
            ephemeral=True,
        )
        return

    # 2) Busy check
    if command_lock.locked():
        await interaction.response.send_message(embed=make_busy_embed(), ephemeral=True)
        return

    # 3) Work
    async with command_lock:
        await interaction.response.defer()
        try:
            list_of_lists = await get_sheet_data()
            username_index = build_username_index(list_of_lists)
            column = POINTS_TYPE_COLUMNS[points_type.value]

            updated, not_found = apply_points(list_of_lists, username_index, names_list, points_amount, column)

            if cell_list:
                await update_cells(cell_list)

            updated_text = "\n".join(f"{name} {old} → **{new}**" for name, old, new in updated) or "None"
            fields = [(f"Updated ({len(updated)})", updated_text, False)]
            if not_found:
                fields.append((f"Not Found ({len(not_found)})", "\n".join(not_found), False))

            embed = make_embed(
                interaction,
                title=f"Points Updated — {points_type.name}",
                description=f"Amount: **+{points_amount}**",
                color=EMBED_COLOR_SUCCESS,
                fields=fields,
                verb="Updated",
            )
            await interaction.followup.send(embed=embed)
            print("Addpoints completed successfully.")

        except Exception as e:
            print(f"Error in addpoints: {e}")
            await interaction.followup.send(
                embed=make_error_embed("Something Went Wrong", "An error occurred while processing the command.")
            )


# ------------------------------------------------------------
# /promote
# ------------------------------------------------------------
@bot.tree.command(name="promote", description="Promote one or more users if they meet the requirements.")
@app_commands.describe(usernames="Username or usernames separated by commas.")
@require_role()
async def promote(interaction: discord.Interaction, usernames: str):
    # 1) Validate
    names_list = parse_username_list(usernames)
    if not names_list:
        await interaction.response.send_message(
            embed=make_error_embed("No Usernames Provided", "You must provide at least one username."),
            ephemeral=True,
        )
        return

    # 2) Busy check
    if command_lock.locked():
        await interaction.response.send_message(embed=make_busy_embed(), ephemeral=True)
        return

    # 3) Work
    async with command_lock:
        await interaction.response.defer()
        try:
            list_of_lists = await get_sheet_data()
            username_index = build_username_index(list_of_lists)

            result = apply_promotions(list_of_lists, username_index, names_list)

            fields = []
            promoted_text = "\n".join(f"{n} : {old} → {new}" for n, old, new in result["promoted"]) or "None"
            fields.append(("Promoted", promoted_text, False))

            if result["not_eligible"]:
                lines = [
                    f"{n} : {s['rank']} ({s['be']}/{s['req_be']} BE, {s['pd']}/{s['req_pd']} PD)"
                    for n, s in result["not_eligible"]
                ]
                fields.append(("Not Yet Eligible", "\n".join(lines), False))

            if result["not_trackable"]:
                lines = [f"{n} : {rank}" for n, rank in result["not_trackable"]]
                fields.append(("NCO/Officer Tier (Not Trackable)", "\n".join(lines), False))

            if result["not_found"]:
                fields.append((f"Not Found ({len(result['not_found'])})", "\n".join(result["not_found"]), False))

            # Only write + re-sort if something actually changed.
            if cell_list:
                await update_cells(cell_list)
                try:
                    await asyncio.to_thread(sort_users)
                except Exception as e:
                    print(f"WARNING: Promotion(s) succeeded but sorting failed: {e}")

            embed = make_embed(
                interaction,
                title="Promotion Results",
                color=EMBED_COLOR_SUCCESS if result["promoted"] else EMBED_COLOR_GOLD,
                fields=fields,
                verb="Updated",
            )
            await interaction.followup.send(embed=embed)
            print("Promotion completed successfully.")

        except Exception as e:
            print(f"Error in promote: {e}")
            await interaction.followup.send(
                embed=make_error_embed("Something Went Wrong", "An error occurred while processing the command.")
            )

# ------------------------------------------------------------
# /battalion stats   (read-only, open to everyone)
# ------------------------------------------------------------
# Implemented as a command GROUP (name="battalion") rather than a flat
# command so it shows up as "/battalion stats" in Discord, matching the
# reference screenshot — and so any future battalion-wide info command
# (e.g. a hypothetical "/battalion roster") can be added as another
# subcommand here without cluttering the top-level command list.
battalion_group = app_commands.Group(name="battalion", description="Battalion-wide info commands.")
 
 
@battalion_group.command(name="stats", description="Display battalion-wide statistics.")
async def battalion_stats(interaction: discord.Interaction):
    # Read-only, single sheet read, no lock needed — safe to run alongside
    # /addpoints, /promote, etc.
    await interaction.response.defer()
    try:
        list_of_lists = await get_sheet_data()
        start = DATA_START_ROW - 1
        end = min(DATA_END_ROW, len(list_of_lists))
 
        rank_counts = {rank: 0 for rank in DISPLAY_RANK_ORDER}  # preserves DISPLAY_RANK_ORDER's order
        total_members = 0
        pending_promotion = 0
        total_be = total_pd = total_hosted = 0
 
        for i in range(start, end):
            row = list_of_lists[i]
            if not get_cell(row, COL_USERNAME):
                continue  # empty row = no player here
 
            total_members += 1
            rank = get_cell(row, COL_RANK)
            if rank in rank_counts:
                rank_counts[rank] += 1
            # Cadets are counted in total_members but excluded from the
            # breakdown (rank_counts only has DISPLAY_RANK_ORDER's ranks),
            # per the spec.
 
            total_be += safe_int(get_cell(row, COL_TRAINING, "0"))
            total_pd += safe_int(get_cell(row, COL_PD, "0"))
            total_hosted += safe_int(get_cell(row, COL_HOSTED, "0"))
 
            # Reuses the exact same eligibility logic /progress and /promote
            # use, so this number can never drift out of sync with them.
            status = get_promotion_status(row)
            if status["trackable"] and status["eligible"]:
                pending_promotion += 1
 
        breakdown_text = "\n".join(f"**{rank}**: {count}" for rank, count in rank_counts.items())
 
        # Two additions beyond the reference screenshot, both computed for
        # free from data already being read for the rank breakdown:
        #   - "Pending Promotions": how many members are eligible RIGHT NOW
        #     but haven't been run through /promote yet — useful at-a-glance
        #     for leadership to know if /promote needs to be run.
        #   - "Points Tracked": total BE/PD/Hosted across the whole
        #     battalion — a rough gauge of overall event participation.
        fields = [
            ("Total Members", str(total_members), True),
            ("Pending Promotions", str(pending_promotion), True),
            ("Rank Breakdown", breakdown_text, False),
            ("Points Tracked (BE / PD / Hosted)", f"{total_be} / {total_pd} / {total_hosted}", False),
        ]
 
        embed = make_embed(
            interaction,
            title="442nd Battalion — Statistics",
            color=EMBED_COLOR_INFO,
            fields=fields,
            verb="Requested",
        )
        await interaction.followup.send(embed=embed)
 
    except Exception as e:
        print(f"Error in battalion stats: {e}")
        await interaction.followup.send(
            embed=make_error_embed("Something Went Wrong", "An error occurred while processing the command.")
        )
 
 
bot.tree.add_command(battalion_group)
 
 
# ------------------------------------------------------------
# /bgcheck
# ------------------------------------------------------------
@bot.tree.command(name="bgcheck", description="Run a background check on a Roblox user.")
@app_commands.describe(roblox_username="The Roblox username to look up.")
async def bgcheck(interaction: discord.Interaction, roblox_username: str):
    # 1) Validate
    roblox_username = roblox_username.strip()
    if not roblox_username:
        await interaction.response.send_message(
            embed=make_error_embed("Invalid Username", "You must provide a Roblox username."), ephemeral=True
        )
        return
 
    # Read-only and doesn't touch the sheet at all (Roblox API only), so no
    # command_lock needed — this can safely run alongside /addpoints, etc.
    await interaction.response.defer()
    try:
        roblox_id, canonical_name = await roblox_get_user_id(bot.http_session, roblox_username)
        if roblox_id is None:
            await interaction.followup.send(
                embed=make_error_embed(
                    "Roblox User Not Found", f"No Roblox account exists for the username **{roblox_username}**."
                )
            )
            return
 
        profile = await fetch_roblox_profile(bot.http_session, roblox_id, set(ROBLOX_GROUPS.values()))
        profile["name"] = canonical_name  # exact-case username from the lookup, not whatever casing was typed
        profile["verified_discord"] = await get_verified_discord_status(bot.http_session, roblox_id)
 
        embed = make_bgcheck_embed(interaction, roblox_id, profile)
        await interaction.followup.send(embed=embed)
 
    except Exception as e:
        print(f"Error in bgcheck: {e}")
        await interaction.followup.send(
            embed=make_error_embed("Something Went Wrong", "An error occurred while processing the command.")
        )
 
 
# ------------------------------------------------------------
# /req   (read-only, open to everyone, no sheet or API calls at all)
# ------------------------------------------------------------
@bot.tree.command(name="req", description="Display promotion point requirements for every rank.")
async def req(interaction: discord.Interaction):
    try:
        # Pulled straight from POINT_PROMOTION_REQUIREMENTS + ranks, so this
        # can never drift out of sync with what /progress and /promote
        # actually enforce — change the numbers in ONE place (the config
        # section near the top of the file) and every command updates together.
        lines = ["**Private**\n└ Graduate from the Cadet Academy\n"]
        for rank, requirement in POINT_PROMOTION_REQUIREMENTS.items():
            try:
                next_rank = ranks[ranks.index(rank) + 1]
            except (ValueError, IndexError):
                continue
            lines.append(
                f"**{rank} → {next_rank}**\n└ BE: **{requirement['be']}** | PD: **{requirement['pd']}**\n"
            )
 
        embed = make_embed(
            interaction,
            title="442nd Battalion — Promotion Requirements",
            description="\n".join(lines),
            color=EMBED_COLOR_INFO,
            verb="Requested",
        )
        await interaction.response.send_message(embed=embed)
 
    except Exception as e:
        print(f"Error in req: {e}")
        await interaction.response.send_message(
            embed=make_error_embed("Something Went Wrong", "An error occurred while processing the command.")
        )

# ============================================================
# EVENT SYSTEM  (/event)
# ============================================================
# Everything below builds the /event command: a DM-driven setup wizard
# that ends with an embed posted in a channel, which people then RSVP to
# either with reactions (general events, one emoji per company) or
# buttons (company events: Accepted / Declined / Tentative). Config for
# this lives in EVENT SYSTEM CONFIG near the top of the file.
#
# Layout of this section:
#   A. In-memory state + JSON persistence (survives bot restarts)
#   B. Small utilities (emoji handling, sentinels)
#   C. Input validators (title, description, UTC offset, date/time, URL, channel)
#   D. DM wizard views (buttons/selects) + the generic text-prompt helper
#   E. Embed builder (shared by the preview AND the live posted embed)
#   F. Posting a confirmed event + the wizard orchestrator
#   G. The /event command itself
#   H. Persistent RSVP button view (company events)
#   I. Reaction handling (general events)
#   J. 20-minutes-out reminder thread + ping
 
 
# ----- A. State + persistence --------------------------------------------
 
active_events = {}      # {message_id: event_dict} — the live source of truth, mirrored to disk on every change
reminder_tasks = {}     # {message_id: asyncio.Task} — the scheduled 20-min-out reminder for each event
active_setups = set()   # user_ids currently mid-wizard in their DMs, so a second /event can't collide with it
_pending_refresh_tasks = {}  # {message_id: asyncio.Task} — debounces reaction bursts (see request_event_refresh)
 
 
def _write_events_file(data):
    with open(EVENTS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
 
 
async def save_events():
    """Persists active_events to disk (in a background thread, since file
    I/O is blocking) so events and their RSVP lists survive a restart."""
    try:
        data = {str(message_id): ev for message_id, ev in active_events.items()}
        await asyncio.to_thread(_write_events_file, data)
    except OSError as e:
        print(f"Failed to save events to {EVENTS_FILE}: {e}")
 
 
def load_events():
    """Loads persisted events back into memory at startup. A missing or
    corrupt file just means 'no events yet', not a crash."""
    if not os.path.exists(EVENTS_FILE):
        return {}
    try:
        with open(EVENTS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return {int(message_id): ev for message_id, ev in raw.items()}
    except (json.JSONDecodeError, ValueError, OSError) as e:
        print(f"Could not load {EVENTS_FILE}, starting with no tracked events: {e}")
        return {}
 
 
# ----- B. Small utilities --------------------------------------------------
 
EVENT_CANCELLED = object()  # sentinel: user typed "cancel" during a text prompt
EVENT_SKIPPED = object()    # sentinel: user typed "skip" on an optional text prompt
 
 
class EventSetupCancelled(Exception):
    """Raised internally the moment the /event wizard should stop (Cancel
    pressed, 'cancel' typed, or a step timed out) — caught once at the top
    of run_event_setup so every step below can just `raise` and let the
    outer handler send the one cancellation/timeout message, instead of
    each step repeating its own return-and-cleanup logic."""
    pass
 
 
def _as_emoji(emoji_str):
    """Accepts either a plain unicode emoji or a custom emoji string like
    '<:name:id>' / '<a:name:id>' (paste it straight from Discord) and
    returns whatever discord.py's add_reaction()/SelectOption() expect."""
    if emoji_str.startswith("<"):
        return discord.PartialEmoji.from_str(emoji_str)
    return emoji_str
 
 
# ----- C. Input validators --------------------------------------------------
# Each validator takes the raw text the user typed and returns
# (True, parsed_value) on success or (False, error_message) to re-prompt.
 
def validate_title(text):
    if not text:
        return False, "The title can't be empty."
    if len(text) > 100:
        return False, f"Titles must be 100 characters or fewer (yours is {len(text)})."
    return True, text
 
 
def validate_description(text):
    if len(text) > EVENT_DESCRIPTION_CHAR_LIMIT:
        return False, f"Descriptions must be {EVENT_DESCRIPTION_CHAR_LIMIT} characters or fewer (yours is {len(text)})."
    return True, text
 
 
UTC_OFFSET_PATTERN = re.compile(r"^(?:UTC|GMT)?\s*([+-]?\d{1,2})(?::?(\d{2}))?$", re.IGNORECASE)
 
 
def validate_utc_offset(text):
    text = text.strip()
    if text.upper() in ("UTC", "GMT"):
        return True, 0.0
    match = UTC_OFFSET_PATTERN.match(text)
    if not match:
        return False, "Give your UTC offset like `+2`, `-5`, `+5:30`, or `UTC+1`."
    hours = int(match.group(1))
    minutes = int(match.group(2)) if match.group(2) else 0
    offset = hours + (minutes / 60 if hours >= 0 else -minutes / 60)
    if not -14 <= offset <= 14:
        return False, "That offset is out of range — it must be between -14 and +14."
    return True, offset
 
 
def validate_datetime(text, utc_offset, after_epoch=None):
    text = text.strip()
    parsed = None
    for fmt in DATETIME_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        return False, "I couldn't read that date/time. Use one of the formats shown above."
 
    # The user typed a LOCAL time at their given UTC offset. Treating the
    # naive value as if it were already UTC and then subtracting the
    # offset converts it into a true UTC instant, so the <t:...> timestamp
    # Discord renders is correct for every viewer regardless of their own
    # timezone.
    utc_dt = parsed.replace(tzinfo=timezone.utc) - timedelta(hours=utc_offset)
    epoch = int(utc_dt.timestamp())
    now_epoch = int(datetime.now(timezone.utc).timestamp())
 
    if epoch <= now_epoch:
        return False, "That time is in the past — give a time in the future."
    if after_epoch is not None and epoch <= after_epoch:
        return False, "The end time must be after the start time."
    return True, epoch
 
 
URL_PATTERN = re.compile(r"^https?://\S+\.\S+$", re.IGNORECASE)
 
 
def validate_url(text):
    text = text.strip()
    if not URL_PATTERN.match(text):
        return False, "That doesn't look like a valid URL — it should start with `http://` or `https://`."
    return True, text
 
 
def resolve_channel(guild, text, needs_reactions):
    """Resolves free text (a mention, an ID, or a name) to a postable
    TextChannel in this guild, and checks the bot actually has the
    permissions the event type will need there."""
    text = text.strip()
    channel = None
 
    mention_match = re.match(r"^<#(\d+)>$", text)
    id_match = re.match(r"^(\d{15,25})$", text)
    if mention_match:
        channel = guild.get_channel(int(mention_match.group(1)))
    elif id_match:
        channel = guild.get_channel(int(id_match.group(1)))
    else:
        name = text.lstrip("#").lower()
        channel = discord.utils.find(lambda c: c.name.lower() == name, guild.text_channels)
 
    if channel is None or not isinstance(channel, discord.TextChannel):
        return False, "I couldn't find that channel in this server. Mention it (`#channel`), or give its exact name or ID."
 
    perms = channel.permissions_for(guild.me)
    missing = []
    if not perms.send_messages:
        missing.append("Send Messages")
    if not perms.embed_links:
        missing.append("Embed Links")
    if needs_reactions and not perms.add_reactions:
        missing.append("Add Reactions")
    if missing:
        return False, f"I don't have **{', '.join(missing)}** permission in {channel.mention}. Pick a different channel."
    return True, channel
 
 
# ----- D. DM wizard views + generic text-prompt helper ---------------------
 
class EventCancelView(discord.ui.View):
    """Shared base for every view used in the /event wizard. Restricts
    interaction to whoever is actually running the wizard and gives every
    step a consistent Cancel button + timeout behavior, instead of
    repeating both bits of logic in every subclass."""
 
    def __init__(self, author_id, timeout=300):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.value = None      # set by whichever button/select the user picks
        self.message = None    # set by send_view() right after sending
 
    async def interaction_check(self, interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This isn't your event setup.", ephemeral=True)
            return False
        return True
 
    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass  # message may have been deleted — nothing to do
 
    async def _finish(self, interaction, value):
        self.value = value
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()
 
    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, row=1)
    async def cancel_btn(self, interaction, button):
        await self._finish(interaction, "cancel")
 
 
async def send_view(dm_channel, content, view):
    """Sends a message carrying one of the wizard's views and remembers
    the resulting Message on the view (so on_timeout can grey the buttons
    out in place instead of leaving dead controls sitting there)."""
    message = await dm_channel.send(content, view=view)
    view.message = message
    return message
 
 
async def require_view_value(view, dm_channel):
    """Waits for the view to be used, then either returns the chosen
    value or raises EventSetupCancelled (after sending the reason) if the
    user hit Cancel or the view timed out."""
    await view.wait()
    if view.value == "cancel":
        await dm_channel.send(embed=make_error_embed("Event Creation Cancelled", "No event was created."))
        raise EventSetupCancelled()
    if view.value is None:
        await dm_channel.send(embed=make_error_embed(
            "Setup Timed Out", "You didn't respond in time, so the event setup was cancelled."
        ))
        raise EventSetupCancelled()
    return view.value
 
 
async def prompt_for_text(dm_channel, author_id, prompt_text, *, validator, allow_skip=False, timeout=300):
    """Sends `prompt_text` in the DM and waits for the next message from
    `author_id`, re-prompting on invalid input (via `validator`) until it
    succeeds. Raises EventSetupCancelled on 'cancel' or a timeout; returns
    EVENT_SKIPPED if `allow_skip` and the user types 'skip'."""
 
    def check(m):
        return m.author.id == author_id and m.channel.id == dm_channel.id
 
    hint = "\n\n*Type `cancel` to stop this step"
    hint += ", or `skip` to leave it blank.*" if allow_skip else ".*"
    await dm_channel.send(prompt_text + hint)
 
    while True:
        try:
            msg = await bot.wait_for("message", check=check, timeout=timeout)
        except asyncio.TimeoutError:
            await dm_channel.send(embed=make_error_embed(
                "Setup Timed Out", "You took too long to respond, so the event setup was cancelled."
            ))
            raise EventSetupCancelled()
 
        content = msg.content.strip()
        if content.lower() == "cancel":
            await dm_channel.send(embed=make_error_embed("Event Creation Cancelled", "No event was created."))
            raise EventSetupCancelled()
        if allow_skip and content.lower() == "skip":
            return EVENT_SKIPPED
 
        ok, result = validator(content)
        if ok:
            return result
        await dm_channel.send(embed=make_error_embed("Invalid Input", result))
        # loop back and wait for another message under the same prompt
 
 
class EventTypeView(EventCancelView):
    @discord.ui.button(label="🌐 General Event", style=discord.ButtonStyle.primary, row=0)
    async def general_btn(self, interaction, button):
        await self._finish(interaction, EVENT_TYPE_GENERAL)
 
    @discord.ui.button(label="🎖️ Company Event", style=discord.ButtonStyle.primary, row=0)
    async def company_btn(self, interaction, button):
        await self._finish(interaction, EVENT_TYPE_COMPANY)
 
 
class CompanySelectView(EventCancelView):
    def __init__(self, author_id):
        super().__init__(author_id)
        options = [
            discord.SelectOption(label=info["label"], value=key, emoji=_as_emoji(info["emoji"]))
            for key, info in BATTALION_COMPANIES.items()
            if key != "guests"  # "Guests" isn't a company you can host a company-specific event for
        ]
        self.select = discord.ui.Select(placeholder="Choose a company...", options=options, row=0)
        self.select.callback = self._on_select
        self.add_item(self.select)
 
    async def _on_select(self, interaction):
        await self._finish(interaction, self.select.values[0])
 
 
class ChannelChoiceView(EventCancelView):
    def __init__(self, author_id, origin_channel):
        super().__init__(author_id)
        here_label = f"Post here (#{origin_channel.name})"[:80]
        here_btn = discord.ui.Button(label=here_label, style=discord.ButtonStyle.primary, row=0)
        here_btn.callback = self._on_here
        self.add_item(here_btn)
 
        choose_btn = discord.ui.Button(label="Choose a different channel", style=discord.ButtonStyle.secondary, row=0)
        choose_btn.callback = self._on_choose
        self.add_item(choose_btn)
 
    async def _on_here(self, interaction):
        await self._finish(interaction, "here")
 
    async def _on_choose(self, interaction):
        await self._finish(interaction, "choose")
 
 
class ConfirmView(EventCancelView):
    @discord.ui.button(label="✅ Confirm & Post", style=discord.ButtonStyle.success, row=0)
    async def confirm_btn(self, interaction, button):
        await self._finish(interaction, "confirm")
 
 
# ----- E. Embed builder -----------------------------------------------------
 
def build_event_embed(event):
    """Builds the event embed from an event dict — used for BOTH the DM
    preview and the live posted message (edited in place as people
    RSVP), so the two can never drift apart."""
    embed = discord.Embed(
        title=event["title"],
        description=truncate_field(event["description"], limit=EVENT_DESCRIPTION_CHAR_LIMIT) if event.get("description") else None,
        color=EMBED_COLOR_EVENT,
    )
 
    time_value = f"<t:{event['start_epoch']}:F>\n<t:{event['start_epoch']}:R>"
    embed.add_field(name="🗓️ Time", value=time_value, inline=False)
    if event.get("end_epoch"):
        end_value = f"<t:{event['end_epoch']}:F>\n<t:{event['end_epoch']}:R>"
        embed.add_field(name="🏁 Ends", value=end_value, inline=False)
 
    if event["type"] == EVENT_TYPE_GENERAL:
        total = 0
        for key, info in BATTALION_COMPANIES.items():
            members = event["categories"].get(key, [])
            names = [event["display_names"].get(str(uid), f"<@{uid}>") for uid in members]
            embed.add_field(
                name=f"{info['emoji']} {info['label']} ({len(members)})",
                value=truncate_field("\n".join(names), limit=EVENT_FIELD_CHAR_LIMIT) if names else "—",
                inline=True,
            )
            total += len(members)
        footer_text = f"{FOOTER_BRAND} • Created by {event['creator_name']} • {total} attending"
    else:
        company_info = BATTALION_COMPANIES.get(event["company"], {"label": event.get("company") or "Unknown", "emoji": ""})
        embed.set_author(name=f"{company_info['emoji']} {company_info['label']} Event".strip())
        for key, label in (("accepted", "✅ Accepted"), ("declined", "❌ Declined"), ("tentative", "❓ Tentative")):
            members = event["categories"].get(key, [])
            names = [event["display_names"].get(str(uid), f"<@{uid}>") for uid in members]
            embed.add_field(
                name=f"{label} ({len(members)})",
                value=truncate_field("\n".join(names), limit=EVENT_FIELD_CHAR_LIMIT) if names else "—",
                inline=True,
            )
        accepted_count = len(event["categories"].get("accepted", []))
        footer_text = f"{FOOTER_BRAND} • Created by {event['creator_name']} • {accepted_count} accepted"
 
    thumbnail = event.get("thumbnail_url")
    if not thumbnail and event["type"] == EVENT_TYPE_COMPANY:
        thumbnail = BATTALION_COMPANIES.get(event["company"], {}).get("logo_url")
    if thumbnail:
        embed.set_thumbnail(url=thumbnail)
    if event.get("banner_url"):
        embed.set_image(url=event["banner_url"])
 
    embed.set_footer(text=footer_text)
    embed.timestamp = datetime.fromtimestamp(event["created_epoch"], tz=timezone.utc)
    return embed
 
 
# ----- F. Posting a confirmed event + the wizard orchestrator --------------
 
async def post_event(dm_channel, guild, target_channel, event):
    """Posts the finished event embed to its target channel, wires up
    reactions (general) or the persistent RSVP view (company), starts
    tracking it in memory + on disk, and schedules its reminder."""
    perms = target_channel.permissions_for(guild.me)
    missing = []
    if not perms.send_messages:
        missing.append("Send Messages")
    if not perms.embed_links:
        missing.append("Embed Links")
    if event["type"] == EVENT_TYPE_GENERAL and not perms.add_reactions:
        missing.append("Add Reactions")
    if not perms.create_public_threads:
        missing.append("Create Public Threads")  # needed later, for the 20-minute reminder thread
    if missing:
        await dm_channel.send(embed=make_error_embed(
            "Missing Permissions",
            f"I'm missing these permissions in {target_channel.mention}: **{', '.join(missing)}**. "
            "Fix them and run `/event` again — nothing was posted.",
        ))
        return
 
    embed = build_event_embed(event)
    view = EventRSVPView() if event["type"] == EVENT_TYPE_COMPANY else None
 
    try:
        message = await target_channel.send(embed=embed, view=view)
    except discord.HTTPException as e:
        await dm_channel.send(embed=make_error_embed("Failed To Post", f"Discord rejected the event message: {e}"))
        return
 
    event["message_id"] = message.id
    active_events[message.id] = event
 
    if event["type"] == EVENT_TYPE_GENERAL:
        for info in BATTALION_COMPANIES.values():
            try:
                await message.add_reaction(_as_emoji(info["emoji"]))
            except discord.HTTPException as e:
                # One bad/duplicate emoji shouldn't take the whole event down —
                # the rest still get added; fix the emoji in BATTALION_COMPANIES
                # and re-add it manually if this happens.
                print(f"Could not add reaction {info['emoji']} to event {message.id}: {e}")
 
    await save_events()
    schedule_event_reminder(event)
 
    success_embed = discord.Embed(
        title="✅ Event Posted!",
        description=f"Your event is live in {target_channel.mention}.",
        color=EMBED_COLOR_SUCCESS,
    )
    success_embed.set_footer(text=FOOTER_BRAND)
    await dm_channel.send(embed=success_embed)
 
 
async def run_event_setup(user, guild, origin_channel, dm_channel):
    """Runs the full multi-step /event wizard entirely inside the user's
    DMs. Every step can be abandoned early — Cancel button, typing
    'cancel', or simply not responding (timeout) — and all three paths
    raise EventSetupCancelled, caught once here so the wizard always
    stops cleanly with no half-created event left behind."""
    try:
        # 1) General or company-specific event?
        type_view = EventTypeView(author_id=user.id)
        await send_view(dm_channel, "**What kind of event is this?**", type_view)
        event_type = await require_view_value(type_view, dm_channel)
 
        # 2) If company-specific, which company?
        company_key = None
        if event_type == EVENT_TYPE_COMPANY:
            company_view = CompanySelectView(author_id=user.id)
            await send_view(dm_channel, "**Which company is this event for?**", company_view)
            company_key = await require_view_value(company_view, dm_channel)
 
        # 3) Which channel should it be posted in?
        channel_view = ChannelChoiceView(author_id=user.id, origin_channel=origin_channel)
        await send_view(dm_channel, "**Where should this event be posted?**", channel_view)
        channel_choice = await require_view_value(channel_view, dm_channel)
 
        if channel_choice == "here":
            target_channel = origin_channel
        else:
            target_channel = await prompt_for_text(
                dm_channel, user.id,
                "Which channel should I post it in? Mention it (`#channel`), or give its exact name or ID.",
                validator=lambda text: resolve_channel(guild, text, needs_reactions=(event_type == EVENT_TYPE_GENERAL)),
            )
 
        # 4) Title (mandatory, max 100 chars)
        title = await prompt_for_text(dm_channel, user.id, "**Event title?** (max 100 characters)", validator=validate_title)
 
        # 5) Description (optional)
        description = await prompt_for_text(dm_channel, user.id, "**Event description?**", validator=validate_description, allow_skip=True)
        if description is EVENT_SKIPPED:
            description = None
 
        # 6) UTC offset (mandatory — needed to interpret the times below)
        offset = await prompt_for_text(
            dm_channel, user.id,
            "**What's your UTC offset?** (e.g. `+2`, `-5`, `+5:30`, `UTC`)",
            validator=validate_utc_offset,
        )
 
        # 7) Start time (mandatory)
        format_list = "\n".join(f"• `{ex}`" for ex in DATETIME_FORMAT_EXAMPLES)
        start_epoch = await prompt_for_text(
            dm_channel, user.id,
            f"**When does it start?** (your local time — I'll account for your UTC{offset:+g} offset)\n{format_list}",
            validator=lambda text: validate_datetime(text, offset),
        )
 
        # 8) End time (optional, must be after the start time)
        end_epoch = await prompt_for_text(
            dm_channel, user.id,
            f"**When does it end?** (same format, optional)\n{format_list}",
            validator=lambda text: validate_datetime(text, offset, after_epoch=start_epoch),
            allow_skip=True,
        )
        if end_epoch is EVENT_SKIPPED:
            end_epoch = None
 
        # 9) Banner image (optional — large image at the bottom of the embed)
        banner_url = await prompt_for_text(
            dm_channel, user.id, "**Banner image URL?** (large image, shown at the bottom — optional)",
            validator=validate_url, allow_skip=True,
        )
        if banner_url is EVENT_SKIPPED:
            banner_url = None
 
        # 10) Thumbnail image (optional — small image, top-right of the embed)
        thumbnail_url = await prompt_for_text(
            dm_channel, user.id, "**Thumbnail image URL?** (small image, top-right — optional)",
            validator=validate_url, allow_skip=True,
        )
        if thumbnail_url is EVENT_SKIPPED:
            thumbnail_url = None
 
        member = guild.get_member(user.id)
        creator_name = member.display_name if member else user.name
 
        event = {
            "type": event_type,
            "company": company_key,
            "title": title,
            "description": description,
            "start_epoch": start_epoch,
            "end_epoch": end_epoch,
            "banner_url": banner_url,
            "thumbnail_url": thumbnail_url,
            "creator_id": user.id,
            "creator_name": creator_name,
            "created_epoch": int(datetime.now(timezone.utc).timestamp()),
            "categories": {},        # filled in as people RSVP
            "display_names": {},     # {user_id_str: display_name}, cached so the embed never needs a live API call to render
            "channel_id": target_channel.id,
            "guild_id": guild.id,
            "reminder_sent": False,
            # Snapshotting the emoji map onto the event itself (rather than
            # always reading BATTALION_COMPANIES live) means an admin can
            # freely edit the config for FUTURE events without breaking
            # reaction handling on ones already posted.
            "emoji_map": {info["emoji"]: key for key, info in BATTALION_COMPANIES.items()} if event_type == EVENT_TYPE_GENERAL else {},
        }
 
        # 11) Preview + confirm
        preview_embed = build_event_embed(event)
        await dm_channel.send(f"**Preview** — this is exactly what will be posted in {target_channel.mention}:", embed=preview_embed)
        confirm_view = ConfirmView(author_id=user.id)
        await send_view(dm_channel, "Post it?", confirm_view)
        await require_view_value(confirm_view, dm_channel)  # only "confirm" reaches this line without raising
 
        # 12) Post it
        await post_event(dm_channel, guild, target_channel, event)
 
    except EventSetupCancelled:
        return
    except discord.Forbidden:
        await dm_channel.send(embed=make_error_embed(
            "Missing Permissions", "I ran into a Discord permissions error and had to stop the setup."
        ))
    except Exception as e:
        print(f"Error in /event setup for {user}: {e}")
        try:
            await dm_channel.send(embed=make_error_embed(
                "Something Went Wrong", "An unexpected error occurred and the setup was cancelled. Please try again."
            ))
        except discord.Forbidden:
            pass  # they closed their DMs mid-setup — nothing more we can do
 
 
# ----- G. The /event command -------------------------------------------------
 
@bot.tree.command(name="event", description="Create an event for members to RSVP to. Setup happens in your DMs.")
@require_role()
async def event_command(interaction: discord.Interaction):
    # Only one setup wizard per user at a time — otherwise two /event runs
    # would both be waiting on messages in the same DM channel and very
    # likely collide, each thinking the other's answers were its own.
    if interaction.user.id in active_setups:
        await interaction.response.send_message(
            embed=make_error_embed(
                "Setup Already In Progress",
                "You already have an event setup running in your DMs. Finish or cancel that one first.",
            ),
            ephemeral=True,
        )
        return
 
    # Remember exactly where the command was called from BEFORE jumping
    # into DMs — this is what lets the wizard offer "post it back here"
    # as its default, one-click channel option.
    origin_channel = interaction.channel
    guild = interaction.guild
 
    await interaction.response.defer(ephemeral=True)
 
    try:
        dm_channel = await interaction.user.create_dm()
        await dm_channel.send(
            f"👋 Let's set up an event! (Started from **#{origin_channel.name}** in **{guild.name}**.)"
        )
    except discord.Forbidden:
        await interaction.followup.send(
            embed=make_error_embed(
                "Can't DM You",
                "I couldn't send you a DM — please enable direct messages from server members and run `/event` again.",
            ),
            ephemeral=True,
        )
        return
    except discord.HTTPException as e:
        await interaction.followup.send(
            embed=make_error_embed("Something Went Wrong", f"Couldn't start the DM setup: {e}"),
            ephemeral=True,
        )
        return
 
    await interaction.followup.send(
        embed=make_embed(interaction, title="📧 Check your DMs to set up the event!", color=EMBED_COLOR_INFO, verb="Requested"),
        ephemeral=True,
    )
 
    active_setups.add(interaction.user.id)
    try:
        await run_event_setup(interaction.user, guild, origin_channel, dm_channel)
    finally:
        active_setups.discard(interaction.user.id)
 
 
# ----- H. Persistent RSVP button view (company events) ---------------------
 
class EventRSVPView(discord.ui.View):
    """Persistent view attached to every company-specific event embed.
    Registered once in setup_hook via bot.add_view(), so the buttons keep
    working after a bot restart without the message needing to be
    resent. Every button routes through _handle(), which figures out
    which event it belongs to from interaction.message — nothing needs to
    be encoded in the custom_id."""
 
    def __init__(self):
        super().__init__(timeout=None)
 
    async def _handle(self, interaction, status_key):
        try:
            event = active_events.get(interaction.message.id)
            if event is None:
                await interaction.response.send_message(
                    embed=make_error_embed(
                        "Event Not Found",
                        "This event isn't being tracked anymore (it may predate the bot's last restart).",
                    ),
                    ephemeral=True,
                )
                return
 
            user_id = interaction.user.id
            if EVENT_RSVP_MUTUALLY_EXCLUSIVE:
                # A member can only hold ONE RSVP status at a time — picking
                # a new one clears any previous one automatically.
                for key in ("accepted", "declined", "tentative"):
                    if key != status_key:
                        other_bucket = event["categories"].setdefault(key, [])
                        if user_id in other_bucket:
                            other_bucket.remove(user_id)
 
            bucket = event["categories"].setdefault(status_key, [])
            if user_id in bucket:
                bucket.remove(user_id)  # clicking your current status again clears it
            else:
                bucket.append(user_id)
            event["display_names"][str(user_id)] = interaction.user.display_name
 
            await interaction.response.edit_message(embed=build_event_embed(event))
            await save_events()
        except Exception as e:
            print(f"Error handling RSVP button ({status_key}): {e}")
            if not interaction.response.is_done():
                try:
                    await interaction.response.send_message(
                        embed=make_error_embed("Something Went Wrong", "Couldn't update your RSVP — please try again."),
                        ephemeral=True,
                    )
                except discord.HTTPException:
                    pass
 
    @discord.ui.button(label="Accepted", emoji="✅", style=discord.ButtonStyle.success, custom_id="event_rsvp_accepted")
    async def accepted_btn(self, interaction, button):
        await self._handle(interaction, "accepted")
 
    @discord.ui.button(label="Declined", emoji="❌", style=discord.ButtonStyle.danger, custom_id="event_rsvp_declined")
    async def declined_btn(self, interaction, button):
        await self._handle(interaction, "declined")
 
    @discord.ui.button(label="Tentative", emoji="❓", style=discord.ButtonStyle.secondary, custom_id="event_rsvp_tentative")
    async def tentative_btn(self, interaction, button):
        await self._handle(interaction, "tentative")
 
 
# ----- I. Reaction handling (general events) --------------------------------
 
def request_event_refresh(event):
    """Schedules an embed refresh a short moment from now instead of
    editing on every single reaction — a burst of people reacting at
    once (e.g. right when the event goes up) then costs one Discord API
    call instead of dozens."""
    message_id = event["message_id"]
    existing = _pending_refresh_tasks.get(message_id)
    if existing and not existing.done():
        return  # a refresh is already queued for this message; nothing more to do
    _pending_refresh_tasks[message_id] = asyncio.create_task(_debounced_refresh(event))
 
 
async def _debounced_refresh(event):
    try:
        await asyncio.sleep(EVENT_REFRESH_DEBOUNCE_SECONDS)
        if event["message_id"] not in active_events:
            return  # deleted / no longer tracked while we were waiting
        await refresh_event_message(event)
        await save_events()
    except Exception as e:
        print(f"Error refreshing event {event.get('message_id')}: {e}")
 
 
async def refresh_event_message(event):
    channel = bot.get_channel(event["channel_id"])
    if channel is None:
        return
    try:
        message = await channel.fetch_message(event["message_id"])
        await message.edit(embed=build_event_embed(event))
    except discord.NotFound:
        active_events.pop(event["message_id"], None)
    except discord.HTTPException as e:
        print(f"Could not refresh event message {event['message_id']}: {e}")
 
 
async def handle_event_reaction(payload, adding):
    """Shared logic for on_raw_reaction_add/remove: keeps a GENERAL
    event's per-company attendance lists in sync with the reactions on
    its message. Company events use buttons instead (EventRSVPView), so
    this only ever touches general events."""
    if payload.user_id == bot.user.id:
        return  # ignore the bot's own placeholder reactions
    event = active_events.get(payload.message_id)
    if event is None or event["type"] != EVENT_TYPE_GENERAL:
        return
 
    company_key = event["emoji_map"].get(str(payload.emoji))
    if company_key is None:
        return  # someone reacted with an unrelated emoji — ignore it
 
    guild = bot.get_guild(event["guild_id"])
    if guild is None:
        return
    try:
        member = payload.member or guild.get_member(payload.user_id) or await guild.fetch_member(payload.user_id)
    except discord.NotFound:
        return  # they reacted then immediately left the server
 
    bucket = event["categories"].setdefault(company_key, [])
    changed = False
    if adding and payload.user_id not in bucket:
        bucket.append(payload.user_id)
        changed = True
    elif not adding and payload.user_id in bucket:
        bucket.remove(payload.user_id)
        changed = True
    if not changed:
        return
 
    event["display_names"][str(payload.user_id)] = member.display_name
    request_event_refresh(event)
 
 
@bot.event
async def on_raw_reaction_add(payload):
    try:
        await handle_event_reaction(payload, adding=True)
    except Exception as e:
        print(f"Error handling event reaction add: {e}")
 
 
@bot.event
async def on_raw_reaction_remove(payload):
    try:
        await handle_event_reaction(payload, adding=False)
    except Exception as e:
        print(f"Error handling event reaction remove: {e}")
 
 
@bot.event
async def on_raw_message_delete(payload):
    """Stops tracking an event if its message gets deleted, so the bot
    doesn't keep trying to edit or remind something that no longer
    exists."""
    event = active_events.pop(payload.message_id, None)
    if event is None:
        return
    task = reminder_tasks.pop(payload.message_id, None)
    if task and not task.done():
        task.cancel()
    try:
        await save_events()
    except Exception as e:
        print(f"Error saving events after message delete: {e}")
 
 
# ----- J. 20-minutes-out reminder thread + ping -----------------------------
 
def schedule_event_reminder(event):
    """Fires the reminder as a background asyncio task. Safe to call
    again for an event that already has one scheduled (e.g. on reload
    from disk at startup) — cancels any previous task first so two
    timers never race each other."""
    message_id = event.get("message_id")
    if message_id is None:
        return
    existing = reminder_tasks.get(message_id)
    if existing and not existing.done():
        existing.cancel()
    if event.get("reminder_sent"):
        return
 
    start_dt = datetime.fromtimestamp(event["start_epoch"], tz=timezone.utc)
    reminder_dt = start_dt - timedelta(minutes=EVENT_REMINDER_LEAD_MINUTES)
    delay = (reminder_dt - datetime.now(timezone.utc)).total_seconds()
    if delay <= 0:
        # Event starts too soon (or has already started) for a reminder to
        # make sense — matches the spec: "doesn't apply if the event was
        # created within 20 minutes from its start."
        return
 
    reminder_tasks[message_id] = asyncio.create_task(_fire_reminder_after_delay(event, delay))
 
 
async def _fire_reminder_after_delay(event, delay):
    try:
        await asyncio.sleep(delay)
        await send_event_reminder(event)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"Error firing reminder for event {event.get('message_id')}: {e}")
 
 
async def _send_chunked(destination, prefix, mentions, allowed_mentions, chunk_size=1900):
    """Sends a big block of user mentions across as many messages as
    needed to stay under Discord's 2000-character limit, instead of
    letting one giant battalion-wide ping fail outright."""
    text = " ".join(mentions)
    first = True
    while text:
        chunk = text[:chunk_size]
        if len(text) > chunk_size:
            cut = chunk.rfind(" ")
            if cut > 0:
                chunk = chunk[:cut]
        await destination.send((prefix if first else "") + chunk, allowed_mentions=allowed_mentions)
        text = text[len(chunk):].lstrip()
        first = False
 
 
async def send_event_reminder(event):
    """Creates a thread under the event message and pings everyone
    attending: all reacted users for a general event, or Accepted +
    Tentative for a company event."""
    message_id = event["message_id"]
    if message_id not in active_events or event.get("reminder_sent"):
        return  # deleted, or somehow already handled
 
    channel = bot.get_channel(event["channel_id"])
    if channel is None:
        return
    try:
        message = await channel.fetch_message(message_id)
    except (discord.NotFound, discord.HTTPException):
        return
 
    if event["type"] == EVENT_TYPE_GENERAL:
        user_ids = sorted({uid for members in event["categories"].values() for uid in members})
    else:
        user_ids = sorted(set(event["categories"].get("accepted", [])) | set(event["categories"].get("tentative", [])))
 
    try:
        thread = await message.create_thread(
            name=(f"⏰ {event['title']} — Starting Soon")[:100],
            auto_archive_duration=60,
        )
    except discord.HTTPException as e:
        print(f"Could not create reminder thread for event {message_id}: {e}")
        return
 
    intro = f"⏰ **{event['title']}** starts in {EVENT_REMINDER_LEAD_MINUTES} minutes!\n"
    allowed = discord.AllowedMentions(users=True, everyone=False, roles=False)
    if not user_ids:
        await thread.send(intro + "No one has RSVP'd yet — see you there anyway!", allowed_mentions=allowed)
    else:
        await _send_chunked(thread, intro, [f"<@{uid}>" for uid in user_ids], allowed)
 
    event["reminder_sent"] = True
    await save_events()


bot.run(DISCORD_BOT_TOKEN)
