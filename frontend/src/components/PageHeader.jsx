export default function PageHeader({ breadcrumb, title, subtitle, actions }) {
  return (
    <div className="page-header admin-header app-page-header">
      <div className="app-page-header-copy">
        <p className="breadcrumb">{breadcrumb}</p>
        <h1 className="app-page-header-title">{title}</h1>
        {subtitle && <p className="app-page-header-subtitle">{subtitle}</p>}
      </div>
      {actions && <div className="page-header-actions">{actions}</div>}
    </div>
  )
}
