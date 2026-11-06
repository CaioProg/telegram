"""One-time interactive login, separate from the unattended service."""
import asyncio
import os
from pathlib import Path
from telethon import TelegramClient


async def login():
    session = os.environ.get('TELEGRAM_SESSION', 'data/telegram')
    Path(session).parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(session, int(os.environ['TELEGRAM_API_ID']), os.environ['TELEGRAM_API_HASH'])
    try:
        await client.start()
        if not await client.is_user_authorized():
            raise SystemExit('A conta leitora não foi autenticada.')
        print('Sessão da conta leitora salva. O serviço já pode iniciar.')
    finally:
        await client.disconnect()


def main():
    asyncio.run(login())


if __name__ == '__main__':
    main()
