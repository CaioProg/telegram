import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon.errors import FloodWaitError, UserNotParticipantError
from telethon.tl.types import Channel

from radar.app import Bot, record_live
from radar.channels import ChannelManager, channel_username
from radar.core import DB, UTC
from radar.worker import Worker


@pytest.fixture
def db(tmp_path):
    database = DB(tmp_path / 'radar.db')
    yield database
    database.conn.close()


def entity(username='ofertas', cid=123):
    return Channel(id=cid, title='Ofertas', photo=None, date=None, broadcast=True, username=username)


def manager(db):
    client = SimpleNamespace(get_entity=AsyncMock(return_value=entity()), get_permissions=AsyncMock(return_value=SimpleNamespace(has_left=False, is_banned=False)))
    return ChannelManager(db, client)


def update(text, uid=1, chat_type='private'):
    message = SimpleNamespace(text=text, reply_text=AsyncMock(return_value=SimpleNamespace(message_id=10)))
    return SimpleNamespace(effective_user=SimpleNamespace(id=uid), effective_chat=SimpleNamespace(type=chat_type), message=message, effective_message=message)


def callback(u, data, message_id=10):
    u.callback_query = SimpleNamespace(data=data, message=SimpleNamespace(message_id=message_id), answer=AsyncMock())
    return u


@pytest.mark.parametrize('value', ['@Ofertas', 'ofertas', 'https://t.me/ofertas', 't.me/ofertas/'])
def test_username(value):
    assert channel_username(value) == 'ofertas'


@pytest.mark.parametrize('value', ['https://evil.test/ofertas', 'https://t.me/+abc', 'https://t.me/ofertas/12', '123', 'a b', 'https://t.me/joinchat/abc'])
def test_invalid_username(value):
    with pytest.raises(ValueError):
        channel_username(value)


async def test_interactive_add_persists_without_import(db):
    bot = Bot(db, 1, manager(db))
    u = update('/adicionarcanal')
    await bot.command(u, None)
    assert bot.sessions[1]['stage'] == 'channel_name'
    u.message.text = 'https://t.me/Ofertas'
    await bot.text(u, None)
    assert db.channel_names() == ['ofertas']
    assert not bot.sessions
    assert db.conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 0
    cid = db.channels()[0]['id']
    assert db.channel_for_peer(-1000000000123)['id'] == cid
    # Duplicate registration does not create another channel.
    await bot.channel_manager.add('@ofertas')
    assert len(db.channels()) == 1


async def test_remove_confirm_and_reactivate_keeps_history(db):
    registry = manager(db)
    cid = await registry.add('@ofertas')
    p = db.products()[0]
    db.record(p, 'ofertas', 1, 50, (10000, None, None), 'https://t.me/ofertas/1')
    job = db.enqueue(p['id'], 0, 100, db.channel_names())
    bot = Bot(db, 1, registry)
    u = update('/removercanal')
    await bot.command(u, None)
    await bot.callback(callback(u, f'channel:{cid}'), None)
    assert db.channel(cid)  # Selecting is not enough; explicit confirmation required.
    await bot.callback(callback(u, 'channel_remove:yes'), None)
    assert db.channel_names() == []
    assert db.channel_for_peer(-1000000000123) is None
    assert db.conn.execute('SELECT status FROM jobs WHERE id=?', (job,)).fetchone()[0] == 'cancelled'
    assert db.conn.execute('SELECT COUNT(*) FROM offers').fetchone()[0] == 1
    assert await registry.add('@ofertas') == cid
    assert len(db.channels()) == 1


async def test_remove_declined_and_expired_keyboard(db):
    registry = manager(db)
    cid = await registry.add('ofertas')
    bot = Bot(db, 1, registry)
    u = update('/removercanal')
    await bot.command(u, None)
    await bot.callback(callback(u, f'channel:{cid}', message_id=9), None)
    assert bot.sessions[1]['stage'] == 'channel_select'
    await bot.callback(callback(u, f'channel:{cid}'), None)
    await bot.callback(callback(u, 'channel_remove:no'), None)
    assert db.channel(cid)


@pytest.mark.parametrize('text', ['/adicionarcanal', '/removercanal', '/listarcanais'])
@pytest.mark.parametrize('uid,chat_type', [(2, 'private'), (1, 'group')])
async def test_channel_commands_restricted(db, text, uid, chat_type):
    registry = manager(db)
    bot = Bot(db, 1, registry)
    u = update(text, uid, chat_type)
    await bot.command(u, None)
    await bot.text(u, None)
    await bot.callback(callback(u, 'channel_remove:yes'), None)
    assert not bot.sessions
    registry.client.get_entity.assert_not_awaited()
    u.message.reply_text.assert_not_called()
    assert db.channel_names() == []


