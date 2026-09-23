"""Bounded SHEIN catalog normalization; quotes and inventory retain their meanings."""

from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal

from .marketplaces import MarketplaceError
from .untrusted import UNTRUSTED_LIMITS, UntrustedSource, sanitize_untrusted_text

MAX_SHEIN_SKUS = 4000
MAX_SKU_PAGE_CHARS = UNTRUSTED_LIMITS[UntrustedSource.MARKETPLACE_PAGE].total_chars - 12_000


def _invalid():
    return MarketplaceError(
        "MARKETPLACE_RESPONSE_INVALID",
        "SHEIN returned an invalid or oversized product structure.",
        502,
    )


def _text(value, private_values, limit=2000):
    if value is None:
        value = ""
    if not isinstance(value, str) or len(value) > limit:
        raise _invalid()
    for private in private_values:
        value = value.replace(private, "[redacted]")
    result = sanitize_untrusted_text(value, UntrustedSource.BUSINESS_RECORD)
    if result.truncated or len(result.text) > limit:
        raise _invalid()
    return result.text


def _identifier(value, private_values):
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", value)
        or any(private in value for private in private_values)
    ):
        raise _invalid()
    return value


def _status(value):
    if value is None:
        return "UNKNOWN"
    if type(value) is not int or value not in {0, 1}:
        raise _invalid()
    return "ON_SHELF" if value else "OFF_SHELF"


def _array(value, maximum):
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > maximum:
        raise _invalid()
    if any(not isinstance(item, dict) for item in value):
        raise _invalid()
    return value


def _english(value, field, private):
    matches, languages = [], set()
    for item in _array(value, 5):
        language = _identifier(item.get("language"), private)
        if language in languages:
            raise _invalid()
        languages.add(language)
        if language == "en":
            matches.append(_text(item.get(field), private))
    return matches[0] if matches else ""


def _amount(value):
    if value is None:
        return None
    if type(value) not in {int, Decimal}:
        raise _invalid()
    amount = Decimal(value)
    if (
        not amount.is_finite()
        or not 0 <= amount <= 10**12
        or not -6 <= amount.as_tuple().exponent <= 12
    ):
        raise _invalid()
    return format(amount, "f")


def _currency(value, private):
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z]{3}", value):
        raise _invalid()
    return _identifier(value, private)


def _quantity(value, *, string_allowed=False):
    if value is None:
        return None
    if string_allowed and isinstance(value, str) and re.fullmatch(r"0|[1-9][0-9]{0,14}", value):
        value = int(value)
    if type(value) is not int or not 0 <= value <= 2**53 - 1:
        raise _invalid()
    return value


def _attribute_id(value, private):
    if value is None:
        return None
    if type(value) is not int or not 0 < value < 2**63:
        raise _invalid()
    return _identifier(str(value), private)


def _state(value, first, second):
    if value is None:
        return "UNKNOWN"
    if type(value) is not int or value not in {1, 2}:
        raise _invalid()
    return first if value == 1 else second


def normalize_details(info, catalog, private):
    if _identifier(info.get("spuName"), private) != catalog["id"]:
        raise _invalid()
    known_skcs = {item["id"]: item for item in catalog["variants"]}
    skc_ids, sku_ids, variants = set(), set(), []
    incomplete = False
    for skc in _array(info.get("skcInfoList"), 100):
        skc_id = _identifier(skc.get("skcName"), private)
        if skc_id not in known_skcs or skc_id in skc_ids:
            raise _invalid()
        skc_ids.add(skc_id)
        title = _english(skc.get("productMultiNameList"), "productName", private)
        title = title or known_skcs[skc_id]["title"]
        skus = _array(skc.get("skuInfoList"), 400)
        incomplete |= not skus
        for sku in skus:
            identifier = _identifier(sku.get("skuCode"), private)
            if identifier in sku_ids or len(sku_ids) >= MAX_SHEIN_SKUS:
                raise _invalid()
            sku_ids.add(identifier)
            prices, supply, attributes = [], [], []
            sites, currencies, attribute_ids = set(), set(), set()
            for price in _array(sku.get("priceInfoList"), 100):
                site = price.get("site")
                site = None if site is None or site == "" else _identifier(site, private)
                currency = _currency(price.get("currency"), private)
                if (site, currency) in sites:
                    raise _invalid()
                sites.add((site, currency))
                prices.append(
                    {
                        "site": site,
                        "currency": currency,
                        "base_price": _amount(price.get("basePrice")),
                        "special_price": _amount(price.get("specialPrice")),
                    }
                )
            for price in _array(sku.get("costInfoList"), 100):
                currency = _currency(price.get("currency"), private)
                if currency in currencies:
                    raise _invalid()
                currencies.add(currency)
                supply.append({"currency": currency, "amount": _amount(price.get("costPrice"))})
            for attribute in _array(sku.get("saleAttributeList"), 2):
                attr_id = _attribute_id(attribute.get("attributeId"), private)
                if attr_id is None or attr_id in attribute_ids:
                    raise _invalid()
                attribute_ids.add(attr_id)
                attributes.append(
                    {
                        "property_id": attr_id,
                        "value_id": _attribute_id(attribute.get("attributeValueId"), private),
                        "value": _english(
                            attribute.get("attributeValueMultiList"),
                            "attributeValueName",
                            private,
                        ),
                    }
                )
            variants.append(
                {
                    "id": identifier,
                    "title": "",
                    "skc_id": skc_id,
                    "skc_title": title,
                    "sku": _text(sku.get("supplierSku"), private, 300),
                    "status": _state(sku.get("mallState"), "SELLING", "STOPPED"),
                    "purchase_status": _state(sku.get("stopPurchase"), "PURCHASING", "STOPPED"),
                    "site_prices": sorted(
                        prices, key=lambda p: (p["site"] or "", p["currency"] or "")
                    ),
                    "supply_prices": sorted(supply, key=lambda p: p["currency"] or ""),
                    "attributes": sorted(attributes, key=lambda a: a["property_id"]),
                    "inventory": None,
                }
            )
    product = {
        **catalog,
        "title": _english(info.get("productMultiNameList"), "productName", private),
        "variants": sorted(variants, key=lambda variant: variant["id"]),
        "variants_loaded": True,
    }
    return product, incomplete or skc_ids != set(known_skcs) or not variants


