import streamlit as st
from supabase import create_client, Client
import fitz
import pandas as pd
import re
from difflib import SequenceMatcher
from datetime import datetime, timedelta, timezone


# ============================================================
# PAGE CONFIGURATION
# ============================================================

st.set_page_config(
    page_title="Meesho Label Organizer",
    page_icon="📦",
    layout="wide",
    initial_sidebar_state="expanded"
)


# ============================================================
# CONSTANTS
# ============================================================

ADMIN_EMAILS = {
    "keyurtank8@gmail.com"
}

MONTHLY_PRICE = 399
LIFETIME_PRICE = 9999

DEMO_HOURS = 12
DEMO_PDF_LIMIT = 2


# ============================================================
# SUPABASE CLIENTS
# ============================================================

def get_secret(name, default=None):
    try:
        return st.secrets[name]
    except Exception:
        return default


SUPABASE_URL = get_secret("SUPABASE_URL")
SUPABASE_KEY = get_secret("SUPABASE_KEY")
SUPABASE_SERVICE_ROLE_KEY = get_secret(
    "SUPABASE_SERVICE_ROLE_KEY",
    None
)


if not SUPABASE_URL or not SUPABASE_KEY:
    st.error(
        "Supabase configuration is missing. "
        "Please add SUPABASE_URL and SUPABASE_KEY to Streamlit Secrets."
    )
    st.stop()


def create_user_client():
    return create_client(
        SUPABASE_URL,
        SUPABASE_KEY
    )


def create_admin_client():
    if SUPABASE_SERVICE_ROLE_KEY:
        return create_client(
            SUPABASE_URL,
            SUPABASE_SERVICE_ROLE_KEY
        )

    return None


# Main authenticated client
supabase: Client = create_user_client()

# Optional server-side administrator client
admin_supabase = create_admin_client()


# ============================================================
# SESSION STATE
# ============================================================

DEFAULT_SESSION_VALUES = {
    "user": None,
    "profile": None,
    "current_page": "Dashboard",
    "batch_results": None,
    "auth_loaded": False
}


for key, value in DEFAULT_SESSION_VALUES.items():
    if key not in st.session_state:
        st.session_state[key] = value


# ============================================================
# GENERAL HELPERS
# ============================================================

def now_utc():
    return datetime.now(timezone.utc)


def normalize_text(text):
    if not text:
        return ""

    text = str(text).lower()
    text = re.sub(r"\s+", " ", text)

    return text.strip()


def format_datetime(value):
    if not value:
        return "Not available"

    try:
        if isinstance(value, str):
            value = datetime.fromisoformat(
                value.replace("Z", "+00:00")
            )

        return value.strftime(
            "%d %b %Y, %I:%M %p"
        )

    except Exception:
        return str(value)


def get_current_email():

    user = st.session_state.get("user")

    if not user:
        return ""

    return str(
        getattr(user, "email", "") or ""
    ).strip().lower()


def get_current_user_id():

    user = st.session_state.get("user")

    if not user:
        return None

    return getattr(user, "id", None)


def is_admin():

    return (
        get_current_email()
        in ADMIN_EMAILS
    )


def get_database_client():
    """
    The administrator can optionally use the Supabase
    Service Role key stored securely in Streamlit Secrets.

    Normal users always use their authenticated client.
    """

    if is_admin() and admin_supabase is not None:
        return admin_supabase

    return supabase


# ============================================================
# LOGIN PERSISTENCE
# ============================================================

def save_auth_session(session):
    """
    Save the Supabase authentication tokens into Streamlit
    query parameters so authentication can be restored after
    a browser refresh.

    Do NOT put the service-role key here.
    """

    if not session:
        return

    access_token = getattr(
        session,
        "access_token",
        None
    )

    refresh_token = getattr(
        session,
        "refresh_token",
        None
    )

    if access_token and refresh_token:
        try:
            st.query_params["sb_access_token"] = access_token
            st.query_params["sb_refresh_token"] = refresh_token
        except Exception:
            pass


def clear_auth_session():

    try:
        params = st.query_params

        if "sb_access_token" in params:
            del params["sb_access_token"]

        if "sb_refresh_token" in params:
            del params["sb_refresh_token"]

    except Exception:
        pass


def restore_login():

    if st.session_state.auth_loaded:
        return

    st.session_state.auth_loaded = True

    # Already authenticated during this Streamlit session
    if st.session_state.user is not None:
        return

    try:
        access_token = st.query_params.get(
            "sb_access_token",
            None
        )

        refresh_token = st.query_params.get(
            "sb_refresh_token",
            None
        )

    except Exception:
        access_token = None
        refresh_token = None

    if not access_token or not refresh_token:
        return

    try:

        response = supabase.auth.set_session(
            access_token,
            refresh_token
        )

        user = getattr(
            response,
            "user",
            None
        )

        session = getattr(
            response,
            "session",
            None
        )

        if user:

            st.session_state.user = user

            if session:
                save_auth_session(session)

    except Exception:

        clear_auth_session()

        st.session_state.user = None
        st.session_state.profile = None


# ============================================================
# PROFILE FUNCTIONS
# ============================================================

def safe_get_profile(user_id):

    if not user_id:
        return None

    try:

        client = get_database_client()

        response = (
            client
            .table("profiles")
            .select("*")
            .eq("id", user_id)
            .execute()
        )

        if response.data:
            return response.data[0]

    except Exception:
        pass

    return None


def refresh_profile():

    user_id = get_current_user_id()

    if not user_id:
        st.session_state.profile = None
        return

    profile = safe_get_profile(user_id)

    # Administrator can use the application even if a profile
    # has not yet been created.
    if profile is None and is_admin():

        profile = {
            "id": user_id,
            "company_id": user_id,
            "company_name": "Administrator"
        }

    st.session_state.profile = profile


def get_company_id():

    profile = st.session_state.get("profile")

    if profile:

        company_id = profile.get(
            "company_id"
        )

        if company_id:
            return company_id

        profile_id = profile.get("id")

        if profile_id:
            return profile_id

    return get_current_user_id()


# ============================================================
# SUBSCRIPTION
# ============================================================

