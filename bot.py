import os
import string

import discord

from discord import Intents
from thefuzz import fuzz

intents = Intents.none()
intents.guilds = True
intents.guild_messages = True
intents.message_content = True

mentions = discord.AllowedMentions.none()

client = discord.Client(
    intents=intents,
    allowed_mentions=mentions,
)

THANKING_WORDS = ["thank", "vroom", "zoom", "nyoom"]
client.thank_channels = set()
client.thank_pairs = {}


@client.event
async def on_guild_join(guild):
    collect_from_guild(guild)


def collect_from_guild(guild):
    thank_channel = discord.utils.find(lambda c: c.name == 'thank', guild.channels)

    if thank_channel is None:
        return

    client.thank_channels.add(thank_channel)

    client.thank_pairs[guild.id] = {}


@client.event
async def on_ready():
    for guild in client.guilds:
        collect_from_guild(guild)


bad_chars = set('/{}\\%$[]#()-=<>|^@`*_')

@client.event
async def on_message(message):
    if message.author == client.user or message.author.bot:
        return

    if message.channel not in client.thank_channels:
        return

    length = len(message.content)
    if 0 > length > 500:
        return

    text = message.content

    text = ''.join(filter(lambda x: x in string.printable, text))
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

    if get_thankness(text, THANKING_WORDS) > 0.7:
        thank_msg = await message.channel.send(text)
        client.thank_pairs[message.guild.id][message.id] = thank_msg


@client.event
async def on_message_delete(message):
    await delete_from_message(message)


@client.event
async def on_message_edit(before, _after):
    await delete_from_message(before)


async def delete_from_message(message):
    if message.author == client.user or message.author.bot:
        return

    thank_msg = client.thank_pairs[message.guild.id].get(message.id)

    if thank_msg is not None:
        try:
            await thank_msg.delete()
        except discord.errors.NotFound:
            pass


def get_thankness(text, keywords):
    thankness = 0
    max_thankness = len(keywords)

    for keyword in keywords:
        partial = fuzz.partial_ratio(keyword, text)
        if partial > 70:
            # if the phrase fuzzes a keyword, calculate thankness for that keyword
            thankness += fuzz.ratio(keyword, text) / 100
        else:
            # if we don't have that phrase, don't consider it for calculating our final thankness ratio
            max_thankness -= 1

    # we didn't fuzz any keywords, no thankness
    if max_thankness < 1:
        return 0

    return thankness * max_thankness


client.run(os.environ['THANK_TOKEN'])
