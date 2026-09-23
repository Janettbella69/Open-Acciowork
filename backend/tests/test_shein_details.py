"""Seller-owned SKU quotes and explicitly selected inventories; no live accounts."""

import copy
import json

import httpx
import pytest
from claude_agent_sdk import create_sdk_mcp_server
from mcp.types import CallToolRequest, CallToolRequestParams
from pydantic import ValidationError
from test_auth_api import FakeRuntime, request_as, trusted_settings
from test_marketplace_access import service_at
from test_marketplaces import Body
from test_shein import CREDENTIALS, SHOP_ID, SPU, STORE, configure, reader, response

from ophira_backend import web_tools
from ophira_backend.auth import AuthorizationError
from ophira_backend.connectors import ConnectorRegistry, ConnectorUnavailableError
from ophira_backend.constants import PrincipalRole, RunStatus
from ophira_backend.main import create_app
from ophira_backend.marketplace_access import (
    MarketplaceAgentRequest,
    MarketplaceGrantRequest,
    MarketplaceReader,
)
from ophira_backend.marketplace_tools import build_marketplace_tool
from ophira_backend.marketplaces import MarketplaceError, MarketplaceReadRequest
from ophira_backend.models import ProjectCreate
from ophira_backend.secret_vault import FileSecretVault
from ophira_backend.shein import (
    SHEIN_DETAILS_PATH,
    SHEIN_PRODUCTS_PATH,
    SHEIN_STOCK_PATH,
    SHEIN_STORE_PATH,
    SHEIN_STORE_SCOPE,
    sign_shein_request,
)


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setattr(web_tools, "_resolve_ips", lambda _: ["93.184.216.34"])
    result = ConnectorRegistry(tmp_path / "connectors", FileSecretVault(tmp_path / "vault"))
    configure(result)
    return result


def request_for(**changes):
    return MarketplaceReadRequest(
        **{
            "operation": "product_details",
            "shop_id": SHOP_ID,
            "product_id": SPU["spuName"],
            "page_size": 10,
            **changes,
        }
    )


def scenario(count=2, *, wide=False):
    catalog = copy.deepcopy(SPU)
    catalog["skcList"] = catalog["skcList"][:1]
    skus, inventory = [], []
    for index in range(count):
        identifier = f"sku_{index:04d}"
        skus.append(
            {
                "skuCode": identifier,
                "supplierSku": f"Seller-{index}",
                "mallState": 1,
                "stopPurchase": 2,
                "priceInfoList": [
                    {
                        "site": f"shein-site-{site}",
                        "currency": "USD",
                        "basePrice": 12.35,
                        "specialPrice": 0,
                    }
                    for site in range(40 if wide else 1)
                ],
                "costInfoList": [{"costPrice": 4.25, "currency": "CNY"}],
                "saleAttributeList": [
                    {
                        "attributeId": 87,
                        "attributeValueId": 9007199254740993,
                        "attributeValueMultiList": [
                            {"language": "en", "attributeValueName": "Large"}
                        ],
                    }
                ],
            }
        )
        inventory.append(
            {
                "skuCode": identifier,
                "totalInventoryQuantity": 100,
                "totalLockedQuantity": 7,
                "totalTempLockQuantity": 3,
                "totalUsableInventory": 90,
                "totalTransitQuantity": 8,
                "totalOutOfStockQty": 2,
                "warehouseInventoryList": [
                    {
                        "warehouseCode": f"warehouse-{warehouse}",
                        "warehouseType": "2",
                        "inventoryQuantity": 100,
                        "lockedQuantity": 7,
                        "tempLockQuantity": 3,
                        "usableInventory": 90,
                        "transitQuantity": 8,
                        "outOfStockQty": "2",
                    }
                    for warehouse in range(30 if wide else 1)
                ],
            }
        )
    return {
        SHEIN_STORE_PATH: copy.deepcopy(STORE),
        SHEIN_PRODUCTS_PATH: {"meta": {"count": 1}, "data": [catalog]},
        SHEIN_DETAILS_PATH: {
            "spuName": SPU["spuName"],
            "productMultiNameList": [{"language": "en", "productName": "Kettle collection"}],
            "skcInfoList": [{"skcName": "skc_red", "skuInfoList": skus}],
        },
        SHEIN_STOCK_PATH: [
            {
                "goodsInventory": [
                    {
                        "spuName": SPU["spuName"],
                        "skcName": "skc_red",
                        "skuList": inventory,
                    }
                ]
            }
        ],
    }


