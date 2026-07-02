from __future__ import annotations

import asyncio
import datetime
import os
import string
import traceback
from typing import TYPE_CHECKING

import aiohttp
import cachetools
import discord
import profanity_check
from discord import AllowedMentions, Intents
from dotenv import load_dotenv
from thefuzz import fuzz
from tinydb import Query, TinyDB

import rag.export
import rag.ingest
from rag.agent import ask_rag_agent

if TYPE_CHECKING:
    from collections.abc import Coroutine

if os.name == "nt":
    # handle Windows imports
    # for colored terminal
    import colorama

    colorama.init()
else:
    # handle POSIX imports
    # for uvloop
    # while we have steam client, we cannot use uvloop due to gevent
    import uvloop

    uvloop.install()


load_dotenv()

intents = Intents.none()
intents.guilds = True
intents.guild_messages = True
intents.message_content = True

mentions = AllowedMentions.none()


class ThankBot(discord.Client):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.thank_channels: set[discord.TextChannel] = set()
        self.thank_pairs: dict[int, cachetools.TTLCache[int, discord.Message]] = {}
        self.reddit_channels: list[discord.TextChannel] = []
        self.help_forums: set[discord.ForumChannel] = set()
        self.volunteer_roles: dict[int, discord.Role] = {}
        self.faq_channels: dict[int, discord.TextChannel] = {}
        self.comtress_general_channels: dict[int, discord.TextChannel] = {}
        self.comtress_players_roles: dict[int, discord.Role] = {}
        self.comtress_testers_roles: dict[int, discord.Role] = {}
        self.zero_players_since: datetime.datetime | None = (
            datetime.datetime.fromtimestamp(0, tz=datetime.timezone.utc)
        )
        self.latest_ping_messages: dict[int, discord.Message] = {}


client = ThankBot(
    intents=intents,
    allowed_mentions=mentions,
)

db = TinyDB("thanks_db.json")

THANKING_WORDS = ["thamk", "vroom", "zoom", "nyoom"]
FILLER_WORDS = {
    "you",
    "so",
    "much",
    "very",
    "a",
    "lot",
    "for",
    "the",
    "too",
    "my",
    "our",
}
EASTER_EGGS = {
    "good bot": "vroom vroom <3",
    "bad bot": ":(",
    "meow": "nyaa",
}


class TaskWrapper:
    def __init__(self, task: asyncio.Task):
        self.task: asyncio.Task = task
        task.add_done_callback(self.on_task_done)

    def __getattr__(self, name):
        return getattr(self.task, name)

    def __await__(self):
        self.task.remove_done_callback(self.on_task_done)
        return self.task.__await__()

    def on_task_done(self, fut: asyncio.Future):
        if fut.cancelled() or not fut.done():
            return
        fut.result()

    def __str__(self):
        return f"TaskWrapper<task={self.task}>"


def create_task(coro: Coroutine, *, name: str = None) -> TaskWrapper:
    task = asyncio.create_task(coro, name=name)
    return TaskWrapper(task)


@client.event
async def on_guild_join(guild: discord.Guild):
    collect_from_guild(guild)


class ChannelCollector:
    def collect(self, guild: discord.Guild) -> list[discord.abc.GuildChannel] | None:
        return None


class ChannelNameCollector(ChannelCollector):
    def __init__(self, channel_name: str):
        self.channel_name = channel_name

    def collect(self, guild: discord.Guild) -> list[discord.abc.GuildChannel] | None:
        channel = discord.utils.find(
            lambda c: c.name.startswith(self.channel_name), guild.channels
        )
        if channel is None:
            return None
        return [channel]


class ChannelCategoryCollector(ChannelCollector):
    def __init__(self, category_name: str):
        self.category_name = category_name

    def collect(self, guild: discord.Guild) -> list[discord.abc.GuildChannel] | None:
        category = discord.utils.find(
            lambda c: c.name.startswith(self.category_name), guild.categories
        )
        if category is None:
            return None
        return category.channels


def collect_channels_from_guild(
    guild: discord.Guild,
    collector: ChannelCollector,
    channel_type: discord.ChannelType = discord.ChannelType.text,
) -> list[discord.abc.GuildChannel]:
    channels = collector.collect(guild)

    if not channels:
        return []

    return [c for c in channels if c.type == channel_type]


