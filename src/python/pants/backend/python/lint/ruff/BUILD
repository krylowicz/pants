# Copyright 2023 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

python_sources()

python_tests(
    name="tests",
    overrides={
        "rules_integration_test.py": {
            "timeout": 330,
            "tags": ["platform_specific_behavior"],
        },
    },
)
