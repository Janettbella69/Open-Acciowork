export type SkillTemplate = {
  id: string
  code: string
  name: string
  description: string
  prompt: string
  category: string
  source: 'Built-in' | 'Local draft'
}

export const builtInSkills: SkillTemplate[] = [
  {
    id: 'market-signal-radar',
    code: 'MR',
    name: 'Market Signal Radar',
    description: 'Track material shifts in buyer needs, policy, category behavior, and demand evidence.',
    prompt: 'Use Market Signal Radar to identify material shifts in buyer needs, policy, category behavior, and demand evidence for this opportunity.',
    category: 'Research',
    source: 'Built-in',
  },
  {
    id: 'supplier-capability-map',
    code: 'SM',
    name: 'Supplier Capability Map',
    description: 'Compare supplier constraints, production readiness, quality risk, and market fit.',
    prompt: 'Use Supplier Capability Map to compare supplier constraints, production readiness, quality risk, and market fit for this opportunity.',
    category: 'Supply',
    source: 'Built-in',
  },
  {
    id: 'downside-boundary',
    code: 'DB',
    name: 'Downside Boundary',
    description: 'Identify cash exposure, stop conditions, and assumptions that require validation.',
    prompt: 'Use Downside Boundary to identify cash exposure, stop conditions, and assumptions that require validation before we proceed.',
    category: 'Capital',
    source: 'Built-in',
  },
  {
    id: 'buyer-test-designer',
    code: 'BT',
    name: 'Buyer Test Designer',
    description: 'Turn sales assumptions into the smallest credible and falsifiable buyer test.',
    prompt: 'Use Buyer Test Designer to turn our sales assumptions into the smallest credible and falsifiable buyer test.',
    category: 'Sales',
    source: 'Built-in',
  },
  {
    id: 'evidence-quality-review',
    code: 'EQ',
    name: 'Evidence Quality Review',
    description: 'Separate facts, user statements, community signals, and inference before decisions.',
    prompt: 'Use Evidence Quality Review to separate observed facts, user statements, community signals, and inference before making a recommendation.',
    category: 'Governance',
    source: 'Built-in',
  },
  {
    id: 'competitor-change-watch',
    code: 'CW',
    name: 'Competitor Change Watch',
    description: 'Review material product, pricing, positioning, and channel changes over time.',
    prompt: 'Use Competitor Change Watch to review material product, pricing, positioning, and channel changes that affect this opportunity.',
    category: 'Monitoring',
    source: 'Built-in',
  },
]