async def test_validation_errors_allow_retry(db):
    registry = manager(db)
    bot = Bot(db, 1, registry)
    u = update('/adicionarcanal')
    await bot.command(u, None)
    registry.client.get_permissions.side_effect = UserNotParticipantError(request=None)
    u.message.text = '@ofertas'
    await bot.text(u, None)
    assert 'não participa' in u.message.reply_text.await_args.args[0]
    assert bot.sessions[1]['stage'] == 'channel_name'
    assert db.channels() == []
    registry.client.get_permissions.side_effect = None
    await bot.text(u, None)
    assert db.channel_names() == ['ofertas']


async def test_rejects_basic_groups_and_handles_flood_wait(db):
    registry = manager(db)
    registry.client.get_entity.return_value = SimpleNamespace(broadcast=False)
    with pytest.raises(ValueError, match='Grupos'):
        await registry.add('ofertas')
    registry.client.get_entity.side_effect = FloodWaitError(request=None, capture=30)
    with pytest.raises(ValueError, match='30 segundos'):
        await registry.add('ofertas')
    assert db.channels() == []


async def test_public_supergroup_supports_registration_live_and_history(db):
    registry = manager(db)
    registry.client.get_entity.return_value = Channel(id=456, title='Grupo de ofertas', photo=None, date=None, broadcast=False, megagroup=True, username='CanalJMS_Descontos')
    bot = Bot(db, 1, registry)
    u = update('/adicionarcanal')
    await bot.command(u, None)
    u.message.text = '@CanalJMS_Descontos'
    await bot.text(u, None)
    assert db.channel_names() == ['canaljms_descontos']
    assert not bot.sessions
    assert db.conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 0
    p = next(p for p in db.products() if 'Edge' in p['name'])
    event = SimpleNamespace(chat_id=-1000000000456, id=10, date=datetime.fromtimestamp(80, UTC), raw_text='Motorola Edge 70 Pro R$ 100,00')
    record_live(db, event)
    assert db.conn.execute('SELECT channel FROM offers').fetchone()[0] == 'canaljms_descontos'

    class Client:
        async def iter_messages(self, channel, **kwargs):
            assert channel == 'canaljms_descontos'
            yield SimpleNamespace(id=9, date=datetime.fromtimestamp(70, UTC), raw_text=event.raw_text)

    db.enqueue(p['id'], 30, 100, db.channel_names())
    await Worker(db, Client(), AsyncMock(), 1, delay=0).step()
    assert db.conn.execute('SELECT COUNT(*) FROM offers').fetchone()[0] == 2
    assert db.conn.execute('SELECT status FROM jobs').fetchone()[0] == 'done'


@pytest.mark.parametrize('username,left,banned', [(None, False, False), ('ofertas', True, False), ('ofertas', False, True)])
async def test_supergroup_requires_public_username_and_membership(db, username, left, banned):
    registry = manager(db)
    registry.client.get_entity.return_value = Channel(id=456, title='Grupo', photo=None, date=None, megagroup=True, username=username)
    registry.client.get_permissions.return_value = SimpleNamespace(has_left=left, is_banned=banned)
    with pytest.raises(ValueError):
        await registry.add('ofertas')
    assert db.channels() == []


@pytest.mark.parametrize('entity_type', ['user', 'bot'])
async def test_users_and_bots_are_rejected(db, entity_type):
    from telethon.tl.types import User
    registry = manager(db)
    registry.client.get_entity.return_value = User(id=456, username='ofertas', bot=entity_type == 'bot')
    with pytest.raises(ValueError, match='usuários e bots'):
        await registry.add('ofertas')
    registry.client.get_permissions.assert_not_awaited()
    assert db.channels() == []


async def test_cancel_while_validation_in_flight(db):
    registry = manager(db)
    ready, release = asyncio.Event(), asyncio.Event()

    async def permissions(*args):
        ready.set()
        await release.wait()
        return SimpleNamespace(has_left=False, is_banned=False)

    registry.client.get_permissions.side_effect = permissions
    bot = Bot(db, 1, registry)
    u = update('/adicionarcanal')
    await bot.command(u, None)
    u.message.text = 'ofertas'
    task = asyncio.create_task(bot.text(u, None))
    await ready.wait()
    await bot.command(update('/cancelar'), None)
    release.set()
    await task
    assert db.channels() == []


async def test_legacy_env_migrated_once_survives_restart(tmp_path):
    path = tmp_path / 'radar.db'
    db = DB(path)
    registry = manager(db)
    await registry.bootstrap('@ofertas')
    cid = db.channels()[0]['id']
    db.remove_channel(cid)
    db.conn.close()
    db = DB(path)
    registry = manager(db)
    await registry.bootstrap('@ofertas')
    assert db.channels() == []
    registry.client.get_entity.assert_not_awaited()
    db.conn.close()


async def test_starts_without_channels_and_shows_empty_state(db):
    registry = manager(db)
    await registry.bootstrap('')
    assert db.channels() == []
    bot = Bot(db, 1, registry)
    u = update('/listarcanais')
    await bot.command(u, None)
    assert 'Nenhum canal' in u.message.reply_text.await_args.args[0]
    await bot.query(u, {'product': db.products()[0]['id']}, 0, 100)
    assert 'Nenhum canal ativo' in u.message.reply_text.await_args.args[0]
    assert db.conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 0


