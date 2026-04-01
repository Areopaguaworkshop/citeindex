//! Rust-native memory manager — Merkle DAG for chat memory with
//! file-based persistence and optional PostgreSQL backend.
//!
//! Matches Phase 3.4 and `rust_core_orchestration.yaml → manage_memory`.

use crate::merkle::{build_merkle_tree, sha256_hex, MerkleTree};
use chrono::Utc;
use serde::{Deserialize, Serialize};
use std::fs::{self, OpenOptions};
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};

/// A single memory entry persisted to JSONL.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MemoryEntry {
    pub entry_id: String,
    pub timestamp: String,
    pub thread_id: String,
    pub query: String,
    pub response: String,
    pub evidence_node_ids: Vec<String>,
    pub sha256: String,
}

impl MemoryEntry {
    pub fn new(
        thread_id: &str,
        query: &str,
        response: &str,
        evidence_node_ids: Vec<String>,
    ) -> Self {
        let timestamp = Utc::now().format("%Y-%m-%dT%H:%M:%S+00:00").to_string();
        let payload = format!("{}|{}|{}", timestamp, query, response);
        let hash = sha256_hex(&payload);
        let entry_id = hash[..16].to_string();

        Self {
            entry_id,
            timestamp,
            thread_id: thread_id.into(),
            query: query.into(),
            response: response.into(),
            evidence_node_ids,
            sha256: hash,
        }
    }
}

/// File-based JSONL memory store with Merkle DAG support.
pub struct MemoryStore {
    memory_dir: PathBuf,
}

impl MemoryStore {
    pub fn new(memory_dir: &Path) -> Self {
        fs::create_dir_all(memory_dir).ok();
        Self {
            memory_dir: memory_dir.to_path_buf(),
        }
    }

    /// Save a memory entry to the thread's JSONL file.
    pub fn save(&self, entry: &MemoryEntry) -> anyhow::Result<()> {
        let path = self.thread_path(&entry.thread_id);
        let mut file = OpenOptions::new().create(true).append(true).open(&path)?;
        let line = serde_json::to_string(entry)?;
        writeln!(file, "{}", line)?;
        tracing::info!(
            entry_id = %entry.entry_id,
            thread = %entry.thread_id,
            "Memory entry saved"
        );
        Ok(())
    }

    /// Load all entries for a thread.
    pub fn load_thread(&self, thread_id: &str) -> Vec<MemoryEntry> {
        let path = self.thread_path(thread_id);
        if !path.exists() {
            return Vec::new();
        }

        let file = match fs::File::open(&path) {
            Ok(f) => f,
            Err(_) => return Vec::new(),
        };

        BufReader::new(file)
            .lines()
            .filter_map(|line| line.ok())
            .filter(|line| !line.trim().is_empty())
            .filter_map(|line| serde_json::from_str::<MemoryEntry>(&line).ok())
            .collect()
    }

    /// Simple keyword search across all threads (or a specific thread).
    pub fn search(&self, query: &str, thread_id: Option<&str>) -> Vec<MemoryEntry> {
        let query_lower = query.to_lowercase();
        let keywords: Vec<&str> = query_lower.split_whitespace().collect();
        if keywords.is_empty() {
            return Vec::new();
        }

        let threads = match thread_id {
            Some(tid) => vec![tid.to_string()],
            None => self.list_threads(),
        };

        let mut results: Vec<(usize, MemoryEntry)> = Vec::new();
        for tid in &threads {
            for entry in self.load_thread(tid) {
                let text = format!("{} {}", entry.query, entry.response).to_lowercase();
                let hits = keywords.iter().filter(|kw| text.contains(*kw)).count();
                if hits > 0 {
                    results.push((hits, entry));
                }
            }
        }

        results.sort_by(|a, b| b.0.cmp(&a.0));
        results.into_iter().map(|(_, e)| e).collect()
    }

    /// Build a Merkle tree for all entries in a thread.
    pub fn build_merkle_dag(&self, thread_id: &str) -> MerkleTree {
        let entries = self.load_thread(thread_id);
        let hashes: Vec<String> = entries.iter().map(|e| e.sha256.clone()).collect();
        build_merkle_tree(&hashes)
    }

    /// List all thread IDs.
    pub fn list_threads(&self) -> Vec<String> {
        let mut threads = Vec::new();
        if let Ok(entries) = fs::read_dir(&self.memory_dir) {
            for entry in entries.flatten() {
                if let Some(name) = entry.file_name().to_str() {
                    if name.ends_with(".jsonl") {
                        threads.push(name.trim_end_matches(".jsonl").to_string());
                    }
                }
            }
        }
        threads.sort();
        threads
    }

    fn thread_path(&self, thread_id: &str) -> PathBuf {
        let safe: String = thread_id
            .chars()
            .map(|c| {
                if c.is_alphanumeric() || c == '-' || c == '_' {
                    c
                } else {
                    '_'
                }
            })
            .collect();
        self.memory_dir.join(format!("{}.jsonl", safe))
    }
}