def handler(data, calls):
    def handle(request):
        calls.append(request)
        return response(data[request.url.path])

    return handle


def first_sku(data):
    return data[SHEIN_DETAILS_PATH]["skcInfoList"][0]["skuInfoList"][0]


def first_stock(data):
    return data[SHEIN_STOCK_PATH][0]["goodsInventory"][0]["skuList"][0]


@pytest.mark.parametrize("kind", [None, "PI", "VI", "JI"])
async def test_exact_signed_requests_keep_quotes_stock_types_and_all_sources(registry, kind):
    data, calls = scenario(), []
    result = await reader(registry, handler(data, calls)).read(
        "shein",
        request_for(inventory_type=kind),
    )
    paths = [SHEIN_STORE_PATH, SHEIN_PRODUCTS_PATH, SHEIN_DETAILS_PATH]
    if kind:
        paths.append(SHEIN_STOCK_PATH)
    assert [call.url.path for call in calls] == paths
    for call in calls:
        assert call.url.host == "93.184.216.34" and not call.url.query
        assert call.method == "POST" and call.headers["host"] == "openapi.sheincorp.com"
        assert call.headers["x-lt-signature"] == sign_shein_request(
            CREDENTIALS["open_key_id"],
            CREDENTIALS["secret_key"],
            call.url.path,
            call.headers["x-lt-timestamp"],
            "Ab123",
        )
    assert json.loads(calls[1].content) == {
        "pageNum": 1,
        "pageSize": 1,
        "languageList": ["en"],
        "spuNameList": [SPU["spuName"]],
    }
    assert json.loads(calls[2].content) == {"spuName": SPU["spuName"], "languageList": ["en"]}
    sku = result["products"][0]["variants"][0]
    assert sku["site_prices"] == [
        {
            "site": "shein-site-0",
            "currency": "USD",
            "base_price": "12.35",
            "special_price": "0",
        }
    ]
    assert sku["supply_prices"] == [{"amount": "4.25", "currency": "CNY"}]
    assert sku["status"] == "SELLING" and sku["purchase_status"] == "STOPPED"
    assert sku["attributes"][0]["value_id"] == "9007199254740993"
    assert sku["skc_id"] == "skc_red" and sku["skc_title"] == "Red kettle"
    assert result["inventory_type"] == kind and result["partial_data"] is False
    assert [source["source_url"].split(".com")[1] for source in result["source_requests"]] == paths
    if kind:
        assert json.loads(calls[3].content) == {"spuNameList": [SPU["spuName"]], "invType": kind}
        assert sku["inventory"]["totals"] == {
            "total": 100,
            "locked": 7,
            "temporary_locked": 3,
            "usable": 90,
            "in_transit": 8,
            "stockout_demand": 2,
        }
        assert sku["inventory"]["warehouses"][0]["stockout_demand"] == 2
    else:
        assert sku["inventory"] is None
    assert all(secret not in json.dumps(result) for secret in CREDENTIALS.values())


async def test_stock_object_schema_and_array_example_are_both_supported(registry):
    data = scenario()
    data[SHEIN_STOCK_PATH] = data[SHEIN_STOCK_PATH][0]
    result = await reader(registry, handler(data, [])).read(
        "shein", request_for(inventory_type="VI")
    )
    assert result["partial_data"] is False


