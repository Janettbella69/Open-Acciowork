type BrandProps = {
  compact?: boolean
  inverse?: boolean
}

export default function Brand({ compact = false, inverse = false }: BrandProps) {
  return (
    <span className="ophira-brand" data-compact={compact} data-inverse={inverse}>
      <img src="/ophira-mark.svg" alt="" width={compact ? 30 : 36} height={compact ? 30 : 36} />
      <span>OPHIRA</span>
      {!compact && <small>AI</small>}
    </span>
  )
}
