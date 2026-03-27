//! Trace System — S7_trace_schema.md
//!
//! Every ExecutionFrame produces one trace. A trace is an ordered sequence
//! of spans in JSONL format recording state transitions, agent calls,
//! tool calls, LLM invocations, and progress.

use std::fs::{self, File};
use std::io::{BufWriter, Write};
use std::path::{Path, PathBuf};

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use uuid::Uuid;

use crate::types::ids::{FrameId, TraceId};

/// Unique identifier for a span within a trace.
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct SpanId(pub Uuid);

impl SpanId {
    pub fn new() -> Self {
        Self(Uuid::new_v4())
    }
}

impl std::fmt::Display for SpanId {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.0)
    }
}

/// Classification of a trace span.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SpanType {
    StateTransition,
    AgentRequest,
    AgentProgress,
    AgentResult,
    ToolCall,
    ToolResponse,
    LlmCall,
    VerifyGate,
    Commit,
    Coverage,
    ConstraintCheck,
    Interrupt,
    Error,
}

/// A single span in the trace.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Span {
    pub trace_id: TraceId,
    pub span_id: SpanId,
    pub parent_span_id: Option<SpanId>,
    pub started_at: DateTime<Utc>,
    pub ended_at: Option<DateTime<Utc>>,
    pub duration_ms: Option<i64>,
    pub span_type: SpanType,
    pub name: String,
    pub frame_id: FrameId,
    pub frame_state: String,
    pub payload: serde_json::Value,
}

impl Span {
    /// Create a new span starting now.
    pub fn new(
        trace_id: TraceId,
        frame_id: FrameId,
        span_type: SpanType,
        name: String,
        frame_state: String,
        parent_span_id: Option<SpanId>,
        payload: serde_json::Value,
    ) -> Self {
        Self {
            trace_id,
            span_id: SpanId::new(),
            parent_span_id,
            started_at: Utc::now(),
            ended_at: None,
            duration_ms: None,
            span_type,
            name,
            frame_id,
            frame_state,
            payload,
        }
    }

    /// Mark this span as finished.
    pub fn finish(&mut self) {
        let now = Utc::now();
        self.duration_ms = Some((now - self.started_at).num_milliseconds());
        self.ended_at = Some(now);
    }
}

/// Trace writer. One per ExecutionFrame.
/// Appends spans to a trace JSONL file.
pub struct TraceWriter {
    trace_id: TraceId,
    frame_id: FrameId,
    writer: BufWriter<File>,
    path: PathBuf,
}

impl TraceWriter {
    /// Create a new trace file for this execution.
    ///
    /// Path: `{traces_dir}/{YYYY-MM-DD}/{trace_id}.jsonl`
    pub fn new(
        trace_id: TraceId,
        frame_id: FrameId,
        traces_dir: &Path,
    ) -> anyhow::Result<Self> {
        let date = Utc::now().format("%Y-%m-%d").to_string();
        let date_dir = traces_dir.join(&date);
        fs::create_dir_all(&date_dir)?;

        let filename = format!("{}.jsonl", trace_id.0);
        let path = date_dir.join(&filename);
        let file = File::create(&path)?;
        let writer = BufWriter::new(file);

        tracing::debug!(trace_id = %trace_id.0, path = %path.display(), "trace file created");

        Ok(Self {
            trace_id,
            frame_id,
            writer,
            path,
        })
    }

    /// Write a span to the trace file.
    pub fn write_span(&mut self, span: &Span) -> anyhow::Result<()> {
        let json = serde_json::to_string(span)?;
        writeln!(self.writer, "{json}")?;
        self.writer.flush()?;
        Ok(())
    }

    /// Create and immediately write a completed span.
    pub fn record(
        &mut self,
        span_type: SpanType,
        name: &str,
        frame_state: &str,
        parent_span_id: Option<SpanId>,
        payload: serde_json::Value,
    ) -> anyhow::Result<SpanId> {
        let mut span = Span::new(
            self.trace_id.clone(),
            self.frame_id.clone(),
            span_type,
            name.to_string(),
            frame_state.to_string(),
            parent_span_id,
            payload,
        );
        span.finish();
        let span_id = span.span_id.clone();
        self.write_span(&span)?;
        Ok(span_id)
    }

