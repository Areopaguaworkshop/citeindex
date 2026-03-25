//! Type contracts for the CiteIndex v12 kernel.
//!
//! These types enforce the five formal invariants (I1–I5) at compile time
//! (typestate) and runtime. They are the foundation that every other module
//! depends on.
//!
//! Layout matches `T_type_contracts.md`.

pub mod ids;
pub mod common;
pub mod claim;
pub mod context_slot;
pub mod commit;
pub mod replay;
pub mod frame;
pub mod state;
pub mod transitions;
pub mod interrupt;
pub mod recovery;
pub mod admission;
pub mod query_plan;
pub mod structure;

// Re-export all public types at the `types` level.
pub use ids::*;
pub use common::*;
pub use claim::*;
pub use context_slot::*;
pub use commit::*;
pub use replay::*;
pub use frame::*;
pub use state::*;
pub use transitions::*;
pub use interrupt::*;
pub use recovery::*;
pub use admission::*;
pub use query_plan::*;
pub use structure::*;
