"""Every knob of the synthetic trial, in one place.

These numbers are invented to produce a realistic-looking dataset with known answers (ADR-004).
They are not physiological reference values and must not be read as such.
"""

SEED = 20261024

STUDY_CODE = "DEMO-STUDY-01"
PROTOCOL_VERSION = "1.0"

# code, country, IANA time zone, subjects enrolled
SITES = [
    ("SITE-A", "US", "America/Los_Angeles", 25),
    ("SITE-B", "US", "America/New_York", 25),
    ("SITE-C", "DE", "Europe/Berlin", 25),
    ("SITE-D", "JP", "Asia/Tokyo", 25),
]

DEVICE_MODEL = {"model_code": "ARRAY-256", "electrode_count": 256, "grid_cols": 16, "pitch_um": 400.0}

# code, manufactured_on, process revision, serial prefix, mean impedance drift (log units per 180 days).
# The drift is ground truth only: it is never written to raw/.
LOTS = [
    ("LOT-A", "2025-05-12", "P3", "DEV-1", 0.10),
    ("LOT-B", "2025-06-30", "P3", "DEV-2", 0.12),
    ("LOT-C", "2025-08-18", "P4", "DEV-3", 0.35),   # the planted signal: lot C drifts faster
]

# code, target day after implant, window (+/- days)
VISITS = [("D7", 7, 2), ("D30", 30, 5), ("D90", 90, 14), ("D180", 180, 21)]

IMPLANT_FIRST = "2025-09-01"
IMPLANT_LAST = "2026-03-31"
ENROLL_BEFORE_IMPLANT_DAYS = (7, 28)
BIRTH_YEARS = (1950, 1985)
WITHDRAWN_SUBJECTS = 3                  # withdraw after D90, so they have no D180 data

# Impedance at 1 kHz, kOhm. log Z = log Z0(electrode) + slope(implant) * day / 180 + noise
IMPEDANCE_BASELINE_MEDIAN = 8.0
IMPEDANCE_ELECTRODE_SD = 0.25           # between electrodes of one array, log scale
IMPEDANCE_IMPLANT_SLOPE_SD = 0.10       # between implants of the same lot, log units per 180 days
IMPEDANCE_NOISE_SD = 0.05               # visit to visit
OPEN_AT_IMPLANT_P = 0.01                # electrode broken from the start
OPEN_LATER_P = 0.005                    # per electrode, per visit after the first
OPEN_KOHM_RANGE = (800.0, 3000.0)

# Perception threshold, uA. Open electrodes cannot be stimulated, so they get no threshold row.
THRESHOLD_MEDIAN = 150.0
THRESHOLD_ELECTRODE_SD = 0.35
THRESHOLD_NOISE_SD = 0.08
NO_RESPONSE_PERSISTENT_P = 0.04         # never respond, at any visit
NO_RESPONSE_TRANSIENT_P = 0.01          # miss one visit
MAX_CURRENT_UA = 1000.0
PULSE_WIDTH_US = 450
THRESHOLD_METHOD = "staircase"

# Stimulation configs: v1 after D7, v2 after D30, v3 after D90 for some implants.
CONFIG_V3_P = 0.6
CONFIG_MAX_FACTOR = 2.5                 # max_ua = threshold * factor, capped at MAX_CURRENT_UA

ADVERSE_EVENTS = 14
AE_TERMS = ["Implant site pain", "Headache", "Implant site inflammation",
            "Stimulation discomfort", "Implant site infection", "Dizziness"]

EXPORT_TOOL = "fitsuite 3.2.0"          # made-up fitting software
FORMAT_VERSION = "1"
