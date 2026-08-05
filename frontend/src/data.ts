export type Opportunity = {
  id: string
  region: string
  category: string
  title: string
  signal: string
  supplyFit: string
  route: string
  capitalQuestion: string
  salesTest: string
  weakestDimension: 'Demand' | 'Supply' | 'Capital' | 'Sales'
  nextMove: string
  tone: 'gold' | 'blue' | 'green'
  tags: string[]
}

export const opportunitySignals: Opportunity[] = [
  {
    id: 'hospitality-amenities',
    region: 'Gulf markets',
    category: 'Hospitality',
    title: 'Refillable amenities for boutique stays',
    signal: 'Buyers repeatedly ask for lower-waste formats without losing a premium guest experience.',
    supplyFit: 'Small-batch customization and multilingual packaging are the strongest fit.',
    route: 'Regional hospitality distributor',
    capitalQuestion: 'Can sample, packaging, and first-order costs stay inside the team’s loss limit?',
    salesTest: 'Interview three distributor profiles with one buyer-language brief.',
    weakestDimension: 'Capital',
    nextMove: 'Bound the first-order cash exposure before supplier outreach expands.',
    tone: 'gold',
    tags: ['B2B', 'OEM-ready', 'Distributor-led'],
  },
  {
    id: 'portable-energy',
    region: 'Southeast Asia',
    category: 'Retail operations',
    title: 'Compact backup power for independent shops',
    signal: 'Operators describe downtime and space limits before they describe technical specifications.',
    supplyFit: 'Compact units with clear service terms matter more than a broad model range.',
    route: 'Local installer partnership',
    capitalQuestion: 'How much working capital is tied up when service inventory is included?',
    salesTest: 'Test the downtime-led pitch with two installer partners and five shop operators.',
    weakestDimension: 'Sales',
    nextMove: 'Translate the pain language into a falsifiable installer-partner sales test.',
    tone: 'blue',
    tags: ['Resilience', 'Service-led', 'Local partner'],
  },
  {
    id: 'mobility-aids',
    region: 'Japan',
    category: 'Independent living',
    title: 'Quiet mobility aids for compact homes',
    signal: 'The recurring need is dignity, storage, and indoor maneuverability—not simply more features.',
    supplyFit: 'Foldable designs and repairable components align with the use environment.',
    route: 'Specialist retailer network',
    capitalQuestion: 'What is the maximum downside after certification, samples, and spare parts?',
    salesTest: 'Ask specialist retailers which after-sales promise is required to list a new brand.',
    weakestDimension: 'Supply',
    nextMove: 'Verify certification scope before treating supplier availability as real.',
    tone: 'green',
    tags: ['Specialist retail', 'After-sales', 'Compact design'],
  },
]

export const workflowSteps = [
  {
    number: '01',
    label: 'Demand',
    title: 'Prove the need is real enough to matter.',
    description: 'Validate the market, people, scenario, competition, price context, and evidence behind the demand hypothesis.',
  },
  {
    number: '02',
    label: 'Supply',
    title: 'Prove the offer can actually be supplied.',
    description: 'Check specifications, supplier availability, minimum orders, lead time, certification, compliance, and service expectations.',
  },
  {
    number: '03',
    label: 'Capital',
    title: 'Make the cash peak and downside explicit.',
    description: 'Model first-order investment, landed cost, cash cycle, maximum exposure, and the assumptions that could break the plan.',
  },
  {
    number: '04',
    label: 'Sales',
    title: 'Design a test that can win—or disprove—the sale.',
    description: 'Define the channel, customer, content, fulfillment path, sales experiment, and a stop condition before scaling effort.',
  },
] as const

export const useCases = [
  ['For the exporter', 'Find where your current capability solves a sharper need.'],
  ['For the sourcing team', 'Start with buyer context before building a supplier list.'],
  ['For the new market team', 'Understand the route to customer before spending on entry.'],
  ['For the small brand', 'Connect product decisions to a market that can support them.'],
] as const
