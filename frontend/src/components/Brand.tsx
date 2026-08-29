type BrandProps = {
  compact?: boolean
  inverse?: boolean
}

export default function Brand({ compact = false, inverse = false }: BrandProps) {
  return (
    <span className="ophira-brand" data-compact={compact} data-inverse={inverse}>
      <img src="/trademind-mark.svg" alt="" width={compact ? 26 : 30} height={compact ? 26 : 30} />
      <span>TradeMind</span>
    </span>
  )
}
