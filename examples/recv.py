import discord
from discord.ext import commands, voice_recv

discord.opus._load_default()

bot = commands.Bot(command_prefix=commands.when_mentioned)


class Testing(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    async def test(self, ctx):
        def callback(member, packet):
            print(member, packet)

        vc = await ctx.author.voice.channel.connect(cls=voice_recv.VoiceRecvClient)
        vc.listen(voice_recv.BasicSink(callback))

    @commands.command()
    async def stop(self, ctx):
        ctx.voice_client.stop_listening()
        await ctx.voice_client.disconnect()

    @commands.command()
    async def die(self, ctx):
        await self.stop(ctx)
        await ctx.bot.close()


@bot.event
async def on_ready():
    print('Logged in as {0.id}/{0}'.format(bot.user))
    print('------')


bot.add_cog(Testing(bot))
bot.run(token)


# def callback(member, packet):
#     ...

# vc = await channel.connect()
# vc.listen(voice_recv.BasicSink(callback))

# Something like this
# TODO: a proper example