@pytest.mark.parametrize(
    "case", ["shop", "catalog", "missing", "detail", "skc", "stock_spu", "stock_skc", "stock_sku"]
)
async def test_identity_mismatch_never_returns_foreign_product_or_stock(registry, case):
    data, calls, changes = scenario(), [], {}
    if case == "shop":
        changes["shop_id"] = "other-shop"
    elif case == "catalog":
        data[SHEIN_PRODUCTS_PATH]["data"][0]["spuName"] = "other-spu"
    elif case == "missing":
        data[SHEIN_PRODUCTS_PATH] = {"meta": {"count": 0}, "data": []}
    elif case == "detail":
        data[SHEIN_DETAILS_PATH]["spuName"] = "other-spu"
    elif case == "skc":
        data[SHEIN_DETAILS_PATH]["skcInfoList"][0]["skcName"] = "other-skc"
    elif case == "stock_spu":
        data[SHEIN_STOCK_PATH][0]["goodsInventory"][0]["spuName"] = "other-spu"
    elif case == "stock_skc":
        data[SHEIN_STOCK_PATH][0]["goodsInventory"][0]["skcName"] = "other-skc"
    else:
        first_stock(data)["skuCode"] = "other-sku"
    with pytest.raises(MarketplaceError):
        await reader(registry, handler(data, calls)).read(
            "shein", request_for(inventory_type="VI", **changes)
        )
    assert len(calls) == (
        1
        if case == "shop"
        else 2
        if case in {"catalog", "missing"}
        else 3
        if case in {"detail", "skc"}
        else 4
    )


@pytest.mark.parametrize(
    "value", [True, -1, "12.35", float("nan"), float("inf"), 10**13, 0.0000001]
)
async def test_invalid_price_numbers_never_become_money(registry, value):
    data = scenario()
    first_sku(data)["priceInfoList"][0]["basePrice"] = value
    with pytest.raises(MarketplaceError):
        await reader(registry, handler(data, [])).read("shein", request_for())


@pytest.mark.parametrize("value", [True, -1, "90", 90.5, 2**53, float("inf")])
async def test_invalid_stock_counts_are_rejected(registry, value):
    data = scenario()
    first_stock(data)["totalUsableInventory"] = value
    with pytest.raises(MarketplaceError):
        await reader(registry, handler(data, [])).read("shein", request_for(inventory_type="PI"))


@pytest.mark.parametrize(
    "case", ["sku", "skc", "site", "supply", "warehouse", "stock", "site_type", "currency", "list"]
)
async def test_duplicate_or_malformed_nested_records_fail_closed(registry, case):
    data = scenario()
    sku = first_sku(data)
    if case == "sku":
        data[SHEIN_DETAILS_PATH]["skcInfoList"][0]["skuInfoList"].append(copy.deepcopy(sku))
    elif case == "skc":
        data[SHEIN_DETAILS_PATH]["skcInfoList"] *= 2
    elif case == "site":
        sku["priceInfoList"] *= 2
    elif case == "supply":
        sku["costInfoList"] *= 2
    elif case == "warehouse":
        first_stock(data)["warehouseInventoryList"] *= 2
    elif case == "stock":
        data[SHEIN_STOCK_PATH] *= 2
    elif case == "site_type":
        sku["priceInfoList"][0]["site"] = False
    elif case == "currency":
        sku["priceInfoList"][0]["currency"] = "usd"
    else:
        sku["priceInfoList"] = {"basePrice": 12}
    with pytest.raises(MarketplaceError):
        await reader(registry, handler(data, [])).read("shein", request_for(inventory_type="VI"))


async def test_missing_values_stay_unknown_and_missing_inventory_is_explicit(registry):
    data = scenario()
    first_sku(data)["priceInfoList"] = [{"site": "shein-us", "specialPrice": 0}]
    first_sku(data)["costInfoList"] = None
    first_stock(data).pop("totalUsableInventory")
    data[SHEIN_STOCK_PATH][0]["goodsInventory"][0]["skuList"] = [first_stock(data)]
    result = await reader(registry, handler(data, [])).read(
        "shein", request_for(inventory_type="JI")
    )
    first, second = result["products"][0]["variants"]
    assert first["site_prices"][0] == {
        "site": "shein-us",
        "currency": None,
        "base_price": None,
        "special_price": "0",
    }
    assert first["supply_prices"] == [] and first["inventory"]["totals"]["usable"] is None
    assert second["inventory"] is None and result["partial_data"]


