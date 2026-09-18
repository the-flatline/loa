//! loa-cortex — the coordinator, as a process.
//!
//! One publisher task owns the PUB socket. libzmq's one-thread-per-socket
//! rule is enforced by the type system (Send, not Sync), so every publish
//! funnels through this one task:
//!
//!   inbox    the PULL leg: frames get relayed byte-for-byte with only the
//!            topic frame swapped; readings get decoded, merged into state,
//!            and republished whole (the merge leg of the same channel).
//!   news     the door's thread posts StateChanged / Event — commands land
//!            on the feed the moment the verb lands. News, never a tick.
//!   power    rails are read on the body once a second; what rides the wire
//!            is a CHANGE, not the sample. Hardware's clock, inside the
//!            daemon — the publishing rule applies to us too.
//!
//! Boot order is the init handshake: bind everything, then publish `init`
//! six times over five seconds (a PUB/SUB slow joiner misses the first
//! ask), while daemons answer by re-pushing their full payloads. Then the
//! store flusher and the door get their threads and the loop takes over.

use std::collections::HashMap;
use std::process;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde_json::Value;

use loa_wire::{
    aio, decode, envelope, pb, route_parts, Publisher, FEED_PORT, INBOUND_PORT, JOIN_GRACE_S,
};

use loa_cortex::build;
use loa_cortex::http::{Door, News};
use loa_cortex::state::{now_s, Cortex};
use loa_cortex::store::Store;
use loa_cortex::vault::Vault;

/// What the publisher hands the store thread: a record of something that
/// happened, or a sample of the air. One channel, one writer, no races.
enum Write {
    Event { kind: String, detail: Value, ts: f64 },
    Baro { ts: f64, hpa: f64 },
}


/// The rails, on the body. `vcgencmd` is the only door to the PMIC; it is
/// slow (tens of ms), blocking, and absent off-Pi — an empty map is the
/// honest answer there. Called once a second from a blocking thread; the
/// publish side sends only when a value CHANGED.
fn read_rails() -> HashMap<String, f64> {
    let mut out = HashMap::new();
    let Ok(pmic) = std::process::Command::new("vcgencmd")
        .arg("pmic_read_adc")
        .output()
    else {
        return out;
    };
    if pmic.status.success() {
        let text = String::from_utf8_lossy(&pmic.stdout);
        for line in text.lines() {
            let Some((k, v)) = line.split_once('=') else { continue };
            let k = k.split_whitespace().next().unwrap_or(k);
            if let Ok(f) = v.trim().trim_end_matches(['V', 'A']).parse::<f64>() {
                out.insert(k.to_string(), f);
            }
        }
    }
    if let Ok(t) = std::process::Command::new("vcgencmd").arg("get_throttled").output() {
        if t.status.success() {
            let text = String::from_utf8_lossy(&t.stdout);
            if let Some(hex) = text.trim().split_once('=').map(|(_, v)| v) {
                if let Ok(u) = u64::from_str_radix(hex.trim(), 16) {
                    out.insert("throttled".into(), u as f64);
                }
            }
        }
    }
    out
}

/// The persisted settings snapshot: the keys that must survive a reboot,
/// read off current state. A command lands, the flusher writes this —
/// idempotent upserts of nine keys cost nothing, and a queue that can
/// forget a setting is worse than a flush that repeats one.
fn settings_snapshot(cx: &Cortex) -> Vec<(String, Value)> {
    let st = &cx.st;
    vec![
        ("mood".into(), Value::String(st.mood.clone())),
        ("ring_state".into(), Value::String(st.ring_state.clone())),
        ("oled_mode".into(), Value::String(st.oled_mode.clone())),
        ("oled_text".into(), st.oled_text.clone().map(Value::String).unwrap_or(Value::Null)),
        ("oled_dim".into(), Value::Bool(st.oled_dim)),
        ("oled_flip".into(), Value::Bool(st.oled_flip)),
        ("ripperdoc".into(), Value::Bool(st.ripperdoc)),
        ("page".into(), Value::String(st.page.clone())),
        ("snr_on".into(), Value::Bool(st.snr_on)),
    ]
}

