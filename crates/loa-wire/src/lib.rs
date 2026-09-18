//! loa-wire — the contract, in Rust.
//!
//! ONE schema (proto/loa.proto), FOUR socket roles, TWO message paths:
//!
//!   state down   the cortex PUBLISHes state; devices and consumers SUBSCRIBE.
//!                Message: [topic][Envelope], topic == payload name.
//!   frames up    a device PUSHes its finished frame to the cortex under its
//!                OWN name; the cortex swaps ONLY the topic frame per ROUTES
//!                and publishes. The payload bytes are never parsed — routing
//!                is checked, payload is trusted, because the device knows its
//!                own glass. A mismatched pair still fails loudly: the
//!                CONSUMER decodes both frames and refuses (see `decode`).
//!
//! Four socket roles, because the direction of data differs:
//!   Publisher (PUB) — the cortex, and only the cortex
//!   Subscriber (SUB) — every consumer, and every daemon (each subscribes to
//!                      `init` so a restarted cortex can ask it to report)
//!   Sender    (PUSH) — the daemons feeding the cortex
//!   Receiver  (PULL) — the cortex collecting from the daemons
//!
//! PUSH/PULL for the inbound leg, not PUB/SUB: many daemons, one collector, and
//! a reading that vanishes because a subscriber was slow is a lie about the
//! body. PUSH/PULL queues; PUB/SUB drops.
//!
//! A device's consumer contract is four words: subscribe, hold the last
//! message, render, repeat. It HOLDS what it receives — nothing in this crate
//! "takes" state from anywhere.

pub mod pb {
    include!(concat!(env!("OUT_DIR"), "/loa.rs"));
}

// prost's encode/decode are trait methods on Message; without this import the
// generated structs have no methods at all.
use prost::Message as _;

/// Bump on ANY change to proto/loa.proto. Both legs check it, so a body and a
/// console that disagree fail on arrival instead of misreading fields into
/// plausible rubbish. This schema is v11: state down, frames up, news-only.
pub const SCHEMA_VERSION: u32 = 11;

/// Every topic the cortex publishes. Matches the oneof field names in the
/// proto — that is what lets `encode` and `decode` stay generic. `init` is
/// control, never state, and nothing here is ever published on a clock.
///
/// `state` is the device leg: the presentation fields, published for the
/// glass daemons to receive. Its payload is a Ripperdoc with no face bytes
/// in it (see ALIASES) — one shape for the state of the displays, not two
/// messages with eleven identical fields that drift.
pub const TOPICS: [&str; 10] = [
    "state", "ripperdoc", "ring", "pir", "sonar", "weather", "power", "fault",
    "event", "init",
];

/// Topics whose payload name is not their own. Exactly one row: the state leg
/// carries a face-less Ripperdoc, because the fields ARE the state and a twin
/// message would be a second implementation of one contract. The relay is the
/// other direction's exception and is not a consumer topic, so it lives in
/// ROUTES, not here.
pub const ALIASES: [(&str, &str); 1] = [("state", "ripperdoc")];

/// The type check for a (topic, body) pair: same name, or a declared alias.
pub fn body_allowed(topic: &str, body: &str) -> bool {
    topic == body || ALIASES.iter().any(|(t, b)| *t == topic && *b == body)
}

/// The relay: inbound topic -> consumer topic. A device pushes a frame under
/// its OWN name so the source is legible in the message itself; the cortex
/// publishes it under the name consumers subscribed to. The table, not a
/// string convention, is the routing authority — an inbound topic not in here
/// is refused, so a typo cannot buy a relayed frame.
pub const ROUTES: [(&str, &str); 2] = [
    // a rendered face rides as a Ripperdoc; the topic frame is all that changes
    ("oled", "ripperdoc"),
    ("ring", "ring"),
];

/// Where consumers subscribe. The cortex BINDS tcp://0.0.0.0:{FEED_PORT} — an
/// address a client cannot connect to — so a remote consumer names the body's
/// host. On the body itself, loopback is the honest default.
pub const FEED_PORT: u16 = 5556;

/// Where daemons feed the cortex. Local to the body, so loopback: nothing
/// outside the chassis has any business pushing into the brain.
pub const INBOUND_PORT: u16 = 5555;

