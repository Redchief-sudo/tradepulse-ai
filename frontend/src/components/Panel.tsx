import type { ReactNode } from 'react'

export function Panel({
  title,
  error,
  loading,
  children,
}: {
  title: string
  error?: string | null
  loading?: boolean
  children: ReactNode
}) {
  return (
    <section className="panel">
      <h2>
        {title}
        {loading ? <span className="panel-loading" title="refreshing" /> : null}
      </h2>
      {error ? <div className="panel-error">{error}</div> : null}
      <div className="panel-body">{children}</div>
    </section>
  )
}

/** Consistent "nothing to show" presentation -- replaces each panel's own ad
 * hoc `<p className="muted">` string with a single shared convention. */
export function EmptyState({ children }: { children: ReactNode }) {
  return <p className="empty-state">{children}</p>
}
