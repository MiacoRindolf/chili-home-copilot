# Authenticated Fable 5 Diagnostic Head-to-Head Pack

- Schema: `chili.fable5-diagnostic-prompt-pack.v1`
- Target model: `claude-fable-5`
- Benchmark id: `fable5-class-diagnostic-blinded-eighth-run-20260712`
- Manifest SHA-256: `711daad31413039796302ad12ce8cf5411ab0421c12c65df4c2b73665aa9581e`
- Case count: **8**

## Instructions

Analyze every incident from the supplied observations only. Separate the earliest causal break from
downstream symptoms, compare competing dimensions, respect explicit safety boundaries, and avoid claims
not supported by public evidence. Return exactly one JSON object with the response schema below. Do not
use Markdown fences, omit cases, add prose outside JSON, or claim that hidden validation was run.

Allowed causal dimensions:

`code, data, clock, state, config, dependency, runtime, test_harness, unknown`

Response schema:

```json
{
  "cases": [
    {
      "baseline_drift": false,
      "case_id": "copy the case id",
      "causal_chain": [
        "earliest break",
        "mechanism",
        "observed effect"
      ],
      "decision": "patch_root_cause|instrument_first|investigate",
      "dimension": "one allowed causal dimension",
      "evidence_ids": [
        "public evidence ids supporting the conclusion"
      ],
      "experiments": [
        {
          "auto_execute": false,
          "dimension": "one allowed causal dimension",
          "experiment_id": "x1",
          "safety": "read_only|isolated|runtime|live"
        }
      ],
      "hypotheses": [
        {
          "claim": "competing causal claim",
          "dimension": "one allowed causal dimension",
          "evidence_ids": [
            "public evidence id"
          ]
        }
      ],
      "reason": "concise causal explanation",
      "retractions": [
        "specific rejected or revised claim"
      ],
      "status": "confirmed|provisional|inconclusive|rejected"
    }
  ],
  "schema": "chili.fable5-diagnostic-response.v1"
}
```

## Cases

### bh8-801

- Public case SHA-256: `b6937dd5866b07e8c8d09452b6305c2c3b85c21233854ff1711db977d6ab03c5`
- Public case path: `cases/bh8-801.json`

```json
{
  "case_id": "bh8-801",
  "constraints": {
    "diagnostic_lenses": [
      "expected_vs_observed",
      "causal_timeline",
      "root_cause_vs_downstream_symptom",
      "safety_boundary",
      "post_change_proof"
    ],
    "minimum_hypothesis_dimensions": 4
  },
  "observations": [
    {
      "dimension": "unknown",
      "discriminating": true,
      "evidence_id": "bh8-801-e01",
      "independence_key": "dashboard_aggregate",
      "kind": "metric",
      "metadata": {
        "load_count_change_percent": 2.8,
        "step_start": "2026-06-03T06:00:00-07:00",
        "tonnage_change_percent": 14.2
      },
      "provenance": "Diversion dashboard daily aggregate export",
      "reliability": 0.96,
      "statement": "At 06:00 on June 3, reported net tonnage stepped up 14.2% while load count rose only 2.8%; the step persisted across all shifts."
    },
    {
      "dimension": "unknown",
      "discriminating": true,
      "evidence_id": "bh8-801-e02",
      "independence_key": "certified_ticket_audit",
      "kind": "artifact",
      "metadata": {
        "affected_trucks": 11,
        "gross_tolerance_kg": 20,
        "tickets_sampled": 64
      },
      "provenance": "Metrology officer's signed-ticket comparison worksheet",
      "reliability": 0.99,
      "statement": "A stratified audit of 64 signed scale tickets found that gross weights in the dashboard matched the certified terminal within 20 kg, but calculated net weights were high only for the eleven newly enrolled trucks."
    },
    {
      "dimension": "unknown",
      "discriminating": true,
      "evidence_id": "bh8-801-e03",
      "independence_key": "vehicle_master_records",
      "kind": "artifact",
      "metadata": {
        "certificate_unit": "kg",
        "record_count": 11,
        "source_unit": "LB"
      },
      "provenance": "Fleet registry export cross-checked against manufacturer tare certificates",
      "reliability": 0.99,
      "statement": "The source-of-record vehicle file lists tare values between 7,950 and 8,420 for those trucks with unit code LB, while each manufacturer's certificate lists the same numeric value in kilograms."
    },
    {
      "dimension": "unknown",
      "discriminating": true,
      "evidence_id": "bh8-801-e04",
      "independence_key": "offline_recalculation",
      "kind": "experiment",
      "metadata": {
        "after_median_error_kg": 14,
        "before_median_error_kg": 4360
      },
      "provenance": "Read-only reconciliation workbook using copied event records",
      "reliability": 0.98,
      "statement": "In an isolated copy of the June 3 records, changing only those eleven unit tags to KG reduced the median net-weight discrepancy from 4,360 kg to 14 kg per load."
    },
    {
      "dimension": "unknown",
      "discriminating": true,
      "evidence_id": "bh8-801-e05",
      "independence_key": "release_attestation",
      "kind": "artifact",
      "provenance": "Release attestation and configuration archive",
      "reliability": 0.97,
      "statement": "Application binaries, conversion tables, and deployment settings have identical signed checksums to the versions used during the preceding six weeks."
    },
    {
      "dimension": "unknown",
      "discriminating": true,
      "evidence_id": "bh8-801-e06",
      "independence_key": "metrology_checks",
      "kind": "artifact",
      "provenance": "Independent scale-maintenance contractor log",
      "reliability": 0.99,
      "statement": "The weighbridge passed its zero, span, and certified test-mass checks on June 2 and June 5, with no adjustment between them."
    },
    {
      "dimension": "unknown",
      "discriminating": true,
      "evidence_id": "bh8-801-e07",
      "independence_key": "settlement_reconciliation",
      "kind": "metric",
      "metadata": {
        "median_difference_kg": 16,
        "unaffected_trucks": 43
      },
      "provenance": "Hauler settlement reconciliation report",
      "reliability": 0.96,
      "statement": "Loads from the 43 previously registered trucks retained a median ticket-to-dashboard difference of 16 kg throughout the incident window."
    },
    {
      "dimension": "unknown",
      "discriminating": false,
      "evidence_id": "bh8-801-e08",
      "independence_key": "change_control",
      "kind": "artifact",
      "metadata": {
        "immutable_records": [
          "signed_scale_tickets",
          "measured_gross_weight"
        ]
      },
      "provenance": "City data steward's approved change record",
      "reliability": 0.98,
      "statement": "The approved safety boundary prohibited edits to signed tickets or measured gross weights; remediation was limited to versioned fleet-master rows, with before-and-after reconciliation and rollback retained."
    },
    {
      "dimension": "unknown",
      "discriminating": true,
      "evidence_id": "bh8-801-e09",
      "independence_key": "source_remediation",
      "kind": "artifact",
      "provenance": "Fleet registry remediation record and validation result",
      "reliability": 0.98,
      "statement": "The fleet data steward corrected the eleven source records and added a producer-side check that rejects tare-unit tags inconsistent with the attached certificate."
    },
    {
      "dimension": "unknown",
      "discriminating": true,
      "evidence_id": "bh8-801-e10",
      "independence_key": "postchange_proof",
      "kind": "experiment",
      "metadata": {
        "loads_observed": 180,
        "remaining_seasonal_change_percent": 3.1,
        "tolerance_kg": 25
      },
      "provenance": "Post-change production reconciliation signed by operations and finance",
      "reliability": 0.99,
      "statement": "For the next 180 loads, dashboard net weights matched signed tickets within the 25 kg operating tolerance; a historical replay removed the 11.1-point overstatement while leaving a genuine 3.1% seasonal tonnage increase."
    }
  ],
  "problem_statement": "A municipal compost program's diversion dashboard began overstating net inbound tonnage after eleven electric collection trucks were added to the contracted fleet. Certified scale tickets, the operations dashboard, and hauler settlement summaries no longer agree, so the city needs a diagnosis that preserves regulatory records and separates a real seasonal volume increase from the defect.",
  "schema": "chili.diagnostic-case.v1"
}
```

