/**
 * Neutral placeholder page.
 *
 * Its only job is to prove the starter renders, that Tailwind is wired, and
 * that the responsive + semantic-HTML baseline works. It contains no business
 * content, no invented claims, and no product-specific design decisions —
 * a generated project replaces this file entirely.
 */
export default function App() {
  return (
    <>
      <a className="skip-link" href="#main">
        Skip to content
      </a>

      <header className="border-b border-[var(--color-hairline)]">
        <div className="mx-auto flex max-w-5xl flex-col gap-3 px-4 py-4 sm:flex-row sm:items-center sm:justify-between sm:px-6">
          <span className="text-sm font-semibold tracking-tight">Frontend Starter</span>
          <nav aria-label="Primary">
            <ul className="flex flex-wrap gap-4 text-sm text-[var(--color-muted)]">
              <li>
                <a href="#stack">Stack</a>
              </li>
              <li>
                <a href="#baseline">Baseline</a>
              </li>
            </ul>
          </nav>
        </div>
      </header>

      <main id="main" className="mx-auto max-w-5xl px-4 py-12 sm:px-6 sm:py-16">
        <h1 className="text-3xl font-semibold tracking-tight sm:text-4xl">Starter is running</h1>
        <p className="mt-4 max-w-2xl text-base text-[var(--color-muted)] sm:text-lg">
          This is a placeholder page. Replace it with the generated website. Development runs with{' '}
          <code>npm run dev</code>; production output is built with <code>npm run build</code>.
        </p>

        <section aria-labelledby="stack" className="mt-12">
          <h2 id="stack" className="text-xl font-semibold tracking-tight">
            Fixed toolchain
          </h2>
          <ul className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            {['React', 'Vite', 'TypeScript', 'Tailwind CSS'].map(item => (
              <li key={item} className="rounded-lg border border-[var(--color-hairline)] px-4 py-3 text-sm font-medium">
                {item}
              </li>
            ))}
          </ul>
        </section>

        <section aria-labelledby="baseline" className="mt-12">
          <h2 id="baseline" className="text-xl font-semibold tracking-tight">
            Included baseline
          </h2>
          <ul className="mt-4 list-disc space-y-2 pl-5 text-sm text-[var(--color-muted)]">
            <li>Responsive layout defaults and a fluid, mobile-first container.</li>
            <li>Semantic landmarks: header, nav, main, section, footer.</li>
            <li>Minimal global reset with visible keyboard focus and a skip link.</li>
            <li>Reduced-motion and light/dark colour-scheme handling.</li>
          </ul>
        </section>
      </main>

      <footer className="border-t border-[var(--color-hairline)]">
        <div className="mx-auto max-w-5xl px-4 py-6 text-sm text-[var(--color-muted)] sm:px-6">Placeholder footer.</div>
      </footer>
    </>
  )
}