def get_subscription_status():

    if is_admin():

        return {
            "access": True,
            "plan": "Administrator",
            "reason": (
                "Full administrator application access"
            )
        }

    user_id = get_current_user_id()

    if not user_id:

        return {
            "access": False,
            "plan": "None",
            "reason": "Not logged in"
        }

    try:

        response = (
            supabase
            .table("payments")
            .select("*")
            .eq("user_id", user_id)
            .order(
                "created_at",
                desc=True
            )
            .execute()
        )

        payments = response.data or []

    except Exception:

        payments = []

    # Lifetime plan
    for payment in payments:

        status = str(
            payment.get("status", "")
        ).lower()

        plan = str(
            payment.get("plan", "")
        ).lower()

        if (
            status in [
                "paid",
                "completed",
                "success"
            ]
            and
            plan in [
                "lifetime",
                "permanent"
            ]
        ):

            return {
                "access": True,
                "plan": "Lifetime",
                "reason": "Lifetime plan active"
            }

    # Monthly plan
    for payment in payments:

        status = str(
            payment.get("status", "")
        ).lower()

        plan = str(
            payment.get("plan", "")
        ).lower()

        created_at = payment.get(
            "created_at"
        )

        if (
            status in [
                "paid",
                "completed",
                "success"
            ]
            and
            plan == "monthly"
            and
            created_at
        ):

            try:

                payment_date = (
                    datetime.fromisoformat(
                        str(created_at)
                        .replace(
                            "Z",
                            "+00:00"
                        )
                    )
                )

                expiry = (
                    payment_date
                    + timedelta(days=30)
                )

                if now_utc() <= expiry:

                    return {
                        "access": True,
                        "plan": "Monthly",
                        "reason": (
                            f"Active until "
                            f"{format_datetime(expiry)}"
                        )
                    }

            except Exception:
                pass

    profile = st.session_state.get("profile")

    if profile:

        demo_started = profile.get(
            "demo_started_at"
        )

        if demo_started:

            try:

                demo_start = (
                    datetime.fromisoformat(
                        str(demo_started)
                        .replace(
                            "Z",
                            "+00:00"
                        )
                    )
                )

                demo_end = (
                    demo_start
                    + timedelta(hours=DEMO_HOURS)
                )

                if now_utc() <= demo_end:

                    return {
                        "access": True,
                        "plan": "Demo",
                        "reason": (
                            f"Demo active until "
                            f"{format_datetime(demo_end)}"
                        )
                    }

            except Exception:
                pass

    return {
        "access": False,
        "plan": "Expired",
        "reason": "No active subscription"
    }


def start_demo():

    if is_admin():
        return True

    user_id = get_current_user_id()

    if not user_id:
        return False

    profile = safe_get_profile(user_id)

    if not profile:
        return False

    if profile.get("demo_started_at"):
        return False

    try:

        supabase.table(
            "profiles"
        ).update({

            "demo_started_at":
                now_utc().isoformat()

        }).eq(
            "id",
            user_id
        ).execute()

        refresh_profile()

        return True

    except Exception as e:

        st.error(
            f"Could not start demo: {e}"
        )

        return False


def count_demo_pdfs():

    if is_admin():
        return 0

    user_id = get_current_user_id()

    if not user_id:
        return 0

    try:

        response = (
            supabase
            .table("pdf_batches")
            .select("pdf_count")
            .eq("user_id", user_id)
            .execute()
        )

        return sum(
            int(
                row.get(
                    "pdf_count",
                    0
                )
                or 0
            )

            for row in (
                response.data
                or []
            )
        )

    except Exception:

        return 0


# ============================================================
# AUTHENTICATION
# ============================================================

def login_user(email, password):

    email = email.strip().lower()

    try:

        response = (
            supabase
            .auth
            .sign_in_with_password({

                "email": email,
                "password": password

            })
        )

        user = getattr(
            response,
            "user",
            None
        )

        session = getattr(
            response,
            "session",
            None
        )

        if not user:

            st.error(
                "Login failed. "
                "Please check your email and password."
            )

            return

        st.session_state.user = user

        if session:
            save_auth_session(session)

        refresh_profile()

        st.success(
            "Login successful!"
        )

        st.rerun()

    except Exception as e:

        message = str(e).lower()

        if (
            "invalid login credentials"
            in message
        ):

            st.error(
                "Incorrect email or password."
            )

        elif "email not confirmed" in message:

            st.error(
                "Please confirm your email "
                "before logging in."
            )

        else:

            st.error(
                f"Login failed: {e}"
            )


def register_user(
    company_name,
    email,
    password
):

    company_name = company_name.strip()
    email = email.strip().lower()

    if not company_name:

        st.error(
            "Please enter a company name."
        )

        return

    if not email:

        st.error(
            "Please enter an email address."
        )

        return

    try:

        response = (
            supabase
            .auth
            .sign_up({

                "email": email,

                "password": password,

                "options": {

                    "data": {

                        "company_name":
                            company_name

                    }

                }

            })
        )

        user = getattr(
            response,
            "user",
            None
        )

        session = getattr(
            response,
            "session",
            None
        )

        if not user:

            st.error(
                "Registration failed."
            )

            return

        if session:

            st.session_state.user = user

            save_auth_session(
                session
            )

            refresh_profile()

            st.success(
                "Account created successfully!"
            )

            st.rerun()

        else:

            st.success(
                "Account created successfully. "
                "Please confirm your email "
                "before logging in."
            )

    except Exception as e:

        message = str(e).lower()

        if (
            "already registered" in message
            or
            "already exists" in message
            or
            "duplicate" in message
        ):

            st.error(
                "This email is already registered. "
                "Please log in instead."
            )

        else:

            st.error(
                f"Registration failed: {e}"
            )


def logout():

    try:
        supabase.auth.sign_out()

    except Exception:
        pass

    clear_auth_session()

    for key in [
        "user",
        "profile",
        "batch_results"
    ]:

        st.session_state[key] = None

    st.session_state.current_page = (
        "Dashboard"
    )

    st.session_state.auth_loaded = True

    st.rerun()


# ============================================================
# MASTER PRODUCTS DATABASE FUNCTIONS
# ============================================================

def get_inventory():

    company_id = get_company_id()

    if not company_id:
        return []

    try:

        client = get_database_client()

        response = (
            client
            .table("master_products")
            .select("*")
            .eq(
                "company_id",
                company_id
            )
            .order(
                "product_name"
            )
            .execute()
        )

        return response.data or []

    except Exception as e:

        st.error(
            f"Could not load Master Products: {e}"
        )

        return []


def get_master_product_name(product):

    return (
        product.get("product_name")
        or
        product.get(
            "master_product_name"
        )
        or
        "Unnamed Product"
    )


def add_master_product(
    product_name,
    inventory_quantity,
    minimum_stock
):

    company_id = get_company_id()

    if not company_id:
        return False, (
            "Company information "
            "could not be found."
        )

    try:

        client = get_database_client()

        client.table(
            "master_products"
        ).insert({

            "company_id":
                company_id,

            "user_id":
                get_current_user_id(),

            "product_name":
                product_name.strip(),

            "inventory_quantity":
                int(inventory_quantity),

            "minimum_stock":
                int(minimum_stock)

        }).execute()

        return True, (
            "Master Product created successfully!"
        )

    except Exception as e:

        return False, str(e)