@pytest.mark.parametrize("change", ["price", "stock", "kind", "product"])
async def test_next_page_cannot_mix_different_product_snapshots(registry, change):
    data = scenario(3)
    instance = reader(registry, handler(data, []))
    first = await instance.read("shein", request_for(page_size=1, inventory_type="VI"))
    options = {"page_size": 1, "inventory_type": "VI", "page_token": first["next_page_token"]}
    if change == "price":
        first_sku(data)["priceInfoList"][0]["basePrice"] = 15
    elif change == "stock":
        first_stock(data)["totalUsableInventory"] = 70
    elif change == "kind":
        options["inventory_type"] = "PI"
    else:
        data[SHEIN_DETAILS_PATH]["productMultiNameList"][0]["productName"] = "Changed product"
    with pytest.raises(MarketplaceError) as error:
        await instance.read("shein", request_for(**options))
    assert error.value.code == "MARKETPLACE_PAGINATION_CHANGED"


@pytest.mark.parametrize("cursor", ["1", "0." + "a" * 64, "4000." + "a" * 64, "https://evil.test"])
async def test_invalid_cursor_fails_before_any_request(registry, cursor):
    with pytest.raises(MarketplaceError):
        await reader(registry, lambda _: pytest.fail("No request")).read(
            "shein", request_for(page_token=cursor)
        )


@pytest.mark.parametrize("operation", ["authorized_shops", "products", "orders", "connections"])
def test_inventory_type_is_not_a_general_read_filter(operation):
    model = MarketplaceAgentRequest if operation == "connections" else MarketplaceReadRequest
    payload = {"operation": operation}
    if operation in {"products", "orders"}:
        payload["shop_id"] = SHOP_ID
    model.model_validate(payload)
    with pytest.raises(ValidationError):
        model.model_validate({**payload, "inventory_type": "VI"})


@pytest.mark.parametrize("case", ["too_many_skus", "oversized_sku"])
async def test_oversized_sku_data_is_rejected_instead_of_truncated(registry, case):
    data = scenario(401 if case == "too_many_skus" else 1, wide=case == "oversized_sku")
    if case == "oversized_sku":
        first_stock(data)["warehouseInventoryList"] = [
            {**first_stock(data)["warehouseInventoryList"][0], "warehouseCode": f"{'w' * 140}-{i}"}
            for i in range(100)
        ]
    with pytest.raises(MarketplaceError) as error:
        await reader(registry, handler(data, [])).read("shein", request_for(inventory_type="VI"))
    assert error.value.code == "MARKETPLACE_RESPONSE_INVALID"


@pytest.mark.parametrize("platform", ["tiktok_shop", "shopify", "shopee", "temu", "aliexpress"])
async def test_inventory_selector_cannot_be_ignored_by_other_platforms(registry, platform):
    registry.ensure_catalog()
    instance = MarketplaceReader(
        registry, transport_factory=lambda: httpx.MockTransport(lambda _: pytest.fail("No request"))
    )
    with pytest.raises(MarketplaceError) as error:
        await instance.read(platform, request_for(inventory_type="VI"))
    assert error.value.code == "MARKETPLACE_FILTER_UNSUPPORTED"


@pytest.mark.parametrize(
    "phase", [SHEIN_STORE_PATH, SHEIN_PRODUCTS_PATH, SHEIN_DETAILS_PATH, SHEIN_STOCK_PATH]
)
@pytest.mark.parametrize("mutation", ["revoke", "rotate", "scope", "project_access"])
async def test_all_network_waits_recheck_credentials_configuration_and_project_access(
    registry, phase, mutation
):
    data, revoked = scenario(), False

    def authorize():
        if revoked:
            raise AuthorizationError("Project grant revoked")

    def handle(request):
        nonlocal revoked
        if request.url.path == phase:
            if mutation == "revoke":
                registry.revoke("shein")
            elif mutation == "rotate":
                registry.store_secret("shein", json.dumps(CREDENTIALS))
            elif mutation == "scope":
                configure(registry, scope=SHEIN_STORE_SCOPE)
            else:
                revoked = True
        return response(data[request.url.path])

    with pytest.raises((MarketplaceError, ConnectorUnavailableError, AuthorizationError)):
        await reader(registry, handle).read(
            "shein", request_for(inventory_type="VI"), authorize=authorize
        )