    /// Record a state transition span.
    pub fn record_transition(
        &mut self,
        from: &str,
        to: &str,
        guard: &str,
        passed: bool,
        error: Option<&str>,
    ) -> anyhow::Result<SpanId> {
        self.record(
            SpanType::StateTransition,
            &format!("{from} → {to}"),
            from,
            None,
            serde_json::json!({
                "from": from,
                "to": to,
                "guard": guard,
                "guard_passed": passed,
                "guard_error": error,
            }),
        )
    }

    /// Record a tool call span.
    pub fn record_tool_call(
        &mut self,
        tool: &str,
        call_id: &str,
        caller: &str,
        params: &serde_json::Value,
        frame_state: &str,
        parent_span_id: Option<SpanId>,
    ) -> anyhow::Result<SpanId> {
        self.record(
            SpanType::ToolCall,
            tool,
            frame_state,
            parent_span_id,
            serde_json::json!({
                "tool": tool,
                "call_id": call_id,
                "caller": caller,
                "params": params,
            }),
        )
    }

    /// Record a tool response span.
    pub fn record_tool_response(
        &mut self,
        tool: &str,
        call_id: &str,
        result_count: usize,
        error: Option<&str>,
        frame_state: &str,
        parent_span_id: Option<SpanId>,
    ) -> anyhow::Result<SpanId> {
        self.record(
            SpanType::ToolResponse,
            &format!("{tool} response"),
            frame_state,
            parent_span_id,
            serde_json::json!({
                "tool": tool,
                "call_id": call_id,
                "result_count": result_count,
                "error": error,
            }),
        )
    }

    /// Record an error span.
    pub fn record_error(
        &mut self,
        name: &str,
        error_type: &str,
        message: &str,
        frame_state: &str,
    ) -> anyhow::Result<SpanId> {
        self.record(
            SpanType::Error,
            name,
            frame_state,
            None,
            serde_json::json!({
                "error_type": error_type,
                "message": message,
            }),
        )
    }

    /// Get the trace file path.
    pub fn path(&self) -> &Path {
        &self.path
    }

    /// Flush the writer.
    pub fn flush(&mut self) -> anyhow::Result<()> {
        self.writer.flush()?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn make_trace_writer() -> (TraceWriter, PathBuf) {
        let dir = std::env::temp_dir().join(format!("citeindex_test_{}", Uuid::new_v4()));
        fs::create_dir_all(&dir).unwrap();
        let trace_id = TraceId::new();
        let frame_id = FrameId::new();
        let writer = TraceWriter::new(trace_id, frame_id, &dir).unwrap();
        (writer, dir)
    }

    #[test]
    fn test_span_new_and_finish() {
        let trace_id = TraceId::new();
        let frame_id = FrameId::new();
        let mut span = Span::new(
            trace_id,
            frame_id,
            SpanType::StateTransition,
            "INIT → PLAN".into(),
            "Init".into(),
            None,
            serde_json::json!({"from": "Init", "to": "Plan"}),
        );
        assert!(span.ended_at.is_none());
        span.finish();
        assert!(span.ended_at.is_some());
        assert!(span.duration_ms.is_some());
    }

    #[test]
    fn test_trace_writer_creates_file() {
        let (writer, dir) = make_trace_writer();
        let path = writer.path().to_path_buf();
        drop(writer);
        assert!(path.exists());
        // Cleanup
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_trace_writer_record_transition() {
        let (mut writer, dir) = make_trace_writer();
        let span_id = writer.record_transition("INIT", "PLAN", "guard_init_to_plan", true, None).unwrap();
        assert!(!span_id.0.is_nil());

        // Read back the file
        let path = writer.path().to_path_buf();
        writer.flush().unwrap();
        drop(writer);

        let content = fs::read_to_string(&path).unwrap();
        assert!(!content.is_empty());
        let span: Span = serde_json::from_str(content.trim()).unwrap();
        assert_eq!(span.span_type, SpanType::StateTransition);
        assert_eq!(span.name, "INIT → PLAN");

        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_span_type_serialization() {
        let json = serde_json::to_string(&SpanType::StateTransition).unwrap();
        assert_eq!(json, "\"state_transition\"");
        let json = serde_json::to_string(&SpanType::LlmCall).unwrap();
        assert_eq!(json, "\"llm_call\"");
    }
}
