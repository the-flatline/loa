//! The vault: a journal the body keeps, and a seal that destroys it if
//! anyone opens the door without the token.
//!
//!   - token.bin: 32 random bytes. The PRESENTED token is
//!     hex(HMAC-SHA256(key = those bytes, msg = b"fragment")). The literal
//!     is seal material, not a name: rename it and every held token
//!     mismatches, and a mismatch WIPES.
//!   - key.bin: a Fernet key (base64url, 32 raw bytes: 16 signing + 16
//!     AES). vault.enc is ONE Fernet token over the JSON list of entries.
//!     The format is implemented here from its public spec (version 0x80,
//!     BE64 timestamp, IV, AES-128-CBC/PKCS7, HMAC-SHA256 truncated to 16
//!     bytes, base64url) because the published crate wants OpenSSL and
//!     this binary must stay a single static file.
//!   - append/read with a wrong token (or none) => WIPE: keys unlinked,
//!     journal overwritten with NULs its own size, marker written. The
//!     theft consumes the prize. No other path wipes.
//!   - status.json is public (0644): sealed, counts, hash prefix, marker —
//!     the FRAG page's numbers, never the words.
//!   - access.log is JSONL and self-auditing: READ is logged BEFORE the
//!     words go out the door.
//!
//! Every write: tmp file, chmod, rename. A crash leaves the old bytes.

use std::fs;
use std::os::unix::fs::OpenOptionsExt;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use base64::Engine as _;
use hmac::{Hmac, Mac};
use sha2::Sha256;
use zeroize::Zeroizing;

pub const DEFAULT_DIR: &str = "/var/lib/vault";

/// The domain separator inside the seal. SEAL MATERIAL, not a name: the
/// directory was once called `fragment` and the verb once `frag` — both
/// renamed, the literal stayed, because changing it changes every derived
/// token and a mismatch WIPES the journal. Do not "fix" it.
const SEAL_DOMAIN: &[u8] = b"fragment";

pub struct Vault {
    dir: PathBuf,
}

fn now_f() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

impl Vault {
    pub fn open(dir: impl AsRef<Path>) -> Self {
        Self { dir: dir.as_ref().to_path_buf() }
    }

    pub fn default_dir() -> Self {
        Self::open(DEFAULT_DIR)
    }

    fn p(&self, name: &str) -> PathBuf {
        self.dir.join(name)
    }

    /// Create key material and an empty journal if absent. Idempotent.
    pub fn ensure(&self) -> Result<(), String> {
        fs::create_dir_all(&self.dir).map_err(|e| format!("vault dir: {e}"))?;
        if !self.p("key.bin").exists() {
            let raw = rand32()?;
            let key = base64::Engine::encode(
                &base64::engine::general_purpose::URL_SAFE_NO_PAD,
                &raw[..],
            );
            write_mode(&self.p("key.bin"), key.as_bytes(), 0o600)?;
        }
        if !self.p("token.bin").exists() {
            { let r = rand32()?; write_mode(&self.p("token.bin"), &r[..], 0o600)?; }
        }
        if !self.p("vault.enc").exists() {
            let t = self.fernet_encrypt(b"[]")?;
            write_mode(&self.p("vault.enc"), t.as_bytes(), 0o600)?;
        }
        self.write_status()?;
        Ok(())
    }

    /// The seal: hex(HMAC-SHA256(token.bin, b"fragment")). "" if the token
    /// is missing — a body with no seal can present nothing, and `false`
    /// from check_token means the door never opens quietly.
    pub fn presented_token(&self) -> String {
        let raw = match fs::read(self.p("token.bin")) {
            Ok(b) if b.len() == 32 => b,
            _ => return String::new(),
        };
        let mut mac =
            Hmac::<Sha256>::new_from_slice(&raw).expect("hmac accepts any key length");
        mac.update(SEAL_DOMAIN);
        hex::encode(mac.finalize().into_bytes())
    }

    pub fn check_token(&self, presented: &str) -> bool {
        if presented.is_empty() {
            return false;
        }
        // constant-time: this is a secret comparison or it is nothing.
        constant_time_eq(presented.as_bytes(), self.presented_token().as_bytes())
    }

    /// The door's answer for an append: the reference's dict, fields and all.
    pub fn append_response(&self, entry: &str) -> serde_json::Value {
        match self.append(entry) {
            Ok(n) => serde_json::json!({"ok": true, "sealed": true, "entries": n}),
            Err(_) => serde_json::json!({"ok": false, "reason": "vault gone", "sealed": false}),
        }
    }

