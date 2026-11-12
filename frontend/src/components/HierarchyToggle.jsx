export default function HierarchyToggle({ expanded, ...props }) {
  return <button {...props} className="om-toggle" type="button" aria-expanded={expanded}>
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="m9 5 7 7-7 7" /></svg>
  </button>
}