def attach_inventory(info, product, private):
    # The official schema says object; the current official example uses object[].
    groups = _array([info] if isinstance(info, dict) else info, 100)
    expected = {variant["id"]: variant for variant in product["variants"]}
    known_skcs = {variant["skc_id"] for variant in product["variants"]}
    observed, goods_count = set(), 0
    incomplete = False
    for group in groups:
        for goods in _array(group.get("goodsInventory"), 100):
            goods_count += 1
            if goods_count > 100 or _identifier(goods.get("spuName"), private) != product["id"]:
                raise _invalid()
            skc_id = _identifier(goods.get("skcName"), private)
            if skc_id not in known_skcs:
                raise _invalid()
            for sku in _array(goods.get("skuList"), 400):
                identifier = _identifier(sku.get("skuCode"), private)
                if (
                    identifier not in expected
                    or identifier in observed
                    or expected[identifier]["skc_id"] != skc_id
                ):
                    raise _invalid()
                observed.add(identifier)
                totals = {
                    "total": _quantity(sku.get("totalInventoryQuantity")),
                    "locked": _quantity(sku.get("totalLockedQuantity")),
                    "temporary_locked": _quantity(sku.get("totalTempLockQuantity")),
                    "usable": _quantity(sku.get("totalUsableInventory")),
                    "in_transit": _quantity(sku.get("totalTransitQuantity")),
                    "stockout_demand": _quantity(sku.get("totalOutOfStockQty")),
                }
                incomplete |= any(
                    totals[key] is None
                    for key in (
                        "total",
                        "locked",
                        "temporary_locked",
                        "usable",
                    )
                )
                warehouses, keys = [], set()
                incomplete |= sku.get("warehouseInventoryList") is None
                for warehouse in _array(sku.get("warehouseInventoryList"), 100):
                    code = _identifier(warehouse.get("warehouseCode"), private)
                    kind = _identifier(warehouse.get("warehouseType"), private)
                    if (code, kind) in keys:
                        raise _invalid()
                    keys.add((code, kind))
                    row = {
                        "code": code,
                        "warehouse_type": kind,
                        "total": _quantity(warehouse.get("inventoryQuantity")),
                        "locked": _quantity(warehouse.get("lockedQuantity")),
                        "temporary_locked": _quantity(warehouse.get("tempLockQuantity")),
                        "usable": _quantity(warehouse.get("usableInventory")),
                        "in_transit": _quantity(warehouse.get("transitQuantity")),
                        "stockout_demand": _quantity(
                            warehouse.get("outOfStockQty"),
                            string_allowed=True,
                        ),
                    }
                    incomplete |= any(
                        row[key] is None
                        for key in (
                            "total",
                            "locked",
                            "temporary_locked",
                            "usable",
                        )
                    )
                    warehouses.append(row)
                expected[identifier]["inventory"] = {
                    "totals": totals,
                    "warehouses": sorted(
                        warehouses, key=lambda w: (w["code"], w["warehouse_type"])
                    ),
                }
    return incomplete or observed != set(expected)


def sku_cursor(value):
    if value is None:
        return 0, None
    if re.fullmatch(r"[1-9][0-9]{0,3}\.[a-f0-9]{64}", value):
        offset, digest = value.split(".")
        if int(offset) < MAX_SHEIN_SKUS:
            return int(offset), digest
    raise MarketplaceError("MARKETPLACE_CURSOR_INVALID", "Use the returned SHEIN SKU cursor.")


def page_skus(product, request, offset, expected_digest):
    digest = hashlib.sha256(
        json.dumps(
            {
                "product": product,
                "shop_id": request.shop_id,
                "inventory_type": request.inventory_type,
            },
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    if expected_digest is not None and expected_digest != digest:
        raise MarketplaceError(
            "MARKETPLACE_PAGINATION_CHANGED",
            "The SHEIN product, prices or inventory changed. Read SKU details from the first page.",
            409,
        )
    variants = product["variants"]
    if offset and offset >= len(variants):
        raise MarketplaceError("MARKETPLACE_CURSOR_INVALID", "Use the returned SHEIN SKU cursor.")
    end, chars = offset, 2
    for variant in variants[offset : offset + request.page_size]:
        size = len(json.dumps(variant, ensure_ascii=False)) + 2
        if chars + size > MAX_SKU_PAGE_CHARS:
            break
        chars += size
        end += 1
    if variants and end == offset:
        raise _invalid()
    return {
        "shops": [],
        "products": [
            {
                **product,
                "variants": variants[offset:end],
                "variants_offset": offset,
                "variants_complete": offset == 0 and end == len(variants),
            }
        ],
        "pagination_unit": "SKU",
        "total_count": len(variants),
        "inventory_type": request.inventory_type,
        "next_page_token": f"{end}.{digest}" if end < len(variants) else None,
    }
