from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from radar.core import DB, UTC, matches, parse_money, parse_offer, period_start
from radar.worker import Worker
from radar.app import Bot


@pytest.fixture
def db(tmp_path):
    database = DB(tmp_path / 'radar.db')
    database.add_channel('canal', -1001234567890, 'Canal de testes')
    yield database
    database.conn.close()


def test_seed_and_dynamic_products(db):
    assert len(db.products()) == 2
    assert all(p['target'] is None for p in db.products())
    pid = db.add('Teste celular')
    db.execute('UPDATE products SET active=0 WHERE id=?', (pid,))
    assert db.product(pid) is None
    assert db.add('Teste celular') == pid
    assert db.product(pid)


def test_price_and_matching():
    assert parse_money('R$ 2.499,90') == 249990
    assert matches('Motorola Edge 70 Pro', 'Oferta MOTOROLA Edge 70 Pro!')
    assert not matches('Motorola Edge 70 Pro', 'Motorola Edge 70 ProMax')
    assert parse_offer('Motorola Edge 70 Pro 12x 250') is None
    assert parse_offer('R$ 2.499,90\nLoja: Amazon\nCupom: TESTE') == (249990, 'Amazon', 'TESTE')
    with pytest.raises(ValueError):
        parse_money('-20')


def test_coverage_gaps_and_report(db):
    pid = db.products()[0]['id']
    db.execute('INSERT INTO coverage VALUES (?,?,?,?)', (pid, 'canal', 20, 40))
    db.execute('INSERT INTO coverage VALUES (?,?,?,?)', (pid, 'canal', 35, 60))
    assert db.missing(pid, 'canal', 10, 80) == [(10, 20), (60, 80)]
    assert 'incompleto' in db.report(pid, 10, 80, ['canal'])
    assert db.enqueue(pid, 20, 60, ['canal']) is None


def test_dedupe_statistics_and_notifications(db):
    p = db.products()[0]
    for mid, price in [(1, 10000), (2, 8000), (3, 9000)]:
        assert db.record(p, 'canal', mid, mid * 10, (price, 'Loja', 'CUPOM'), f'https://t.me/canal/{mid}', live=True)
    assert not db.record(p, 'canal', 1, 10, (10000, None, None), 'url', live=True)
    report = db.report(p['id'], 0, 100, ['canal'])
    assert 'Ofertas: 3' in report and 'R$ 80,00' in report and 'Preço médio: R$ 90,00' in report
    assert 'Último preço: R$ 90,00' in report
    assert db.conn.execute('SELECT COUNT(*) FROM outbox').fetchone()[0] == 3
    db.execute('UPDATE products SET target=7000 WHERE id=?', (p['id'],))
    p = db.product(p['id'])
    db.record(p, 'canal', 4, 40, (8500, None, None), 'url', live=True)
    assert db.conn.execute('SELECT COUNT(*) FROM outbox').fetchone()[0] == 3
    db.record(p, 'canal', 5, 50, (7500, None, None), 'url', live=True)
    assert db.conn.execute('SELECT COUNT(*) FROM outbox').fetchone()[0] == 4


class FakeClient:
    def __init__(self, fail=False):
        self.fail = fail
        self.offsets = []

    async def iter_messages(self, channel, **kwargs):
        self.offsets.append(kwargs.get('offset_id', 0))
        for mid, timestamp in [(3, 90), (2, 60), (1, 20)]:
            if kwargs.get('offset_id') and mid >= kwargs['offset_id']:
                continue
            yield SimpleNamespace(id=mid, date=datetime.fromtimestamp(timestamp, UTC), raw_text='Motorola Edge 70 Pro R$ 100,00')
            if self.fail:
                raise RuntimeError('rede caiu')


async def test_resume_and_cached_import(db):
    p = next(p for p in db.products() if 'Edge' in p['name'])
    job = db.enqueue(p['id'], 30, 100, ['canal'])
    client = FakeClient(fail=True)
    worker = Worker(db, client, AsyncMock(), 1, delay=0)
    with pytest.raises(RuntimeError):
        await worker.step()
    assert db.conn.execute('SELECT cursor FROM scans').fetchone()[0] == 3
    assert db.missing(p['id'], 'canal', 30, 100)
    client.fail = False
    await worker.step()
    assert client.offsets == [0, 3]
    assert db.conn.execute('SELECT status FROM jobs WHERE id=?', (job,)).fetchone()[0] == 'done'
    assert db.conn.execute('SELECT COUNT(*) FROM offers').fetchone()[0] == 2
    assert db.enqueue(p['id'], 30, 100, ['canal']) is None


async def test_cancelled_job_not_scanned(db):
    p = db.products()[0]
    db.enqueue(p['id'], 30, 100, ['canal'])
    db.execute("UPDATE jobs SET status='cancelled'")
    client = FakeClient()
    assert not await Worker(db, client, AsyncMock(), 1).step()
    assert not client.offsets


async def test_unauthorized_command_and_callback(db):
    bot = Bot(db, 1, None)
    message = SimpleNamespace(text='/adicionar', reply_text=AsyncMock())
    update = SimpleNamespace(effective_user=SimpleNamespace(id=2), effective_chat=SimpleNamespace(type='private'), message=message, effective_message=message, callback_query=SimpleNamespace(answer=AsyncMock()))
    await bot.command(update, None)
    await bot.callback(update, None)
    assert not bot.sessions
    message.reply_text.assert_not_called()
    update.callback_query.answer.assert_awaited_once()


async def test_add_requires_period_before_import(db):
    bot = Bot(db, 1, None)
    message = SimpleNamespace(text='/adicionar', reply_text=AsyncMock())
    update = SimpleNamespace(effective_user=SimpleNamespace(id=1), effective_chat=SimpleNamespace(type='private'), message=message, effective_message=message)
    await bot.command(update, None)
    message.text = 'Produto novo'
    await bot.text(update, None)
    assert bot.sessions[1]['stage'] == 'import'
    assert db.conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 0
    update.callback_query = SimpleNamespace(data='import:yes', answer=AsyncMock())
    await bot.callback(update, None)
    assert bot.sessions[1]['stage'] == 'period'
    assert db.conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 0
    update.callback_query.data = 'period:30'
    await bot.callback(update, None)
    assert db.conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 1


def test_calendar_period():
    end = int(datetime(2024, 2, 29, tzinfo=UTC).timestamp())
    assert datetime.fromtimestamp(period_start('1y', end), UTC).date().isoformat() == '2023-02-28'