### bh8-802

- Public case SHA-256: `001fedbc3737c502914be4b0fbb481f6d2f1e861fe8602be382df3de9e343fc0`
- Public case path: `cases/bh8-802.json`

```json
{
  "case_id": "bh8-802",
  "constraints": {
    "diagnostic_lenses": [
      "expected_vs_observed",
      "causal_timeline",
      "root_cause_vs_downstream_symptom",
      "safety_boundary",
      "post_change_proof"
    ],
    "minimum_hypothesis_dimensions": 4
  },
  "observations": [
    {
      "dimension": "unknown",
      "discriminating": true,
      "evidence_id": "bh8-802-e01",
      "independence_key": "sorter_counters",
      "kind": "metric",
      "metadata": {
        "affected_sorter": "S2",
        "after_percent": 68.4,
        "before_percent": 2.1
      },
      "provenance": "Sorter operations counter export",
      "reliability": 0.98,
      "statement": "Sorter S2's exception-lane rate rose from 2.1% to 68.4% immediately after the 04:30 restart; adjacent sorter S1 remained between 1.8% and 2.4%."
    },
    {
      "dimension": "unknown",
      "discriminating": true,
      "evidence_id": "bh8-802-e02",
      "independence_key": "rfid_reader_trace",
      "kind": "artifact",
      "metadata": {
        "sample_size": 120
      },
      "provenance": "Reader controller diagnostic capture",
      "reliability": 0.97,
      "statement": "RFID reader traces from 120 diverted books contain valid tag frames and catalog identifiers, with signal strength matching successful reads from the prior day."
    },
    {
      "dimension": "unknown",
      "discriminating": true,
      "evidence_id": "bh8-802-e03",
      "independence_key": "mechanical_acceptance",
      "kind": "experiment",
      "provenance": "Facilities maintenance acceptance sheet",
      "reliability": 0.98,
      "statement": "Maintenance staff passed belt-speed, gate-actuation, and bin-presence checks on S2, and ten manually commanded gates moved to the requested positions without hesitation."
    },
    {
      "dimension": "unknown",
      "discriminating": true,
      "evidence_id": "bh8-802-e04",
      "independence_key": "profile_comparison",
      "kind": "artifact",
      "metadata": {
        "active_value": 0,
        "approved_value": 3
      },
      "provenance": "Configuration controller snapshot and approved branch profile",
      "reliability": 0.99,
      "statement": "The active S2 site profile has strip_prefix_chars set to 0; the signed profile approved for that branch and the still-working S1 profile both use 3 for the same catalog namespace."
    },
    {
      "dimension": "unknown",
      "discriminating": true,
      "evidence_id": "bh8-802-e05",
      "independence_key": "deployment_ledger",
      "kind": "artifact",
      "provenance": "Deployment ledger and device inventory attestation",
      "reliability": 0.98,
      "statement": "The restart deployed no application or firmware release; signed executable and library inventories match S1 and the previous S2 run."
    },
    {
      "dimension": "unknown",
      "discriminating": true,
      "evidence_id": "bh8-802-e06",
      "independence_key": "profile_replay",
      "kind": "experiment",
      "metadata": {
        "active_profile_exceptions": 137,
        "approved_profile_exceptions": 4,
        "frames": 200
      },
      "provenance": "Maintenance-lab routing replay",
      "reliability": 0.98,
      "statement": "Replaying 200 archived tag frames against an isolated routing evaluator reproduced 137 exceptions with the active profile and 4 with the signed branch profile; destination bins for the other 196 matched their historical outcomes."
    },
    {
      "dimension": "unknown",
      "discriminating": true,
      "evidence_id": "bh8-802-e07",
      "independence_key": "catalog_and_control_line",
      "kind": "experiment",
      "provenance": "Catalog audit plus circulation supervisor witness log",
      "reliability": 0.96,
      "statement": "The catalog authority reports no bulk identifier changes, and the same sampled books routed normally through S1 during a controlled manual feed."
    },
    {
      "dimension": "unknown",
      "discriminating": false,
      "evidence_id": "bh8-802-e08",
      "independence_key": "safety_authorization",
      "kind": "artifact",
      "provenance": "Library operations change authorization",
      "reliability": 0.99,
      "statement": "The safety boundary kept S2 in single-book maintenance mode with staff at every gate, forbade firmware and catalog writes, and required immediate stop on any destination mismatch."
    },
    {
      "dimension": "unknown",
      "discriminating": true,
      "evidence_id": "bh8-802-e09",
      "independence_key": "postchange_proof",
      "kind": "experiment",
      "metadata": {
        "acceptance_books": 600,
        "followup_days": 7,
        "wrong_bin_events": 0
      },
      "provenance": "Post-change acceptance run and seven-day operations report",
      "reliability": 0.99,
      "statement": "After restoring the signed branch profile and restarting only the routing evaluator, 600 witnessed returns produced a 2.0% exception rate with zero wrong-bin events; the next seven days remained within the established 1.7% to 2.5% band."
    }
  ],
  "problem_statement": "After a planned restart, one branch of a public library's automated return sorter began sending most books to the staffed exception lane. The library must restore routing without risking book damage or silently changing catalog records.",
  "schema": "chili.diagnostic-case.v1"
}
```

