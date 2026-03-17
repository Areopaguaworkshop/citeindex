//! Python IPC — Rust calls Python pipelines via subprocess with JSON on
//! stdin/stdout.
//!
//! Matches `rust_core_orchestration.yaml → trigger_ingestion/chat/search`
//! with `tool: python_runtime`.

use serde_json::Value;
use std::process::Stdio;
use tokio::process::Command;

/// Result of a Python subprocess call.
#[derive(Debug)]
pub struct PythonResult {
    pub exit_code: i32,
    pub stdout: String,
    pub stderr: String,
    pub json: Option<Value>,
}

/// Call the `citeindex` Python CLI as a subprocess.
pub async fn call_python(
    python_bin: &str,
    args: &[&str],
    stdin_json: Option<&Value>,
) -> anyhow::Result<PythonResult> {
    let mut cmd = Command::new(python_bin);
    cmd.arg("-m").arg("citeindex.cli");
    cmd.args(args);
    cmd.stdout(Stdio::piped());
    cmd.stderr(Stdio::piped());

    if stdin_json.is_some() {
        cmd.stdin(Stdio::piped());
    } else {
        cmd.stdin(Stdio::null());
    }

    let mut child = cmd.spawn()?;

    // Write stdin if provided
    if let (Some(json_data), Some(stdin)) = (stdin_json, child.stdin.as_mut()) {
        use tokio::io::AsyncWriteExt;
        let bytes = serde_json::to_vec(json_data)?;
        stdin.write_all(&bytes).await?;
        drop(child.stdin.take());
    }

    let output = child.wait_with_output().await?;
    let stdout = String::from_utf8_lossy(&output.stdout).to_string();
    let stderr = String::from_utf8_lossy(&output.stderr).to_string();
    let exit_code = output.status.code().unwrap_or(-1);

    let json = serde_json::from_str::<Value>(&stdout).ok();

    Ok(PythonResult {
        exit_code,
        stdout,
        stderr,
        json,
    })
}

/// Trigger ingestion via the Python CLI.
pub async fn trigger_ingestion(
    python_bin: &str,
    input_path: &str,
    corpus_root: &str,
    extra_args: &[&str],
) -> anyhow::Result<PythonResult> {
    let mut args = vec!["ingest", input_path, "--corpus-root", corpus_root];
    args.extend_from_slice(extra_args);
    call_python(python_bin, &args, None).await
}

/// Trigger search via the Python CLI.
pub async fn trigger_search(
    python_bin: &str,
    query: &str,
    corpus_root: &str,
    cite_style: &str,
) -> anyhow::Result<PythonResult> {
    call_python(
        python_bin,
        &["search", query, "--corpus-root", corpus_root, "--cite-style", cite_style],
        None,
    )
    .await
}

/// Trigger single-shot chat via the Python CLI.
pub async fn trigger_chat(
    python_bin: &str,
    prompt: &str,
    corpus_root: &str,
    llm_model: &str,
    thread_id: &str,
) -> anyhow::Result<PythonResult> {
    call_python(
        python_bin,
        &[
            "chat",
            "--prompt",
            prompt,
            "--corpus-root",
            corpus_root,
            "--llm",
            llm_model,
            "--thread",
            thread_id,
        ],
        None,
    )
    .await
}