    /// READ logs itself before any words move, so a reader who deletes the log
    /// line with the read still leaves the line. 30 verbatim lines, oldest
    /// first, exactly as written.
    pub fn read_response(&self) -> serde_json::Value {
        match self.read() {
            Ok((entries, access_log)) =>
                serde_json::json!({"ok": true, "access_log": access_log, "entries": entries}),
            Err(_) => serde_json::json!({"ok": false, "reason": "vault gone", "sealed": false}),
        }
    }
}

// --- internals: entropy, atomic writes, comparison ------------------------

/// 32 bytes from getrandom(): the kernel's pool, no device file to open,
/// no third crate. Blocking is correct here — this is key material.
fn rand32() -> Result<Zeroizing<[u8; 32]>, String> {
    let mut buf = Zeroizing::new([0u8; 32]);
    let mut n = 0;
    while n < 32 {
        let r = unsafe {
            libc::getrandom(buf.as_mut_ptr().add(n).cast(), 32 - n, 0)
        };
        if r < 0 {
            return Err(format!("getrandom: {}", std::io::Error::last_os_error()));
        }
        n += r as usize;
    }
    Ok(buf)
}

/// tmp file, mode, rename: a crash mid-write leaves the previous bytes
/// intact and a readable vault. os.replace's trick, kept.
fn write_mode(path: &Path, bytes: &[u8], mode: u32) -> Result<(), String> {
    let tmp = path.with_extension("tmp");
    let mut f = fs::OpenOptions::new()
        .write(true).create(true).truncate(true)
        .mode(mode)
        .open(&tmp)
        .map_err(|e| format!("open {}: {e}", path.display()))?;
    f.write_all(bytes).map_err(|e| format!("write {}: {e}", path.display()))?;
    f.sync_all().map_err(|e| format!("sync {}: {e}", path.display()))?;
    fs::rename(&tmp, path).map_err(|e| format!("rename {}: {e}", path.display()))
}

fn constant_time_eq(a: &[u8], b: &[u8]) -> bool {
    // length is not a secret here (both are 64-hex or "" vs 64-hex), and
    // the loop never short-circuits on content: the digest is the check.
    if a.len() != b.len() {
        return false;
    }
    let mut diff = 0u8;
    for (x, y) in a.iter().zip(b) {
        diff |= x ^ y;
    }
    diff == 0
}

// --- Fernet, from the public spec ----------------------------------------
// token = base64url( 0x80 || ts_be64 || iv(16) || AES-128-CBC(key, pkcs7)
//                    || HMAC-SHA256(sign, above) )  — the tag is the FULL 32-byte digest
// key   = base64url(32 raw): first 16 sign, last 16 encrypt. Verify the
// HMAC BEFORE touching the cipher: a tampered token fails, it never
// decrypts into garbage.

/// base64 '=' padding, off the end of a byte slice (str has trim, [u8] has
/// no such thing, and inventing one beats a lossy conversion).
fn strip_pad(b: &[u8]) -> &[u8] {
    let mut end = b.len();
    while end > 0 && b[end - 1] == b'=' {
        end -= 1;
    }
    &b[..end]
}

fn key_halves(raw_b64: &[u8]) -> Result<(Vec<u8>, Vec<u8>), String> {
    let eng = &base64::engine::general_purpose::URL_SAFE_NO_PAD;
    // The reference writes key.bin WITH base64 padding; ours without. Both
    // are the same 32 raw bytes, and a key that fails to decode reads as a
    // sealed vault — so trim the pad and accept either shape.
    let raw = eng
        .decode(strip_pad(raw_b64))
        .map_err(|e| format!("key not base64url: {e}"))?;
    if raw.len() != 32 {
        return Err(format!("fernet key is 32 raw bytes, got {}", raw.len()));
    }
    Ok((raw[..16].to_vec(), raw[16..].to_vec()))
}

