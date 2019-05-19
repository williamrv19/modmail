import asyncio
import json
import os
import typing

import isodate

from discord.ext.commands import BadArgument

from core._color_data import ALL_COLORS
from core.models import InvalidConfigError
from core.time import UserFriendlyTime


class ConfigManager:

    allowed_to_change_in_command = {
        # activity
        'twitch_url',

        # bot settings
        'main_category_id', 'disable_autoupdates', 'prefix', 'mention',
        'main_color', 'user_typing', 'mod_typing', 'account_age', 'guild_age',
        'reply_without_command',

        # logging
        'log_channel_id',

        # threads
        'sent_emoji', 'blocked_emoji', 'close_emoji', 'disable_recipient_thread_close',
        'thread_creation_response', 'thread_creation_footer', 'thread_creation_title',
        'thread_close_footer', 'thread_close_title', 'thread_close_response',
        'thread_self_close_response',

        # moderation
        'recipient_color', 'mod_tag', 'mod_color',

        # anonymous message
        'anon_username', 'anon_avatar_url', 'anon_tag'
    }

    internal_keys = {
        # bot presence
        'activity_message', 'activity_type', 'status', 'oauth_whitelist',

        # moderation
        'blocked', 'command_permissions', 'level_permissions',

        # threads
        'snippets', 'notification_squad', 'subscriptions', 'closures',

        # misc
        'aliases', 'plugins'
    }

    protected_keys = {
        # Modmail
        'modmail_guild_id', 'guild_id',
        'log_url', 'mongo_uri', 'owners',

        # bot
        'token',

        # GitHub
        'github_access_token',

        # Logging
        'log_level'
    }

    colors = {
        'mod_color', 'recipient_color', 'main_color'
    }

    time_deltas = {
        'account_age', 'guild_age'
    }

    valid_keys = allowed_to_change_in_command | internal_keys | protected_keys

    def __init__(self, bot):
        self.bot = bot
        self._cache = {}
        self._ready_event = asyncio.Event()
        self.populate_cache()

    def __repr__(self):
        return repr(self.cache)

    @property
    def api(self):
        return self.bot.api

    @property
    def ready_event(self) -> asyncio.Event:
        return self._ready_event

    @property
    def cache(self) -> dict:
        return self._cache

    @cache.setter
    def cache(self, val: dict):
        self._cache = val

    def populate_cache(self) -> dict:
        data = {
            'snippets': {},
            'plugins': [],
            'aliases': {},
            'blocked': {},
            'oauth_whitelist': [],
            'command_permissions': {},
            'level_permissions': {},
            'notification_squad': {},
            'subscriptions': {},
            'closures': {},
            'log_level': 'INFO'
        }

        data.update(os.environ)

        if os.path.exists('config.json'):
            with open('config.json') as f:
                # Config json should override env vars
                data.update(json.load(f))

        self.cache = {
            k.lower(): v for k, v in data.items()
            if k.lower() in self.valid_keys
        }
        return self.cache

    async def clean_data(self, key: str,
                         val: typing.Any) -> typing.Tuple[str, str]:
        value_text = val
        clean_value = val

        # when setting a color
        if key in self.colors:
            hex_ = ALL_COLORS.get(val)

            if hex_ is None:
                if not isinstance(val, str):
                    raise InvalidConfigError('Invalid color name or hex.')
                if val.startswith('#'):
                    val = val[1:]
                if len(val) != 6:
                    raise InvalidConfigError('Invalid color name or hex.')
                for letter in val:
                    if letter not in {'0', '1', '2', '3', '4', '5', '6', '7',
                                      '8', '9', 'a', 'b', 'c', 'd', 'e', 'f'}:
                        raise InvalidConfigError('Invalid color name or hex.')
                clean_value = '#' + val
                value_text = clean_value
            else:
                clean_value = hex_
                value_text = f'{val} ({clean_value})'

        elif key in self.time_deltas:
            try:
                isodate.parse_duration(val)
            except isodate.ISO8601Error:
                try:
                    converter = UserFriendlyTime()
                    time = await converter.convert(None, val)
                    if time.arg:
                        raise ValueError
                except BadArgument as exc:
                    raise InvalidConfigError(*exc.args)
                except Exception:
                    raise InvalidConfigError(
                        'Unrecognized time, please use ISO-8601 duration format '
                        'string or a simpler "human readable" time.'
                    )
                clean_value = isodate.duration_isoformat(time.dt - converter.now)
                value_text = f'{val} ({clean_value})'

        return clean_value, value_text

    async def update(self, data: typing.Optional[dict] = None) -> dict:
        """Updates the config with data from the cache"""
        if data is not None:
            self.cache.update(data)
        await self.api.update_config(self.cache)
        return self.cache

    async def refresh(self) -> dict:
        """Refreshes internal cache with data from database"""
        data = await self.api.get_config()
        self.cache.update(data)
        self.ready_event.set()
        return self.cache

    async def wait_until_ready(self) -> None:
        await self.ready_event.wait()

    def __getattr__(self, value: str) -> typing.Any:
        return self.cache[value]

    def __setitem__(self, key: str, item: typing.Any) -> None:
        self.cache[key] = item

    def __getitem__(self, key: str) -> typing.Any:
        return self.cache[key]

    def get(self, key: str, default: typing.Any = None) -> typing.Any:
        return self.cache.get(key, default)
