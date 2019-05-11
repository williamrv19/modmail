import asyncio
import re
import os
from datetime import datetime
from typing import Optional, Union
from types import SimpleNamespace as param

import discord
from discord.ext import commands

from dateutil import parser
from natural.date import duration

from core import checks
from core.decorators import trigger_typing
from core.models import Bot, PermissionLevel
from core.paginator import PaginatorSession
from core.time import UserFriendlyTime, human_timedelta
from core.utils import format_preview, User


class Modmail:
    """Commands directly related to Modmail functionality."""

    def __init__(self, bot: Bot):
        self.bot = bot

    @commands.command()
    @trigger_typing
    @checks.has_permissions(PermissionLevel.OWNER)
    async def setup(self, ctx):
        """Sets up a server for Modmail"""
        if self.bot.main_category:
            return await ctx.send(
                f'{self.bot.modmail_guild} is already set up.'
            )

        category = await self.bot.modmail_guild.create_category(
            name='Modmail',
            overwrites=self.bot.overwrites(ctx)
        )

        await category.edit(position=0)

        log_channel = await self.bot.modmail_guild.create_text_channel(
            name='bot-logs', category=category
        )

        embed = discord.Embed(
            title='Friendly Reminder:',
            description='You may use the `config set log_channel_id '
                        '<channel-id>` command to set up a custom log channel'
                        ', then you can delete the default '
                        f'{log_channel.mention} channel.',
            color=self.bot.main_color
        )

        embed.set_footer(text=f'Type "{self.bot.prefix}help" '
                              'for a complete list of commands.')
        await log_channel.send(embed=embed)

        self.bot.config['main_category_id'] = category.id
        self.bot.config['log_channel_id'] = log_channel.id

        await self.bot.config.update()
        await ctx.send('Successfully set up server.\n'
                       'Consider setting permission groups to give access '
                       'to roles or users the ability to use Modmail.\n'
                       f'Type `{self.bot.prefix}permissions` for more info.')
        if not self.bot.config.permissions:
            await self.bot.update_perms(PermissionLevel.REGULAR, -1)
            await self.bot.update_perms(PermissionLevel.OWNER, ctx.author.id)

    @commands.group(invoke_without_command=True)
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def snippets(self, ctx):
        """Returns a list of snippets that are currently set."""
        embeds = []

        if self.bot.snippets:
            embed = discord.Embed(color=self.bot.main_color,
                                  description='Here is a list of snippets '
                                              'that are currently configured.')
        else:
            embed = discord.Embed(
                color=discord.Color.red(),
                description='You dont have any snippets at the moment.'
            )
            embed.set_footer(
                text=f'Do {self.bot.prefix}help snippets for more commands.'
            )

        embed.set_author(name='Snippets', icon_url=ctx.guild.icon_url)
        embeds.append(embed)

        for name, value in self.bot.snippets.items():
            if len(embed.fields) == 5:
                embed = discord.Embed(color=self.bot.main_color,
                                      description=embed.description)
                embed.set_author(name='Snippets', icon_url=ctx.guild.icon_url)
                embeds.append(embed)
            embed.add_field(name=name, value=value, inline=False)

        session = PaginatorSession(ctx, *embeds)
        await session.run()

    @snippets.command(name='add')
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def add_(self, ctx, name: str.lower, *, value):
        """Add a snippet to the bot config."""
        if 'snippets' not in self.bot.config.cache:
            self.bot.config['snippets'] = {}

        self.bot.config.snippets[name] = value
        await self.bot.config.update()

        embed = discord.Embed(
            title='Added snippet',
            color=self.bot.main_color,
            description=f'`{name}` points to: {value}'
        )

        await ctx.send(embed=embed)

    @snippets.command(name='remove', aliases=['del', 'delete', 'rm'])
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def remove_(self, ctx, *, name: str.lower):
        """Removes a snippet from bot config."""

        if self.bot.config.snippets.get(name):
            embed = discord.Embed(
                title='Removed snippet',
                color=self.bot.main_color,
                description=f'`{name}` no longer exists.'
            )
            del self.bot.config['snippets'][name]
            await self.bot.config.update()

        else:
            embed = discord.Embed(
                title='Error',
                color=discord.Color.red(),
                description=f'Snippet `{name}` does not exist.'
            )

        await ctx.send(embed=embed)

    @commands.command()
    @checks.has_permissions(PermissionLevel.MODERATOR)
    async def move(self, ctx, *, category: discord.CategoryChannel):
        """Moves a thread to a specified category."""
        thread = ctx.thread
        if not thread:
            embed = discord.Embed(
                title='Error',
                description='This is not a Modmail thread.',
                color=discord.Color.red()
            )
            return await ctx.send(embed=embed)

        await thread.channel.edit(category=category, sync_permissions=True)
        await ctx.message.add_reaction('✅')

    @staticmethod
    async def send_scheduled_close_message(ctx, after, silent=False):
        human_delta = human_timedelta(after.dt)

        silent = '*silently* ' if silent else ''

        embed = discord.Embed(
            title='Scheduled close',
            description=f'This thread will close {silent}in {human_delta}.',
            color=discord.Color.red()
        )

        if after.arg and not silent:
            embed.add_field(name='Message', value=after.arg)

        embed.set_footer(text='Closing will be cancelled '
                              'if a thread message is sent.')
        embed.timestamp = after.dt

        await ctx.send(embed=embed)

    @commands.command(usage='[after] [close message]')
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def close(self, ctx, *, after: UserFriendlyTime = None):
        """
        Close the current thread.

        Close after a period of time:
        - `close in 5 hours`
        - `close 2m30s`

        Custom close messages:
        - `close 2 hours The issue has been resolved.`
        - `close We will contact you once we find out more.`

        Silently close a thread (no message)
        - `close silently`
        - `close in 10m silently`

        Stop a thread from closing:
        - `close cancel`
        """

        thread = ctx.thread

        now = datetime.utcnow()

        close_after = (after.dt - now).total_seconds() if after else 0
        message = after.arg if after else None
        silent = str(message).lower() in {'silent', 'silently'}
        cancel = str(message).lower() == 'cancel'

        if cancel:

            if thread.close_task is not None:
                await thread.cancel_closure()
                embed = discord.Embed(color=discord.Color.red(),
                                      description='Scheduled close '
                                                  'has been cancelled.')
            else:
                embed = discord.Embed(
                    color=discord.Color.red(),
                    description='This thread has not already '
                                'been scheduled to close.'
                )

            return await ctx.send(embed=embed)

        if after and after.dt > now:
            await self.send_scheduled_close_message(ctx, after, silent)

        await thread.close(
            closer=ctx.author,
            after=close_after,
            message=message,
            silent=silent,
        )

    @commands.command(aliases=['alert'])
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def notify(self, ctx, *, role=None):
        """
        Notify a given role or yourself to the next thread message received.

        Once a thread message is received you will be pinged once only.
        """
        thread = ctx.thread

        if not role:
            mention = ctx.author.mention
        elif role.lower() in ('here', 'everyone'):
            mention = '@' + role
        else:
            converter = commands.RoleConverter()
            role = await converter.convert(ctx, role)
            mention = role.mention

        if str(thread.id) not in self.bot.config['notification_squad']:
            self.bot.config['notification_squad'][str(thread.id)] = []

        mentions = self.bot.config['notification_squad'][str(thread.id)]

        if mention in mentions:
            embed = discord.Embed(color=discord.Color.red(),
                                  description=f'{mention} is already '
                                  'going to be mentioned.')
        else:
            mentions.append(mention)
            await self.bot.config.update()
            embed = discord.Embed(color=self.bot.main_color,
                                  description=f'{mention} will be mentioned '
                                  'on the next message received.')
        return await ctx.send(embed=embed)

    @commands.command(aliases=['sub'])
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def subscribe(self, ctx, *, role=None):
        """
        Notify yourself or a given role for every thread message received.

        You will be pinged for every thread message
        received until you unsubscribe.
        """
        thread = ctx.thread

        if not role:
            mention = ctx.author.mention
        elif role.lower() in ('here', 'everyone'):
            mention = '@' + role
        else:
            converter = commands.RoleConverter()
            role = await converter.convert(ctx, role)
            mention = role.mention

        if str(thread.id) not in self.bot.config['subscriptions']:
            self.bot.config['subscriptions'][str(thread.id)] = []

        mentions = self.bot.config['subscriptions'][str(thread.id)]

        if mention in mentions:
            embed = discord.Embed(color=discord.Color.red(),
                                  description=f'{mention} is already '
                                  'subscribed to this thread.')
        else:
            mentions.append(mention)
            await self.bot.config.update()
            embed = discord.Embed(
                color=self.bot.main_color,
                description=f'{mention} will now be '
                'notified of all messages received.'
            )
        return await ctx.send(embed=embed)

    @commands.command(aliases=['unsub'])
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def unsubscribe(self, ctx, *, role=None):
        """Unsubscribe yourself or a given role from a thread."""
        thread = ctx.thread

        if not role:
            mention = ctx.author.mention
        elif role.lower() in ('here', 'everyone'):
            mention = '@' + role
        else:
            converter = commands.RoleConverter()
            role = await converter.convert(ctx, role)
            mention = role.mention

        if str(thread.id) not in self.bot.config['subscriptions']:
            self.bot.config['subscriptions'][str(thread.id)] = []

        mentions = self.bot.config['subscriptions'][str(thread.id)]

        if mention not in mentions:
            embed = discord.Embed(color=discord.Color.red(),
                                  description=f'{mention} is not already '
                                  'subscribed to this thread.')
        else:
            mentions.remove(mention)
            await self.bot.config.update()
            embed = discord.Embed(color=self.bot.main_color,
                                  description=f'{mention} is now unsubscribed '
                                  'to this thread.')
        return await ctx.send(embed=embed)

    @commands.command()
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def nsfw(self, ctx):
        """Flags a Modmail thread as nsfw."""
        await ctx.channel.edit(nsfw=True)
        await ctx.message.add_reaction('✅')

    @commands.command()
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def loglink(self, ctx):
        """Return the link to the current thread's logs."""
        log_link = await self.bot.api.get_log_link(ctx.channel.id)
        await ctx.send(
            embed=discord.Embed(
                color=self.bot.main_color,
                description=log_link
            )
        )

    def format_log_embeds(self, logs, avatar_url):
        embeds = []
        logs = tuple(logs)
        title = f'Total Results Found ({len(logs)})'

        for entry in logs:

            key = entry['key']

            created_at = parser.parse(entry['created_at'])

            prefix = os.getenv('LOG_URL_PREFIX', '/logs')
            if prefix == 'NONE':
                prefix = ''


            log_url = self.bot.config.log_url.strip('/') + f'{prefix}/{key}'

            username = entry['recipient']['name'] + '#'
            username += entry['recipient']['discriminator']

            embed = discord.Embed(color=self.bot.main_color,
                                  timestamp=created_at)
            embed.set_author(name=f'{title} - {username}',
                             icon_url=avatar_url,
                             url=log_url)
            embed.url = log_url
            embed.add_field(name='Created',
                            value=duration(created_at, now=datetime.utcnow()))
            embed.add_field(name='Closed By',
                            value=f"<@{entry['closer']['id']}>")

            if entry['recipient']['id'] != entry['creator']['id']:
                embed.add_field(name='Created by',
                                value=f"<@{entry['creator']['id']}>")

            embed.add_field(name='Preview',
                            value=format_preview(entry['messages']),
                            inline=False)
            embed.add_field(name='Link', value=log_url)
            embed.set_footer(
                text='Recipient ID: ' + str(entry['recipient']['id'])
            )
            embeds.append(embed)
        return embeds

    @commands.group(invoke_without_command=True)
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def logs(self, ctx, *, member: User = None):
        """Shows a list of previous Modmail thread logs of a member."""

        await ctx.trigger_typing()

        if not member:
            thread = ctx.thread
            if not thread:
                raise commands.MissingRequiredArgument(param(name='member'))
            user = thread.recipient
        else:
            user = member

        default_avatar = 'https://cdn.discordapp.com/embed/avatars/0.png'
        icon_url = getattr(user, 'avatar_url', default_avatar)

        logs = await self.bot.api.get_user_logs(user.id)

        if not any(not log['open'] for log in logs):
            embed = discord.Embed(color=discord.Color.red(),
                                  description='This user does not '
                                              'have any previous logs.')
            return await ctx.send(embed=embed)

        logs = reversed([e for e in logs if not e['open']])

        embeds = self.format_log_embeds(logs, avatar_url=icon_url)

        session = PaginatorSession(ctx, *embeds)
        await session.run()

    @logs.command(name='closed-by')
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def closed_by(self, ctx, *, user: User = None):
        """Returns all logs closed by a user."""
        user = user or ctx.author

        query = {
            'guild_id': str(self.bot.guild_id),
            'open': False,
            'closer.id': str(user.id)
        }

        projection = {
            'messages': {'$slice': 5}
        }

        entries = await self.bot.db.logs.find(query, projection).to_list(None)

        embeds = self.format_log_embeds(entries,
                                        avatar_url=self.bot.guild.icon_url)

        if not embeds:
            embed = discord.Embed(
                color=discord.Color.red(),
                description='No log entries have been found for that query'
                )
            return await ctx.send(embed=embed)

        session = PaginatorSession(ctx, *embeds)
        await session.run()

    @logs.command()
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def search(self, ctx, limit: Optional[int] = None, *, query):
        """Searches all logs for a message that contains your query."""

        await ctx.trigger_typing()

        query = {
            'guild_id': str(self.bot.guild_id),
            'open': False,
            '$text': {
                '$search': f'"{query}"'
                }
        }

        projection = {
            'messages': {'$slice': 5}
        }

        entries = await self.bot.db.logs.find(query, projection).to_list(limit)

        embeds = self.format_log_embeds(entries,
                                        avatar_url=self.bot.guild.icon_url)

        if not embeds:
            embed = discord.Embed(
                color=discord.Color.red(),
                description='No log entries have been found for that query'
                )
            return await ctx.send(embed=embed)

        session = PaginatorSession(ctx, *embeds)
        await session.run()

    @commands.command()
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def reply(self, ctx, *, msg=''):
        """Reply to users using this command.

        Supports attachments and images as well as
        automatically embedding image URLs.
        """
        ctx.message.content = msg
        async with ctx.typing():
            await ctx.thread.reply(ctx.message)

    @commands.command()
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def anonreply(self, ctx, *, msg=''):
        """Reply to a thread anonymously.

        You can edit the anonymous user's name,
        avatar and tag using the config command.

        Edit the `anon_username`, `anon_avatar_url`
        and `anon_tag` config variables to do so.
        """
        ctx.message.content = msg
        async with ctx.typing():
            await ctx.thread.reply(ctx.message, anonymous=True)

    @commands.command()
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def note(self, ctx, *, msg=''):
        """Take a note about the current thread, useful for noting context."""
        ctx.message.content = msg
        async with ctx.typing():
            await ctx.thread.note(ctx.message)

    async def find_linked_message(self, ctx, message_id):
        linked_message_id = None

        async for msg in ctx.channel.history():
            if message_id is None and msg.embeds:
                embed = msg.embeds[0]
                if isinstance(self.bot.mod_color, discord.Color):
                    mod_color = self.bot.mod_color.value
                else:
                    mod_color = self.bot.mod_color
                if embed.color.value != mod_color or not embed.author.url:
                    continue
                # TODO: use regex to find the linked message id
                linked_message_id = str(embed.author.url).split('/')[-1]
                break
            elif message_id and msg.id == message_id:
                url = msg.embeds[0].author.url
                linked_message_id = str(url).split('/')[-1]
                break

        return linked_message_id

    @commands.command()
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def edit(self, ctx, message_id: Optional[int] = None,
                   *, new_message):
        """Edit a message that was sent using the reply command.

        If no `message_id` is provided, the
        last message sent by a mod will be edited.

        `[message_id]` the id of the message that you want to edit.
        `new_message` is the new message that will be edited in.
        """
        thread = ctx.thread

        linked_message_id = await self.find_linked_message(ctx, message_id)

        if linked_message_id is None:
            return await ctx.send(embed=discord.Embed(
                title='Failed',
                description='Cannot find a message to edit.',
                color=discord.Color.red()
            ))

        await asyncio.gather(
            thread.edit_message(linked_message_id, new_message),
            self.bot.api.edit_message(linked_message_id, new_message)
        )

        await ctx.message.add_reaction('✅')

    @commands.command()
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @trigger_typing
    async def contact(self, ctx,
                      category: Optional[discord.CategoryChannel] = None, *,
                      user: Union[discord.Member, discord.User]):
        """Create a thread with a specified member.

        If the optional category argument is passed, the thread
        will be created in the specified category.
        """

        if user.bot:
            embed = discord.Embed(
                color=discord.Color.red(),
                description='Cannot start a thread with a bot.'
            )
            return await ctx.send(embed=embed)

        exists = await self.bot.threads.find(recipient=user)
        if exists:
            embed = discord.Embed(
                color=discord.Color.red(),
                description='A thread for this user already '
                            f'exists in {exists.channel.mention}.'
            )

        else:
            thread = self.bot.threads.create(user, creator=ctx.author,
                                             category=category)
            await thread.wait_until_ready()
            embed = discord.Embed(
                title='Created thread',
                description=f'Thread started in {thread.channel.mention} '
                f'for {user.mention}.',
                color=self.bot.main_color
            )

        await ctx.send(embed=embed)

    @commands.command()
    @checks.has_permissions(PermissionLevel.MODERATOR)
    @trigger_typing
    async def blocked(self, ctx):
        """Returns a list of blocked users"""
        embed = discord.Embed(title='Blocked Users',
                              color=self.bot.main_color,
                              description='Here is a list of blocked users.')

        users = []
        not_reachable = []

        for id_, reason in self.bot.blocked_users.items():
            user = self.bot.get_user(int(id_))
            if user:
                users.append((user, reason))
            else:
                not_reachable.append((id_, reason))

        if users:
            val = '\n'.join(u.mention + (f' - `{r}`' if r else '')
                            for u, r in users)
            embed.add_field(name='Currently Known', value=val)
        if not_reachable:
            val = '\n'.join(f'`{i}`' + (f' - `{r}`' if r else '')
                            for i, r in not_reachable)
            embed.add_field(name='Unknown', value=val, inline=False)

        if not users and not not_reachable:
            embed.description = 'Currently there are no blocked users.'

        await ctx.send(embed=embed)

    @commands.command()
    @checks.has_permissions(PermissionLevel.MODERATOR)
    @trigger_typing
    async def block(self, ctx, user: Optional[User] = None, *,
                    after: UserFriendlyTime = None):
        """
        Block a user from using Modmail.

        Note: reasons that start with "System Message: " are reserved for internal
        use only.
        """
        reason = ''

        if user is None:
            thread = ctx.thread
            if thread:
                user = thread.recipient
            elif after is None:
                raise commands.MissingRequiredArgument(param(name='user'))
            else:
                raise commands.BadArgument(f'User "{after.arg}" not found')

        if after is not None:
            reason = after.arg
            if reason.startswith('System Message: '):
                raise commands.BadArgument('The reason cannot start with `System Message:`.')
            if re.search(r'%(.+?)%$', reason) is not None:
                raise commands.MissingRequiredArgument(param(name='reason'))
            if after.dt > after.now:
                reason = f'{reason} %{after.dt.isoformat()}%'

        if not reason:
            reason = None

        mention = user.mention if hasattr(user, 'mention') else f'`{user.id}`'

        extend = f' for `{reason}`' if reason is not None else ''
        msg = self.bot.blocked_users.get(str(user.id))
        if msg is None:
            msg = ''

        if str(user.id) not in self.bot.blocked_users or extend or msg.startswith('System Message: '):
            if str(user.id) in self.bot.blocked_users:

                old_reason = msg.strip().rstrip('.') or 'no reason'
                embed = discord.Embed(
                    title='Success',
                    description=f'{mention} was previously blocked for '
                    f'"{old_reason}". {mention} is now blocked{extend}.',
                    color=self.bot.main_color
                )
            else:
                embed = discord.Embed(
                    title='Success',
                    color=self.bot.main_color,
                    description=f'{mention} is now blocked{extend}.'
                )
            self.bot.config.blocked[str(user.id)] = reason
            await self.bot.config.update()
        else:
            embed = discord.Embed(
                title='Error',
                color=discord.Color.red(),
                description=f'{mention} is already blocked.'
            )

        return await ctx.send(embed=embed)

    @commands.command()
    @checks.has_permissions(PermissionLevel.MODERATOR)
    @trigger_typing
    async def unblock(self, ctx, *, user: User = None):
        """
        Unblocks a user from using Modmail.

        Note: reasons start with "System Message: " are reserved for internal
        use only.
        """


        if user is None:
            thread = ctx.thread
            if thread:
                user = thread.recipient
            else:
                raise commands.MissingRequiredArgument(param(name='user'))

        mention = user.mention if hasattr(user, 'mention') else f'`{user.id}`'

        if str(user.id) in self.bot.blocked_users:
            msg = self.bot.blocked_users.get(str(user.id))
            if msg is None:
                msg = ''
            del self.bot.config.blocked[str(user.id)]
            await self.bot.config.update()

            if msg.startswith('System Message: '):
                # If the user is blocked internally (for example: below minimum account age)
                # Show an extended message stating the original internal message
                reason = msg[16:].strip().rstrip('.') or 'no reason'
                embed = discord.Embed(
                    title='Success',
                    description=f'{mention} was previously blocked internally due to '
                    f'"{reason}". {mention} is no longer blocked.',
                    color=self.bot.main_color
                )
            else:
                embed = discord.Embed(
                    title='Success',
                    color=self.bot.main_color,
                    description=f'{mention} is no longer blocked.'
                )
        else:
            embed = discord.Embed(
                title='Error',
                description=f'{mention} is not blocked.',
                color=discord.Color.red()
            )

        return await ctx.send(embed=embed)

    @commands.command()
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def delete(self, ctx, message_id = None):
        """Delete a message that was sent using the reply command.

        Deletes the previous message, unless a message ID is provided, which in that case,
        deletes the message with that message ID.
        """
        thread = ctx.thread

        try:
            message_id = int(message_id)
        except ValueError:
            raise commands.BadArgument('An integer message ID needs to be specified.')

        linked_message_id = await self.find_linked_message(ctx, message_id)

        if linked_message_id is None:
            return await ctx.send(embed=discord.Embed(
                title='Failed',
                description='Cannot find a message to delete.',
                color=discord.Color.red()
            ))

        await thread.delete_message(linked_message_id)
        await ctx.message.add_reaction('✅')


def setup(bot):
    bot.add_cog(Modmail(bot))
