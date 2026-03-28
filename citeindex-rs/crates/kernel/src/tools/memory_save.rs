//! Tool: memory_save — save memory node to memory_index.

use super::{MemoryAccessEntry, ToolContext, ToolError};

pub fn execute(
    params: &serde_json::Value,
    ctx: &mut ToolContext,
) -> Result<serde_json::Value, ToolError> {
    use tantivy::schema::Facet;

    let schema = ctx.memory_index.schema();

    let memory_id = params.get("memory_id").and_then(|v| v.as_str())
        .ok_or_else(|| ToolError::InvalidParams { param: "memory_id".into(), message: "required".into() })?;
    let session_id = params.get("session_id").and_then(|v| v.as_str()).unwrap_or("");
    let title = params.get("title").and_then(|v| v.as_str()).unwrap_or("");
    let description = params.get("description").and_then(|v| v.as_str()).unwrap_or("");
    let content = params.get("content").and_then(|v| v.as_str()).unwrap_or("");
    let hierarchy_path = params.get("hierarchy_path").and_then(|v| v.as_str()).unwrap_or("");
    let merkle_hash = params.get("merkle_hash").and_then(|v| v.as_str()).unwrap_or("");
    let language = params.get("language").and_then(|v| v.as_str()).unwrap_or("en");

    let mut tantivy_doc = tantivy::TantivyDocument::new();

    macro_rules! add {
        ($name:expr, $val:expr) => {
            if let Ok(f) = schema.get_field($name) { tantivy_doc.add_text(f, $val); }
        };
    }

    add!("memory_id", memory_id);
    add!("session_id", session_id);
    add!("title", title);
    add!("description", description);
    add!("content", content);
    add!("merkle_hash", merkle_hash);
    add!(&format!("title_{language}"), title);
    add!(&format!("description_{language}"), description);
    add!(&format!("content_{language}"), content);

    if !hierarchy_path.is_empty() {
        if let Ok(f) = schema.get_field("hierarchy_path") {
            let fp = if hierarchy_path.starts_with('/') { hierarchy_path.to_string() }
                     else { format!("/{hierarchy_path}") };
            tantivy_doc.add_facet(f, Facet::from(&fp));
        }
    }

    if let Ok(f) = schema.get_field("created_at") {
        tantivy_doc.add_date(f, tantivy::DateTime::from_timestamp_secs(chrono::Utc::now().timestamp()));
    }

    let mut writer = ctx.memory_writer.lock()
        .map_err(|e| ToolError::IndexError(format!("lock: {e}")))?;
    writer.add_document(tantivy_doc)
        .map_err(|e| ToolError::IndexError(format!("add: {e}")))?;
    writer.commit()
        .map_err(|e| ToolError::IndexError(format!("commit: {e}")))?;

    let now = chrono::Utc::now().to_rfc3339();
    ctx.memory_access_cache.insert(memory_id.to_string(), MemoryAccessEntry {
        access_count: 0,
        last_accessed: now.clone(),
    });

    Ok(serde_json::json!({"status": "ok", "memory_id": memory_id, "indexed_at": now}))
}
