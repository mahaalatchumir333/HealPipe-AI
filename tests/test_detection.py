import numpy as np
import pandas as pd

from pipeline.detect import SENSORS, detect_faults


class SensorExpectation:
    def __init__(self, sensor):
        self.sensor = sensor

    def predict(self, features):
        if self.sensor == "PS1":
            return features["second_in_cycle"].to_numpy() * 0.2
        return np.zeros(len(features))


def test_detects_and_reconstructs_faults_with_status_and_confidence():
    frame = pd.DataFrame({
        "cycle_id": [1] * 60,
        "second_in_cycle": np.arange(60),
    })
    for sensor in SENSORS:
        frame[sensor] = 0.0

    frame.loc[10:17, "PS1"] = 10.0
    frame.loc[24, "PS2"] = 50.0
    frame.loc[30:49, "PS3"] = np.linspace(0.0, 2.0, 20)
    frame.loc[5, "FS1"] = np.nan
    models = {sensor: SensorExpectation(sensor) for sensor in SENSORS}

    results = detect_faults(frame, models)
    faults = set(results.loc[results["fault_type"] != "healthy", "fault_type"])

    assert {"freeze", "spike", "drift", "missing"}.issubset(faults)
    modified = results[results["modified"]]
    assert not modified.empty
    assert modified["confidence"].between(0, 100).all()
    assert modified["status"].isin({"Estimated", "Corrected", "Unresolved"}).all()
    assert (modified.loc[modified["fault_type"] == "missing", "status"] == "Estimated").all()
    assert modified["reconstructed_value"].notna().all()