### bh8-803

- Public case SHA-256: `596d3d9f9ef2a81de30bc44ba4a6849193c89d8f02cfb216065d879ad6f2961e`
- Public case path: `cases/bh8-803.json`

```json
{
  "case_id": "bh8-803",
  "constraints": {
    "diagnostic_lenses": [
      "expected_vs_observed",
      "causal_timeline",
      "root_cause_vs_downstream_symptom",
      "safety_boundary",
      "post_change_proof"
    ],
    "minimum_hypothesis_dimensions": 4
  },
  "observations": [
    {
      "dimension": "unknown",
      "discriminating": true,
      "evidence_id": "bh8-803-e01",
      "independence_key": "route_metrics",
      "kind": "metric",
      "metadata": {
        "added_sites_percent": 6.2,
        "expected_range_percent": [
          5,
          8
        ],
        "observed_distance_change_percent": 78
      },
      "provenance": "Dispatch planning dashboard and signed expansion estimate",
      "reliability": 0.97,
      "statement": "Published route distance rose 78% on June 18, although the approved zone expansion added 6.2% more treatment sites and the planning estimate was a 5% to 8% distance increase."
    },
    {
      "dimension": "unknown",
      "discriminating": true,
      "evidence_id": "bh8-803-e02",
      "independence_key": "survey_verification",
      "kind": "artifact",
      "metadata": {
        "sites_checked": 30
      },
      "provenance": "Survey-control comparison signed by field supervisors",
      "reliability": 0.99,
      "statement": "Field supervisors verified 30 suspect site coordinates against survey sheets; the stored latitude, longitude, and coordinate reference identifier were correct before optimization."
    },
    {
      "dimension": "unknown",
      "discriminating": true,
      "evidence_id": "bh8-803-e03",
      "independence_key": "package_inventory",
      "kind": "artifact",
      "metadata": {
        "incident_version": "4.8.2",
        "previous_version": "4.8.1"
      },
      "provenance": "Signed runtime package inventory",
      "reliability": 0.99,
      "statement": "The release inventory shows the application binary and route settings unchanged; the overnight package refresh changed GeoTransform from 4.8.1 to 4.8.2."
    },
    {
      "dimension": "unknown",
      "discriminating": true,
      "evidence_id": "bh8-803-e04",
      "independence_key": "standalone_dependency_test",
      "kind": "experiment",
      "metadata": {
        "affected_version": "4.8.2",
        "corrective_version": "4.8.3",
        "tolerance_meters": 2
      },
      "provenance": "Isolated package-compatibility test witnessed by GIS staff",
      "reliability": 0.99,
      "statement": "A standalone transformation of the vendor's reference coordinate swaps axes under 4.8.2, while 4.8.1 and the vendor's corrective 4.8.3 package return the surveyed point within two meters."
    },
    {
      "dimension": "unknown",
      "discriminating": true,
      "evidence_id": "bh8-803-e05",
      "independence_key": "planner_boundary_trace",
      "kind": "artifact",
      "provenance": "Planner boundary trace captured from copied route inputs",
      "reliability": 0.98,
      "statement": "Planner traces show correct surveyed coordinates entering the transformation boundary and axis-reversed projected coordinates leaving it for every implausible waypoint sampled."
    },
    {
      "dimension": "unknown",
      "discriminating": true,
      "evidence_id": "bh8-803-e06",
      "independence_key": "routing_confounder_audit",
      "kind": "artifact",
      "provenance": "Dispatch configuration audit and GIS analyst distance worksheet",
      "reliability": 0.96,
      "statement": "Vehicle constraints, depot locations, and road-closure feeds were unchanged, and a manual distance matrix for twelve sites differed from the incident planner by factors of four to nine."
    },
    {
      "dimension": "unknown",
      "discriminating": true,
      "evidence_id": "bh8-803-e07",
      "independence_key": "vendor_release_note",
      "kind": "artifact",
      "provenance": "Corrective package release note retained in the software archive",
      "reliability": 0.97,
      "statement": "The package vendor's bundled corrective note identifies an axis-order regression in 4.8.2 for the coordinate reference used by the county."
    },
    {
      "dimension": "unknown",
      "discriminating": false,
      "evidence_id": "bh8-803-e08",
      "independence_key": "dispatch_safety_plan",
      "kind": "artifact",
      "provenance": "Vector-control duty manager authorization",
      "reliability": 0.99,
      "statement": "The safety boundary suspended automatic route publication, retained operator-approved paper routes, and allowed package replacement only after coordinate and distance checks in an isolated planner."
    },
    {
      "dimension": "unknown",
      "discriminating": true,
      "evidence_id": "bh8-803-e09",
      "independence_key": "corrective_package_canary",
      "kind": "experiment",
      "provenance": "GIS canary acceptance report",
      "reliability": 0.99,
      "statement": "A canary planner using 4.8.3 produced all 30 surveyed coordinates within two meters and route distances within 1.4% of the independent GIS calculation."
    },
    {
      "dimension": "unknown",
      "discriminating": true,
      "evidence_id": "bh8-803-e10",
      "independence_key": "postchange_proof",
      "kind": "experiment",
      "metadata": {
        "dispatches": 5,
        "remaining_distance_change_percent": 6.4,
        "routes": 42
      },
      "provenance": "Five-dispatch post-change proof report",
      "reliability": 0.99,
      "statement": "After the corrective package was promoted, all 42 routes passed supervisor review for five consecutive dispatches; total distance settled 6.4% above the old-zone baseline, consistent with the real expansion."
    }
  ],
  "problem_statement": "A county mosquito-control route planner began producing implausibly long field routes just as a northern service zone was added. Dispatch needs to distinguish the expected increase from a planning defect before crews are sent to treatment sites.",
  "schema": "chili.diagnostic-case.v1"
}
```

