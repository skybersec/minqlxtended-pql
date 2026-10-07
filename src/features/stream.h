/*
Copyright (C) 2026 Thomas Jones <me@thomasjones.id.au>

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.
*/

#ifndef STREAM_H
#define STREAM_H

#include "engine/quake_common.h"

/*
 * Live per-POV demo streaming. The blocks demos.c writes to a .dm_91 go over one TCP connection
 * to a relay instead, which can serve live spectating without a server slot. Demo_Capture taps
 * both, and sv_demoRecord does not come into it.
 *
 * THREAD RULE: the stream thread never dereferences client_t, svs, sv or any cvar_t. The game
 * thread copies what it needs into the ring or the config snapshot. cvar_t::string is Z_Free'd on
 * every set, so reading one off-thread is a use-after-free.
 *
 * A 16MB ring and one detached thread, the same shape as demos.c, but the ring is its own: a
 * send() to a wedged relay can stall for good, and demo_ring_put closes every open file when it
 * backs up. The thread keeps the newest gamestate per slot and replays it on reconnect, since
 * every later snapshot deltas against a frame the relay never saw.
 */

#define MAX_STREAM_CLIENTS MAX_CLIENTS

// Per-slot view for "qlx_stream" and stream_status(). Game thread only, as are its source arrays.
typedef struct {
    int slot;
    int active;      // a POV is open for this slot
    int desynced;    // blocks were lost; waiting for a full snapshot to start from
    int resyncing;   // a full snapshot has been asked for and not yet seen
    int requested;   // 1 explicitly streamed, -1 explicitly excluded, 0 following the cvars
    unsigned gen;    // bumped per open, so a stale completion cannot kill a reopened POV
    char name[40];   // client_t::name as it was when the POV opened, colour codes intact
} stream_slot_status_t;

typedef struct {
    int enabled;                 // sv_demoStream is set and a host is configured
    int connected;               // handshake complete; blocks are going out
    char endpoint[160];          // "host:port", or "" when unconfigured
    char fingerprint[16];        // leading bytes of a hash of the token; the token stays here
    unsigned pending;            // bytes still in the ring
    unsigned lag;                // ms the oldest queued block has been waiting
    unsigned high_water;         // deepest the ring has been since the last reset
    unsigned long long sent;     // bytes handed to the socket
    unsigned long long frames;   // frames handed to the socket
    unsigned dropped_ring;       // blocks dropped because the ring was full
    unsigned dropped_link;       // blocks dropped because the link was down
    unsigned dropped_stall;      // blocks dropped because the relay stopped reading
    unsigned dropped_lag;        // blocks dropped for being past sv_demoStreamMaxLag
    unsigned gaps;               // gap frames emitted
    unsigned resyncs;            // full snapshots forced
    unsigned reconnects;         // successful handshakes after the first
    char error[96];              // last transport error, or ""
    int slot_count;              // entries in slots[] worth reading
    stream_slot_status_t slots[MAX_STREAM_CLIENTS];
} stream_status_t;

// A link coming up or going down. The game thread picks it up and can take the GIL to dispatch it.
typedef struct {
    int connected;
    char endpoint[160];
    char error[96]; // why it went down, or "" when it came up
} stream_link_event_t;

// qtrue once per transition. Game thread only. A link that flaps between two frames reports
// only where it ended up.
qboolean Stream_PollLinkChange(stream_link_event_t *out);

void Stream_Init(void); // register + cache cvars; safe to call more than once

// Whether anything about this slot could still want a block. Kept cheap: the tap asks for every
// outgoing message, so a server that streams nothing never reads client_t.
int Stream_Interested(int slot);

// The stream's demo_want. Opens or closes this slot's POV as a side effect and returns whether
// the block that follows belongs in it. Game thread only, as is everything below.
int Stream_Want(int slot, int is_gamestate, client_t *client);

// Queue one block. Never fails the caller: a full ring is reported to the relay as a gap.
void Stream_Block(int slot, int seq, int is_gamestate, client_t *client, const unsigned char *data,
                  uint32_t len);

// Once per game frame, before the engine's own frame. Publishes map changes and configstrings,
// and sets up the forced full snapshots a resync needs.
void Stream_Frame(void);

// Per-slot override of the sv_demoStreamSlots mask: 1 always streams, -1 never, 0 follows the
// mask. qfalse if the slot is out of range. Mode 1 starts the stream on its own, given a host.
qboolean Stream_Request(int slot, int mode);
int Stream_GetRequest(int slot);       // current override mode for the slot
qboolean Stream_IsStreaming(int slot); // a point of view is currently open for the slot
void Stream_ClearRequests(void);       // server shutdown only; see the note on the definition

void Stream_ClientDisconnect(int slot); // close this client's POV
void Stream_CloseAll(void);             // map change: every POV starts again at a fresh gamestate

// Stream_CloseAll, then a bounded wait for the closes to reach the socket. For shutdown, where
// returning early truncates the stream the relay is writing.
void Stream_DrainClose(void);

void Stream_Reset(void);                  // clear the counters
void Stream_Report(void);                 // "qlx_stream" console output
void Stream_Reconnect(void);              // drop the link; the thread reconnects immediately
void Stream_ResyncAll(void);              // force a full snapshot on every open POV
void Stream_Status(stream_status_t *out); // snapshot for stream_status()

#endif /* STREAM_H */
