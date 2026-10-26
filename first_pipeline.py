
import csv

sensor_data = [
    70, 71, 72, 150, 73, 72, None, 71,
    71, 71, 71, 71
]

results = []

for i, value in enumerate(sensor_data):
    fault = "normal"
    reason = "Reading looks normal"

    if value is None:
        fault = "gap"
        reason = "Sensor reading is missing"

    elif value == 150:
        fault = "spike"
        reason = "Unusually high reading in this demo"

    elif i >= 3 and all(
        sensor_data[j] == value
        for j in range(i - 3, i + 1)
    ):
        fault = "possible_frozen_sensor"
        reason = "Same reading repeated four times"

    results.append({
        "reading_number": i + 1,
        "original_value": "" if value is None else value,
        "fault_label": fault,
        "reason": reason
    })

with open("detection_results.csv", "w", newline="") as file:
    columns = [
        "reading_number",
        "original_value",
        "fault_label",
        "reason"
    ]

    writer = csv.DictWriter(file, fieldnames=columns)
    writer.writeheader()
    writer.writerows(results)

print("SELF-HEALING PIPELINE")
print("-" * 35)

for item in results:
    if item["fault_label"] != "normal":
        print(
            f'Reading {item["reading_number"]}: '
            f'{item["fault_label"]} - {item["reason"]}'
        )

print("-" * 35)
print("Results saved to detection_results.csv")