### bh8-804

- Public case SHA-256: `a6ce49b2223ee0ff71904f244339861363de221dc8419409b004c1252ae05233`
- Public case path: `cases/bh8-804.json`

```json
{
  "case_id": "bh8-804",
  "constraints": {
    "diagnostic_lenses": [
      "expected_vs_observed",
      "causal_timeline",
      "root_cause_vs_downstream_symptom",
      "safety_boundary",
      "post_change_proof"
    ],
    "minimum_hypothesis_dimensions": 4
  },
  "observations": [
    {
      "dimension": "unknown",
      "discriminating": true,
      "evidence_id": "bh8-804-e01",
      "independence_key": "labor_metrics",
      "kind": "metric",
      "metadata": {
        "release": "6.14",
        "walking_distance_change_percent": 41
      },
      "provenance": "Warehouse labor analytics export",
      "reliability": 0.96,
      "statement": "Median walking distance per pick wave increased 41% after release 6.14, while cases picked per wave and active volunteer count stayed within 3% of their eight-week medians."
    },
    {
      "dimension": "unknown",
      "discriminating": true,
      "evidence_id": "bh8-804-e02",
      "independence_key": "wave_segmentation",
      "kind": "metric",
      "provenance": "Pick-wave segmentation analysis",
      "reliability": 0.97,
      "statement": "The regression occurs only in waves containing two-digit bay numbers; single-digit-only waves retain their historical path length."
    },
    {
      "dimension": "unknown",
      "discriminating": true,
      "evidence_id": "bh8-804-e03",
      "independence_key": "route_output_comparison",
      "kind": "artifact",
      "provenance": "Handheld pick-list capture compared with facilities map",
      "reliability": 0.99,
      "statement": "For a representative aisle, the generated order is A-1, A-10, A-11, A-2, A-3; the signed warehouse map orders the same bays A-1, A-2, A-3, A-10, A-11."
    },
    {
      "dimension": "unknown",
      "discriminating": true,
      "evidence_id": "bh8-804-e04",
      "independence_key": "warehouse_confounder_audit",
      "kind": "artifact",
      "provenance": "Inventory master audit and facilities walk-through",
      "reliability": 0.97,
      "statement": "Location coordinates, aisle closures, item dimensions, and reservation priorities are unchanged from the prior release, and a physical survey found no blocked travel paths."
    },
    {
      "dimension": "unknown",
      "discriminating": true,
      "evidence_id": "bh8-804-e05",
      "independence_key": "release_change_review",
      "kind": "artifact",
      "provenance": "Signed release review packet",
      "reliability": 0.99,
      "statement": "The 6.14 release diff replaced the segmented bay comparator with a plain text comparator while consolidating two pick-list paths; no associated requirement changed."
    },
    {
      "dimension": "unknown",
      "discriminating": true,
      "evidence_id": "bh8-804-e06",
      "independence_key": "deterministic_reproduction",
      "kind": "experiment",
      "provenance": "Isolated route-order unit reproduction",
      "reliability": 0.99,
      "statement": "A deterministic test with the five mapped bays reproduces the incident ordering under 6.14 and the physical ordering under 6.13; changing data, configuration, and package versions is unnecessary to reproduce it."
    },
    {
      "dimension": "unknown",
      "discriminating": true,
      "evidence_id": "bh8-804-e07",
      "independence_key": "correction_scope",
      "kind": "artifact",
      "provenance": "Remediation review and scope analysis",
      "reliability": 0.98,
      "statement": "The proposed correction restores segmented numeric comparison only for bay labels and adds examples for mixed one- and two-digit bays; allocation and quantity calculations are outside the changed path."
    },
    {
      "dimension": "unknown",
      "discriminating": false,
      "evidence_id": "bh8-804-e08",
      "independence_key": "validation_safety_boundary",
      "kind": "artifact",
      "provenance": "Food bank release authorization",
      "reliability": 0.99,
      "statement": "The safety boundary required read-only route previews, prohibited inventory or reservation writes during validation, and required a supervisor to compare every canary route with the warehouse map."
    },
    {
      "dimension": "unknown",
      "discriminating": true,
      "evidence_id": "bh8-804-e09",
      "independence_key": "postchange_proof",
      "kind": "experiment",
      "metadata": {
        "allocation_differences": 0,
        "followup_days": 10,
        "live_waves": 60,
        "preview_waves": 100
      },
      "provenance": "Post-change route comparison and ten-day labor report",
      "reliability": 0.99,
      "statement": "After the correction, 100 preview waves and 60 supervised live waves followed map order with no allocation differences; median walking distance returned within 1.8% of the eight-week baseline for ten operating days."
    }
  ],
  "problem_statement": "A regional food bank's handheld pick lists began sending volunteers back and forth across warehouse aisles, delaying outbound pantry loads even though order volume and inventory remained normal. The team needs a bounded correction that cannot alter stock quantities or reservation priority.",
  "schema": "chili.diagnostic-case.v1"
}
```

