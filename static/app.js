let activePoints = [];
let activeRunId = null;
let activeRigData = null;
let benchmarkReport = null;
let activeReplay = null;

async function getJson(url) {
    const token = localStorage.getItem('healpipeToken');
    const response = await fetch(url, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
    });
    if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        const error = new Error(body.detail || `Request failed: ${response.status}`);
        error.status = response.status;
        throw error;
    }
    return response.json();
}

function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, character => ({
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#39;',
    })[character]);
}

function renderOverview(metrics, report = null) {
    const cards = document.getElementById('overviewMetrics') || document.getElementById('overviewCards');
    if (!cards) return;
    const statusCounts = report?.status_counts || {};
    const statusTotal = Number(report?.points || 0);
    const statusCard = status => {
        const count = Number(statusCounts[status] || 0);
        const share = statusTotal ? (count / statusTotal * 100).toFixed(1) : '0.0';
        return [`${status} values`, `${count.toLocaleString()} (${share}%)`];
    };
    const items = report ? [
        ['Dataset partition', report.partition],
        ['Complete cycles', Number(report.cycles || 0).toLocaleString()],
        ['Telemetry duration', `${Number(report.telemetry_seconds || 0).toLocaleString()} s`],
        ['Sensor readings', Number(report.points || 0).toLocaleString()],
        ['Processing latency', `${Number(report.duration_seconds || 0).toFixed(2)} s`],
        ['Average repair confidence', report.mean_modified_confidence === null ? '—' : `${Number(report.mean_modified_confidence || 0).toFixed(1)}%`],
        ['Diagnosed faults', Object.entries(report.fault_distribution || {}).map(([fault, count]) => `${fault}: ${Number(count).toLocaleString()}`).join(' · ') || 'None'],
        statusCard('Measured'),
        statusCard('Estimated'),
        statusCard('Corrected'),
        statusCard('Unresolved'),
    ] : [
        ['Total cycles', Number(metrics.total_cycles || 0).toLocaleString()],
        ['Sensor channels', metrics.channels || 0],
        ['Missing readings', Number(metrics.missing_points || 0).toLocaleString()],
        ['Missing rate', `${metrics.missing_rate || 0}%`],
        ['Dataset partition', metrics.partition || 'validation'],
    ];
    cards.innerHTML = items.map(([label, value]) => `
        <div class="stat-box"><label>${label}</label><strong>${escapeHtml(value)}</strong></div>
    `).join('');
}

function renderRunsTable(rows) {
    const target = document.getElementById('runsTable');
    if (!target) return;
    if (!rows?.length) {
        target.innerHTML = '<div class="run-item"><span>No runs yet</span></div>';
        return;
    }
    target.innerHTML = rows.map(row => `
        <a class="run-item" href="/dashboard?run=${encodeURIComponent(row.id)}">
            <div><strong>${escapeHtml(row.name)}</strong><br />
            <small>${escapeHtml(row.partition)} • ${escapeHtml(row.created_at || 'recent')}</small></div>
            <span class="badge">${escapeHtml(row.status)}</span>
        </a>
    `).join('');
}

function renderChart(series) {
    const svg = document.getElementById('chartSvg');
    if (!svg || !series?.observed) return;
    activePoints = series.points || series.observed.map((value, index) => ({
        cycle_id: null,
        second_in_cycle: index,
        sensor_id: series.sensor,
        observed_value: value,
        reconstructed_value: series.repaired?.[index],
        reference_value: series.reference?.[index],
        fault_type: series.faults?.[index] || 'healthy',
        status: series.statuses?.[index] || (series.faults?.[index] === 'healthy' ? 'Measured' : 'Corrected'),
        confidence: series.confidence?.[index] ?? 100,
        acceptance_decision: series.points?.[index]?.acceptance_decision || 'retained',
        decision_reason: series.points?.[index]?.decision_reason || '',
        confidence_method: series.points?.[index]?.confidence_method || '',
        diagnosis: series.points?.[index]?.diagnosis || '',
        reasoning: series.points?.[index]?.reasoning || '',
        repair_method: series.points?.[index]?.repair_method || '',
        supporting_sensors: series.points?.[index]?.supporting_sensors || [],
    }));

    const width = 760;
    const height = 320;
    const padding = 42;
    const values = [
        ...(series.reference || []),
        ...series.observed,
        ...(series.repaired || []),
    ].filter(value => value !== null && value !== undefined).map(Number).filter(Number.isFinite);
    if (!values.length) return;
    const maxValue = Math.max(...values);
    const minValue = Math.min(...values);
    const span = Math.max(maxValue - minValue, 1e-8);
    const xAt = index => padding + index / Math.max(series.observed.length - 1, 1) * (width - padding * 2);
    const yAt = value => height - padding - (Number(value) - minValue) / span * (height - padding * 2);
    const createLine = (line, color, strokeWidth, dash = '', status = null) => {
        const segments = [];
        let segment = [];
        const addSegment = () => {
            if (segment.length > 1) segments.push(segment.join(' '));
            segment = [];
        };
        (line || []).forEach((value, index) => {
            const finite = value !== null && value !== undefined && Number.isFinite(Number(value));
            const statusMatches = status === null || activePoints[index]?.status === status;
            if (!finite || !statusMatches) {
                addSegment();
                return;
            }
            segment.push(`${xAt(index)},${yAt(value)}`);
        });
        addSegment();
        const dashAttribute = dash ? `stroke-dasharray="${dash}"` : '';
        return segments.map(points => `<polyline points="${points}" fill="none" stroke="${color}" stroke-width="${strokeWidth}" ${dashAttribute} />`).join('');
    };

    const markerRows = activePoints.map((point, index) => {
        const observedIsFinite = point.observed_value !== null
            && point.observed_value !== undefined
            && Number.isFinite(Number(point.observed_value));
        const value = point.reconstructed_value ?? point.observed_value ?? (point.status === 'Unresolved' ? minValue : null);
        if (value === null || value === undefined || !Number.isFinite(Number(value))) return '';
        const color = point.status === 'Estimated' ? '#c49aff'
            : point.status === 'Corrected' ? '#ff7d7d'
                : point.status === 'Unresolved' ? '#ff5e67' : '#7ef0b6';
        const hollow = point.status === 'Unresolved' ? 'fill="none"' : `fill="${color}"`;
        return `<circle class="chart-point" data-point-index="${index}" tabindex="0" role="button"
            aria-label="${escapeHtml(point.sensor_id)} ${escapeHtml(point.fault_type)} ${escapeHtml(point.status)} at second ${escapeHtml(point.second_in_cycle)}"
            cx="${xAt(index)}" cy="${yAt(value)}" r="${point.status === 'Measured' ? 2.5 : 4}"
            ${hollow} stroke="${color}" ${!observedIsFinite && point.status === 'Unresolved' ? 'stroke-width="2.5"' : ''}>
            <title>${escapeHtml(point.fault_type)} • ${escapeHtml(point.status)} • ${Number(point.confidence).toFixed(1)}%</title>
        </circle>`;
    }).join('');

    svg.innerHTML = `
        ${series.reference ? createLine(series.reference, '#c49aff', 2, '6 4') : ''}
        ${createLine(series.observed, '#8ea4bb', 1.8)}
        ${createLine(series.repaired, '#62d0ff', 2.5, '', 'Measured')}
        ${createLine(series.repaired, '#c49aff', 2.5, '5 3', 'Estimated')}
        ${createLine(series.repaired, '#ff7d7d', 2.5, '5 3', 'Corrected')}
        ${markerRows}
    `;
    const heading = document.getElementById('chartHeading');
    if (heading) heading.textContent = `${series.sensor} trend • cycle ${series.selected_cycle ?? activePoints[0]?.cycle_id ?? '—'}`;
    svg.onclick = event => {
        const marker = event.target.closest('[data-point-index]');
        if (marker) inspectPoint(Number(marker.dataset.pointIndex));
    };
    svg.onkeydown = event => {
        if (event.key !== 'Enter' && event.key !== ' ') return;
        const marker = event.target.closest('[data-point-index]');
        if (marker) {
            event.preventDefault();
            inspectPoint(Number(marker.dataset.pointIndex));
        }
    };
    window.renderSensorRig?.(activeRigData, series.sensor);
}

