const SECTION_RULE_RE = /^[=~`^"*:#-]{3,}\s*$/;
const TABLE_ROW_RE = /^[|+].*[|+]$/;

function cleanInlineMarkup(value) {
  return value
    .replace(/!\[[^\]]*]\(([^)]+)\)/g, "")
    .replace(/\[([^\]]+)]\(([^)]+)\)/g, "$1")
    .replace(/`([^`]+?)\s*<[^>]+>`_/g, "$1")
    .replace(/`([^`]+?)`_/g, "$1")
    .replace(/:[a-z_]+:`([^`]+)`/gi, "$1")
    .replace(/``([^`]+)``/g, "$1")
    .replace(/`([^`]+)`/g, "$1")
    .replace(/\*\*([^*]+)\*\*/g, "$1")
    .replace(/__([^_]+)__/g, "$1")
    .replace(/\*([^*]+)\*/g, "$1")
    .replace(/_([^_]+)_/g, "$1")
    .replace(/\s+/g, " ")
    .trim();
}

function normalizePackageDescription(raw) {
  if (!raw) {
    return "";
  }

  const lines = raw.replace(/\r/g, "").split("\n");
  const normalized = [];
  let skipIndentedDirectiveBlock = false;

  for (const line of lines) {
    const trimmed = line.trim();

    if (skipIndentedDirectiveBlock) {
      if (!trimmed || /^\s+/.test(line)) {
        continue;
      }
      skipIndentedDirectiveBlock = false;
    }

    if (!trimmed) {
      if (normalized[normalized.length - 1] !== "") {
        normalized.push("");
      }
      continue;
    }

    if (trimmed.startsWith(".. ")) {
      skipIndentedDirectiveBlock = true;
      continue;
    }

    if (SECTION_RULE_RE.test(trimmed) || TABLE_ROW_RE.test(trimmed)) {
      continue;
    }

    normalized.push(cleanInlineMarkup(trimmed));
  }

  return normalized.join("\n").replace(/\n{3,}/g, "\n\n").trim();
}

export function getPackageDescriptionParagraphs(raw, maxParagraphs = 6) {
  const normalized = normalizePackageDescription(raw);
  if (!normalized) {
    return [];
  }

  return normalized
    .split(/\n{2,}/)
    .map((paragraph) => paragraph.replace(/\s+/g, " ").trim())
    .filter(Boolean)
    .slice(0, maxParagraphs);
}

export function getPackageDescriptionPreview(raw, maxLength = 140) {
  const text = getPackageDescriptionParagraphs(raw, 2).join(" ");
  if (!text) {
    return "";
  }

  if (text.length <= maxLength) {
    return text;
  }

  const shortened = text.slice(0, maxLength - 1).trimEnd();
  const boundaryIndex = shortened.lastIndexOf(" ");
  return `${(boundaryIndex > 60 ? shortened.slice(0, boundaryIndex) : shortened).trimEnd()}…`;
}