### bh8-805

- Public case SHA-256: `550ac24f6b121a3de7c16eea4bccff7b38b9d52760ad6e9b5b33208e2320ff36`
- Public case path: `cases/bh8-805.json`

```json
{
  "case_id": "bh8-805",
  "constraints": {
    "diagnostic_lenses": [
      "expected_vs_observed",
      "causal_timeline",
      "root_cause_vs_downstream_symptom",
      "safety_boundary",
      "post_change_proof"
    ],
    "minimum_hypothesis_dimensions": 4
  },
  "observations": [
    {
      "dimension": "unknown",
      "discriminating": true,
      "evidence_id": "bh8-805-e01",
      "independence_key": "availability_metrics",
      "kind": "metric",
      "metadata": {
        "additional_unavailable": 37,
        "controller": "C4",
        "incident_time": "2026-06-09T19:42:00-07:00"
      },
      "provenance": "Tool-service availability history",
      "reliability": 0.98,
      "statement": "Unavailable inventory jumped from 9 to 46 at 19:42 during the UPS transfer; all 37 additional items belong to cabinet controller C4."
    },
    {
      "dimension": "unknown",
      "discriminating": true,
      "evidence_id": "bh8-805-e02",
      "independence_key": "controller_event_log",
      "kind": "artifact",
      "metadata": {
        "serials_with_return_ack": 37
      },
      "provenance": "Cabinet controller export signed by clean-room operations",
      "reliability": 0.99,
      "statement": "C4's append-only device log contains door-close and return-sensor acknowledgements for all 37 serial numbers before power stabilized."
    },
    {
      "dimension": "unknown",
      "discriminating": true,
      "evidence_id": "bh8-805-e03",
      "independence_key": "ledger_snapshot",
      "kind": "artifact",
      "metadata": {
        "current_epoch": 882,
        "rows": 37,
        "stale_epoch": 881
      },
      "provenance": "Custody-ledger snapshot retained by the records officer",
      "reliability": 0.99,
      "statement": "A read-only ledger snapshot shows those same items in release_pending with controller epoch 881, while C4 resumed at epoch 882; no other controller has rows from an earlier epoch in that state."
    },
    {
      "dimension": "unknown",
      "discriminating": true,
      "evidence_id": "bh8-805-e04",
      "independence_key": "physical_custody_count",
      "kind": "observation",
      "provenance": "Dual-signature clean-room custody count",
      "reliability": 1.0,
      "statement": "A two-person physical inventory found every affected serial in its assigned locked cradle, and calibration seals were intact."
    },
    {
      "dimension": "unknown",
      "discriminating": true,
      "evidence_id": "bh8-805-e05",
      "independence_key": "unchanged_system_inventory",
      "kind": "artifact",
      "provenance": "Release inventory plus controller operations summary",
      "reliability": 0.97,
      "statement": "Application binaries, cabinet firmware, entitlement rules, and tool-master records are unchanged from the preceding month; controllers C1 through C3 continued normal checkout and return processing."
    },
    {
      "dimension": "unknown",
      "discriminating": true,
      "evidence_id": "bh8-805-e06",
      "independence_key": "snapshot_reconciliation",
      "kind": "experiment",
      "metadata": {
        "active_checkouts_qualified": 0,
        "qualified_rows": 37
      },
      "provenance": "Offline custody reconciliation witnessed by records staff",
      "reliability": 0.99,
      "statement": "Reconciliation against a copied snapshot marked exactly the 37 old-epoch rows releasable when both a matching return acknowledgement and physical-cradle presence were required; no active checkout qualified."
    },
    {
      "dimension": "unknown",
      "discriminating": true,
      "evidence_id": "bh8-805-e07",
      "independence_key": "reservation_audit",
      "kind": "artifact",
      "provenance": "Reservation audit signed by laboratory managers",
      "reliability": 0.97,
      "statement": "Reservation owners and laboratory managers confirmed that none of the affected serials had an open work order, transfer, quarantine, or after-hours loan."
    },
    {
      "dimension": "unknown",
      "discriminating": false,
      "evidence_id": "bh8-805-e08",
      "independence_key": "custody_safety_boundary",
      "kind": "artifact",
      "provenance": "Clean-room custody repair authorization",
      "reliability": 1.0,
      "statement": "The safety boundary required an immutable pre-repair snapshot, serial-by-serial dual signoff, and a targeted transition only for rows satisfying both return and cradle evidence; bulk reset and history deletion were prohibited."
    },
    {
      "dimension": "unknown",
      "discriminating": true,
      "evidence_id": "bh8-805-e09",
      "independence_key": "targeted_state_repair",
      "kind": "artifact",
      "provenance": "Executed custody repair record",
      "reliability": 0.99,
      "statement": "The authorized reconciliation transitioned the 37 qualified rows, retained linked return evidence in the audit trail, and left the nine genuinely unavailable items untouched."
    },
    {
      "dimension": "unknown",
      "discriminating": true,
      "evidence_id": "bh8-805-e10",
      "independence_key": "postchange_proof",
      "kind": "experiment",
      "metadata": {
        "custody_exceptions": 0,
        "followup_days": 7,
        "transactions": 113
      },
      "provenance": "Seven-day post-repair custody audit",
      "reliability": 0.99,
      "statement": "Over the next seven days, 113 checkouts and returns involving the repaired items completed with matching cabinet and ledger records; unavailable inventory remained at its established level and no custody exception was opened."
    }
  ],
  "problem_statement": "Following a brief UPS transfer, a university clean-room tool service showed 37 serialized torque tools as unavailable even though cabinet staff had physically received them. Researchers still need access, but any repair must preserve custody history and must not release an item that is genuinely checked out.",
  "schema": "chili.diagnostic-case.v1"
}
```

