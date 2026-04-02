//! v12 agent IPC — Rust drives Python adapters over NDJSON.
//!
//! Replaces the legacy `python -m citeindex.cli` one-shot subprocess path with
//! long-lived v12 agent processes for chat/search/ingest.

use std::collections::HashSet;
use std::fs;
use std::path::{Path, PathBuf};
use std::time::Duration;

use anyhow::Context;
use citeindex_kernel::agent_runtime::{
    verify_output_hash, ActivationSection, AgentManifest as RuntimeManifest, AgentMessage,
    AgentProcess, AgentSection, AgentState, InnerLoopSection, LlmContractSection, ResourcesSection,
    ToolCallPayload, ToolsAllowedSection,
};
use citeindex_kernel::tools::{self, ToolContext};
use serde_json::Value;
use tokio::sync::Mutex;
use uuid::Uuid;

struct KernelToolState {
    corpus_root: PathBuf,
    ctx: ToolContext,
    indexed_corpus_dirs: HashSet<String>,
    indexed_memory_entries: HashSet<String>,
}

impl KernelToolState {
    fn new(corpus_root: PathBuf) -> anyhow::Result<Self> {
        let runtime_documents_dir = corpus_root.join(".v12_runtime").join("documents");
        fs::create_dir_all(runtime_documents_dir.join("structured"))?;
        let ctx = tools::in_memory_context(runtime_documents_dir)?;
        Ok(Self {
            corpus_root,
            ctx,
            indexed_corpus_dirs: HashSet::new(),
            indexed_memory_entries: HashSet::new(),
        })
    }

    fn sync_corpus(&mut self) -> anyhow::Result<()> {
        if !self.corpus_root.exists() {
            return Ok(());
        }

        for entry in fs::read_dir(&self.corpus_root)? {
            let entry = entry?;
            let path = entry.path();
            if !path.is_dir() || !path.join("csl.json").exists() {
                continue;
            }

            let key = path.to_string_lossy().to_string();
            if self.indexed_corpus_dirs.contains(&key) {
                continue;
            }

            self.index_document_dir(&path)?;
            self.indexed_corpus_dirs.insert(key);
        }

        Ok(())
    }

    fn sync_memory(&mut self) -> anyhow::Result<()> {
        let memory_dir = self.corpus_root.join(".memory");
        if !memory_dir.exists() {
            return Ok(());
        }

        for entry in fs::read_dir(&memory_dir)? {
            let entry = entry?;
            let path = entry.path();
            if !path.is_file() || path.extension().and_then(|ext| ext.to_str()) != Some("jsonl") {
                continue;
            }

            let content = fs::read_to_string(&path)?;
            for line in content.lines() {
                let line = line.trim();
                if line.is_empty() {
                    continue;
                }

                let value: Value = serde_json::from_str(line).with_context(|| {
                    format!("failed to parse memory entry in {}", path.display())
                })?;
                let memory_id = value
                    .get("entry_id")
                    .and_then(|field| field.as_str())
                    .map(str::to_string)
                    .unwrap_or_default();
                if memory_id.is_empty() || self.indexed_memory_entries.contains(&memory_id) {
                    continue;
                }

                let query = value
                    .get("query")
                    .and_then(|field| field.as_str())
                    .unwrap_or("");
                let response = value
                    .get("response")
                    .and_then(|field| field.as_str())
                    .unwrap_or("");
                let params = serde_json::json!({
                    "memory_id": memory_id,
                    "session_id": value.get("thread_id").and_then(|field| field.as_str()).unwrap_or("default"),
                    "title": query,
                    "description": query,
                    "content": response,
                    "merkle_hash": value.get("sha256").and_then(|field| field.as_str()).unwrap_or(""),
                    "language": detect_language(&format!("{query} {response}")),
                });

                citeindex_kernel::tools::memory_save::execute(&params, &mut self.ctx)
                    .map_err(anyhow::Error::from)?;
                self.indexed_memory_entries.insert(
                    value
                        .get("entry_id")
                        .and_then(|field| field.as_str())
                        .unwrap_or_default()
                        .to_string(),
                );
            }
        }

        Ok(())
    }