// PUBLISHING MODEL: news. A message rides the wire the instant it becomes
// true — a frame the moment it's rendered, a reading the moment it's taken,
// a command's effect the moment state moves. Every message is full-shape; a
// consumer never merges. A sensor's sampling rate (a DHT reads at 1 Hz
// whether you like it or not) is hardware's clock and lives INSIDE the
// daemon; what the wire carries is the news, at its own rate. State lives in
// the cortex's RAM, so a late joiner gets the truth from `init` and news
// thereafter. JOIN_GRACE_S exists because PUB/SUB slow joiners are physics,
// not scheduling.

/// Long enough for a SUB to establish its subscription before the first
/// publish; PUB/SUB drops to a slow joiner.
pub const JOIN_GRACE_S: f64 = 0.25;

#[derive(Debug)]
pub enum Error {
    /// Publisher and subscriber disagree about the wire format.
    SchemaMismatch(u32),
    /// The topic frame and the payload inside it disagree.
    TopicMismatch { topic: String, body: String },
    /// A message on the feed whose topic nothing publishes.
    UnknownTopic(String),
    /// An inbound topic with no relay route.
    NoRoute(String),
    /// A ZMQ message that is not two frames.
    Framing(usize),
    /// A message with no body at all.
    NoBody,
    Zmq(String),
}

impl std::fmt::Display for Error {
    fn fmt(&self, s: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Error::SchemaMismatch(v) => write!(
                s,
                "wire schema v{v}, this build speaks v{SCHEMA_VERSION} — refusing to guess at the fields"
            ),
            Error::TopicMismatch { topic, body } => write!(
                s,
                "topic frame says {topic:?}, payload is {body:?} — refusing to guess which half is right"
            ),
            Error::UnknownTopic(t) => write!(s, "nobody publishes topic {t:?}"),
            Error::NoRoute(t) => {
                write!(s, "inbound topic {t:?} has no relay route — refusing to invent one")
            }
            Error::Framing(n) => {
                write!(s, "a message arrived with {n} frames, not 2 ([topic][envelope])")
            }
            Error::NoBody => write!(s, "an envelope arrived with an empty oneof"),
            Error::Zmq(e) => write!(s, "zmq: {e}"),
        }
    }
}

impl std::error::Error for Error {}

pub type Result<T> = std::result::Result<T, Error>;

fn zmq_err(e: zmq::Error) -> Error {
    Error::Zmq(e.to_string())
}

/// The name a body's Envelope carries, if any. This is the type check: the
/// oneof's field name, which matches the topic names by construction.
pub fn body_name(env: &pb::Envelope) -> Option<&'static str> {
    Some(match env.body.as_ref()? {
        pb::envelope::Body::Ripperdoc(_) => "ripperdoc",
        pb::envelope::Body::Ring(_) => "ring",
        pb::envelope::Body::Pir(_) => "pir",
        pb::envelope::Body::Sonar(_) => "sonar",
        pb::envelope::Body::Weather(_) => "weather",
        pb::envelope::Body::Power(_) => "power",
        pb::envelope::Body::Fault(_) => "fault",
        pb::envelope::Body::Event(_) => "event",
        pb::envelope::Body::Init(_) => "init",
    })
}

/// A message ready for the wire: [topic][Envelope] as two frames. The topic
/// must equal the payload's own name; a message whose topic and payload
/// disagree fails HERE, at the publisher, not in a consumer that would decode
/// it into plausible rubbish. (The relay does not go through encode — it swaps
/// a topic frame only; the consumer's `decode` is that path's referee.)
pub fn encode(topic: &str, env: &pb::Envelope) -> Result<[Vec<u8>; 2]> {
    if env.schema_version != SCHEMA_VERSION {
        return Err(Error::SchemaMismatch(env.schema_version));
    }
    let body = body_name(env).ok_or(Error::NoBody)?;
    if !body_allowed(topic, &body) {
        return Err(Error::TopicMismatch {
            topic: topic.into(),
            body: body.into(),
        });
    }
    Ok([topic.as_bytes().to_vec(), env.encode_to_vec()])
}