def update_master_product(
    product_id,
    product_name,
    inventory_quantity,
    minimum_stock
):

    try:

        client = get_database_client()

        client.table(
            "master_products"
        ).update({

            "product_name":
                product_name.strip(),

            "inventory_quantity":
                int(inventory_quantity),

            "minimum_stock":
                int(minimum_stock)

        }).eq(
            "id",
            product_id
        ).eq(
            "company_id",
            get_company_id()
        ).execute()

        return True, (
            "Master Product updated successfully!"
        )

    except Exception as e:

        return False, str(e)


def delete_master_product(
    product_id,
    product_name
):

    company_id = get_company_id()

    try:

        client = get_database_client()

        # Delete related SKU mappings first
        try:

            client.table(
                "sku_mappings"
            ).delete().eq(

                "company_id",
                company_id

            ).eq(

                "master_product_name",
                product_name

            ).execute()

        except Exception:
            pass

        # Delete product
        client.table(
            "master_products"
        ).delete().eq(

            "id",
            product_id

        ).eq(

            "company_id",
            company_id

        ).execute()

        return True, (
            "Master Product deleted successfully!"
        )

    except Exception as e:

        return False, str(e)


# ============================================================
# SKU MAPPING FUNCTIONS
# ============================================================

def get_user_mappings():

    company_id = get_company_id()

    if not company_id:
        return []

    try:

        client = get_database_client()

        response = (
            client
            .table("sku_mappings")
            .select("*")
            .eq(
                "company_id",
                company_id
            )
            .execute()
        )

        return response.data or []

    except Exception as e:

        st.error(
            f"Could not load SKU mappings: {e}"
        )

        return []


def save_sku_mapping(
    sku,
    master_product_name
):

    sku = str(sku).strip()

    if not sku:
        return False, (
            "SKU cannot be empty."
        )

    try:

        client = get_database_client()

        # Check if this SKU already exists
        existing = (
            client
            .table("sku_mappings")
            .select("*")
            .eq(
                "company_id",
                get_company_id()
            )
            .eq(
                "sku",
                sku
            )
            .execute()
        )

        if existing.data:

            client.table(
                "sku_mappings"
            ).update({

                "master_product_name":
                    master_product_name

            }).eq(

                "id",
                existing.data[0]["id"]

            ).execute()

        else:

            client.table(
                "sku_mappings"
            ).insert({

                "company_id":
                    get_company_id(),

                "user_id":
                    get_current_user_id(),

                "master_product_name":
                    master_product_name,

                "sku":
                    sku

            }).execute()

        return True, (
            "SKU mapping saved successfully!"
        )

    except Exception as e:

        return False, str(e)


def delete_sku_mapping(mapping_id):

    try:

        client = get_database_client()

        client.table(
            "sku_mappings"
        ).delete().eq(
            "id",
            mapping_id
        ).execute()

        return True

    except Exception as e:

        st.error(
            f"Could not delete mapping: {e}"
        )

        return False


# ============================================================
# PDF EXTRACTION
# ============================================================

def extract_text_from_page(page):

    try:
        return page.get_text("text")

    except Exception:
        return ""


def extract_product_section(page_text):

    if not page_text:
        return ""

    pattern = (
        r"Product\s*Details\s*"
        r"(.*?)"
        r"(?:"
        r"TAX\s*INVOICE"
        r"|BILL\s*TO"
        r"|Purchase\s*Order\s*No\.?"
        r")"
    )

    match = re.search(
        pattern,
        page_text,
        flags=(
            re.IGNORECASE
            | re.DOTALL
        )
    )

    if not match:
        return ""

    section = match.group(1)

    section = re.sub(

        r"SKU\s+Size\s+Qty\s+Color\s+Order\s*No\.?",

        "",

        section,

        flags=re.IGNORECASE

    )

    return re.sub(
        r"\s+",
        " ",
        section
    ).strip()


def parse_product_details(page_text):

    """
    Extract from every PDF page:

    SKU
    Size
    Quantity
    Color
    Order Number
    """

    section = extract_product_section(
        page_text
    )

    result = {

        "sku": "",
        "size": "",
        "qty": 1,
        "color": "",
        "order_no": "",
        "raw_product_details":
            section

    }

    if not section:
        return result

    working = section

    # --------------------------------------------------------
    # ORDER NUMBER
    # --------------------------------------------------------

    order_match = re.search(

        r"(\d{12,}(?:_\d+)?)\s*$",

        working

    )

    if order_match:

        result["order_no"] = (
            order_match.group(1)
        )

        working = (
            working[
                :order_match.start()
            ]
            .strip()
        )

    # --------------------------------------------------------
    # COLOR
    # --------------------------------------------------------

    known_colors = [

        "Multicolor",
        "Rose Gold",
        "Green",
        "Maroon",
        "Red",
        "Blue",
        "Black",
        "White",
        "Gold",
        "Silver",
        "Yellow",
        "Pink",
        "Purple",
        "Orange",
        "Brown",
        "Grey",
        "Gray",
        "Beige",
        "Cream"

    ]

    color_pattern = "|".join(

        re.escape(color)

        for color in sorted(
            known_colors,
            key=len,
            reverse=True
        )

    )

    color_match = re.search(

        rf"\b({color_pattern})\s*$",

        working,

        flags=re.IGNORECASE

    )

    if color_match:

        result["color"] = (
            color_match.group(1)
            .strip()
        )

        working = (
            working[
                :color_match.start()
            ]
            .strip()
        )

    # --------------------------------------------------------
    # QUANTITY
    # --------------------------------------------------------

    qty_match = re.search(

        r"\b(\d+)\s*$",

        working

    )

    if qty_match:

        result["qty"] = int(
            qty_match.group(1)
        )

        working = (
            working[
                :qty_match.start()
            ]
            .strip()
        )

    # --------------------------------------------------------
    # SIZE
    # --------------------------------------------------------

    size_pattern = (

        r"\b("
        r"Free\s+Size"
        r"|One\s+Size"
        r"|XXXL"
        r"|XXL"
        r"|XL"
        r"|XS"
        r"|S"
        r"|M"
        r"|L"
        r"|Small"
        r"|Medium"
        r"|Large"
        r")\s*$"

    )

    size_match = re.search(

        size_pattern,

        working,

        flags=re.IGNORECASE

    )

    if size_match:

        result["size"] = re.sub(

            r"\s+",

            " ",

            size_match.group(1)

        ).strip()

        working = (
            working[
                :size_match.start()
            ]
            .strip()
        )

    # Everything remaining is SKU/Product Name

    result["sku"] = re.sub(

        r"\s+",

        " ",

        working

    ).strip()

    return result


# ============================================================
# AUTOMATIC PRODUCT MATCHING
# ============================================================

