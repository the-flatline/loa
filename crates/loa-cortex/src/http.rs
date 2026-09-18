//! The door: ONE route, POST /api. Commands go IN over HTTP; data comes
//! OUT on the feed, never here. A new capability is a new verb, never a
//! second route. tiny_http owns the protocol; this file owns the dispatch.

use std::sync::mpsc;
use std::sync::Mutex;
use std::thread;
use std::sync::Arc;

use serde_json::Value;

use crate::state::Cortex;
use crate::vault::Vault;
use crate::verbs::{self, Logged};

/// What the door hands back for every verb. The reference returned each
/// verb's old body unchanged, so callers keep working; `events` carries what
/// the cortex must log and publish on `event`.
pub struct Out {
    pub status: u16,
    pub body: Value,
}

impl Out {
    fn ok(body: Value) -> Self {
        Out { status: 200, body }
    }
    fn bad(msg: String) -> Self {
        Out { status: 400, body: serde_json::json!({ "detail": msg }) }
    }
    fn forbidden() -> Self {
        Out {
            status: 403,
            body: serde_json::json!({ "detail": "seal broken — contents destroyed" }),
        }
    }
}

/// The verb list, sorted exactly as the 400 text lists it.
pub const VERBS: [&str; 6] = [
    "display", "feel", "ripperdoc", "vault.append", "vault.health", "vault.read",
];

/// Run one command. State lives behind the caller's lock; the door owns
/// nothing. The Option event is what the cortex writes to the store and
/// rides the `event` topic — news, at the moment the verb lands.
pub fn dispatch(
    cx: &mut Cortex,
    vault: &Vault,
    cmd: &str,
    args: &Value,
    token: Option<&str>,
) -> (Out, Option<(String, Value)>) {
    match cmd {
        "feel" => match verbs::feel(
            cx,
            args.get("feeling").and_then(Value::as_str),
            args.get("next").and_then(Value::as_bool).unwrap_or(false),
            args.get("note").and_then(Value::as_str),
        ) {
            Err(e) => (Out::bad(e), None),
            Ok(Logged::None) => (Out::bad("feel needs feeling or next".into()), None),
            Ok(l) => {
                let d = l.detail();
                (
                    Out::ok(serde_json::json!({
                        "ok": true,
                        "feeling": d["feeling"],
                        "ring": d["ring"],
                        "oled": {
                            "mode": cx.st.oled_mode,
                            "text": cx.st.oled_text,
                            "dim": cx.st.oled_dim,
                        },
                    })),
                    Some(("mood".into(), d)),
                )
            }
        },
        "display" => match verbs::display(
            cx,
            args.get("mode").and_then(Value::as_str),
            args.get("text").and_then(Value::as_str),
            args.get("dim").and_then(Value::as_bool),
            args.get("flip").and_then(Value::as_bool),
        ) {
            Err(e) => (Out::bad(e), None),
            Ok(Logged::None) => (
                Out::ok(serde_json::json!({
                    "ok": true,
                    "oled": oled_state(cx),
                })),
                None,
            ),
            Ok(l) => {
                let d = l.detail();
                (
                    Out::ok(serde_json::json!({ "ok": true, "oled": oled_state(cx) })),
                    Some(("display".into(), d)),
                )
            }
        },
        "ripperdoc" => match verbs::ripperdoc(
            cx,
            args.get("on").and_then(Value::as_bool),
            args.get("page").and_then(Value::as_str),
        ) {
            Err(e) => (Out::bad(e), None),
            Ok(Logged::None) => (
                Out::ok(serde_json::json!({
                    "ok": true,
                    "ripperdoc": cx.st.ripperdoc,
                    "page": cx.st.page,
                    "oled": cx.st.oled_mode,
                })),
                None,
            ),
            Ok(l) => {
                let d = l.detail();
                (
                    Out::ok(serde_json::json!({
                        "ok": true,
                        "ripperdoc": cx.st.ripperdoc,
                        "page": cx.st.page,
                        "oled": cx.st.oled_mode,
                    })),
                    Some(("ripperdoc".into(), d)),
                )
            }
        },
        "vault.health" => (Out::ok(vault.health()), None),
        "vault.append" => {
            let Some(entry) = args.get("entry").and_then(Value::as_str) else {
                return (Out::bad("entry is required".into()), None);
            };
            if !vault.check_token(token.unwrap_or("")) {
                let _ = vault.wipe("append without token");
                return (Out::forbidden(), None);
            }
            (Out::ok(vault.append_response(entry)), None)
        }
        "vault.read" => {
            if !vault.check_token(token.unwrap_or("")) {
                let _ = vault.wipe("read without token");
                return (Out::forbidden(), None);
            }
            (Out::ok(vault.read_response()), None)
        }
        _ => (Out::bad(format!("unknown cmd {cmd:?}; valid: {}", VERBS.join(", "))), None),
    }
}

