//! loa-feed — is the wire really there, from Rust?
//!
//! The shortest thing that can be wrong in a port: the subscriber. This
//! connects to the live feed, decodes envelopes, and prints what it got and how
//! fast — so the interop question is answered before a line of panel or TUI
//! code is written on top of it.
//!
//!     loa-feed                        (default endpoint, 3 seconds)
//!     loa-feed tcp://192.168.1.200:5556 5

use std::collections::BTreeMap;

use loa_topic::{body_name, pb, Rate, Subscriber, TOPICS};

#[tokio::main]
async fn main() {
    let mut args = std::env::args().skip(1);
    let endpoint = args
        .next()
        .unwrap_or_else(|| loa_topic::feed_endpoints().remove(0));
    let secs: f64 = args
        .next()
        .and_then(|s| s.parse().ok())
        .unwrap_or(3.0);

    println!("loa-feed: subscribing to {endpoint} for {secs}s");
    let mut sub = match Subscriber::connect(&[endpoint.clone()], &TOPICS).await {
        Ok(s) => s,
        Err(e) => {
            eprintln!("connect failed: {e}");
            std::process::exit(1);
        }
    };

    // A SUB has to establish before a PUB will send it anything: a message
    // published before the subscription lands is dropped, silently, and looks
    // like a mute body.
    tokio::time::sleep(std::time::Duration::from_millis(300)).await;

    let mut counts: BTreeMap<String, u64> = BTreeMap::new();
    let mut newest: BTreeMap<String, String> = BTreeMap::new();
    let mut face_len = 0usize;
    let mut rate = Rate::new();
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs_f64(secs);

    while std::time::Instant::now() < deadline {
        match sub.recv_timeout(50).await {
            Ok(Some(env)) => {
                rate.tick();
                *counts.entry(env.topic.clone()).or_insert(0) += 1;
                let summary = describe(&env.topic, &env.env);
                if env.topic == "ripperdoc" {
                    if let Some(pb::envelope::Body::Ripperdoc(r)) = &env.env.body {
                        face_len = r.face.as_ref().map(|b| b.len()).unwrap_or(0);
                    }
                }
                newest.insert(env.topic.clone(), summary);
            }
            Ok(None) => {}
            Err(e) => {
                eprintln!("feed error: {e}");
                std::process::exit(2);
            }
        }
    }

    println!("\n{:<10} {:>6}  newest", "topic", "msgs");
    for t in TOPICS {
        let n = counts.get(t).copied().unwrap_or(0);
        let line = newest.get(t).cloned().unwrap_or_else(|| "—".into());
        println!("{t:<10} {n:>6}  {line}");
    }
    let (n, el) = rate.reset();
    println!("\n{n} messages in {el:.2}s = {:.1} msg/s", n as f64 / el);
    println!("ripperdoc frame on the wire: {face_len} bytes ({} 128 B pages)",
             if face_len == 0 { 0 } else { face_len / 128 });
}

/// The little that proves the payload is the one the topic promised. Not a
/// renderer: this is a wire check, and the numbers here are the ones a consumer
/// would act on.
fn describe(topic: &str, env: &pb::Envelope) -> String {
    use pb::envelope::Body;
    match (&env.body, topic) {
        (Some(Body::Ripperdoc(r)), _) => format!(
            "page={:?} mood={:?} oled_mode={:?} face={}B flip={:?} ts={:?}",
            r.page,
            r.mood,
            r.oled_mode,
            r.face.as_ref().map(|b| b.len()).unwrap_or(0),
            r.oled_flip,
            r.ts
        ),
        (Some(Body::Ring(r)), _) => format!(
            "ring={}B ts={:?}",
            r.ring.as_ref().map(|b| b.len()).unwrap_or(0),
            r.ts
        ),
        (Some(Body::Pir(p)), _) => format!(
            "high={:?} count={:?} last_hold={:?}",
            p.high, p.count, p.last_hold
        ),
        (Some(Body::Sonar(s)), _) => format!("cm={:?} count={:?}", s.cm, s.count),
        (Some(Body::Baro(b)), _) => format!(
            "hpa={:?} temp={:?} trend={:?} pts={}",
            b.pressure_hpa,
            b.temp_c,
            b.trend,
            b.series.len()
        ),
        (Some(Body::Weather(w)), _) => {
            format!("temp={:?} hum={:?}", w.temp_c, w.hum_pct)
        }
        (Some(Body::Power(p)), _) => {
            let mut rails: Vec<String> =
                p.rails.iter().take(3).map(|(k, v)| format!("{k}={v:.2}")).collect();
            rails.sort();
            format!("{} rails  {}", p.rails.len(), rails.join(" "))
        }
        (Some(Body::Fault(f)), _) => format!(
            "condition={:?} rows={} ({})",
            f.condition,
            f.rows.len(),
            f.rows.iter().take(2).map(|r| r.code.clone()).collect::<Vec<_>>().join(",")
        ),
        (Some(Body::Event(e)), _) => format!("kind={:?}", e.kind),
        (Some(Body::Init(i)), _) => format!("source={:?}", i.source),
        (None, _) => format!("EMPTY oneof (decoded as {})", body_name(env)),
    }
}