def collect_channel_from_guild(
    guild: discord.Guild,
    collector: ChannelCollector,
    channel_type: discord.ChannelType = discord.ChannelType.text,
) -> discord.abc.GuildChannel | None:
    channels = collect_channels_from_guild(guild, collector, channel_type)
    if not channels:
        return None
    return channels[0]


def collect_from_guild(guild: discord.Guild):
    thank_channel = collect_channel_from_guild(guild, ChannelNameCollector("thamk"))
    if thank_channel:
        client.thank_channels.add(thank_channel)

    # limit thank message pairs to 1 day and 100 messages
    client.thank_pairs[guild.id] = cachetools.TTLCache(
        maxsize=100, ttl=datetime.timedelta(days=1).total_seconds()
    )

    reddit_channel = collect_channel_from_guild(guild, ChannelNameCollector("reddit"))
    if reddit_channel:
        client.reddit_channels.append(reddit_channel)

    faq_channel = collect_channel_from_guild(guild, ChannelNameCollector("faq"))
    if faq_channel:
        client.faq_channels[guild.id] = faq_channel

    help_forums = collect_channels_from_guild(
        guild, ChannelCategoryCollector("Help & Support"), discord.ChannelType.forum
    )
    if help_forums:
        client.help_forums.update(help_forums)

    client.volunteer_roles[guild.id] = discord.utils.get(guild.roles, name="Volunteer")

    comtress_general = collect_channel_from_guild(
        guild, ChannelNameCollector("comtress-general")
    )
    if comtress_general:
        client.comtress_general_channels[guild.id] = comtress_general

    client.comtress_players_roles[guild.id] = discord.utils.get(
        guild.roles, name="Comtress Players"
    )
    client.comtress_testers_roles[guild.id] = discord.utils.get(
        guild.roles, name="Comtress Testers"
    )


window = datetime.timedelta(hours=1)


async def clear_reddit_channels():
    clear_time = datetime.datetime.now(tz=datetime.timezone.utc) - window
    for channel in client.reddit_channels:
        messages = True
        tries = 3
        while messages:
            try:
                messages = await channel.purge(
                    before=clear_time, oldest_first=True, reason="reddit"
                )
            except Exception as e:
                print(e)
                tries -= 1
                if tries <= 0:
                    break
            await asyncio.sleep(0.5)


clear_interval = 60 * 10


async def reddit_clear_job():
    while True:
        await clear_reddit_channels()
        await asyncio.sleep(clear_interval)


reddit_clear_inst: TaskWrapper | None = None


def schedule_reddit_clear():
    global reddit_clear_inst
    if reddit_clear_inst is not None:
        reddit_clear_inst.task.cancel()
    reddit_clear_inst = create_task(reddit_clear_job(), name="Reddit Clear Job")


async def rag_sync_job():
    await client.wait_until_ready()
    while not client.is_closed():
        print("Running RAG sync job...")
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, rag.export.ensure_repo_cloned)

            logs_changed = await rag.export.export_archived_logs(client)
            logs_changed |= await rag.export.export_live_forums(client)
            logs_changed |= await rag.export.export_github_releases()

            await loop.run_in_executor(None, rag.ingest.ingest_all, True, logs_changed)
        except Exception:
            print("RAG sync job failed:")
            traceback.print_exc()
        await asyncio.sleep(60 * 60 * 24)


rag_sync_inst: TaskWrapper | None = None


def schedule_rag_sync():
    global rag_sync_inst
    if rag_sync_inst is not None:
        rag_sync_inst.task.cancel()
    rag_sync_inst = create_task(rag_sync_job(), name="RAG Sync Job")


async def check_roles_pinged_recently(channel: discord.TextChannel) -> bool:
    now = datetime.datetime.now(tz=datetime.timezone.utc)
    one_hour_ago = now - datetime.timedelta(hours=1)
    try:
        roles = {"Comtress Players", "Comtress Testers"}
        async for message in channel.history(limit=200, after=one_hour_ago):
            for role in message.role_mentions:
                if role.name in roles:
                    return True
    except Exception as e:
        print(f"Error checking channel history: {e}")
        return True
    return False