/// Build an Envelope carrying `body` at this build's schema. The one place
/// schema_version is stamped, so no caller can forget it.
pub fn envelope(body: pb::envelope::Body) -> pb::Envelope {
    pb::Envelope {
        schema_version: SCHEMA_VERSION,
        body: Some(body),
    }
}

/// One decoded message off the feed. This is the consumer-side referee for
/// BOTH paths: a state message and a relayed frame arrive identically shaped,
/// and either way the topic and the payload must agree or the schema must be
/// this build's. A relay that swapped in the wrong topic dies right here.
#[derive(Debug)]
pub struct Msg {
    pub topic: String,
    pub env: pb::Envelope,
}

pub fn decode(parts: [Vec<u8>; 2]) -> Result<Msg> {
    let topic = String::from_utf8_lossy(&parts[0]).into_owned();
    if !TOPICS.contains(&topic.as_str()) {
        return Err(Error::UnknownTopic(topic));
    }
    let env = pb::Envelope::decode(parts[1].as_slice())
        .map_err(|e| Error::Zmq(format!("envelope: {e}")))?;
    if env.schema_version != SCHEMA_VERSION {
        return Err(Error::SchemaMismatch(env.schema_version));
    }
    let body = body_name(&env).ok_or(Error::NoBody)?;
    if !body_allowed(&topic, &body) {
        return Err(Error::TopicMismatch { topic, body: body.into() });
    }
    Ok(Msg { topic, env })
}

/// The relay step: resolve an inbound topic through ROUTES, swap ONLY the
/// topic frame, pass the payload through byte-for-byte. This function must
/// never grow a parse.
pub fn route_parts(topic: &[u8], payload: &[u8]) -> Result<[Vec<u8>; 2]> {
    let inbound = std::str::from_utf8(topic)
        .map_err(|_| Error::UnknownTopic(String::from_utf8_lossy(topic).into_owned()))?;
    let out = ROUTES
        .iter()
        .find(|(from, _)| *from == inbound)
        .map(|(_, to)| *to)
        .ok_or_else(|| Error::NoRoute(inbound.to_string()))?;
    Ok([out.as_bytes().to_vec(), payload.to_vec()])
}

// ---------------------------------------------------------------------------
// The four socket roles. Each one is a thin holder for a libzmq socket plus
// the context that must outlive it — dropping a zmq Context closes its
// sockets, so the context lives in the struct, never per call.
// ---------------------------------------------------------------------------

/// The cortex's out-leg. The ONLY publisher in the body.
pub struct Publisher {
    _ctx: zmq::Context,
    sock: zmq::Socket,
    pub endpoint: String,
}

impl Publisher {
    /// Bind the feed. `endpoint` is tcp://0.0.0.0:5556 on the body — a client
    /// cannot connect to 0.0.0.0, so consumers name the host instead.
    pub fn bind(ctx: &zmq::Context, endpoint: &str) -> Result<Self> {
        let ctx = ctx.clone();
        let sock = ctx.socket(zmq::PUB).map_err(zmq_err)?;
        // A bounded window, not a single slot: the pipe holds roughly a
        // second of frames (120 at 120fps), arrivals past it drop at the
        // socket, and the consumer's drain keeps the newest. RCVHWM/SNDHWM
        // enforce min() across the pipe, so BOTH ends must carry the window
        // or the shallow one silently wins — measured with the e2e's 50-frame
        // burst, where sndhwm=1 let a stale frame hold the slot and the
        // newest frame of a finished burst never arrived at all.
        sock.set_sndhwm(120).map_err(zmq_err)?;
        sock.set_linger(0).map_err(zmq_err)?;
        sock.bind(endpoint).map_err(zmq_err)?;
        Ok(Self { _ctx: ctx, sock, endpoint: endpoint.into() })
    }

    /// Publish a state message: encodes through the contract, so a
    /// topic/payload disagreement fails here, at the source.
    pub fn publish(&self, topic: &str, env: &pb::Envelope) -> Result<()> {
        let [name, body] = encode(topic, env)?;
        self.sock.send_multipart([name, body], zmq::DONTWAIT).map_err(zmq_err)?;
        Ok(())
    }