    fn index_document_dir(&mut self, path: &Path) -> anyhow::Result<()> {
        let csl = read_json(path.join("csl.json"))?;
        let document = read_optional_json(path.join("document.json"))?;
        let merkle = read_optional_json(path.join("merkle.json"))?;

        let doc_id = preferred_str(&csl, &["id", "content_hash"])
            .map(str::to_string)
            .unwrap_or_else(|| {
                path.file_name()
                    .unwrap_or_default()
                    .to_string_lossy()
                    .to_string()
            });
        let title = preferred_str(&csl, &["title"]).unwrap_or(&doc_id);
        let authors = format_authors(csl.get("author"));
        let abstract_text = preferred_str(&csl, &["abstract", "abstract_text"])
            .map(str::to_string)
            .or_else(|| document.as_ref().and_then(first_document_text))
            .unwrap_or_else(|| title.to_string());
        let language = preferred_str(&csl, &["language"])
            .filter(|language| !language.is_empty())
            .map(str::to_string)
            .unwrap_or_else(|| detect_language(&format!("{title} {abstract_text}")));

        let params = serde_json::json!({
            "doc_id": doc_id,
            "title": title,
            "authors": authors,
            "year": extract_year(csl.get("issued")).unwrap_or(0),
            "doi": preferred_str(&csl, &["DOI", "doi"]).unwrap_or(""),
            "abstract_text": abstract_text,
            "venue": preferred_str(&csl, &["container-title", "container_title"]).unwrap_or(""),
            "doc_type": preferred_str(&csl, &["type", "csl_type", "source_type"]).unwrap_or("article-journal"),
            "quality_tier": preferred_str(&csl, &["ci_quality_tier", "quality_tier"]).unwrap_or("silver"),
            "hierarchy_path": preferred_str(&csl, &["ci_hierarchy_path"]).unwrap_or(""),
            "merkle_hash": merkle.as_ref().and_then(|value| preferred_str(value, &["root"])).unwrap_or(""),
            "language": language,
        });

        write_compat_tree(
            &self.ctx.documents_dir,
            params
                .get("doc_id")
                .and_then(|value| value.as_str())
                .unwrap_or_default(),
            &csl,
            document.as_ref(),
            merkle.as_ref(),
        )?;

        citeindex_kernel::tools::index_document::execute(&params, &mut self.ctx)
            .map_err(anyhow::Error::from)?;
        Ok(())
    }
}

/// v12 agent runtime bridge used by the legacy core engine.
pub struct AgentRuntime {
    data_dir: String,
    default_model: String,
    coordinator: Mutex<AgentProcess>,
    librarian: Mutex<AgentProcess>,
    ingest: Mutex<AgentProcess>,
    tool_state: Mutex<Option<KernelToolState>>,
}

impl AgentRuntime {
    /// Build a new bridge backed by the thin v12 Python adapters.
    pub fn new(python_bin: &str, data_dir: &str, default_model: &str) -> Self {
        Self {
            data_dir: data_dir.to_string(),
            default_model: default_model.to_string(),
            coordinator: Mutex::new(agent_process(
                python_bin,
                "CoordinatorAgent",
                "citeindex.agents.coordinator",
                "cloud_standard",
                &[
                    "search_memory",
                    "tantivy_search",
                    "memory_save",
                    "tree_load",
                    "tree_traverse",
                    "csl_render",
                ],
            )),
            librarian: Mutex::new(agent_process(
                python_bin,
                "LibrarianAgent",
                "citeindex.agents.librarian",
                "cloud_standard",
                &["tantivy_search", "tree_load", "search_memory"],
            )),
            ingest: Mutex::new(agent_process(
                python_bin,
                "IngestAgent",
                "citeindex.agents.ingest",
                "cloud_standard",
                &["tree_load", "tantivy_index"],
            )),
            tool_state: Mutex::new(None),
        }
    }