def get_flag_emoji(country_code: str) -> str:
    if not country_code or len(country_code) != 2 or not country_code.isalpha():
        return ""
    return "".join(chr(ord(c) + 127397) for c in country_code.upper())


async def comtress_check_job():
    await client.wait_until_ready()
    headers = {"User-Agent": "ComtressPlayerCheckerBot/1.0"}
    async with aiohttp.ClientSession(headers=headers) as session:
        while not client.is_closed():
            try:
                async with session.get(
                    "https://api.teamcomtress.com/servers"
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        servers = data.get("response", {}).get("servers", [])
                        total_players = sum(s.get("players", 0) for s in servers)

                        now = datetime.datetime.now(tz=datetime.timezone.utc)

                        if total_players == 0:
                            if client.zero_players_since is None:
                                client.zero_players_since = now

                            # Delete existing ping messages on quiet time
                            for guild_id, message in list(
                                client.latest_ping_messages.items()
                            ):
                                if message:
                                    try:
                                        await message.delete()
                                    except discord.errors.NotFound:
                                        pass
                                    except Exception as e:
                                        print(
                                            f"Error deleting ping message in guild {guild_id}: {e}"
                                        )
                            client.latest_ping_messages.clear()
                        else:
                            # We have active players!
                            should_ping = False
                            if client.zero_players_since is not None:
                                quiet_duration = now - client.zero_players_since
                                if quiet_duration >= datetime.timedelta(hours=1):
                                    should_ping = True

                            # Build the embed
                            embed = discord.Embed(
                                title="Active Team Comtress 2 Servers",
                                description=f"Total active players: **{total_players}**",
                                color=discord.Color.green(),
                                timestamp=now,
                            )
                            # Sort servers by player count descending
                            sorted_servers = sorted(
                                servers, key=lambda s: s.get("players", 0), reverse=True
                            )
                            for s in sorted_servers:
                                players = s.get("players", 0)
                                if players < 1:
                                    continue

                                name = s.get("name", "Unknown Server")
                                max_players = s.get("max_players", 24)
                                map_name = s.get("map", "unknown")
                                location = s.get("location", "")
                                flag = get_flag_emoji(location)
                                flag_str = f"{flag} " if flag else ""

                                embed.add_field(
                                    name=f"{flag_str}{name}",
                                    value=f"👥 **{players}/{max_players}** players  •  🗺️ `{map_name}`",
                                    inline=False,
                                )

                            for guild in client.guilds:
                                channel = client.comtress_general_channels.get(guild.id)
                                comtress_players = client.comtress_players_roles.get(
                                    guild.id
                                )
                                if channel and comtress_players:
                                    message = client.latest_ping_messages.get(guild.id)
                                    send_ping = False

                                    # If message exists:
                                    if message:
                                        try:
                                            # Check if it is silent
                                            is_silent = (
                                                message.flags.suppress_notifications
                                            )
                                            # If it's silent, but it has been > 1 hour since the last role ping,
                                            # delete the silent message and recreate it as a pinging message.
                                            recently_pinged = (
                                                await check_roles_pinged_recently(
                                                    channel
                                                )
                                            )
                                            if is_silent and not recently_pinged:
                                                try:
                                                    await message.delete()
                                                except discord.errors.NotFound:
                                                    pass
                                                message = None
                                                send_ping = True
                                            else:
                                                # Otherwise, just edit the existing message embed silently
                                                await message.edit(embed=embed)
                                        except discord.errors.NotFound:
                                            message = None
                                        except Exception as e:
                                            print(
                                                f"Error editing/updating ping message in guild {guild.id}: {e}"
                                            )
                                            message = None

                                    # If no message exists (or was deleted/recreated), send a new one
                                    if not message:
                                        if not send_ping:
                                            recently_pinged = (
                                                await check_roles_pinged_recently(
                                                    channel
                                                )
                                            )
                                            if should_ping and not recently_pinged:
                                                send_ping = True

                                        message_content = f"{comtress_players.mention} Join up! There are players on Team Comtress 2!"

                                        allowed_mentions = discord.AllowedMentions(
                                            roles=[comtress_players]
                                        )

                                        try:
                                            new_msg = await send_with_retry(
                                                channel,
                                                message_content,
                                                embed=embed,
                                                allowed_mentions=allowed_mentions,
                                                silent=not send_ping,
                                            )
                                            if new_msg:
                                                client.latest_ping_messages[
                                                    guild.id
                                                ] = new_msg
                                        except Exception as e:
                                            print(
                                                f"Error sending ping message in guild {guild.id}: {e}"
                                            )

                            client.zero_players_since = None
                    else:
                        print(
                            f"Failed to fetch Team Comtress servers: HTTP status {response.status}"
                        )
            except Exception as e:
                print(f"Error checking Team Comtress players: {e}")
                traceback.print_exc()

            await asyncio.sleep(60)


comtress_check_inst: TaskWrapper | None = None


def schedule_comtress_check():
    global comtress_check_inst
    if comtress_check_inst is not None:
        comtress_check_inst.task.cancel()
    comtress_check_inst = create_task(comtress_check_job(), name="Comtress Check Job")


@client.event
async def on_ready():
    for guild in client.guilds:
        collect_from_guild(guild)

    schedule_reddit_clear()
    schedule_rag_sync()
    schedule_comtress_check()

    print("Ready.")


bad_chars = set("/{}\\%$[]#()-=<>|^@`*_")


def interpret_int(txt: str):
    if not txt:
        return None
    try:
        return int(txt)
    except ValueError:
        return None


THANK_BAIT_USER_ID = interpret_int(os.getenv("THANK_BAIT_USER_ID"))


async def bait_msg(message: discord.Message):
    await message.channel.send("bait used to be believable")


async def send_with_retry(sendable, *args, **kwargs):
    for _ in range(3):
        try:
            return await sendable.send(*args, **kwargs)
        except discord.errors.DiscordServerError:
            await asyncio.sleep(2)


@client.event
async def on_thread_create(thread: discord.Thread):
    if not thread.guild:
        return

    thread_owner = thread.owner
    if thread_owner is None:
        thread_owner = thread.guild.get_member(thread.owner_id)
        if not thread_owner:
            thread_owner = await thread.guild.fetch_member(thread.owner_id)

    if thread_owner is None:
        return

    if thread_owner == client.user or thread_owner.bot:
        return

    volunteer_role = client.volunteer_roles.get(thread.guild.id)
    if volunteer_role is None:
        return

    if thread.parent not in client.help_forums:
        return

    # wait for starter_message
    starter_msg = thread.starter_message
    while not starter_msg:
        await asyncio.sleep(2)
        try:
            starter_msg = await thread.fetch_message(thread.id)
        except Exception:
            pass

    allowed_mentions = AllowedMentions(users=[thread_owner], roles=[volunteer_role])

    faq_channel = client.faq_channels.get(thread.guild.id)
    faq_mention = faq_channel.mention if faq_channel else "#faq"

    await send_with_retry(
        thread,
        f"""Hello {thread_owner.mention}! I see you need some assistance. Make sure to supply as much detail as possible in your post so that someone may help you at their earliest convenience.

It may be helpful to check {faq_mention} and the [Installation Instructions](https://docs.comfig.app/latest/setup/install/) and [Quick Fixes](https://docs.comfig.app/latest/next_steps/quick_fixes/) as they can provide an immediate answer or solution to your question.

I have also pinged {volunteer_role.mention} so that they see your thread and can help you as soon as possible!

Once you're done, tag this thread as :white_check_mark: Solved.""",
        allowed_mentions=allowed_mentions,
    )

    try:
        prompt_content = f"Title: {thread.name}\n\n{starter_msg.content}"
        if starter_msg.attachments:
            attachment_names = [a.filename for a in starter_msg.attachments]
            prompt_content += (
                f"\n[User provided attachments: {', '.join(attachment_names)}]"
            )

        response = await ask_rag_agent(prompt_content)

        if response.startswith("NEED_INFO:"):
            question = response.replace("NEED_INFO:", "").strip()
            await send_with_retry(thread, f"{thread_owner.mention} {question}")
        elif response == "ESCALATE":
            pass  # Volunteer is already pinged
        else:
            thread_owner_mention = AllowedMentions(users=[thread_owner])
            full_text = f"{thread_owner.mention} Here is some relevant information that might help:\n\n{response}\n\n*If this doesn't solve your issue, {volunteer_role.name}s will be here soon to assist further!*"

            for i in range(0, len(full_text), 1950):
                await send_with_retry(
                    thread,
                    full_text[i : i + 1950],
                    allowed_mentions=thread_owner_mention,
                )
    except Exception as e:
        print("RAG agent failed:", e)


@client.event
async def on_message(message: discord.Message):
    if message.author == client.user or message.author.bot:
        return

    if client.user in message.mentions:
        target_user = None

        if message.reference:
            if isinstance(message.reference.resolved, discord.Message):
                target_user = message.reference.resolved.author
            elif message.reference.cached_message:
                target_user = message.reference.cached_message.author
            elif message.reference.message_id:
                try:
                    ref_msg = await message.channel.fetch_message(
                        message.reference.message_id
                    )
                    target_user = ref_msg.author
                except Exception:
                    pass

        if not target_user:
            for user in message.mentions:
                if user != client.user and not user.bot and user != message.author:
                    target_user = user
                    break

        if target_user:
            if target_user == message.author:
                await message.channel.send("You cannot thamk yourself!")
                return
            elif target_user == client.user or target_user.bot:
                pass
            else:
                UserQuery = Query()
                user_record = db.search(UserQuery.user_id == target_user.id)
                new_thanks = 1
                if user_record:
                    new_thanks = user_record[0].get("thanks", 0) + 1
                    db.update(
                        {"thanks": new_thanks}, UserQuery.user_id == target_user.id
                    )
                else:
                    db.insert({"user_id": target_user.id, "thanks": 1})

                await message.channel.send(
                    f"Logged a thamk for {target_user.mention}! They now have {new_thanks} thamks.",
                    silent=True,
                )
                return

    is_bait = message.author.id == THANK_BAIT_USER_ID
    is_thank_channel = message.channel in client.thank_channels
    is_valid_target = is_bait or is_thank_channel
    if not is_valid_target:
        return

    length = len(message.content)
    if length < 1 or length > 1019:
        return

    text = message.content

    text = "".join(filter(lambda x: x in string.printable, text))
    if not text:
        return

    text = text.strip()
    if not text:
        return

    if any((c in bad_chars) for c in text):
        return

    if "http" in text:
        return

    text = text.lower()

    if not text:
        return

    if profanity_check.predict([text])[0] > 0.5:
        return

    if is_bait:
        await bait_msg(message)
        return

    easter_egg_reply = EASTER_EGGS.get(text)
    if easter_egg_reply:
        thank_msg = await message.channel.send(easter_egg_reply)
        client.thank_pairs[message.guild.id][message.id] = thank_msg
        return

    if get_thankness(text) > 70:
        thank_msg = await message.channel.send(text.replace("n", "m"))
        client.thank_pairs[message.guild.id][message.id] = thank_msg


@client.event
async def on_message_delete(message: discord.Message):
    await delete_from_message(message)


@client.event
async def on_message_edit(before: discord.Message, _after: discord.Message):
    await delete_from_message(before)


async def delete_from_message(message: discord.Message):
    if message.author == client.user or message.author.bot:
        return

    if not message.guild:
        return

    guild_pairs = client.thank_pairs.get(message.guild.id)
    if not guild_pairs:
        return

    thank_msg = guild_pairs.pop(message.id, None)

    if thank_msg is not None:
        try:
            await thank_msg.delete()
        except discord.errors.NotFound:
            pass


def get_thankness(text: str) -> float:
    words = [w for w in text.split() if w not in FILLER_WORDS]

    length = len(words)

    if length < 1 or length > 170:
        return 0.0

    thankness = 0.0

    for word in words:
        best = 0.0
        for keyword in THANKING_WORDS:
            if keyword == word:
                best = 100.0
                break
            ratio = fuzz.ratio(keyword, word)
            if ratio > best:
                best = ratio
        thankness += best

    return thankness / length


if __name__ == "__main__":
    client.run(os.environ["THANK_TOKEN"])
