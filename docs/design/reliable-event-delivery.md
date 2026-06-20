# Reliable Event Delivery for Coding Bridge

**Status:** Proposed (design + protocol v2 spec)
**Scope:** 3 repos — `CodingBridge` (node), `coding-bridge` relay (in
`PlatformService/coding-bridge/worker/app/` on `origin/main`), `Nexior` (browser)
**Author:** (design)
**Supersedes:** the implicit "history is the recovery path" behaviour.

> **Verified against `origin/main` of all three repos (2026-06-13, re-pulled).**
> No one has touched event-delivery durability — fan-out is still fire-and-forget
> and the protocol still has no `seq`/buffer/ack/replay. Recent merges are
> *related context*, and one (**rewind/fork**) materially shapes the design:
> - **Relay #992** "stop reconnect race from evicting live nodes" fixes only the
>   node *connection-slot* race (newest socket wins); it does nothing for lost
>   in-flight events. Our outbox/replay work complements it.
> - **Relay #995** added `observability.py`, which already extracts `msg_id`
>   (= envelope `id`), `trace_id`, `session_id`, `node_event` per event for CLS —
>   a ready hook for **dedup-by-id** and for the Phase-5 chaos-test telemetry.
> - **Node #17** "drop resume transcript replay" added a node-side dedup guard
>   keyed by line-uuid + message-id — the same dedup-by-id principle this design
>   generalizes to the wire.
> - **Node #18 + Browser #910 — REWIND / FORK.** Editing a past prompt forks the
>   conversation: action `session.edit {cut_uuid, restore_code}` → node runs
>   `claude --fork-session --resume <sid> --resume-session-at <cut_uuid>` (+
>   optional `rewind_files()` FS checkpoint). The browser **optimistically and
>   locally** truncates its event list (`truncateEventsBefore` → `events.slice(
>   0, index)`) — **no rewind event is recorded on the wire.** This breaks naive
>   log replay (a reconnecting browser would replay the abandoned turns). See §4b.
>
> **Phase 1 must branch off `origin/main` at `PlatformService/coding-bridge/`,
> NOT the stale `feat/coding-bridge` worktree (behind main, old repo-root path).**

---

## 1. Problem