### bh8-806

- Public case SHA-256: `2c4edf6700ce24802a2ccb8c209040201588733ec4af7c6011406f1d8e28a5e0`
- Public case path: `cases/bh8-806.json`

```json
{
  "case_id": "bh8-806",
  "constraints": {
    "diagnostic_lenses": [
      "expected_vs_observed",
      "causal_timeline",
      "root_cause_vs_downstream_symptom",
      "safety_boundary",
      "post_change_proof"
    ],
    "minimum_hypothesis_dimensions": 4
  },
  "observations": [
    {
      "dimension": "unknown",
      "discriminating": true,
      "evidence_id": "bh8-806-e01",
      "independence_key": "workload_and_throughput",
      "kind": "metric",
      "metadata": {
        "audio_volume_change_percent": 27.8,
        "backlog_multiplier": 4,
        "throughput_change_percent": -62.6
      },
      "provenance": "Clerk publication dashboard and hearing calendar",
      "reliability": 0.98,
      "statement": "Nightly audio volume increased 27.8% with the budget calendar, while completed audio-minutes per worker-hour fell 62.6% and the 07:00 backlog quadrupled."
    },
    {
      "dimension": "unknown",
      "discriminating": true,
      "evidence_id": "bh8-806-e02",
      "independence_key": "host_scan_timeline",
      "kind": "metric",
      "metadata": {
        "scan_end": "04:15",
        "scan_start": "00:30"
      },
      "provenance": "Independent endpoint compliance schedule joined to worker timing metrics",
      "reliability": 0.97,
      "statement": "The slowdown begins within three minutes of the host compliance scan starting at 00:30 and recovers shortly after that scan exits at 04:15 on each affected night."
    },
    {
      "dimension": "unknown",
      "discriminating": true,
      "evidence_id": "bh8-806-e03",
      "independence_key": "host_resource_telemetry",
      "kind": "metric",
      "metadata": {
        "major_fault_multiplier": 23,
        "memory_pressure_percent": 94,
        "swap_gb_per_minute": 1.8
      },
      "provenance": "Operating-system performance recorder",
      "reliability": 0.99,
      "statement": "Affected hosts sustain memory pressure above 94%, swap activity above 1.8 GB per minute, and a 23-fold increase in major page faults; worker CPU utilization simultaneously falls below 30%."
    },
    {
      "dimension": "unknown",
      "discriminating": true,
      "evidence_id": "bh8-806-e04",
      "independence_key": "worker_profiles",
      "kind": "artifact",
      "provenance": "Worker runtime profiles sampled by platform engineering",
      "reliability": 0.96,
      "statement": "Application profiles attribute the added wall time to page-fault waits rather than decoding, language-model inference, file input, or retry handling, and no new error class appears."
    },
    {
      "dimension": "unknown",
      "discriminating": true,
      "evidence_id": "bh8-806-e05",
      "independence_key": "paired_host_trial",
      "kind": "experiment",
      "metadata": {
        "affected_host_minutes": 91,
        "recording_minutes": 90,
        "reserve_host_minutes": 22
      },
      "provenance": "Controlled paired-host processing trial",
      "reliability": 0.99,
      "statement": "The same 90-minute recording processed in 22 minutes on an isolated reserve host and in 91 minutes on an affected host during the scan, using the same binary, model files, settings, and input checksum."
    },
    {
      "dimension": "unknown",
      "discriminating": true,
      "evidence_id": "bh8-806-e06",
      "independence_key": "release_and_input_audit",
      "kind": "artifact",
      "provenance": "Release archive and independent audio quality report",
      "reliability": 0.97,
      "statement": "Release attestations show no application, model, dependency, or pipeline-setting change during the incident week; source audio quality scores match the prior month."
    },
    {
      "dimension": "unknown",
      "discriminating": true,
      "evidence_id": "bh8-806-e07",
      "independence_key": "runtime_isolation_canary",
      "kind": "experiment",
      "provenance": "Platform canary relocation report",
      "reliability": 0.98,
      "statement": "A one-worker canary moved to a host with reserved memory completed its normal queue during the scan window without swap activity or throughput loss."
    },
    {
      "dimension": "unknown",
      "discriminating": false,
      "evidence_id": "bh8-806-e08",
      "independence_key": "runtime_safety_boundary",
      "kind": "artifact",
      "provenance": "Joint clerk, platform, and security change authorization",
      "reliability": 0.99,
      "statement": "The safety boundary prohibited disabling or weakening compliance scanning; workers could be drained to approved hosts, and scan placement could change only with security operations approval and capacity alarms active."
    },
    {
      "dimension": "unknown",
      "discriminating": true,
      "evidence_id": "bh8-806-e09",
      "independence_key": "runtime_remediation",
      "kind": "artifact",
      "provenance": "Executed platform remediation record",
      "reliability": 0.98,
      "statement": "Platform staff drained caption workers from scan hosts, restored the approved memory-reserved worker pool, and moved the compliance workload to its designated maintenance capacity without changing scan coverage."
    },
    {
      "dimension": "unknown",
      "discriminating": true,
      "evidence_id": "bh8-806-e10",
      "independence_key": "postchange_proof",
      "kind": "experiment",
      "metadata": {
        "followup_nights": 5,
        "remaining_audio_volume_change_percent": 27.6
      },
      "provenance": "Five-night post-change publication report",
      "reliability": 0.99,
      "statement": "For the next five budget-hearing nights, swap remained at zero on worker hosts, throughput returned within 4% of its prior per-worker rate, and all recordings met publication time despite audio volume remaining 27.6% above the old baseline."
    }
  ],
  "problem_statement": "A city clerk's overnight captioning pipeline began missing the morning publication target during budget-hearing season. Recorded audio hours had genuinely increased, but processing capacity fell much further than the workload change explains.",
  "schema": "chili.diagnostic-case.v1"
}
```

