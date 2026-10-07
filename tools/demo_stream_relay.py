#!/usr/bin/env python3
# minqlxtended - Extends Quake Live's dedicated server with extra functionality and scripting.
# Copyright (C) 2026 Thomas Jones <me@thomasjones.id.au>

# This file is part of minqlxtended.

# minqlxtended is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# minqlxtended is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with minqlxtended. If not, see <http://www.gnu.org/licenses/>.

"""A reference relay for the sv_demoStream protocol.

    python3 tools/demo_stream_relay.py --port 27999 --out ./povs

One thread per server. Each gets a directory under --out named from its Steam account ID
(sv_steamAccount), or the peer address if it has none, and that name prefixes its log lines.
POV filenames carry the match GUID (configstring 712) as it stood when the POV opened. A POV is
per client per map, so one file can span several matches.

A test harness. It has no broadcast delay, no viewer handling and no authentication beyond a
token, so it should not face the public. Streaming every point of view live is a wallhack, and
anything serving these onward needs all of that first.
"""

import argparse
import os
import socket
import struct
import sys
import threading
import time

MAGIC = 0x56544C51
PROTOCOL = 1

HELLO, HELLO_ACK, HEARTBEAT = 0x01, 0x02, 0x03
SLOT_OPEN, BLOCK, SLOT_CLOSE, GAP = 0x10, 0x11, 0x12, 0x13
CONFIGSTRINGS, SERVER_INFO = 0x20, 0x21

NAMES = {
    HELLO: "HELLO", HELLO_ACK: "HELLO_ACK", HEARTBEAT: "HEARTBEAT",
    SLOT_OPEN: "SLOT_OPEN", BLOCK: "BLOCK", SLOT_CLOSE: "SLOT_CLOSE", GAP: "GAP",
    CONFIGSTRINGS: "CONFIGSTRINGS", SERVER_INFO: "SERVER_INFO",
}

SLOT_ANY = 0xFFFF
MAX_FRAME = 65536
CS_MATCH_GUID = 712

CLOSE_REASONS = ["disconnect", "map change", "shutdown", "new gamestate", "streaming off"]
GAP_REASONS = ["ring full", "relay stalled", "link down", "too far behind"]

_print_lock = threading.Lock()


def cstr(raw):
    """A NUL-padded fixed-width field as text."""
    return raw.split(b"\0", 1)[0].decode("utf-8", "replace")


def safe_name(text, fallback="x"):
    """Text reduced to what is safe in a filename."""
    return "".join(c for c in text if c.isalnum() or c in "-_.") or fallback


class Pov:
    """One point of view, written as a .dm_91 that is valid at every instant.

    Each block is `int32 seq, int32 len, body`. After each one we append the -1/-1 end marker and
    seek back over it, so the next block overwrites it and a reader always sees a terminated demo.
    """

    EOF = struct.pack("<ii", -1, -1)

    def __init__(self, path):
        self.path = path
        self.fh = open(path, "wb")  # noqa: SIM115 -- held open across blocks
        self.blocks = 0
        self.desynced = False

    def block(self, seq, body):
        self.fh.write(struct.pack("<ii", seq, len(body)))
        self.fh.write(body)
        self.blocks += 1
        here = self.fh.tell()
        self.fh.write(self.EOF)
        self.fh.flush()
        self.fh.seek(here)

    def close(self):
        self.fh.write(self.EOF)
        self.fh.close()