def product_similarity(a, b):

    a_normalized = normalize_text(a)
    b_normalized = normalize_text(b)

    if not a_normalized or not b_normalized:
        return 0.0

    a_tokens = set(
        a_normalized.split()
    )

    b_tokens = set(
        b_normalized.split()
    )

    token_score = (

        len(
            a_tokens
            &
            b_tokens
        )

        /

        max(
            1,
            len(
                a_tokens
                |
                b_tokens
            )
        )

    )

    sequence_score = SequenceMatcher(

        None,

        a_normalized,

        b_normalized

    ).ratio()

    return max(
        token_score,
        sequence_score
    )


def find_matching_master_product(
    extracted,
    mappings,
    master_products
):

    sku = str(
        extracted.get(
            "sku",
            ""
        )
    ).strip()

    normalized_sku = normalize_text(
        sku
    )

    if not normalized_sku:

        return (
            None,
            "No SKU extracted"
        )

    # --------------------------------------------------------
    # EXACT SAVED SKU MAPPING
    # --------------------------------------------------------

    for mapping in mappings:

        mapped_sku = normalize_text(
            mapping.get(
                "sku",
                ""
            )
        )

        if (
            mapped_sku
            ==
            normalized_sku
        ):

            return (

                mapping.get(
                    "master_product_name"
                ),

                "Saved SKU Mapping"

            )

    # --------------------------------------------------------
    # DIRECT NAME MATCH
    # --------------------------------------------------------

    for product in master_products:

        product_name = (
            get_master_product_name(
                product
            )
        )

        normalized_product = (
            normalize_text(
                product_name
            )
        )

        if (
            normalized_product
            and
            (
                normalized_product
                in
                normalized_sku

                or

                normalized_sku
                in
                normalized_product
            )
        ):

            return (
                product_name,
                "Automatic Name Match"
            )

    # --------------------------------------------------------
    # SIMILARITY MATCH
    # --------------------------------------------------------

    best_product = None
    best_score = 0.0

    for product in master_products:

        product_name = (
            get_master_product_name(
                product
            )
        )

        score = product_similarity(
            sku,
            product_name
        )

        if score > best_score:

            best_score = score
            best_product = product_name

    if (
        best_product
        and
        best_score >= 0.72
    ):

        return (

            best_product,

            (
                "Automatic Similarity Match "
                f"({best_score:.0%})"
            )

        )

    return (
        None,
        "No Confident Match"
    )


# ============================================================
# PDF REORGANIZATION
# ============================================================

def reorganize_pdfs(uploaded_files):

    mappings = get_user_mappings()

    master_products = (
        get_inventory()
    )

    categorized_pages = {}

    uncategorized_pages = []

    extracted_rows = []

    auto_mappings = []

    known_skus = {

        normalize_text(
            mapping.get(
                "sku",
                ""
            )
        )

        for mapping in mappings

        if mapping.get("sku")

    }

    for uploaded_file in uploaded_files:

        pdf_bytes = (
            uploaded_file.getvalue()
        )

        document = fitz.open(

            stream=pdf_bytes,

            filetype="pdf"

        )

        try:

            for page_number in range(
                len(document)
            ):

                page = (
                    document.load_page(
                        page_number
                    )
                )

                page_text = (
                    extract_text_from_page(
                        page
                    )
                )

                details = (
                    parse_product_details(
                        page_text
                    )
                )

                master_product, match_method = (

                    find_matching_master_product(

                        details,

                        mappings,

                        master_products

                    )

                )

                extracted_rows.append({

                    "Source PDF":
                        uploaded_file.name,

                    "Page":
                        page_number + 1,

                    "SKU":
                        details.get(
                            "sku",
                            ""
                        ),

                    "Size":
                        details.get(
                            "size",
                            ""
                        ),

                    "Qty":
                        details.get(
                            "qty",
                            1
                        ),

                    "Color":
                        details.get(
                            "color",
                            ""
                        ),

                    "Order No.":
                        details.get(
                            "order_no",
                            ""
                        ),

                    "Master Product":

                        master_product

                        or

                        "Uncategorized",

                    "Match Method":
                        match_method

                })

                sku_key = normalize_text(
                    details.get(
                        "sku",
                        ""
                    )
                )

                # ------------------------------------------------
                # SAVE AUTOMATIC HIGH-CONFIDENCE MAPPING
                # ------------------------------------------------

                if (

                    master_product

                    and

                    sku_key

                    and

                    sku_key
                    not in
                    known_skus

                    and

                    match_method.startswith(
                        "Automatic"
                    )

                ):

                    success, message = (
                        save_sku_mapping(

                            details.get(
                                "sku",
                                ""
                            ),

                            master_product

                        )
                    )

                    if success:

                        known_skus.add(
                            sku_key
                        )

                        mappings.append({

                            "sku":

                                details.get(
                                    "sku",
                                    ""
                                ),

                            "master_product_name":

                                master_product

                        })

                        auto_mappings.append({

                            "SKU":

                                details.get(
                                    "sku",
                                    ""
                                ),

                            "Master Product":

                                master_product

                        })

                # ------------------------------------------------
                # CREATE SINGLE PAGE PDF
                # ------------------------------------------------

                single_page_pdf = (
                    fitz.open()
                )

                single_page_pdf.insert_pdf(

                    document,

                    from_page=page_number,

                    to_page=page_number

                )

                item = {

                    "pdf":
                        single_page_pdf,

                    "details":
                        details

                }

                if master_product:

                    categorized_pages.setdefault(

                        master_product,

                        []

                    ).append(
                        item
                    )

                else:

                    uncategorized_pages.append(
                        item
                    )

        finally:

            document.close()

    return {

        "categorized":
            categorized_pages,

        "uncategorized":
            uncategorized_pages,

        "extracted_rows":
            extracted_rows,

        "auto_mappings":
            auto_mappings

    }


def create_reorganized_pdf(
    categorized_pages,
    uncategorized_pages
):

    output = fitz.open()

    try:

        for product in sorted(

            categorized_pages.keys(),

            key=lambda value:
                value.lower()

        ):

            for item in (
                categorized_pages[
                    product
                ]
            ):

                output.insert_pdf(
                    item["pdf"]
                )

        for item in uncategorized_pages:

            output.insert_pdf(
                item["pdf"]
            )

        return output.tobytes()

    finally:

        output.close()


def create_pickup_list(
    categorized_pages
):

    rows = []

    for product, pages in (
        categorized_pages.items()
    ):

        rows.append({

            "Master Product":
                product,

            "Labels / Orders":
                len(pages),

            "Total Quantity":

                sum(

                    int(

                        item["details"].get(
                            "qty",
                            1
                        )

                        or 1

                    )

                    for item in pages

                )

        })

    if not rows:

        return pd.DataFrame(

            columns=[

                "Master Product",

                "Labels / Orders",

                "Total Quantity"

            ]

        )

    return (

        pd.DataFrame(
            rows
        )

        .sort_values(
            by="Master Product"
        )

    )