    pub async fn chat(
        &self,
        prompt: &str,
        corpus_root: &str,
        llm_model: &str,
        thread_id: &str,
    ) -> anyhow::Result<Value> {
        self.request(
            &self.coordinator,
            llm_model,
            serde_json::json!({
                "operation": "chat",
                "prompt": prompt,
                "thread_id": thread_id,
                "corpus_root": corpus_root,
                "llm_model": llm_model,
            }),
        )
        .await
    }

    pub async fn search(
        &self,
        query: &str,
        corpus_root: &str,
        cite_style: &str,
    ) -> anyhow::Result<Value> {
        self.request(
            &self.librarian,
            &self.default_model,
            serde_json::json!({
                "query": query,
                "corpus_root": corpus_root,
                "cite_style": cite_style,
                "top_k": 20,
            }),
        )
        .await
    }

    pub async fn ingest(
        &self,
        input_path: &str,
        corpus_root: &str,
        extra_args: &[&str],
    ) -> anyhow::Result<Value> {
        let mut inputs = serde_json::json!({
            "input_ref": input_path,
            "corpus_root": corpus_root,
        });

        if let Some(doc_type_override) = extract_doc_type_override(extra_args) {
            inputs["doc_type_override"] = Value::String(doc_type_override.to_string());
        }

        self.request(&self.ingest, &self.default_model, inputs)
            .await
    }

    pub async fn shutdown(&self) -> anyhow::Result<()> {
        shutdown_agent(&self.coordinator).await?;
        shutdown_agent(&self.librarian).await?;
        shutdown_agent(&self.ingest).await?;
        Ok(())
    }

    async fn request(
        &self,
        slot: &Mutex<AgentProcess>,
        model: &str,
        inputs: Value,
    ) -> anyhow::Result<Value> {
        let mut agent = slot.lock().await;

        match self.request_once(&mut agent, model, inputs.clone()).await {
            Ok(output) => Ok(output),
            Err(first_error) => {
                tracing::warn!(agent = %agent.name, "agent request failed, retrying after restart: {first_error}");
                let _ = agent.shutdown().await;
                self.request_once(&mut agent, model, inputs)
                    .await
                    .with_context(|| {
                        format!("retry failed after initial agent error: {first_error}")
                    })
            }
        }
    }

    async fn request_once(
        &self,
        agent: &mut AgentProcess,
        model: &str,
        inputs: Value,
    ) -> anyhow::Result<Value> {
        ensure_spawned(agent, &self.data_dir, model).await?;

        let task_id = Uuid::new_v4().to_string();
        let request = serde_json::json!({
            "type": "request",
            "task_id": task_id,
            "inputs": inputs,
        });

        agent.state = AgentState::Running;
        agent.current_task_id = Some(task_id.clone());
        agent.send(&request).await?;

        loop {
            let timeout_s = agent.manifest.resources.request_timeout_s;
            let message = tokio::time::timeout(Duration::from_secs(timeout_s), agent.recv())
                .await
                .with_context(|| format!("request timeout for agent {}", agent.name))??;

            match message {
                AgentMessage::Progress(progress) => {
                    tracing::debug!(
                        agent = %agent.name,
                        task_id = %progress.task_id,
                        stage = %progress.stage,
                        detail = %progress.detail,
                        "agent progress"
                    );
                }
                AgentMessage::LlmReport(report) => {
                    tracing::debug!(
                        agent = %agent.name,
                        task_id = %report.task_id,
                        model = %report.model,
                        total_tokens = report.total_tokens,
                        "agent llm report"
                    );
                }
                AgentMessage::Result(payload) => {
                    finalize_task(agent);
                    if payload.task_id != task_id {
                        anyhow::bail!(
                            "agent {} returned result for task {}, expected {}",
                            agent.name,
                            payload.task_id,
                            task_id
                        );
                    }
                    if !verify_output_hash(&payload.output, &payload.output_hash) {
                        anyhow::bail!(
                            "agent {} returned invalid output hash for task {}",
                            agent.name,
                            task_id
                        );
                    }
                    return Ok(payload.output);
                }
                AgentMessage::Error(payload) => {
                    finalize_task(agent);
                    anyhow::bail!(
                        "agent {} returned {} for task {}: {}",
                        agent.name,
                        payload.error_type,
                        payload.task_id,
                        payload.message
                    );
                }
                AgentMessage::ToolCall(payload) => {
                    self.handle_tool_call(agent, payload).await?;
                }
                AgentMessage::InitAck(_) | AgentMessage::ShutdownAck(_) => {
                    tracing::warn!(agent = %agent.name, "ignoring unexpected control message during request");
                }
            }
        }
    }