/// Boot RAM from the store's settings. A missing key keeps its default; a
/// key the shape rejects is skipped loudly — a half-adopted boot is the
/// worst kind of lie.
fn adopt_settings(cx: &mut Cortex, rows: Vec<(String, Value)>) {
    use Value::{Bool, Null, String as VStr};
    let st = &mut cx.st;
    for (k, v) in rows {
        match (k.as_str(), &v) {
            (_, Null) => {}
            ("mood", VStr(s)) => st.mood = s.clone(),
            ("ring_state", VStr(s)) => st.ring_state = s.clone(),
            ("oled_mode", VStr(s)) => st.oled_mode = s.clone(),
            ("oled_text", VStr(s)) => st.oled_text = Some(s.clone()),
            ("oled_dim", Bool(b)) => st.oled_dim = *b,
            ("oled_flip", Bool(b)) => st.oled_flip = *b,
            ("ripperdoc", Bool(b)) => st.ripperdoc = *b,
            ("page", VStr(s)) => st.page = s.clone(),
            ("snr_on", Bool(b)) => st.snr_on = *b,
            (k, v) => eprintln!("loa-cortex: stored setting {k:?}={v} is not a shape I speak; kept default"),
        }
    }
}

/// The relay's fork in the road: does this inbound topic ride back out as
/// bytes, or come through the contract into state? ROUTES is the only
/// table — an unmapped, unreadable topic is a loud drop, never an
/// invention.
fn is_frame(topic: &[u8]) -> bool {
    loa_wire::ROUTES.iter().any(|(from, _)| from.as_bytes() == topic)
}

/// Publish a state-body on its consumer topic through the contract
/// (encode checks topic-vs-payload at the source — a cortex that builds a
/// wrong pair dies here, not on the glass).
fn publish(pub_sock: &Publisher, topic: &str, body: pb::envelope::Body) {
    if let Err(e) = pub_sock.publish(topic, &envelope(body)) {
        eprintln!("loa-cortex: publish {topic}: {e}");
    }
}

/// The news response: what a state move puts back on the wire. `state`
/// down to the devices, `ripperdoc` and `ring` out to consumers with the
/// held frames inside, and `fault` recomputed (the injected DB-DOWN row
/// depends on the flusher's state, so it is rebuilt on every move).
fn republish(cx: &Cortex, pub_sock: &Publisher, db_down: bool) {
    publish(pub_sock, "state", build::build_state(&cx.st));
    publish(pub_sock, "ripperdoc", build::build_ripperdoc(&cx.st, cx.last_face.as_deref()));
    publish(pub_sock, "ring", build::build_ring(cx.last_ring.as_deref()));
    publish(pub_sock, "fault", build::build_fault(cx, db_down));
}

/// Handle one message off the PULL leg. Frames: swap the topic frame and
/// relay — no decode, no parse, no opinion (the device knows its glass).
/// The held-frame cache updates from the envelope's own payload, because
/// the next consumer publish must carry the newest picture.
fn on_inbox(
    parts: [Vec<u8>; 2],
    pub_sock: &Publisher,
    cx: &Mutex<Cortex>,
    db_down: &AtomicBool,
    writes: &tokio::sync::mpsc::UnboundedSender<Write>,
) {
    let topic = parts[0].clone();
    if is_frame(&topic) {
        match route_parts(&topic, &parts[1]) {
            Ok(relay) => {
                if let Err(e) = pub_sock.relay(relay) {
                    eprintln!("loa-cortex: relay: {e}");
                }
            }
            Err(e) => eprintln!("loa-cortex: bad inbound frame: {e}"),
        }
        return;
    }
    let msg = match decode(parts) {
        Ok(m) => m,
        Err(e) => {
            eprintln!("loa-cortex: inbound refused: {e}");
            return;
        }
    };
    let (topic, body) = {
        let mut guard = cx.lock().unwrap_or_else(|p| p.into_inner());
        guard.merge(&msg);
        // pressure that just arrived is also a durable sample: the trend
        // window forgets after 3 h, the store never does.
        if let Some(pb::envelope::Body::Weather(w)) = msg.env.body.as_ref() {
            if let (Some(ts), Some(hpa)) = (w.ts, w.pressure_hpa) {
                let _ = writes.send(Write::Baro { ts, hpa });
            }
        }
        // which leg it came in on decides which consumer topic rebuilds;
        // anything unmapped was merged for the record and nothing more.
        let down = db_down.load(Ordering::Relaxed);
        match msg.env.body.as_ref() {
            Some(pb::envelope::Body::Pir(_)) => ("pir", build::build_pir(&guard.st)),
            Some(pb::envelope::Body::Sonar(_)) => ("sonar", build::build_sonar(&guard.st)),
            Some(pb::envelope::Body::Weather(_)) => ("weather", build::build_weather(&guard)),
            Some(pb::envelope::Body::Fault(_)) => ("fault", build::build_fault(&guard, down)),
            _ => return,
        }
    };


    publish(pub_sock, topic, body);
}

