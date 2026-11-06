import asyncio
import contextlib
import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup as Keyboard, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters
from telethon import TelegramClient, events

from .core import DB, UTC, matches, money, now, parse_money, parse_offer, period_start
from .worker import Worker, message_url
from .health import HEARTBEAT

PERIODS = [('7 dias', '7'), ('30 dias', '30'), ('90 dias', '90'), ('6 meses', '6m'), ('1 ano', '1y'), ('Personalizado', 'custom')]


class Bot:
    def __init__(self, db, admin, channels):
        self.db, self.admin, self.channels = db, admin, channels
        self.sessions = {}

    def allowed(self, update):
        return bool(update.effective_user and update.effective_user.id == self.admin and update.effective_chat and update.effective_chat.type == 'private')

    async def send(self, update, text, buttons=None):
        sent = await update.effective_message.reply_text(text, reply_markup=Keyboard(buttons) if buttons else None)
        if buttons and self.admin in self.sessions:
            self.sessions[self.admin]['keyboard'] = sent.message_id

    async def command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.allowed(update):
            return
        command = update.message.text.split()[0].split('@')[0][1:]
        if command == 'cancelar':
            self.sessions.pop(self.admin, None)
            self.db.execute("UPDATE jobs SET status='cancelled' WHERE status IN ('pending','running')")
            await self.send(update, 'Conversa e buscas pendentes canceladas. Ofertas já salvas foram preservadas.')
            return
        if command == 'buscas':
            jobs = self.db.conn.execute('SELECT id,status,error FROM jobs ORDER BY id DESC LIMIT 10').fetchall()
            await self.send(update, '\n'.join(f"#{j['id']}: {j['status']} {j['error'] or ''}" for j in jobs) or 'Nenhuma busca.')
            return
        if command in ('start', 'help'):
            await self.send(update, '/adicionar /remover /listar /preco /historico /menorpreco\n/buscas /cancelar\nUse os comandos em conversa privada.')
            return
        if command == 'listar':
            await self.send(update, '\n'.join(f"{p['name']} — alvo: {money(p['target']) if p['target'] else 'não definido'}" for p in self.db.products()) or 'Nenhum produto monitorado.')
            return
        self.sessions[self.admin] = {'action': command, 'stage': 'name' if command == 'adicionar' else 'product'}
        if command == 'adicionar':
            await self.send(update, 'Qual o nome do produto? /cancelar para sair.')
        else:
            products = self.db.products()
            await self.send(update, 'Selecione o produto:', [[Button(p['name'], callback_data=f"product:{p['id']}")] for p in products])

    async def periods(self, update, session):
        session['stage'] = 'period'
        await self.send(update, 'Qual período deseja consultar/importar?', [[Button(label, callback_data=f'period:{key}')] for label, key in PERIODS])

    async def query(self, update, session, start, end):
        pid = session['product']
        await self.send(update, self.db.report(pid, start, end, self.channels))
        job = self.db.enqueue(pid, start, end, self.channels)
        if job:
            await self.send(update, f'Busca #{job} iniciada em segundo plano apenas para períodos ausentes. O resultado chegará aqui. /cancelar interrompe as buscas; /buscas mostra o progresso.')
        self.sessions.pop(self.admin, None)

    async def callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.allowed(update):
            await update.callback_query.answer('Acesso restrito.', show_alert=True)
            return
        await update.callback_query.answer()
        session = self.sessions.get(self.admin)
        if not session:
            await self.send(update, 'Conversa encerrada. Execute o comando novamente.')
            return
        origin = getattr(update.callback_query, 'message', None)
        if origin is not None and origin.message_id != session.get('keyboard'):
            await self.send(update, 'Este botão expirou. Use os botões da conversa atual.')
            return
        data = update.callback_query.data
        if data.startswith('product:') and session['stage'] == 'product':
            pid = int(data.split(':')[1])
            if not self.db.product(pid):
                await self.send(update, 'Produto não está mais ativo.')
                return
            session['product'] = pid
            action = session['action']
            if action == 'remover':
                session['stage'] = 'confirm'
                await self.send(update, 'Confirmar remoção do monitoramento? O histórico será preservado.', [[Button('Remover', callback_data='remove:yes'), Button('Manter', callback_data='remove:no')]])
            elif action == 'preco':
                session['stage'] = 'price'
                await self.send(update, 'Informe o preço-alvo (ex.: 2.499,90), ou escreva sem alvo.')
            else:
                await self.periods(update, session)
        elif data.startswith('remove:') and session['stage'] == 'confirm':
            if data == 'remove:yes':
                self.db.execute('UPDATE products SET active=0 WHERE id=?', (session['product'],))
                self.db.execute("UPDATE jobs SET status='cancelled' WHERE product=? AND status IN ('pending','running')", (session['product'],))
            self.sessions.pop(self.admin, None)
            await self.send(update, 'Concluído.')
        elif data.startswith('import:') and session['stage'] == 'import':
            if data == 'import:yes':
                await self.periods(update, session)
            else:
                self.sessions.pop(self.admin, None)
                await self.send(update, 'Produto cadastrado. Monitoramento ativo sem importar histórico.')
        elif data.startswith('period:') and session['stage'] == 'period':
            key = data.split(':')[1]
            if key == 'custom':
                session['stage'] = 'custom'
                await self.send(update, 'Envie início e fim: DD/MM/AAAA DD/MM/AAAA (datas UTC; dia final incluído).')
            elif key in dict((key, label) for label, key in PERIODS):
                end = now()
                await self.query(update, session, period_start(key, end), end)

    async def text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.allowed(update):
            return
        session = self.sessions.get(self.admin)
        if not session:
            return
        text = update.message.text.strip()
        try:
            if session['stage'] == 'name':
                session['product'] = self.db.add(text)
                session['stage'] = 'import'
                await self.send(update, 'Produto cadastrado, sem exigir preço-alvo. Deseja importar ofertas anteriores?', [[Button('Sim', callback_data='import:yes'), Button('Não', callback_data='import:no')]])
            elif session['stage'] == 'price':
                target = None if text.casefold() == 'sem alvo' else parse_money(text)
                self.db.execute('UPDATE products SET target=? WHERE id=? AND active=1', (target, session['product']))
                self.sessions.pop(self.admin, None)
                await self.send(update, 'Preço-alvo atualizado.')
            elif session['stage'] == 'custom':
                if not re.fullmatch(r'\d{2}/\d{2}/\d{4} \d{2}/\d{2}/\d{4}', text):
                    raise ValueError('Use DD/MM/AAAA DD/MM/AAAA.')
                first, last = [datetime.strptime(x, '%d/%m/%Y').replace(tzinfo=UTC) for x in text.split()]
                if first > last or last.date() > datetime.now(UTC).date():
                    raise ValueError('Use datas passadas, com início anterior ou igual ao fim.')
                await self.query(update, session, int(first.timestamp()), min(now(), int((last + timedelta(days=1)).timestamp())))
        except ValueError as exc:
            await self.send(update, str(exc))


