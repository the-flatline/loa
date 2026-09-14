//! loa-common — what two or more modules must agree about, and nothing else.
//! A thing lives here exactly when a second crate needs the same definition;
//! pixels, renderers, and hardware live in the module that owns them.

pub mod condition;
pub mod geom;
