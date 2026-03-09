//! Core orchestrator engine — coordinates Python IPC, plugins, memory,
//! and task monitoring.
//!
//! Matches `rust_core_orchestration.yaml`.

use crate::config::CiteIndexConfig;
use crate::ipc;
use crate::memory::{MemoryEntry, MemoryStore};
use serde_json::Value;
use std::path::PathBuf;

/// The central orchestrator that ties together all CiteIndex subsystems.
pub struct Engine {
    pub config: CiteIndexConfig,
    pub memory: MemoryStore,
}

impl Engine {
    /// Create a new engine with the given configuration.
    pub fn new(config: CiteIndexConfig) -> Self {
        let memory_dir = config.corpus_root.join(".memory");
        let memory = MemoryStore::new(&memory_dir);
        Self { config, memory }
    }

    /// Run ingestion via the Python subprocess.
    pub async fn ingest(
        &self,
        input_path: &str,
        extra_args: &[&str],
    ) -> anyhow::Result<Value> {
        let corpus = self.config.corpus_root.to_string_lossy();
        let result = ipc::trigger_ingestion(
            &self.config.python_bin,
            input_path,
            &corpus,
            extra_args,
        )
        .await?;

        if result.exit_code != 0 {
            tracing::warn!(
                exit_code = result.exit_code,
                stderr = %result.stderr,
                "Ingestion returned non-zero"
            );
        }

        Ok(result.json.unwrap_or(Value::Null))
    }

    /// Run search via the Python subprocess.
    pub async fn search(&self, query: &str) -> anyhow::Result<Value> {
        let corpus = self.config.corpus_root.to_string_lossy();
        let result =
            ipc::trigger_search(&self.config.python_bin, query, &corpus).await?;
        Ok(result.json.unwrap_or(Value::Null))
    }

    /// Run chat via the Python subprocess.
    pub async fn chat(
        &self,
        prompt: &str,
        thread_id: &str,
    ) -> anyhow::Result<Value> {
        let corpus = self.config.corpus_root.to_string_lossy();
        let result = ipc::trigger_chat(
            &self.config.python_bin,
            prompt,
            &corpus,
            &self.config.llm.model,
            thread_id,
        )
        .await?;

        // The Python chat pipeline already saves memory, but we can also
        // save on the Rust side for the Rust memory DAG.
        if let Some(ref json) = result.json {
            let answer = json
                .get("answer_human")
                .and_then(|v| v.as_str())
                .unwrap_or("");
            if !answer.is_empty() {
                let evidence_ids: Vec<String> = json
                    .get("answer_machine")
                    .and_then(|m| m.get("evidence"))
                    .and_then(|e| e.as_array())
                    .map(|arr| {
                        arr.iter()
                            .filter_map(|v| v.get("node_id").and_then(|n| n.as_str()))
                            .map(String::from)
                            .collect()
                    })
                    .unwrap_or_default();

                let entry = MemoryEntry::new(thread_id, prompt, answer, evidence_ids);
                if let Err(e) = self.memory.save(&entry) {
                    tracing::warn!("Failed to save Rust-side memory: {}", e);
                }
            }
        }

        Ok(result.json.unwrap_or(Value::Null))
    }

    /// Search chat memory.
    pub fn memory_search(
        &self,
        query: &str,
        thread_id: Option<&str>,
    ) -> Vec<MemoryEntry> {
        self.memory.search(query, thread_id)
    }

    /// Get the corpus root path.
    pub fn corpus_root(&self) -> &PathBuf {
        &self.config.corpus_root
    }
}
