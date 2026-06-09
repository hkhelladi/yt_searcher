"""M4 — Tech detection.

Gated by `enrichment.tech_detect: true` in the run_config (default off).

Uses python-Wappalyzer (https://pypi.org/project/python-Wappalyzer/) against
HTML already cached by M3 — no separate fetch, no Playwright, no Chromium.
If the offer eventually needs JS-rendered analysis we can layer Playwright
in here behind a sub-sub-flag; for v1 the static HTML is enough.
"""

from __future__ import annotations

import asyncio
from typing import Any

from enrichment.config import TECH_DETECT_CONCURRENCY
from enrichment.schema import EnrichedRecord


# Static-site generators / static hosts → site_is_dynamic = False
_STATIC_MARKERS = frozenset({
    "Hugo", "Jekyll", "Eleventy", "Astro", "Gatsby",
    "GitHub Pages", "Netlify", "Vercel", "Cloudflare Pages",
    "MkDocs", "Pelican", "Docusaurus",
})
# Server-side tech → site_is_dynamic = True
_DYNAMIC_MARKERS = frozenset({
    "WordPress", "Drupal", "Joomla", "Ghost", "Wix", "Squarespace",
    "PHP", "Ruby on Rails", "Django", "Laravel", "Express",
    "ASP.NET", "Flask", "FastAPI", "Spring", "Symfony", "CodeIgniter",
})
# Ecommerce stacks (in addition to anything tagged "Ecommerce" category)
_ECOM_MARKERS = frozenset({
    "Shopify", "WooCommerce", "Magento", "BigCommerce", "PrestaShop",
    "Squarespace Commerce", "Wix Stores", "Ecwid", "OpenCart",
})
# Category → site_type (first match wins; order matters)
_CATEGORY_TO_TYPE: list[tuple[str, str]] = [
    ("Ecommerce", "ecommerce"),
    ("Live chat", "saas"),
    ("CRM", "saas"),
    ("Marketing automation", "saas"),
    ("News", "media"),
    ("Publishing", "media"),
    ("Blogs", "blog"),
    ("CMS", "blog"),
]


def _check_lib_or_raise() -> Any:
    try:
        from Wappalyzer import Wappalyzer, WebPage  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "Tech detection (M4) is enabled but python-Wappalyzer is not installed.\n"
            "Install with:  pip install python-Wappalyzer\n"
            "Or disable by setting `enrichment.tech_detect: false` in the run_config."
        ) from exc
    return Wappalyzer, WebPage


def _derive_signals(techs: dict[str, list[str]]) -> dict[str, Any]:
    """`techs` is {technology_name: [categories]} from analyze_with_categories."""
    tech_names = list(techs.keys())
    all_categories = {cat for cats in techs.values() for cat in cats}

    is_ecom = (
        "Ecommerce" in all_categories
        or any(t in _ECOM_MARKERS for t in tech_names)
    )

    has_static = any(t in _STATIC_MARKERS for t in tech_names)
    has_dynamic = any(t in _DYNAMIC_MARKERS for t in tech_names)
    if has_dynamic:
        is_dynamic: bool | None = True
    elif has_static and not has_dynamic:
        is_dynamic = False
    else:
        is_dynamic = True  # default when ambiguous

    site_type = "other"
    if is_ecom:
        site_type = "ecommerce"
    else:
        for cat, label in _CATEGORY_TO_TYPE:
            if cat in all_categories:
                site_type = label
                break
        else:
            # Heuristic for portfolio: no CMS, dynamic flag off, just a few static markers
            if not is_dynamic:
                site_type = "portfolio"

    return {
        "site_technologies": sorted(tech_names),
        "site_is_ecommerce": is_ecom,
        "site_is_dynamic": is_dynamic,
        "site_type": site_type,
    }


def _analyse_one(wappalyzer, WebPage, record: EnrichedRecord) -> EnrichedRecord:
    if not record.site_resolved or not record.site_final_url:
        if "M4" not in record.stages_completed:
            record.stages_completed.append("M4")
        return record

    html = record.site_html_cache.get("/") or next(iter(record.site_html_cache.values()), "")
    if not html:
        if "M4" not in record.stages_completed:
            record.stages_completed.append("M4")
        return record

    try:
        webpage = WebPage(url=record.site_final_url, html=html, headers={})
        techs = wappalyzer.analyze_with_categories(webpage)
    except Exception:
        # python-Wappalyzer raises a grab-bag of exception types on edge cases;
        # treat any failure as "no detection" rather than crashing the run.
        if "M4" not in record.stages_completed:
            record.stages_completed.append("M4")
        return record

    # Normalise: analyze_with_categories returns {tech_name: {"categories": [...], "versions": [...]}}
    norm: dict[str, list[str]] = {}
    for name, meta in (techs or {}).items():
        if isinstance(meta, dict) and "categories" in meta:
            norm[name] = list(meta["categories"])
        elif isinstance(meta, (list, tuple)):
            norm[name] = list(meta)
        else:
            norm[name] = []

    signals = _derive_signals(norm)
    record.site_technologies = signals["site_technologies"]
    record.site_is_ecommerce = signals["site_is_ecommerce"]
    record.site_is_dynamic = signals["site_is_dynamic"]
    record.site_type = signals["site_type"]

    if "M4" not in record.stages_completed:
        record.stages_completed.append("M4")
    return record


async def stage_tech(records: list[EnrichedRecord]) -> list[EnrichedRecord]:
    """Run python-Wappalyzer per resolved site, throttled by TECH_DETECT_CONCURRENCY.

    python-Wappalyzer is sync; we run each detection in a thread and throttle
    via an asyncio.Semaphore so we don't oversubscribe CPU/RAM.
    """
    if not records:
        return records

    Wappalyzer, WebPage = _check_lib_or_raise()
    wappalyzer = Wappalyzer.latest()
    sem = asyncio.Semaphore(TECH_DETECT_CONCURRENCY)

    async def one(record: EnrichedRecord) -> EnrichedRecord:
        async with sem:
            return await asyncio.to_thread(_analyse_one, wappalyzer, WebPage, record)

    return await asyncio.gather(*(one(r) for r in records))