impl Vault {
    pub fn fernet_encrypt(&self, plain: &[u8]) -> Result<String, String> {
    use aes::Aes128;
    use cbc::cipher::block_padding::Pkcs7;
    use cbc::cipher::{BlockEncryptMut, KeyIvInit};
    type Enc = cbc::Encryptor<Aes128>;

    let (sign, enc) = key_halves(&fs::read(self.p("key.bin")).map_err(|e| e.to_string())?)?;
    let iv: [u8; 16] = {
        let r = rand32()?;
        r[..16].try_into().expect("16 of 32")
    };
    let ts = now_f() as u64;
    // CBC through the crate's padded API: the chain (IV, then ciphertext
    // blocks) is the crate's job, and hand-rolling it is how a loop with
    // one IV becomes ECB in a costume.
    let ct = Enc::new_from_slices(&enc, &iv)
        .map_err(|e| e.to_string())?
        .encrypt_padded_vec_mut::<Pkcs7>(plain);
    let mut body = Vec::with_capacity(25 + ct.len() + 32);
    body.push(0x80u8);
    body.extend_from_slice(&ts.to_be_bytes());
    body.extend_from_slice(&iv);
    body.extend_from_slice(&ct);
    let mut mac = Hmac::<Sha256>::new_from_slice(&sign).map_err(|e| e.to_string())?;
    mac.update(&body);
    let sig = mac.finalize().into_bytes();
    body.extend_from_slice(&sig);
    Ok(base64::Engine::encode(
        &base64::engine::general_purpose::URL_SAFE_NO_PAD, &body))
}