function renderSummary(run) {
    const target = document.getElementById('runSummary');
    if (!target) return;
    const metrics = run.metrics || {};
    target.innerHTML = `
        <div class="stat-box"><label>Run</label><strong>${escapeHtml(run.name)}</strong></div>
        <div class="stat-box"><label>Average repair confidence</label><strong>${metrics.success_probability ?? '--'}%</strong></div>
        <div class="stat-box"><label>Modified values</label><strong>${metrics.modified_count ?? 0}</strong></div>
        <div class="stat-box"><label>Fault diagnoses</label><strong>${escapeHtml(Object.entries(metrics.fault_counts || {}).map(([fault, count]) => `${fault}: ${count}`).join(' · ') || 'None')}</strong></div>
        <div class="stat-box"><label>Status breakdown</label><strong>${escapeHtml(Object.entries(metrics.status_counts || {}).map(([status, count]) => `${status}: ${count}`).join(' · ') || '—')}</strong></div>
        <div class="stat-box"><label>Acceptance / refusal</label><strong>${escapeHtml(Object.entries(metrics.decision_counts || {}).map(([decision, count]) => `${decision}: ${count}`).join(' · ') || '—')}</strong></div>
    `;
}

function renderDecisionMetrics(report) {
    const target = document.getElementById('decisionMetrics');
    if (!target) return;
    const decisions = report.decision_metrics || {};
    const items = [
        ['Accepted repairs', decisions.accepted_repairs],
        ['Accepted repairs within tolerance', decisions.accepted_repair_success_rate === null || decisions.accepted_repair_success_rate === undefined
            ? 'Not available' : `${Number(decisions.accepted_repair_success_rate).toFixed(1)}%`],
        ['Refused / unresolved', decisions.refused_repairs],
        ['Fault decision coverage', decisions.fault_decision_coverage === undefined
            ? 'Not available' : `${Number(decisions.fault_decision_coverage).toFixed(1)}%`],
    ];
    target.innerHTML = items.map(([label, value]) => `
        <div class="stat-box"><label>${escapeHtml(label)}</label><strong>${escapeHtml(value ?? 'Not available')}</strong></div>
    `).join('');
}

function renderScenarioResult(result) {
    const target = document.getElementById('scenarioResults');
    if (!target) return;
    const scenario = result.scenario;
    const metrics = result.metrics;
    const items = [
        ['Diagnosed correctly', `${metrics.correctly_diagnosed_points} / ${metrics.injected_points}`],
        ['Diagnosis precision / recall / F1', `${metrics.diagnosis_precision}% / ${metrics.diagnosis_recall}% / ${metrics.diagnosis_f1}%`],
        ['Accepted repairs', metrics.accepted_repairs],
        ['Refused repairs', metrics.refused_repairs],
        ['Mean accepted confidence', metrics.mean_accepted_confidence === null || metrics.mean_accepted_confidence === undefined
            ? 'No repairs accepted' : `${metrics.mean_accepted_confidence}%`],
        ['Accepted repairs within tolerance', metrics.accepted_repairs_within_tolerance],
        ['Accepted repair success rate', metrics.accepted_repair_success_rate === null
            ? 'No repairs accepted' : `${metrics.accepted_repair_success_rate}%`],
        ['False alarms outside injected region', metrics.false_alarms_outside_injected_region],
        ['Statuses across analyzed cycle', Object.entries(metrics.status_counts)
            .map(([status, count]) => `${status}: ${count}`).join(' · ')],
    ];
    const auditRows = (result.fault_audit || []).map(point => `
        <tr>
            <td>${escapeHtml(point.sensor_id)}</td>
            <td>${escapeHtml(point.location_area || 'not documented')}</td>
            <td>${escapeHtml(point.second_in_cycle)}</td>
            <td>${escapeHtml(point.fault_type)}</td>
            <td>${escapeHtml(point.status)}</td>
            <td>${escapeHtml(point.acceptance_decision)}</td>
            <td>${Number(point.confidence).toFixed(1)}%</td>
            <td>${escapeHtml(point.observed_value ?? 'missing')}</td>
            <td>${escapeHtml(point.reconstructed_value ?? 'withheld')}</td>
            <td>${escapeHtml(point.diagnosis)}</td>
            <td>${escapeHtml(point.process_event_warning || '')}</td>
        </tr>
    `).join('');
    const sensorContext = scenario.sensor_context || {};
    const locationContext = scenario.sensors.map(sensor => {
        const context = sensorContext[sensor] || {};
        const condition = context.component_context;
        const conditionText = condition
            ? `Documented cycle condition (context only): cooler ${condition.cooler}, valve ${condition.valve}, pump ${condition.pump}, accumulator ${condition.accumulator}.`
            : 'No cycle condition context available.';
        return `<li><strong>${escapeHtml(sensor)}</strong> — area ${escapeHtml(context.location_area || 'not documented')}. ${escapeHtml(conditionText)}</li>`;
    }).join('');
    target.innerHTML = `
        <p><strong>${escapeHtml(scenario.fault_type)}</strong> on ${escapeHtml(scenario.sensors.join(', '))},
            cycle ${escapeHtml(scenario.cycle_id)}, second ${escapeHtml(scenario.start_second)},
            duration ${escapeHtml(scenario.duration_seconds)}s
            ${scenario.severity_sensor_scales === null ? '' : `, severity ${escapeHtml(scenario.severity_sensor_scales)} sensor scales`}.
            ${scenario.deterministic ? 'Deterministic scenario.' : ''}</p>
        <div class="card-grid">${items.map(([label, value]) => `
            <div class="stat-box"><label>${escapeHtml(label)}</label><strong>${escapeHtml(value)}</strong></div>
        `).join('')}</div>
        <p class="muted">${escapeHtml(metrics.tolerance_definition)} False alarms count diagnoses outside the injected sensor/time region.</p>
        <p><strong>Location/context:</strong> The data fault is located at the sensor(s) listed below; this is a signal-quality finding, not a diagnosis of a failed component.</p>
        <ul>${locationContext}</ul>
        <div class="audit-table"><table><thead><tr>
            <th>Sensor</th><th>Area</th><th>Second</th><th>Diagnosis</th><th>Status</th><th>Decision</th>
            <th>Confidence</th><th>Observed</th><th>Output</th><th>Reason</th><th>Process context</th>
        </tr></thead><tbody>${auditRows}</tbody></table></div>
    `;
}

