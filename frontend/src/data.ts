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
    id: 'product-trend-discovery',
    region: 'Product discovery',
    category: 'Trends & demand',
    title: 'Spot products before they peak',
    signal: 'Search growth, review gaps, creator posts, and bestseller movement point to demand before a crowded category becomes obvious.',
    supplyFit: 'The product must be differentiated enough to source, price, and position—not merely trending.',
    route: 'Amazon, TikTok Shop, and Shopify',
    capitalQuestion: 'Does the likely margin still work after samples, ads, fees, returns, and freight?',
    salesTest: 'Validate one narrow audience with a landing page, creative set, and clear stop condition.',
    weakestDimension: 'Demand',
    nextMove: 'Compare the trend signal with review evidence and a realistic landed-margin range.',
    tone: 'gold',
    tags: ['Trend research', 'Review mining', 'Product validation'],
  },
  {
    id: 'supplier-margin-workspace',
    region: 'Sourcing & margin',
    category: 'Suppliers',
    title: 'Compare factories and true margin',
    signal: 'A promising product becomes viable only when quotes, MOQ, lead time, quality, and certification can be compared on equal terms.',
    supplyFit: 'Supplier evidence, sample quality, customization, and production readiness stay visible beside every quote.',
    route: '1688, Alibaba.com, and direct factories',
    capitalQuestion: 'What is the landed unit cost and maximum cash exposure at the first realistic order size?',
    salesTest: 'Shortlist suppliers, draft one RFQ, and compare normalized quotes before negotiation.',
    weakestDimension: 'Supply',
    nextMove: 'Normalize supplier quotes and calculate landed margin before choosing a factory.',
    tone: 'blue',
    tags: ['Supplier search', 'Quote comparison', 'Landed cost'],
  },
  {
    id: 'listing-localization',
    region: 'Listing & SEO',
    category: 'Conversion',
    title: 'Localize listings that convert',
    signal: 'Keywords, reviews, competitor claims, and local buying language reveal what each market needs to understand before it buys.',
    supplyFit: 'Product facts, packaging claims, certifications, and available creative assets constrain what the listing can promise.',
    route: 'Amazon, Shopify, eBay, and TikTok Shop',
    capitalQuestion: 'Which creative and channel tests can be run before scaling inventory or ad spend?',
    salesTest: 'Generate a localized listing pack and test one claim, image order, and audience at a time.',
    weakestDimension: 'Sales',
    nextMove: 'Turn verified product facts into a localized listing and a measurable conversion test.',
    tone: 'green',
    tags: ['Listing', 'Localization', 'SEO'],
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
  ['For marketplace sellers', 'Research products, improve listings, and monitor competitors across Amazon, eBay, and TikTok Shop.'],
  ['For sourcing teams', 'Find factories, compare quotes, and calculate landed margin before committing cash.'],
  ['For DTC brands', 'Turn product facts into localized Shopify pages, launch creative, and market tests.'],
  ['For cross-border operators', 'Connect product research, suppliers, listings, content, and store work in one place.'],
] as const