    /// Publish an already-routed relay pair (from `route_parts`). No re-check:
    /// the relay was built by swapping a frame the contract already saw.
    pub fn relay(&self, pair: [Vec<u8>; 2]) -> Result<()> {
        // A bounded retry, and the WHY is a race the e2e test caught: with
        // sndhwm=1 an EAGAIN drops the frame, and in a news-only feed the
        // LAST frame of a burst may be the only one that ever matters — a
        // still device sends no "next" to heal a lost 49. Retry for a few
        // ms so the newest wins against a momentarily-full queue; a
        // permanently wedged consumer still can't stall the body, because
        // the window is finite and expiry swallows the drop (PUB semantics,
        // just delayed).
        for _ in 0..8 {
            match self
                .sock
                .send_multipart([pair[0].as_slice(), pair[1].as_slice()], zmq::DONTWAIT)
            {
                Ok(()) => return Ok(()),
                Err(zmq::Error::EAGAIN) => {
                    std::thread::sleep(std::time::Duration::from_micros(500))
                }
                Err(e) => return Err(zmq_err(e)),
            }
        }
        Ok(())
    }
}

/// Every consumer and every daemon's in-leg. Subscribes by topic NAME; ZMQ
/// does the filtering natively, so a ring consumer never sees a pixel.
pub struct Subscriber {
    _ctx: zmq::Context,
    sock: zmq::Socket,
    pub endpoints: Vec<String>,
}

impl Subscriber {
    pub fn connect(ctx: &zmq::Context, endpoints: &[String], topics: &[&str]) -> Result<Self> {
        let ctx = ctx.clone();
        let sock = ctx.socket(zmq::SUB).map_err(zmq_err)?;
        sock.set_linger(0).map_err(zmq_err)?;
        // Newest-wins, with a bounded window: the queue holds up to
        // RCVHWM frames, `drain` keeps only the newest of what arrived, and
        // arrivals past the mark are dropped at the socket. NOT RCVHWM=1:
        // with a single slot, a stale frame OCCUPIES it and the socket
        // drops the NEW message — a stalled consumer resumes on old data,
        // and a still device sends nothing to heal it. The queue is the
        // last ~second of frames (120 at frame rate); the lie it can tell
        // is bounded by that window, never by "whenever the consumer last
        // looked". NOT ZMQ_CONFLATE either: measured on this stack (the
        // e2e's 50-frame burst, 2026-09-18), it accepts the option and
        // delivers zero — multipart messages die in it, silently.
        sock.set_rcvhwm(120).map_err(zmq_err)?;
        for ep in endpoints {
            sock.connect(ep).map_err(zmq_err)?;
        }
        for t in topics {
            sock.set_subscribe(t.as_bytes()).map_err(zmq_err)?;
        }
        Ok(Self { _ctx: ctx, sock, endpoints: endpoints.to_vec() })
    }

    /// Take everything queued and keep only the newest full message — the
    /// consumer shows the latest it can and never works through old ones.
    pub fn drain(&mut self) -> Result<Option<Msg>> {
        let mut last = None;
        loop {
            // Poll for a whole message, don't peek ZMQ_RCVMORE first: that
            // flag only means anything MID-message, so a drain that checked
            // it before receiving ever read the first frame as "no more" and
            // returned None — silently, forever. The e2e relay caught it.
            let mut item = self.sock.as_poll_item(zmq::POLLIN);
            let ready = zmq::poll(&mut [item], 0).map_err(zmq_err)?;
            if ready == 0 {
                break;
            }
            let topic = self.sock.recv_bytes(zmq::DONTWAIT).map_err(zmq_err)?;
            let payload = self.sock.recv_bytes(zmq::DONTWAIT).map_err(zmq_err)?;
            last = Some(decode([topic.to_vec(), payload.to_vec()])?);
        }
        Ok(last)
    }
}

/// The daemon's out-leg: PUSHes readings and frames to the cortex's Receiver.
pub struct Sender {
    _ctx: zmq::Context,
    sock: zmq::Socket,
    pub endpoint: String,
}