    async fn handle_tool_call(
        &self,
        agent: &mut AgentProcess,
        payload: ToolCallPayload,
    ) -> anyhow::Result<()> {
        let mut tool_state = self.tool_state.lock().await;
        if tool_state.is_none() {
            *tool_state = Some(KernelToolState::new(PathBuf::from(&self.data_dir))?);
        }

        let state = tool_state
            .as_mut()
            .context("kernel tool runtime not initialized")?;
        state.sync_corpus()?;
        state.sync_memory()?;

        let tool_manifest = tools::AgentManifest {
            name: agent.name.0.clone(),
            tools_allowed: agent.manifest.tools_set(),
        };
        let call = tools::ToolCall {
            tool: payload.tool,
            call_id: payload.call_id,
            params: payload.params,
        };
        let response =
            tools::dispatch_tool_call(&call, &agent.name, &tool_manifest, &mut state.ctx)
                .map_err(anyhow::Error::from)?;

        if call.tool == "memory_save" {
            if let Some(memory_id) = response
                .result
                .as_ref()
                .and_then(|value| value.get("memory_id"))
                .and_then(|value| value.as_str())
            {
                state.indexed_memory_entries.insert(memory_id.to_string());
            }
        }

        agent
            .send(&serde_json::json!({
                "type": "tool_response",
                "call_id": response.call_id,
                "result": response.result,
                "error": response.error,
            }))
            .await
    }
}

fn agent_process(
    python_bin: &str,
    agent_name: &str,
    module: &str,
    model_tier: &str,
    tools_allowed: &[&str],
) -> AgentProcess {
    let manifest = RuntimeManifest {
        agent: AgentSection {
            name: agent_name.to_string(),
            version: "12.0".to_string(),
            domain: "core-runtime-bridge".to_string(),
            entry_point: format!("{python_bin} -m {module}"),
            description: format!("Transitional core runtime bridge for {agent_name}"),
        },
        llm_contract: LlmContractSection {
            model_tier: model_tier.to_string(),
            grounding: "required".to_string(),
            temperature: 0.0,
            max_tokens: 4096,
            output_schema: String::new(),
        },
        activation: ActivationSection::default(),
        tools_allowed: ToolsAllowedSection {
            tools: tools_allowed
                .iter()
                .map(|tool| (*tool).to_string())
                .collect(),
        },
        resources: ResourcesSection::default(),
        inner_loop: InnerLoopSection::default(),
    };

    AgentProcess::new(manifest)
}

async fn ensure_spawned(
    agent: &mut AgentProcess,
    data_dir: &str,
    model: &str,
) -> anyhow::Result<()> {
    if matches!(agent.state, AgentState::NotSpawned | AgentState::Dead) {
        let model_tier = agent.manifest.llm_contract.model_tier.clone();
        agent.spawn_and_init(data_dir, model, &model_tier).await?;
    }
    Ok(())
}

