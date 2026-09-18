//! The contract's own tests. If encode/decode/route_parts are wrong, every
//! daemon downstream is wrong, so they are tested adversarially the way the
//! wire has actually behaved in the field (phantom LEDs, three-frames-not-two).

use loa_wire::{
    body_name, decode, encode, envelope, pb, route_parts, Error, SCHEMA_VERSION,
};
use prost::Message as _;

fn face_msg() -> pb::Envelope {
    envelope(pb::envelope::Body::Ripperdoc(pb::Ripperdoc {
        face: Some(vec![0xAB; 1024]),
        page: Some("mood".into()),
        ..Default::default()
    }))
}

fn ring_msg() -> pb::Envelope {
    envelope(pb::envelope::Body::Ring(pb::Ring {
        ring: Some(vec![0x10; 72]),
        ..Default::default()
    }))
}

#[test]
fn state_round_trips_through_the_contract() {
    let [name, body] = encode("ripperdoc", &face_msg()).expect("valid pair");
    let msg = decode([name, body]).expect("round trip");
    assert_eq!(msg.topic, "ripperdoc");
    match msg.env.body.unwrap() {
        pb::envelope::Body::Ripperdoc(r) => assert_eq!(r.face.unwrap(), vec![0xAB; 1024]),
        other => panic!("oneof drifted: {other:?}"),
    }
}

#[test]
fn topic_and_payload_must_agree_at_the_publisher() {
    // the phantom-LED failure: a right-SIZED payload with the wrong subject.
    let e = encode("ring", &face_msg()).unwrap_err();
    assert!(matches!(e, Error::TopicMismatch { .. }), "{e}");
    let e = encode("ripperdoc", &ring_msg()).unwrap_err();
    assert!(matches!(e, Error::TopicMismatch { .. }), "{e}");
}

#[test]
fn a_foreign_schema_fails_on_arrival_not_by_misreading() {
    let mut env = face_msg();
    let other = SCHEMA_VERSION - 2;
    env.schema_version = other;
    // encode refuses to SEND it... (the version, not a literal: a test that
    // hardcodes the number breaks on every bump and proves nothing between)
    let e = encode("ripperdoc", &env).unwrap_err();
    assert!(matches!(e, Error::SchemaMismatch(v) if v == other), "{e}");
    // ...and a hand-built frame fails at the consumer's decode.
    let raw = env.encode_to_vec();
    let e = decode([b"ripperdoc".to_vec(), raw]).unwrap_err();
    assert!(matches!(e, Error::SchemaMismatch(v) if v == other), "{e}");
}

#[test]
fn a_topic_nobody_publishes_is_refused() {
    let e = decode([b"pixels".to_vec(), vec![]]).unwrap_err();
    assert!(matches!(e, Error::UnknownTopic(ref t) if t == "pixels"), "{e}");
}

#[test]
fn relay_routes_the_topic_and_touches_nothing_else() {
    let payload = face_msg().encode_to_vec();
    let [out_topic, out_body] = route_parts(b"oled", &payload).expect("oled has a route");
    assert_eq!(out_topic, b"ripperdoc");
    assert_eq!(out_body, payload, "payload must survive byte-for-byte");

    // the consumer-side referee accepts the relayed frame as a normal Ripperdoc
    let msg = decode([out_topic, out_body]).expect("relayed frame decodes");
    assert_eq!(msg.topic, "ripperdoc");
    assert_eq!(body_name(&msg.env), Some("ripperdoc"));
}

#[test]
fn ring_relays_under_its_own_name_unchanged() {
    let payload = ring_msg().encode_to_vec();
    let [t, b] = route_parts(b"ring", &payload).expect("ring has a route");
    assert_eq!(t, b"ring");
    assert_eq!(b, payload);
}

#[test]
fn an_unrouted_inbound_topic_is_refused_not_invented() {
    // baro is dead as a topic (v9 folded it); init is a control, never a frame.
    let e = route_parts(b"baro", b"whatever").unwrap_err();
    assert!(matches!(e, Error::NoRoute(ref t) if t == "baro"), "{e}");
    let e = route_parts(b"init", b"nope").unwrap_err();
    assert!(matches!(e, Error::NoRoute(_)), "{e}");
}

