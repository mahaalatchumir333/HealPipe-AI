let activePoints = [];
let activeRunId = null;

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
    `;
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
        </div>
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
                <td>${escapeHtml(event.observed_value ?? 'missing')}</td>
                <td>${escapeHtml(event.reconstructed_value ?? '—')}</td>
                <td>${Number(event.confidence).toFixed(1)}%</td>
                <td>${escapeHtml(event.diagnosis)}</td>
            </tr>
        `;
    }).join('');
    target.innerHTML = `<table><thead><tr>
        <th>Cycle</th><th>Second</th><th>Sensor</th><th>Fault</th><th>Status</th>
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
            <td>${Number(point.confidence).toFixed(1)}%</td>
            <td>${escapeHtml(point.reasoning || point.diagnosis || 'Insufficient evidence; output withheld.')}</td>
        </tr>`;
    }).join('');
    target.innerHTML = `<table><thead><tr><th>Cycle</th><th>Second</th><th>Sensor</th><th>Suspected fault</th><th>Confidence</th><th>Abstention reason</th></tr></thead><tbody>${rows}</tbody></table>`;
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
        try {
            const runs = await getJson('/api/runs');
            renderRunsTable(runs.runs || []);
        } catch {
            renderRunsTable([]);
        }
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
        if (error.status === 401) localStorage.removeItem('healpipeToken');
        const target = document.getElementById('evaluationSummary');
        if (target) target.innerHTML = `<p class="muted">${escapeHtml(error.message)}. <a href="/">Go to account access to sign in.</a></p>`;
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

async function createDemoRun() {
    const button = document.getElementById('demoRunButton');
    if (button) {
        button.disabled = true;
        button.textContent = 'Analyzing telemetry…';
    }
    try {
        const response = await fetch('/jobs', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                Authorization: `Bearer ${localStorage.getItem('healpipeToken') || ''}`,
            },
            body: JSON.stringify({ name: 'website-demo', partition: 'validation' }),
        });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.detail || `Analysis failed: ${response.status}`);
        if (button) button.textContent = 'Analysis queued…';
        const job = await pollJob(payload.job_id);
        window.location.href = `/dashboard?run=${job.run_id}`;
    } catch (error) {
        if (button) {
            button.disabled = false;
            button.textContent = 'Run demo analysis';
        }
        window.alert(error.message);
    }
}

async function pollJob(jobId) {
    for (let attempt = 0; attempt < 300; attempt += 1) {
        await new Promise(resolve => setTimeout(resolve, 1000));
        const job = await getJson(`/jobs/${jobId}`);
        if (job.status === 'completed') return job;
        if (job.status === 'failed') throw new Error(job.error || 'Analysis failed.');
    }
    throw new Error('Analysis is still running; refresh the dashboard later.');
}

async function authenticate(path) {
    const response = await fetch(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            username: document.getElementById('authUsername').value,
            password: document.getElementById('authPassword').value,
            mfa_code: document.getElementById('mfaCode').value || null,
        }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Authentication failed.');
    if (data.access_token) {
        localStorage.setItem('healpipeToken', data.access_token);
        document.getElementById('authMessage').textContent = 'Signed in. You can now run analysis and upload telemetry.';
        loadOverview();
        return;
    }
    document.getElementById('authMessage').textContent = data.message || 'Account created. Sign in to continue.';
}

async function setupMfa() {
    const response = await fetch('/auth/mfa/setup', {
        method: 'POST',
        headers: { Authorization: `Bearer ${localStorage.getItem('healpipeToken') || ''}` },
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Unable to start MFA setup.');
    document.getElementById('authMessage').textContent =
        `Add this secret to an authenticator app, then confirm with a code: ${data.secret}`;
}

async function confirmMfa() {
    const response = await fetch('/auth/mfa/confirm', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            Authorization: `Bearer ${localStorage.getItem('healpipeToken') || ''}`,
        },
        body: JSON.stringify({ code: document.getElementById('mfaCode').value }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Unable to enable MFA.');
    document.getElementById('authMessage').textContent = 'MFA is enabled for your account.';
}

async function submitUpload(event) {
    event.preventDefault();
    const fileInput = document.getElementById('telemetryFile');
    const status = document.getElementById('uploadStatus');
    const progress = document.getElementById('uploadProgress');
    if (!localStorage.getItem('healpipeToken')) {
        status.textContent = 'Sign in before uploading telemetry.';
        return;
    }
    const file = fileInput.files[0];
    if (!file) return;
    const form = new FormData();
    form.append('file', file);
    progress.hidden = false;
    progress.value = 25;
    status.textContent = 'Uploading and validating file…';
    try {
        const response = await fetch('/jobs/upload', {
            method: 'POST',
            headers: { Authorization: `Bearer ${localStorage.getItem('healpipeToken')}` },
            body: form,
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || 'Upload failed.');
        progress.value = 60;
        status.textContent = 'Upload accepted; analysis is running…';
        const job = await pollJob(data.job_id);
        progress.value = 100;
        window.location.href = `/dashboard?run=${job.run_id}`;
    } catch (error) {
        status.textContent = error.message;
        progress.value = 0;
    }
}

function showAuthError(error) {
    const message = document.getElementById('authMessage');
    if (message) message.textContent = error.message;
}

document.addEventListener('DOMContentLoaded', () => {
    const demoButton = document.getElementById('demoRunButton');
    const refreshButton = document.getElementById('refreshButton');
    const loginButton = document.getElementById('loginButton');
    const registerButton = document.getElementById('registerButton');
    const mfaSetupButton = document.getElementById('mfaSetupButton');
    const mfaConfirmButton = document.getElementById('mfaConfirmButton');
    const uploadForm = document.getElementById('uploadForm');
    const sensorSelect = document.getElementById('sensorSelect');
    const partitionSelect = document.getElementById('evaluationPartition');
    const cycleSelect = document.getElementById('cycleSelect');
    const referenceToggle = document.getElementById('showGroundTruth');

    if (document.getElementById('chartSvg')) loadDashboard();
    else loadOverview();
    if (demoButton) demoButton.addEventListener('click', createDemoRun);
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
    if (loginButton) loginButton.addEventListener('click', () => authenticate('/auth/login').catch(showAuthError));
    if (registerButton) registerButton.addEventListener('click', () => authenticate('/auth/register').catch(showAuthError));
    if (mfaSetupButton) mfaSetupButton.addEventListener('click', () => setupMfa().catch(showAuthError));
    if (mfaConfirmButton) mfaConfirmButton.addEventListener('click', () => confirmMfa().catch(showAuthError));
    if (uploadForm) uploadForm.addEventListener('submit', submitUpload);
});
