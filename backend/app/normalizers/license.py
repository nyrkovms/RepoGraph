LICENSE_ALIASES: dict[str, str] = {
    "mit": "MIT",
    "mit license": "MIT",
    "apache 2.0": "Apache-2.0",
    "apache-2.0": "Apache-2.0",
    "bsd": "BSD-3-Clause",
    "bsd-3-clause": "BSD-3-Clause",
    "gplv3": "GPL-3.0-only",
    "gpl-3.0": "GPL-3.0-only",
}


def normalize_license(license_expression: str | None, classifiers: list[str], license_raw: str | None) -> str | None:
    if license_expression:
        return license_expression.strip() or None

    for classifier in classifiers:
        lower = classifier.lower()
        if "mit" in lower:
            return "MIT"
        if "apache" in lower and "2" in lower:
            return "Apache-2.0"
        if "bsd" in lower:
            return "BSD-3-Clause"

    if not license_raw:
        return None

    return LICENSE_ALIASES.get(license_raw.strip().lower())
