"""Keep partial pipe reads/writes outside the deadline-owning supervisor."""

from multiprocessing.connection import Connection
from queue import Empty, Queue
from threading import Event, Thread

from kg.query._meter import FRAME_BYTES, Frame, Reply, send


class Channel:
    def __init__(self, connection: Connection) -> None:
        self.connection = connection
        self.incoming: Queue[Frame | Exception] = Queue(maxsize=1)
        self.outgoing: Queue[Reply] = Queue(maxsize=1)
        self.closed = Event()
        self.thread = Thread(target=self._pump, name="kg-query-pipe", daemon=True)
        self.thread.start()

    def _pump(self) -> None:
        try:
            while not self.closed.is_set():
                frame = Frame.model_validate_json(self.connection.recv_bytes(FRAME_BYTES))
                self.incoming.put(frame)
                while not self.closed.is_set():
                    try:
                        reply = self.outgoing.get(timeout=0.02)
                    except Empty:
                        continue
                    send(self.connection, reply)
                    break
        except (EOFError, OSError, ValueError) as error:
            self.incoming.put(error)

    def receive(self, timeout: float) -> Frame | None:
        try:
            frame = self.incoming.get(timeout=timeout)
        except Empty:
            return None
        if isinstance(frame, Exception):
            raise frame
        return frame

    def reply(self, reply: Reply) -> None:
        self.outgoing.put_nowait(reply)

    def close(self) -> None:
        # Call only after the child has exited: that closes its pipe writer.
        self.closed.set()
        self.connection.close()
        self.thread.join(timeout=0.5)
        if self.thread.is_alive():
            raise OSError("Query pipe cleanup failed")
