//! WeakSignal Escalation Chain — I7_weak_signal_escalation.md
//!
//! Activates when BM25 search results are too weak to provide reliable
//! grounding. Progressively broadens search through 5 escalation steps,
//! each more expensive than the last.

use serde::{Deserialize, Serialize};

use crate::scoring::ScoreFusionWeights;

/// Default BM25 max score threshold for triggering escalation.
pub const DEFAULT_WEAK_SIGNAL_THRESHOLD: f32 = 0.40;

/// Maximum citation expansion hops (Step 3).
pub const DEFAULT_MAX_HOPS: u32 = 3;

/// Maximum documents for deep traversal (Step 5).
pub const DEFAULT_DEEP_LIMIT: u32 = 3;

/// Check if WeakSignal escalation should activate.
pub fn should_escalate(bm25_max_score: f32, threshold: f32) -> bool {
    bm25_max_score < threshold
}

/// The 5 escalation steps.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum EscalationStep {
    /// Step 1: Maximally expand query using synonym tables.
    SynonymExpansion,
    /// Step 2: Shift primary search from document_index to claim_index.
    IndexSwitch,
    /// Step 3: N-hop traversal on citation graph.
    CitationExpansion,
    /// Step 4: Regex-based heading search on document trees.
    StructuralSearch,
    /// Step 5: LLM-powered deep traversal on top candidates.
    DeepTraversal,
}

impl EscalationStep {
    /// Returns true if this step requires an LLM call.
    pub fn requires_llm(&self) -> bool {
        matches!(self, Self::DeepTraversal)
    }

    /// Human-readable name for trace logging.
    pub fn name(&self) -> &'static str {
        match self {
            Self::SynonymExpansion => "Synonym Expansion",
            Self::IndexSwitch => "Index Switch",
            Self::CitationExpansion => "Citation Expansion",
            Self::StructuralSearch => "Structural Search",
            Self::DeepTraversal => "Deep Traversal",
        }
    }

    /// Step number (1-based).
    pub fn number(&self) -> u32 {
        match self {
            Self::SynonymExpansion => 1,
            Self::IndexSwitch => 2,
            Self::CitationExpansion => 3,
            Self::StructuralSearch => 4,
            Self::DeepTraversal => 5,
        }
    }
}

/// All steps in escalation order.
pub const ESCALATION_STEPS: &[EscalationStep] = &[
    EscalationStep::SynonymExpansion,
    EscalationStep::IndexSwitch,
    EscalationStep::CitationExpansion,
    EscalationStep::StructuralSearch,
    EscalationStep::DeepTraversal,
];

/// Result of a single escalation step.
#[derive(Debug, Clone)]
pub struct StepResult {
    pub step: EscalationStep,
    pub bm25_max_before: f32,
    pub bm25_max_after: f32,
    pub results_count: usize,
    pub escalated: bool,
}

/// Result of the full escalation chain.
#[derive(Debug, Clone)]
pub struct EscalationResult {
    /// Steps that were executed.
    pub steps_executed: Vec<StepResult>,
    /// Final best BM25 max score after all steps.
    pub final_bm25_max: f32,
    /// Which step produced the final results (None if no escalation needed).
    pub final_step: Option<EscalationStep>,
    /// Total results found across all steps.
    pub total_results: usize,
    /// Whether LLM deep traversal was triggered (Step 5).
    pub llm_deep_triggered: bool,
    /// Whether the threshold was met by any step.
    pub threshold_met: bool,
}

/// Score fusion weights for WeakSignal retrieval mode (from I4).
pub fn weak_signal_weights() -> ScoreFusionWeights {
    ScoreFusionWeights {
        w_bm25: 0.40,
        w_hierarchy: 0.20,
        w_citation_degree: 0.15,
        w_recency: 0.15,
        w_claim_density: 0.10,
    }
}

/// Configuration for the escalation chain.
#[derive(Debug, Clone)]
pub struct EscalationConfig {
    pub threshold: f32,
    pub max_hops: u32,
    pub deep_limit: u32,
    pub deep_model_tier: String,
}

impl Default for EscalationConfig {
    fn default() -> Self {
        Self {
            threshold: DEFAULT_WEAK_SIGNAL_THRESHOLD,
            max_hops: DEFAULT_MAX_HOPS,
            deep_limit: DEFAULT_DEEP_LIMIT,
            deep_model_tier: "cloud_standard".into(),
        }
    }
}