impl Sender {
    pub fn connect(ctx: &zmq::Context, endpoint: &str) -> Result<Self> {
        let ctx = ctx.clone();
        let sock = ctx.socket(zmq::PUSH).map_err(zmq_err)?;
        // PUSH blocks when the cortex is not there — never on this body:
        // a daemon stalled on a send stops driving its own glass. HIGHWATER
        // bounds the queue instead; the cortex's PULL drains it.
        sock.set_sndhwm(1000).map_err(zmq_err)?;
        sock.connect(endpoint).map_err(zmq_err)?;
        Ok(Self { _ctx: ctx, sock, endpoint: endpoint.into() })
    }

    /// Push a state reading (topic == payload name, checked in encode).
    pub fn push(&self, topic: &str, env: &pb::Envelope) -> Result<()> {
        let [name, body] = encode(topic, env)?;
        self.sock.send_multipart([name, body], zmq::DONTWAIT).map_err(zmq_err)?;
        Ok(())
    }

    /// Push a finished frame under the device's OWN name ("oled"/"ring").
    /// No encode — the payload is the device's own bytes and the contract's
    /// referee lives at the consumer's decode.
    pub fn push_frame(&self, topic: &str, env: &pb::Envelope) -> Result<()> {
        let [name, body] = [topic.as_bytes().to_vec(), env.encode_to_vec()];
        self.sock.send_multipart([name, body], zmq::DONTWAIT).map_err(zmq_err)?;
        Ok(())
    }
}

/// The cortex's in-leg: the only way a reading or frame reaches the body's
/// brain. PUSH/PULL queues; a reading never vanishes because someone was slow.
pub struct Receiver {
    _ctx: zmq::Context,
    sock: zmq::Socket,
    pub endpoint: String,
}

impl Receiver {
    pub fn bind(ctx: &zmq::Context, endpoint: &str) -> Result<Self> {
        let ctx = ctx.clone();
        let sock = ctx.socket(zmq::PULL).map_err(zmq_err)?;
        sock.bind(endpoint).map_err(zmq_err)?;
        Ok(Self { _ctx: ctx, sock, endpoint: endpoint.into() })
    }

    /// One inbound message, or None if nothing is waiting.
    pub fn recv_nowait(&self) -> Result<Option<[Vec<u8>; 2]>> {
        let item = self.sock.as_poll_item(zmq::POLLIN);
        let ready = zmq::poll(&mut [item], 0).map_err(zmq_err)?;
        if ready == 0 {
            return Ok(None);
        }
        let topic = self.sock.recv_bytes(zmq::DONTWAIT).map_err(zmq_err)?;
        let payload = self.sock.recv_bytes(zmq::DONTWAIT).map_err(zmq_err)?;
        Ok(Some([topic.to_vec(), payload.to_vec()]))
    }

    /// Blocking one-message wait for state readings.
    pub fn recv(&self) -> Result<[Vec<u8>; 2]> {
        let topic = self.sock.recv_bytes(0).map_err(zmq_err)?;
        let payload = self.sock.recv_bytes(0).map_err(zmq_err)?;
        Ok([topic.to_vec(), payload.to_vec()])
    }
}

// ---------------------------------------------------------------------------
// The async face. Same libzmq underneath; this changes who waits.
//
// A zmq socket's file descriptor (ZMQ_FD) is epoll-visible by design: readable
// means messages are pending. tokio's AsyncFd registers that FD with the
// reactor, so a daemon can `await` its inbox instead of parking a thread in
// recv. No second ZMTP, no reimplementation — libzmq still owns the wire,
// tokio only owns the waiting.
//
// TCP keepalive is on for the PUSH/PULL leg: a dead peer is a dead pipe the
// TRANSPORT reports in ~20 seconds. That is the liveness check — there is no
// heartbeat message on this wire, and publishing one would put the tick back
// in through the side door.
// ---------------------------------------------------------------------------
#[cfg(feature = "async")]
pub mod aio {
    use super::*;
    use std::os::unix::io::{AsRawFd, RawFd};
    use tokio::io::unix::AsyncFd;

    /// The FD, as far as tokio is concerned. Ownership stays with the
    /// zmq::Socket this borrows from: AInbound declares `fd` before `sock`,
    /// so the watcher deregisters before the socket closes the FD.
    struct FdRef(RawFd);
    impl AsRawFd for FdRef {
        fn as_raw_fd(&self) -> RawFd {
            self.0
        }
    }

