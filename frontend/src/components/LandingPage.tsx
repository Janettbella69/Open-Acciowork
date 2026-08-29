import { useEffect, useState } from 'react'
import Brand from './Brand'
import { useT } from '../i18n'
import { opportunitySignals, useCases } from '../data'

const promptExamples = [
  'Find products gaining demand on Amazon and TikTok Shop',
  'Compare suppliers, quotes, and landed margin for this product',
  'Localize this listing for the US, UK, and Germany',
  'Turn this product into a launch plan across Shopify and social',
]

const modes = ['For you', 'Products', 'Sourcing', 'Listings', 'Content', 'Store', 'Growth'] as const

const modePrompts: Record<(typeof modes)[number], string> = {
  'For you': '',
  Products: promptExamples[0],
  Sourcing: promptExamples[1],
  Listings: promptExamples[2],
  Content: promptExamples[3],
  Store: 'Audit my store for the next highest-impact improvement',
  Growth: 'Find the strongest growth experiment for this product and market',
}

const heroOpportunities = [
  ...opportunitySignals.map((opportunity, index) => ({
    ...opportunity,
    panel: heroPanel(index),
    prompt: promptExamples[index],
  })),
  {
    id: 'multichannel-launch',
    title: 'Launch across every channel',
    region: 'Store growth',
    panel: 'europe',
    prompt: promptExamples[3],
  },
] as const

const heroCardTitles = [
  'Find what sells',
  'Source with confidence',
  'Localize to convert',
  'Launch and grow',
] as const

function heroPanel(index: number) {
  return (['demand', 'route', 'sale'] as const)[index]
}

const storyChapters = [
  {
    panel: 'demand',
    title: 'Find what sells',
    description: 'TradeMind reads search trends, bestseller movement, reviews, creator posts, and competitor gaps together—so your next product starts with evidence instead of a guess.',
  },
  {
    panel: 'route',
    title: 'Source with confidence',
    description: 'Search factories, compare quotes, check MOQ and production risk, and calculate landed margin in one workspace before your team commits inventory or cash.',
  },
  {
    panel: 'sale',
    title: 'Launch and grow',
    description: 'Turn verified product facts into localized listings, SEO copy, launch creative, and store experiments—then keep watching the signals that should change your next move.',
  },
] as const

const decisionOutputs = [
  { title: 'Product trend radar', type: 'Product research', variant: 'demand' },
  { title: 'Supplier quote comparison', type: 'Sourcing table', variant: 'route' },
  { title: 'Landed margin model', type: 'Profit worksheet', variant: 'sale' },
  { title: 'Localized listing pack', type: 'Listing & SEO', variant: 'buyer' },
  { title: 'Launch creative kit', type: 'Content', variant: 'capital' },
  { title: 'Store growth board', type: 'Automation', variant: 'decision' },
] as const

const architectureFacts = [
  ['ONE', 'Commerce workspace', 'Research, suppliers, listings, content, and store work share the same context.'],
  ['TRUE', 'Landed margin', 'Quotes, fees, freight, ads, and returns stay visible beside revenue.'],
  ['READY', 'Publishable work', 'Move from research to listings, creative, tables, and outreach without starting over.'],
  ['LIVE', 'Always watching', 'Scheduled work can track competitor prices, reviews, and market changes.'],
] as const