# ============================================================
# INVENTORY FUNCTIONS
# ============================================================

def update_inventory(
    product_id,
    new_quantity,
    new_minimum_stock=None
):

    try:

        update_data = {

            "inventory_quantity":
                int(new_quantity)

        }

        if new_minimum_stock is not None:

            update_data[
                "minimum_stock"
            ] = int(
                new_minimum_stock
            )

        client = get_database_client()

        client.table(
            "master_products"
        ).update(

            update_data

        ).eq(

            "id",
            product_id

        ).eq(

            "company_id",
            get_company_id()

        ).execute()

        return True

    except Exception as e:

        st.error(
            f"Inventory update failed: {e}"
        )

        return False


def deduct_inventory_from_pickup(
    pickup_dataframe
):

    inventory = get_inventory()

    inventory_lookup = {

        normalize_text(
            get_master_product_name(
                product
            )
        ):
            product

        for product in inventory

    }

    successful = 0

    failed = []

    for _, row in (
        pickup_dataframe.iterrows()
    ):

        product_name = str(

            row[
                "Master Product"
            ]

        ).strip()

        quantity_needed = int(

            row.get(

                "Total Quantity",

                row[
                    "Labels / Orders"
                ]

            )

        )

        inventory_item = (
            inventory_lookup.get(

                normalize_text(
                    product_name
                )

            )
        )

        if not inventory_item:

            failed.append(

                f"{product_name}: "
                "Product not found"

            )

            continue

        current_quantity = int(

            inventory_item.get(

                "inventory_quantity",

                0

            )

            or 0

        )

        new_quantity = max(

            0,

            current_quantity
            -
            quantity_needed

        )

        if update_inventory(

            inventory_item["id"],

            new_quantity

        ):

            successful += 1

            try:

                client = (
                    get_database_client()
                )

                client.table(

                    "inventory_history"

                ).insert({

                    "company_id":
                        get_company_id(),

                    "master_product_id":
                        inventory_item["id"],

                    "change_quantity":
                        -quantity_needed,

                    "reason":
                        (
                            "Automatic deduction "
                            "from organized PDF batch"
                        )

                }).execute()

            except Exception:
                pass

        else:

            failed.append(

                f"{product_name}: "
                "Could not update inventory"

            )

    return (
        successful,
        failed
    )


def get_low_stock_products():

    inventory = get_inventory()

    low_stock = []

    for product in inventory:

        quantity = int(

            product.get(

                "inventory_quantity",

                0

            )

            or 0

        )

        minimum_stock = int(

            product.get(

                "minimum_stock",

                0

            )

            or 0

        )

        if quantity <= minimum_stock:

            low_stock.append(
                product
            )

    return low_stock


# ============================================================
# AUTH PAGE
# ============================================================

def show_auth_page():

    st.title(
        "📦 Meesho Label Organizer"
    )

    st.markdown(

        """
### Organize your Meesho labels intelligently

- 📄 Read every page of uploaded PDFs
- 🏷️ Extract SKU, Size, Quantity and Color
- 🔗 Automatically map SKUs to Master Products
- 📚 Reorganize labels by Master Product
- 📋 Generate Pick-Up Lists
- 📦 Manage inventory
- ⚠️ Track low stock
- 💻 Access your account from multiple devices
        """

    )

    login_tab, register_tab = (
        st.tabs([

            "🔐 Login",

            "📝 Register"

        ])
    )

    # --------------------------------------------------------
    # LOGIN
    # --------------------------------------------------------

    with login_tab:

        st.subheader(
            "Login to your account"
        )

        login_email = st.text_input(

            "Email",

            key="login_email"

        )

        login_password = st.text_input(

            "Password",

            type="password",

            key="login_password"

        )

        if st.button(

            "Login",

            type="primary",

            use_container_width=True

        ):

            if (
                not login_email
                or
                not login_password
            ):

                st.warning(
                    "Please enter your email "
                    "and password."
                )

            else:

                login_user(

                    login_email,

                    login_password

                )

    # --------------------------------------------------------
    # REGISTER
    # --------------------------------------------------------

    with register_tab:

        st.subheader(
            "Create an account"
        )

        company_name = st.text_input(
            "Company Name"
        )

        register_email = st.text_input(
            "Email",
            key="register_email"
        )

        register_password = (
            st.text_input(

                "Password",

                type="password",

                key="register_password"

            )
        )

        confirm_password = (
            st.text_input(

                "Confirm Password",

                type="password"

            )
        )

        if st.button(

            "Create Account",

            type="primary",

            use_container_width=True

        ):

            if not all([

                company_name,

                register_email,

                register_password,

                confirm_password

            ]):

                st.warning(
                    "Please complete all fields."
                )

            elif (

                register_password
                !=
                confirm_password

            ):

                st.error(
                    "Passwords do not match."
                )

            elif len(
                register_password
            ) < 6:

                st.error(
                    "Password must contain at least "
                    "6 characters."
                )

            else:

                register_user(

                    company_name,

                    register_email,

                    register_password

                )


# ============================================================
# DASHBOARD
# ============================================================

def show_dashboard():

    st.title(
        "📦 Meesho Label Organizer"
    )

    profile = (
        st.session_state.profile
    )

    company_name = (

        profile.get(
            "company_name"
        )

        if profile

        else

        "Your Company"

    )

    st.write(
        f"### Welcome, {company_name} 👋"
    )

    subscription = (
        get_subscription_status()
    )

    st.info(

        f"Current Plan: "
        f"{subscription['plan']}"

    )

    inventory = get_inventory()

    low_stock = (
        get_low_stock_products()
    )

    col1, col2, col3 = (
        st.columns(3)
    )

    with col1:

        st.metric(

            "Master Products",

            len(inventory)

        )

    with col2:

        total_inventory = sum(

            int(

                item.get(

                    "inventory_quantity",

                    0

                )

                or 0

            )

            for item in inventory

        )

        st.metric(

            "Total Inventory",

            total_inventory

        )

    with col3:

        st.metric(

            "Low Stock Products",

            len(low_stock)

        )

    st.divider()

    if low_stock:

        st.error(
            "⚠️ LOW STOCK ALERTS"
        )

        for product in low_stock:

            name = (
                get_master_product_name(
                    product
                )
            )

            quantity = int(

                product.get(

                    "inventory_quantity",

                    0

                )

                or 0

            )

            minimum = int(

                product.get(

                    "minimum_stock",

                    0

                )

                or 0

            )

            st.warning(

                f"📦 **{name}** — "
                f"Current Stock: {quantity} | "
                f"Minimum Limit: {minimum}"

            )

    else:

        st.success(
            "✅ All products are above "
            "their minimum stock limits."
        )


