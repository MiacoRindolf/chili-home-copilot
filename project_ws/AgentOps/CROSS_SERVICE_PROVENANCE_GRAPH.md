# CHILI Cross-Service Provenance Graph

Date: 2026-07-11

## Purpose

Real incidents often show healthy source coverage and an empty sink at the same time. A flat classifier can blame data loss even when the first break is a stalled consumer. CHILI now builds a bounded lineage graph over evidence, components, flow edges, and privacy-minimized correlation identity.

## Evidence Contract

Observations may provide:

- `service_id`, `producer_id`, `consumer_id`, and `sink_id`
- `edge_from`, `edge_to`, `expected_edge_state`, and `actual_edge_state`
- `artifact_hash`
- raw `correlation_id` or `correlation_ids` at ingestion only

Raw correlation values are immediately converted to a 20-character SHA-256 fingerprint. The normalized case and graph retain fingerprints, not raw identifiers.

## Graph Semantics

`build_provenance_graph` creates:

- component and evidence nodes
- explicit producer/consumer/sink flow edges
- causal-evidence edges
- ordered correlation-sequence edges
- independence-key clusters so duplicate observations do not masquerade as independent proof
- per-component artifact-hash divergence
- source/runtime revision parity metadata
- the earliest explicit broken flow edge

When a producer edge is healthy and the first downstream edge stalls, the graph classifies the incident as `consumer_starvation`. A later missing sink row remains evidence, but it cannot displace the grounded first broken edge.

## Runtime Wiring

Bounded `log_search` probes parse request, correlation, trace, event, run, and job IDs from returned matches. Only hashed fingerprints and bounded service names derived from approved log paths enter the graph. Probe output remains subject to the existing file, tail, byte, match, and output caps.

## Safety

- No raw-command probe was added.
- No network, container, database, or runtime mutation was added.
- Database evidence remains schema or aggregate only.
- Raw correlation IDs are not graph keys.
- A graph-selected root that supersedes the model remains provisional unless independently confirmed.

## Validation

- A shuffled scanner → queue → consumer → sink incident finds the queue-to-consumer stall before the missing sink row.
- Healthy producer delivery plus a stalled consumer classifies as `consumer_starvation`.
- Three observations sharing one request form one ordered hashed-correlation group.
- Shared `independence_key` evidence is clustered instead of over-counted.
- A bounded log probe emits a service node and the expected SHA-256 correlation fingerprint.
- Focused diagnostic, probe, runtime-evidence, and service suite: **168 passed**.
- Broad routing, evidence, repair, identity, and autonomy suite: **303 passed**.
- Disclosed eight-case heuristic regression: **100/100**.

External tracing systems, automatic role inference for arbitrary log formats, and broad Fable 5 parity remain outside the current proof.
