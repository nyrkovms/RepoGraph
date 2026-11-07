import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";

import { searchPackages } from "./api";
import { getPackageDescriptionPreview } from "./packageDescription";
import { displayName, ecosystemLabel, itemKey, nativeName, packageHref } from "./packageNames";

const AUTOCOMPLETE_LIMIT = 8;

// The query field with autocomplete. Enter without a highlighted suggestion calls `onSubmit`; a suggestion opens its
// package. `ecosystem` narrows the suggestions.
export default function SearchBox({ value, onChange, onSubmit, placeholder, ariaLabel = "Search query", ecosystem = "" }) {
  const navigate = useNavigate();
  const autocompleteRequestId = useRef(0);

  const [found, setSuggestions] = useState([]);
  const [open, setSuggestionsOpen] = useState(false);
  const [highlightedIndex, setHighlightedIndex] = useState(-1);
  // An emptied field shows nothing, whatever the last answer was.
  const hasQuery = Boolean(value.trim());
  const suggestions = hasQuery ? found : [];
  const suggestionsOpen = hasQuery && open;

  useEffect(() => {
    const trimmedQuery = value.trim();
    if (!trimmedQuery) {
      return;
    }

    const requestId = autocompleteRequestId.current + 1;
    autocompleteRequestId.current = requestId;

    const timeoutId = window.setTimeout(async () => {
      try {
        const payload = await searchPackages({
          q: trimmedQuery,
          limit: AUTOCOMPLETE_LIMIT,
          sort: "relevance",
          ...(ecosystem ? { ecosystem } : {}),
        });
        if (autocompleteRequestId.current !== requestId) {
          return;
        }
        setSuggestions(payload.data || []);
        setSuggestionsOpen(Boolean(payload.data?.length));
        setHighlightedIndex((payload.data || []).length ? 0 : -1);
      } catch {
        if (autocompleteRequestId.current !== requestId) {
          return;
        }
        setSuggestions([]);
        setSuggestionsOpen(false);
        setHighlightedIndex(-1);
      }
    }, 250);

    return () => window.clearTimeout(timeoutId);
  }, [value, ecosystem]);

  function closeSuggestions() {
    setSuggestionsOpen(false);
    setHighlightedIndex(-1);
  }

  function selectSuggestion(item) {
    onChange(item.ecosystem === "maven" || item.ecosystem === "rpm" ? nativeName(item) : item.normalized_name);
    closeSuggestions();
    navigate(packageHref(item));
  }

  function handleSubmit() {
    closeSuggestions();
    onSubmit();
  }

  function handleInputKeyDown(event) {
    if (!suggestionsOpen || !suggestions.length) {
      if (event.key === "Enter") {
        event.preventDefault();
        handleSubmit();
      }
      return;
    }

    if (event.key === "ArrowDown") {
      event.preventDefault();
      setHighlightedIndex((current) => (current + 1) % suggestions.length);
      return;
    }

    if (event.key === "ArrowUp") {
      event.preventDefault();
      setHighlightedIndex((current) => (current <= 0 ? suggestions.length - 1 : current - 1));
      return;
    }

    if (event.key === "Escape") {
      event.preventDefault();
      closeSuggestions();
      return;
    }

    if (event.key === "Enter") {
      event.preventDefault();
      if (highlightedIndex >= 0 && highlightedIndex < suggestions.length) {
        selectSuggestion(suggestions[highlightedIndex]);
      } else {
        handleSubmit();
      }
    }
  }

  return (
    <div className="autocomplete">
      <input
        placeholder={placeholder}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={handleInputKeyDown}
        onFocus={() => {
          if (suggestions.length) {
            setSuggestionsOpen(true);
          }
        }}
        onBlur={() => {
          window.setTimeout(() => closeSuggestions(), 120);
        }}
        aria-label={ariaLabel}
        autoComplete="off"
      />
      {suggestionsOpen && suggestions.length ? (
        <ul className="autocomplete-list" role="listbox" aria-label="Search suggestions">
          {suggestions.map((item, index) => (
            <li key={itemKey(item)}>
              <button
                type="button"
                className={index === highlightedIndex ? "autocomplete-item active" : "autocomplete-item"}
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => selectSuggestion(item)}
              >
                <span>
                  <b>{displayName(item)}</b>
                  <span className="tag eco-badge">{ecosystemLabel(item.ecosystem)}</span>
                  <span className="muted autocomplete-version">{item.latest_version}</span>
                </span>
                {item.description ? (
                  <span className="muted autocomplete-description">{getPackageDescriptionPreview(item.description, 120)}</span>
                ) : null}
              </button>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
