import { useEffect, useState } from 'react'
import Brand from './Brand'
import OpportunityPreview from './OpportunityPreview'
import { opportunitySignals, useCases, workflowSteps } from '../data'

const promptExamples = [
  'Find a product opportunity for boutique hotels in the Gulf',
  'Where could our compact power products solve a real need?',
  'Map a route into Japan for an independent-living product',
]

export default function LandingPage() {
  const [menuOpen, setMenuOpen] = useState(false)
  const [prompt, setPrompt] = useState('')

  useEffect(() => {
    const closeMenu = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setMenuOpen(false)
    }
    window.addEventListener('keydown', closeMenu)
    return () => window.removeEventListener('keydown', closeMenu)
  }, [])

  return (
    <div className="ophira-site">
      <header className="site-header">
        <nav className="site-nav" aria-label="Primary navigation">
          <a href="/" aria-label="OPHIRA AI home"><Brand /></a>
          <div className="site-links" data-open={menuOpen}>
            <a href="#product" onClick={() => setMenuOpen(false)}>Product</a>
            <a href="#workflow" onClick={() => setMenuOpen(false)}>How it works</a>
            <a href="#use-cases" onClick={() => setMenuOpen(false)}>Use cases</a>
            <a href="#manifesto" onClick={() => setMenuOpen(false)}>Manifesto</a>
            <a className="site-open-app" href="/app">Open Ophira <span aria-hidden="true">→</span></a>
          </div>
          <button
            className="site-menu"
            type="button"
            aria-expanded={menuOpen}
            aria-label={menuOpen ? 'Close menu' : 'Open menu'}
            onClick={() => setMenuOpen((open) => !open)}
          ><span /><span /></button>
        </nav>
      </header>

      <main>
        <section className="ophira-hero">
          <div className="hero-kicker"><span>AI global commerce system</span><i />For small teams that trade across borders</div>
          <h1>Discover what the<br />world needs.</h1>
          <p>Connect who can supply it.</p>

          <form className="opportunity-prompt" action="/app" method="get">
            <label htmlFor="opportunity-brief">What opportunity should Ophira explore?</label>
            <div>
              <input
                id="opportunity-brief"
                name="brief"
                value={prompt}
                onChange={(event) => setPrompt(event.target.value)}
                placeholder="Describe a product, market, capability, or customer..."
                autoComplete="off"
                required
              />
              <button type="submit">Explore <span aria-hidden="true">→</span></button>
            </div>
            <div className="prompt-examples" aria-label="Example opportunity prompts">
              {promptExamples.map((example) => (
                <button key={example} type="button" onClick={() => setPrompt(example)}>{example}</button>
              ))}
            </div>
          </form>

          <div className="hero-note"><span>Demand</span><span>Supply</span><span>Capital</span><span>Sales</span></div>
        </section>

        <section className="product-story" id="product">
          <div className="section-heading">
            <p>One commercial picture</p>
            <h2>One project truth.<br />Four dimensions of proof.</h2>
          </div>
          <OpportunityPreview />
        </section>

        <section className="workflow" id="workflow">
          <div className="workflow-intro">
            <p>How Ophira works</p>
            <h2>The intelligence lives in the connections.</h2>
            <span>Opportunity Lead keeps one shared project ledger, finds the weakest dimension, and delegates only the validation work that matters next.</span>
          </div>
          <ol className="workflow-list">
            {workflowSteps.map((step) => (
              <li key={step.number}>
                <span>{step.number}</span>
                <small>{step.label}</small>
                <div><h3>{step.title}</h3><p>{step.description}</p></div>
              </li>
            ))}
          </ol>
        </section>

        <section className="opportunity-gallery" aria-labelledby="opportunity-gallery-heading">
          <div className="section-heading compact">
            <p>Opportunity, in context</p>
            <h2 id="opportunity-gallery-heading">See why a market might fit—not only that it is growing.</h2>
          </div>
          <div className="opportunity-cards">
            {opportunitySignals.map((opportunity, index) => (
              <article key={opportunity.id} data-tone={opportunity.tone}>
                <div><span>{opportunity.region}</span><small>{String(index + 1).padStart(2, '0')}</small></div>
                <p>{opportunity.category}</p>
                <h3>{opportunity.title}</h3>
                <dl>
                  <div><dt>Signal</dt><dd>{opportunity.signal}</dd></div>
                  <div><dt>Weakest</dt><dd>{opportunity.weakestDimension} · {opportunity.nextMove}</dd></div>
                </dl>
              </article>
            ))}
          </div>
          <p className="illustrative-note">Illustrative workspace content. Real projects distinguish observed facts, user statements, community signals, and inference in a shared evidence ledger.</p>
        </section>

        <section className="use-cases" id="use-cases">
          <div className="section-heading compact">
            <p>Built for small teams</p>
            <h2>Know where to move before you move everything.</h2>
          </div>
          <div className="use-case-list">
            {useCases.map(([title, description], index) => (
              <article key={title}>
                <span>{String(index + 1).padStart(2, '0')}</span>
                <h3>{title}</h3>
                <p>{description}</p>
                <a href="/app" aria-label={`Explore OPHIRA ${title}`}>↗</a>
              </article>
            ))}
          </div>
        </section>

        <section className="manifesto" id="manifesto">
          <div>
            <p>Why OPHIRA</p>
            <h2>Value should travel to where it is genuinely needed.</h2>
          </div>
          <div className="manifesto-copy">
            <p>Ophira is not about chasing gold. It is about using wisdom to recognize resources, organize supply, and carry something valuable across distance.</p>
            <blockquote>Discover what the world needs.<br />Connect who can supply it.</blockquote>
          </div>
        </section>

        <section className="final-cta">
          <div><p>Start with one market question.</p><h2>Find the path worth building.</h2></div>
          <a href="/app">Open Ophira <span aria-hidden="true">→</span></a>
        </section>
      </main>

      <footer className="site-footer">
        <div><Brand /><p>AI global commerce intelligence for small cross-border teams.</p></div>
        <div><strong>Product</strong><a href="#product">Overview</a><a href="#workflow">Workflow</a><a href="/app">Open app</a></div>
        <div><strong>Company</strong><a href="#manifesto">Manifesto</a><a href="https://github.com/Janettbella69/Open-Acciowork" target="_blank" rel="noreferrer">GitHub ↗</a></div>
        <small>© 2026 OPHIRA AI</small>
      </footer>
    </div>
  )
}