async fn shutdown_agent(slot: &Mutex<AgentProcess>) -> anyhow::Result<()> {
    let mut agent = slot.lock().await;
    agent.shutdown().await
}

fn finalize_task(agent: &mut AgentProcess) {
    agent.state = AgentState::Idle;
    agent.current_task_id = None;
}

fn read_json(path: PathBuf) -> anyhow::Result<Value> {
    let text =
        fs::read_to_string(&path).with_context(|| format!("failed to read {}", path.display()))?;
    let value = serde_json::from_str(&text)
        .with_context(|| format!("failed to parse {}", path.display()))?;
    Ok(value)
}

fn read_optional_json(path: PathBuf) -> anyhow::Result<Option<Value>> {
    if !path.exists() {
        return Ok(None);
    }
    read_json(path).map(Some)
}

fn preferred_str<'a>(value: &'a Value, keys: &[&str]) -> Option<&'a str> {
    keys.iter()
        .find_map(|key| value.get(*key).and_then(|field| field.as_str()))
}

fn extract_year(value: Option<&Value>) -> Option<i64> {
    value
        .and_then(|issued| issued.get("date-parts"))
        .and_then(|parts| parts.as_array())
        .and_then(|parts| parts.first())
        .and_then(|part| part.as_array())
        .and_then(|part| part.first())
        .and_then(|year| year.as_i64())
}

fn format_authors(value: Option<&Value>) -> String {
    value
        .and_then(|authors| authors.as_array())
        .map(|authors| {
            authors
                .iter()
                .take(3)
                .filter_map(|author| {
                    let literal = author.get("literal").and_then(|field| field.as_str());
                    let family = author.get("family").and_then(|field| field.as_str());
                    let given = author.get("given").and_then(|field| field.as_str());

                    literal
                        .map(str::to_string)
                        .or_else(|| match (family, given) {
                            (Some(family), Some(given))
                                if !family.is_empty() && !given.is_empty() =>
                            {
                                Some(format!("{family} {given}"))
                            }
                            (Some(family), _) if !family.is_empty() => Some(family.to_string()),
                            (_, Some(given)) if !given.is_empty() => Some(given.to_string()),
                            _ => None,
                        })
                })
                .collect::<Vec<_>>()
                .join(", ")
        })
        .unwrap_or_default()
}

fn first_document_text(document: &Value) -> Option<String> {
    if let Some(nodes) = document.get("nodes").and_then(|value| value.as_array()) {
        if let Some(text) = nodes.iter().find_map(first_text_value) {
            return Some(text);
        }
    }
    first_text_value(document)
}

fn first_text_value(value: &Value) -> Option<String> {
    match value {
        Value::String(text) if !text.trim().is_empty() => Some(text.trim().to_string()),
        Value::Array(items) => items.iter().find_map(first_text_value),
        Value::Object(map) => {
            if let Some(text) = map.get("text").and_then(|value| value.as_str()) {
                if !text.trim().is_empty() {
                    return Some(text.trim().to_string());
                }
            }

            map.values().find_map(first_text_value)
        }
        _ => None,
    }
}

fn detect_language(text: &str) -> String {
    if text
        .chars()
        .any(|ch| ('\u{3040}'..='\u{30ff}').contains(&ch))
    {
        return "ja".to_string();
    }
    if text
        .chars()
        .any(|ch| ('\u{4e00}'..='\u{9fff}').contains(&ch))
    {
        return "zh".to_string();
    }
    "en".to_string()
}

