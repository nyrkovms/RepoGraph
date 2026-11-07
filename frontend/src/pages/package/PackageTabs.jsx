export const TAB_PANEL_ID = "package-tabpanel";

const tabButtonId = (id) => `package-tab-${id}`;

// Tabs with automatic activation: the arrow keys, Home and End move the selection and the focus together.
export function PackageTabs({ tabs, active, onSelect, children }) {
  function onKeyDown(event) {
    const index = tabs.findIndex((tab) => tab.id === active);
    let next = -1;
    if (event.key === "ArrowRight") {
      next = (index + 1) % tabs.length;
    } else if (event.key === "ArrowLeft") {
      next = (index - 1 + tabs.length) % tabs.length;
    } else if (event.key === "Home") {
      next = 0;
    } else if (event.key === "End") {
      next = tabs.length - 1;
    }
    if (next < 0) {
      return;
    }
    event.preventDefault();
    onSelect(tabs[next].id);
    document.getElementById(tabButtonId(tabs[next].id))?.focus();
  }

  return (
    <>
      <div className="tabs" role="tablist" aria-label="Package sections" onKeyDown={onKeyDown}>
        {tabs.map((tab) => (
          <button
            key={tab.id}
            type="button"
            role="tab"
            id={tabButtonId(tab.id)}
            className="tab"
            aria-selected={tab.id === active}
            aria-controls={TAB_PANEL_ID}
            tabIndex={tab.id === active ? 0 : -1}
            onClick={() => onSelect(tab.id)}
          >
            {tab.label}
            {tab.count !== undefined && tab.count !== null ? <span className="tab-count">{tab.count}</span> : null}
          </button>
        ))}
      </div>
      <div className="tab-panel" role="tabpanel" id={TAB_PANEL_ID} aria-labelledby={tabButtonId(active)}>
        {children}
      </div>
    </>
  );
}