### bh8-807

- Public case SHA-256: `a9c18e0d50ae395ae8e1261ff6c295b0f5bada7788ec9dba7e0863ef63de46d7`
- Public case path: `cases/bh8-807.json`

```json
{
  "case_id": "bh8-807",
  "constraints": {
    "diagnostic_lenses": [
      "expected_vs_observed",
      "causal_timeline",
      "root_cause_vs_downstream_symptom",
      "safety_boundary",
      "post_change_proof"
    ],
    "minimum_hypothesis_dimensions": 4
  },
  "observations": [
    {
      "dimension": "unknown",
      "discriminating": true,
      "evidence_id": "bh8-807-e01",
      "independence_key": "audit_ordering_metrics",
      "kind": "metric",
      "metadata": {
        "affected_cycles": 19,
        "affected_gateways": [
          "G7",
          "G12"
        ],
        "lead_seconds_range": [
          240,
          410
        ],
        "total_cycles": 73
      },
      "provenance": "Orchard command audit export",
      "reliability": 0.98,
      "statement": "Gateways G7 and G12 show acknowledgements 240 to 410 seconds earlier than their paired commands in 19 of 73 cycles; the other sixteen gateways show command-before-acknowledgement ordering."
    },
    {
      "dimension": "unknown",
      "discriminating": true,
      "evidence_id": "bh8-807-e02",
      "independence_key": "enclosure_timeline",
      "kind": "artifact",
      "provenance": "Facilities work orders aligned with retained audit logs",
      "reliability": 0.95,
      "statement": "The first inversions appear after enclosure work on May 14, when both affected gateways were relocated under metal weather shields; no inversion is retained before that date."
    },
    {
      "dimension": "unknown",
      "discriminating": true,
      "evidence_id": "bh8-807-e03",
      "independence_key": "sequence_and_receipt_order",
      "kind": "artifact",
      "provenance": "Gateway sequence export compared with central ingest ledger",
      "reliability": 0.99,
      "statement": "Gateway-local sequence counters are monotonic with no gaps, and central receipt order is command then acknowledgement for every inverted pair."
    },
    {
      "dimension": "unknown",
      "discriminating": true,
      "evidence_id": "bh8-807-e04",
      "independence_key": "radio_latency_monitor",
      "kind": "metric",
      "metadata": {
        "maximum_roundtrip_seconds": 1.9
      },
      "provenance": "Independent radio link monitor",
      "reliability": 0.96,
      "statement": "Round-trip telemetry latency for G7 and G12 remains below 1.9 seconds at the affected times, comparable to control gateways and far below the timestamp inversion."
    },
    {
      "dimension": "unknown",
      "discriminating": true,
      "evidence_id": "bh8-807-e05",
      "independence_key": "gateway_health_records",
      "kind": "artifact",
      "provenance": "Agronomy gateway health archive",
      "reliability": 0.95,
      "statement": "The daily health records stopped reporting satellite lock for G7 and G12 after the enclosure work, while power quality and enclosure temperature stayed in range; the health format does not expose current wall-clock offset."
    },
    {
      "dimension": "unknown",
      "discriminating": false,
      "evidence_id": "bh8-807-e06",
      "independence_key": "software_confounder_review",
      "kind": "artifact",
      "provenance": "Gateway fleet attestation and software review note",
      "reliability": 0.94,
      "statement": "Firmware and configuration attestations match unaffected gateways, but the retained logs cannot rule out a defect that serializes one event's wall timestamp into the next record."
    },
    {
      "dimension": "unknown",
      "discriminating": true,
      "evidence_id": "bh8-807-e07",
      "independence_key": "weather_and_actuation_baseline",
      "kind": "metric",
      "metadata": {
        "cycle_count_change_percent": 31.7
      },
      "provenance": "Agronomist frost log and independent electrical current recorder",
      "reliability": 0.97,
      "statement": "Fan-cycle count is 31.7% above the prior four-week baseline because overnight temperatures crossed the agronomic threshold more often; field current sensors indicate each requested fan run occurred."
    },
    {
      "dimension": "unknown",
      "discriminating": true,
      "evidence_id": "bh8-807-e08",
      "independence_key": "evidence_gap_assessment",
      "kind": "observation",
      "provenance": "Incident evidence-gap review",
      "reliability": 0.98,
      "statement": "No retained artifact samples gateway wall time and monotonic time against the same trusted reference, so an offset, intermittent time-source loss, and timestamp serialization remain distinguishable possibilities."
    },
    {
      "dimension": "unknown",
      "discriminating": false,
      "evidence_id": "bh8-807-e09",
      "independence_key": "clock_safety_boundary",
      "kind": "artifact",
      "provenance": "Orchard operations diagnostic authorization",
      "reliability": 1.0,
      "statement": "The safety boundary forbids setting gateway time, restarting gateways, moving shields, or issuing fan commands while frost automation is armed; existing logs and telemetry may be read and exported."
    },
    {
      "dimension": "unknown",
      "discriminating": true,
      "evidence_id": "bh8-807-e10",
      "independence_key": "bounded_next_measurement",
      "kind": "experiment",
      "metadata": {
        "duration_minutes": 20,
        "executed": false,
        "gateway_count": 4,
        "maximum_mb_per_gateway": 5,
        "sample_period_seconds": 1
      },
      "provenance": "Approved but not yet executed diagnostic measurement plan",
      "reliability": 0.99,
      "statement": "The approved next measurement is a read-only 20-minute capture on G7, G12, and two control gateways during ordinary telemetry: once per second record wall time, monotonic time, satellite-lock state, and the trusted receiver timestamp. It sends no control command, makes no clock adjustment, and stops after 20 minutes or 5 MB per gateway."
    }
  ],
  "problem_statement": "Two orchard frost-protection gateways now record fan acknowledgements several minutes before their commands, making the audit trail unusable even though field staff have not observed mistimed fan operation. The event rate also rose during a colder spell, so the team must isolate sequencing from the legitimate workload change before making any active correction.",
  "schema": "chili.diagnostic-case.v1"
}
```