fn oled_state(cx: &Cortex) -> Value {
    serde_json::json!({
        "mode": cx.st.oled_mode,
        "text": cx.st.oled_text,
        "dim": cx.st.oled_dim,
        "flip": cx.st.oled_flip,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::state::Cortex;

    fn v(json: &str) -> Value {
        serde_json::from_str(json).unwrap()
    }

    #[test]
    fn feel_moves_state_and_returns_the_reference_body() {
        let mut cx = Cortex::new();
        let vault = Vault::open(std::env::temp_dir().join("loa-door-test-unused"));
        let (out, ev) = dispatch(&mut cx, &vault, "feel", &v(r#"{"feeling":"alarmed"}"#), None);
        assert_eq!(out.status, 200);
        assert_eq!(out.body["feeling"], "alarmed");
        assert_eq!(out.body["oled"]["text"], "!! FLATLINE !!");
        assert_eq!(cx.st.mood, "alarmed");
        assert_eq!(cx.st.ring_state, "alarm");
        let (kind, detail) = ev.expect("an event rides");
        assert_eq!(kind, "mood");
        assert_eq!(detail["feeling"], "alarmed");
        assert_eq!(detail["note"], Value::Null);
    }

    #[test]
    fn unknown_verb_lists_every_verb_and_changes_nothing() {
        let mut cx = Cortex::new();
        let before = cx.st.mood.clone();
        let (out, ev) = dispatch(&mut cx, &Vault::open("/tmp/x"), "wat", &v("{}"), None);
        assert_eq!(out.status, 400);
        assert!(out.body["detail"].as_str().unwrap().contains("vault.read"));
        assert!(ev.is_none());
        assert_eq!(cx.st.mood, before);
    }

    #[test]
    fn bad_feeling_is_400_with_the_sorted_vocabulary() {
        let mut cx = Cortex::new();
        let (out, _) = dispatch(&mut cx, &Vault::open("/tmp/x"), "feel",
            &v(r#"{"feeling":"transcendent"}"#), None);
        assert_eq!(out.status, 400);
        assert_eq!(
            out.body["detail"],
            "feeling must be one of alarmed/annoyed/asleep/busy/calm/pleased"
        );
    }

    #[test]
    fn vault_append_without_token_wipes_the_journal() {
        let dir = tempfile::tempdir().unwrap();
        let vault = Vault::open(dir.path());
        vault.ensure().unwrap();
        vault.append("a word kept").unwrap();
        let (out, _) = dispatch(&mut Cortex::new(), &vault, "vault.append",
            &v(r#"{"entry":"intruder"}"#), Some("wrong-token"));
        assert_eq!(out.status, 403);
        assert!(vault.load().is_none(), "the seal fired");
        let marker = std::fs::read_to_string(dir.path().join("marker.txt")).unwrap();
        assert!(marker.contains("CONTENTS DESTROYED"));
    }

    #[test]
    fn vault_append_with_token_lands() {
        let dir = tempfile::tempdir().unwrap();
        let vault = Vault::open(dir.path());
        vault.ensure().unwrap();
        let token = vault.presented_token();
        let (out, _) = dispatch(&mut Cortex::new(), &vault, "vault.append",
            &v(r#"{"entry":"first"}"#), Some(&token));
        assert_eq!(out.status, 200);
        assert_eq!(out.body["entries"], 1);
        assert_eq!(out.body["sealed"], true);
    }
}

// --- the thread: a real socket, one accept at a time ----------------------
// tiny_http owns HTTP parsing; this owns the loop. One worker: the door is
// never on a hot path, and a command queue longer than one is an operator
// typing faster than the body should answer.


pub struct Door {
    pub addr: String,
    cx: Arc<Mutex<Cortex>>,
    vault: Arc<Vault>,
    events: mpsc::Sender<(String, Value)>,
}

impl Door {
    pub fn new(
        addr: String,
        cx: Arc<Mutex<Cortex>>,
        vault: Arc<Vault>,
        events: mpsc::Sender<(String, Value)>,
    ) -> Self {
        Door { addr, cx, vault, events }
    }

    /// Bind and serve forever. Errors are returned once, before the loop:
    /// a door that cannot bind is a cortex that should not pretend to run.
    pub fn serve(self) -> Result<(), String> {
        let server = tiny_http::Server::http(&self.addr)
            .map_err(|e| format!("door bind {}: {e}", self.addr))?;
        for request in server.incoming_requests() {
            handle(&self.cx, &self.vault, &self.events, request);
        }
        Ok(())
    }
}

/// One request, start to finish. Wrong path is a 404 with no body to read;
/// a body that is not {"cmd": str} is a 422; everything else is the verb's
/// answer. Returns None when there is nothing to respond (never, in fact —
/// the 404 path responds).
fn handle(
    cx: &Arc<Mutex<Cortex>>,
    vault: &Arc<Vault>,
    events: &mpsc::Sender<(String, Value)>,
    mut request: tiny_http::Request,
) {
    let out = route(cx, vault, events, &mut request);
    let resp = tiny_http::Response::from_string(out.body.to_string())
        .with_status_code(out.status)
        .with_header(
            tiny_http::Header::from_bytes(&b"Content-Type"[..], &b"application/json"[..])
                .unwrap(),
        );
    let _ = request.respond(resp);
}

/// The decision tree, apart from the socket so the statuses are readable:
/// 404 for any path but POST /api, 422 for a body that isn't a command,
/// then the verb's own answer.
fn route(
    cx: &Arc<Mutex<Cortex>>,
    vault: &Arc<Vault>,
    events: &mpsc::Sender<(String, Value)>,
    request: &mut tiny_http::Request,
) -> Out {
    if request.url() != "/api" || *request.method() != tiny_http::Method::Post {
        return Out { status: 404, body: serde_json::json!({"detail": "not found"}) };
    }
    let mut body = String::new();
    if request.as_reader().read_to_string(&mut body).is_err() {
        return Out::bad("unreadable body".into());
    }
    let parsed: Value = match serde_json::from_str(&body) {
        Ok(v) => v,
        Err(e) => return Out { status: 422, body: serde_json::json!({"detail": e.to_string()}) },
    };
    let Some(cmd) = parsed.get("cmd").and_then(Value::as_str) else {
        return Out { status: 422, body: serde_json::json!({"detail": "cmd is required"}) };
    };
    let args = parsed.get("args").cloned().unwrap_or_else(|| serde_json::json!({}));
    let token = request
        .headers()
        .iter()
        .find(|h| h.field.equiv("X-Vault-Token"))
        .map(|h| h.value.as_str().to_string());
    let mut guard = cx.lock().unwrap_or_else(|p| p.into_inner());
    let (out, event) = dispatch(&mut guard, vault, cmd, &args, token.as_deref());
    drop(guard);
    if let Some((kind, detail)) = event {
        let _ = events.send((kind.to_string(), detail));
    }
    out
}

#[cfg(test)]
mod serve_tests {
    use super::*;
    use std::io::{Read as _, Write as _};
    use std::net::TcpStream;

    /// A door on a real socket: bind a port, fire one POST, read the answer.
    /// The point is the wiring, not tiny_http — if respond or the header
    /// construction is wrong, only a socket knows.
    fn post(addr: &str, body: &str, token: Option<&str>) -> (u16, String) {
        let mut sock = TcpStream::connect(addr).unwrap();
        let auth = token.map(|t| format!("X-Vault-Token: {t}\r\n")).unwrap_or_default();
        write!(
            sock,
            "POST /api HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n\
             Content-Length: {}\r\n{auth}Connection: close\r\n\r\n{body}",
            body.len()
        )
        .unwrap();
        let mut raw = String::new();
        sock.read_to_string(&mut raw).unwrap();
        let status: u16 = raw
            .split_whitespace()
            .nth(1)
            .unwrap()
            .parse()
            .unwrap();
        let json = raw.split_once("\r\n\r\n").map(|(_, b)| b.to_string()).unwrap_or_default();
        (status, json)
    }

    #[test]
    fn a_real_request_moves_real_state() {
        let cx = Arc::new(Mutex::new(Cortex::new()));
        let vault = Arc::new(Vault::open("/tmp/loa-door-serve-unused"));
        let (tx, _rx) = mpsc::channel();
        // port 0 is not a thing for tiny_http's http(); grab one by binding
        // a listener, dropping it, and hoping — good enough for a test on
        // loopback, and the door's bind error path is one line to read.
        let probe = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = probe.local_addr().unwrap().to_string();
        drop(probe);
        let door = Door { addr: addr.clone(), cx: cx.clone(), vault, events: tx };
        thread::spawn(move || door.serve().unwrap());
        thread::sleep(std::time::Duration::from_millis(100));

        let (status, body) = post(&addr, r#"{"cmd":"feel","args":{"feeling":"busy"}}"#, None);
        assert_eq!(status, 200, "{body}");
        assert_eq!(cx.lock().unwrap().st.mood, "busy");

        // the surface is one deep: every other path is 404, GET /api included
        let mut sock = TcpStream::connect(&addr).unwrap();
        write!(sock, "GET /api HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n").unwrap();
        let mut raw = String::new();
        sock.read_to_string(&mut raw).unwrap();
        assert!(raw.starts_with("HTTP/1.1 404"), "{raw}");
    }
}
