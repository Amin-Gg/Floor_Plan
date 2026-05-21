"""
File utility functions for output management.
"""
import os
import json
import uuid
import logging
from config.constants import IMAGES_OUTPUT_DIR, JSON_OUTPUT_DIR

logger = logging.getLogger(__name__)


def _unique_filename(prefix: str, ext: str) -> str:
    """
    Generate a concurrency-safe filename using a UUID suffix.
    Sequential counters (plan1, plan2...) are not safe when multiple Gunicorn
    workers scan the directory simultaneously — they can generate the same number.
    UUID suffixes guarantee uniqueness without filesystem coordination.
    """
    return f"{prefix}_{uuid.uuid4().hex[:8]}.{ext}"


# Kept for backward compatibility — routes that call getNextTestNumber()
# still work; they now receive a UUID-based unique string.
def getNextTestNumber() -> str:
    return uuid.uuid4().hex[:8]


def saveJsonToFile(json_data: dict, custom_name: str = None) -> str:
    """Save JSON data to the JSON output directory. Returns the filename."""
    filename = f"{custom_name}.json" if custom_name else _unique_filename("plan", "json")
    filepath = os.path.join(JSON_OUTPUT_DIR, filename)
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(json_data, f, indent=2, ensure_ascii=False)
        logger.info("JSON saved to: %s", filepath)
        return filename
    except OSError as exc:
        logger.error("Error saving JSON file: %s", exc)
        return None


def saveAccuracyAnalysis(accuracy_data: dict, test_num: str) -> str:
    """Save accuracy analysis JSON to the output directory."""
    filename = f"acc_{test_num}.json"
    filepath = os.path.join(JSON_OUTPUT_DIR, filename)
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(accuracy_data, f, indent=2, ensure_ascii=False)
        logger.info("Accuracy analysis saved to: %s", filepath)
        return filename
    except OSError as exc:
        logger.error("Error saving accuracy analysis: %s", exc)
        return None
