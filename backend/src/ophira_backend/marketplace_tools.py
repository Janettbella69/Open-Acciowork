"""One SDK-native read tool for project-bound shop discovery and product pages."""

import json

from claude_agent_sdk import ToolAnnotations, tool

from .auth import AuthorizationError
from .connectors import ConnectorUnavailableError
from .constants import TOOL_READ_MARKETPLACE
from .marketplace_access import MarketplaceAccessService, MarketplaceAgentRequest
from .marketplaces import MarketplaceError
from .storage import LedgerNotFoundError, SourceReceiptError
from .untrusted import UNTRUSTED_DATA_NOTICE, UntrustedSource, sanitize_untrusted_payload


def build_marketplace_tool(service: MarketplaceAccessService, project_id: str, run_id: str):
    @tool(
        TOOL_READ_MARKETPLACE.rsplit("__", 1)[1],
        "Read marketplace data within this project's human-approved shop grants. "
        "Use operation=connections to list available binding_id values (offset pagination), "
        "then operation=products with one returned binding_id and optionally a product status "
        "and next-page token. Each product page includes a server source_receipt.id for "
        "record_evidence in this run/session. No credentials, connector URLs, project ids, "
        "shop ciphers or grant changes are accepted. These are the seller's own listings, "
        "not market-wide demand or competitor sales. Not a substitute for commercial judgement.",
        MarketplaceAgentRequest.model_json_schema(),
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, idempotentHint=False, openWorldHint=True,
        ),
    )
    async def read_marketplace(args):
        try:
            request = MarketplaceAgentRequest.model_validate(args)
            result = await service.read_for_run(project_id, run_id, request)
        except (AuthorizationError, LedgerNotFoundError):
            result = {"error": "Project, run or shop access is unavailable. "
                      "Ask the project administrator to review the shop grant."}
            failed = True
        except (MarketplaceError, ConnectorUnavailableError) as exc:
            result = {"error_code": exc.code, "error": str(exc)}
            failed = True
        except (ValueError, OSError, SourceReceiptError):
            result = {"error": "Marketplace read could not produce a valid, scoped observation."}
            failed = True
        else:
            failed = False
        if not failed and request.operation == "connections":
            result = sanitize_untrusted_payload(result, UntrustedSource.MARKETPLACE_PAGE).value
        return {
            "content": [{"type": "text", "text": json.dumps(
                {"note": UNTRUSTED_DATA_NOTICE, **result},
                ensure_ascii=False, separators=(",", ":"),
            )}],
            "is_error": failed,
        }

    return read_marketplace