async def test_http_details_and_private_error_responses(tmp_path, monkeypatch):
    monkeypatch.setattr(web_tools, "_resolve_ips", lambda _: ["93.184.216.34"])
    app = create_app(trusted_settings(tmp_path), runtime=FakeRuntime())
    configure(app.state.connectors)
    calls = []
    app.state.marketplace_reader.transport_factory = lambda: httpx.MockTransport(
        handler(scenario(), calls)
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://gateway.test"
    ) as client:
        path = "/api/connectors/shein/marketplace-read"
        payload = request_for(inventory_type="PI").model_dump()
        denied = await request_as(
            client, "POST", path, "member", PrincipalRole.MEMBER, json=payload
        )
        assert denied.status_code == 403 and not calls
        result = await request_as(client, "POST", path, "owner", PrincipalRole.OWNER, json=payload)
        assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
        assert result.json()["inventory_type"] == "PI" and len(calls) == 4


async def test_wide_skus_page_without_losing_quotes_or_warehouses_through_native_mcp(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(web_tools, "_resolve_ips", lambda _: ["93.184.216.34"])
    service = service_at(tmp_path)
    configure(service.connectors)
    data = scenario(8, wide=True)
    service.reader.transport_factory = lambda: httpx.MockTransport(handler(data, []))
    project = service.ledger.create_project(ProjectCreate(name="SHEIN", objective="Review SKUs"))
    granted = await service.grant(
        project.id,
        MarketplaceGrantRequest(connector_id="shein", shop_id=SHOP_ID, expected_revision=0),
        None,
    )
    binding = granted["bindings"][0]["id"]
    run = service.ledger.create_run(project.id, "Read the SKU inventory")
    run = service.ledger.update_run(
        run.id, sdk_session_id="synthetic-sku-session", status=RunStatus.RUNNING
    )
    tool = build_marketplace_tool(service, project.id, run.id)
    server = create_sdk_mcp_server(name="synthetic-shein", tools=[tool])
    seen, cursor = [], None
    for _ in range(9):
        result = await server["instance"].request_handlers[CallToolRequest](
            CallToolRequest(
                params=CallToolRequestParams(
                    name=tool.name,
                    arguments={
                        "operation": "product_details",
                        "binding_id": binding,
                        "product_id": SPU["spuName"],
                        "inventory_type": "VI",
                        "page_token": cursor,
                    },
                )
            )
        )
        assert result.root.isError is False, result
        observed = json.loads(result.root.content[0].text)
        page = observed["observation"]
        assert page["inventory_type"] == "VI" and len(page["source_requests"]) == 4
        variants = page["products"][0]["variants"]
        assert 0 < len(variants) < 8
        for sku in variants:
            assert len(sku["site_prices"]) == 40 and len(sku["inventory"]["warehouses"]) == 30
        seen.extend(sku["id"] for sku in variants)
        receipt = service.ledger.verify_tool_receipt(
            project.id,
            observed["source_receipt"]["id"],
            run_id=run.id,
            session_id=run.sdk_session_id,
        )
        assert receipt.payload["observation"] == page
        cursor = page["next_page_token"]
        if cursor is None:
            break
    assert seen == [f"sku_{index:04d}" for index in range(8)] and cursor is None
    assert service.ledger.get_project(project.id).evidence == []
    service.revoke(project.id, binding, granted["revision"], None)
    with pytest.raises(AuthorizationError):
        await service.read_for_run(
            project.id,
            run.id,
            MarketplaceAgentRequest(
                operation="product_details",
                binding_id=binding,
                product_id=SPU["spuName"],
                inventory_type="VI",
            ),
        )


async def test_decimal_lexemes_are_preserved_and_duplicate_json_keys_rejected(registry):
    data = scenario()

    def handle(request):
        if request.url.path != SHEIN_DETAILS_PATH:
            return response(data[request.url.path])
        raw = json.dumps({"code": "0", "info": data[SHEIN_DETAILS_PATH]})
        return httpx.Response(200, stream=Body(raw.replace("12.35", "0.123456").encode()))

    result = await reader(registry, handle).read("shein", request_for())
    assert result["products"][0]["variants"][0]["site_prices"][0]["base_price"] == "0.123456"

    def duplicate(request):
        if request.url.path != SHEIN_DETAILS_PATH:
            return handle(request)
        return httpx.Response(200, stream=Body(b'{"code":"0","info":{},"info":{}}'))

    with pytest.raises(MarketplaceError):
        await reader(registry, duplicate).read("shein", request_for())
