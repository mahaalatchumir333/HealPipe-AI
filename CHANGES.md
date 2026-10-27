# Changes and evaluation notes

## Completed work

- Added validation-only detector parameter search and validation reports stratified by fault type, duration, and severity. A one-second freeze is reported as inherently ambiguous; validation contained no such examples, so it was not tuned against.
- Recalibrated confidence after selecting detector parameters from validation data.
- Generated a separate seed-303 test injection and evaluated it once. The original test report remains unchanged as the **pre-tuning** baseline.
- Added a deterministic validation benchmark with CSV/JSON output, runtime estimates, bounded pilots, dashboard filters, and coverage/error heatmap. The full report has 189 available cells and 567 scenario repeats at three repeats per cell; one-second freeze cells are included and treated as inherently ambiguous, not tunable.
- Persisted scenario parameters, point-level decisions, series, and summary metrics; added saved-run retrieval and JSONL download.
- Added documented sensor metadata, component-condition context, replay, a validated real-feed adapter contract, configurable channel schema, demo-only upload UI, and an offline Three.js rig with 2D fallback.
- Kept PS4-PS6 and VS1 disabled pending retraining and evaluation. Excluded CE, CP, and SE from supporting inputs.

## Held-out comparison

The original test report is the pre-tuning baseline. The new report uses a fresh injection seed (303) on the held-out test cycles and was evaluated once after validation-only tuning. Values are from generated evaluation reports, not dashboard constants.

| Metric | Pre-tuning test | Fresh-seed test v2 | Change / interpretation |
|---|---:|---:|---|
| Macro F1 | 59.76% | 61.57% | +1.81 percentage points |
| Accepted-repair success within statistical tolerance | 94.53% | 93.57% | -0.96 points; worse |
| Fault-decision coverage | 53.58% | 60.15% | +6.57 points |
| Healthy-point false-positive rate | 1.970% | 1.802% | -0.168 points |
| Untouched cycles with any diagnosis | Not reported in baseline | 143/143 (100%) | v2 indicates a substantial cycle-level false-alarm limitation |
| Healthy points diagnosed as faults | Not reported in baseline | 4,665 | v2 |
| Healthy points modified | Not reported in baseline | 3,341 | v2; no comparable baseline field |

The baseline healthy-point false-positive rate is derived from its confusion matrix. Cycle-level false-positive and unintended-change counts are not present in the baseline report and are not represented as a before/after improvement. The higher coverage and macro F1 do not offset the lower accepted-repair success or the high untouched-cycle alarm rate.

## Validation evidence and generated reports

- `artifacts/evaluation/validation_detector_tuning.json`: search candidates, overall and stratified detector metrics, untouched-cycle results, and the one-second freeze note.
- `artifacts/detector_params.json`: chosen detector parameter version and validation evidence.
- `artifacts/calibration/confidence_report.json`: confidence calibration trained from validation data after tuning.
- `artifacts/evaluation/test_evaluation.json`: preserved pre-tuning baseline.
- `artifacts/evaluation/test_evaluation_v2.json`: one-time fresh-seed evaluation.
- `artifacts/benchmark/benchmark.json` and `benchmark.csv`: validation-only benchmark outputs.

## Verification

Run the complete test suite from the repository root on Windows:

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
```

The latest completed full-suite run passed **16 tests**. Dashboard JavaScript syntax checks (`node --check static\app.js` and `node --check static\rig.js`) and `git diff --check` passed. The full benchmark completed 567 repeats across 189 cells in 648.202 seconds. The dashboard smoke check loaded all 189 benchmark rows and rendered the 3D canvas with 14 clickable sensor buttons.

## Known limitations

- Freeze detection remains weak on validation; a one-second freeze has no independent cross-sensor evidence in the available validation examples.
- Healthy cycles can still receive diagnoses. In v2, every untouched cycle had at least one diagnosis, so cycle-level false-alarm behavior needs further validation before operational use.
- Scenario benchmark cells are synthetic, use a small number of validation-cycle repeats, and cover only the available physical channels in each group.
- The dataset documentation does not give individual sensor locations. Their physical area is therefore “not documented”; rig marker positions are illustrative rather than measured locations. The profile's component values are context only.
- Optional channels have not been enabled, retrained, or evaluated. The real-feed adapter is an interface only; no real acquisition integration is included.
- Telemetry replay is simulated and runs the offline pipeline only after a complete cycle. It is not instantaneous streaming repair.
- Upload checks enforce the configured schema and cycle structure, but the demo upload pathway is not a production ingestion service.

## What this work does not claim

Faults are synthetic and statistically scaled. A 1 Hz sample is a one-second average, not a raw high-rate observation. The tolerance is a statistical proxy, not a manufacturer limit. The rig is illustrative, not CAD and not to scale. Component-condition context is not a failed-component diagnosis. No production readiness, financial savings, field reliability, or real-time acquisition claim is made.
