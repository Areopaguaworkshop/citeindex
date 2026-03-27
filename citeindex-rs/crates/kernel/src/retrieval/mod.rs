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
    SynonymExpansion,
    IndexSwitch,
    CitationExpansion,
    StructuralSearch,
    DeepTraversal,
}

impl EscalationStep {
    pub fn requires_llm(&self) -> bool {
        matches!(self, Self::DeepTraversal)
    }

    pub fn name(&self) -> &'static str {
        match self {
            Self::SynonymExpansion => "Synonym Expansion",
            Self::IndexSwitch => "Index Switch",
            Self::CitationExpansion => "Citation Expansion",
            Self::StructuralSearch => "Structural Search",
            Self::DeepTraversal => "Deep Traversal",
        }
    }

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
    pub steps_executed: Vec<StepResult>,
    pub final_bm25_max: f32,
    pub final_step: Option<EscalationStep>,
    pub total_results: usize,
    pub llm_deep_triggered: bool,
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
/// Tries each step in order, stopping when BM25 max score exceeds threshold.
/// Phase 3: stub implementation — individual steps return unchanged scores.
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

        let step_result = StepResult {
            step,
            bm25_max_before: current_bm25_max,
            bm25_max_after: current_bm25_max,
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

/// Expand query terms using synonym tables (Step 1 stub).
pub fn expand_synonyms(query: &str, _synonyms: &[(String, Vec<String>)]) -> String {
    query.to_string()
}

/// N-hop citation expansion (Step 3 stub).
pub fn citation_expand(_seed_doc_ids: &[String], _max_hops: u32) -> Vec<String> {
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
        assert_eq!(expand_synonyms("attention mechanism", &[]), "attention mechanism");
    }

    #[test]
    fn test_citation_expand_stub() {
        assert!(citation_expand(&["doc1".into()], 3).is_empty());
    }

    #[test]
    fn test_escalation_config_default() {
        let config = EscalationConfig::default();
        assert_eq!(config.threshold, 0.40);
        assert_eq!(config.max_hops, 3);
        assert_eq!(config.deep_limit, 3);
    }
}
