(() => {
    const statusColors = {
        Measured: 0x7ef0b6,
        Estimated: 0xc49aff,
        Corrected: 0xff7d7d,
        Unresolved: 0xff5e67,
        Unknown: 0x8ea4bb,
    };
    const faultColors = {
        healthy: 0x8ea4bb,
        missing: 0x62d0ff,
        freeze: 0xc49aff,
        spike: 0xffbd69,
        drift: 0xff7d7d,
    };
    const markerPositions = {
        PS1: [-2.1, 0.54, -1.2],
        PS2: [-0.65, 0.54, -1.2],
        PS3: [0.8, 0.54, -1.2],
        PS4: [-1.4, 0.54, -0.75],
        PS5: [0.1, 0.54, -0.75],
        PS6: [1.35, 0.54, -0.75],
        FS1: [-0.2, 0.5, 1.15],
        FS2: [1.35, 0.5, 1.15],
        TS1: [-2.8, 0.54, 0.9],
        TS2: [-2.15, 0.54, 1.45],
        TS3: [2.05, 0.54, 1.45],
        TS4: [2.75, 0.54, 0.9],
        EPS1: [-3.05, 0.75, 0],
        VS1: [-2.55, 1.1, 0],
    };

    let renderer;
    let scene;
    let camera;
    let markerMeshes = [];
    let sensorMap;
    let sensorPoints = {};
    let selectedSensor = null;
    let fallbackActive = false;
    let animationStarted = false;
    let pumpRotor;
    let motorRotor;
    let coolerFan;
    let valveLever;
    let accumulatorPiston;
    let tankSurface;
    let tankBubbles = [];
    let flowParticles = [];
    let flowSegments = [];
    let flowLength = 0;
    let dragState = null;
    let pointerMoved = false;
    let lastAnimationSeconds = null;
    let lastInteractionSeconds = 0;
    let orbitTheta = Math.atan2(7.5, 9.5);
    let orbitPhi = Math.acos(6.5 / Math.sqrt(7.5 ** 2 + 6.5 ** 2 + 9.5 ** 2));
    let orbitRadius = Math.sqrt(7.5 ** 2 + 6.5 ** 2 + 9.5 ** 2);
    let orbitTarget;
    const raycaster = window.THREE ? new THREE.Raycaster() : null;
    const pointer = window.THREE ? new THREE.Vector2() : null;

    function escapeHtml(value) {
        return String(value ?? '').replace(/[&<>"']/g, character => ({
            '&': '&amp;',
            '<': '&lt;',
            '>': '&gt;',
            '"': '&quot;',
            "'": '&#39;',
        })[character]);
    }

    function summarizePoints(points) {
        if (!points?.length) return { status: 'Unknown', fault: 'healthy' };
        const statusPriority = ['Unresolved', 'Corrected', 'Estimated', 'Measured'];
        const status = statusPriority.find(value => points.some(point => point.status === value)) || 'Unknown';
        const faults = points.reduce((counts, point) => {
            if (point.fault_type && point.fault_type !== 'healthy') {
                counts[point.fault_type] = (counts[point.fault_type] || 0) + 1;
            }
            return counts;
        }, {});
        const fault = Object.entries(faults).sort((left, right) => right[1] - left[1])[0]?.[0] || 'healthy';
        return { status, fault };
    }

    function dispatchSensorSelection(sensor) {
        selectedSensor = sensor;
        document.dispatchEvent(new CustomEvent('healpipe:sensor-selected', {
            detail: { sensor },
        }));
    }

    function componentMaterial(color, roughness = 0.72) {
        return new THREE.MeshStandardMaterial({ color, roughness, metalness: 0.12 });
    }

    function addBox(parent, name, position, dimensions, color) {
        const mesh = new THREE.Mesh(
            new THREE.BoxGeometry(...dimensions),
            componentMaterial(color),
        );
        mesh.position.set(...position);
        mesh.name = name;
        parent.add(mesh);
        return mesh;
    }

    function addCylinder(parent, name, position, radiusTop, radiusBottom, height, color, radialSegments = 20) {
        const mesh = new THREE.Mesh(
            new THREE.CylinderGeometry(radiusTop, radiusBottom, height, radialSegments),
            componentMaterial(color),
        );
        mesh.position.set(...position);
        mesh.name = name;
        parent.add(mesh);
        return mesh;
    }

    function addPipe(parent, from, to, radius = 0.055, color = 0x62d0ff) {
        const start = new THREE.Vector3(...from);
        const end = new THREE.Vector3(...to);
        const direction = new THREE.Vector3().subVectors(end, start);
        const pipe = new THREE.Mesh(
            new THREE.CylinderGeometry(radius, radius, direction.length(), 12),
            componentMaterial(color, 0.42),
        );
        pipe.position.copy(start).add(end).multiplyScalar(0.5);
        pipe.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), direction.normalize());
        parent.add(pipe);
    }

    function createRotor(position, color, bladeCount = 4) {
        const rotor = new THREE.Group();
        rotor.position.set(...position);
        const hub = new THREE.Mesh(
            new THREE.SphereGeometry(0.075, 14, 12),
            componentMaterial(color, 0.3),
        );
        rotor.add(hub);
        for (let bladeIndex = 0; bladeIndex < bladeCount; bladeIndex += 1) {
            const blade = new THREE.Mesh(
                new THREE.BoxGeometry(0.38, 0.045, 0.04),
                componentMaterial(color, 0.34),
            );
            blade.rotation.z = (Math.PI / bladeCount) * bladeIndex;
            rotor.add(blade);
        }
        scene.add(rotor);
        return rotor;
    }

    function buildRig() {
        scene = new THREE.Scene();
        scene.background = new THREE.Color(0x0b1622);
        scene.fog = new THREE.Fog(0x0b1622, 13, 25);
        camera = new THREE.PerspectiveCamera(42, 1, 0.1, 100);
        orbitTarget = new THREE.Vector3(0, 0.6, 0);
        updateCamera();

        scene.add(new THREE.HemisphereLight(0xc9e7ff, 0x263344, 2.2));
        const keyLight = new THREE.DirectionalLight(0xffffff, 2.1);
        keyLight.position.set(4, 8, 5);
        scene.add(keyLight);

        const floor = new THREE.Mesh(
            new THREE.PlaneGeometry(10, 6),
            new THREE.MeshStandardMaterial({ color: 0x142536, roughness: 0.9 }),
        );
        floor.rotation.x = -Math.PI / 2;
        floor.position.y = -0.02;
        scene.add(floor);

        addBox(scene, 'test-rig base', [0, 0.08, 0], [7, 0.16, 3.8], 0x263b4e);
        addBox(scene, 'electric motor', [-2.9, 0.62, 0], [1.15, 0.86, 0.92], 0x47738a);
        addCylinder(scene, 'motor shaft', [-2.14, 0.61, 0], 0.15, 0.15, 0.38, 0x9eb7c9);
        motorRotor = createRotor([-2.9, 0.62, 0.49], 0xa8e5f3, 5);
        addCylinder(scene, 'pump', [-1.52, 0.55, 0], 0.47, 0.42, 0.76, 0xd9895b);
        pumpRotor = new THREE.Group();
        pumpRotor.position.set(-1.52, 0.55, 0.405);
        const rotorRing = new THREE.Mesh(
            new THREE.TorusGeometry(0.28, 0.035, 8, 28),
            componentMaterial(0xffcc79, 0.34),
        );
        pumpRotor.add(rotorRing);
        const rotorHub = new THREE.Mesh(
            new THREE.SphereGeometry(0.075, 14, 12),
            componentMaterial(0xffe2a8, 0.3),
        );
        pumpRotor.add(rotorHub);
        for (let spoke = 0; spoke < 4; spoke += 1) {
            const blade = new THREE.Mesh(
                new THREE.BoxGeometry(0.36, 0.035, 0.035),
                componentMaterial(0xffcc79, 0.34),
            );
            blade.rotation.z = (Math.PI / 2) * spoke;
            pumpRotor.add(blade);
        }
        scene.add(pumpRotor);
        addCylinder(scene, 'tank', [-2.25, 0.83, 1.12], 0.54, 0.54, 1.35, 0x4c8795, 28);
        tankSurface = new THREE.Mesh(
            new THREE.TorusGeometry(0.34, 0.025, 8, 32),
            new THREE.MeshBasicMaterial({ color: 0x9beaff }),
        );
        tankSurface.position.set(-2.25, 1.52, 1.12);
        tankSurface.rotation.x = Math.PI / 2;
        scene.add(tankSurface);
        tankBubbles = [];
        for (let index = 0; index < 3; index += 1) {
            const bubble = new THREE.Mesh(
                new THREE.SphereGeometry(0.045, 10, 8),
                new THREE.MeshBasicMaterial({ color: 0xb4f0ff }),
            );
            bubble.userData.phase = index / 3;
            bubble.userData.offset = (index - 1) * 0.18;
            scene.add(bubble);
            tankBubbles.push(bubble);
        }
        addBox(scene, 'cooler', [1.65, 0.68, -0.03], [0.9, 1.1, 0.85], 0x4e8f9e);
        addBox(scene, 'cooler fins', [1.65, 0.7, -0.49], [0.72, 0.78, 0.08], 0x8db6bc);
        coolerFan = createRotor([1.65, 0.7, -0.57], 0xc5eef2, 5);
        addCylinder(scene, 'valve body', [0.05, 0.55, 0.05], 0.32, 0.32, 0.58, 0xd2a94f);
        valveLever = new THREE.Group();
        valveLever.position.set(0.05, 1.0, 0.05);
        addBox(valveLever, 'valve handle', [0.32, 0, 0], [0.65, 0.08, 0.12], 0xefc76a);
        scene.add(valveLever);
        addCylinder(scene, 'accumulator', [2.85, 0.92, -0.95], 0.34, 0.34, 1.15, 0x8d79b5, 24);
        accumulatorPiston = new THREE.Group();
        accumulatorPiston.position.set(2.85, 1.52, -0.95);
        addCylinder(accumulatorPiston, 'accumulator cap', [0, 0, 0], 0.19, 0.19, 0.1, 0xc1b4df);
        scene.add(accumulatorPiston);

        const path = [
            [-2.25, 0.25, 1.12], [-1.52, 0.27, 0], [0.05, 0.27, 0.05],
            [1.65, 0.27, -0.03], [2.85, 0.3, -0.95], [2.85, 0.27, 0.75],
            [0.05, 0.27, 0.75], [-2.25, 0.27, 1.12],
        ];
        for (let index = 0; index < path.length - 1; index += 1) {
            addPipe(scene, path[index], path[index + 1]);
        }
        flowSegments = [];
        flowLength = 0;
        for (let index = 0; index < path.length - 1; index += 1) {
            const start = new THREE.Vector3(...path[index]);
            const end = new THREE.Vector3(...path[index + 1]);
            const length = start.distanceTo(end);
            flowSegments.push({ start, end, length, offset: flowLength });
            flowLength += length;
        }
        flowParticles = [];
        for (let index = 0; index < 5; index += 1) {
            const particle = new THREE.Mesh(
                new THREE.SphereGeometry(0.095, 12, 10),
                new THREE.MeshBasicMaterial({ color: 0x9beaff }),
            );
            particle.userData.phase = index / 5;
            scene.add(particle);
            flowParticles.push(particle);
        }

        const markerGroup = new THREE.Group();
        scene.add(markerGroup);
        markerMeshes = [];
        Object.entries(markerPositions).forEach(([sensor, position]) => {
            const marker = new THREE.Mesh(
                new THREE.SphereGeometry(0.14, 20, 16),
                new THREE.MeshStandardMaterial({
                    color: statusColors.Unknown,
                    emissive: 0x000000,
                    roughness: 0.35,
                    metalness: 0.1,
                }),
            );
            marker.position.set(...position);
            marker.userData.sensor = sensor;
            marker.userData.status = 'Unknown';
            const halo = new THREE.Mesh(
                new THREE.TorusGeometry(0.21, 0.035, 8, 28),
                new THREE.MeshBasicMaterial({ color: faultColors.healthy }),
            );
            halo.rotation.x = Math.PI / 2;
            marker.add(halo);
            markerGroup.add(marker);
            markerMeshes.push(marker);
        });
    }

    function useFallback(message) {
        const canvas = document.getElementById('rig3d');
        const fallback = document.getElementById('rigFallback');
        fallbackActive = true;
        if (canvas) canvas.hidden = true;
        if (fallback) {
            fallback.hidden = false;
            fallback.dataset.error = message || '';
        }
    }

    function initializeRenderer() {
        const container = document.getElementById('rig3d');
        if (!container || !window.THREE || !THREE.WebGLRenderer) {
            useFallback('WebGL is unavailable.');
            return false;
        }
        try {
            renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
            renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
            renderer.domElement.tabIndex = 0;
            renderer.domElement.setAttribute('aria-label', 'Interactive 3D rig. Drag to orbit, scroll to zoom, use arrow keys to rotate and plus or minus to zoom.');
            container.replaceChildren(renderer.domElement);
            buildRig();
            resizeRenderer();
            if (!animationStarted) {
                animationStarted = true;
                const animate = () => {
                    if (renderer && renderer.domElement.isConnected) {
                        animateRig(performance.now() / 1000);
                        renderer.render(scene, camera);
                    }
                    window.requestAnimationFrame(animate);
                };
                window.requestAnimationFrame(animate);
            }
            renderer.domElement.addEventListener('pointerdown', handlePointerDown);
            renderer.domElement.addEventListener('pointermove', handlePointerMove);
            renderer.domElement.addEventListener('pointerup', handlePointerUp);
            renderer.domElement.addEventListener('pointercancel', handlePointerCancel);
            renderer.domElement.addEventListener('wheel', handleWheel, { passive: false });
            renderer.domElement.addEventListener('click', handleCanvasClick);
            renderer.domElement.addEventListener('keydown', handleCanvasKeydown);
            window.addEventListener('resize', resizeRenderer);
            return true;
        } catch (error) {
            useFallback(`WebGL renderer could not be initialized: ${error.message}`);
            return false;
        }
    }

    function resizeRenderer() {
        const container = document.getElementById('rig3d');
        if (!renderer || !container || container.hidden) return;
        const width = Math.max(container.clientWidth, 320);
        const height = Math.max(container.clientHeight, 280);
        renderer.setSize(width, height, false);
        camera.aspect = width / height;
        camera.updateProjectionMatrix();
    }

    function updateCamera() {
        orbitPhi = Math.max(0.18, Math.min(Math.PI - 0.18, orbitPhi));
        orbitRadius = Math.max(5.2, Math.min(18, orbitRadius));
        const sinPhi = Math.sin(orbitPhi);
        camera.position.set(
            orbitTarget.x + orbitRadius * sinPhi * Math.sin(orbitTheta),
            orbitTarget.y + orbitRadius * Math.cos(orbitPhi),
            orbitTarget.z + orbitRadius * sinPhi * Math.cos(orbitTheta),
        );
        camera.lookAt(orbitTarget);
    }

    function animateRig(seconds) {
        const delta = lastAnimationSeconds === null
            ? 0
            : Math.min(seconds - lastAnimationSeconds, 0.1);
        lastAnimationSeconds = seconds;
        if (seconds - lastInteractionSeconds > 2 && !dragState) {
            orbitTheta += delta * 0.12;
            updateCamera();
        }
        if (pumpRotor) pumpRotor.rotation.z = seconds * 2.4;
        if (motorRotor) motorRotor.rotation.z = seconds * 2.4;
        if (coolerFan) coolerFan.rotation.z = -seconds * 1.6;
        if (valveLever) valveLever.rotation.z = Math.sin(seconds * 0.65) * 0.12;
        if (accumulatorPiston) accumulatorPiston.position.y = 1.52 + Math.sin(seconds * 1.7) * 0.045;
        if (tankSurface) tankSurface.rotation.z = seconds * 0.35;
        tankBubbles.forEach(bubble => {
            const phase = (seconds * 0.22 + bubble.userData.phase) % 1;
            bubble.position.set(
                -2.25 + bubble.userData.offset,
                0.55 + phase * 0.78,
                1.12 + Math.sin(phase * Math.PI * 2 + bubble.userData.phase) * 0.12,
            );
        });
        flowParticles.forEach(particle => {
            let distance = ((seconds * 0.65 + particle.userData.phase) % 1) * flowLength;
            const segment = flowSegments.find(candidate => distance <= candidate.offset + candidate.length)
                || flowSegments[flowSegments.length - 1];
            const progress = Math.max(0, Math.min(1, (distance - segment.offset) / segment.length));
            particle.position.lerpVectors(segment.start, segment.end, progress);
        });
    }

    function handlePointerDown(event) {
        if (event.button !== 0 || !renderer) return;
        lastInteractionSeconds = performance.now() / 1000;
        dragState = { x: event.clientX, y: event.clientY, theta: orbitTheta, phi: orbitPhi };
        pointerMoved = false;
        renderer.domElement.setPointerCapture(event.pointerId);
        renderer.domElement.classList.add('is-dragging');
    }

    function handlePointerMove(event) {
        if (!dragState || !renderer) return;
        const dx = event.clientX - dragState.x;
        const dy = event.clientY - dragState.y;
        if (Math.abs(dx) + Math.abs(dy) > 4) pointerMoved = true;
        orbitTheta = dragState.theta - dx * 0.008;
        orbitPhi = dragState.phi + dy * 0.008;
        updateCamera();
    }

    function finishPointerDrag(event) {
        if (renderer?.domElement.hasPointerCapture(event.pointerId)) {
            renderer.domElement.releasePointerCapture(event.pointerId);
        }
        dragState = null;
        renderer?.domElement.classList.remove('is-dragging');
    }

    function handlePointerUp(event) {
        finishPointerDrag(event);
        window.setTimeout(() => {
            pointerMoved = false;
        }, 0);
    }

    function handlePointerCancel(event) {
        finishPointerDrag(event);
    }

    function handleWheel(event) {
        event.preventDefault();
        lastInteractionSeconds = performance.now() / 1000;
        orbitRadius *= Math.exp(event.deltaY * 0.001);
        updateCamera();
    }

    function handleCanvasKeydown(event) {
        const step = event.shiftKey ? 0.2 : 0.08;
        if (event.key === 'ArrowLeft') orbitTheta -= step;
        else if (event.key === 'ArrowRight') orbitTheta += step;
        else if (event.key === 'ArrowUp') orbitPhi -= step;
        else if (event.key === 'ArrowDown') orbitPhi += step;
        else if (event.key === '+' || event.key === '=') orbitRadius *= 0.9;
        else if (event.key === '-') orbitRadius *= 1.1;
        else return;
        lastInteractionSeconds = performance.now() / 1000;
        event.preventDefault();
        updateCamera();
    }

    function handleCanvasClick(event) {
        if (pointerMoved || !renderer || !raycaster || !pointer) {
            pointerMoved = false;
            return;
        }
        const bounds = renderer.domElement.getBoundingClientRect();
        pointer.x = ((event.clientX - bounds.left) / bounds.width) * 2 - 1;
        pointer.y = -((event.clientY - bounds.top) / bounds.height) * 2 + 1;
        raycaster.setFromCamera(pointer, camera);
        const hit = raycaster.intersectObjects(markerMeshes, false)[0];
        if (hit?.object?.userData?.sensor) dispatchSensorSelection(hit.object.userData.sensor);
    }

    function renderFallback(sensors) {
        const target = document.getElementById('rigFallback');
        if (!target) return;
        const positions = Object.entries(markerPositions);
        const components = [
            ['Motor', 120, 110], ['Pump', 230, 110], ['Valve', 370, 110],
            ['Cooler', 510, 110], ['Accumulator', 640, 110], ['Tank', 120, 250],
        ];
        const componentSvg = components.map(([label, x, y]) =>
            `<g aria-hidden="true"><rect x="${x}" y="${y}" width="78" height="48" rx="8" fill="#263b4e" stroke="#62d0ff" /><text x="${x + 39}" y="${y + 29}" text-anchor="middle" fill="#eef5fb" font-size="12">${label}</text></g>`
        ).join('');
        const sensorSvg = positions.map(([sensor], index) => {
            const angle = (index / positions.length) * Math.PI * 2;
            const x = 380 + Math.cos(angle) * 285;
            const y = 205 + Math.sin(angle) * 120;
            const summary = summarizePoints(sensorPoints[sensor]);
            const selected = sensor === selectedSensor;
            const statusColor = `#${(statusColors[summary.status] ?? statusColors.Unknown).toString(16).padStart(6, '0')}`;
            const outlineColor = `#${(faultColors[summary.fault] ?? faultColors.healthy).toString(16).padStart(6, '0')}`;
            return `<g class="rig-marker ${selected ? 'selected' : ''}" data-rig-sensor="${sensor}" tabindex="0" role="button" aria-label="${sensor}: ${summary.status}, ${summary.fault}">
                <circle cx="${x}" cy="${y}" r="15" fill="${statusColor}" stroke="${outlineColor}" stroke-width="4" />
                <text x="${x}" y="${y + 30}" text-anchor="middle" fill="#eef5fb" font-size="12">${sensor}</text>
                <title>${sensor} • ${summary.status} • ${summary.fault}</title>
            </g>`;
        }).join('');
        target.innerHTML = `${target.dataset.error ? `<p class="muted">${escapeHtml(target.dataset.error)} Showing the 2D schematic fallback.</p>` : ''}
            <svg viewBox="0 0 760 410" role="group" aria-label="Interactive 2D schematic hydraulic rig fallback">
                <path class="rig-flow-path" d="M155 135 H270 V220 H410 V135 H550 V220 H640 V135 M270 220 H155 V275" fill="none" stroke="#62d0ff" stroke-width="12" stroke-linecap="round" stroke-linejoin="round" opacity=".6" />
                ${componentSvg}${sensorSvg}
            </svg>`;
        target.querySelectorAll('[data-rig-sensor]').forEach(marker => {
            marker.addEventListener('click', () => dispatchSensorSelection(marker.dataset.rigSensor));
            marker.addEventListener('keydown', event => {
                if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault();
                    dispatchSensorSelection(marker.dataset.rigSensor);
                }
            });
        });
    }

    function renderSensorRig(data, activeSensor) {
        const pointsBySensor = data?.sensors || {};
        sensorPoints = pointsBySensor;
        sensorMap = data?.sensor_map || sensorMap;
        selectedSensor = activeSensor || selectedSensor;
        if (!sensorMap?.sensors) return;
        const sensors = Object.keys(markerPositions).filter(sensor => sensorMap.sensors[sensor]);
        if (!sensors.length) return;
        const legendTarget = document.getElementById('rigSensorList');

        if (!renderer && !fallbackActive) initializeRenderer();
        if (renderer) {
            markerMeshes.forEach(marker => {
                const sensor = marker.userData.sensor;
                const summary = summarizePoints(sensorPoints[sensor]);
                marker.userData.status = summary.status;
                marker.material.color.setHex(statusColors[summary.status] ?? statusColors.Unknown);
                marker.children[0].material.color.setHex(faultColors[summary.fault] ?? faultColors.healthy);
                marker.scale.setScalar(sensor === selectedSensor ? 1.22 : 1);
            });
        }
        renderFallback(sensors);

        if (legendTarget) {
            legendTarget.innerHTML = sensors.map(sensor => {
                const summary = summarizePoints(sensorPoints[sensor]);
                return `<button type="button" class="rig-sensor-button ${sensor === selectedSensor ? 'selected' : ''}" data-rig-sensor="${sensor}">
                    <strong>${sensor}</strong><span>${summary.status}</span><small>${summary.fault}</small>
                </button>`;
            }).join('');
            legendTarget.querySelectorAll('[data-rig-sensor]').forEach(button => {
                button.addEventListener('click', () => dispatchSensorSelection(button.dataset.rigSensor));
            });
        }
        const detail = document.getElementById('rigSelection');
        if (detail && selectedSensor) {
            const metadata = sensorMap.sensors[selectedSensor] || {};
            detail.textContent = `${selectedSensor} (${metadata.sensor_type || 'sensor'}): ${metadata.location_area || 'not documented'}; ${metadata.documented_location_note || 'Physical location not documented.'} Select a marker to open its sensor plot and audit records for this cycle.`;
        }
    }

    window.renderSensorRig = renderSensorRig;
})();