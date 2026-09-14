//! Codegen from the ONE schema. The .proto is the contract; nothing here
//! hand-writes a message, so a field added to proto/loa.proto reaches every
//! Rust crate or the build fails.

fn main() {
    let proto = "../../proto/loa.proto";
    println!("cargo:rerun-if-changed={proto}");
    prost_build::compile_protos(&[proto], &["../../proto"])
        .expect("codegen from proto/loa.proto — the wire contract is not optional");
}
