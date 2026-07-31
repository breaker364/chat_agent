## ADDED Requirements

### Requirement: Public dataset registry
The system SHALL define RAG benchmark datasets in a data-driven registry rather than branching on dataset names in retrieval code. The default registry SHALL include public retrieval and QA dataset adapters for BEIR-compatible datasets, MS MARCO Passage, Natural Questions Open, and HotpotQA where local download and license terms permit use.

#### Scenario: Evaluation suite is loaded
- **WHEN** `knowledge_evaluate` starts with the default suite
- **THEN** the system SHALL load dataset definitions, splits, corpus locations, query fields, relevance labels, answer labels, and metric configuration from registry data

#### Scenario: Dataset is unavailable
- **WHEN** a configured public dataset cannot be downloaded or its license terms require manual setup
- **THEN** the system SHALL mark that dataset as blocked in the evaluation report with the exact reason and SHALL NOT silently treat it as passed

### Requirement: Reproducible dataset acquisition
The system SHALL download or load public benchmark datasets into a configured evaluation cache and SHALL record dataset version, source, checksum when available, split, sample limit, and preprocessing settings for every run.

#### Scenario: Benchmark corpus is cached
- **WHEN** an evaluation dataset has already been acquired with matching version and checksum metadata
- **THEN** the system SHALL reuse the cached copy instead of downloading it again

#### Scenario: Sampled smoke run is requested
- **WHEN** the evaluator is called with a sample limit
- **THEN** the report SHALL identify the run as a smoke run and SHALL NOT overwrite full-suite release baselines

### Requirement: Retrieval benchmark metrics
The system SHALL compute retrieval metrics for each public retrieval dataset, including nDCG@10, Recall@5, Recall@10, Recall@20, MRR@10, MAP, candidate coverage, and per-query failure examples.

#### Scenario: Retrieval benchmark completes
- **WHEN** the evaluator runs a retrieval dataset with relevance judgments
- **THEN** the report SHALL include metric values for lexical baseline, dense retrieval, semantic-chunk hybrid retrieval, and semantic-chunk hybrid-plus-rerank when configured

#### Scenario: A query has no retrieved relevant chunk
- **WHEN** a benchmark query has relevance labels but no relevant chunk appears in the configured candidate depth
- **THEN** the report SHALL include that query in the failure examples with query id, expected document ids, and top retrieved document ids

### Requirement: Answer benchmark metrics
The system SHALL compute answer quality metrics for labeled QA datasets, including exact match, token F1, grounded-answer rate, citation support rate, and refusal accuracy when the dataset or configured negative set supports no-answer evaluation.

#### Scenario: QA benchmark completes
- **WHEN** the evaluator runs a labeled QA dataset
- **THEN** the report SHALL include retrieval context ids, generated answers, gold answers, answer metrics, citation support status, and aggregate pass/fail status

#### Scenario: Generated answer lacks source support
- **WHEN** a generated answer contains a factual claim not supported by retrieved benchmark context
- **THEN** the evaluator SHALL mark the item as citation-unsupported and include it in the failure examples

### Requirement: RAG-specific evaluation metrics
The system SHALL support optional RAGAS-compatible metrics for context precision, context recall, faithfulness, and answer correctness when a judge model or label-compatible evaluator is configured.

#### Scenario: Judge model is configured
- **WHEN** RAGAS-compatible evaluation is enabled with a valid judge model configuration
- **THEN** the report SHALL record the judge model, prompts or metric versions, item scores, aggregate scores, and pass/fail status

#### Scenario: Judge model is not configured
- **WHEN** RAGAS-compatible metrics are requested but no valid judge model is configured
- **THEN** the evaluator SHALL mark those metrics as blocked and SHALL still run deterministic retrieval and QA metrics

### Requirement: Benchmark quality gates
The system SHALL evaluate configured quality gates and SHALL fail the run when required gates are not met. The default release gate SHALL require the structure-first semantic chunking plus hybrid retrieval pipeline to meet or beat the lexical baseline on average nDCG@10 across the configured public retrieval suite, with no individual dataset below 95% of lexical baseline nDCG@10 unless the report records an explicit waiver.

#### Scenario: Candidate pipeline passes gates
- **WHEN** all configured retrieval, answer, citation, and operational gates are satisfied
- **THEN** the report SHALL mark the benchmark run as passed and SHALL write a machine-readable summary for regression comparison

#### Scenario: Candidate pipeline fails a gate
- **WHEN** any required metric is below its configured threshold and no waiver is recorded
- **THEN** the report SHALL mark the benchmark run as failed and SHALL include the failed metric, threshold, observed value, and affected dataset

### Requirement: Regression baseline management
The system SHALL store benchmark baselines separately from personal knowledge data and SHALL compare new runs against the selected baseline without modifying the baseline unless explicitly requested.

#### Scenario: New evaluation run has an existing baseline
- **WHEN** `knowledge_evaluate` is called with a baseline id
- **THEN** the evaluator SHALL compare current metrics to that baseline and report improvements, regressions, and unchanged metrics

#### Scenario: Baseline update is requested
- **WHEN** a caller explicitly requests baseline promotion after a passing full-suite run
- **THEN** the system SHALL store the new baseline with run id, configuration hash, dataset metadata, and metric summary

### Requirement: Evaluation report artifacts
The system SHALL write evaluation artifacts to a configured report directory, including a human-readable Markdown report, machine-readable JSON summary, per-dataset metric files, and bounded failure examples.

#### Scenario: Evaluation finishes
- **WHEN** an evaluation run completes, fails, or is blocked
- **THEN** the system SHALL return the report paths and SHALL write artifacts containing run status, configuration, dataset metadata, metrics, gates, failures, waivers, and environment details

### Requirement: CI-compatible smoke verification
The system SHALL provide a small deterministic smoke suite that can run without downloading large corpora and SHALL validate evaluator wiring, metric computation, report generation, and gate failure behavior.

#### Scenario: Smoke tests run in CI
- **WHEN** the test suite runs without external dataset downloads
- **THEN** the evaluator smoke tests SHALL pass using fixture corpora and SHALL verify that failing metrics produce a failed report
