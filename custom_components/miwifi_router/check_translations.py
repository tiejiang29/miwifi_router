#!/usr/bin/env python3
"""Diagnostic script to verify translations are loadable.

Run this script INSIDE the Home Assistant container (or where HA can read it)
to check if the translations files are syntactically valid and properly
structured for HA to load.

Usage (in HA container/SSH add-on):
    python3 /config/custom_components/miwifi_router/check_translations.py

Or run locally to just check JSON syntax:
    python3 check_translations.py
"""

import json
import os
import sys


def check_translations_dir(integration_dir: str) -> int:
    """Check translations/ directory of an integration."""
    print(f"Checking integration at: {integration_dir}")
    print()

    # Check manifest.json
    manifest_path = os.path.join(integration_dir, "manifest.json")
    if not os.path.isfile(manifest_path):
        print(f"[FAIL] manifest.json not found at {manifest_path}")
        return 1

    with open(manifest_path) as f:
        manifest = json.load(f)

    domain = manifest.get("domain")
    name = manifest.get("name")
    version = manifest.get("version")
    print(f"[OK] manifest.json: domain={domain}, name={name}, version={version}")

    if not domain:
        print("[FAIL] manifest.json missing 'domain' field")
        return 1

    # Check translations/ directory
    translations_dir = os.path.join(integration_dir, "translations")
    if not os.path.isdir(translations_dir):
        print(f"[FAIL] translations/ directory not found at {translations_dir}")
        print("       Custom integrations MUST have a translations/ directory")
        print("       with at least en.json for translations to work.")
        return 1

    print(f"[OK] translations/ directory exists")

    # List translation files
    translation_files = sorted(
        f for f in os.listdir(translations_dir) if f.endswith(".json")
    )
    if not translation_files:
        print("[FAIL] No .json files in translations/ directory")
        return 1

    print(f"[OK] Found {len(translation_files)} translation file(s):")
    for f in translation_files:
        print(f"       - {f}")

    # en.json is required as fallback
    if "en.json" not in translation_files:
        print("[WARN] en.json not found — English fallback missing")
        print("       Users with non-translated languages will see raw keys")

    # Check each translation file
    print()
    print("Validating translation file contents:")
    print()
    errors = 0
    for filename in translation_files:
        filepath = os.path.join(translations_dir, filename)
        try:
            with open(filepath) as f:
                data = json.load(f)
            print(f"[OK] {filename}: valid JSON")

            # Check required structure
            required_keys = ["config", "options"]
            for key in required_keys:
                if key not in data:
                    print(f"  [WARN] {filename}: missing top-level key '{key}'")

            # Check config.step structure
            config_steps = data.get("config", {}).get("step", {})
            for step_id in ["user"]:
                if step_id not in config_steps:
                    print(f"  [WARN] {filename}: missing config.step.{step_id}")

            # Check options.step structure
            options_steps = data.get("options", {}).get("step", {})
            for step_id in ["init", "confirm_unit_change"]:
                if step_id not in options_steps:
                    print(f"  [WARN] {filename}: missing options.step.{step_id}")
                else:
                    step = options_steps[step_id]
                    if "title" not in step:
                        print(f"  [WARN] {filename}: options.step.{step_id} missing 'title'")
                    if "data" not in step:
                        print(f"  [WARN] {filename}: options.step.{step_id} missing 'data'")

            # Print the actual title for init step (for verification)
            init_step = options_steps.get("init", {})
            init_title = init_step.get("title", "(missing)")
            print(f"  options.step.init.title = {init_title!r}")

            # Print confirm_unit_change step details
            confirm_step = options_steps.get("confirm_unit_change", {})
            confirm_title = confirm_step.get("title", "(missing)")
            confirm_desc = confirm_step.get("description", "(missing)")
            print(f"  options.step.confirm_unit_change.title = {confirm_title!r}")
            if "{changes}" not in confirm_desc:
                print(f"  [WARN] confirm_unit_change.description missing {{changes}} placeholder")
            else:
                print(f"  [OK] confirm_unit_change.description has {{changes}} placeholder")

        except json.JSONDecodeError as e:
            print(f"[FAIL] {filename}: invalid JSON — {e}")
            errors += 1

    print()
    if errors == 0:
        print("=" * 60)
        print("[SUCCESS] All translation files are valid!")
        print()
        print("If translations still don't show in HA, try:")
        print("  1. Fully restart Home Assistant (not just reload integration)")
        print("  2. Hard refresh browser (Ctrl+Shift+R or Cmd+Shift+R)")
        print("  3. Check HA language setting (Profile → Language)")
        print("  4. Check HA logs for translation loading errors")
        print("=" * 60)
        return 0
    else:
        print("=" * 60)
        print(f"[FAIL] {errors} translation file(s) have errors")
        print("=" * 60)
        return 1


def main():
    # Try to find the integration directory
    possible_paths = [
        # When run from inside the integration directory
        ".",
        # When run from custom_components/
        "miwifi_router",
        # HA typical paths
        "/config/custom_components/miwifi_router",
        "/homeassistant/custom_components/miwifi_router",
    ]

    # Also accept path as command-line argument
    if len(sys.argv) > 1:
        possible_paths.insert(0, sys.argv[1])

    integration_dir = None
    for path in possible_paths:
        if os.path.isfile(os.path.join(path, "manifest.json")):
            integration_dir = os.path.abspath(path)
            break

    if not integration_dir:
        print("Could not find integration directory (with manifest.json)")
        print("Tried:")
        for p in possible_paths:
            print(f"  - {p}")
        print()
        print("Usage: python3 check_translations.py [path_to_integration_dir]")
        return 1

    return check_translations_dir(integration_dir)


if __name__ == "__main__":
    sys.exit(main())
