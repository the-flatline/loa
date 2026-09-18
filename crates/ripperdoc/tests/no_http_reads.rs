//! The console reads the FEED and never HTTP-GETs.
//!
//! This was a Python test (`tests/test_ripperdoc_remote.py`) that parsed the
//! module's AST, because the console twice regressed into polling `/state`
//! while its comments said "feed" — a console that looks like it is on pub/sub
//! and is actually polling is worse than one that plainly polls, since nobody
//! goes looking for the bug.
//!
//! The console is Rust again, so the guarantee moves with it. HTTP is for
//! VERBS: the only request this program makes is a hand-rolled POST to /api.
//! A GET anywhere in the source — and any HTTP client crate that would hide
//! one — is the same regression in a new shape.
use std::fs;
use std::path::Path;

fn source(name: &str) -> String {
    let p = Path::new(env!("CARGO_MANIFEST_DIR")).join("src").join(name);
    fs::read_to_string(&p).unwrap_or_else(|e| panic!("{}: {e}", p.display()))
}

fn code_lines(src: &str) -> impl Iterator<Item = &str> {
    // `/state` and `/live` may be DISCUSSED in a comment; what is forbidden is
    // a string or call that would actually fetch them. Coarse by design: the
    // crate keeps its comments and its code apart well enough for this.
    src.lines()
        .map(str::trim_start)
        .filter(|l| !l.starts_with("//"))
}

#[test]
fn the_console_makes_no_http_reads() {
    for file in ["main.rs", "view.rs"] {
        let src = source(file);
        for forbidden in [
            "GET ", // the request line of a read
            "ureq",
            "reqwest",
            "tiny_http",
            "hyper", // crates with a get() in their palm
            "http::get",
            "/state",
            "/live",
            "/snapshot",
        ] {
            let hits: Vec<&str> = code_lines(&src).filter(|l| l.contains(forbidden)).collect();
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
    assert!(
        src.contains("POST {path} HTTP/1.1"),
        "the console has no command path at all"
    );
    assert!(
        src.contains("/api"),
        "the command path does not name the one door, /api"
    );
    // exactly one request line in the crate: adding a second HTTP call is a
    // decision, not a side effect, and this line makes someone make it.
    let posts = code_lines(&src).filter(|l| l.contains("HTTP/1.1")).count();
    assert_eq!(posts, 1, "more than one HTTP request line in the console");
}
