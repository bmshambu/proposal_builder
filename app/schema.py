"""
Payload schema for the Public Audit Template 2026, mirrored from the Templafy
generation script's pydantic model (as seen in generate_public_audit_template_2026.py).

IMPORTANT: the enum values below (audit/tax, new-client, sectors, sub-sectors,
cities) must match the Literal[...] values in your generation script EXACTLY, or
Templafy will reject the payload. The authoritative source is your script's
`--list-constraints` output; update these lists if they drift. (We can wire the
form to call --list-constraints automatically later.)
"""

# --- text fields -------------------------------------------------------------
# form_field -> exact Templafy payload key
TEXT_FIELDS = {
    "full_client_name": "FullClientName",
    "short_client_name": "ShortClientName",
    "due_date": "DueDate",   # format YYYYMMDD, e.g. 20191130
}

# --- single-choice (enum) fields --------------------------------------------
AUDIT_TAX_OPTIONS = ["Audit only", "Audit and Tax"]   # verify 2nd against --list-constraints
NEW_CLIENT_OPTIONS = ["New Audit Client", "Expansion of Services"]

SECTORS = [
    "Asset Management", "Banking & Capital Markets", "Consumer & Retail",
    "Energy, Renewables & Chemicals", "Healthcare", "Industrial Manufacturing",
    "Insurance", "Life Sciences", "Media & Telecom", "State, Local & Education",
    "Technology",
]

SUBSECTORS_BY_SECTOR = {
    "Asset Management": ["Asset Management"],
    "Banking & Capital Markets": ["Banking & Capital Markets", "FinTech"],
    "Consumer & Retail": ["Agribusiness", "Consumer Products", "Food and Drink",
                          "Food Retail", "Restaurants", "Retail"],
    "Energy, Renewables & Chemicals": ["Chemicals", "Digital Infrastructure",
                                       "Oil and Gas", "Power & Utilities",
                                       "Renewables"],
    "Healthcare": ["Healthcare", "Healthtech"],
    "Industrial Manufacturing": ["Aerospace and Defense", "Automotive",
                                 "Industrials", "Metals", "Transportation"],
    "Insurance": ["Insurance"],
    "Life Sciences": ["Life Sciences"],
    "Media & Telecom": ["Media", "Telecom"],
    "State, Local & Education": ["Higher Ed (HERON) Federal Government",
                                 "State and Local Government"],
    "Technology": ["Hardware", "Semiconductors", "Software", "Technology"],
}

CITIES = [
    "Atlanta", "Austin", "Boston", "Charlotte", "Chicago", "Dallas", "Denver",
    "Houston", "Kansas City", "Los Angeles", "Minneapolis", "Nashville",
    "New York", "Philadelphia", "Phoenix", "Portland", "Salt Lake City",
    "San Francisco", "Seattle", "Silicon Valley", "St. Louis",
]

ENUM_FIELDS = {
    "audit_or_tax": "Is_this_proposal_for_audit_only_or_is_tax_included",
    "new_or_expansion": ("Is_this_a_new_audit_client_or_new_entities_for_"
                         "existing_client_i_e_Expansion_of_Services"),
    "sector": "What_Industry_sector_is_this_proposal_for",
    "sub_sector": "Select_industry_sub_sector",
    "city": "If_the_local_office_presence_important_select_the_appropriate_city",
}

# --- boolean toggle fields (each maps to optional section/slides) ------------
# form_field -> (exact Templafy key, human label)
BOOLEAN_FIELDS = {
    "transition_lab": ("Join_us_for_a_transition_lab", "Join us for a transition lab"),
    "continuity_is_key": ("Continuity_is_key", "Continuity is key"),
    "shared_history": ("Shared_history", "Shared history"),
    "annual_symposium": ("Annual_Symposium", "Annual Symposium"),
    "inclusive_culture": ("Fostering_an_Inclusive_Culture", "Fostering an Inclusive Culture"),
    "internal_audit": ("Working_Together_with_Internal_Audit", "Working Together with Internal Audit"),
    "quality": ("Quality_in_all_we_do", "Quality in all we do"),
    "about_kpmg": ("About_KPMG", "About KPMG"),
    "peer_review": ("Peer_review", "Peer review"),
    "cultivating_talent": ("Cultivating_talent", "Cultivating talent"),
    "intl_statutory": ("International_statutory_audits", "International statutory audits"),
}

# --- fixed / compliance fields ----------------------------------------------
# Note: "Accept" and "Springboard_ID" are intentionally omitted — the script
# does not require them.
FIXED_FIELDS = {
    "I agree to comply with these policies": "Accept",
}


def build_payload(form):
    """Turn a flat form dict (string keys from the HTML form) into the exact
    Templafy payload dict. Missing booleans default to False."""
    p = {}
    p[TEXT_FIELDS["full_client_name"]] = form.get("full_client_name", "").strip()
    p[TEXT_FIELDS["short_client_name"]] = form.get("short_client_name", "").strip()
    p[TEXT_FIELDS["due_date"]] = _normalize_date(form.get("due_date", "").strip())

    p[ENUM_FIELDS["audit_or_tax"]] = form.get("audit_or_tax", AUDIT_TAX_OPTIONS[0])
    p[ENUM_FIELDS["new_or_expansion"]] = form.get("new_or_expansion", NEW_CLIENT_OPTIONS[0])
    p[ENUM_FIELDS["sector"]] = form.get("sector", SECTORS[-1])
    p[ENUM_FIELDS["sub_sector"]] = form.get("sub_sector", "")
    p[ENUM_FIELDS["city"]] = form.get("city", CITIES[12])

    for fkey, (tkey, _label) in BOOLEAN_FIELDS.items():
        # HTML checkboxes only appear in form data when checked
        p[tkey] = fkey in form and str(form.get(fkey)).lower() in ("on", "true", "1", "yes")

    p.update(FIXED_FIELDS)
    return p


def _normalize_date(s):
    """Accept 2019-11-30 or 20191130 -> 20191130."""
    digits = "".join(ch for ch in s if ch.isdigit())
    return digits[:8] if len(digits) >= 8 else s