#[test]
fn empty_oneof_is_an_error_not_a_default_face() {
    let bare = pb::Envelope { schema_version: SCHEMA_VERSION, body: None };
    let e = encode("ring", &bare).unwrap_err();
    assert!(matches!(e, Error::NoBody), "{e}");
}

#[test]
fn push_pull_round_trips_over_inproc() {
    // the two-frame contract, over REAL sockets. ONE context for both roles:
    // inproc endpoints only exist inside the context that minted them, and a
    // Sender from another context would queue into nothing forever.
    let ctx = zmq::Context::new();
    let rx = loa_wire::Receiver::bind(&ctx, "inproc://loa-test").expect("bind");
    let tx = loa_wire::Sender::connect(&ctx, "inproc://loa-test").expect("connect");
    tx.push_frame("oled", &face_msg()).expect("push");
    // poll out instead of blocking forever: the pipe attaching is async, but
    // a test that can hang is a test that tells you nothing when it fails.
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
    let got = loop {
        if let Some(parts) = rx.recv_nowait().expect("nowait") {
            break parts;
        }
        assert!(std::time::Instant::now() < deadline, "no message off the PULL in 5s");
        std::thread::sleep(std::time::Duration::from_millis(5));
    };
    let [topic, body] = got;
    assert_eq!(topic, b"oled");
    assert_eq!(body, face_msg().encode_to_vec());
}

#[test]
fn the_state_leg_carries_a_faceless_ripperdoc() {
    // "state" is the down-leg topic: presentation fields to the devices, and
    // its payload is a face-less Ripperdoc. The alias is declared, so the
    // pair passes; an undeclared pair still fails.
    let rd = pb::Ripperdoc {
        page: Some("sensors".into()),
        mood: Some("calm".into()),
        // face: None — a state message has no pixels. That is the whole
        // difference between the down leg and the up leg.
        ..Default::default()
    };
    let env = envelope(pb::envelope::Body::Ripperdoc(rd));
    let parts = encode("state", &env).expect("the alias must be declared");
    let msg = decode(parts).expect("decode");
    assert_eq!(msg.topic, "state");
    match msg.env.body {
        Some(pb::envelope::Body::Ripperdoc(rd)) => assert!(rd.face.is_none()),
        other => panic!("state leg must carry a Ripperdoc, got {other:?}"),
    }
}

#[test]
fn an_alias_is_only_for_its_own_topic() {
    // state<->ripperdoc is the declared alias; state<->ring is not a thing.
    let env = envelope(pb::envelope::Body::Ring(pb::Ring {
        ring: Some(vec![0u8; 72]),
        ..Default::default()
    }));
    let e = encode("state", &env).unwrap_err();
    assert!(matches!(e, Error::TopicMismatch { .. }), "{e}");
    // and the consumer topic never aliases: "ripperdoc" carrying a Ring dies.
    let e = encode("ripperdoc", &env).unwrap_err();
    assert!(matches!(e, Error::TopicMismatch { .. }), "{e}");
}

#[test]
fn a_publisher_reaches_a_subscriber_over_tcp() {
    // The e2e relay found the gap: every socket test so far was PUSH->PULL.
    // PUB->SUB is the whole feed; prove the pair, with the slow-joiner
    // grace the contract promises.
    let ctx = zmq::Context::new();
    let pub_sock = loa_wire::Publisher::bind(&ctx, "tcp://127.0.0.1:19020").unwrap();
    let mut sub = loa_wire::Subscriber::connect(
        &ctx, &["tcp://127.0.0.1:19020".into()], &["ripperdoc"]).unwrap();
    std::thread::sleep(std::time::Duration::from_millis(250));
    let env = face_msg();
    pub_sock.publish("ripperdoc", &env).unwrap();
    let t0 = std::time::Instant::now();
    let msg = loop {
        if let Some(m) = sub.drain().unwrap() { break m; }
        assert!(t0.elapsed() < std::time::Duration::from_secs(3), "SUB saw nothing");
        std::thread::sleep(std::time::Duration::from_millis(2));
    };
    assert_eq!(msg.topic, "ripperdoc");
}
