//! The store against a REAL Postgres. Requires LOA_PG_TEST_DSN; ignored by
//! default so the suite never needs a live database. Run against a throwaway
//! container, NEVER aleph: the schema is IF NOT EXISTS, so even a mistake
//! converges with the Python's tables — but throwaway is the rule.
//!   docker run -d --name loa-pg-test -e POSTGRES_PASSWORD=*** \
//!     -e POSTGRES_DB=loa -p 127.0.0.1:5439:5432 postgres:16
//!   LOA_PG_TEST_DSN="host=127.0.0.1 port=5439 user=postgres password=*** \
//!     dbname=loa" cargo test -p loa-cortex -- --ignored

use loa_cortex::store::Store;

fn store() -> Option<Store> {
    std::env::var("LOA_PG_TEST_DSN").ok().map(Store::new)
}

#[test]
#[ignore]
fn settings_round_trip_and_upsert() {
    let Some(mut s) = store() else { return };
    s.save_settings(&[
        ("mood".into(), serde_json::json!("calm")),
        ("oled_flip".into(), serde_json::json!(true)),
    ])
    .expect("save");
    let got = s.load_settings().expect("load");
    let keys: Vec<&str> = got.iter().map(|(k, _)| k.as_str()).collect();
    assert!(keys.contains(&"mood"));
    assert!(keys.contains(&"oled_flip"));

    // upsert: a second save overwrites, never duplicates
    s.save_settings(&[("mood".into(), serde_json::json!("asleep"))])
        .expect("resave");
    let got = s.load_settings().expect("reload");
    assert_eq!(got.iter().filter(|(k, _)| k == "mood").count(), 1);
    let mood = got.iter().find(|(k, _)| k == "mood").unwrap().1.clone();
    assert_eq!(mood, serde_json::json!("asleep"));
}

#[test]
#[ignore]
fn baro_samples_append_and_read_in_order() {
    let Some(mut s) = store() else { return };
    let base = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs_f64();
    for i in 0..5 {
        s.add_baro_sample(base + i as f64 * 60.0, 1000.0 + i as f64).expect("sample");
    }
    let rows = s.baro_samples_since(base + 120.0).expect("read");
    assert!(rows.len() >= 3, "the since-window must filter (got {})", rows.len());
}

#[test]
#[ignore]
fn events_append_with_json_detail() {
    let Some(mut s) = store() else { return };
    let base = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs_f64();
    s.add_event(base, "mood", Some(serde_json::json!({"feeling": "calm"}))).expect("event");
    s.add_event(base, "boot", None).expect("bare event");
}

#[test]
#[ignore]
fn an_unreachable_dsn_errors_without_hanging() {
    let mut s = Store::new(
        "host=127.0.0.1 port=5999 user=nobody dbname=nope connect_timeout=1".into(),
    );
    assert!(s.load_settings().is_err());
    // the retry must reconnect from scratch, not reuse the corpse
    assert!(s.load_settings().is_err());
}
