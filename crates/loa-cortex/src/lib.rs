//! loa-cortex — the coordinator. It holds the body's state, routes commands,
//! relays frames to the topics, and owns every consumer-facing publish. It
//! never touches a pixel.
//!
//! The binary is the last thing wired up here: state first, then the legs
//! (relay, merge, commands, store flush, init), then the tasks that tie them
//! to the sockets, then main. Each layer is tested against the layer below
//! before the one above it exists.

pub mod build;
pub mod moods;
pub mod store;
pub mod vault;
pub mod verbs;
pub mod state;