    pub fn fernet_decrypt(&self, token: &str) -> Result<Vec<u8>, String> {
    use aes::Aes128;
    use cbc::cipher::block_padding::Pkcs7;
    use cbc::cipher::{BlockDecryptMut, KeyIvInit};
    type Dec = cbc::Decryptor<Aes128>;

    let eng = &base64::engine::general_purpose::URL_SAFE_NO_PAD;
    // The reference's tokens ride WITH base64 padding; ours are written
    // without. Accept both shapes — the journal on the body was cut with
    // the pad, and refusing it there would read a healthy vault as gone.
    let blob = eng.decode(token.trim_end_matches('=')).map_err(|e| format!("token b64: {e}"))?;
    if blob.len() < 57 || blob[0] != 0x80 {
        return Err("not a fernet v1 token".into());
    }
    let (sign, enc) = key_halves(&fs::read(self.p("key.bin")).map_err(|e| e.to_string())?)?;
    let (body, sig) = blob.split_at(blob.len() - 32);
    let mut mac = Hmac::<Sha256>::new_from_slice(&sign).map_err(|e| e.to_string())?;
    mac.update(body);
    // The FULL 32-byte digest is the tag — a round-trip against your own
    // encoder can never catch a wrong length here; only real vectors can.
    // Verify before decrypt: a forged token fails here and never reaches
    // the cipher as plausible rubbish.
    let want = mac.finalize().into_bytes();
    if !constant_time_eq(&want[..], sig) {
        return Err("hmac mismatch — token is not ours".into());
    }
    let iv: [u8; 16] = body[9..25].try_into().expect("checked len");
    let ct = &body[25..];
    Dec::new_from_slices(&enc, &iv)
        .map_err(|e| e.to_string())?
        .decrypt_padded_vec_mut::<Pkcs7>(ct)
        .map_err(|e| format!("cipher: {e}"))
}

// --- the journal ----------------------------------------------------------

/// Load the entries. None means the vault is GONE (wiped or unreadable) —
/// the door's answer for a destroyed journal and the seal's `sealed:false`.
pub fn load(&self) -> Option<Vec<serde_json::Value>> {
    let tok = fs::read_to_string(self.p("vault.enc")).ok()?;
    let plain = self.fernet_decrypt(tok.trim()).ok()?;
    serde_json::from_slice(&plain).ok()
}

pub fn append(&self, entry: &str) -> Result<usize, String> {
    let mut entries = self.load().ok_or("vault gone")?;
    entries.push(serde_json::json!({"ts": now_f(), "entry": entry}));
    let plain = serde_json::to_vec(&entries).map_err(|e| e.to_string())?;
    let tok = self.fernet_encrypt(&plain)?;
    write_mode(&self.p("vault.enc"), tok.as_bytes(), 0o600)?;
    self.log_access("APPEND", &entries.len().to_string())?;
    self.write_status()?;
    Ok(entries.len())
}

/// READ is logged BEFORE the words are returned: a reader who destroys the
/// log line with the read still leaves the read. The last 30 access lines
/// ride out verbatim — raw JSONL, the body's own handwriting.
pub fn read(&self) -> Result<(Vec<serde_json::Value>, Vec<String>), String> {
    self.log_access("READ", &self.load().map(|e| e.len()).unwrap_or(0).to_string())?;
    let entries = self.load().ok_or_else(|| "vault gone".to_string())?;
    let lines: Vec<String> = fs::read_to_string(self.p("access.log"))
        .unwrap_or_default()
        .lines()
        .rev()
        .take(30)
        .map(String::from)
        .collect::<Vec<_>>()
        .into_iter()
        .rev()
        .collect();
    self.write_status()?;
    Ok((entries, lines))
}

/// The theft consumes the prize. Unlink the keys first — an interrupt
/// between unlink and overwrite leaves the journal without a key, which is
/// the same dead as zeroed bytes and no less markerable.
pub fn wipe(&self, reason: &str) -> Result<(), String> {
    let size = fs::metadata(self.p("vault.enc")).map(|m| m.len()).unwrap_or(1);
    for k in ["key.bin", "token.bin"] {
        match fs::remove_file(self.p(k)) {
            Ok(()) => {}
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
            Err(e) => return Err(format!("unlink {k}: {e}")),
        }
    }
    write_mode(&self.p("vault.enc"), &vec![0u8; size as usize], 0o600)?;
    let stamp = time_stamp();
    write_mode(
        &self.p("marker.txt"),
        format!("SEALED {stamp} / OPENED BY {reason} / CONTENTS DESTROYED\n").as_bytes(),
        0o644,
    )?;
    self.log_access("WIPED", reason)?;
    self.write_status()?;
    Ok(())
}

// --- the log and the public status ----------------------------------------

fn log_access(&self, event: &str, detail: &str) -> Result<(), String> {
    let line = serde_json::to_string(&serde_json::json!(
        {"ts": now_f(), "event": event, "detail": detail}))
        .map_err(|e| e.to_string())?;
    let path = self.p("access.log");
    let mut f = fs::OpenOptions::new()
        .append(true).create(true).mode(0o600)
        .open(&path)
        .map_err(|e| format!("open access.log: {e}"))?;
    writeln!(f, "{line}").map_err(|e| format!("write access.log: {e}"))
}

/// status.json: what the body can show about the seal without ever showing
/// the words. 0644 on purpose — sealed is a fact, the journal is the secret.
fn write_status(&self) -> Result<(), String> {
    let enc = fs::read(self.p("vault.enc")).unwrap_or_default();
    let sealed = self.load().is_some();
    let entries = self.load().map(|e| e.len()).unwrap_or(0);
    let access_count = fs::read_to_string(self.p("access.log"))
        .map(|s| s.lines().count())
        .unwrap_or(0);
    let hash = if enc.is_empty() {
        serde_json::Value::Null
    } else {
        use sha2::{Digest, Sha256};
        let h = Sha256::digest(&enc);
        serde_json::Value::String(hex::encode(&h[..8]))
    };
    let marker = fs::read_to_string(self.p("marker.txt"))
        .map(|s| serde_json::Value::String(s.trim().to_string()))
        .unwrap_or(serde_json::Value::Null);
    let status = serde_json::json!({
        "sealed": sealed, "entries": entries, "access_count": access_count,
        "hash": hash, "marker": marker,
    });
    let pretty = serde_json::to_string_pretty(&status).map_err(|e| e.to_string())?;
    write_mode(&self.p("status.json"), format!("{pretty}\n").as_bytes(), 0o644)
}

/// status.json for the door: the reference's `health()` shape, with
/// `sealed`/`entries` folded from whether the journal decrypts.
pub fn health(&self) -> serde_json::Value {
    let sealed = self.load();
    let mut s = self.public_status();
    let count = sealed.as_ref().map(|e| e.len()).unwrap_or(0);
    s["sealed"] = serde_json::json!(sealed.is_some());
    s["entries"] = serde_json::json!(count);
    s
}

/// Public status for the feed: mtime-cached so a 120fps face page never
/// rereads files. The renderer gets a number, not a syscall.
pub fn public_status(&self) -> serde_json::Value {
    let path = self.p("status.json");
    if let Ok(meta) = fs::metadata(&path) {
        if let Ok(since) = meta.modified() {
            let age = since.elapsed().map(|d| d.as_secs()).unwrap_or(0);
            if age <= 1 {
                if let Ok(s) = fs::read_to_string(&path) {
                    if let Ok(v) = serde_json::from_str(&s) {
                        return v;
                    }
                }
            }
        }
    }
    let _ = self.write_status();
    fs::read_to_string(&path)
        .ok()
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or_else(|| serde_json::json!({"sealed": false, "error": "no vault"}))
    }
}

