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

from datetime import datetime, timezone

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

FOOTER_BRAND = "442nd ORBAT System"


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



bot.run(DISCORD_BOT_TOKEN)