# ============================================================
# MASTER PRODUCTS PAGE
# ============================================================

def show_master_products():

    st.title(
        "🏷️ Master Products"
    )

    # --------------------------------------------------------
    # ADD PRODUCT
    # --------------------------------------------------------

    with st.expander(

        "➕ Add New Master Product",

        expanded=False

    ):

        new_product_name = (
            st.text_input(

                "Master Product Name",

                key="new_product_name"

            )
        )

        new_quantity = (
            st.number_input(

                "Initial Inventory Quantity",

                min_value=0,

                step=1,

                key="new_product_quantity"

            )
        )

        new_minimum = (
            st.number_input(

                "Minimum Stock Alert Limit",

                min_value=0,

                step=1,

                key="new_product_minimum"

            )
        )

        if st.button(

            "Add Master Product",

            type="primary"

        ):

            if not (
                new_product_name.strip()
            ):

                st.warning(
                    "Please enter a product name."
                )

            else:

                success, message = (

                    add_master_product(

                        new_product_name,

                        new_quantity,

                        new_minimum

                    )

                )

                if success:

                    st.success(
                        message
                    )

                    st.rerun()

                else:

                    st.error(
                        f"Could not add product: "
                        f"{message}"
                    )

    # --------------------------------------------------------
    # PRODUCT LIST
    # --------------------------------------------------------

    inventory = get_inventory()

    if not inventory:

        st.info(
            "No Master Products have been created yet."
        )

        return

    st.subheader(
        "📦 Your Master Products"
    )

    display_rows = []

    for product in inventory:

        display_rows.append({

            "Master Product":
                get_master_product_name(
                    product
                ),

            "Quantity":

                product.get(

                    "inventory_quantity",

                    0

                ),

            "Minimum Stock":

                product.get(

                    "minimum_stock",

                    0

                )

        })

    st.dataframe(

        pd.DataFrame(
            display_rows
        ),

        use_container_width=True,

        hide_index=True

    )

    # --------------------------------------------------------
    # EDIT PRODUCT
    # --------------------------------------------------------

    st.divider()

    st.subheader(
        "✏️ Edit Master Product"
    )

    product_options = {

        (
            f"{get_master_product_name(product)} "
            f"({str(product.get('id'))[:8]}...)"
        ):
            product

        for product in inventory

    }

    selected_edit_label = (
        st.selectbox(

            "Select a Master Product to edit",

            [""] + list(
                product_options.keys()
            ),

            key="edit_master_product_select"

        )
    )

    if selected_edit_label:

        selected_product = (
            product_options[
                selected_edit_label
            ]
        )

        product_id = (
            selected_product.get(
                "id"
            )
        )

        current_name = (
            get_master_product_name(
                selected_product
            )
        )

        current_quantity = int(

            selected_product.get(

                "inventory_quantity",

                0

            )

            or 0

        )

        current_minimum = int(

            selected_product.get(

                "minimum_stock",

                0

            )

            or 0

        )

        edit_name = (
            st.text_input(

                "Product Name",

                value=current_name,

                key=f"edit_name_{product_id}"

            )
        )

        col1, col2 = (
            st.columns(2)
        )

        with col1:

            edit_quantity = (
                st.number_input(

                    "Current Stock",

                    min_value=0,

                    value=current_quantity,

                    step=1,

                    key=(
                        f"edit_quantity_"
                        f"{product_id}"
                    )

                )
            )

        with col2:

            edit_minimum = (
                st.number_input(

                    "Minimum Stock Limit",

                    min_value=0,

                    value=current_minimum,

                    step=1,

                    key=(
                        f"edit_minimum_"
                        f"{product_id}"
                    )

                )
            )

        if st.button(

            "💾 Save Product Changes",

            type="primary",

            key=(
                f"save_product_"
                f"{product_id}"
            )

        ):

            if not edit_name.strip():

                st.warning(
                    "Product name cannot be empty."
                )

            else:

                success, message = (

                    update_master_product(

                        product_id,

                        edit_name,

                        edit_quantity,

                        edit_minimum

                    )

                )

                if success:

                    st.success(
                        message
                    )

                    st.rerun()

                else:

                    st.error(
                        f"Could not update product: "
                        f"{message}"
                    )

    # --------------------------------------------------------
    # DELETE PRODUCT
    # --------------------------------------------------------

    st.divider()

    st.subheader(
        "🗑️ Delete Master Product"
    )

    selected_delete_label = (
        st.selectbox(

            "Select the Master Product to delete",

            [""] + list(
                product_options.keys()
            ),

            key="delete_master_product_select"

        )
    )

    if selected_delete_label:

        selected_product = (
            product_options[
                selected_delete_label
            ]
        )

        selected_id = (
            selected_product.get("id")
        )

        selected_name = (
            get_master_product_name(
                selected_product
            )
        )

        st.warning(

            f"You are about to permanently delete "
            f"**{selected_name}**."

        )

        confirm_delete = (
            st.checkbox(

                (
                    f"I confirm that I want "
                    f"to delete "
                    f"{selected_name}"
                ),

                key=(
                    f"confirm_delete_"
                    f"{selected_id}"
                )

            )
        )

        if st.button(

            "🗑️ Delete Selected Master Product",

            disabled=not confirm_delete,

            key=(
                f"delete_product_"
                f"{selected_id}"
            )

        ):

            success, message = (

                delete_master_product(

                    selected_id,

                    selected_name

                )

            )

            if success:

                st.success(
                    message
                )

                st.rerun()

            else:

                st.error(
                    f"Could not delete product: "
                    f"{message}"
                )


# ============================================================
# SKU MAPPINGS PAGE
# ============================================================

