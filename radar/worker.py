import asyncio
import logging
from datetime import datetime
from telethon.errors import FloodWaitError
from .core import UTC, matches, parse_offer

log = logging.getLogger(__name__)


def message_url(channel, message):
    return f'https://t.me/{channel}/{message}'


class Worker:
    def __init__(self, db, client, bot, admin, delay=0.4):
        self.db, self.client, self.bot = db, client, bot
        self.admin, self.delay = admin, delay

    async def scan(self, scan, product):
        # Percorre da mais recente à mais antiga e salva o cursor
        # inclusive para mensagens sem ofertas.
        kwargs = {'offset_id': scan['cursor']} if scan['cursor'] else {'offset_date': datetime.fromtimestamp(scan['end'], UTC)}
        count = 0
        async for msg in self.client.iter_messages(scan['channel'], **kwargs):
            status = self.db.conn.execute('SELECT status FROM jobs WHERE id=?', (scan['job'],)).fetchone()[0]
            if status == 'cancelled' or not self.db.channel_active(scan['channel']):
                return
            stamp = int(msg.date.timestamp())
            if stamp < scan['start']:
                break
            if stamp < scan['end']:
                parsed = parse_offer(msg.raw_text or '') if matches(product['name'], msg.raw_text or '') else None
                if parsed:
                    self.db.record(product, scan['channel'], msg.id, stamp, parsed, message_url(scan['channel'], msg.id))
            # Offers commit before cursor: restart can repeat an insert, never skip it.
            self.db.execute('UPDATE scans SET cursor=? WHERE id=?', (msg.id, scan['id']))
            count += 1
            if count % 50 == 0:
                await asyncio.sleep(self.delay)
        status = self.db.conn.execute('SELECT status FROM jobs WHERE id=?', (scan['job'],)).fetchone()[0]
        if status == 'cancelled' or not self.db.channel_active(scan['channel']):
            return
        with self.db.conn:
            self.db.conn.execute('UPDATE scans SET done=1 WHERE id=?', (scan['id'],))
            self.db.conn.execute('INSERT INTO coverage VALUES (?,?,?,?)', (product['id'], scan['channel'], scan['start'], scan['end']))

    async def step(self):
        job = self.db.conn.execute("SELECT * FROM jobs WHERE status IN ('pending','running') ORDER BY id LIMIT 1").fetchone()
        if not job:
            return False
        product = self.db.product(job['product'])
        if not product:
            self.db.execute("UPDATE jobs SET status='cancelled' WHERE id=?", (job['id'],))
            return True
        self.db.execute("UPDATE jobs SET status='running',error=NULL WHERE id=?", (job['id'],))
        scans = self.db.conn.execute('SELECT * FROM scans WHERE job=? AND done=0', (job['id'],)).fetchall()
        for scan in scans:
            if not self.db.channel_active(scan['channel']):
                self.db.execute("UPDATE jobs SET status='cancelled' WHERE id=?", (job['id'],))
                return True
            if not self.db.missing(product['id'], scan['channel'], scan['start'], scan['end']):
                self.db.execute('UPDATE scans SET done=1 WHERE id=?', (scan['id'],))
                continue
            await self.scan(scan, product)
            if self.db.conn.execute('SELECT status FROM jobs WHERE id=?', (job['id'],)).fetchone()[0] == 'cancelled':
                return True
        self.db.execute("UPDATE jobs SET status='done' WHERE id=?", (job['id'],))
        return True

    async def run(self):
        while True:
            try:
                if not await self.step():
                    await asyncio.sleep(1)
            except FloodWaitError as exc:
                log.warning('Telegram limitou a importação; aguardando %s segundos', exc.seconds)
                await asyncio.sleep(exc.seconds + 1)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception('Falha temporária na importação; retomada em 30 segundos')
                self.db.execute("UPDATE jobs SET error='Falha temporária; nova tentativa automática' WHERE status='running'")
                await asyncio.sleep(30)

    async def deliver(self):
        while True:
            try:
                for row in self.db.conn.execute('SELECT * FROM outbox WHERE sent=0 ORDER BY id LIMIT 20').fetchall():
                    await self.bot.send_message(self.admin, row['text'])
                    self.db.execute('UPDATE outbox SET sent=1 WHERE id=?', (row['id'],))
                for job in self.db.conn.execute("SELECT * FROM jobs WHERE status IN ('done','cancelled') AND notified=0").fetchall():
                    requested = [r[0] for r in self.db.conn.execute('SELECT DISTINCT channel FROM scans WHERE job=?', (job['id'],))]
                    report = self.db.report(job['product'], job['start'], job['end'], sorted(set(requested + self.db.channel_names())))
                    await self.bot.send_message(self.admin, f"Busca #{job['id']}: {job['status']}\n{report}")
                    self.db.execute('UPDATE jobs SET notified=1 WHERE id=?', (job['id'],))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception('Falha ao entregar notificação; será tentada novamente')
            await asyncio.sleep(2)
