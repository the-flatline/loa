//! End-to-end over real sockets: bind a fake feed (PUB) and a fake inbound
//! (PULL), spawn the daemon against them, and assert what the doctrine says
//! the ring must do: state down, frames up, the mute gate closed until a
//! fault message arrives, and an event's one-shot reaching the wire.
//!
//! Not part of `cargo test --lib` — run with:
//!     cargo test --manifest-path crates/loa-ring/Cargo.toml --test e2e -- --ignored

use std::process::Command;
use std::time::{Duration, Instant};

use prost::Message as _;

use loa_wire::{decode, envelope, pb, Publisher, Receiver};

fn frame_rgb(buf: &[u8]) -> (u8, u8, u8) {
    // Max of each channel across the 24 LEDs: enough to tell a voice from
    // its brightest pixel (mute red <= 15, breath blue <= 35, comet blue
    // ~78, glitch cyan ~153).
    let (mut r, mut g, mut b) = (0u8, 0u8, 0u8);
    for px in buf.chunks_exact(3) {
        r = r.max(px[0]);
        g = g.max(px[1]);
        b = b.max(px[2]);
    }
    (r, g, b)
}

#[test]
#[ignore = "spawns the daemon and binds sockets; run with --ignored"]
fn state_down_frames_up() {
    let ctx = zmq::Context::new();
    let feed = Publisher::bind(&ctx, "tcp://127.0.0.1:15656").expect("feed bind");
    let inbox = Receiver::bind(&ctx, "tcp://127.0.0.1:15655").expect("inbox bind");

    let exe = env!("CARGO_BIN_EXE_loa-ring");
    let mut child = Command::new(exe)
        .args([
            "--no-strip",
            "--feed",
            "tcp://127.0.0.1:15656",
            "--inbound",
            "tcp://127.0.0.1:15655",
            "--secs",
            "12",
        ])
        .spawn()
        .expect("spawn loa-ring");

    // PUB/SUB slow joiner: give the SUB its grace before the first news.
    std::thread::sleep(Duration::from_millis(700));

    let collect = |ms: u64| -> Vec<(u8, u8, u8)> {
        let until = Instant::now() + Duration::from_millis(ms);
        let mut v = Vec::new();
        while Instant::now() < until {
            if let Ok(Some(parts)) = inbox.recv_nowait() {
                assert_eq!(parts[0], b"ring".to_vec(), "inbound topic must be `ring`");
                assert!(
                    contract_holds(&parts),
                    "the pushed envelope passes the wire referee"
                );
                let env = pb::Envelope::decode(&parts[1][..]).expect("a frame envelope decodes");
                let body = env.body.expect("frame envelope carries a body");
                let pb::envelope::Body::Ring(m) = body else {
                    panic!("inbound `ring` carries {:?}", std::mem::discriminant(&body))
                };
                let rgb = m.ring.expect("a pushed frame carries pixels");
                assert_eq!(rgb.len(), 72, "a frame is 72 bytes");
                v.push(frame_rgb(&rgb));
            } else {
                std::thread::sleep(Duration::from_millis(2));
            }
        }
        v
    };

    // Phase 1: no fault has ever arrived. The mute gate holds regardless of
    // what state says — frames must still flow (news, own clock), and no
    // frame may light above the mute blink (white 15,15,15).
    feed.publish(
        "state",
        &envelope(pb::envelope::Body::Ripperdoc(pb::Ripperdoc {
            ring_state: Some("home".into()),
            condition: Some("well".into()),
            ..Default::default()
        })),
    )
    .unwrap();
    let muted = collect(1500);
    assert!(
        muted.len() > 200,
        "the render clock runs uncapped: got {} frames",
        muted.len()
    );
    assert!(
        muted
            .iter()
            .all(|(r, g, b)| *r <= 16 && *g <= 16 && *b <= 16),
        "mute gate: no fault seen, nothing above the blink: {muted:?}",
    );

    // Phase 2: a fault arrives — the gate opens and the well breath takes
    // the ring. A green breath peaks at 35 with zero blue.
    feed.publish(
        "fault",
        &envelope(pb::envelope::Body::Fault(pb::Fault {
            rows: vec![],
            condition: Some("well".into()),
            ts: Some(0.0),
        })),
    )
    .unwrap();
    let home = collect(1000);
    assert!(home.len() > 200, "frames keep flowing: got {}", home.len());
    assert!(
        home.iter().any(|(r, g, _b)| *g > 20 && *r < 5),
        "the breath must reach the wire: {home:?}"
    );

    // Phase 3: an event kind "ring" detail.state "scan" is a one-shot
    // record — the comet (hue ~150: blue ~78 at the head) must pass.
    feed.publish(
        "event",
        &envelope(pb::envelope::Body::Event(pb::Event {
            ts: 1.0,
            kind: "ring".into(),
            detail: [("state".to_string(), "scan".to_string())]
                .into_iter()
                .collect(),
        })),
    )
    .unwrap();
    let mut saw_comet = false;
    for _ in 0..25 {
        for f in collect(100) {
            if f.2 >= 40 && f.2 <= 140 {
                saw_comet = true;
            }
        }
        if saw_comet {
            break;
        }
    }
    assert!(saw_comet, "the scan one-shot must ride the wire as frames");

    // Phase 4: a glitch event — the coins are saturated cyan (b = 153).
    feed.publish(
        "event",
        &envelope(pb::envelope::Body::Event(pb::Event {
            ts: 2.0,
            kind: "ring".into(),
            detail: [("state".to_string(), "glitch".to_string())]
                .into_iter()
                .collect(),
        })),
    )
    .unwrap();
    let mut saw_glitch = false;
    for _ in 0..10 {
        for f in collect(100) {
            if f.2 >= 140 {
                saw_glitch = true;
            }
        }
        if saw_glitch {
            break;
        }
    }
    assert!(
        saw_glitch,
        "the glitch one-shot must ride the wire as frames"
    );

    // --secs 12 is still running; it exits on its own.
    let status = child.wait().expect("wait for loa-ring");
    assert!(status.success(), "clean exit: {status}");
}

/// The relay's referee on the consumer side: `decode` accepts the topic
/// frame and the body name together — the pushed `ring` envelope must pass
/// it exactly as the relayed leg would.
fn contract_holds(parts: &[Vec<u8>; 2]) -> bool {
    decode(parts.to_vec().try_into().unwrap()).is_ok()
}