A user sends a prompt from the browser. The node (Claude Code / Codex on the
user's machine) runs the turn and answers. But **nothing streams back live** —
the browser shows a blank, "waiting" state. Only after a **page refresh** does
the answer appear, pulled from `history.get` (Claude's own local transcript).

The answer was never lost on the node. It was lost **in transit**, and the only
reason a refresh recovers it is that Claude Code persists its own transcript to
disk and we read it back. History is accidentally acting as a lossy backstop for
a real-time guarantee we never built.

## 2. Root cause: fire-and-forget with no durable, sequenced log

Every node→browser event crosses **three hops**, and *each one* silently drops
in-flight events on any disconnect, with **zero replay**:

| Hop | Failure mode | Where (today) |
|-----|--------------|---------------|
| **A. node → relay** | node's WS to relay is down during the 1–30 s reconnect window → emitted events are dropped on the floor | `connection.py` `_send_envelope`: `if ws is None: return` |
| **B. relay internal** | relay process restart (deploy/crash) wipes in-memory state mid-turn | relay `main.py` — no buffering |
| **C. relay → browser** | relay fans out **by `user_id`, fire-and-forget**, skipping dead sockets; on reconnect the browser only re-requests `history.list` + `capabilities`, **never a replay** | relay `_forward_to_browsers`; Nexior `onOpen` |

There is **no `seq`, no cursor, no ack, no outbox** anywhere in any of the three
repos. The wire envelope already carries a uuid `id` and a `ts`, but nothing
uses them for ordering, dedup, or replay.

### Reference: Claude Code already solved this

The upstream Claude Code bridge implements exactly the pattern we adopt:

- **Monotonic `sequence_num` + resume-from-cursor** — every streamed message is
  numbered; on reconnect the client sends `Last-Event-ID` / `from_sequence_num`
  and the server replays **only `seq > cursor`** instead of from zero.
- **Bounded UUID dedup ring (LRU ~2000)** — catches echoes and re-deliveries on
  both the send and receive sides.
- **Crash-recovery pointer file** (`bridge-pointer.json`) — lets a restarted
  process re-attach to a live session.
- **Catch-up-then-tail flush gate** — backlog flushes first, live events queue,
  then drain — no gap, no interleave.

We rebuild the same skeleton, with the **relay as the durable broker** (the node
is outbound-only behind NAT and can never be pulled).

## 3. Goals / non-goals

**Goals**

1. A browser reconnect or full page reload **silently catches up** the live
   stream — no blank wait, no dependence on `history.get`.
2. A node↔relay reconnect loses **no** events and produces **no** duplicates.
3. Ordering is preserved per session; deltas reassemble correctly.
4. The design is **storage-agnostic**: ships on an in-memory ring buffer (no DB),
   with a documented, drop-in MongoDB durability tier.
5. Graceful, *explicit* degradation when a cursor is too old — never a silent
   loss.

**Non-goals (this revision)**

- Surviving a relay process restart with **zero** loss (deferred; see §9).
- Multi-replica relay (it is single-replica by design).
- Changing the permission / pairing / auth flows.

## 4. Architecture overview

One primitive, applied to the two reliable hops:

> **A per-session, append-only event log with a monotonic sequence number,
> idempotent append (dedup by envelope `id`), and cursor-based replay.**

```
   ── HOP A: node → relay ──────────────      ── HOP C: relay → browser ──────────────
   node keeps an OUTBOX (node_local_seq)       relay keeps a per-session LOG (seq)
   relay ACKs the contiguous tail              browser tracks last_seq per session
   node trims on ack, RESENDS on reconnect     on (re)connect: resume_from {sid:last_seq}
   relay DEDUPES by envelope id (idempotent)   relay replays seq>cursor, then tails live
```

- The **relay is the single durable broker.** It owns the authoritative
  per-session `seq` (assigned on append), so the number survives node restarts.
- The **node** guarantees at-least-once delivery *to the relay* via its outbox +
  ack; the relay's dedup-by-`id` makes redelivery idempotent.
- The **browser** guarantees it can re-derive the full live stream from its
  cursor; if the cursor predates the retained window, it falls back to a one-shot
  `history.get` and resumes live from there.

### Storage decision

The relay is `replicas: 1`, `Recreate`, **stateful by design** — its live
registry is already in RAM. The event log's day-one backing store is therefore a
**bounded in-memory ring buffer per session** (e.g. last 5 000 events or 30 min,
whichever first; evict oldest). **No database is required** for goals 1–3.

MongoDB (already wired into the relay for the node registry) is the **deferred**
durability tier (§9): persist only *committed* events (`session.text`,
`session.tool_use`, `session.tool_result`, `session.result`) to a **capped
collection**; keep `session.text_delta` RAM-only (cheap, high-volume, and fully
reconstructable from the committed `session.text`). Swapping the store in is a
change behind the log interface, not a redesign.

## 4b. Rewind / fork: the log is event-sourced (critical)

Editing a past prompt **forks** the conversation (node #18, browser #910): turns
after the edit point are discarded and a new branch begins. Today the browser
handles this **optimistically and locally** — `truncateEventsBefore` slices its
in-memory event list and fires `session.edit {cut_uuid}`; the node forks the
Claude session. **Nothing on the wire records that a rewind happened.**

This is the one place a naive "append-only log + replay from cursor" design is
**wrong**: a browser that reconnects *after* an edit, replaying the relay log
from its cursor, would re-render the abandoned turns plus the new branch.

**Resolution — event sourcing (the industry-standard model, and what Claude Code
itself does).** The durable log is the source of truth; the UI is a *projection*
(a fold over the log). The log is **append-only and never physically truncated**;
a rewind is a **first-class, sequenced event** that the projection interprets:

- One **durable log per bridge `session_id`** (the browser-generated id, stable
  across forks). The Claude SDK's own `sdk_session_id` is just metadata carried
  inside events — we do **not** segment the log by it.
- On `session.edit`, the **node emits an authoritative `session.rewound
  {cut_event_id, cut_uuid, sdk_session_id}`** event *before* streaming the forked
  turn. The relay assigns it a `seq` and appends it like any other event.
- The browser's projection reducer, on replaying or live-receiving
  `session.rewound`, **truncates its rendered events back to `cut_event_id`**,
  then continues folding. Live and replay paths now use the **same** authoritative
  signal (today's local optimistic truncation becomes a UI prediction confirmed
  by the event).
- **`seq` never resets across a rewind** — it keeps climbing monotonically, so
  cursor-resume always works (confirmed by Claude Code: monotonic timestamps,
  no reset; rewind is a skip-marker, not a counter reset).

This mirrors Claude Code precisely: its transcript JSONL is append-only;
interactive rewind marks discarded entries in a `skippedTimestamps` set rather
than deleting them; `--resume-session-at` slices *in memory* only; and a
`--fork-session` copies messages into a new session id while leaving the original
intact. Same invariant: **never mutate the past; supersede it with a new event.**

Industry best practices this design leans on: **event sourcing** (log = truth,
view = fold), **transactional outbox** (the node outbox in §6.2), **idempotent
consumer / dedup-by-id** (effectively-once over at-least-once delivery),
**resume-from-cursor** (SSE `Last-Event-ID`), and **bounded log + explicit
truncation signal** (`cursor_too_old`, like Kafka log retention).

The `restore_code` / `rewind_files()` filesystem checkpoint is **orthogonal** —
it is node-local working-tree state handled by the Claude SDK's
`enable_file_checkpointing`, not part of the event-delivery log.

## 5. Protocol v2 (wire format)

`PROTOCOL_VERSION: 1 → 2`. All additions are **additive**; a v1 peer ignores
unknown fields and the relay degrades to today's fire-and-forget for it.

### 5.1 Node → relay (inner event envelopes)

Each node→browser event envelope gains:

| Field | Type | Meaning |
|-------|------|---------|
| `id` | uuid (already present) | **dedup key** — formalized. Stable across resends. |
| `node_seq` | int | monotonic per `(node connection epoch)`, for outbox/ack only. Not shown to the browser. |
| `session_id` | str (in payload, already present) | which session log this appends to. |

### 5.2 Relay → node

New envelope type:

```
NODE_ACK = "node.ack"
payload: { "up_to_node_seq": <int> }   # highest contiguous node_seq durably appended
```

The node trims its outbox to `up_to_node_seq` and resends anything above it on
reconnect.

### 5.3 Relay → browser (fanned-out events)

Each `node.to_browser` event payload gains:

| Field | Type | Meaning |
|-------|------|---------|
| `seq` | int | **authoritative**, monotonic per `session_id`, assigned by the relay on append. The browser orders and dedupes on this. |

### 5.4 Browser → relay

The browser's subscribe / `sessions.list` action gains an optional cursor map:

```
resume_from: { "<session_id>": <last_seq>, ... }
```

New control events relay → browser:

```
SESSION_STREAM_TRUNCATED = "session.stream_truncated"
payload: { session_id, reason: "cursor_too_old" | "node_outbox_overflow",
           resume_with: "history" }
```

New rewind event (node → browser, sequenced & logged — see §4b):

```
SESSION_REWOUND = "session.rewound"
payload: { session_id, cut_event_id, cut_uuid, sdk_session_id }
```

The node emits this when it handles `session.edit`, *before* streaming the forked
turn. The browser's projection truncates rendered events back to `cut_event_id`
on both live receipt and replay — so a reconnect after an edit reconstructs the
correct branch instead of replaying abandoned turns.

### 5.5 Browser → node command durability (Phase 4)

Symmetric: the browser tags commands with `cmd_id` (uuid) + `cmd_seq`; the relay
queues them per node when the node is briefly offline and delivers on reconnect;
the node acks `browser.cmd_ack { up_to_cmd_seq }`. Out of scope for Phases 1–3.

## 6. Component responsibilities

### 6.1 Relay (`coding-bridge`, the heart)

- **Per-session log** (`SessionLog`): `append(event) -> seq`, dedup by `id`
  (bounded LRU per session), `read_since(cursor) -> [events]`,
  `earliest_seq()`. Backed by an in-memory ring buffer; capacity + TTL bounded.
- **On node event:** dedup by `id`; if new, `seq = log.append(event)`; stamp
  `seq` into the payload; fan out to the user's browsers; send `node.ack` with
  the highest contiguous `node_seq`.
- **On browser (re)connect with `resume_from`:** for each session, **catch-up
  then tail** — begin buffering live events, `read_since(last_seq)`, emit the
  backlog in order, flush the buffered live tail, dedupe the overlap by `seq`.
- **Cursor too old:** if `last_seq < log.earliest_seq()`, emit
  `session.stream_truncated{reason: cursor_too_old}` → browser does one
  `history.get`, then resumes live from the newest `seq`.
- **GC:** drop a session's log on `session.closed` + grace, or on TTL/idle.

### 6.2 Node (`CodingBridge`)

- **Delete the silent drop** in `_send_envelope`. Disconnected sends **enqueue**.
- **Outbox** per connection epoch: `deque[(node_seq, envelope)]`, bounded. On
  send: assign `node_seq`, append, transmit if connected. On `node.ack`: trim to
  `up_to_node_seq`. On reconnect: resend the unacked tail **in order** before new
  traffic.
- **Outbox overflow** (long disconnect): drop oldest, set a per-session
  "truncated" flag so the next delivered event carries
  `stream_truncated{reason: node_outbox_overflow}` → browser falls back to
  `history.get`. Never a silent loss.
- **Session-pointer** (Phase 5): persist `{session_id}` per active session
  (`~/.ace-bridge/sessions/`) so a restarted node can re-attach and the relay's
  log + the node's `resume` keep the stream coherent.

### 6.3 Browser (`Nexior`)

- **Persist `last_seq` per `session_id`** (vuex-persistedstate) so it survives a
  full reload, not just a WS reconnect.
- **On (re)connect:** send `resume_from` from the persisted cursors (today it
  only sends `history.list` + `capabilities`).
- **On each event:** update `last_seq`; **order/dedupe by `seq`** (ignore
  `seq <= last_seq`). Keep the existing `stream_id`-keyed delta assembly.
- **On `stream_truncated`:** run a one-shot `history.get` for that session, then
  continue live from the newest `seq`.

## 7. Failure-mode matrix (after the fix)

| Event | Recovered by | DB needed? |
|-------|--------------|------------|
| Browser tab refresh / reload | browser `resume_from` cursor → relay replay (RAM) | No |
| Browser WS blip / sleep-wake | same | No |
| Node↔relay reconnect gap | node outbox resend + relay dedup | No |
| Multiple browser tabs | each tracks its own cursor; relay replays per tab | No |
| Cursor older than retained window | `stream_truncated` → `history.get` | No |
| **Relay process restart mid-turn** | node outbox resend covers what the node still holds; older committed events need the **Mongo tier** (§9) | **Only for full coverage** |

## 8. Phased rollout (no throwaway work)

Each phase is a permanent component of the final design, independently shippable,
fully back-compat (v1 peers keep working, degraded to today's behaviour).

| Phase | Repo | Work | Unblocks |
|-------|------|------|----------|
| **0** | all | This doc; freeze protocol v2 wire format | everything |
| **1** | relay | `SessionLog` (in-memory ring), `seq` assignment, dedup-by-`id`, `resume_from` replay + catch-up-then-tail, `node.ack` | 2, 3 |
| **2** | node | Outbox + ack + resend; **delete silent drop**; overflow marker; **emit `session.rewound` on `session.edit`** (§4b) | — |
| **3** | browser | Persist `last_seq`; send `resume_from`; order/dedupe by `seq`; make rewind a **projection over the log** (apply `session.rewound` on replay, not just optimistic local truncation); `stream_truncated` → history fallback | — |
| **4** | relay + browser | Browser→node command durability (queue + ack) | — |
| **5** | node + relay | Node session-pointer re-attach; chaos test harness | — |

**Hard dependency:** Phases 2 and 3 are inert until Phase 1 lands in the relay.

## 9. Deferred: MongoDB durability tier (relay-restart survival)

Drop-in, no protocol change:

- New capped collection `coding_bridge_events` (or TTL collection), one doc per
  **committed** event: `{ session_id, seq, id, event, payload, ts }`, indexed on
  `(session_id, seq)`.
- `SessionLog.append` writes committed events through to Mongo; deltas stay
  RAM-only. On relay startup, `read_since` for an active session hydrates from
  Mongo if the RAM ring is cold.
- `seq` continuity across restart: persist the per-session high-water mark with
  the events; on startup, resume numbering from `max(seq)`.

This closes the last row of §7 at the cost of one low-volume write per committed
event. Defer until in-memory replay is proven in production.

## 10. Testing

- **Unit:** `SessionLog` append/dedup/read_since/eviction; node outbox
  trim/resend ordering; browser seq dedup/ordering.
- **Chaos harness** (Phase 5): drive a real turn, then programmatically
  (a) drop the node↔relay socket mid-stream, (b) drop the browser↔relay socket
  mid-stream, (c) restart the relay process mid-stream. Assert: **every** event
  the node emitted is delivered to the browser **exactly once, in order**, with
  no reliance on `history.get` for (a) and (b).
- **Back-compat:** a v1 node against a v2 relay, and a v2 node against a v1
  browser, both still function (degraded, no crash).
