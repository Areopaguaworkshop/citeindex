//! Tool: memory_save — stub implementation.
//!
//! Phase 2: returns NotImplemented. Full implementation in Phase 3.

use super::{ToolContext, ToolError};

pub fn execute(
    _params: &serde_json::Value,
    _ctx: &mut ToolContext,
) -> Result<serde_json::Value, ToolError> {
    Err(ToolError::NotImplemented("memory_save".into()))
}
