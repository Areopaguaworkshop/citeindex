//! CiteIndex v12 Kernel — DKEE state machine, type contracts, tool dispatcher.
//!
//! This crate implements the Rust kernel layer of the CiteIndex Harness OS.
//! The kernel owns the state machine and enforces invariants I1–I5 at compile
//! time (typestate) and runtime. It never calls an LLM directly.

pub mod types;
pub mod config;
pub mod storage;
pub mod state_machine;
pub mod indexes;
pub mod argument_graph;
pub mod scoring;
pub mod gate;
pub mod tools;