#[tokio::main]
async fn main() {
    let ctx = zmq::Context::new();
    let feed_bind = std::env::var("LOA_FEED_BIND")
        .unwrap_or_else(|_| format!("tcp://0.0.0.0:{FEED_PORT}"));
    let inbound_bind = std::env::var("LOA_INBOUND_BIND")
        .unwrap_or_else(|_| format!("tcp://127.0.0.1:{INBOUND_PORT}"));
    let door_addr = format!(
        "{}:{}",
        std::env::var("LOA_API_BIND").unwrap_or_else(|_| "0.0.0.0".into()),
        std::env::var("LOA_API_PORT").unwrap_or_else(|_| "8765".into())
    );

    let pub_sock = match Publisher::bind(&ctx, &feed_bind) {
        Ok(p) => Arc::new(p),
        Err(e) => {
            eprintln!("loa-cortex: no feed on {feed_bind}: {e}");
            process::exit(1);
        }
    };
    let inbox = match aio::AInbound::pull(&ctx, &inbound_bind) {
        Ok(i) => i,
        Err(e) => {
            eprintln!("loa-cortex: no inbound on {inbound_bind}: {e}");
            process::exit(1);
        }
    };

    let cx = Arc::new(Mutex::new(Cortex::new()));
    let vault = Arc::new(Vault::default_dir());
    if let Err(e) = vault.ensure() {
        eprintln!("loa-cortex: vault: {e}"); // no vault is not no cortex
    }
    let db_down = Arc::new(AtomicBool::new(false));

    // --- boot: the store first; settings are the decisions that survive ----
    let mut store_opt: Option<Store> = std::env::var("LOA_PG_DSN")
        .ok()
        .filter(|s| !s.is_empty())
        .map(Store::new);
    if let Some(s) = store_opt.as_mut() {
        match s.load_settings() {
            Ok(rows) => adopt_settings(&mut cx.lock().unwrap(), rows),
            Err(e) => {
                eprintln!("loa-cortex: boot from store failed: {e}");
                db_down.store(true, Ordering::Relaxed);
            }
        }
    } else {
        db_down.store(true, Ordering::Relaxed);
        eprintln!("loa-cortex: no LOA_PG_DSN — RAM-only, DB DOWN is the truth");
    }

    // --- the door and the flusher: threads with channels in, news out ------
    let (news_tx, mut news_rx) = tokio::sync::mpsc::unbounded_channel::<News>();
    {
        let cx2 = Arc::clone(&cx);
        let v2 = Arc::clone(&vault);
        let tx2 = news_tx.clone();
        std::thread::spawn(move || {
            let door = Door::new(door_addr, cx2, v2, tx2);
            if let Err(e) = door.serve() {
                eprintln!("loa-cortex: door died: {e}");
                process::exit(1);
            }
        });
    }
    let (ev_tx, mut ev_rx) =
        tokio::sync::mpsc::unbounded_channel::<Write>();
    {
        // the flusher: one thread, one blocking connection, one channel.
        // Settings ride as snapshots; events ride whole. The store is
        // moved in here: whoever owns the connection owns the DSN.
        let db2 = Arc::clone(&db_down);
        let cx = Arc::clone(&cx);
        std::thread::spawn(move || {
            let Some(mut store) = store_opt else { return };
            loop {
                // block on the first event, then drain what piled up
                let mut events = Vec::new();
                match ev_rx.blocking_recv() {
                    Some(e) => events.push(e),
                    None => break,
                }
                while let Ok(e) = ev_rx.try_recv() {
                    events.push(e);
                }
                let snapshot = settings_snapshot(
                    &cx.lock().unwrap_or_else(|p| p.into_inner()),
                );
                let mut failed = false;
                for w in &events {
                    let r = match w {
                        Write::Event { kind, detail, ts } =>
                            store.add_event(*ts, kind, Some(detail.clone())),
                        Write::Baro { ts, hpa } => store.add_baro_sample(*ts, *hpa),
                    };
                    if let Err(e) = r {
                        eprintln!("loa-cortex: write lost: {e}");
                        failed = true;
                    }
                }
                if let Err(e) = store.save_settings(&snapshot) {
                    eprintln!("loa-cortex: settings not flushed: {e}");
                    failed = true;
                }
                db2.store(failed, Ordering::Relaxed);
            }
        });
    }

    // --- the init handshake: the ask, six times, then news forever ---------
    {
        let init = envelope(build::build_init());
        for i in 0..6 {
            let _ = pub_sock.publish("init", &init);
            if i < 5 {
                tokio::time::sleep(Duration::from_secs_f64(1.0)).await;
            }
        }
        // JOIN_GRACE on top of the five seconds: the first real publish
        // after the last ask, so a subscriber that joined late still lands.
        tokio::time::sleep(Duration::from_secs_f64(JOIN_GRACE_S)).await;
        republish(
            &cx.lock().unwrap_or_else(|p| p.into_inner()),
            &pub_sock,
            db_down.load(Ordering::Relaxed),
        );
    }

    // --- the loop: two FDs, one task, zero clocks --------------------------
    let (power_tx, mut power_rx) = tokio::sync::mpsc::unbounded_channel::<HashMap<String, f64>>();
    {
        let tx = power_tx;
        std::thread::spawn(move || {
            let mut last: HashMap<String, f64> = HashMap::new();
            loop {
                std::thread::sleep(Duration::from_secs(1));
                let now = read_rails();
                if now != last && !now.is_empty() {
                    last = now.clone();
                    if tx.send(now).is_err() {
                        break;
                    }
                }
            }
        });
    }

    let pub_sock_ref = Arc::clone(&pub_sock);
    drop(pub_sock); // the task owns it from here; one holder
    loop {
        tokio::select! {
            r = inbox.readable() => {
                if r.is_err() { continue; }
                while let Ok(Some(parts)) = inbox.recv_nowait() {
                    on_inbox(parts, &pub_sock_ref, &cx, &db_down, &ev_tx);
                }
            }
            Some(news) = news_rx.recv() => {
                match news {
                    News::Event { kind, detail } => {
                        let ts = now_s();
                        let _ = ev_tx.send(Write::Event { kind: kind.clone(), detail: detail.clone(), ts });
                        publish(&pub_sock_ref, "event",
                            build::build_event(&kind, &detail, ts));
                    }
                    News::StateChanged => {
                        let guard = cx.lock().unwrap_or_else(|p| p.into_inner());
                        let db = db_down.load(Ordering::Relaxed);
                        republish(&guard, &pub_sock_ref, db);
                    }
                }
            }
            Some(rails) = power_rx.recv() => {
                let map: serde_json::Map<String, Value> = rails.into_iter()
                    .map(|(k, v)| (k, Value::from(v))).collect();
                publish(&pub_sock_ref, "power", build::build_power(&map));
            }
        }
    }
}