def show_sku_mappings():

    st.title(
        "🔗 SKU → Master Product Mapping"
    )

    inventory = get_inventory()

    if not inventory:

        st.warning(
            "Create at least one Master Product first."
        )

        return

    product_names = [

        get_master_product_name(
            product
        )

        for product in inventory

    ]

    with st.expander(

        "➕ Add SKU Mapping",

        expanded=True

    ):

        selected_product = (
            st.selectbox(

                "Select Master Product",

                product_names,

                key="mapping_product"

            )
        )

        sku = st.text_input(
            "SKU"
        )

        if st.button(

            "Save SKU Mapping",

            type="primary"

        ):

            success, message = (

                save_sku_mapping(

                    sku,

                    selected_product

                )

            )

            if success:

                st.success(
                    message
                )

                st.rerun()

            else:

                st.error(
                    f"Could not save mapping: "
                    f"{message}"
                )

    mappings = (
        get_user_mappings()
    )

    st.divider()

    st.subheader(
        "📋 Existing Mappings"
    )

    if not mappings:

        st.info(
            "No SKU mappings have been created yet."
        )

        return

    mapping_rows = []

    for mapping in mappings:

        mapping_rows.append({

            "SKU":
                mapping.get("sku"),

            "Master Product":
                mapping.get(
                    "master_product_name"
                ),

            "ID":
                mapping.get("id")

        })

    st.dataframe(

        pd.DataFrame(
            mapping_rows
        ),

        use_container_width=True,

        hide_index=True

    )

    delete_options = {

        (
            f"{mapping.get('sku')} → "
            f"{mapping.get('master_product_name')}"
        ):
            mapping

        for mapping in mappings

        if mapping.get("id")

    }

    if delete_options:

        selected_mapping = (
            st.selectbox(

                "Delete an existing mapping",

                [""] + list(
                    delete_options.keys()
                )

            )
        )

        if selected_mapping:

            mapping = (
                delete_options[
                    selected_mapping
                ]
            )

            if st.button(
                "Delete Selected Mapping"
            ):

                if delete_sku_mapping(
                    mapping["id"]
                ):

                    st.success(
                        "Mapping deleted."
                    )

                    st.rerun()


# ============================================================
# PDF ORGANIZER PAGE
# ============================================================

def show_pdf_organizer():

    st.title(
        "📄 PDF Label Organizer"
    )

    st.caption(

        "Each PDF page is individually read. "
        "SKU, Size, Quantity, Color and "
        "Order Number are extracted automatically."

    )

    subscription = (
        get_subscription_status()
    )

    if not subscription["access"]:

        st.warning(
            "You need an active plan "
            "or demo to use this feature."
        )

        if st.button(
            "View Plans"
        ):

            st.session_state.current_page = (
                "Subscription"
            )

            st.rerun()

        return

    used_pdfs = count_demo_pdfs()

    if (
        subscription["plan"]
        == "Demo"
    ):

        remaining = max(

            0,

            DEMO_PDF_LIMIT
            -
            used_pdfs

        )

        st.info(

            f"Demo usage: "
            f"{used_pdfs}/"
            f"{DEMO_PDF_LIMIT} PDFs "

            f"({remaining} remaining)"

        )

        if remaining <= 0:

            st.error(
                "Your demo PDF limit "
                "has been reached."
            )

            return

    uploaded_files = (
        st.file_uploader(

            "Upload Meesho Label PDFs",

            type=["pdf"],

            accept_multiple_files=True

        )
    )

    if (

        uploaded_files

        and

        st.button(

            "🚀 Extract, Map & Organize Labels",

            type="primary",

            use_container_width=True

        )

    ):

        if (

            subscription["plan"]
            == "Demo"

            and

            used_pdfs
            +
            len(uploaded_files)

            >
            DEMO_PDF_LIMIT

        ):

            st.error(
                "This upload exceeds your "
                "remaining demo limit."
            )

            return

        with st.spinner(

            "Reading every page and "
            "automatically organizing labels..."

        ):

            try:

                results = reorganize_pdfs(
                    uploaded_files
                )

                st.session_state.batch_results = (
                    results
                )

                try:

                    client = (
                        get_database_client()
                    )

                    client.table(

                        "pdf_batches"

                    ).insert({

                        "user_id":
                            get_current_user_id(),

                        "company_id":
                            get_company_id(),

                        "pdf_count":
                            len(uploaded_files)

                    }).execute()

                except Exception:
                    pass

                st.success(
                    "Labels extracted and organized!"
                )

            except Exception as e:

                st.error(
                    f"PDF processing failed: {e}"
                )

                return

    results = (
        st.session_state.batch_results
    )

    if not results:
        return

    categorized = (
        results["categorized"]
    )

    uncategorized = (
        results["uncategorized"]
    )

    # --------------------------------------------------------
    # EXTRACTED DATA
    # --------------------------------------------------------

    st.divider()

    st.subheader(
        "🔎 Extracted Product Details"
    )

    extracted_df = pd.DataFrame(

        results.get(

            "extracted_rows",

            []

        )

    )

    if not extracted_df.empty:

        st.dataframe(

            extracted_df,

            use_container_width=True,

            hide_index=True

        )

        st.download_button(

            "⬇️ Download Extracted Data CSV",

            data=(
                extracted_df
                .to_csv(
                    index=False
                )
                .encode("utf-8")
            ),

            file_name=(
                "meesho_extracted_product_details.csv"
            ),

            mime="text/csv"

        )

    # --------------------------------------------------------
    # AUTOMATIC MAPPINGS
    # --------------------------------------------------------

    auto_mappings = (
        results.get(

            "auto_mappings",

            []

        )
    )

    if auto_mappings:

        st.success(

            f"🤖 "
            f"{len(auto_mappings)} new "
            f"SKU mapping(s) were automatically saved."

        )

        st.dataframe(

            pd.DataFrame(
                auto_mappings
            ),

            use_container_width=True,

            hide_index=True

        )

    # --------------------------------------------------------
    # RESULTS
    # --------------------------------------------------------

    st.divider()

    st.subheader(
        "📊 Organization Results"
    )

    total_categorized = sum(

        len(pages)

        for pages in (
            categorized.values()
        )

    )

    col1, col2, col3 = (
        st.columns(3)
    )

    col1.metric(

        "Master Product Categories",

        len(categorized)

    )

    col2.metric(

        "Organized Labels",

        total_categorized

    )

    col3.metric(

        "Uncategorized Labels",

        len(uncategorized)

    )

    # --------------------------------------------------------
    # PICKUP LIST
    # --------------------------------------------------------

    pickup_dataframe = (
        create_pickup_list(
            categorized
        )
    )

    st.subheader(
        "📋 Pick-Up List"
    )

    st.dataframe(

        pickup_dataframe,

        use_container_width=True,

        hide_index=True

    )

    st.download_button(

        "⬇️ Download Pick-Up List CSV",

        data=(

            pickup_dataframe
            .to_csv(
                index=False
            )
            .encode("utf-8")

        ),

        file_name="pickup_list.csv",

        mime="text/csv"

    )

    # --------------------------------------------------------
    # INVENTORY DEDUCTION
    # --------------------------------------------------------

    if not pickup_dataframe.empty:

        st.subheader(
            "📦 Inventory Action"
        )

        st.warning(

            "This will subtract the "
            "Total Quantity from your inventory."

        )

        if st.button(

            "➖ Deduct Pick-Up List "
            "From Inventory",

            type="primary"

        ):

            success_count, failed = (

                deduct_inventory_from_pickup(
                    pickup_dataframe
                )

            )

            if success_count:

                st.success(

                    f"Inventory updated for "
                    f"{success_count} product(s)."

                )

            for error in failed:

                st.error(error)

    # --------------------------------------------------------
    # DOWNLOAD REORGANIZED PDF
    # --------------------------------------------------------

    st.subheader(
        "📄 Download Reorganized Labels"
    )

    if (
        categorized
        or
        uncategorized
    ):

        output_pdf = (
            create_reorganized_pdf(

                categorized,

                uncategorized

            )
        )

        st.download_button(

            "⬇️ Download Reorganized PDF",

            data=output_pdf,

            file_name=(
                "reorganized_meesho_labels.pdf"
            ),

            mime="application/pdf",

            type="primary",

            use_container_width=True

        )

    if uncategorized:

        st.warning(

            f"{len(uncategorized)} page(s) "
            "could not be matched to a "
            "Master Product."

        )


