"""Event delivery: one ws_events poller per process, rows handed to the connections allowed to see them."""
import asyncio

from .protocol import EVENT_LOOKBACK, run_sync


# ---------------------------------------------------------------- events ----
class EventHub:
    """Identity ids are handed out at INSERT, not at COMMIT: a transaction holding id 7 may commit after id 8
    was already read. So each poll re-reads the last EVENT_LOOKBACK ids and delivers only ids not seen yet."""

    def __init__(self, service, interval=1.0, lookback=EVENT_LOOKBACK):
        self.service, self.interval, self.lookback = service, interval, lookback
        self.connections = set()
        self.last_event_id = None
        self.seen = set()

    def subscribe(self, connection):
        self.connections.add(connection)

    def unsubscribe(self, connection):
        self.connections.discard(connection)

    def _read(self):
        with self.service.db.transaction() as tx:
            if self.last_event_id is None:
                # Start at the end of the log; events already there count as delivered.
                row = tx.one('SELECT max(event_id) AS n FROM ws_events')
                self.last_event_id = row['n'] or 0
                self.seen = {r['event_id'] for r in tx.all('SELECT event_id FROM ws_events WHERE event_id>%s',
                                                          (max(0, self.last_event_id - self.lookback),))}
                return []
            floor = max(0, self.last_event_id - self.lookback)
            rows = tx.all('SELECT * FROM ws_events WHERE event_id>%s ORDER BY event_id LIMIT 2000', (floor,))
        fresh = [r for r in rows if r['event_id'] not in self.seen]
        for r in fresh:
            self.seen.add(r['event_id'])
        if rows:
            self.last_event_id = max(self.last_event_id, rows[-1]['event_id'])
        floor = max(0, self.last_event_id - self.lookback)
        self.seen = {i for i in self.seen if i > floor}
        return fresh

    async def poll_once(self):
        rows = await run_sync(self._read)
        for row in rows:
            for connection in list(self.connections):
                connection.offer(row)
        return len(rows)

    async def run(self):
        while True:
            try:
                await self.poll_once()
            except Exception:
                pass  # DB hiccup: the next poll re-reads from the lookback floor.
            await asyncio.sleep(self.interval)
