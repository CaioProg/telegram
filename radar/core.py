"""Regras de negócio e persistência. Valores monetários em centavos; datas UTC."""
import calendar
import re
import sqlite3
import unicodedata
from datetime import datetime, timedelta, timezone

UTC = timezone.utc


def now():
    return int(datetime.now(UTC).timestamp())


def normalize(text):
    return ''.join(c for c in unicodedata.normalize('NFKD', text.casefold())
                   if not unicodedata.combining(c))


def matches(name, text):
    words = re.findall(r'\w+', normalize(name))
    return bool(words) and re.search(r'\b' + r'\W+'.join(map(re.escape, words)) + r'\b', normalize(text)) is not None


def parse_money(text):
    value = text.strip().replace('R$', '').strip()
    if not re.fullmatch(r'(?:\d{1,3}(?:\.\d{3})+|\d+)(?:,\d{1,2})?', value):
        raise ValueError('Informe um preço como 2.499,90.')
    whole, _, fraction = value.replace('.', '').partition(',')
    cents = int(whole) * 100 + int((fraction + '00')[:2])
    if cents <= 0:
        raise ValueError('O preço deve ser positivo.')
    return cents


def parse_offer(text):
    # Exige preço identificado em reais; evita números de modelo/parcelas isoladas.
    prices = re.findall(r'R\$\s*((?:\d{1,3}(?:\.\d{3})+|\d+)(?:,\d{1,2})?)(?![\d.,])', text, re.I)
    if not prices:
        return None
    price = parse_money(prices[-1])
    coupon = re.search(r'cupom\s*[:\-]?\s*([\w-]+)', text, re.I)
    store = re.search(r'(?:loja|vendido por)\s*[:\-]\s*([^\n]+)', text, re.I)
    return price, store.group(1).strip()[:120] if store else None, coupon.group(1)[:80] if coupon else None


def money(cents):
    return 'R$ ' + f'{cents / 100:,.2f}'.replace(',', 'X').replace('.', ',').replace('X', '.')


def period_start(choice, end):
    date = datetime.fromtimestamp(end, UTC)
    if choice in ('7', '30', '90'):
        return int((date - timedelta(days=int(choice))).timestamp())
    months = {'6m': 6, '1y': 12}[choice]
    index = date.year * 12 + date.month - 1 - months
    year, month = divmod(index, 12)
    month += 1
    return int(date.replace(year=year, month=month, day=min(date.day, calendar.monthrange(year, month)[1])).timestamp())