async def test_remove_channel_during_scan_does_not_complete_coverage(db):
    cid = await manager(db).add('ofertas')
    p = next(p for p in db.products() if 'Edge' in p['name'])
    job = db.enqueue(p['id'], 30, 100, db.channel_names())

    class Client:
        async def iter_messages(self, channel, **kwargs):
            yield SimpleNamespace(id=2, date=datetime.fromtimestamp(80, UTC), raw_text='Motorola Edge 70 Pro R$ 100,00')
            db.remove_channel(cid)
            yield SimpleNamespace(id=1, date=datetime.fromtimestamp(60, UTC), raw_text='Motorola Edge 70 Pro R$ 80,00')

    await Worker(db, Client(), AsyncMock(), 1).step()
    assert db.conn.execute('SELECT status FROM jobs WHERE id=?', (job,)).fetchone()[0] == 'cancelled'
    assert db.conn.execute('SELECT COUNT(*) FROM coverage').fetchone()[0] == 0
    assert db.conn.execute('SELECT COUNT(*) FROM offers').fetchone()[0] == 1


async def test_username_change_keeps_history_and_channel_identity(db):
    registry = manager(db)
    cid = await registry.add('ofertas')
    p = db.products()[0]
    db.record(p, 'ofertas', 1, 50, (10000, None, None), 'https://t.me/ofertas/1')
    registry.client.get_entity.return_value = entity('ofertasnovas')
    assert await registry.add('ofertasnovas') == cid
    assert db.channel_names() == ['ofertasnovas']
    assert db.conn.execute('SELECT channel FROM offers').fetchone()[0] == 'ofertasnovas'


async def test_live_monitoring_uses_current_database_without_restart(db):
    registry = manager(db)
    event = SimpleNamespace(chat_id=-1000000000123, id=1, date=datetime.fromtimestamp(50, UTC), raw_text='Motorola Edge 70 Pro R$ 100,00')
    record_live(db, event)
    assert db.conn.execute('SELECT COUNT(*) FROM offers').fetchone()[0] == 0
    cid = await registry.add('ofertas')
    record_live(db, event)
    assert db.conn.execute('SELECT COUNT(*) FROM offers').fetchone()[0] == 1
    assert db.conn.execute('SELECT COUNT(*) FROM outbox').fetchone()[0] == 1
    db.remove_channel(cid)
    event.id = 2
    record_live(db, event)
    assert db.conn.execute('SELECT COUNT(*) FROM offers').fetchone()[0] == 1
    await registry.add('ofertas')
    record_live(db, event)
    assert db.conn.execute('SELECT COUNT(*) FROM offers').fetchone()[0] == 2


async def test_left_account_cannot_register_channel(db):
    registry = manager(db)
    registry.client.get_permissions.return_value = SimpleNamespace(has_left=True, is_banned=False)
    with pytest.raises(ValueError, match='não participa'):
        await registry.add('ofertas')
    assert db.channels() == []


def test_additive_migration_preserves_old_database(tmp_path):
    import sqlite3
    path = tmp_path / 'legacy.db'
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE products(id INTEGER PRIMARY KEY, name TEXT NOT NULL, normalized TEXT UNIQUE NOT NULL, target INTEGER, active INTEGER NOT NULL DEFAULT 1)')
    conn.execute("INSERT INTO products VALUES (1,'Produto antigo','produto antigo',12345,1)")
    conn.commit()
    conn.close()
    db = DB(path)
    assert db.product(1)['target'] == 12345
    db.add_channel('ofertas', -1000000000123, 'Ofertas')
    db.conn.close()
    db = DB(path)
    assert db.channel_names() == ['ofertas']
    assert db.product(1)['name'] == 'Produto antigo'
    db.conn.close()


async def test_legacy_mixed_case_history_is_reused(db):
    p = db.products()[0]
    db.record(p, 'Ofertas', 1, 50, (10000, None, None), 'https://t.me/Ofertas/1')
    db.execute('INSERT INTO coverage VALUES (?,?,?,?)', (p['id'], 'Ofertas', 0, 100))
    await manager(db).bootstrap('Ofertas')
    assert db.enqueue(p['id'], 0, 100, db.channel_names()) is None
    assert not db.record(p, 'ofertas', 1, 50, (10000, None, None), 'https://t.me/ofertas/1')
    assert db.conn.execute('SELECT COUNT(*) FROM offers').fetchone()[0] == 1


def test_reassigned_username_does_not_mix_channel_histories(db):
    original = db.add_channel('ofertas', -1000000000123, 'Original')
    db.add_channel('outrocanal', -1000000000456, 'Outro')
    with pytest.raises(ValueError, match='outro canal'):
        db.add_channel('outrocanal', -1000000000123, 'Renomeado')
    assert db.channel(original)['username'] == 'ofertas'
    assert len(db.channels()) == 2