/// Marker timestamps carry the zone: the reference's strftime(%z) shape.
fn time_stamp() -> String {
    chrono::Local::now().format("%Y-%m-%d %H:%M:%S %z").to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The compatibility oracle: vectors cut from the reference vault's own
    /// primitives (tests/vault_vectors.json) before it was retired. The Rust
    /// vault must read what the body already holds — the journal decrypts,
    /// the seal derives the same token, forever.
    fn vectors() -> serde_json::Value {
        let raw = include_str!("../tests/vault_vectors.json");
        serde_json::from_str(raw).expect("vectors are JSON")
    }

    fn vault_with(v: &serde_json::Value) -> (tempfile::TempDir, Vault) {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("key.bin"), v["fernet_key_b64"].as_str().unwrap().as_bytes())
            .unwrap();
        std::fs::write(dir.path().join("vault.enc"), v["journal_token"].as_str().unwrap().as_bytes())
            .unwrap();
        std::fs::write(dir.path().join("token.bin"),
            hex::decode(v["token_raw_hex"].as_str().unwrap()).unwrap()).unwrap();
        let vault = Vault::open(dir.path());
        (dir, vault)
    }

    #[test]
    fn the_live_journal_decrypts_and_the_seal_derives() {
        let v = vectors();
        let (_d, vault) = vault_with(&v);
        // ensure() must not flinch at existing files
        vault.ensure().expect("ensure leaves a real vault alone");
        let entries = vault.load().expect("the python-cut journal must decrypt");
        let expected: Vec<serde_json::Value> =
            serde_json::from_value(v["entries"].clone()).unwrap();
        assert_eq!(entries, expected);
        assert_eq!(vault.presented_token(), v["presented"].as_str().unwrap());
    }

    #[test]
    fn a_forged_token_fails_the_hmac_before_the_cipher() {
        let v = vectors();
        let (_d, vault) = vault_with(&v);
        let mut blob = base64::engine::general_purpose::URL_SAFE_NO_PAD
            .decode(v["journal_token"].as_str().unwrap()).unwrap();
        let last = blob.len() - 1;
        blob[last] ^= 0x01; // flip one ciphertext bit
        let forged = base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(&blob);
        assert!(vault.fernet_decrypt(&forged).is_err(), "tampered token accepted");
    }

    #[test]
    fn own_cipher_round_trips() {
        let dir = tempfile::tempdir().unwrap();
        let vault = Vault::open(dir.path());
        vault.ensure().unwrap();
        let ct = vault.fernet_encrypt(b"hello world").unwrap();
        let back = vault.fernet_decrypt(&ct).unwrap();
        assert_eq!(back, b"hello world");
    }

    #[test]
    fn round_trip_and_wipe_arm() {
        let dir = tempfile::tempdir().unwrap();
        let vault = Vault::open(dir.path());
        vault.ensure().unwrap();
        assert_eq!(vault.append("a line").unwrap(), 1);
        assert_eq!(vault.append("another").unwrap(), 2);
        let (entries, log) = vault.read().unwrap();
        assert_eq!(entries.len(), 2);
        assert_eq!(log.len(), 3, "APPEND, APPEND, READ");
        // READ was logged BEFORE the words came back
        assert!(log[2].contains("\"READ\""));

        vault.wipe("test arm").unwrap();
        assert_eq!(vault.load(), None, "wiped vault must be gone");
        assert_eq!(vault.presented_token(), "", "no token.bin, no token");
        let marker = std::fs::read_to_string(dir.path().join("marker.txt")).unwrap();
        assert!(marker.contains("OPENED BY test arm"), "{marker}");
        assert!(marker.contains("CONTENTS DESTROYED"));
        // append after a wipe: gone is gone, never a fresh start
        assert!(vault.append("resurrect").is_err());
    }

    #[test]
    fn status_is_public_and_counts() {
        let dir = tempfile::tempdir().unwrap();
        let vault = Vault::open(dir.path());
        vault.ensure().unwrap();
        vault.append("one").unwrap();
        let s = vault.public_status();
        assert_eq!(s["sealed"], serde_json::json!(true));
        assert_eq!(s["entries"], serde_json::json!(1));
        assert_eq!(s["access_count"], serde_json::json!(1));
        assert!(s["hash"].as_str().unwrap().len() == 16);
        assert_eq!(s["marker"], serde_json::Value::Null);
    }

}