    fn keepalive(sock: &zmq::Socket) -> Result<()> {
        // ~20 seconds to notice a peer that stopped answering, from the
        // transport itself: idle 5s, then 3 probes 5s apart.
        sock.set_tcp_keepalive(1).map_err(zmq_err)?;
        sock.set_tcp_keepalive_idle(5).map_err(zmq_err)?;
        sock.set_tcp_keepalive_intvl(5).map_err(zmq_err)?;
        sock.set_tcp_keepalive_cnt(3).map_err(zmq_err)?;
        Ok(())
    }

    fn watch(sock: &zmq::Socket) -> Result<AsyncFd<FdRef>> {
        let fd = sock.get_fd().map_err(zmq_err)?;
        AsyncFd::new(FdRef(fd)).map_err(|e| Error::Zmq(e.to_string()))
    }

    /// The awaiting side of the contract: a SUB (a daemon or consumer
    /// receiving state) or a PULL (the cortex's inbox). Both hand back
    /// FRAMES; what happens next is decided by the holder.
    pub struct AInbound {
        // Dropped first (declaration order): the reactor forgets the FD
        // before the socket closes it.
        fd: AsyncFd<FdRef>,
        sock: zmq::Socket,
    }

    impl AInbound {
        /// Subscribe to the feed. The RCVHWM=1 / newest-wins discipline is
        /// `Subscriber`'s and is repeated here: an async consumer that falls
        /// behind must show the NEWEST frame, not work through a backlog.
        pub fn sub(ctx: &zmq::Context, endpoints: &[String], topics: &[&str]) -> Result<Self> {
            let sock = ctx.socket(zmq::SUB).map_err(zmq_err)?;
            sock.set_linger(0).map_err(zmq_err)?;
            // RCVHWM=120, not 1: a one-frame queue lets a STALE frame
            // block the new one and a still device never replaces it.
            // drain-to-newest over a bounded queue is the behaviour; see
            // Subscriber::connect for the full why.
            sock.set_rcvhwm(120).map_err(zmq_err)?;
            for ep in endpoints {
                sock.connect(ep).map_err(zmq_err)?;
            }
            for t in topics {
                sock.set_subscribe(t.as_bytes()).map_err(zmq_err)?;
            }
            keepalive(&sock)?;
            let fd = watch(&sock)?;
            Ok(Self { fd, sock })
        }

        /// The cortex's inbox: bind the inbound PUSH/PULL port. Queued, never
        /// dropped — a reading that vanishes is a lie about the body.
        pub fn pull(ctx: &zmq::Context, endpoint: &str) -> Result<Self> {
            let sock = ctx.socket(zmq::PULL).map_err(zmq_err)?;
            sock.bind(endpoint).map_err(zmq_err)?;
            keepalive(&sock)?;
            let fd = watch(&sock)?;
            Ok(Self { fd, sock })
        }

        /// Park until at least one message is pending.
        pub async fn readable(&self) -> Result<()> {
            let mut r = self.fd.readable().await.map_err(|e| Error::Zmq(e.to_string()))?;
            r.clear_ready();
            Ok(())
        }

        /// One message if one is here, without waiting. The relay batch loop
        /// drains with this so every queued frame is forwarded.
        pub fn recv_nowait(&self) -> Result<Option<[Vec<u8>; 2]>> {
            let Ok(topic) = self.sock.recv_bytes(zmq::DONTWAIT) else {
                return Ok(None);
            };
            let payload = self.sock.recv_bytes(zmq::DONTWAIT).map_err(zmq_err)?;
            Ok(Some([topic, payload]))
        }

        /// Await the newest full message and decode it through the contract.
        /// A state consumer's whole loop: newest truth, or sleep until there
        /// is one.
        pub async fn next_state(&self) -> Result<Msg> {
            loop {
                self.readable().await?;
                // Drain to newest: RCVHWM=1 means at most one frames-ahead
                // per pipe, and the newest is the only one worth drawing.
                let mut newest = None;
                while let Some(parts) = self.recv_nowait()? {
                    newest = Some(parts);
                }
                if let Some(parts) = newest {
                    return decode(parts);
                }
            }
        }
    }
}