fn write_compat_tree(
    documents_dir: &Path,
    doc_id: &str,
    csl: &Value,
    document: Option<&Value>,
    merkle: Option<&Value>,
) -> anyhow::Result<()> {
    let structured_dir = documents_dir.join("structured");
    fs::create_dir_all(&structured_dir)?;

    let tree_path = structured_dir.join(format!("{doc_id}.citeindex.json"));
    let mut sections = serde_json::Map::new();

    if let Some(nodes) = document
        .and_then(|value| value.get("nodes"))
        .and_then(|value| value.as_array())
    {
        for node in nodes {
            let section_key = node
                .get("section_path")
                .and_then(|value| value.as_str())
                .filter(|value| !value.is_empty())
                .unwrap_or("root")
                .to_string();
            let entry = sections
                .entry(section_key)
                .or_insert_with(|| Value::Array(Vec::new()));
            if let Some(array) = entry.as_array_mut() {
                array.push(node.clone());
            }
        }
    }

    let level_1 = if sections.is_empty() {
        let fallback_text = document.and_then(first_document_text).unwrap_or_default();
        vec![serde_json::json!({
            "node_id": format!("{doc_id}:section:root"),
            "heading": "Document",
            "section_number": serde_json::Value::Null,
            "section_type": "document",
            "page_range": serde_json::Value::Null,
            "children": [{
                "node_id": format!("{doc_id}:subsection:root"),
                "heading": "Document",
                "section_number": serde_json::Value::Null,
                "children": [{
                    "node_id": format!("{doc_id}:root"),
                    "locator_type": "paragraph",
                    "page_number": serde_json::Value::Null,
                    "page_label": serde_json::Value::Null,
                    "text_blocks": [],
                    "figures": [],
                    "tables": [],
                    "paragraph_number": 0,
                    "paragraph_id": format!("{doc_id}:root"),
                    "text": fallback_text,
                    "start_time": serde_json::Value::Null,
                    "end_time": serde_json::Value::Null,
                    "speaker": serde_json::Value::Null,
                    "transcript_text": serde_json::Value::Null,
                    "children": [],
                }],
            }],
        })]
    } else {
        sections
            .into_iter()
            .map(|(section_path, nodes)| {
                let locators = nodes
                    .as_array()
                    .into_iter()
                    .flatten()
                    .enumerate()
                    .map(|(index, node)| {
                        let node_id = node
                            .get("node_id")
                            .and_then(|value| value.as_str())
                            .map(str::to_string)
                            .unwrap_or_else(|| format!("{doc_id}:{section_path}:{index}"));
                        serde_json::json!({
                            "node_id": node_id,
                            "locator_type": "paragraph",
                            "page_number": node.get("page").cloned().unwrap_or(serde_json::Value::Null),
                            "page_label": node.get("page").cloned().unwrap_or(serde_json::Value::Null),
                            "text_blocks": [],
                            "figures": [],
                            "tables": [],
                            "paragraph_number": node.get("paragraph").cloned().unwrap_or(serde_json::json!(index as i64)),
                            "paragraph_id": node.get("node_id").cloned().unwrap_or_else(|| serde_json::json!(format!("{doc_id}:{index}"))),
                            "text": node.get("text").cloned().unwrap_or(serde_json::Value::Null),
                            "start_time": serde_json::Value::Null,
                            "end_time": serde_json::Value::Null,
                            "speaker": serde_json::Value::Null,
                            "transcript_text": serde_json::Value::Null,
                            "children": [],
                        })
                    })
                    .collect::<Vec<_>>();

                serde_json::json!({
                    "node_id": format!("{doc_id}:section:{section_path}"),
                    "heading": section_path,
                    "section_number": serde_json::Value::Null,
                    "section_type": "section",
                    "page_range": serde_json::Value::Null,
                    "children": [{
                        "node_id": format!("{doc_id}:subsection:{section_path}"),
                        "heading": section_path,
                        "section_number": serde_json::Value::Null,
                        "children": locators,
                    }],
                })
            })
            .collect::<Vec<_>>()
    };

    let tree = serde_json::json!({
        "citeindex_version": "12.0",
        "tree_version": "1.0",
        "level_0": {
            "id": csl.get("id").cloned().unwrap_or_else(|| serde_json::json!(doc_id)),
            "type": csl.get("type").cloned().unwrap_or_else(|| serde_json::json!("article-journal")),
            "title": csl.get("title").cloned().unwrap_or_else(|| serde_json::json!(doc_id)),
            "author": csl.get("author").cloned().unwrap_or_else(|| serde_json::json!([])),
            "editor": csl.get("editor").cloned().unwrap_or_else(|| serde_json::json!([])),
            "issued": csl.get("issued").cloned().unwrap_or(serde_json::Value::Null),
            "DOI": csl.get("DOI").cloned().or_else(|| csl.get("doi").cloned()).unwrap_or(serde_json::Value::Null),
            "ISBN": csl.get("ISBN").cloned().unwrap_or(serde_json::Value::Null),
            "URL": csl.get("URL").cloned().unwrap_or(serde_json::Value::Null),
            "container-title": csl.get("container-title").cloned().or_else(|| csl.get("container_title").cloned()).unwrap_or(serde_json::Value::Null),
            "volume": csl.get("volume").cloned().unwrap_or(serde_json::Value::Null),
            "issue": csl.get("issue").cloned().unwrap_or(serde_json::Value::Null),
            "page": csl.get("page").cloned().unwrap_or(serde_json::Value::Null),
            "publisher": csl.get("publisher").cloned().unwrap_or(serde_json::Value::Null),
            "publisher-place": csl.get("publisher-place").cloned().unwrap_or(serde_json::Value::Null),
            "abstract": csl.get("abstract").cloned().or_else(|| document.and_then(first_document_text).map(serde_json::Value::String)).unwrap_or(serde_json::Value::Null),
            "language": csl.get("language").cloned().unwrap_or_else(|| serde_json::json!(detect_language(preferred_str(csl, &["title"]).unwrap_or(doc_id)))),
            "keyword": csl.get("keyword").cloned().unwrap_or(serde_json::Value::Null),
            "ci_doc_id": doc_id,
            "ci_quality_tier": csl.get("ci_quality_tier").cloned().unwrap_or_else(|| serde_json::json!("silver")),
            "ci_hierarchy_path": csl.get("ci_hierarchy_path").cloned().unwrap_or(serde_json::Value::Null),
            "ci_merkle_hash": merkle.and_then(|value| preferred_str(value, &["root"])).map(|value| serde_json::Value::String(value.to_string())).unwrap_or(serde_json::Value::Null),
            "ci_source_type": csl.get("source_type").cloned().unwrap_or(serde_json::Value::Null),
            "ci_ingested_at": csl.get("ingestion_timestamp").cloned().unwrap_or(serde_json::Value::Null),
            "ci_structure_confidence": serde_json::Value::Null,
            "ci_indexed_at": serde_json::Value::Null,
            "ci_project_ids": [],
            "ci_claim_anchors": [],
        },
        "level_1": level_1,
    });

    fs::write(tree_path, serde_json::to_vec_pretty(&tree)?)?;
    Ok(())
}

fn extract_doc_type_override(extra_args: &[&str]) -> Option<String> {
    let mut args = extra_args.iter().copied();
    while let Some(arg) = args.next() {
        if matches!(arg, "--type" | "--doc-type-override") {
            return args.next().map(str::to_string);
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_extract_doc_type_override_from_legacy_args() {
        let extra = ["--foo", "bar", "--doc-type-override", "thesis"];
        assert_eq!(
            extract_doc_type_override(&extra),
            Some("thesis".to_string())
        );
    }

    #[test]
    fn test_extract_doc_type_override_from_short_type_alias() {
        let extra = ["--type", "article"];
        assert_eq!(
            extract_doc_type_override(&extra),
            Some("article".to_string())
        );
    }

    #[test]
    fn test_detect_language_prefers_cjk_content() {
        assert_eq!(detect_language("教会历史"), "zh");
        assert_eq!(detect_language("ギリシャ教父"), "ja");
        assert_eq!(detect_language("church history"), "en");
    }
}