class DB:
    def __init__(self, path):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript('''
        PRAGMA journal_mode=WAL;
        PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS products(
          id INTEGER PRIMARY KEY, name TEXT NOT NULL, normalized TEXT UNIQUE NOT NULL,
          target INTEGER, active INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS offers(
          product INTEGER REFERENCES products(id), channel TEXT, message INTEGER,
          date INTEGER, price INTEGER, store TEXT, coupon TEXT, url TEXT,
          PRIMARY KEY(product,channel,message));
        CREATE TABLE IF NOT EXISTS jobs(
          id INTEGER PRIMARY KEY, product INTEGER REFERENCES products(id), start INTEGER,
          end INTEGER, status TEXT DEFAULT 'pending', error TEXT, notified INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS scans(
          id INTEGER PRIMARY KEY, job INTEGER REFERENCES jobs(id), channel TEXT,
          start INTEGER, end INTEGER, cursor INTEGER DEFAULT 0, done INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS coverage(
          product INTEGER, channel TEXT, start INTEGER, end INTEGER);
        CREATE TABLE IF NOT EXISTS outbox(
          id INTEGER PRIMARY KEY, text TEXT NOT NULL, sent INTEGER DEFAULT 0);
        ''')
        if not self.conn.execute('SELECT 1 FROM products LIMIT 1').fetchone():
            for name in ('Motorola Edge 70 Pro', 'Motorola Signature'):
                self.add(name)

    def execute(self, sql, params=()):
        with self.conn:
            return self.conn.execute(sql, params)

    def add(self, name):
        name = name.strip()
        if not 2 <= len(name) <= 100 or not re.search(r'\w', name):
            raise ValueError('Use um nome entre 2 e 100 caracteres.')
        self.execute('INSERT INTO products(name,normalized) VALUES (?,?) ON CONFLICT(normalized) DO UPDATE SET active=1', (name, normalize(name)))
        return self.conn.execute('SELECT id FROM products WHERE normalized=?', (normalize(name),)).fetchone()[0]

    def products(self):
        return self.conn.execute('SELECT * FROM products WHERE active=1 ORDER BY name').fetchall()

    def product(self, pid):
        return self.conn.execute('SELECT * FROM products WHERE id=? AND active=1', (pid,)).fetchone()

    def missing(self, pid, channel, start, end):
        ranges = self.conn.execute('SELECT start,end FROM coverage WHERE product=? AND channel=? AND end>? AND start<? ORDER BY start', (pid, channel, start, end))
        cursor = start
        gaps = []
        for a, b in ranges:
            if a > cursor:
                gaps.append((cursor, min(a, end)))
            cursor = max(cursor, b)
        if cursor < end:
            gaps.append((cursor, end))
        return gaps

    def enqueue(self, pid, start, end, channels):
        gaps = [(channel, a, b) for channel in channels for a, b in self.missing(pid, channel, start, end)]
        if not gaps:
            return None
        with self.conn:
            job = self.conn.execute('INSERT INTO jobs(product,start,end) VALUES (?,?,?)', (pid, start, end)).lastrowid
            self.conn.executemany('INSERT INTO scans(job,channel,start,end) VALUES (?,?,?,?)', [(job, *gap) for gap in gaps])
        return job

    def record(self, product, channel, message, date, parsed, url, live=False):
        price, store, coupon = parsed
        with self.conn:
            minimum = self.conn.execute('SELECT MIN(price) FROM offers WHERE product=?', (product['id'],)).fetchone()[0]
            inserted = self.conn.execute('INSERT OR IGNORE INTO offers VALUES (?,?,?,?,?,?,?,?)', (product['id'], channel, message, date, price, store, coupon, url)).rowcount
            if inserted and live and (product['target'] is None or price <= product['target'] or minimum is None or price < minimum):
                label = 'Novo menor preço observado nos canais' if minimum is None or price < minimum else 'Promoção encontrada'
                self.conn.execute('INSERT INTO outbox(text) VALUES (?)', (f"{label}: {product['name']}\n{money(price)}\nLoja: {store or 'não informada'} | Cupom: {coupon or 'não informado'}\n{url}",))
        return inserted

    def report(self, pid, start, end, channels):
        rows = self.conn.execute('SELECT * FROM offers WHERE product=? AND date>=? AND date<? ORDER BY date,message', (pid, start, end)).fetchall()
        incomplete = any(self.missing(pid, c, start, end) for c in channels)
        state = 'Histórico incompleto: há períodos ainda não processados.' if incomplete else 'Histórico processado para os canais configurados.'
        if not rows:
            return f'Nenhuma oferta encontrada no período.\n{state}'
        best = min(rows, key=lambda r: r['price'])
        latest = rows[-1]
        date = datetime.fromtimestamp(best['date'], UTC).strftime('%d/%m/%Y %H:%M UTC')
        links = '\n'.join(r['url'] for r in sorted(rows, key=lambda r: r['price'])[:10])
        return (f"Ofertas: {len(rows)}\nMenor preço observado nos canais: {money(best['price'])}\n"
                f"Preço médio: {money(round(sum(r['price'] for r in rows) / len(rows)))}\n"
                f"Último preço: {money(latest['price'])}\nMelhor oferta: {date}\n"
                f"Loja: {best['store'] or 'não informada'} | Cupom: {best['coupon'] or 'não informado'}\n"
                f"{state}\nNão representa o menor preço de todo o mercado.\n"
                f"Links das melhores ofertas (até 10):\n{links}")