async def run():
    required = ['TELEGRAM_API_ID', 'TELEGRAM_API_HASH', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_ADMIN_ID', 'TELEGRAM_CHANNELS']
    if any(not os.environ.get(key) for key in required):
        raise SystemExit('Configure: ' + ', '.join(required))
    channels = list(dict.fromkeys(c.strip().lstrip('@') for c in os.environ['TELEGRAM_CHANNELS'].split(',') if c.strip()))
    if not channels or any(not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{3,}', c) for c in channels):
        raise SystemExit('TELEGRAM_CHANNELS deve conter usernames de canais públicos separados por vírgula.')
    admin = int(os.environ['TELEGRAM_ADMIN_ID'])
    db_path = os.environ.get('RADAR_DB', 'data/radar.sqlite3')
    session_path = os.environ.get('TELEGRAM_SESSION', 'data/telegram')
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    Path(session_path).parent.mkdir(parents=True, exist_ok=True)
    db = DB(db_path)
    app = Application.builder().token(os.environ['TELEGRAM_BOT_TOKEN']).build()
    controller = Bot(db, admin, channels)
    app.add_handler(CommandHandler(['start', 'help', 'adicionar', 'remover', 'listar', 'preco', 'historico', 'menorpreco', 'cancelar', 'buscas'], controller.command))
    app.add_handler(CallbackQueryHandler(controller.callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, controller.text))
    client = TelegramClient(session_path, int(os.environ['TELEGRAM_API_ID']), os.environ['TELEGRAM_API_HASH'], flood_sleep_threshold=0)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        db.conn.close()
        raise SystemExit('Sessão Telegram ausente ou expirada. Execute python -m radar.login antes de iniciar o serviço.')
    # Resolve todos os canais antes de instalar o handler; falha explícita se inacessível.
    entities = [await client.get_entity(channel) for channel in channels]
    channel_names = {entity.id: channel for entity, channel in zip(entities, channels)}

    async def live(event):
        channel = channel_names.get(event.chat.id)
        if not channel:
            return
        for product in db.products():
            if matches(product['name'], event.raw_text):
                parsed = parse_offer(event.raw_text)
                if parsed:
                    db.record(product, channel, event.id, int(event.date.timestamp()), parsed, message_url(channel, event.id), live=True)

    client.add_event_handler(live, events.NewMessage(chats=entities))
    tasks = []
    try:
        async with app:
            await app.start()
            await app.updater.start_polling()
            worker = Worker(db, client, app.bot, admin, channels)
            tasks = [asyncio.create_task(worker.run()), asyncio.create_task(worker.deliver())]

            async def heartbeat():
                while True:
                    if client.is_connected() and app.running and app.updater.running and all(not task.done() for task in tasks[:2]):
                        HEARTBEAT.touch()
                    await asyncio.sleep(10)

            tasks.append(asyncio.create_task(heartbeat()))
            try:
                await client.run_until_disconnected()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await app.updater.stop()
                await app.stop()
    finally:
        HEARTBEAT.unlink(missing_ok=True)
        await client.disconnect()
        db.conn.close()


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    # Evita registrar URLs da Bot API que incluem o token.
    logging.getLogger('httpx').setLevel(logging.WARNING)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run())


if __name__ == '__main__':
    main()
