"""Accio 本地能力面元数据快照（只取元数据+内容哈希，不复制正文）。

用途：竞品研究基线。Accio 客户端会在后台同步/覆盖/删除本地插件与 Skill
（实测：Skill 31→124、插件 15→19、accio-site-builder 的 skill 38→16、
pickfu 取消安装后整目录消失），所以任何"当前数量"都是切片，需要定点留档。

用法：python3 snapshot_accio.py <输出目录>
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ACCOUNT = Path("/Users/isabella/.accio/accounts/1780279321")
DESC_LIMIT = 240

DOMAINS = [
    ("选品与市场情报", r"selection|select|product-scout|market|trend|bestseller|hot-product|blue-ocean|蓝海|demand|opportunity|insight|jungle-scout|category"),
    ("供应商与采购", r"supplier|sourcing|source|1688|purchas|rfq|inquiry|factory|dropship|distribution|procure"),
    ("Listing 与 SEO", r"listing|seo|keyword|serp|title|description-generator|geo-|programmatic|rank"),
    ("营销与广告", r"marketing|ads?|ppc|campaign|promo|coupon|discount|influencer|social|instagram|tiktok|xiaohongshu|email|copywriting|creative|roas|ab-test"),
    ("客户与 CRM", r"customer|crm|review|voice|retention|winback|loyalty|referral|rfm|ltv|helpdesk|negotiat|chat-analysis|okki"),
    ("物流关务与合规", r"logistic|shipping|customs|tariff|hs-code|fulfil|warehouse|inventory|gdpr|compliance|vat|tax|ip-violation|prohibited|brand-protection|chargeback|fraud|return"),
    ("财务与定价", r"pricing|price|profit|margin|cost|financial|dcf|valuation|budget|forecast|payment|currency|checkout|invoice|dashboard"),
    ("店铺与平台运营", r"shopify|shopline|amazon|amz|etsy|alibaba|store|shop-|publish|migration|diagnosis|multi-platform|rpa|order|sycm|tiktok-shop|social-commerce"),
    ("内容与创意", r"image|video|design|photo|packaging|logo|brand-name|content|copy|higgsfield|remotion|canvas|avatar|prompt-guide"),
    ("建站与开发", r"site|website|web-|frontend|react|supabase|liquid|theme|dev|code|schema|api|cli|mcp"),
    ("办公文档", r"docx|pdf|pptx|xlsx|document|presentation|spreadsheet|knowledge-base|yuque|doc-coauthoring|internal-comms|feishu|lark|dingtalk|gmail|mail"),
    ("元能力与研究", r"skill-creator|skill-finder|skill-vetter|self-improvement|plugin-create|research|company-research|people-research|org-structure|competitive|competitor|audit|analyzer"),
]

OUR_SKILLS = {
    "global-opportunity-execution": "机会验证主线",
    "supplier-due-diligence": "供应商尽调",
    "landed-cost-analysis": "落地成本与单位经济性",
    "customer-path": "客户路径",
}


def classify(name: str, desc: str) -> str:
    blob = f"{name} {desc}".lower()
    for domain, pattern in DOMAINS:
        if re.search(pattern, blob):
            return domain
    return "其他"


def frontmatter_field(text: str, field: str) -> str:
    """Read one frontmatter field, tolerating folded (>-) and multi-line values."""
    if not text.startswith("---"):
        return ""
    end = text.find("\n---", 3)
    block = text[3 : end if end > 0 else 4000]
    lines = block.splitlines()
    for i, line in enumerate(lines):
        if not line.strip().lower().startswith(f"{field}:"):
            continue
        inline = line.split(":", 1)[1].strip().lstrip(">|-").strip()
        collected = [inline] if inline else []
        for follow in lines[i + 1 :]:
            if re.match(r"^\s*[a-zA-Z_]+\s*:", follow):
                break
            if follow.strip():
                collected.append(follow.strip())
        return " ".join(collected).strip().strip('"')
    return ""


def scan_skills() -> list[dict]:
    rows = []
    for path in sorted(ACCOUNT.rglob("SKILL.md")):
        rel = path.relative_to(ACCOUNT)
        parts = rel.parts
        if parts[0] == "skills":
            origin, owner = "account", "(账号级)"
        elif parts[0] == "plugins" and "installed" in parts:
            origin, owner = "plugin", parts[parts.index("installed") + 1]
        elif parts[0] == "agents":
            origin, owner = "agent", parts[1]
        else:
            origin, owner = "other", parts[0]
        text = path.read_text(encoding="utf-8", errors="ignore")
        name = frontmatter_field(text, "name") or path.parent.name
        desc = " ".join(frontmatter_field(text, "description").split())[:DESC_LIMIT]
        rows.append(
            {
                "name": name,
                "dir": path.parent.name,
                "origin": origin,
                "owner": owner,
                "domain": classify(path.parent.name, desc),
                "bytes": len(text),
                "sha256": hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()[:16],
                "mtime": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
                "path": str(rel),
                "description": desc,
            }
        )
    return rows


def scan_plugins() -> list[dict]:
    root = ACCOUNT / "plugins" / "installed"
    rows = []
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        meta_path = d / "plugin.json"
        meta = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                meta = {}
        skills = list((d / "skills").rglob("SKILL.md")) if (d / "skills").exists() else []
        chars = sum(len(f.read_text(encoding="utf-8", errors="ignore")) for f in skills)
        rows.append(
            {
                "name": d.name,
                "displayName": meta.get("displayName", ""),
                "version": meta.get("version", ""),
                "category": meta.get("category", ""),
                "description": " ".join(str(meta.get("description", "")).split())[:DESC_LIMIT],
                "skills": len(skills),
                "skill_chars": chars,
                "has_mcp": bool(meta.get("mcpServers")),
                "has_subagents": bool(meta.get("subAgents")),
                "has_connectors": bool(meta.get("connectors")),
                "mtime": datetime.fromtimestamp(d.stat().st_mtime, timezone.utc).isoformat(),
            }
        )
    return rows


def scan_catalogs() -> dict:
    out = {}
    skill_cache = ACCOUNT / "skills" / "remote_skills_cache.json"
    if skill_cache.exists():
        data = json.loads(skill_cache.read_text(encoding="utf-8"))
        out["skills"] = [
            {
                "name": s.get("name"),
                "official": s.get("official"),
                "description": " ".join(str(s.get("description", "")).split())[:DESC_LIMIT],
            }
            for s in data.get("skills", [])
        ]
    plug_cache = ACCOUNT / "plugins" / "remote-catalog-cache-en.json"
    if plug_cache.exists():
        data = json.loads(plug_cache.read_text(encoding="utf-8"))
        items = data if isinstance(data, list) else next(
            (v for v in data.values() if isinstance(v, list)), []
        )
        out["plugins"] = [
            {
                "name": i.get("name"),
                "displayName": i.get("displayName"),
                "category": i.get("category"),
                "source": i.get("source"),
                "summary": " ".join(str(i.get("summary", "")).split())[:DESC_LIMIT],
            }
            for i in items
        ]
    return out


def main() -> None:
    out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc)
    skills, plugins, catalogs = scan_skills(), scan_plugins(), scan_catalogs()
    payload = {
        "captured_at": stamp.isoformat(),
        "source": str(ACCOUNT),
        "note": "metadata + content hashes only; no skill bodies copied",
        "counts": {
            "local_skill_files": len(skills),
            "local_skill_unique": len({r["name"] for r in skills}),
            "local_plugins": len(plugins),
            "catalog_skills": len(catalogs.get("skills", [])),
            "catalog_plugins": len(catalogs.get("plugins", [])),
        },
        "plugins": plugins,
        "skills": skills,
        "remote_catalogs": catalogs,
    }
    json_path = out_dir / f"accio-inventory-{stamp:%Y%m%dT%H%M%SZ}.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {json_path} ({json_path.stat().st_size:,} B)")
    print(json.dumps(payload["counts"], ensure_ascii=False))


if __name__ == "__main__":
    main()