# ============================================================
# INVENTORY PAGE
# ============================================================

def show_inventory():

    st.title(
        "📦 Inventory Management"
    )

    inventory = get_inventory()

    if not inventory:

        st.info(
            "No products available. "
            "Add Master Products first."
        )

        return

    for product in inventory:

        name = (
            get_master_product_name(
                product
            )
        )

        product_id = (
            product["id"]
        )

        current_quantity = int(

            product.get(

                "inventory_quantity",

                0

            )

            or 0

        )

        minimum_stock = int(

            product.get(

                "minimum_stock",

                0

            )

            or 0

        )

        with st.expander(

            f"📦 {name}",

            expanded=False

        ):

            col1, col2 = (
                st.columns(2)
            )

            with col1:

                new_quantity = (
                    st.number_input(

                        "Current Quantity",

                        min_value=0,

                        value=current_quantity,

                        step=1,

                        key=(
                            f"inventory_quantity_"
                            f"{product_id}"
                        )

                    )
                )

            with col2:

                new_minimum = (
                    st.number_input(

                        "Minimum Stock Limit",

                        min_value=0,

                        value=minimum_stock,

                        step=1,

                        key=(
                            f"inventory_minimum_"
                            f"{product_id}"
                        )

                    )
                )

            if st.button(

                "💾 Save Changes",

                key=(
                    f"save_inventory_"
                    f"{product_id}"
                )

            ):

                if update_inventory(

                    product_id,

                    new_quantity,

                    new_minimum

                ):

                    st.success(
                        "Inventory updated successfully!"
                    )

                    st.rerun()


# ============================================================
# SUBSCRIPTION PAGE
# ============================================================

def create_payment_record(
    plan,
    amount
):

    try:

        supabase.table(
            "payments"
        ).insert({

            "user_id":
                get_current_user_id(),

            "company_id":
                get_company_id(),

            "plan":
                plan,

            "amount":
                amount,

            "status":
                "pending"

        }).execute()

        st.info(

            "A payment record was created. "
            "Connect a payment provider such as "
            "Razorpay to automatically activate "
            "subscriptions after payment."

        )

    except Exception as e:

        st.error(
            f"Payment initialization failed: {e}"
        )


def show_subscription_page():

    st.title(
        "💳 Subscription"
    )

    if is_admin():

        st.success(

            "🛡️ Administrator account detected. "
            "You have complete application access "
            "without purchasing a subscription."

        )

        return

    current_status = (
        get_subscription_status()
    )

    if current_status["access"]:

        st.success(

            f"Your "
            f"{current_status['plan']} "
            f"access is active."

        )

        st.info(
            current_status["reason"]
        )

        return

    col1, col2, col3 = (
        st.columns(3)
    )

    with col1:

        st.subheader(
            "🆓 Demo"
        )

        st.write(
            f"{DEMO_HOURS} hours access"
        )

        st.write(
            f"Maximum {DEMO_PDF_LIMIT} PDFs"
        )

        if st.button(
            "Start Free Demo"
        ):

            if start_demo():

                st.success(
                    "Demo started!"
                )

                st.rerun()

            else:

                st.warning(
                    "Demo could not be started "
                    "or has already been used."
                )

    with col2:

        st.subheader(
            "💳 Monthly"
        )

        st.write(
            f"₹{MONTHLY_PRICE}/month"
        )

        if st.button(
            "Choose Monthly Plan"
        ):

            create_payment_record(

                "monthly",

                MONTHLY_PRICE

            )

    with col3:

        st.subheader(
            "💎 Lifetime"
        )

        st.write(
            f"₹{LIFETIME_PRICE} one-time"
        )

        if st.button(
            "Choose Lifetime Plan"
        ):

            create_payment_record(

                "lifetime",

                LIFETIME_PRICE

            )


# ============================================================
# SIDEBAR AND NAVIGATION
# ============================================================

def show_main_app():

    profile = (
        st.session_state.profile
    )

    company_name = (

        profile.get(

            "company_name",

            "Meesho Label Organizer"

        )

        if profile

        else

        "Meesho Label Organizer"

    )

    with st.sidebar:

        st.title(
            "📦 Meesho Organizer"
        )

        st.caption(
            company_name
        )

        st.divider()

        pages = [

            "Dashboard",

            "PDF Organizer",

            "Master Products",

            "SKU Mappings",

            "Inventory",

            "Subscription"

        ]

        for page in pages:

            if st.button(

                page,

                use_container_width=True,

                key=f"navigation_{page}"

            ):

                st.session_state.current_page = (
                    page
                )

                st.rerun()

        st.divider()

        subscription = (
            get_subscription_status()
        )

        st.caption(

            f"Current Plan: "
            f"{subscription['plan']}"

        )

        if is_admin():

            st.success(
                "🛡️ Administrator Access"
            )

            st.caption(
                "Full application access"
            )

        st.divider()

        if st.button(

            "🚪 Logout",

            use_container_width=True,

            type="secondary"

        ):

            logout()

    page = (
        st.session_state.current_page
    )

    if page == "Dashboard":

        show_dashboard()

    elif page == "PDF Organizer":

        show_pdf_organizer()

    elif page == "Master Products":

        show_master_products()

    elif page == "SKU Mappings":

        show_sku_mappings()

    elif page == "Inventory":

        show_inventory()

    elif page == "Subscription":

        show_subscription_page()


# ============================================================
# APPLICATION START
# ============================================================

restore_login()

if st.session_state.user is None:

    show_auth_page()

else:

    if st.session_state.profile is None:

        refresh_profile()

    # Administrator can continue even without a database profile.
    if (
        st.session_state.profile is None
        and
        not is_admin()
    ):

        st.error(

            "Your account is authenticated, "
            "but your profile could not be loaded. "
            "Please check your Supabase profile "
            "and RLS configuration."

        )

        if st.button(
            "Logout"
        ):

            logout()

    else:

        show_main_app()
