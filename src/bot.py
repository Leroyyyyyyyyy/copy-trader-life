from __future__ import annotations

import asyncio
from datetime import timezone

from telethon import TelegramClient, events

from .engine import Engine
from .util import setup_logging

log = setup_logging()


class ChannelBot:
    def __init__(self, client: TelegramClient, engine: Engine, usernames: list[str]):
        self.client = client
        self.engine = engine
        self.usernames = [u.lstrip("@") for u in usernames]
        self._entities: dict[str, object] = {}

    async def resolve(self) -> None:
        for name in self.usernames:
            entity = await self.client.get_entity(name)
            self._entities[name] = entity
            title = getattr(entity, "title", name)
            log.info("listening %s (%s)", name, title)

    async def run(self) -> None:
        await self.resolve()
        chats = list(self._entities.values())

        @self.client.on(events.NewMessage(chats=chats))
        async def on_new_message(event):  # type: ignore[no-untyped-def]
            await self._handle(event)

        @self.client.on(events.MessageEdited(chats=chats))
        async def on_edit(event):  # type: ignore[no-untyped-def]
            # 编辑不当新信号，避免同一帖反复开仓。
            return

        log.info("realtime listen started, %s channels", len(chats))
        await self.client.run_until_disconnected()

    async def _handle(self, event) -> None:  # type: ignore[no-untyped-def]
        try:
            chat = await event.get_chat()
            username = getattr(chat, "username", None) or str(event.chat_id)
            text = event.raw_text or ""
            date = event.date.replace(tzinfo=timezone.utc).isoformat() if event.date else ""
            image_path = None
            msg = event.message
            if msg is not None and getattr(msg, "photo", None) is not None:
                path = f"data/charts/live_{event.id}.jpg"
                try:
                    await msg.download_media(file=path)
                    image_path = path
                except Exception:
                    log.exception("download media failed #%s", event.id)
            # 同步处理（含可能很慢的 vision 读图）放到线程池，避免阻塞事件循环。
            await asyncio.to_thread(self.engine.handle_text, username, int(event.id), date, text, image_path)
        except Exception:
            log.exception("handle message failed")

    async def poll_loop(self, interval: float = 2.5) -> None:
        while True:
            try:
                self.engine.poll_pending()
            except Exception:
                log.exception("poll pending failed")
            await asyncio.sleep(interval)