async function runFaultScenario() {
    const button = document.getElementById('runScenarioButton');
    const target = document.getElementById('scenarioResults');
    if (!button || !target) return;
    button.disabled = true;
    button.textContent = 'Evaluating…';
    target.innerHTML = '<p>Running the selected deterministic scenario through diagnosis, reconstruction, calibration, and decision scoring…</p>';
    try {
        const cycleId = document.getElementById('cycleSelect')?.value;
        const payload = {
            partition: document.getElementById('evaluationPartition')?.value || 'validation',
            sensor_id: document.getElementById('sensorSelect')?.value || 'PS1',
            cycle_id: cycleId ? Number(cycleId) : null,
            fault_type: document.getElementById('scenarioFaultType').value,
            severity: Number(document.getElementById('scenarioSeverity').value),
            duration_seconds: Number(document.getElementById('scenarioDuration').value),
            start_second: Number(document.getElementById('scenarioStart').value),
            affected_channels: Number(document.getElementById('scenarioChannels').value),
        };
        const response = await fetch('/api/scenarios/evaluate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || `Scenario failed: ${response.status}`);
        renderScenarioResult(data);
        if (activeRigData?.sensors) {
            activeRigData = {
                ...activeRigData,
                sensors: {
                    ...activeRigData.sensors,
                    [data.series.sensor]: data.series.points,
                },
            };
        }
        activeRunId = null;
        const scenarioDownload = document.getElementById('downloadScenarioButton');
        if (scenarioDownload) {
            scenarioDownload.disabled = !data.run_id;
            scenarioDownload.dataset.runId = String(data.run_id || '');
        }
        renderSummary({
            name: `${data.scenario.fault_type} scenario`,
            metrics: {
                success_probability: data.metrics.mean_accepted_confidence ?? '--',
                modified_count: data.metrics.accepted_repairs,
                fault_counts: { [data.scenario.fault_type]: data.metrics.injected_points },
                status_counts: data.metrics.status_counts,
                decision_counts: {
                    accepted: data.metrics.accepted_repairs,
                    refused: data.metrics.refused_repairs,
                },
            },
        });
        renderChart(data.series);
        renderAuditTable(data.series.points.filter(point => point.fault_type !== 'healthy'));
        renderUnresolvedPoints(data.series.points);
        const download = document.getElementById('downloadAuditButton');
        if (download) download.disabled = true;
        document.getElementById('sensor-explorer')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    } catch (error) {
        target.innerHTML = `<p role="alert">${escapeHtml(error.message)}</p>`;
    } finally {
        button.disabled = false;
        button.textContent = 'Run scenario';
    }
}

function inspectPoint(index) {
    const point = activePoints[index];
    if (!point) return;
    const inspector = document.getElementById('pointInspector');
    if (!inspector) return;
    inspector.innerHTML = `
        <h3>Point-level inspector</h3>
        <div class="card-grid">
            <div class="stat-box"><label>Coordinate</label><strong>Cycle ${escapeHtml(point.cycle_id ?? '—')}, second ${escapeHtml(point.second_in_cycle)}</strong></div>
            <div class="stat-box"><label>Sensor / status</label><strong>${escapeHtml(point.sensor_id)} / ${escapeHtml(point.status)}</strong></div>
            <div class="stat-box"><label>Observed → output</label><strong>${escapeHtml(point.observed_value ?? 'missing')} → ${escapeHtml(point.reconstructed_value ?? 'unresolved')}</strong></div>
            <div class="stat-box"><label>Diagnosis / confidence</label><strong>${escapeHtml(point.fault_type)} / ${Number(point.confidence).toFixed(1)}%</strong></div>
            <div class="stat-box"><label>Decision</label><strong>${escapeHtml(point.acceptance_decision || 'not recorded')}</strong></div>
        </div>
        <p><strong>Location finding:</strong> ${escapeHtml(point.location_statement || `data fault located at sensor ${point.sensor_id} (area ${point.location_area || 'not documented'}); this is a signal-quality finding, not a diagnosis of a failed component`)}</p>
        ${point.process_event_warning ? `<p role="status"><strong>${escapeHtml(point.process_event_warning)}</strong></p>` : ''}
        ${point.component_context ? `<p><strong>Documented component condition (context only):</strong> cooler ${escapeHtml(point.component_context.cooler)}, valve ${escapeHtml(point.component_context.valve)}, pump ${escapeHtml(point.component_context.pump)}, accumulator ${escapeHtml(point.component_context.accumulator)}. This cycle profile is context only, not a fault diagnosis.</p>` : '<p class="muted">Documented component condition is unavailable for this point.</p>'}
        <p><strong>Decision rationale:</strong> ${escapeHtml(point.decision_reason || 'Decision metadata unavailable.')}</p>
        <p><strong>Confidence scoring:</strong> ${escapeHtml(point.confidence_method || 'scoring method unavailable')}</p>
        <p><strong>Method:</strong> ${escapeHtml(point.repair_method || 'measured_input')}</p>
        <p><strong>Supporting channels:</strong> ${escapeHtml((point.supporting_sensors || []).join(', ') || 'None available')}</p>
        <p><strong>Reason:</strong> ${escapeHtml(point.reasoning || point.diagnosis || 'No fault evidence; original measurement retained.')}</p>
        ${point.reference_value !== undefined ? `<p><strong>Reference (evaluation only):</strong> ${escapeHtml(point.reference_value)}</p>` : ''}
        <details><summary>Point audit JSON</summary><pre>${escapeHtml(JSON.stringify(point, null, 2))}</pre></details>
    `;
}

function renderAuditTable(events) {
    const target = document.getElementById('auditTable');
    if (!target) return;
    const selected = (events || []).filter(event => event.fault_type !== 'healthy');
    if (!selected.length) {
        target.innerHTML = '<p>No flagged values in this series.</p>';
        return;
    }
    const rows = selected.slice(0, 100).map(event => {
        const index = activePoints.findIndex(point => (
            point.cycle_id === event.cycle_id
            && point.second_in_cycle === event.second_in_cycle
            && point.sensor_id === event.sensor_id
        ));
        return `
            <tr data-point-index="${index}" tabindex="0">
                <td>${escapeHtml(event.cycle_id)}</td>
                <td>${escapeHtml(event.second_in_cycle)}</td>
                <td>${escapeHtml(event.sensor_id)}</td>
                <td>${escapeHtml(event.fault_type)}</td>
                <td>${escapeHtml(event.status)}</td>
                <td>${escapeHtml(event.acceptance_decision || 'not recorded')}</td>
                <td>${escapeHtml(event.observed_value ?? 'missing')}</td>
                <td>${escapeHtml(event.reconstructed_value ?? '—')}</td>
                <td>${Number(event.confidence).toFixed(1)}%</td>
                <td>${escapeHtml(event.diagnosis)}</td>
            </tr>
        `;
    }).join('');
    target.innerHTML = `<table><thead><tr>
        <th>Cycle</th><th>Second</th><th>Sensor</th><th>Fault</th><th>Status</th><th>Decision</th>
        <th>Observed</th><th>Repaired</th><th>Confidence</th><th>Diagnosis</th>
        </tr></thead><tbody>${rows}</tbody></table>`;
    target.querySelectorAll('[data-point-index]').forEach(row => {
        row.addEventListener('click', () => inspectPoint(Number(row.dataset.pointIndex)));
        row.addEventListener('keydown', event => {
            if (event.key === 'Enter') inspectPoint(Number(row.dataset.pointIndex));
        });
    });
}

function renderUnresolvedPoints(points) {
    const target = document.getElementById('unresolvedTable');
    if (!target) return;
    const unresolved = (points || []).filter(point => point.status === 'Unresolved');
    if (!unresolved.length) {
        target.innerHTML = '<p>No unresolved values in the selected cycle.</p>';
        return;
    }
    const rows = unresolved.map(point => {
        const index = activePoints.findIndex(candidate => (
            candidate.cycle_id === point.cycle_id
            && candidate.second_in_cycle === point.second_in_cycle
            && candidate.sensor_id === point.sensor_id
        ));
        return `<tr data-point-index="${index}" tabindex="0">
            <td>${escapeHtml(point.cycle_id)}</td>
            <td>${escapeHtml(point.second_in_cycle)}</td>
            <td>${escapeHtml(point.sensor_id)}</td>
            <td>${escapeHtml(point.fault_type)}</td>
            <td>${escapeHtml(point.acceptance_decision || 'refused')}</td>
            <td>${Number(point.confidence).toFixed(1)}%</td>
            <td>${escapeHtml(point.reasoning || point.diagnosis || 'Insufficient evidence; output withheld.')}</td>
        </tr>`;
    }).join('');
    target.innerHTML = `<table><thead><tr><th>Cycle</th><th>Second</th><th>Sensor</th><th>Suspected fault</th><th>Decision</th><th>Confidence</th><th>Abstention reason</th></tr></thead><tbody>${rows}</tbody></table>`;
    target.querySelectorAll('[data-point-index]').forEach(row => {
        row.addEventListener('click', () => inspectPoint(Number(row.dataset.pointIndex)));
        row.addEventListener('keydown', event => {
            if (event.key === 'Enter') inspectPoint(Number(row.dataset.pointIndex));
        });
    });
}

function renderEvaluation(report) {
    const summary = document.getElementById('evaluationSummary');
    const accuracy = report.classification_report || {};
    if (!summary) return;
    renderDecisionMetrics(report);
    const cards = [
        ['Partition', report.partition],
        ['Complete cycles', Number(report.cycles || 0).toLocaleString()],
        ['Telemetry duration', `${Number(report.telemetry_seconds || 0).toLocaleString()} s`],
        ['Processed values', Number(report.points).toLocaleString()],
        ['Injected fault values', Number(report.injected_points).toLocaleString()],
        ['Detector accuracy', `${((accuracy.accuracy || 0) * 100).toFixed(2)}%`],
        ['Macro F1', `${(((accuracy['macro avg'] || {})['f1-score'] || 0) * 100).toFixed(2)}%`],
        ['Evaluation runtime', `${Number(report.duration_seconds || 0).toFixed(2)} s`],
        ['Repair MAE / RMSE', `${Number(report.repair_mae).toFixed(3)} / ${Number(report.repair_rmse).toFixed(3)}`],
        ['Unresolved points', report.unresolved_points ?? 0],
    ];
    summary.innerHTML = cards.map(([label, value]) => `
        <div class="stat-box"><label>${escapeHtml(label)}</label><strong>${escapeHtml(value)}</strong></div>
    `).join('');

    const matrix = document.getElementById('confusionMatrix');
    if (matrix) {
        const labels = report.class_names || [];
        matrix.innerHTML = `<table><thead><tr><th>Actual \\ Predicted</th>${labels.map(name => `<th>${escapeHtml(name)}</th>`).join('')}</tr></thead><tbody>${
            labels.map((name, row) => `<tr><th>${escapeHtml(name)}</th>${labels.map((_, column) => `<td class="matrix-cell">${report.confusion_matrix?.[row]?.[column] ?? 0}</td>`).join('')}</tr>`).join('')
        }</tbody></table>`;
    }

    const classification = document.getElementById('classificationTable');
    const classes = ['missing', 'freeze', 'spike', 'drift'];
    if (classification) {
        classification.innerHTML = `<table><thead><tr><th>Fault class</th><th>Precision</th><th>Recall</th><th>F1</th><th>Support</th></tr></thead><tbody>${
            classes.map(name => {
                const value = report.classification_report?.[name] || {};
                return `<tr><td>${name}</td><td>${(value.precision * 100 || 0).toFixed(1)}%</td><td>${(value.recall * 100 || 0).toFixed(1)}%</td><td>${(value['f1-score'] * 100 || 0).toFixed(1)}%</td><td>${value.support || 0}</td></tr>`;
            }).join('')
        }</tbody></table>`;
    }

    const baseline = document.getElementById('baselineTable');
    if (baseline) {
        const methods = report.repair_baselines || {};
        baseline.innerHTML = `<table><thead><tr><th>Method</th><th>MAE</th><th>RMSE</th><th>Points</th></tr></thead><tbody>${
            Object.entries(methods).map(([method, values]) => `<tr><td>${escapeHtml(method.replaceAll('_', ' '))}</td><td>${values.mae.toFixed(3)}</td><td>${values.rmse.toFixed(3)}</td><td>${values.points}</td></tr>`).join('')
        }</tbody></table>`;
    }

    const repairs = document.getElementById('repairTable');
    if (repairs) {
        repairs.innerHTML = `<table><thead><tr><th>Sensor</th><th>Values repaired</th><th>MAE</th><th>RMSE</th></tr></thead><tbody>${
            Object.entries(report.repair_mae_by_sensor || {}).map(([sensor, value]) => `<tr><td>${sensor}</td><td>${value.count}</td><td>${value.mae.toFixed(3)}</td><td>${value.rmse.toFixed(3)}</td></tr>`).join('')
        }</tbody></table>`;
    }

    const statuses = report.status_counts || {};
    const stress = report.multi_sensor_events || {};
    const stressTarget = document.getElementById('stressSummary');
    if (stressTarget) {
        const items = [
            ['Single-channel fault events', stress.single_channel ?? 'Unavailable'],
            ['Two-channel fault events', stress.two_channels ?? 'Unavailable'],
            ['Three-or-more-channel events', stress.three_or_more_channels ?? 'Unavailable'],
            ['Unresolved / abstained points', report.unresolved_points ?? 0],
        ];
        stressTarget.innerHTML = items.map(([label, value]) => `<div class="stat-box"><label>${label}</label><strong>${escapeHtml(value)}</strong></div>`).join('');
    }
    const unresolved = document.getElementById('unresolvedTable');
    if (unresolved) unresolved.innerHTML = `<p>${statuses.Unresolved || 0} points were abstained from. Their reconstructed output remains null rather than fabricating a value.</p>`;
    renderReliability(report.confidence_reliability || []);
}

function renderReliability(bins) {
    const svg = document.getElementById('reliabilitySvg');
    if (!svg) return;
    const width = 500;
    const height = 280;
    const padding = 42;
    const x = value => padding + value / 100 * (width - padding * 2);
    const y = value => height - padding - value / 100 * (height - padding * 2);
    const points = bins.map(bin => `${x(bin.mean_confidence)},${y(bin.observed_success_rate)}`).join(' ');
    const diagonal = `${x(0)},${y(0)} ${x(100)},${y(100)}`;
    svg.innerHTML = `
        <line x1="${padding}" y1="${height - padding}" x2="${width - padding}" y2="${height - padding}" stroke="#8ea4bb" />
        <line x1="${padding}" y1="${padding}" x2="${padding}" y2="${height - padding}" stroke="#8ea4bb" />
        <polyline points="${diagonal}" fill="none" stroke="#8ea4bb" stroke-dasharray="5 5" />
        <polyline points="${points}" fill="none" stroke="#62d0ff" stroke-width="3" />
        ${bins.map(bin => `<circle cx="${x(bin.mean_confidence)}" cy="${y(bin.observed_success_rate)}" r="5" fill="#7ef0b6"><title>${bin.count} repairs: confidence ${bin.mean_confidence.toFixed(1)}%, success ${bin.observed_success_rate.toFixed(1)}%</title></circle>`).join('')}
        <text x="${width / 2 - 58}" y="${height - 8}" fill="#8ea4bb">Predicted confidence (%)</text>
        <text x="5" y="${padding - 8}" fill="#8ea4bb">Observed success (%)</text>
    `;
}

function initializeBenchmarkFilters(report) {
    const filterValues = {
        benchmarkFaultFilter: [...new Set((report.cells || []).map(cell => cell.fault_type))].sort(),
        benchmarkGroupFilter: [...new Set((report.cells || []).map(cell => cell.sensor_group))].sort(),
        benchmarkChannelsFilter: [...new Set((report.cells || []).map(cell => cell.affected_channels))].sort((a, b) => a - b),
    };
    Object.entries(filterValues).forEach(([id, values]) => {
        const select = document.getElementById(id);
        if (!select) return;
        const selected = select.value;
        select.innerHTML = `<option value="">All</option>${values.map(value =>
            `<option value="${escapeHtml(value)}">${escapeHtml(value)}${id === 'benchmarkChannelsFilter' ? ' channel(s)' : ''}</option>`
        ).join('')}`;
        select.value = selected;
        select.addEventListener('change', () => renderBenchmark(report));
    });
}

function renderBenchmark(report = benchmarkReport) {
    const status = document.getElementById('benchmarkStatus');
    const table = document.getElementById('benchmarkTable');
    const heatmap = document.getElementById('benchmarkHeatmap');
    if (!report || !table || !heatmap) return;
    const fault = document.getElementById('benchmarkFaultFilter')?.value || '';
    const group = document.getElementById('benchmarkGroupFilter')?.value || '';
    const channels = document.getElementById('benchmarkChannelsFilter')?.value || '';
    const cells = (report.cells || []).filter(cell => (
        (!fault || cell.fault_type === fault)
        && (!group || cell.sensor_group === group)
        && (!channels || String(cell.affected_channels) === channels)
    ));
    if (status) {
        status.textContent = `Generated on ${escapeHtml(report.partition)} data: ${cells.length} visible of ${report.cells?.length || 0} cells, ${report.repeats || 0} repeats per cell, seed ${report.seed ?? '—'}. ${report.truncated ? 'This report is a bounded pilot, not the full grid.' : 'Full available grid.'}`;
    }

    const byChannel = new Map();
    cells.forEach(cell => {
        const count = Number(cell.affected_channels);
        if (!byChannel.has(count)) byChannel.set(count, []);
        byChannel.get(count).push(cell);
    });
    const channelRows = [...byChannel.entries()].sort((a, b) => a[0] - b[0]).map(([count, rows]) => {
        const coverageValues = rows.map(row => Number(row.accepted_repair_coverage)).filter(Number.isFinite);
        const errorValues = rows.map(row => Number(row.normalized_error)).filter(Number.isFinite);
        const coverage = coverageValues.length ? coverageValues.reduce((sum, value) => sum + value, 0) / coverageValues.length : null;
        const error = errorValues.length ? errorValues.reduce((sum, value) => sum + value, 0) / errorValues.length : null;
        const hue = coverage === null ? 210 : Math.round(coverage * 120);
        return `<div class="benchmark-heat-cell" style="--coverage-hue:${hue}">
            <strong>${count} channel${count === 1 ? '' : 's'}</strong>
            <span>Accepted coverage: ${coverage === null ? '—' : `${(coverage * 100).toFixed(1)}%`}</span>
            <span>Normalized error: ${error === null ? '—' : error.toFixed(3)}</span>
            <small>${rows.length} cells</small>
        </div>`;
    }).join('');
    heatmap.innerHTML = channelRows || '<p class="muted">No benchmark cells match these filters.</p>';

    if (!cells.length) {
        table.innerHTML = '<p class="muted">No benchmark cells match these filters.</p>';
        return;
    }
    const rows = cells.map(cell => `<tr>
        <td>${escapeHtml(cell.fault_type)}</td><td>${escapeHtml(cell.sensor_group)}</td>
        <td>${escapeHtml(cell.affected_channels)}</td><td>${escapeHtml(cell.duration_seconds)}s</td>
        <td>${cell.severity === null || cell.severity === undefined ? '—' : escapeHtml(cell.severity)}</td>
        <td>${(Number(cell.precision) * 100).toFixed(1)}%</td>
        <td>${(Number(cell.recall) * 100).toFixed(1)}%</td>
        <td>${(Number(cell.accepted_repair_coverage) * 100).toFixed(1)}%</td>
        <td>${(Number(cell.unresolved_rate) * 100).toFixed(1)}%</td>
        <td>${cell.normalized_error === null || cell.normalized_error === undefined ? '—' : Number(cell.normalized_error).toFixed(3)}</td>
        <td>${(Number(cell.within_tolerance_rate) * 100).toFixed(1)}%</td>
        <td>${cell.mean_confidence === null || cell.mean_confidence === undefined ? '—' : Number(cell.mean_confidence).toFixed(1)}</td>
        <td>${escapeHtml(cell.repeats)}</td>
    </tr>`).join('');
    table.innerHTML = `<table><thead><tr>
        <th>Fault</th><th>Sensor group</th><th>Channels</th><th>Duration</th><th>Severity</th>
        <th>Precision</th><th>Recall</th><th>Accepted coverage</th><th>Unresolved</th>
        <th>Normalized error</th><th>Within tolerance</th><th>Mean confidence</th><th>Repeats</th>
        </tr></thead><tbody>${rows}</tbody></table>`;
}

async function loadBenchmark() {
    const status = document.getElementById('benchmarkStatus');
    if (!status) return;
    try {
        benchmarkReport = await getJson('/api/benchmark');
        initializeBenchmarkFilters(benchmarkReport);
        renderBenchmark(benchmarkReport);
    } catch (error) {
        status.textContent = `Benchmark report unavailable: ${error.message}`;
    }
}

function startTelemetryReplay() {
    const button = document.getElementById('replayStartButton');
    const clock = document.getElementById('replayClock');
    const progress = document.getElementById('replayProgress');
    const status = document.getElementById('replayStatus');
    const partition = document.getElementById('evaluationPartition')?.value || 'test';
    const sensor = document.getElementById('sensorSelect')?.value || 'PS1';
    const cycle = document.getElementById('cycleSelect')?.value;
    if (!button || !clock || !progress || !status) return;
    if (!cycle) {
        status.textContent = 'Select a complete 60-second cycle before starting the recorded replay.';
        return;
    }
    activeReplay?.close();
    const samples = [];
    progress.value = 0;
    clock.textContent = 'SIMULATED 00:00';
    status.textContent = 'Streaming recorded one-second averages; repairs remain pending until the full cycle completes.';
    button.disabled = true;
    const params = new URLSearchParams({
        partition,
        sensor_id: sensor,
        cycle_id: cycle,
    });
    const stream = new EventSource(`/api/telemetry/replay?${params}`);
    activeReplay = stream;
    stream.addEventListener('tick', event => {
        const tick = JSON.parse(event.data);
        samples.push(tick.samples);
        progress.value = samples.length;
        clock.textContent = `${tick.clock_label} ${tick.simulated_time} / 00:59`;
        status.textContent = `Accumulated recorded second ${tick.second_in_cycle + 1} of 60. Repair status: pending until cycle completion.`;
    });
    stream.addEventListener('cycle_complete', event => {
        const completed = JSON.parse(event.data);
        const series = completed.series;
        renderChart(series);
        renderAuditTable(completed.audit_records);
        renderUnresolvedPoints(series.points);
        renderSummary({
            name: `Recorded replay • cycle ${completed.cycle_id}`,
            metrics: {
                status_counts: completed.status_counts,
                modified_count: Number(completed.status_counts.Corrected || 0)
                    + Number(completed.status_counts.Estimated || 0),
            },
        });
        if (activeRigData?.sensors) {
            activeRigData = {
                ...activeRigData,
                sensors: { ...activeRigData.sensors, [completed.sensor_id]: series.points },
            };
        }
        window.renderSensorRig?.(activeRigData, completed.sensor_id);
        clock.textContent = 'SIMULATED 01:00 — cycle complete';
        status.textContent = `${completed.repair_timing} The chart, status summary, and audit records now show the completed cycle.`;
        const download = document.getElementById('downloadAuditButton');
        if (download) download.disabled = true;
    });
    stream.addEventListener('done', () => {
        stream.close();
        if (activeReplay === stream) activeReplay = null;
        button.disabled = false;
    });
    stream.onerror = () => {
        if (stream.readyState === EventSource.CLOSED) {
            status.textContent = 'Replay stream ended before completion; refresh the dashboard and try again.';
            stream.close();
            if (activeReplay === stream) activeReplay = null;
            button.disabled = false;
        }
    };
}

async function loadOverview() {
    try {
        const metrics = await getJson('/api/summary');
        renderOverview(metrics);
        const systemStatus = document.getElementById('systemStatus');
        if (systemStatus) systemStatus.textContent = metrics.missing_rate === 0 ? 'Healthy' : 'Attention';
        const faultCoverage = document.getElementById('faultCoverage');
        if (faultCoverage) faultCoverage.textContent = `${metrics.missing_rate || 0}% missing`;
        const confidence = document.getElementById('modelConfidence');
        if (confidence) confidence.textContent = 'Run analysis to score repairs';
    } catch (error) {
        console.error(error);
    }
}

async function loadDashboard() {
    const partition = document.getElementById('evaluationPartition')?.value || 'test';
    const sensor = document.getElementById('sensorSelect')?.value || 'PS1';
    const cycleSelect = document.getElementById('cycleSelect');
    const selectedCycle = cycleSelect?.value || '';
    const includeReference = document.getElementById('showGroundTruth')?.checked || false;
    const runId = new URLSearchParams(window.location.search).get('run');
    try {
        const summary = await getJson('/api/summary');
        const report = await getJson(`/api/evaluation?partition=${encodeURIComponent(partition)}`);
        renderOverview(summary, report);
        renderEvaluation(report);
        const partitionSummary = document.getElementById('partitionSummary');
        if (partitionSummary) partitionSummary.textContent =
            `${partition} partition • ${Number(report.cycles || 0).toLocaleString()} cycles • ${Number(report.telemetry_seconds || 0).toLocaleString()} telemetry seconds • ${report.points.toLocaleString()} sensor readings • ${Number(report.duration_seconds || 0).toFixed(2)} seconds processing`;

        if (runId) {
            const runs = await getJson('/api/runs');
            const run = (runs.runs || []).find(item => String(item.id) === runId);
            if (!run) throw new Error('Run was not found for this account.');
            activeRunId = run.id;
            renderSummary(run);
            const cycleQuery = selectedCycle ? `&cycle_id=${encodeURIComponent(selectedCycle)}` : '';
            const series = await getJson(`/runs/${run.id}/series?sensor_id=${encodeURIComponent(sensor)}${cycleQuery}`);
            if (cycleSelect) {
                cycleSelect.innerHTML = (series.cycles || []).map(cycle =>
                    `<option value="${escapeHtml(cycle)}">Cycle ${escapeHtml(cycle)}</option>`
                ).join('');
                cycleSelect.value = String(series.selected_cycle ?? '');
            }
            activeRigData = await getJson(`/runs/${run.id}/rig?cycle_id=${encodeURIComponent(series.selected_cycle)}`);
            renderChart(series);
            renderAuditTable((run.events || []).filter(event => event.sensor_id === sensor));
            renderUnresolvedPoints(series.points || []);
        } else {
            activeRunId = null;
            renderSummary({
                name: `${partition} evaluation`,
                metrics: {
                    success_probability: report.confidence_reliability?.reduce((sum, bin) => sum + bin.mean_confidence * bin.count, 0) /
                        Math.max(report.confidence_reliability?.reduce((sum, bin) => sum + bin.count, 0) || 1, 1),
                    modified_count: report.modified_points,
                    fault_counts: report.fault_distribution,
                    status_counts: report.status_counts,
                },
            });
            const cycleQuery = selectedCycle ? `&cycle_id=${encodeURIComponent(selectedCycle)}` : '';
            const referenceQuery = `&include_reference=${includeReference}`;
            const series = await getJson(`/api/evaluation/series?partition=${encodeURIComponent(partition)}&sensor_id=${encodeURIComponent(sensor)}${cycleQuery}${referenceQuery}`);
            if (cycleSelect) {
                cycleSelect.innerHTML = (series.cycles || []).map(cycle =>
                    `<option value="${escapeHtml(cycle)}">Cycle ${escapeHtml(cycle)}</option>`
                ).join('');
                cycleSelect.value = String(series.selected_cycle ?? '');
            }
            activeRigData = await getJson(`/api/evaluation/rig?partition=${encodeURIComponent(partition)}&cycle_id=${encodeURIComponent(series.selected_cycle)}`);
            renderChart(series);
            renderAuditTable(series.points.filter(point => point.fault_type !== 'healthy'));
            renderUnresolvedPoints(series.points);
        }
        const download = document.getElementById('downloadAuditButton');
        if (download) download.disabled = !activeRunId;
        const systemStatus = document.getElementById('systemStatus');
        if (systemStatus) systemStatus.textContent = 'Evaluation complete';
    } catch (error) {
        console.error(error);
        if (error.status === 401 && includeReference) {
            const target = document.getElementById('evaluationSummary');
            if (target) target.innerHTML = '<p class="muted">Reference values are protected. Sign in through an authorized client before enabling judge-mode reference display.</p>';
            return;
        }
        if (error.status === 401) localStorage.removeItem('healpipeToken');
        const target = document.getElementById('evaluationSummary');
        if (target) target.innerHTML = `<p class="muted">${escapeHtml(error.message)}.</p>`;
    }
}

async function downloadAudit(runId) {
    const response = await fetch(`/runs/${runId}/audit`, {
        headers: { Authorization: `Bearer ${localStorage.getItem('healpipeToken') || ''}` },
    });
    if (!response.ok) throw new Error(`Audit download failed: ${response.status}`);
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement('a');
    link.href = url;
    link.download = `healpipe-run-${runId}-audit.jsonl`;
    link.click();
    URL.revokeObjectURL(url);
}

async function downloadScenarioAudit(runId) {
    const token = localStorage.getItem('healpipeToken');
    const headers = token ? { Authorization: `Bearer ${token}` } : {};
    const response = await fetch(`/api/scenarios/runs/${runId}/audit`, { headers });
    if (!response.ok) throw new Error(`Scenario audit download failed: ${response.status}`);
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement('a');
    link.href = url;
    link.download = `healpipe-scenario-${runId}.jsonl`;
    link.click();
    URL.revokeObjectURL(url);
}

async function initializeDemoUpload() {
    const config = await getJson('/api/config');
    const section = document.getElementById('demoIngestion');
    const form = document.getElementById('demoUploadForm');
    const status = document.getElementById('demoUploadStatus');
    if (!config.demo_upload_enabled || !section || !form || !status) return;
    section.hidden = false;
    form.addEventListener('submit', async event => {
        event.preventDefault();
        const file = document.getElementById('demoTelemetryFile')?.files?.[0];
        if (!file) return;
        if (file.size > 25 * 1024 * 1024) {
            status.textContent = 'File exceeds the 25 MB limit.';
            return;
        }
        status.textContent = 'Uploading and validating telemetry…';
        try {
            const body = new FormData();
            body.append('file', file);
            const token = localStorage.getItem('healpipeToken');
            const headers = token ? { Authorization: `Bearer ${token}` } : {};
            const response = await fetch('/jobs/upload', { method: 'POST', headers, body });
            const result = await response.json();
            if (!response.ok) throw new Error(result.detail || `Upload failed: ${response.status}`);
            status.textContent = `Job ${result.job_id}: ${result.status}`;
            for (let attempt = 0; attempt < 30; attempt += 1) {
                await new Promise(resolve => setTimeout(resolve, 1000));
                const job = await getJson(`/jobs/${result.job_id}`);
                status.textContent = `Job ${result.job_id}: ${job.status}${job.error ? ` — ${job.error}` : ''}`;
                if (job.status === 'completed' || job.status === 'failed') break;
            }
        } catch (error) {
            status.textContent = error.message;
        }
    });
}

async function initializeSensorSchema() {
    const config = await getJson('/api/config');
    const select = document.getElementById('sensorSelect');
    if (!select || !Array.isArray(config.sensors)) return;
    const selected = select.value;
    select.innerHTML = config.sensors.map(sensor =>
        `<option value="${escapeHtml(sensor.sensor_id)}">${escapeHtml(sensor.sensor_id)} (${escapeHtml(sensor.unit)})</option>`
    ).join('');
    if (config.sensors.some(sensor => sensor.sensor_id === selected)) select.value = selected;
}

document.addEventListener('DOMContentLoaded', () => {
    const refreshButton = document.getElementById('refreshButton');
    const sensorSelect = document.getElementById('sensorSelect');
    const partitionSelect = document.getElementById('evaluationPartition');
    const cycleSelect = document.getElementById('cycleSelect');
    const referenceToggle = document.getElementById('showGroundTruth');
    const scenarioType = document.getElementById('scenarioFaultType');
    const scenarioDuration = document.getElementById('scenarioDuration');
    const scenarioSeverity = document.getElementById('scenarioSeverity');
    const scenarioButton = document.getElementById('runScenarioButton');
    const scenarioDownload = document.getElementById('downloadScenarioButton');
    const replayButton = document.getElementById('replayStartButton');

    const updateScenarioInputs = () => {
        if (!scenarioType || !scenarioDuration || !scenarioSeverity) return;
        const fault = scenarioType.value;
        const durationRange = fault === 'freeze' ? [3, 10]
            : fault === 'drift' ? [10, 30]
                : fault === 'spike' ? [1, 1] : [1, 30];
        scenarioDuration.min = String(durationRange[0]);
        scenarioDuration.max = String(durationRange[1]);
        if (Number(scenarioDuration.value) < durationRange[0]
            || Number(scenarioDuration.value) > durationRange[1]) {
            scenarioDuration.value = String(durationRange[0]);
        }
        scenarioDuration.disabled = fault === 'spike';
        scenarioSeverity.disabled = !['spike', 'drift'].includes(fault);
    };

    if (document.getElementById('chartSvg')) loadDashboard();
    else loadOverview();
    initializeDemoUpload().catch(error => console.error('Unable to load demo upload settings:', error));
    initializeSensorSchema().catch(error => console.error('Unable to load configured sensor schema:', error));
    loadBenchmark();
    if (refreshButton) refreshButton.addEventListener('click', loadDashboard);
    if (sensorSelect) sensorSelect.addEventListener('change', () => {
        if (cycleSelect) cycleSelect.value = '';
        loadDashboard();
    });
    if (partitionSelect) partitionSelect.addEventListener('change', () => {
        if (cycleSelect) cycleSelect.value = '';
        loadDashboard();
    });
    if (cycleSelect) cycleSelect.addEventListener('change', loadDashboard);
    if (referenceToggle) referenceToggle.addEventListener('change', loadDashboard);
    if (scenarioType) {
        scenarioType.addEventListener('change', updateScenarioInputs);
        updateScenarioInputs();
    }
    if (scenarioButton) scenarioButton.addEventListener('click', runFaultScenario);
    if (replayButton) replayButton.addEventListener('click', startTelemetryReplay);
    if (scenarioDownload) scenarioDownload.addEventListener('click', async () => {
        const runId = Number(scenarioDownload.dataset.runId);
        if (!runId) return;
        try {
            await downloadScenarioAudit(runId);
        } catch (error) {
            const target = document.getElementById('scenarioResults');
            if (target) target.insertAdjacentHTML('afterbegin', `<p role="alert">${escapeHtml(error.message)}</p>`);
        }
    });
    document.addEventListener('healpipe:sensor-selected', event => {
        const sensor = event.detail?.sensor;
        const sensorSelect = document.getElementById('sensorSelect');
        if (!sensor || !sensorSelect || !Array.from(sensorSelect.options).some(option => option.value === sensor || option.textContent === sensor)) return;
        sensorSelect.value = sensor;
        loadDashboard().then(() => {
            document.getElementById('audit-inspector')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
        });
    });
});