class Relay:
    """One server's connection. Nothing here is shared with another server."""

    def __init__(self, out_root, token, peer, slow=0):
        self.out_root = out_root
        self.out_dir = None  # set once the HELLO names the server
        self.token = token
        self.slow = slow
        self.key = f"{peer[0]}-{peer[1]}"
        self.started = time.monotonic()
        self.read_total = 0
        self.povs = {}
        self.configstrings = {}
        self.server_id = None
        self.match_guid = ""

    def throttle(self, nbytes):
        """Hold the read rate down to --slow bytes a second.

        A relay reading steadily but too slowly trips the server's sv_demoStreamMaxLag
        watermark and not its stall timer. This is how that gets tested."""
        if not self.slow:
            return
        self.read_total += nbytes
        behind = (self.started + self.read_total / self.slow) - time.monotonic()
        if behind > 0:
            time.sleep(behind)

    def log(self, message):
        with _print_lock:
            print(f"[{time.strftime('%H:%M:%S')}] [{self.key}] {message}", flush=True)

    def open_pov(self, slot, gen, name):
        self.close_pov(slot, "superseded")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        guid = f"_m{safe_name(self.match_guid)[:8]}" if self.match_guid else ""
        path = os.path.join(self.out_dir,
                            f"{stamp}_slot{slot:02d}_{safe_name(name)}_g{gen}{guid}.dm_91")
        self.povs[slot] = Pov(path)
        return path

    def close_pov(self, slot, why):
        pov = self.povs.pop(slot, None)
        if pov is None:
            return
        pov.close()
        if pov.blocks <= 1:
            os.unlink(pov.path)
            self.log(f"  slot {slot}: {why}, discarded (gamestate only)")
        else:
            self.log(f"  slot {slot}: {why}, {pov.blocks} blocks in {os.path.basename(pov.path)}")

    def handle(self, ftype, flags, slot, payload):
        if ftype == SERVER_INFO:
            self.server_id, text_len = struct.unpack_from("<II", payload, 0)
            info = payload[8:8 + text_len].decode("utf-8", "replace")
            mapname = dict(zip(info.split("\\")[1::2], info.split("\\")[2::2])).get("mapname", "?")
            self.log(f"SERVER_INFO  serverId={self.server_id} mapname={mapname}")

        elif ftype == CONFIGSTRINGS:
            # Flag 0x01 begins a fresh set; a frame without it patches what we hold.
            _server_id, count = struct.unpack_from("<II", payload, 0)
            if flags & 0x01:
                self.configstrings = {}
            off = 8
            for _ in range(count):
                index, vlen = struct.unpack_from("<HH", payload, off)
                self.configstrings[index] = payload[off + 4:off + 4 + vlen].decode("utf-8", "replace")
                off += 4 + vlen
            last = " (last)" if flags & 0x02 else ""
            self.log(f"CONFIGSTRINGS  {count} pairs, {len(self.configstrings)} held{last}")
            guid = self.configstrings.get(CS_MATCH_GUID, "")
            if guid != self.match_guid:
                self.match_guid = guid
                self.log(f"  match GUID is now {guid or '(none)'}")

        elif ftype == SLOT_OPEN:
            gen, gs_seq, steam_id, oflags, cmd_seq, gs_cmd_seq = struct.unpack_from("<IIQIII", payload, 0)
            name = cstr(payload[32:72])
            kind = "replay" if oflags & 0x02 else "live"
            path = self.open_pov(slot, gen, name)
            self.log(f"SLOT_OPEN    slot={slot} gen={gen} {kind} name={name!r} steam={steam_id} "
                     f"gs_seq={gs_seq} cmd_seq={cmd_seq}/{gs_cmd_seq} -> {os.path.basename(path)}")
            if oflags & 0x02:
                # The cached gamestate's configstrings are as old as the block. Splicing in `cs`
                # commands from CONFIGSTRINGS, numbered from gs_cmd_seq + 1, needs a Huffman bit
                # writer. Until then a replayed POV has stale scores and names.
                self.log(f"  slot {slot}: replay, {len(self.configstrings)} configstrings need "
                         f"splicing from seq {gs_cmd_seq + 1}")

        elif ftype == BLOCK:
            seq, gen = struct.unpack_from("<II", payload, 0)
            body = payload[8:]
            pov = self.povs.get(slot)
            if pov is None:
                return
            pov.block(seq, body)
            if flags & 0x02 and pov.desynced:
                pov.desynced = False
                self.log(f"  slot {slot}: re-anchored on a full snapshot at seq {seq}")
            if flags & 0x01:
                self.log(f"BLOCK        slot={slot} gen={gen} seq={seq} gamestate, {len(body)} bytes")

        elif ftype == SLOT_CLOSE:
            gen, reason = struct.unpack_from("<II", payload, 0)
            why = CLOSE_REASONS[reason] if reason < len(CLOSE_REASONS) else f"reason {reason}"
            self.log(f"SLOT_CLOSE   slot={slot} gen={gen} {why}")
            self.close_pov(slot, why)

        elif ftype == GAP:
            gen, first_seq, dropped, reason = struct.unpack_from("<IIII", payload, 0)
            why = GAP_REASONS[reason] if reason < len(GAP_REASONS) else f"reason {reason}"
            pov = self.povs.get(slot)
            if pov is not None:
                pov.desynced = True
            self.log(f"GAP          slot={slot} gen={gen} lost {dropped} blocks from seq {first_seq} ({why})")

        elif ftype == HEARTBEAT:
            uptime, pending = struct.unpack_from("<II", payload, 0)
            self.log(f"HEARTBEAT    up {uptime / 1000:.0f}s, {pending} bytes queued on the server")

    def hello(self, payload):
        magic, protocol, session, steam_id, max_clients, fps = struct.unpack_from("<IIQQII", payload, 0)
        token = cstr(payload[32:96])
        build = cstr(payload[128:168])  # bytes 96-128 are reserved

        if magic != MAGIC:
            return 2, "bad magic"
        if protocol != PROTOCOL:
            return 2, f"protocol {protocol}, expected {PROTOCOL}"
        if self.token is not None and token != self.token:
            return 1, "token mismatch"

        # Keyed by Steam account, to keep two servers behind one address apart.
        if steam_id:
            self.key = str(steam_id)
        self.out_dir = os.path.join(self.out_root, self.key)
        os.makedirs(self.out_dir, exist_ok=True)

        self.log(f"HELLO        build={build!r} session={session:#x} "
                 f"steam={steam_id} max_clients={max_clients} sv_fps={fps} -> {self.out_dir}/")
        return 0, None

    def serve(self, conn):
        """Reads frames until the link ends. Returns why it ended."""
        buf = b""
        greeted = False
        # Small reads while throttling, to keep the pauses short. Multi-second stalls would trip
        # the server's stall timer instead of its lag watermark.
        read_size = max(1024, min(65536, self.slow // 10)) if self.slow else 65536
        while True:
            chunk = conn.recv(read_size)
            if not chunk:
                return "server closed the connection"
            self.throttle(len(chunk))
            buf += chunk

            while len(buf) >= 8:
                ftype, flags, slot, length = struct.unpack_from("<BBHI", buf, 0)
                if length > MAX_FRAME:
                    return f"frame length {length} is out of range"
                if len(buf) < 8 + length:
                    break
                payload = buf[8:8 + length]
                buf = buf[8 + length:]

                if not greeted:
                    if ftype != HELLO:
                        return f"first frame was {NAMES.get(ftype, ftype)}, not HELLO"
                    status, why = self.hello(payload)
                    conn.sendall(struct.pack("<BBHIII", HELLO_ACK, 0, SLOT_ANY, 8, PROTOCOL, status))
                    if status != 0:
                        return f"refused: {why}"
                    greeted = True
                    continue

                self.handle(ftype, flags, slot, payload)
                if ftype == HEARTBEAT:
                    conn.sendall(struct.pack("<BBHIII", HEARTBEAT, 0, SLOT_ANY, 8, 0, 0))

    def shutdown(self, why):
        for slot in list(self.povs):
            self.close_pov(slot, why)


def serve_connection(conn, peer, args):
    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    relay = Relay(args.out, args.token, peer, args.slow)
    relay.log(f"server connected from {peer[0]}:{peer[1]}")
    if args.slow:
        relay.log(f"reading at {args.slow} bytes/sec on purpose")
    why = "relay stopping"
    try:
        why = relay.serve(conn)
    except (ConnectionResetError, BrokenPipeError, TimeoutError, struct.error) as err:
        why = f"connection lost: {err}"
    finally:
        relay.log(f"link ended: {why}")
        relay.shutdown(why)
        conn.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=27999)
    ap.add_argument("--out", default="povs", help="directory for the .dm_91 files, one subdirectory per server")
    ap.add_argument("--token", default=None, help="require this token in the handshake")
    ap.add_argument("--slow", type=int, default=0, metavar="BYTES_PER_SEC",
                    help="read this slowly, to exercise the server's sv_demoStreamMaxLag shedding")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((args.host, args.port))
    listener.listen(16)
    print(f"listening on {args.host}:{args.port}, writing to {args.out}/<server>/", flush=True)

    try:
        while True:
            conn, peer = listener.accept()
            threading.Thread(target=serve_connection, args=(conn, peer, args), daemon=True).start()
    except KeyboardInterrupt:
        # Daemon threads, and every POV file on disk already ends with a terminator.
        return 0


if __name__ == "__main__":
    sys.exit(main())