export default function LandingPage() {
  const t = useT()
  const [menuOpen, setMenuOpen] = useState(false)
  const [prompt, setPrompt] = useState('')
  const [activeMode, setActiveMode] = useState<(typeof modes)[number]>('For you')

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
        <nav className="site-nav" aria-label={t('Primary navigation')}>
          <a href="/" aria-label={t('TradeMind home')}><Brand /></a>
          <div className="site-links" data-open={menuOpen}>
            <a href="#overview" onClick={() => setMenuOpen(false)}>{t('Overview')}</a>
            <a href="#workflow" onClick={() => setMenuOpen(false)}>{t('How it works')}</a>
            <a href="#product" onClick={() => setMenuOpen(false)}>{t('Product')}</a>
            <a href="#use-cases" onClick={() => setMenuOpen(false)}>{t('Use cases')}</a>
            <a href="#manifesto" onClick={() => setMenuOpen(false)}>{t('Principles')}</a>
          </div>
          <div className="site-nav-end">
            <a className="site-open-app" href="/app">{t('Open workspace')}</a>
            <button
              className="site-menu"
              type="button"
              aria-expanded={menuOpen}
              aria-label={menuOpen ? t('Close menu') : t('Open menu')}
              onClick={() => setMenuOpen((open) => !open)}
            ><span /><span /></button>
          </div>
        </nav>
      </header>

      <main>
        <section className="ophira-hero" id="overview">
          <div className="hero-copy">
            <h1>{t('Go beyond')} <em>{t('borders.')}</em></h1>
            <p>{t('From product idea to global sales.')}</p>
          </div>

          <form className="opportunity-prompt" action="/app" method="get">
            <input
              id="opportunity-brief"
              name="brief"
              value={prompt}
              onChange={(event) => setPrompt(event.target.value)}
              placeholder={t('Explore any market, product, or customer')}
              aria-label={t('What opportunity should TradeMind explore?')}
              autoComplete="off"
              required
            />
            <div className="prompt-bar">
              <div className="prompt-tools" aria-hidden="true">
                <span>＋</span><span>◇</span>
              </div>
              <button type="submit">{t('Start')} <span aria-hidden="true">→</span></button>
            </div>
          </form>

          <div className="commerce-modes" role="group" aria-label={t('Opportunity modes')}>
            {modes.map((mode) => (
              <button
                key={mode}
                type="button"
                aria-pressed={activeMode === mode}
                onClick={() => {
                  setActiveMode(mode)
                  setPrompt(t(modePrompts[mode]))
                }}
              >{t(mode)}</button>
            ))}
          </div>

          <div className="hero-recommendations" aria-label={t('Example opportunities')}>
            {heroOpportunities.map((opportunity, index) => (
              <button
                key={opportunity.id}
                type="button"
                data-panel={opportunity.panel}
                aria-label={t(opportunity.title)}
                onClick={() => setPrompt(t(opportunity.prompt))}
              >
                <span className="recommendation-art" aria-hidden="true" />
                <span className="recommendation-copy">
                  <strong>{t(heroCardTitles[index])}</strong>
                </span>
              </button>
            ))}
          </div>
        </section>

        <section className="studio-story" id="workflow">
          <h2>{t('Your AI commerce team')}</h2>
          <div className="story-grid">
            {storyChapters.map((chapter) => (
              <article key={chapter.title}>
                <div className="story-art" data-panel={chapter.panel}>
                  <h3>{t(chapter.title)}</h3>
                </div>
                <p>{t(chapter.description)}</p>
              </article>
            ))}
          </div>
        </section>

        <section className="product-intro" id="product">
          <h2>{t('Introduction to TradeMind')}</h2>
          <div className="trade-film">
            <div className="trade-film-copy">
              <span>{t('From product to profit')}</span>
              <h3>{t('Run the whole business.')}</h3>
              <p>{t('TradeMind keeps product research, sourcing, margin, listings, content, and store work in one living commerce workspace.')}</p>
            </div>
            <div className="trade-film-steps" aria-label={t('TradeMind commercial path')}>
              {['Discover', 'Source', 'Launch', 'Grow'].map((step, index) => (
                <span key={step}><small>{String(index + 1).padStart(2, '0')}</small>{t(step)}</span>
              ))}
            </div>
            <a href="/app" aria-label={t('Open TradeMind workspace')}><span aria-hidden="true">↗</span></a>
          </div>
        </section>

        <section className="decision-gallery" aria-labelledby="decision-gallery-heading">
          <div className="gallery-heading">
            <h2 id="decision-gallery-heading">{t('Made for real decisions')}</h2>
            <p>{t('Research your team can question, refine, and turn into action.')}</p>
          </div>
          <div className="decision-grid">
            {decisionOutputs.map((output) => (
              <a href="/app" key={output.title}>
                <div className="decision-cover" data-variant={output.variant} aria-hidden="true">
                  {output.variant === 'demand' && <><span>MARKET</span><em>SIGNAL</em></>}
                  {output.variant === 'route' && <><span>ROUTE</span><em>MAP</em></>}
                  {output.variant === 'sale' && <><span>BUYER</span><em>TEST</em></>}
                  {output.variant === 'buyer' && <><span>BUYER</span><span>LANGUAGE</span></>}
                  {output.variant === 'capital' && <><span>FIRST</span><em>ORDER</em></>}
                  {output.variant === 'decision' && <><span>NEXT</span><span>MOVE ↗</span></>}
                </div>
                <h3>{t(output.title)}</h3>
                <p>{t(output.type)}</p>
              </a>
            ))}
          </div>
        </section>

        <section className="manifesto-banner" id="manifesto">
          <div className="manifesto-image" aria-hidden="true" />
          <h2>{t('One product idea can become a business that sells around the world.')}</h2>
        </section>

        <section className="use-cases" id="use-cases">
          <div className="gallery-heading">
            <h2>{t('Made for cross-border sellers')}</h2>
            <p>{t('From first product search to the next thousand orders.')}</p>
          </div>
          <div className="role-strip">
            {useCases.map(([title, description], index) => (
              <a href="/app" key={title} data-tone={String(index + 1)}>
                <span>{String(index + 1).padStart(2, '0')}</span>
                <div><h3>{t(title)}</h3><p>{t(description)}</p></div>
                <i aria-hidden="true">↗</i>
              </a>
            ))}
          </div>
        </section>

        <section className="architecture" aria-labelledby="architecture-heading">
          <div className="architecture-intro">
            <h2 id="architecture-heading">{t('Commerce, connected')}</h2>
            <p>{t('Product research is only useful when it can become a supplier shortlist, a margin model, a listing, and a store experiment. TradeMind keeps that work connected from first signal to repeat sale.')}</p>
          </div>
          <div className="architecture-grid">
            {architectureFacts.map(([value, label, description]) => (
              <article key={value}>
                <small>{t(label)}</small>
                <strong>{t(value)}</strong>
                <p>{t(description)}</p>
              </article>
            ))}
          </div>
        </section>

        <section className="final-cta">
          <h2>{t('Go beyond')} <em>{t('borders.')}</em></h2>
          <a href="/app">{t('Start now')} <span aria-hidden="true">→</span></a>
        </section>
      </main>

      <footer className="site-footer">
        <div><Brand /><p>{t('AI global commerce intelligence for cross-border teams.')}</p></div>
        <div><strong>{t('Product')}</strong><a href="#overview">{t('Overview')}</a><a href="#workflow">{t('How it works')}</a><a href="/app">{t('Open workspace')}</a></div>
        <div><strong>{t('Company')}</strong><a href="#manifesto">{t('Principles')}</a><a href="https://github.com/Janettbella69/Open-Acciowork" target="_blank" rel="noreferrer">GitHub ↗</a></div>
        <small>© 2026 TradeMind</small>
      </footer>
    </div>
  )
}
