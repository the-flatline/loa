//! loa-sense — the senses' plumbing, in Rust.
//!
//! What a sense daemon needs to BE one: publish a reading, publish an event, and
//! answer the cortex's init handshake with a full payload. Nothing here knows
//! what a PIR is — the drivers live with their daemons, same as in Python.
//!
//! ONE THING IS DIFFERENT FROM PYTHON, ON PURPOSE. There is no `use_topic("pir")`
//! to call at startup: the topic is derived from the payload on every send
//! (`body_topic_of`). A daemon cannot publish a PIR reading on the ring topic by
//! mistyping a string, because it never names a topic at all.
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use loa_topic::{body_topic_of, pb, SCHEMA_VERSION};
use prost::Message;

/// Where readings go: the cortex PULLs here. Loopback, because the senses are
/// ON the body and there is nothing on the far side of the network this side of
/// the topic.
pub const DEFAULT_INBOUND: &str = "tcp://127.0.0.1:5555";

/// Where the init ask comes FROM (the feed the cortex publishes on).
pub const DEFAULT_FEED: &str = "tcp://127.0.0.1:5556";

pub fn now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// A sense daemon's uplink.
pub struct Sense {
    /// Declared BEFORE the context so it is dropped first: libzmq wants sockets
    /// closed before the context terminates, and field drop order is
    /// declaration order. Getting this backwards hangs at exit, which reads as
    /// a daemon that will not shut down.
    push: Mutex<zmq::Socket>,
    _ctx: zmq::Context,
    pub inbound: String,
}

impl Sense {
    pub fn open(inbound: &str) -> Result<Arc<Self>, String> {
        let ctx = zmq::Context::new();
        let push = ctx.socket(zmq::PUSH).map_err(|e| e.to_string())?;
        // A reading is not a frame: it must not be dropped on the way out. The
        // topics drop old frames happily (the newest is the truth); a sensor
        // reading that never arrived is a hole in the record.
        push.set_linger(1000).map_err(|e| e.to_string())?;
        push.connect(inbound).map_err(|e| e.to_string())?;
        Ok(Arc::new(Self {
            push: Mutex::new(push),
            _ctx: ctx,
            inbound: inbound.to_string(),
        }))
    }

    /// Publish one reading. The topic comes from the payload.
    pub fn send(&self, body: pb::envelope::Body) -> Result<(), String> {
        let topic = body_topic_of(&body);
        let env = pb::Envelope {
            schema_version: SCHEMA_VERSION,
            body: Some(body),
        };
        let sock = self.push.lock().map_err(|_| "push socket poisoned".to_string())?;
        sock.send_multipart([topic.as_bytes().to_vec(), env.encode_to_vec()], 0)
            .map_err(|e| e.to_string())
    }

    /// A record. Always the `event` topic — an event is not a state.
    pub fn event(&self, kind: &str, detail: &[(&str, &str)]) -> Result<(), String> {
        let ev = pb::Event {
            ts: now(),
            kind: kind.to_string(),
            detail: detail
                .iter()
                .map(|(k, v)| (k.to_string(), v.to_string()))
                .collect(),
        };
        self.send(pb::envelope::Body::Event(ev))
    }

    /// Answer the init ask with the FULL current payload.
    ///
    /// The daemons PUSH up and the cortex PULLs, so the cortex cannot ask
    /// anything down that leg. The ask travels the other way, on the `init`
    /// topic, which is why a sense holds a socket in both directions.
    ///
    /// `full` is the daemon's own memory of what it measured. The Python side
    /// keeps this as an accumulated dict; here the daemon hands over a closure
    /// that builds its current message, which is the same thing with the types
    /// checked.
    pub fn answer_init<F>(self: &Arc<Self>, feed: &str, full: F) -> Result<(), String>
    where
        F: Fn() -> pb::envelope::Body + Send + 'static,
    {
        let ctx = zmq::Context::new();
        let sub = ctx.socket(zmq::SUB).map_err(|e| e.to_string())?;
        sub.set_linger(0).map_err(|e| e.to_string())?;
        sub.set_subscribe(b"init").map_err(|e| e.to_string())?;
        sub.connect(feed).map_err(|e| e.to_string())?;
        let me = Arc::clone(self);
        thread::spawn(move || {
            loop {
                match sub.poll(zmq::POLLIN, 500) {
                    Ok(0) => continue,
                    Ok(_) => {}
                    // The context is going away under the socket: a process
                    // being torn down must not log a traceback as it exits.
                    Err(_) => return,
                }
                if sub.recv_multipart(0).is_err() {
                    return;
                }
                // The FULL payload and nothing else. The Python answer sends no
                // event either: an extra event kind here would be a record the
                // cortex has never seen, and the ring plays events.
                let _ = me.send(full());
            }
            // `sub` and `ctx` live until here; both are dropped with the thread.
        });
        Ok(())
    }
}

/// A setting, in the order the Python config reader uses: environment first,
/// then loa.conf, then the built-in default.
///
/// loa.conf is read as JSON (`{"sense": {"gpio": 17}}`), which is what the body
/// writes. A missing or unreadable file is not an error — the bench runs these
/// drivers with no config at all, and a default is what the Python side does.
pub fn setting(env_key: &str, group: &str, key: &str, default: f64) -> f64 {
    if let Ok(v) = std::env::var(env_key) {
        if let Ok(n) = v.parse::<f64>() {
            return n;
        }
    }
    let path = std::env::var("HOME").unwrap_or_else(|_| "/home/flatline".into()) + "/loa.conf";
    let text = match std::fs::read_to_string(&path) {
        Ok(t) => t,
        Err(_) => return default,
    };
    let json: serde_json::Value = match serde_json::from_str(&text) {
        Ok(j) => j,
        Err(_) => return default,
    };
    json.get(group)
        .and_then(|g| g.get(key))
        .and_then(|v| v.as_f64())
        .unwrap_or(default)
}

/// A string setting — env, then loa.conf, then the default. `serde_json` gives
/// back a bool or a number as well as a string, so all three are accepted and
/// rendered; a missing key is the default and never an error.
pub fn setting_str(env_key: &str, group: &str, key: &str, default: &str) -> String {
    if let Ok(v) = std::env::var(env_key) {
        if !v.is_empty() {
            return v;
        }
    }
    let path = std::env::var("HOME").unwrap_or_else(|_| "/home/flatline".into()) + "/loa.conf";
    let json: serde_json::Value = std::fs::read_to_string(&path)
        .ok()
        .and_then(|t| serde_json::from_str(&t).ok())
        .unwrap_or(serde_json::Value::Null);
    match json.get(group).and_then(|g| g.get(key)) {
        Some(serde_json::Value::String(s)) => s.clone(),
        Some(serde_json::Value::Bool(b)) => b.to_string(),
        Some(serde_json::Value::Number(n)) => n.to_string(),
        _ => default.to_string(),
    }
}

/// A yes/no setting, with the SAME truthiness the Python reader uses: the
/// offline spellings are the point, because loa.conf carries `snr_enabled:
/// false` for a module that is deliberately unplugged, and a reader that only
/// understood `false` would turn the sonar back on.
pub fn setting_bool(env_key: &str, group: &str, key: &str, default: bool) -> bool {
    let raw = setting_str(env_key, group, key, if default { "true" } else { "false" });
    !matches!(raw.trim().to_ascii_lowercase().as_str(),
              "0" | "false" | "no" | "off" | "")
}

/// A slow loop that keeps running: a daemon whose poll returns instantly would
/// burn a core doing nothing.
pub fn pause(secs: f64) {
    thread::sleep(Duration::from_secs_f64(secs.max(0.0)));
}