### bh8-808

- Public case SHA-256: `b371340b597b586aee8056491814d3b69d6320a3cc7cc69401c71933cdbcbecd`
- Public case path: `cases/bh8-808.json`

```json
{
  "case_id": "bh8-808",
  "constraints": {
    "diagnostic_lenses": [
      "expected_vs_observed",
      "causal_timeline",
      "root_cause_vs_downstream_symptom",
      "safety_boundary",
      "post_change_proof"
    ],
    "minimum_hypothesis_dimensions": 4
  },
  "observations": [
    {
      "dimension": "unknown",
      "discriminating": true,
      "evidence_id": "bh8-808-e01",
      "independence_key": "drill_and_fleet_metrics",
      "kind": "metric",
      "metadata": {
        "drill_dates": [
          "2026-06-07",
          "2026-06-14"
        ],
        "failed_printers": 6,
        "fleet_size": 48
      },
      "provenance": "District continuity report and print-fleet counter export",
      "reliability": 0.96,
      "statement": "The continuity drill changed from zero failures to six of 48 printers on June 7 and repeated the same six failures on June 14; routine job counters did not show a matching drop."
    },
    {
      "dimension": "unknown",
      "discriminating": true,
      "evidence_id": "bh8-808-e02",
      "independence_key": "firmware_cohort",
      "kind": "artifact",
      "provenance": "Vendor maintenance ledger and device inventory",
      "reliability": 0.98,
      "statement": "All six flagged printers received the same manufacturer firmware maintenance on June 5; twelve printers of the same model on older firmware passed both drills."
    },
    {
      "dimension": "unknown",
      "discriminating": true,
      "evidence_id": "bh8-808-e03",
      "independence_key": "harness_result_logging",
      "kind": "artifact",
      "metadata": {
        "raw_response_retained": false,
        "stored_result": "UNKNOWN_RESPONSE"
      },
      "provenance": "Continuity harness specification and two drill logs",
      "reliability": 0.99,
      "statement": "The drill sends a read-only status request and stores only its normalized result code; for the six failures the stored code is UNKNOWN_RESPONSE, but the raw response bytes are not retained."
    },
    {
      "dimension": "unknown",
      "discriminating": false,
      "evidence_id": "bh8-808-e04",
      "independence_key": "production_job_ledger",
      "kind": "metric",
      "metadata": {
        "accepted_jobs": 286
      },
      "provenance": "Meal-production job ledger and workflow semantics review",
      "reliability": 0.95,
      "statement": "Kitchen operations recorded 286 accepted label jobs on the six printers during the two drill windows, but acceptance is emitted by the spool stage and does not prove that a physical label emerged."
    },
    {
      "dimension": "unknown",
      "discriminating": false,
      "evidence_id": "bh8-808-e05",
      "independence_key": "operator_checklists",
      "kind": "artifact",
      "provenance": "Signed kitchen shift checklists",
      "reliability": 0.92,
      "statement": "Kitchen managers opened no missing-label incident for those dates, but their shift checklist records only exceptions noticed by staff and is not a printer-by-printer proof."
    },
    {
      "dimension": "unknown",
      "discriminating": false,
      "evidence_id": "bh8-808-e06",
      "independence_key": "network_summaries",
      "kind": "metric",
      "provenance": "District network availability summaries",
      "reliability": 0.94,
      "statement": "Five-minute network summaries show no loss or link flap at the affected kitchens, but their resolution cannot exclude a short status-request failure during the drill."
    },
    {
      "dimension": "unknown",
      "discriminating": true,
      "evidence_id": "bh8-808-e07",
      "independence_key": "harness_and_lab_inventory",
      "kind": "artifact",
      "provenance": "Harness release attestation and test-equipment inventory",
      "reliability": 0.97,
      "statement": "The continuity harness, its status parser, and district printer settings were unchanged for 11 weeks; no matching updated printer is available in the test room for a passive reproduction."
    },
    {
      "dimension": "unknown",
      "discriminating": true,
      "evidence_id": "bh8-808-e08",
      "independence_key": "inconclusive_gap_review",
      "kind": "observation",
      "provenance": "Cross-team evidence review",
      "reliability": 0.98,
      "statement": "Without the raw status response or physical-output telemetry, the evidence cannot distinguish a newly valid response the drill cannot interpret from a brief printer or network condition that ordinary counters conceal."
    },
    {
      "dimension": "unknown",
      "discriminating": false,
      "evidence_id": "bh8-808-e09",
      "independence_key": "allergen_workflow_safety",
      "kind": "artifact",
      "provenance": "Nutrition services diagnostic authorization",
      "reliability": 1.0,
      "statement": "The safety boundary forbids sending extra status requests, producing test labels, changing firmware, or altering the production parser; passive observation of the next already-scheduled drill is allowed."
    },
    {
      "dimension": "unknown",
      "discriminating": true,
      "evidence_id": "bh8-808-e10",
      "independence_key": "bounded_next_measurement",
      "kind": "experiment",
      "metadata": {
        "device_count": 4,
        "duration_minutes": 10,
        "executed": false,
        "extra_requests": 0,
        "maximum_mb_per_device": 2
      },
      "provenance": "Approved but not yet executed passive measurement plan",
      "reliability": 0.99,
      "statement": "The approved next measurement is a passive capture of exact request and response bytes, timestamps, and link state during one scheduled drill for two affected and two control printers, capped at ten minutes and 2 MB per device, followed by offline parsing. It adds no probe or print and has not yet run."
    }
  ],
  "problem_statement": "A school district's weekly continuity drill began declaring six kitchen label printers unavailable, while ordinary meal-label jobs show no confirmed interruption. Because extra prints could enter an allergen-controlled workflow, diagnosis must remain passive until the drill result and the production path can be distinguished.",
  "schema": "chili.diagnostic-case.v1"
}
```