/// Run the escalation chain.
///
/// This is the orchestrator that tries each step in order, stopping
/// when the BM25 max score exceeds the threshold.
///
/// Phase 3: stub implementation. Individual steps will be implemented
/// when the full tool layer is connected. Returns an EscalationResult
/// showing what would be attempted.
pub fn run_escalation(
    _query: &str,
    initial_bm25_max: f32,
    config: &EscalationConfig,
) -> EscalationResult {
    let current_bm25_max = initial_bm25_max;
    let mut steps_executed = Vec::new();
    let mut final_step = None;
    let mut llm_deep_triggered = false;
    let mut threshold_met = false;

    for &step in ESCALATION_STEPS {
        if current_bm25_max >= config.threshold {
            threshold_met = true;
            break;
        }

        // Phase 3: stub — each step returns the same score (no real escalation).
        // Real implementation will execute the actual search operations.
        let step_result = StepResult {
            step,
            bm25_max_before: current_bm25_max,
            bm25_max_after: current_bm25_max, // stub: no change
            results_count: 0,
            escalated: true,
        };

        if step == EscalationStep::DeepTraversal {
            llm_deep_triggered = true;
        }

        steps_executed.push(step_result);
        final_step = Some(step);
    }

    EscalationResult {
        steps_executed,
        final_bm25_max: current_bm25_max,
        final_step,
        total_results: 0,
        llm_deep_triggered,
        threshold_met,
    }
}

// ── Step 1: Synonym Expansion ────────────────────────────────

/// Expand query terms using synonym tables.
///
/// Phase 3 stub — returns the original query unchanged.
/// Full implementation loads `config/synonyms/*.toml` and expands all terms.
pub fn expand_synonyms(query: &str, _synonyms: &[(String, Vec<String>)]) -> String {
    query.to_string()
}

// ── Step 3: Citation Expansion ───────────────────────────────

/// N-hop citation expansion starting from seed doc_ids.
///
/// Phase 3 stub — returns empty set.
/// Full implementation queries citation_graph table in SQLite.
pub fn citation_expand(
    _seed_doc_ids: &[String],
    _max_hops: u32,
) -> Vec<String> {
    Vec::new()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_should_escalate() {
        assert!(should_escalate(0.30, 0.40));
        assert!(!should_escalate(0.50, 0.40));
        assert!(!should_escalate(0.40, 0.40));
    }

    #[test]
    fn test_escalation_step_properties() {
        assert!(EscalationStep::DeepTraversal.requires_llm());
        assert!(!EscalationStep::SynonymExpansion.requires_llm());
        assert_eq!(EscalationStep::SynonymExpansion.number(), 1);
        assert_eq!(EscalationStep::DeepTraversal.number(), 5);
    }

    #[test]
    fn test_weak_signal_weights_valid() {
        let weights = weak_signal_weights();
        assert!(weights.validate().is_ok());
    }

    #[test]
    fn test_run_escalation_below_threshold() {
        let config = EscalationConfig::default();
        let result = run_escalation("test query", 0.30, &config);
        assert_eq!(result.steps_executed.len(), 5);
        assert!(!result.threshold_met);
        assert!(result.llm_deep_triggered);
    }

    #[test]
    fn test_run_escalation_above_threshold() {
        let config = EscalationConfig::default();
        let result = run_escalation("test query", 0.50, &config);
        assert_eq!(result.steps_executed.len(), 0);
        assert!(result.threshold_met);
        assert!(!result.llm_deep_triggered);
    }

    #[test]
    fn test_expand_synonyms_stub() {
        let result = expand_synonyms("attention mechanism", &[]);
        assert_eq!(result, "attention mechanism");
    }

    #[test]
    fn test_citation_expand_stub() {
        let result = citation_expand(&["doc1".into()], 3);
        assert!(result.is_empty());
    }

    #[test]
    fn test_escalation_config_default() {
        let config = EscalationConfig::default();
        assert_eq!(config.threshold, 0.40);
        assert_eq!(config.max_hops, 3);
        assert_eq!(config.deep_limit, 3);
    }
}
