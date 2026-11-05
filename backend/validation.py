import re
import unicodedata
from urllib.parse import urlsplit


def text_width(text):
    return sum(2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
               for char in unicodedata.normalize("NFC", text))


def valid_url(url):
    try:
        parsed = urlsplit(url)
        return (parsed.scheme in {"http", "https"} and bool(parsed.hostname)
                and not parsed.username and not parsed.password
                and not any(char.isspace() or ord(char) < 32 for char in url)
                and len(url) <= 2048 and (parsed.port or 0) >= 0)
    except ValueError:
        return False


def validate(creative):
    issues = []
    for label, assets, minimum, maximum, limit, prefix in (
        ("Headline", creative.headlines, 3, 15, 30, "HEADLINE"),
        ("Description", creative.descriptions, 2, 4, 90, "DESCRIPTION"),
    ):
        if not minimum <= len(assets) <= maximum:
            issues.append(f"Provide {minimum}–{maximum} {label.lower()}s.")
        pins = {f"{prefix}_{position}" for position in range(1, minimum + 1)}
        seen, filled = set(), set()
        unpinned = 0
        for i, asset in enumerate(assets, 1):
            text = asset.text.strip()
            if not text or text_width(asset.text) > limit:
                issues.append(f"{label} {i} must contain 1–{limit} character units.")
            key = unicodedata.normalize("NFKC", " ".join(text.split())).casefold()
            if key in seen:
                issues.append(f"{label} {i} duplicates an earlier asset.")
            seen.add(key)
            if not asset.pin:
                unpinned += 1
            elif asset.pin in pins:
                filled.add(asset.pin)
            else:
                issues.append(f"{label} {i} has an unsupported pin.")
        if filled and len(pins - filled) > unpinned:
            issues.append(f"Adjust {label.lower()} pins so every position can be filled.")
    if not creative.final_urls:
        issues.append("Provide at least one final URL.")
    for i, url in enumerate(creative.final_urls, 1):
        if not valid_url(url):
            issues.append(f"Final URL {i} must be an absolute HTTP or HTTPS URL.")
    for i, path in enumerate((creative.path1, creative.path2), 1):
        if text_width(path) > 15 or re.search(r"[\s/]", path):
            issues.append(f"Display path {i} allows 15 character units, without spaces or slashes.")
    if creative.path2 and not creative.path1:
        issues.append("Enter display path 1 before display path 2.")
    return issues
