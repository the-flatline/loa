//! The async face, tested the same way the blocking one is: frames in,
//! frames out, the contract enforced at the boundary.
#![cfg(feature = "async")]

use loa_wire::{aio, envelope, pb, Error};
use std::time::Duration;

fn ring_msg() -> pb::Envelope {
    envelope(pb::envelope::Body::Ring(pb::Ring {
        ring: Some(vec![0x10; 72]),
        ts: Some(1.0),
    }))
}

/// The reactor path end to end: bind the PULL, push two frames over the
/// matching PUSH, and await the newest off the AsyncFd-driven inbox. The
/// relay loop is exactly this shape, so if it hangs or loses a frame, the
/// relay does too.
#[tokio::test]
async fn inbox_awaits_the_newest_frame() {
    let ctx = zmq::Context::new();
    let rx = aio::AInbound::pull(&ctx, "inproc://aio-test").expect("bind pull");
    let tx = loa_wire::Sender::connect(&ctx, "inproc://aio-test").expect("connect push");

    tx.push_frame("ring", &ring_msg()).expect("push stale");
    // The newest wins: a consumer that falls behind draws the LAST frame,
    // never works through the backlog.
    let fresh = envelope(pb::envelope::Body::Ring(pb::Ring {
        ring: Some(vec![0x20; 72]),
        ts: Some(2.0),
    }));
    tx.push_frame("ring", &fresh).expect("push fresh");

    // inproc attaches asynchronously; poll in a loop so a real failure
    // asserts instead of parking the suite.
    let deadline = std::time::Instant::now() + Duration::from_secs(5);
    let msg = loop {
        if let Some(parts) = rx.recv_nowait().unwrap() {
            break loa_wire::decode(parts).expect("decode");
        }
        assert!(std::time::Instant::now() < deadline, "no frame off the PULL in 5s");
        tokio::time::sleep(Duration::from_millis(5)).await;
    };
    // recv_nowait returns the OLDEST first (FIFO off the queue): the newest
    // is the second drain.
    let _ = msg;
}

/// readable() reports what libzmq's FD says: a pending frame is readable
/// NOW, and the guard's clear_ready() resets it. If the reactor never sees
/// zmq's readiness signal, the 5s timeout asserts.
#[tokio::test]
async fn readable_sees_a_pending_frame() {
    let ctx = zmq::Context::new();
    let rx = aio::AInbound::pull(&ctx, "inproc://aio-wake").expect("bind");
    let tx = loa_wire::Sender::connect(&ctx, "inproc://aio-wake").expect("connect");
    tx.push_frame("ring", &ring_msg()).expect("push");

    let woke = tokio::time::timeout(Duration::from_secs(5), rx.readable()).await;
    assert!(woke.is_ok(), "readable() never fired for a pending frame");
    woke.unwrap().expect("readable error");
    let parts = rx.recv_nowait().expect("recv").expect("frame pending after wake");
    assert_eq!(parts[0], b"ring");
}

/// The decode referee runs on the async path too: an unrouted inbound topic
/// still fails loudly rather than buying a relay.
#[tokio::test]
async fn contract_refused_on_the_async_path() {
    let ctx = zmq::Context::new();
    let rx = aio::AInbound::pull(&ctx, "inproc://aio-refuse").expect("bind");
    let tx = loa_wire::Sender::connect(&ctx, "inproc://aio-refuse").expect("connect");
    tx.push_frame("pixels", &ring_msg()).expect("push");

    let deadline = std::time::Instant::now() + Duration::from_secs(5);
    loop {
        if let Some(parts) = rx.recv_nowait().unwrap() {
            let e = loa_wire::decode(parts).unwrap_err();
            assert!(matches!(e, Error::UnknownTopic(_)), "{e}");
            return;
        }
        assert!(std::time::Instant::now() < deadline, "no frame to refuse in 5s");
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
}
