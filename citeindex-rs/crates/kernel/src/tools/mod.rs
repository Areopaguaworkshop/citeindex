//! Tool Dispatcher — I1_tool_dispatcher_contract.md
//!
//! The kernel's syscall layer. Agents never access indexes, databases, or
//! files directly. Every data operation goes through a tool call.

pub mod search_documents;
pub mod search_claims;
pub mod search_memory;
pub mod index_document;
pub mod index_claim;
pub mod delete_document;
pub mod ag_query_claims;
pub mod ag_query_contradictions;
pub mod ag_write_edge;
pub mod merkle_compute;
pub mod merkle_verify;
pub mod csl_render;
pub mod tree_load;
pub mod tree_traverse;
pub mod regex_search;
pub mod memory_save;

use std::collections::{HashMap, HashSet};
use std::path::PathBuf;
use std::sync::{Arc, Mutex};

use crate::scoring::ScoreFusionWeights;
use crate::types::ids::AgentName;

/// Tool call from an agent.
#[derive(Debug, Clone, serde::Deserialize)]
pub struct ToolCall {
    pub tool: String,
    pub call_id: String,
    pub params: serde_json::Value,
}

/// Tool response back to agent.
#[derive(Debug, Clone, serde::Serialize)]
pub struct ToolResponse {
    pub call_id: String,
    pub result: Option<serde_json::Value>,
    pub error: Option<ToolErrorPayload>,
}

/// Serializable error payload for agent communication.
#[derive(Debug, Clone, serde::Serialize)]
pub struct ToolErrorPayload {
    pub error_type: String,
    pub message: String,
}

/// Tool errors.
#[derive(Debug, thiserror::Error)]
pub enum ToolError {
    #[error("permission denied: agent {agent} cannot call tool {tool}")]
    PermissionDenied { agent: String, tool: String },

    #[error("unknown tool: {0}")]
    UnknownTool(String),

    #[error("invalid params: {param}: {message}")]
    InvalidParams { param: String, message: String },

    #[error("not found: {resource_type} {id}")]
    NotFound { resource_type: String, id: String },

    #[error("index error: {0}")]
    IndexError(String),

    #[error("database error: {0}")]
    DatabaseError(String),

    #[error("io error: {0}")]
    IoError(String),

    #[error("merkle error: {0}")]
    MerkleError(String),

    #[error("not implemented: {0}")]
    NotImplemented(String),
}

impl ToolError {
    pub fn to_payload(&self) -> ToolErrorPayload {
        let error_type = match self {
            Self::PermissionDenied { .. } => "PermissionDenied",
            Self::UnknownTool(_) => "UnknownTool",
            Self::InvalidParams { .. } => "InvalidParams",
            Self::NotFound { .. } => "NotFound",
            Self::IndexError(_) => "IndexError",
            Self::DatabaseError(_) => "DatabaseError",
            Self::IoError(_) => "IoError",
            Self::MerkleError(_) => "MerkleError",
            Self::NotImplemented(_) => "NotImplemented",
        };
        ToolErrorPayload {
            error_type: error_type.to_string(),
            message: self.to_string(),
        }
    }
}

/// Agent manifest — defines which tools an agent may call.
#[derive(Debug, Clone)]
pub struct AgentManifest {
    pub name: String,
    pub tools_allowed: HashSet<String>,
}

/// Shared mutable state available to all tools.
///
/// Holds tantivy index handles (readers + writers) for the three indexes,
/// a SQLite connection for the ArgumentGraph, and score fusion weights.
pub struct ToolContext {
    // ── Tantivy indexes ──────────────────────────────────
    pub document_index: tantivy::Index,
    pub claim_index: tantivy::Index,
    pub memory_index: tantivy::Index,
    pub document_writer: Arc<Mutex<tantivy::IndexWriter>>,
    pub claim_writer: Arc<Mutex<tantivy::IndexWriter>>,
    pub memory_writer: Arc<Mutex<tantivy::IndexWriter>>,

    // ── SQLite ───────────────────────────────────────────
    pub argument_graph_db: Arc<Mutex<rusqlite::Connection>>,

    // ── Paths ────────────────────────────────────────────
    pub documents_dir: PathBuf,
    pub sources_dir: PathBuf,

    // ── Scoring ──────────────────────────────────────────
    pub score_fusion_weights: ScoreFusionWeights,

    // ── Memory access cache ──────────────────────────────
    pub memory_access_cache: HashMap<String, MemoryAccessEntry>,
}

/// Memory access metadata entry.
#[derive(Debug, Clone)]
pub struct MemoryAccessEntry {
    pub access_count: u64,
    pub last_accessed: String,
}

/// Dispatch a tool call, enforcing agent permissions.
pub fn dispatch_tool_call(
    call: &ToolCall,
    agent_name: &AgentName,
    agent_manifest: &AgentManifest,
    ctx: &mut ToolContext,
) -> Result<ToolResponse, ToolError> {
    // 1. Check permission
    if !agent_manifest.tools_allowed.contains(&call.tool) {
        return Err(ToolError::PermissionDenied {
            agent: agent_name.0.clone(),
            tool: call.tool.clone(),
        });
    }

    // 2. Route to tool implementation
    let result = match call.tool.as_str() {
        "search_documents"        => search_documents::execute(&call.params, ctx),
        "search_claims"           => search_claims::execute(&call.params, ctx),
        "search_memory"           => search_memory::execute(&call.params, ctx),
        "index_document"          => index_document::execute(&call.params, ctx),
        "index_claim"             => index_claim::execute(&call.params, ctx),
        "delete_document"         => delete_document::execute(&call.params, ctx),
        "ag_query_claims"         => ag_query_claims::execute(&call.params, ctx),
        "ag_query_contradictions" => ag_query_contradictions::execute(&call.params, ctx),
        "ag_write_edge"           => ag_write_edge::execute(&call.params, ctx),
        "merkle_compute"          => merkle_compute::execute(&call.params, ctx),
        "merkle_verify"           => merkle_verify::execute(&call.params, ctx),
        "csl_render"              => csl_render::execute(&call.params, ctx),
        "tree_load"               => tree_load::execute(&call.params, ctx),
        "tree_traverse"           => tree_traverse::execute(&call.params, ctx),
        "regex_search"            => regex_search::execute(&call.params, ctx),
        "memory_save"             => memory_save::execute(&call.params, ctx),
        _                         => Err(ToolError::UnknownTool(call.tool.clone())),
    };

    match result {
        Ok(value) => Ok(ToolResponse {
            call_id: call.call_id.clone(),
            result: Some(value),
            error: None,
        }),
        Err(e) => Ok(ToolResponse {
            call_id: call.call_id.clone(),
            result: None,
            error: Some(e.to_payload()),
        }),
    }
}
