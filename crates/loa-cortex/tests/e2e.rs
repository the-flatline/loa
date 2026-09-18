//! The relay, over real sockets: a device pushes a frame up, a consumer
//! downstream sees it on its topic — routed, intact, in order. No
use loa_wire::{aio, decode, envelope, pb, route_parts, Sender, SCHEMA_VERSION};
use prost::Message as _;
use std::sync::{Mutex, OnceLock};
use std::time::Instant;

fn seen() -> &'static Mutex<Vec<f64>> {
    static SEEN: OnceLock<Mutex<Vec<f64>>> = OnceLock::new();
    SEEN.get_or_init(|| Mutex::new(Vec::new()))
}

fn face_frame(i: u8) -> pb::Envelope {
    envelope(pb::envelope::Body::Ripperdoc(pb::Ripperdoc {
        face: Some(vec![i; 1024]),
        ts: Some(f64::from(i)),
        ..Default::default()
    }))
}

/// A fake device pushes N face frames up as "oled"; a relay (exactly what
/// main's loop does) forwards them; a consumer on the feed gets them as
/// "ripperdoc" with bytes intact. The relay is route_parts + publish — if
/// this passes, main's relay leg has nothing left to invent.
#[tokio::test]
async fn frames_relay_from_device_to_consumer_intact() {

    let ctx = zmq::Context::new();
    // bind on the collector and the feed; the device and the consumer
    // connect. zmq peers only form bind<->connect — connect<->connect is a
    // silent nothing, which is exactly how this test lost every frame.
    let relay_in = aio::AInbound::pull(&ctx, "tcp://127.0.0.1:19010").unwrap();
    let dev = Sender::connect(&ctx, "tcp://127.0.0.1:19010").unwrap();
    let feed = loa_wire::Publisher::bind(&ctx, "tcp://127.0.0.1:19011").unwrap();
    let mut sub = loa_wire::Subscriber::connect(&ctx, &["tcp://127.0.0.1:19011".into()], &["ripperdoc"]).unwrap();
    tokio::time::sleep(std::time::Duration::from_millis(150)).await;

    // device: 50 frames, indexed
    for i in 0..50u8 {
        dev.push_frame("oled", &face_frame(i)).unwrap();
    }
    // relay: drain everything that landed and route it (main's hot leg)
    let deadline = Instant::now() + std::time::Duration::from_secs(5);
    let mut quiet = 0;
    let mut relayed = 0;
    while quiet < 3 {
        let mut n = 0;
        while let Some(parts) = relay_in.recv_nowait().unwrap() {
            let routed = route_parts(&parts[0], &parts[1]).unwrap();
            assert_eq!(&routed[0], b"ripperdoc", "oled must route to ripperdoc");
            feed.relay(routed).unwrap();
            n += 1;
            relayed += 1;
        }
        // quiet only counts once something HAS arrived: TCP delivery of the
        // first packet can lag the pushes by a few ms, and a loop that
        // starts counting quiet before the first frame lands relays nothing
        // and calls it done.
        quiet = if n > 0 || relayed > 0 { if n > 0 { 0 } else { quiet + 1 } } else { 0 };
        assert!(Instant::now() < deadline, "relay saw nothing ({} relayed)", relayed);
        tokio::time::sleep(std::time::Duration::from_millis(2)).await;
    }
    assert_eq!(relayed, 50, "PUSH->PULL queues, it does not drop: all 50 must reach the relay");
    // consumer: the burst fits the 120-frame window on both ends, so the
    // newest frame must land; older frames may or may not — PUB drops are
    // allowed, holding the LAST is not negotiable (news-only feeds have no
    // next frame to heal a lost one).
    let msg = loop {
        if let Some(m) = sub.drain().unwrap() {
            let newest = m.env.body.as_ref().and_then(|b| match b {
                pb::envelope::Body::Ripperdoc(r) => r.ts, _ => None });
            if let Some(ts) = newest {
                let mut v = seen().lock().unwrap();
                if !v.contains(&ts) { v.push(ts); }
            }
            if newest == Some(49.0) { break m; }
        }
        assert!(Instant::now() < deadline + std::time::Duration::from_secs(5),
                "frame 49 never reached the consumer: saw {:?}", seen().lock().unwrap());
        tokio::time::sleep(std::time::Duration::from_millis(2)).await;
    };
    assert_eq!(msg.topic, "ripperdoc");
    let rd = match msg.env.body { Some(pb::envelope::Body::Ripperdoc(r)) => r, _ => panic!() };
    assert_eq!(rd.face.unwrap(), vec![49u8; 1024], "newest frame, bytes intact");
    assert_eq!(msg.env.schema_version, SCHEMA_VERSION);
}
