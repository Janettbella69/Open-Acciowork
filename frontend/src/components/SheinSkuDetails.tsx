import { SHEIN_INVENTORY_LABELS, type MarketplaceRead, type SheinInventoryQuantities, type SheinSkuInventory } from '../api/marketplaces'
import { useT } from '../i18n'

const quantities: [keyof SheinInventoryQuantities, string][] = [
  ['total', 'Total stock'], ['locked', 'Locked stock'], ['temporary_locked', 'Temporarily locked stock'],
  ['usable', 'Usable stock'], ['in_transit', 'In-transit stock'], ['stockout_demand', 'Stockout demand'],
]
const statuses: Record<string, string> = { SELLING: 'Selling', STOPPED: 'Stopped', PURCHASING: 'Purchasing', UNKNOWN: 'Unknown' }

function Inventory({ value }: { value: SheinSkuInventory }) {
  const t = useT()
  return <>
    {quantities.map(([key, label]) => <div key={key}>{t(label)}: {value.totals[key] ?? t('Unknown')}</div>)}
    <details>
      <summary>{t('Warehouse inventory ({count})', { count: String(value.warehouses.length) })}</summary>
      {value.warehouses.length ? <div className="marketplace-table"><table>
        <caption>{t('Reported warehouse inventory')}</caption>
        <thead><tr><th>{t('Warehouse')}</th><th>{t('Reported warehouse type')}</th>{quantities.map(([key, label]) => <th key={key}>{t(label)}</th>)}</tr></thead>
        <tbody>{value.warehouses.map((warehouse) => <tr key={`${warehouse.code}:${warehouse.warehouse_type}`}>
          <th scope="row">{warehouse.code}</th><td>{warehouse.warehouse_type}</td>
          {quantities.map(([key]) => <td key={key}>{warehouse[key] ?? t('Unknown')}</td>)}
        </tr>)}</tbody>
      </table></div> : <p>{t('Warehouse details were not supplied.')}</p>}
    </details>
  </>
}

export default function SheinSkuDetails({ detail, disabled, onNext }: { detail: MarketplaceRead; disabled: boolean; onNext: (id: string) => void }) {
  const t = useT()
  return <section aria-label={t('SHEIN SKU details')}>
    <p>{t('Site base and special prices are reported quotes; a zero special price does not establish the effective sale price. Supply prices are separate.')}</p>
    <p>{t(detail.inventory_type ? SHEIN_INVENTORY_LABELS[detail.inventory_type] : 'Prices only · inventory not requested')}</p>
    {detail.inventory_type && <p>{t('Inventory totals and warehouse rows are reported separately. Do not add them together or infer missing quantities.')}</p>}
    {detail.partial_data && <p role="status">{t('Some product or inventory records were not supplied. Missing values remain unknown.')}</p>}
    {detail.products.map((product) => <div key={product.id}>
      <h4>{product.title || product.id}</h4>
      <p>{t('SKU rows {start}–{end} of {total}', { start: String(product.variants?.length ? (product.variants_offset ?? 0) + 1 : 0), end: String((product.variants_offset ?? 0) + (product.variants?.length ?? 0)), total: String(detail.total_count ?? t('Unknown')) })}</p>
      {product.variants?.length ? <div className="marketplace-table"><table>
        <caption>{t('SHEIN SKU quotes and inventory · current page')}</caption>
        <thead><tr><th>{t('SKU')}</th><th>{t('SKC group')}</th><th>{t('Status')}</th><th>{t('Site prices')}</th><th>{t('Supply prices')}</th><th>{t('Inventory')}</th></tr></thead>
        <tbody>{product.variants.map((variant) => <tr key={variant.id}>
          <th scope="row">{variant.id}<br /><small>{t('Seller SKU')}: {variant.sku || t('Not supplied')}</small>
            {variant.attributes?.length ? <ul>{variant.attributes.map((attribute) => <li key={attribute.property_id}>{attribute.property_id}: {attribute.value || t('Not supplied')}{attribute.value_id ? ` (${attribute.value_id})` : ''}</li>)}</ul> : null}
          </th>
          <td>{variant.skc_id}<br />{variant.skc_title || t('Title not supplied')}</td>
          <td>{t('Sales status')}: {t(statuses[variant.status] ?? 'Unknown')}<br />{t('Purchase status')}: {t(statuses[variant.purchase_status ?? 'UNKNOWN'] ?? 'Unknown')}</td>
          <td>{variant.site_prices?.length ? <ul>{variant.site_prices.map((price, index) => <li key={`${price.site}:${price.currency}:${index}`}>
            {price.site ?? t('Site unknown')} · {price.currency ?? t('Currency unknown')}<br />
            {t('Base price')}: {price.base_price ?? t('Unknown')} · {t('Special price')}: {price.special_price ?? t('Unknown')}
          </li>)}</ul> : t('Not supplied')}</td>
          <td>{variant.supply_prices?.length ? <ul>{variant.supply_prices.map((price, index) => <li key={`${price.currency}:${index}`}>{price.currency ?? t('Currency unknown')} {price.amount ?? t('Unknown')}</li>)}</ul> : t('Not supplied')}</td>
          <td>{detail.inventory_type ? variant.inventory ? <Inventory value={variant.inventory} /> : t('Inventory not supplied') : t('Not requested')}</td>
        </tr>)}</tbody>
      </table></div> : <p>{t('No variants supplied')}</p>}
      {detail.next_page_token && <button type="button" disabled={disabled} onClick={() => onNext(product.id)}>{t('Next SKU page')}</button>}
    </div>)}
    {detail.source_requests?.length ? <details><summary>{t('Source requests')}</summary><ul>{detail.source_requests.map((source, index) => <li key={`${source.source_url}:${index}`}>
      {source.source_url} · {t('Request ID')}: {source.request_id || t('Not supplied')}
    </li>)}</ul></details> : null}
  </section>
}
