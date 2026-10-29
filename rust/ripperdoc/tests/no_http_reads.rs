//! The console reads the FEED and never HTTP-GETs.
//!
//! This was a Python test (`tests/test_ripperdoc_remote.py`) that parsed the
//! module's AST, because the console twice regressed into polling `/state`
//! while its comments said "feed" — a console that looks like it is on pub/sub
//! and is actually polling is worse than one that plainly polls, since nobody
//! goes looking for the bug.
//!
//! The console is Rust now, so the guarantee moves with it. HTTP is for VERBS:
//! the only request this program makes is a POST to /api. A GET anywhere in the
//! source is the same regression in a new language.
use std::fs;
use std::path::Path;

fn source(name: &str) -> String {
    let p = Path::new(env!("CARGO_MANIFEST_DIR")).join("src").join(name);
    fs::read_to_string(&p).unwrap_or_else(|e| panic!("{}: {e}", p.display()))
}

#[test]
fn the_console_makes_no_http_reads() {
    for file in ["main.rs", "view.rs"] {
        let src = source(file);
        for forbidden in ["ureq::get", "reqwest::get", "http::get", "/state", "/live"] {
            // `/state` and `/live` may be DISCUSSED in a comment; what is
            // forbidden is a string or call that would actually fetch them.
            let hits: Vec<&str> = src
                .lines()
                .filter(|l| {
                    let l = l.trim_start();
                    !l.starts_with("//") && l.contains(forbidden)
                })
                .collect();
            assert!(
                hits.is_empty(),
                "{file} still reaches for {forbidden}: {hits:?} — the data path is the topic, \
                 commands go over POST /api, reads never do"
            );
        }
    }
}

#[test]
fn the_only_http_call_is_a_post_to_the_door() {
    let src = source("main.rs");
    let posts = src.matches("ureq::post").count();
    assert!(posts >= 1, "the console has no command path at all");
    assert!(
        src.contains("/api"),
        "the command path does not name the one door, /api"
    );
}
