import discord
from fuzzywuzzy import fuzz
import os

client = discord.Client()

thanking_words = ["thank", "vroom", "zoom", "7"]
thank_channels = []
thank_proxy_channels = []
thank_proxy_pairs = {}
thank_pairs = {}


@client.event
async def on_guild_join(guild):
    collect_from_guild(guild)


def collect_from_guild(guild):
    thank_channel = discord.utils.find(lambda c: c.name == 'thank', guild.channels)

    if thank_channel is None:
        return

    thank_channels.append(thank_channel)

    thank_pairs[guild.id] = {}

    thank_proxy_channel = discord.utils.find(lambda c: c.name == 'thank-proxy', guild.channels)

    if thank_proxy_channel is None:
        return

    thank_proxy_channels.append(thank_proxy_channel)

    thank_proxy_pairs[thank_proxy_channel.id] = thank_channel


@client.event
async def on_ready():
    for guild in client.guilds:
        collect_from_guild(guild)


@client.event
async def on_message(message):
    if message.author == client.user or message.author.bot:
        return

    text = message.content

    if message.channel in thank_channels:
        if get_thankness(text, *thanking_words) > 85:
            thank_msg = await message.channel.send(message.content.lower())
            thank_pairs[message.guild.id][message.id] = thank_msg
    elif message.channel in thank_proxy_channels:
        await thank_proxy_pairs[message.channel.id].send(message.content)


@client.event
async def on_message_delete(message):
    delete_from_message(message)


@client.event
async def on_message_edit(before, after):
    delete_from_message(before)


def delete_from_message(message):
    if message.author == client.user or message.author.bot:
        return

    thank_msg = thank_pairs[message.guild.id].get(message.id)

    if thank_msg is not None:
        client.delete_message(thank_msg)


def get_thankness(text, *keywords):
    thankness = 0
    max_thankness = len(keywords)

    for keyword in keywords:
        partial = fuzz.partial_ratio(keyword, text)
        if partial > 70:
            # if the phrase fuzzes a keyword, calculate thankness for that keyword
            thankness += (fuzz.ratio(keyword, text) + partial) / 2
        else:
            # if we don't have that phrase, don't consider it for calculating our final thankness ratio
            max_thankness -= 1

    # we didn't fuzz any keywords, no thankness
    if max_thankness < 1:
        return 0

    return thankness / max_thankness


client.run(os.environ['THANK_TOKEN'])
