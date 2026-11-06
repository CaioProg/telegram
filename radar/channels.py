"""Validation of public channels using the authenticated reader account."""
import logging
import re

from telethon import utils
from telethon.errors import FloodWaitError, RPCError, UserNotParticipantError

log = logging.getLogger(__name__)


def channel_username(text):
    value = text.strip()
    value = re.sub(r'^(?:https?://)?(?:www\.)?t\.me/', '', value, flags=re.I)
    value = value.removeprefix('@').removesuffix('/')
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{3,31}', value):
        raise ValueError('Envie @username ou https://t.me/username de um canal público, sem link de mensagem ou convite.')
    return value.lower()


class ChannelManager:
    def __init__(self, db, client):
        self.db, self.client = db, client

    async def add(self, text, current=lambda: True):
        username = channel_username(text)
        try:
            entity = await self.client.get_entity(username)
            if not getattr(entity, 'broadcast', False) or not getattr(entity, 'username', None):
                raise ValueError('Use um canal público de transmissão. Grupos, usuários e bots não são aceitos.')
            permissions = await self.client.get_permissions(entity, 'me')
            if permissions.has_left or permissions.is_banned:
                raise UserNotParticipantError(request=None)
        except FloodWaitError as exc:
            raise ValueError(f'O Telegram limitou as consultas. Tente novamente em {exc.seconds} segundos.') from exc
        except UserNotParticipantError as exc:
            raise ValueError('A conta leitora ainda não participa desse canal. Entre no canal pelo Telegram dessa conta e tente novamente.') from exc
        except (RPCError, OSError, ValueError) as exc:
            if isinstance(exc, ValueError) and str(exc).startswith('Use um canal público'):
                raise
            raise ValueError('Não foi possível validar o canal. Confira o username e o acesso da conta leitora e tente novamente.') from exc
        if not current():
            return None
        canonical = channel_username(entity.username)
        return self.db.add_channel(canonical, utils.get_peer_id(entity), entity.title)

    async def bootstrap(self, raw):
        # Legacy env is consumed exactly once, so removing a channel survives restarts.
        if self.db.conn.execute("SELECT 1 FROM settings WHERE key='channels_env_imported'").fetchone():
            return
        for value in dict.fromkeys(item.strip() for item in raw.split(',') if item.strip()):
            try:
                await self.add(value)
            except ValueError as exc:
                log.warning('Canal legado não importado: %s', exc)
        self.db.execute("INSERT INTO settings VALUES ('channels_env_imported','1')")
