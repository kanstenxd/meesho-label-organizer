import streamlit as st
from supabase import create_client, Client
import fitz  # PyMuPDF
import pandas as pd
import re
from difflib import SequenceMatcher
from datetime import datetime, timedelta, timezone
import time
import extra_streamlit_components as stx

st.set_page_config(
    page_title="Meesho Label Organizer",
    page_icon="📦",
    layout="wide",
    initial_sidebar_state="expanded",
)

MONTHLY_PRICE = 399
LIFETIME_PRICE = 9999
DEMO_HOURS = 12
DEMO_PDF_LIMIT = 2
ADMIN_EMAILS = {"keyurtank8@gmail.com"}

COOKIE_ACCESS = "meesho_access_token"
COOKIE_REFRESH = "meesho_refresh_token"
COOKIE_LOGIN_MARKER = "meesho_login_marker"
COOKIE_EXPIRY_DAYS = 30


# ============================================================
# SUPABASE + COOKIE CONNECTIONS
# ============================================================

def get_supabase():
    try:
        return create_client(
            st.secrets["SUPABASE_URL"],
            st.secrets["SUPABASE_KEY"],
        )
    except Exception:
        st.error(
            "Supabase configuration is missing. Add SUPABASE_URL and "
            "SUPABASE_KEY to Streamlit secrets."
        )
        st.stop()


def get_cookie_manager():
    # Keep one stable component identity across reruns. CookieManager loads
    # browser cookies asynchronously after a Streamlit page refresh.
    return stx.CookieManager(key="meesho_auth_cookie_manager")


supabase: Client = get_supabase()
cookie_manager = get_cookie_manager()


# ============================================================
# SESSION STATE
# ============================================================

DEFAULT_SESSION_STATE = {
    "user": None,
    "profile": None,
    "current_page": "Dashboard",
    "batch_results": None,
    "auth_restored": False,
    "auth_restore_attempts": 0,
    "auth_restore_pending": False,
    "auth_restore_started_at": None,
    "auth_cookie_seen": False,
    "auth_component_ready": False,
    "auth_restore_generation": 0,
}

for key, value in DEFAULT_SESSION_STATE.items():
    if key not in st.session_state:
        st.session_state[key] = value


# ============================================================
# GENERAL HELPERS
# ============================================================

def now_utc():
    return datetime.now(timezone.utc)


def normalize_text(text):
    """
    Normalize product/SKU text for matching.

    Case, underscores, hyphens and other punctuation are ignored while
    preserving word boundaries. Example:
    'OWEL_CHICK-SET' -> 'owel chick set'
    """
    text = str(text or "").lower()
    text = re.sub(r"[_\-]+", " ", text)
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()
def get_current_user_id():
    user = st.session_state.get("user")
    return getattr(user, "id", None) if user else None


def get_current_email():
    user = st.session_state.get("user")
    return str(getattr(user, "email", "") or "").strip().lower()


def is_admin():
    return get_current_email() in ADMIN_EMAILS


def format_datetime(value):
    if not value:
        return "Not available"
    try:
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value.strftime("%d %b %Y, %I:%M %p")
    except Exception:
        return str(value)


# ============================================================
# PERSISTENT LOGIN
# ============================================================

def clear_auth_cookies():
    """Cookies are cleared only by an explicit user logout."""
    try:
        cookie_manager.delete(COOKIE_ACCESS)
        cookie_manager.delete(COOKIE_REFRESH)
        cookie_manager.delete(COOKIE_LOGIN_MARKER)
    except Exception:
        pass


def save_auth_session(session):
    """Persist the Supabase session in browser cookies."""
    if not session:
        return

    try:
        expires_at = now_utc() + timedelta(days=COOKIE_EXPIRY_DAYS)
        access_token = getattr(session, "access_token", None)
        refresh_token = getattr(session, "refresh_token", None)
        user = getattr(session, "user", None)

        if access_token:
            cookie_manager.set(COOKIE_ACCESS, str(access_token), expires_at=expires_at)
        if refresh_token:
            cookie_manager.set(COOKIE_REFRESH, str(refresh_token), expires_at=expires_at)

        # This marker lets the app distinguish a returning user whose browser
        # cookies are still loading from a genuinely new visitor.
        if user and getattr(user, "id", None):
            cookie_manager.set(
                COOKIE_LOGIN_MARKER,
                str(user.id),
                expires_at=expires_at,
            )
    except Exception:
        # Never treat a temporary CookieManager issue as a logout.
        pass


def get_response_user(response):
    try:
        if hasattr(response, "user") and response.user:
            return response.user
    except Exception:
        pass

    try:
        if hasattr(response, "data") and getattr(response.data, "user", None):
            return response.data.user
    except Exception:
        pass

    return None


def _get_cookie_auth():
    """Read authentication cookies without mistaking component startup for logout."""
    try:
        cookies = cookie_manager.get_all()
        if cookies is None:
            return None, None, None, False, False
        if not isinstance(cookies, dict):
            return None, None, None, False, False

        # CookieManager returns an empty dict while its frontend component is
        # still starting on some Streamlit refreshes. Do not treat that as a
        # confirmed logout.
        access_token = cookies.get(COOKIE_ACCESS)
        refresh_token = cookies.get(COOKIE_REFRESH)
        marker = cookies.get(COOKIE_LOGIN_MARKER)
        has_any_auth_cookie = bool(access_token or refresh_token or marker)
        return access_token, refresh_token, marker, has_any_auth_cookie, True
    except Exception:
        return None, None, None, False, False


def _get_response_session(response):
    try:
        if hasattr(response, "session") and response.session:
            return response.session
    except Exception:
        pass
    try:
        if hasattr(response, "data") and getattr(response.data, "session", None):
            return response.data.session
    except Exception:
        pass
    return None


def restore_login_from_cookie():
    """Restore authentication after refresh.

    IMPORTANT: Nothing in this function logs a user out or deletes cookies.
    A fresh Streamlit session is created on browser refresh, so the app waits
    for CookieManager to become available before deciding whether to show the
    login screen.
    """
    if st.session_state.get("user") is not None:
        st.session_state.auth_restored = True
        st.session_state.auth_restore_pending = False
        return True

    access_token, refresh_token, marker, has_auth_cookie, component_ready = _get_cookie_auth()

    # The custom component may need multiple reruns after a hard refresh.
    if not component_ready:
        st.session_state.auth_restore_pending = True
        return None

    st.session_state.auth_component_ready = True

    if has_auth_cookie:
        st.session_state.auth_cookie_seen = True

    # If no auth cookies are currently visible, wait for a short initialization
    # window before treating this as a genuinely logged-out visitor. This avoids
    # the common refresh race where get_all() initially returns {}.
    if not access_token or not refresh_token:
        started = st.session_state.get("auth_restore_started_at")
        if started is None:
            started = time.time()
            st.session_state.auth_restore_started_at = started

        elapsed = time.time() - float(started)
        if elapsed < 8.0:
            st.session_state.auth_restore_attempts = int(st.session_state.get("auth_restore_attempts", 0)) + 1
            st.session_state.auth_restore_pending = True
            return None

        # We only reach False after CookieManager has had time to initialize.
        # No cookies are deleted here.
        st.session_state.auth_restore_pending = False
        st.session_state.auth_restored = True
        return False

    # Both tokens are present. Restore the exact Supabase session first.
    session = None
    try:
        response = supabase.auth.set_session(access_token, refresh_token)
        session = _get_response_session(response)
    except Exception:
        session = None

    # Supabase versions differ in refresh_session's signature. Try both forms.
    if not session:
        try:
            response = supabase.auth.refresh_session()
            session = _get_response_session(response)
        except TypeError:
            try:
                response = supabase.auth.refresh_session(refresh_token)
                session = _get_response_session(response)
            except Exception:
                session = None
        except Exception:
            session = None

    if session:
        try:
            save_auth_session(session)
        except Exception:
            pass

    try:
        response = supabase.auth.get_user()
        user = get_response_user(response)
    except Exception:
        user = None

    if user:
        st.session_state.user = user
        st.session_state.auth_restored = True
        st.session_state.auth_restore_attempts = 0
        st.session_state.auth_restore_pending = False
        st.session_state.auth_restore_started_at = None
        return True

    # A transient Supabase failure must never be converted into a logout.
    # Keep attempting restoration while the browser tokens remain present.
    st.session_state.auth_restore_pending = True
    return None


# ============================================================
# PROFILE / COMPANY
# ============================================================

def safe_get_profile(user_id):
    if not user_id:
        return None

    try:
        response = (
            supabase.table("profiles")
            .select("*")
            .eq("id", user_id)
            .limit(1)
            .execute()
        )
        return response.data[0] if response.data else None
    except Exception:
        return None


def ensure_profile():
    """
    Ensure every authenticated user has a profile row.

    A missing profile must never be treated as a logout or as an authentication
    failure. The user remains logged in; a profile is created when the database
    policy allows it.
    """
    user_id = get_current_user_id()
    if not user_id:
        return None

    existing = safe_get_profile(user_id)
    if existing:
        return existing

    user = st.session_state.get("user")
    metadata = getattr(user, "user_metadata", None) or {}
    email = get_current_email()
    company_name = (
        metadata.get("company_name")
        or metadata.get("full_name")
        or (email.split("@")[0] if email else "Meesho Label Organizer")
    )

    payload = {
        "id": user_id,
        "company_id": user_id,
        "company_name": company_name,
    }

    try:
        (
            supabase.table("profiles")
            .upsert(payload, on_conflict="id")
            .execute()
        )
    except Exception:
        # RLS/schema problems must not log the user out or block the app.
        pass

    return safe_get_profile(user_id)


def refresh_profile():
    user_id = get_current_user_id()
    st.session_state.profile = (
        ensure_profile() if user_id else None
    )


def get_company_id():
    profile = st.session_state.get("profile") or {}

    if profile.get("company_id"):
        return profile["company_id"]

    if profile.get("id"):
        return profile["id"]

    return get_current_user_id()


# ============================================================
# SUBSCRIPTIONS
# ============================================================

def get_subscription_status():
    if is_admin():
        return {
            "access": True,
            "plan": "Administrator",
            "reason": "Full administrator application access",
        }

    # Subscription access belongs to the authenticated user. A missing
    # company profile must not break login or subscription checks.
    profile = st.session_state.get("profile") or {}
    user_id = get_current_user_id()

    if not user_id:
        return {
            "access": False,
            "plan": "Expired",
            "reason": "No authenticated user",
        }

    try:
        response = (
            supabase.table("payments")
            .select("*")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .execute()
        )
        payments = response.data or []
    except Exception:
        payments = []

    for payment in payments:
        status = normalize_text(payment.get("status"))
        plan = normalize_text(payment.get("plan"))

        if status in {"paid", "completed", "success"} and plan in {
            "lifetime",
            "permanent",
        }:
            return {
                "access": True,
                "plan": "Lifetime",
                "reason": "Lifetime plan active",
            }

        if (
            status in {"paid", "completed", "success"}
            and plan == "monthly"
            and payment.get("created_at")
        ):
            try:
                payment_date = datetime.fromisoformat(
                    str(payment["created_at"]).replace("Z", "+00:00")
                )
                expiry = payment_date + timedelta(days=30)

                if now_utc() <= expiry:
                    return {
                        "access": True,
                        "plan": "Monthly",
                        "reason": f"Valid until {format_datetime(expiry)}",
                    }
            except Exception:
                pass

    demo_started = profile.get("demo_started_at")

    if demo_started:
        try:
            demo_start = datetime.fromisoformat(
                str(demo_started).replace("Z", "+00:00")
            )
            demo_end = demo_start + timedelta(hours=DEMO_HOURS)

            if now_utc() <= demo_end:
                return {
                    "access": True,
                    "plan": "Demo",
                    "reason": f"Demo active until {format_datetime(demo_end)}",
                }
        except Exception:
            pass

    return {
        "access": False,
        "plan": "Expired",
        "reason": "No active subscription",
    }


def start_demo():
    user_id = get_current_user_id()
    if not user_id:
        return False

    profile = st.session_state.get("profile") or ensure_profile() or {}

    if profile.get("demo_started_at"):
        return False

    # If the profile could not be created because of an RLS policy, report the
    # actual database issue instead of treating it as a logout.
    try:
        (
            supabase.table("profiles")
            .update({"demo_started_at": now_utc().isoformat()})
            .eq("id", user_id)
            .execute()
        )
        refresh_profile()
        return True
    except Exception as e:
        st.error(f"Could not start demo: {e}")
        return False


def count_demo_pdfs():
    user_id = get_current_user_id()

    if not user_id:
        return 0

    try:
        response = (
            supabase.table("pdf_batches")
            .select("pdf_count")
            .eq("user_id", user_id)
            .execute()
        )
        return sum(
            int(row.get("pdf_count", 0) or 0)
            for row in (response.data or [])
        )
    except Exception:
        return 0


# ============================================================
# AUTHENTICATION
# ============================================================

def login_user(email, password):
    try:
        response = supabase.auth.sign_in_with_password(
            {
                "email": email.strip().lower(),
                "password": password,
            }
        )

        user = getattr(response, "user", None)
        session = getattr(response, "session", None)

        if not user:
            st.error("Login failed. Please check your email and password.")
            return

        st.session_state.user = user
        st.session_state.auth_restored = True

        if session:
            save_auth_session(session)
        else:
            # Keep a login marker even if the auth response does not expose
            # the session object in this client version.
            try:
                cookie_manager.set(COOKIE_LOGIN_MARKER, str(user.id), expires_at=now_utc() + timedelta(days=COOKIE_EXPIRY_DAYS))
            except Exception:
                pass

        refresh_profile()
        # IMPORTANT: Do not call st.rerun() immediately after setting cookies.
        # CookieManager sends the set-cookie command to the browser only when
        # this script run is allowed to finish. An immediate rerun can cancel
        # that command, leaving no persistent login cookies after refresh.
        st.success("Login successful! Your login will remain active until you click Logout.")
        return True

    except Exception as e:
        message = str(e).lower()

        if "invalid login credentials" in message:
            st.error("Incorrect email or password.")
        elif "email not confirmed" in message:
            st.error("Please confirm your email before logging in.")
        else:
            st.error(f"Login failed: {e}")


def register_user(company_name, email, password):
    company_name = company_name.strip()
    email = email.strip().lower()

    if not company_name or not email or not password:
        st.error("Please complete all fields.")
        return

    try:
        response = supabase.auth.sign_up(
            {
                "email": email,
                "password": password,
                "options": {
                    "data": {
                        "company_name": company_name,
                    }
                },
            }
        )

        user = getattr(response, "user", None)

        if not user:
            st.error("Account registration failed.")
            return

        if getattr(user, "identities", None) == []:
            st.error("This email is already registered. Please log in instead.")
            return

        session = getattr(response, "session", None)

        if session:
            st.session_state.user = user
            st.session_state.auth_restored = True
            save_auth_session(session)
            refresh_profile()
            # Let this Streamlit run finish so CookieManager can persist the
            # new authentication cookies before any later browser refresh.
            st.success("Account created successfully! Your login will remain active until you click Logout.")
            return True
        else:
            st.success(
                "Account created. Please confirm your email, then log in."
            )

    except Exception as e:
        message = str(e).lower()

        if any(
            phrase in message
            for phrase in [
                "already registered",
                "already exists",
                "duplicate",
                "email_exists",
            ]
        ):
            st.error("This email is already registered. Please log in instead.")
        else:
            st.error(f"Registration failed: {e}")


def logout():
    try:
        supabase.auth.sign_out()
    except Exception:
        pass

    clear_auth_cookies()

    st.session_state.user = None
    st.session_state.profile = None
    st.session_state.batch_results = None
    st.session_state.current_page = "Dashboard"
    st.session_state.auth_restored = False
    st.session_state.auth_restore_attempts = 0
    st.session_state.auth_restore_pending = False
    st.session_state.auth_restore_started_at = None
    st.session_state.auth_cookie_seen = False
    st.session_state.auth_component_ready = False
    st.session_state.auth_restore_generation = int(st.session_state.get("auth_restore_generation", 0)) + 1

    st.rerun()


# ============================================================
# MASTER PRODUCT DATABASE FUNCTIONS
# ============================================================

def get_inventory():
    company_id = get_company_id()

    if not company_id:
        return []

    try:
        response = (
            supabase.table("master_products")
            .select("*")
            .eq("company_id", company_id)
            .order("product_name")
            .execute()
        )
        return response.data or []
    except Exception as e:
        st.error(f"Inventory loading error: {e}")
        return []


def add_master_product(product_name, inventory_quantity, minimum_stock):
    try:
        response = (
            supabase.table("master_products")
            .insert(
                {
                    "company_id": get_company_id(),
                    "user_id": get_current_user_id(),
                    "product_name": product_name.strip(),
                    "inventory_quantity": int(inventory_quantity),
                    "minimum_stock": int(minimum_stock),
                }
            )
            .execute()
        )
        return response is not None
    except Exception as e:
        st.error(f"Could not add product: {e}")
        return False


def update_master_product(
    product_id,
    product_name,
    inventory_quantity,
    minimum_stock,
):
    try:
        payload = {
            "product_name": product_name.strip(),
            "inventory_quantity": int(inventory_quantity),
            "minimum_stock": int(minimum_stock),
        }

        # updated_at may not exist in older schemas, so first try it
        # only if the plain update succeeds without schema errors.
        response = (
            supabase.table("master_products")
            .update(payload)
            .eq("id", product_id)
            .eq("company_id", get_company_id())
            .execute()
        )
        return response

    except Exception as e:
        st.error(f"Could not update Master Product: {e}")
        return None


def delete_master_product(product_id, product_name):
    company_id = get_company_id()

    try:
        master_column = _get_sku_mapping_master_column()
        if master_column:
            delete_value = product_name
            if master_column == "master_product_id":
                delete_value = product_id
            (
                supabase.table("sku_mappings")
                .delete()
                .eq("company_id", company_id)
                .eq(master_column, delete_value)
                .execute()
            )
    except Exception:
        pass

    try:
        (
            supabase.table("inventory_history")
            .delete()
            .eq("company_id", company_id)
            .eq("master_product_id", product_id)
            .execute()
        )
    except Exception:
        pass

    try:
        (
            supabase.table("master_products")
            .delete()
            .eq("id", product_id)
            .eq("company_id", company_id)
            .execute()
        )
        return True
    except Exception as e:
        st.error(f"Could not delete Master Product: {e}")
        return False


def get_low_stock_products():
    return [
        product
        for product in get_inventory()
        if int(product.get("inventory_quantity", 0) or 0)
        <= int(product.get("minimum_stock", 0) or 0)
    ]


def update_inventory(product_id, new_quantity, reason=None):
    try:
        current = (
            supabase.table("master_products")
            .select("inventory_quantity")
            .eq("id", product_id)
            .eq("company_id", get_company_id())
            .limit(1)
            .execute()
        )

        old_quantity = 0
        if current.data:
            old_quantity = int(
                current.data[0].get("inventory_quantity", 0) or 0
            )

        (
            supabase.table("master_products")
            .update({"inventory_quantity": int(new_quantity)})
            .eq("id", product_id)
            .eq("company_id", get_company_id())
            .execute()
        )

        try:
            (
                supabase.table("inventory_history")
                .insert(
                    {
                        "company_id": get_company_id(),
                        "master_product_id": product_id,
                        "change_quantity": int(new_quantity) - old_quantity,
                        "reason": reason or "Manual inventory update",
                    }
                )
                .execute()
            )
        except Exception:
            pass

        return True

    except Exception as e:
        st.error(f"Inventory update failed: {e}")
        return False


# ============================================================
# SKU MAPPINGS
# ============================================================

def _get_sku_mapping_sku_column():
    """Resolve the SKU text column used by the installed sku_mappings table."""
    cached = st.session_state.get("sku_mapping_sku_column")
    if cached:
        return cached

    candidates = [
        "sku_name",
        "sku",
        "product_sku",
        "sku_text",
        "label_sku",
        "mapped_sku",
    ]

    company_id = get_company_id()
    for column in candidates:
        try:
            query = supabase.table("sku_mappings").select(f"id,{column}").limit(1)
            if company_id:
                query = query.eq("company_id", company_id)
            query.execute()
            st.session_state.sku_mapping_sku_column = column
            return column
        except Exception:
            continue
    return None


def _sku_mappings_has_user_id():
    """Check whether the installed sku_mappings table has a user_id column.

    Newer database versions use user_id for Row Level Security.  We detect the
    column instead of assuming every older installation has it.
    """
    cached = st.session_state.get("sku_mappings_has_user_id")
    if cached is not None:
        return bool(cached)
    try:
        supabase.table("sku_mappings").select("id,user_id").limit(1).execute()
        st.session_state.sku_mappings_has_user_id = True
        return True
    except Exception:
        st.session_state.sku_mappings_has_user_id = False
        return False


def _get_sku_mapping_master_column():
    """Resolve the master-product column without assuming one fixed schema.

    Different versions of this project have used different column names.  The
    app therefore detects the column that actually exists before reading or
    writing a mapping.  This specifically prevents the PostgREST PGRST204
    error caused by hard-coding ``master_product_name``.
    """
    cached = st.session_state.get("sku_mapping_master_column")
    if cached:
        return cached

    candidates = [
        "master_product_name",
        "master_product",
        "master_product_id",
        "product_name",
        "mapped_product",
        "master_name",
        "category",
    ]

    company_id = get_company_id()
    for column in candidates:
        try:
            query = supabase.table("sku_mappings").select(f"id,{column}").limit(1)
            if company_id:
                query = query.eq("company_id", company_id)
            query.execute()
            st.session_state.sku_mapping_master_column = column
            return column
        except Exception:
            continue
    return None


def _master_product_value_for_storage(master_column, master_product_name):
    """Convert a product name to an ID when the installed schema stores IDs."""
    if master_column != "master_product_id":
        return master_product_name

    target = normalize_text(master_product_name)
    for product in get_inventory():
        if normalize_text(product.get("product_name", "")) == target:
            return product.get("id")
    return None


def _master_product_name_from_value(master_column, value):
    """Convert a stored master-product ID back to a display name when needed."""
    if master_column != "master_product_id":
        return value
    if value in (None, ""):
        return ""
    value = str(value)
    for product in get_inventory():
        if str(product.get("id")) == value:
            return product.get("product_name", "")
    return value

def _normalize_mapping_row(row, sku_column=None, master_column=None):
    """Return a mapping row with consistent ``sku`` and master-product keys."""
    item = dict(row or {})
    sku_column = sku_column or _get_sku_mapping_sku_column()
    master_column = master_column or _get_sku_mapping_master_column()

    if "sku" not in item or item.get("sku") in (None, ""):
        if sku_column and sku_column in item:
            item["sku"] = item.get(sku_column)
        if not item.get("sku"):
            for column in ("sku_name", "product_sku", "sku_text", "label_sku", "mapped_sku"):
                if item.get(column):
                    item["sku"] = item.get(column)
                    break

    if not item.get("master_product_name"):
        if master_column and master_column in item:
            item["master_product_name"] = _master_product_name_from_value(
                master_column,
                item.get(master_column),
            )
        if not item.get("master_product_name"):
            for column in ("master_product", "product_name", "mapped_product", "master_name", "category"):
                if item.get(column):
                    item["master_product_name"] = item.get(column)
                    break

    return item


def get_user_mappings():
    """Load mappings belonging to the current authenticated user/company."""
    company_id = get_company_id()
    user_id = get_current_user_id()
    if not company_id and not user_id:
        return []

    try:
        query = supabase.table("sku_mappings").select("*")

        # sku_mappings RLS in the current database is based on user_id.  Use
        # that column when available; this also prevents company_id from
        # accidentally hiding a user's own mappings.
        if _sku_mappings_has_user_id() and user_id:
            query = query.eq("user_id", user_id)
        elif company_id:
            query = query.eq("company_id", company_id)

        response = query.execute()
        sku_column = _get_sku_mapping_sku_column()
        master_column = _get_sku_mapping_master_column()
        return [_normalize_mapping_row(row, sku_column, master_column) for row in (response.data or [])]
    except Exception as e:
        st.error(f"Could not load SKU mappings: {e}")
        return []

def save_sku_mapping(sku, master_product_name):
    """Save a user-approved SKU mapping.

    The first-time assignment is always user initiated.  The saved row stores
    user_id whenever that column exists so Supabase RLS can verify ownership.
    """
    sku = str(sku or "").strip()
    master_product_name = str(master_product_name or "").strip()
    if not sku or not master_product_name:
        return False

    company_id = get_company_id()
    user_id = get_current_user_id()
    if not user_id:
        st.error("Could not save SKU mapping: your login session could not be verified.")
        return False

    sku_column = _get_sku_mapping_sku_column()
    master_column = _get_sku_mapping_master_column()
    if not sku_column or not master_column:
        missing = []
        if not sku_column:
            missing.append("SKU")
        if not master_column:
            missing.append("Master Product")
        st.error(
            "Could not save SKU mapping because the sku_mappings table does not "
            f"contain a supported {' and '.join(missing)} column."
        )
        return False

    master_value = _master_product_value_for_storage(master_column, master_product_name)
    if master_value in (None, ""):
        st.error(
            "Could not save SKU mapping because the selected Master Product "
            "could not be resolved in the current database schema."
        )
        return False

    has_user_id = _sku_mappings_has_user_id()

    try:
        query = supabase.table("sku_mappings").select("id").eq(sku_column, sku).limit(1)
        if has_user_id:
            query = query.eq("user_id", user_id)
        elif company_id:
            query = query.eq("company_id", company_id)
        existing = query.execute()

        if existing.data:
            update_data = {master_column: master_value}
            (
                supabase.table("sku_mappings")
                .update(update_data)
                .eq("id", existing.data[0]["id"])
                .execute()
            )
        else:
            insert_data = {
                sku_column: sku,
                master_column: master_value,
            }
            if company_id:
                insert_data["company_id"] = company_id
            # This is the important RLS fix.  The database policy checks
            # user_id = auth.uid(), so omitting user_id causes error 42501.
            if has_user_id:
                insert_data["user_id"] = user_id

            (
                supabase.table("sku_mappings")
                .insert(insert_data)
                .execute()
            )
        return True
    except Exception as e:
        st.session_state.pop("sku_mapping_sku_column", None)
        st.session_state.pop("sku_mapping_master_column", None)
        st.session_state.pop("sku_mappings_has_user_id", None)
        st.error(f"Could not save SKU mapping: {e}")
        return False

def delete_sku_mapping(mapping_id):
    try:
        (
            supabase.table("sku_mappings")
            .delete()
            .eq("id", mapping_id)
            .eq("company_id", get_company_id())
            .execute()
        )
        return True
    except Exception as e:
        st.error(f"Could not delete SKU mapping: {e}")
        return False


# ============================================================
# PDF EXTRACTION
# ============================================================

def extract_text_from_page(page):
    try:
        return page.get_text("text") or ""
    except Exception:
        return ""


def extract_product_section(page_text):
    if not page_text:
        return ""

    match = re.search(
        r"Product\s*Details\s*(.*?)(?:"
        r"TAX\s*INVOICE|"
        r"BILL\s*TO\s*/?\s*SHIP\s*TO|"
        r"Purchase\s*Order\s*(?:No\.?)?|"
        r"Order\s*Details|"
        r"Payment\s*Details|"
        r"Seller\s*Details"
        r")",
        page_text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    if match:
        return match.group(1).strip()

    fallback = re.search(
        r"Product\s*Details\s*(.*)",
        page_text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    return fallback.group(1).strip() if fallback else ""


def parse_product_details(page_text):
    """
    Extract SKU, Size, Qty and Color from every PDF page.

    Long Meesho product names often wrap over multiple lines, so parsing
    works from right to left:
        Product Name / SKU | Size | Qty | Color
    """
    section = extract_product_section(page_text)

    result = {
        "sku": "",
        "size": "",
        "qty": 1,
        "color": "",
        "raw_product_details": section,
    }

    if not section:
        return result

    text = re.sub(r"SKU\s+Size\s+Qty\s+Color", "", section, flags=re.I)
    working = re.sub(r"\s+", " ", text).strip()

    known_colors = [
        "Multicolor",
        "Multi Color",
        "Rose Gold",
        "Light Blue",
        "Dark Blue",
        "Sky Blue",
        "Navy Blue",
        "Bottle Green",
        "Sea Green",
        "Off White",
        "Black",
        "White",
        "Red",
        "Blue",
        "Green",
        "Yellow",
        "Orange",
        "Pink",
        "Purple",
        "Brown",
        "Grey",
        "Gray",
        "Gold",
        "Silver",
        "Maroon",
        "Beige",
        "Cream",
    ]

    color_pattern = "|".join(
        re.escape(color)
        for color in sorted(known_colors, key=len, reverse=True)
    )

    color_match = re.search(
        rf"\b({color_pattern})\s*$",
        working,
        flags=re.I,
    )

    if color_match:
        result["color"] = re.sub(
            r"\s+",
            " ",
            color_match.group(1),
        ).strip()
        working = working[:color_match.start()].strip()

    qty_match = re.search(r"\b(\d{1,4})\s*$", working)

    if qty_match:
        qty = int(qty_match.group(1))
        if qty > 0:
            result["qty"] = qty
            working = working[:qty_match.start()].strip()

    size_pattern = (
        r"Free\s+Size|One\s+Size|"
        r"XXS|XS|S|M|L|XL|XXL|XXXL|"
        r"Small|Medium|Large|"
        r"\d{1,3}(?:\.\d+)?\s*(?:cm|inch|in)?"
    )

    size_match = re.search(
        rf"\b({size_pattern})\s*$",
        working,
        flags=re.I,
    )

    if size_match:
        result["size"] = re.sub(
            r"\s+",
            " ",
            size_match.group(1),
        ).strip()
        working = working[:size_match.start()].strip()

    result["sku"] = re.sub(r"\s+", " ", working).strip()

    return result


# ============================================================
# AUTOMATIC MASTER PRODUCT MATCHING
# ============================================================

def get_master_product_name(product):
    return str(
        product.get("product_name")
        or product.get("master_product_name")
        or ""
    ).strip()


def product_similarity(a, b):
    """
    Return a similarity score without automatically assigning the result.
    Similar/typo matches must be reviewed by the user.
    """
    a_norm = normalize_text(a)
    b_norm = normalize_text(b)

    if not a_norm or not b_norm:
        return 0.0

    a_tokens = a_norm.split()
    b_tokens = b_norm.split()

    token_set_a = set(a_tokens)
    token_set_b = set(b_tokens)
    token_score = (
        len(token_set_a & token_set_b)
        / max(1, len(token_set_a | token_set_b))
    )

    phrase_score = SequenceMatcher(
        None,
        a_norm,
        b_norm,
    ).ratio()

    # Compare individual words too. This helps suggest close spellings such
    # as oval / owal / ovel when the Master Product contains "owel".
    word_scores = [
        SequenceMatcher(None, sku_word, product_word).ratio()
        for sku_word in a_tokens
        for product_word in b_tokens
    ]
    word_score = max(word_scores) if word_scores else 0.0

    return max(
        token_score,
        phrase_score,
        word_score,
    )


def phrase_tokens_exist_in_sku(master_name, sku):
    """
    True when the complete Master Product phrase occurs as consecutive words
    inside the extracted SKU. Matching ignores case, underscores and hyphens.
    """
    master_tokens = normalize_text(master_name).split()
    sku_tokens = normalize_text(sku).split()

    # The requested automatic rule is based on an exact two-word (or longer)
    # Master Product phrase.
    if len(master_tokens) < 2:
        return False

    if len(master_tokens) > len(sku_tokens):
        return False

    for index in range(
        len(sku_tokens) - len(master_tokens) + 1
    ):
        if sku_tokens[
            index:index + len(master_tokens)
        ] == master_tokens:
            return True

    return False


def get_ranked_master_product_candidates(
    sku,
    master_products,
    minimum_score=0.35,
    limit=5,
):
    candidates = []

    for product in master_products:
        name = get_master_product_name(product)
        if not name:
            continue

        score = product_similarity(sku, name)

        if score >= minimum_score:
            candidates.append(
                {
                    "name": name,
                    "score": round(score, 4),
                }
            )

    candidates.sort(
        key=lambda item: item["score"],
        reverse=True,
    )

    return candidates[:limit]


def find_matching_master_product(
    extracted,
    mappings,
    master_products,
):
    """
    Matching order:

    1. A previously saved SKU mapping -> assign immediately. This is the only
       situation where the app assigns a SKU without asking again.
    2. For a SKU seen for the first time, never assign it automatically, even
       when an exact Master Product phrase appears in the SKU. Instead, show
       the best possible Master Product suggestions and wait for the user's
       decision.
    3. Once the user explicitly assigns a SKU to a Master Product, that exact
       normalized SKU mapping is saved and will be reused automatically in
       future uploads. Matching ignores case, underscores and hyphens.
    """
    sku = str(extracted.get("sku", "") or "").strip()
    sku_norm = normalize_text(sku)

    if not sku_norm:
        return (
            None,
            "No SKU extracted",
            0.0,
            [],
        )

    # Priority 1: saved exact SKU mapping.
    for mapping in mappings:
        if normalize_text(
            mapping.get("sku")
        ) == sku_norm:
            return (
                mapping.get("master_product_name"),
                "Saved SKU mapping",
                1.0,
                [],
            )

    # Priority 2: exact Master Product phrase inside the SKU.
    # IMPORTANT: an exact phrase match is only a suggestion for a SKU that has
    # never been manually mapped before. The user must approve it the first
    # time. After approval, Priority 1 above remembers the mapping and future
    # occurrences are assigned automatically.
    exact_phrase_matches = []

    for product in master_products:
        name = get_master_product_name(product)

        if not name:
            continue

        if phrase_tokens_exist_in_sku(name, sku):
            exact_phrase_matches.append(name)

    if exact_phrase_matches:
        exact_phrase_matches.sort(
            key=lambda value: len(
                normalize_text(value).split()
            ),
            reverse=True,
        )

        candidates = [
            {"name": name, "score": 1.0}
            for name in exact_phrase_matches
        ]

        # Add other possible similar Master Products after the exact matches so
        # the user can still choose a different product if necessary.
        existing = {
            normalize_text(candidate["name"])
            for candidate in candidates
        }
        for candidate in get_ranked_master_product_candidates(
            sku,
            master_products,
            minimum_score=0.0,
        ):
            if (
                normalize_text(candidate["name"])
                not in existing
            ):
                candidates.append(candidate)

        return (
            None,
            "Review required: exact Master Product phrase found",
            1.0,
            candidates[:5],
        )

    # Priority 3: similar or otherwise unmatched first-time SKUs also require
    # user confirmation. Return the best available choices; never assign one
    # automatically until the user has explicitly approved a mapping.
    candidates = get_ranked_master_product_candidates(
        sku,
        master_products,
        minimum_score=0.0,
    )

    if candidates:
        best = candidates[0]

        return (
            None,
            (
                "Review required: suggested match "
                f"{best['name']} ({best['score']:.0%})"
            ),
            best["score"],
            candidates,
        )

    return (
        None,
        "No Master Products available for review",
        0.0,
        [],
    )


# ============================================================
# PDF ORGANIZATION
# ============================================================

def reorganize_pdfs(uploaded_files):
    mappings = get_user_mappings()
    master_products = get_inventory()

    categorized_pages = {}
    uncategorized_pages = []
    extracted_rows = []
    auto_mappings = []
    remembered_assignments = []
    remembered_assignment_keys = set()
    review_candidates = {}

    known_skus = {
        normalize_text(mapping.get("sku"))
        for mapping in mappings
        if mapping.get("sku")
    }

    for uploaded_file in uploaded_files:
        document = fitz.open(
            stream=uploaded_file.getvalue(),
            filetype="pdf",
        )

        try:
            for page_number in range(len(document)):
                page = document.load_page(page_number)
                details = parse_product_details(
                    extract_text_from_page(page)
                )

                (
                    master_product,
                    match_method,
                    confidence,
                    candidates,
                ) = find_matching_master_product(
                    details,
                    mappings,
                    master_products,
                )

                sku_key = normalize_text(details["sku"])

                # A SKU that was manually mapped earlier is remembered in the
                # database. When the exact SKU appears again, it is assigned
                # immediately without asking the user again. Keep a separate
                # batch record so the interface can clearly notify the user.
                if (
                    master_product
                    and sku_key
                    and match_method == "Saved SKU mapping"
                    and sku_key not in remembered_assignment_keys
                ):
                    remembered_assignment_keys.add(sku_key)
                    remembered_assignments.append(
                        {
                            "SKU": details["sku"],
                            "Master Product": master_product,
                            "Match Method": (
                                "Automatically assigned from your previous "
                                "SKU Mapping"
                            ),
                        }
                    )

                # Do not expose the source PDF name in the extracted data.
                extracted_rows.append(
                    {
                        "Page": page_number + 1,
                        "SKU": details["sku"],
                        "Size": details["size"],
                        "Qty": details["qty"],
                        "Color": details["color"],
                        "Master Product": (
                            master_product
                            or "Needs review"
                        ),
                        "Match Method": match_method,
                    }
                )

                # Never create a new mapping automatically for a first-time
                # SKU. A mapping is persisted only after the user explicitly
                # assigns the SKU on the review screen or in SKU Mappings.
                # Existing saved mappings are still reused automatically.

                # Keep one review item per unique extracted SKU.
                if (
                    not master_product
                    and sku_key
                    and candidates
                    and sku_key not in review_candidates
                ):
                    review_candidates[sku_key] = {
                        "sku": details["sku"],
                        "candidates": candidates,
                        "row_indexes": [
                            len(extracted_rows) - 1
                        ],
                    }
                elif (
                    not master_product
                    and sku_key
                    and candidates
                    and sku_key in review_candidates
                ):
                    review_candidates[sku_key][
                        "row_indexes"
                    ].append(
                        len(extracted_rows) - 1
                    )

                single_page_pdf = fitz.open()
                single_page_pdf.insert_pdf(
                    document,
                    from_page=page_number,
                    to_page=page_number,
                )

                item = {
                    "pdf": single_page_pdf,
                    "details": details,
                    "sku": details["sku"],
                    "master_product": master_product,
                }

                if master_product:
                    categorized_pages.setdefault(
                        master_product,
                        [],
                    ).append(item)
                else:
                    uncategorized_pages.append(item)

        finally:
            document.close()

    return {
        "categorized": categorized_pages,
        "uncategorized": uncategorized_pages,
        "extracted_rows": extracted_rows,
        "auto_mappings": auto_mappings,
        "remembered_assignments": remembered_assignments,
        "review_candidates": review_candidates,
    }


def apply_review_assignment(
    results,
    sku,
    master_product_name,
    match_method,
):
    """
    Apply a user-approved similar match to every extracted page having the
    same SKU, persist the mapping, and update the current batch in memory.
    """
    sku = str(sku or "").strip()
    master_product_name = str(
        master_product_name or ""
    ).strip()

    if not sku or not master_product_name:
        return False

    if not save_sku_mapping(
        sku,
        master_product_name,
    ):
        return False

    sku_key = normalize_text(sku)

    categorized = results.setdefault(
        "categorized",
        {},
    )
    uncategorized = results.setdefault(
        "uncategorized",
        [],
    )

    still_uncategorized = []

    for item in uncategorized:
        if normalize_text(
            item.get("sku")
        ) == sku_key:
            item["master_product"] = master_product_name
            categorized.setdefault(
                master_product_name,
                [],
            ).append(item)
        else:
            still_uncategorized.append(item)

    results["uncategorized"] = still_uncategorized

    for row in results.get(
        "extracted_rows",
        [],
    ):
        if normalize_text(
            row.get("SKU")
        ) == sku_key:
            row["Master Product"] = master_product_name
            row["Match Method"] = match_method

    review_candidates = results.get(
        "review_candidates",
        {},
    )
    review_candidates.pop(
        sku_key,
        None,
    )

    return True


def create_reorganized_pdf(categorized_pages, uncategorized_pages):
    output = fitz.open()

    try:
        for product in sorted(
            categorized_pages.keys(),
            key=lambda value: value.lower(),
        ):
            for item in categorized_pages[product]:
                output.insert_pdf(item["pdf"])

        for item in uncategorized_pages:
            output.insert_pdf(item["pdf"])

        return output.tobytes()

    finally:
        output.close()


def create_pickup_list(categorized_pages):
    rows = []

    for product, pages in categorized_pages.items():
        rows.append(
            {
                "Master Product": product,
                "Labels / Orders": len(pages),
                "Total Quantity": sum(
                    int(item["details"].get("qty", 1) or 1)
                    for item in pages
                ),
            }
        )

    dataframe = pd.DataFrame(
        rows,
        columns=[
            "Master Product",
            "Labels / Orders",
            "Total Quantity",
        ],
    )

    if not dataframe.empty:
        dataframe = dataframe.sort_values(
            "Master Product",
            key=lambda column: column.str.lower(),
        )

    return dataframe


def deduct_inventory_from_pickup(pickup_dataframe):
    inventory = get_inventory()

    inventory_lookup = {
        normalize_text(product.get("product_name")): product
        for product in inventory
    }

    successful = 0
    failed = []

    for _, row in pickup_dataframe.iterrows():
        product_name = str(row["Master Product"]).strip()
        quantity_needed = int(
            row.get("Total Quantity", 0) or 0
        )

        product = inventory_lookup.get(
            normalize_text(product_name)
        )

        if not product:
            failed.append(
                f"{product_name}: Master Product not found."
            )
            continue

        current = int(
            product.get("inventory_quantity", 0) or 0
        )

        if update_inventory(
            product["id"],
            max(0, current - quantity_needed),
            "Automatic deduction from organized PDF batch",
        ):
            successful += 1
        else:
            failed.append(
                f"{product_name}: Inventory update failed."
            )

    return successful, failed


# ============================================================
# AUTH PAGE
# ============================================================

def show_auth_page():
    st.title("📦 Meesho Label Organizer")

    st.markdown(
        """
        ### Organize your Meesho labels intelligently

        - Extract SKU, Size, Quantity and Color from every PDF page
        - Automatically group labels under Master Products
        - Learn high-confidence SKU mappings automatically
        - Generate organized PDFs and Pick-Up Lists
        - Edit and delete Master Products
        - Manage inventory and low-stock limits
        """
    )

    login_tab, register_tab = st.tabs(
        ["🔐 Login", "📝 Register"]
    )

    with login_tab:
        email = st.text_input(
            "Email",
            key="login_email",
        )
        password = st.text_input(
            "Password",
            type="password",
            key="login_password",
        )

        if st.button(
            "Login",
            type="primary",
            use_container_width=True,
        ):
            if not email or not password:
                st.warning(
                    "Please enter your email and password."
                )
            else:
                login_user(email, password)

    with register_tab:
        company_name = st.text_input(
            "Company Name",
            key="register_company",
        )
        email = st.text_input(
            "Email",
            key="register_email",
        )
        password = st.text_input(
            "Password",
            type="password",
            key="register_password",
        )
        confirm_password = st.text_input(
            "Confirm Password",
            type="password",
            key="register_confirm_password",
        )

        if st.button(
            "Create Account",
            type="primary",
            use_container_width=True,
        ):
            if password != confirm_password:
                st.error("Passwords do not match.")
            elif len(password) < 6:
                st.error(
                    "Password must contain at least 6 characters."
                )
            else:
                register_user(
                    company_name,
                    email,
                    password,
                )


# ============================================================
# DASHBOARD
# ============================================================

def show_dashboard():
    st.title("📦 Meesho Label Organizer Dashboard")

    profile = st.session_state.get("profile") or {}
    company_name = (
        profile.get("company_name")
        or (
            "Administrator"
            if is_admin()
            else "Your Company"
        )
    )

    st.write(f"### Welcome, {company_name} 👋")

    subscription = get_subscription_status()

    st.caption(
        f"Plan: {subscription['plan']} | "
        f"{subscription['reason']}"
    )

    inventory = get_inventory()
    low_stock = get_low_stock_products()

    col1, col2, col3 = st.columns(3)

    col1.metric("Master Products", len(inventory))
    col2.metric(
        "Total Inventory",
        sum(
            int(item.get("inventory_quantity", 0) or 0)
            for item in inventory
        ),
    )
    col3.metric("Low Stock Products", len(low_stock))

    st.divider()

    if low_stock:
        st.error("⚠️ LOW STOCK ALERTS")

        for product in low_stock:
            st.warning(
                f"📦 **{product.get('product_name', 'Unnamed Product')}** — "
                f"Current Stock: "
                f"{int(product.get('inventory_quantity', 0) or 0)} | "
                f"Minimum Limit: "
                f"{int(product.get('minimum_stock', 0) or 0)}"
            )
    else:
        st.success(
            "✅ All products are currently above their minimum stock limits."
        )


# ============================================================
# MASTER PRODUCTS PAGE
# ============================================================

def show_master_products():
    st.title("🏷️ Master Products")

    with st.expander(
        "➕ Add New Master Product",
        expanded=False,
    ):
        with st.form("add_master_product_form"):
            product_name = st.text_input(
                "Master Product Name"
            )

            col1, col2 = st.columns(2)

            with col1:
                initial_quantity = st.number_input(
                    "Initial Inventory Quantity",
                    min_value=0,
                    value=0,
                    step=1,
                )

            with col2:
                minimum_stock = st.number_input(
                    "Minimum Stock Alert Limit",
                    min_value=0,
                    value=0,
                    step=1,
                )

            submitted = st.form_submit_button(
                "Add Master Product",
                type="primary",
            )

        if submitted:
            if not product_name.strip():
                st.warning(
                    "Please enter a Master Product name."
                )
            elif add_master_product(
                product_name,
                initial_quantity,
                minimum_stock,
            ):
                st.success(
                    "Master Product added successfully!"
                )
                st.rerun()

    inventory = get_inventory()

    if not inventory:
        st.info(
            "No Master Products have been created yet."
        )
        return

    st.subheader("📋 Existing Master Products")

    display_rows = [
        {
            "ID": item.get("id"),
            "Master Product": item.get("product_name", ""),
            "Quantity": item.get("inventory_quantity", 0),
            "Minimum Stock": item.get("minimum_stock", 0),
        }
        for item in inventory
    ]

    st.dataframe(
        pd.DataFrame(display_rows),
        use_container_width=True,
        hide_index=True,
    )

    st.divider()

    # ---------------- EDIT ----------------

    st.subheader("✏️ Edit Master Product")

    edit_options = {
        (
            f"{item.get('product_name', 'Unnamed Product')} "
            f"— {str(item.get('id', ''))[:8]}"
        ): item
        for item in inventory
    }

    selected_edit_label = st.selectbox(
        "Select a Master Product to edit",
        list(edit_options.keys()),
        key="edit_master_product_selector",
    )

    selected_product = edit_options[selected_edit_label]
    product_id = selected_product["id"]

    with st.form(
        f"edit_master_product_form_{product_id}"
    ):
        edited_name = st.text_input(
            "Master Product Name",
            value=str(
                selected_product.get(
                    "product_name",
                    "",
                )
            ),
        )

        col1, col2 = st.columns(2)

        with col1:
            edited_quantity = st.number_input(
                "Current Inventory Quantity",
                min_value=0,
                value=int(
                    selected_product.get(
                        "inventory_quantity",
                        0,
                    )
                    or 0
                ),
                step=1,
            )

        with col2:
            edited_minimum = st.number_input(
                "Minimum Stock Alert Limit",
                min_value=0,
                value=int(
                    selected_product.get(
                        "minimum_stock",
                        0,
                    )
                    or 0
                ),
                step=1,
            )

        save_edit = st.form_submit_button(
            "💾 Save Master Product Changes",
            type="primary",
        )

    if save_edit:
        if not edited_name.strip():
            st.error(
                "Master Product name cannot be empty."
            )
        else:
            old_name = selected_product.get(
                "product_name",
                "",
            )

            response = update_master_product(
                product_id,
                edited_name,
                edited_quantity,
                edited_minimum,
            )

            if response is not None:
                if edited_name.strip() != str(old_name).strip():
                    try:
                        master_column = _get_sku_mapping_master_column()
                        if master_column:
                            if master_column == "master_product_id":
                                old_value = selected_product.get("id")
                                new_value = selected_product.get("id")
                            else:
                                old_value = old_name
                                new_value = edited_name.strip()
                            (
                                supabase.table("sku_mappings")
                                .update({master_column: new_value})
                                .eq("company_id", get_company_id())
                                .eq(master_column, old_value)
                                .execute()
                            )
                    except Exception:
                        pass

                st.success(
                    "Master Product updated successfully!"
                )
                st.rerun()

    st.divider()

    # ---------------- DELETE ----------------

    st.subheader("🗑️ Delete Master Product")

    delete_options = {
        (
            f"{item.get('product_name', 'Unnamed Product')} "
            f"— {str(item.get('id', ''))[:8]}"
        ): item
        for item in inventory
    }

    selected_delete_label = st.selectbox(
        "Select the Master Product you want to delete",
        ["Select a product..."] + list(
            delete_options.keys()
        ),
        key="delete_master_product_selector",
    )

    if selected_delete_label != "Select a product...":
        product = delete_options[selected_delete_label]

        st.warning(
            f"You are about to permanently delete "
            f"**{product.get('product_name', 'this product')}**."
        )

        confirm = st.checkbox(
            "I understand that this action cannot be undone.",
            key="delete_master_product_confirmation",
        )

        if st.button(
            "🗑️ Delete Selected Master Product",
            disabled=not confirm,
            type="primary",
        ):
            if delete_master_product(
                product["id"],
                product.get("product_name", ""),
            ):
                st.success(
                    "Master Product deleted successfully."
                )
                st.rerun()


# ============================================================
# SKU MAPPINGS PAGE
# ============================================================

def show_sku_mappings():
    st.title("🔗 SKU → Master Product Mapping")

    inventory = get_inventory()

    if not inventory:
        st.warning(
            "Create at least one Master Product first."
        )
        return

    product_names = [
        item.get("product_name")
        for item in inventory
        if item.get("product_name")
    ]

    with st.expander(
        "➕ Add or Update SKU Mapping",
        expanded=True,
    ):
        with st.form("add_sku_mapping_form"):
            selected_product = st.selectbox(
                "Master Product",
                product_names,
            )

            sku = st.text_input(
                "SKU / Product Name Extracted From PDF"
            )

            save = st.form_submit_button(
                "Save SKU Mapping",
                type="primary",
            )

        if save and save_sku_mapping(
            sku,
            selected_product,
        ):
            st.success(
                "SKU mapping saved successfully!"
            )
            st.rerun()

    mappings = get_user_mappings()

    if not mappings:
        st.info(
            "No mappings exist yet. First-time SKU matches will always ask for "
            "your confirmation. After you save a mapping, that same SKU will "
            "be remembered and assigned automatically in future uploads."
        )
        return

    display = pd.DataFrame(mappings)

    columns = [
        column
        for column in [
            "master_product_name",
            "sku",
        ]
        if column in display.columns
    ]

    st.dataframe(
        display[columns],
        use_container_width=True,
        hide_index=True,
    )

    if "id" in display.columns:
        mapping_options = {
            (
                f"{row.get('sku', '')} → "
                f"{row.get('master_product_name', '')}"
            ): row.get("id")
            for _, row in display.iterrows()
        }

        selected_mapping = st.selectbox(
            "Delete a mapping",
            ["Select a mapping..."]
            + list(mapping_options.keys()),
        )

        if (
            selected_mapping
            != "Select a mapping..."
        ):
            if st.button("Delete Selected Mapping"):
                if delete_sku_mapping(
                    mapping_options[selected_mapping]
                ):
                    st.success("SKU mapping deleted.")
                    st.rerun()


# ============================================================
# PDF ORGANIZER PAGE
# ============================================================

def show_pdf_organizer():
    st.title("📄 PDF Label Organizer")

    st.caption(
        "Every page is processed individually. SKU, Size, Qty and Color "
        "are extracted, matched to a Master Product, automatically saved "
        "when the match is reliable, and reordered under that product."
    )

    subscription = get_subscription_status()

    if not subscription["access"]:
        st.warning(
            "You need an active plan or demo to use this feature."
        )

        if st.button("View Plans"):
            st.session_state.current_page = "Subscription"
            st.rerun()

        return

    used_pdfs = count_demo_pdfs()

    if subscription["plan"] == "Demo":
        remaining = max(
            0,
            DEMO_PDF_LIMIT - used_pdfs,
        )

        st.info(
            f"Demo usage: {used_pdfs}/{DEMO_PDF_LIMIT} PDFs "
            f"({remaining} remaining)"
        )

        if remaining <= 0:
            st.error(
                "Your demo PDF limit has been reached."
            )
            return

    uploaded_files = st.file_uploader(
        "Upload Meesho Label PDFs",
        type=["pdf"],
        accept_multiple_files=True,
    )

    if uploaded_files and st.button(
        "🚀 Extract, Map & Organize Labels",
        type="primary",
        use_container_width=True,
    ):
        if (
            subscription["plan"] == "Demo"
            and used_pdfs + len(uploaded_files)
            > DEMO_PDF_LIMIT
        ):
            st.error(
                "This upload exceeds your remaining demo PDF limit."
            )
            return

        with st.spinner(
            "Reading every PDF page and organizing labels..."
        ):
            try:
                results = reorganize_pdfs(uploaded_files)
                st.session_state.batch_results = results

                try:
                    (
                        supabase.table("pdf_batches")
                        .insert(
                            {
                                "user_id": get_current_user_id(),
                                "company_id": get_company_id(),
                                "pdf_count": len(uploaded_files),
                            }
                        )
                        .execute()
                    )
                except Exception:
                    pass

                st.success(
                    "Labels extracted and organized successfully!"
                )

                # Popup notifications for SKU mappings remembered from a
                # previous manual assignment in SKU Mappings or review mode.
                for assignment in results.get(
                    "remembered_assignments",
                    [],
                ):
                    st.toast(
                        (
                            f"🔁 {assignment['SKU']} was automatically "
                            f"assigned to '{assignment['Master Product']}' "
                            "because you assigned this SKU to that Master "
                            "Product previously."
                        ),
                        icon="🔁",
                    )

            except Exception as e:
                st.error(f"PDF processing failed: {e}")
                return

    results = st.session_state.get("batch_results")

    if not results:
        return

    categorized = results["categorized"]
    uncategorized = results["uncategorized"]

    st.divider()

    st.subheader("🔎 Extracted Product Details")

    extracted_df = pd.DataFrame(
        results.get("extracted_rows", [])
    )

    if not extracted_df.empty:
        # The extracted data intentionally contains no Source PDF column.
        preferred_columns = [
            "Page",
            "SKU",
            "Size",
            "Qty",
            "Color",
            "Master Product",
            "Match Method",
        ]
        visible_columns = [
            column
            for column in preferred_columns
            if column in extracted_df.columns
        ]

        st.dataframe(
            extracted_df[visible_columns],
            use_container_width=True,
            hide_index=True,
        )

        st.download_button(
            "⬇️ Download Extracted Data CSV",
            data=extracted_df[
                visible_columns
            ].to_csv(
                index=False
            ).encode("utf-8"),
            file_name="meesho_extracted_product_details.csv",
            mime="text/csv",
        )

    # --------------------------------------------------------
    # SIMILAR SKU REVIEW
    # --------------------------------------------------------

    review_candidates = results.get(
        "review_candidates",
        {},
    )

    if review_candidates:
        st.divider()
        st.subheader("🔍 Review Similar SKU Matches")
        st.info(
            "Every SKU that has not been assigned by you previously requires "
            "your confirmation. The app will suggest the best possible Master "
            "Products, but it will not assign a first-time SKU automatically. "
            "Once you assign it, the mapping is remembered and the same SKU "
            "will be assigned automatically in future uploads."
        )

        for review_key, review in list(
            review_candidates.items()
        ):
            sku = review.get("sku", "")
            candidates = review.get(
                "candidates",
                [],
            )

            if not sku or not candidates:
                continue

            with st.expander(
                f"SKU: {sku}",
                expanded=True,
            ):
                best = candidates[0]

                st.write(
                    f"**Highest possible match:** "
                    f"{best['name']} ({best['score']:.0%})"
                )

                st.caption(
                    "Other possible Master Products are listed below. "
                    "Choose one to assign this SKU."
                )

                candidate_options = [
                    (
                        f"{candidate['name']} "
                        f"— {candidate['score']:.0%}"
                    )
                    for candidate in candidates
                ]

                selected_label = st.selectbox(
                    "Possible Master Product",
                    candidate_options,
                    key=f"review_master_{review_key}",
                )

                selected_index = candidate_options.index(
                    selected_label
                )
                selected_master = candidates[
                    selected_index
                ]["name"]

                col1, col2 = st.columns(2)

                with col1:
                    if st.button(
                        "Assign Selected Master Product",
                        key=f"assign_review_{review_key}",
                        type="primary",
                    ):
                        if apply_review_assignment(
                            results,
                            sku,
                            selected_master,
                            "User-approved similar match",
                        ):
                            st.success(
                                f"'{sku}' was assigned to "
                                f"'{selected_master}'."
                            )
                            st.session_state.batch_results = results
                            st.rerun()

                with col2:
                    new_master_name = st.text_input(
                        "Or create a new Master Product",
                        placeholder="Enter new Master Product name",
                        key=f"new_master_{review_key}",
                    )

                    if st.button(
                        "Create & Assign",
                        key=f"create_assign_{review_key}",
                    ):
                        new_master_name = (
                            new_master_name.strip()
                        )

                        if not new_master_name:
                            st.warning(
                                "Enter a new Master Product name first."
                            )
                        else:
                            existing_names = {
                                normalize_text(
                                    get_master_product_name(
                                        product
                                    )
                                )
                                for product in get_inventory()
                            }

                            if normalize_text(
                                new_master_name
                            ) not in existing_names:
                                if not add_master_product(
                                    new_master_name,
                                    0,
                                    0,
                                ):
                                    st.error(
                                        "Could not create the new "
                                        "Master Product."
                                    )
                                    continue

                            if apply_review_assignment(
                                results,
                                sku,
                                new_master_name,
                                (
                                    "New Master Product created "
                                    "and assigned"
                                ),
                            ):
                                st.success(
                                    f"Created/used '{new_master_name}' "
                                    f"and assigned '{sku}'."
                                )
                                st.session_state.batch_results = results
                                st.rerun()

    remembered_assignments = results.get(
        "remembered_assignments",
        [],
    )

    if remembered_assignments:
        st.info(
            f"🔁 {len(remembered_assignments)} SKU(s) were automatically "
            "assigned using mappings you saved previously. No confirmation "
            "was needed because those SKU-to-Master-Product assignments are "
            "remembered for future uploads."
        )

        st.dataframe(
            pd.DataFrame(remembered_assignments),
            use_container_width=True,
            hide_index=True,
        )

    auto_mappings = results.get(
        "auto_mappings",
        [],
    )

    if auto_mappings:
        st.success(
            f"🤖 {len(auto_mappings)} new SKU mapping(s) "
            f"were automatically saved."
        )

        st.dataframe(
            pd.DataFrame(auto_mappings),
            use_container_width=True,
            hide_index=True,
        )

    st.subheader("📊 Organization Results")

    total_categorized = sum(
        len(pages)
        for pages in categorized.values()
    )

    col1, col2, col3 = st.columns(3)

    col1.metric(
        "Master Product Categories",
        len(categorized),
    )

    col2.metric(
        "Organized Labels",
        total_categorized,
    )

    col3.metric(
        "Uncategorized Labels",
        len(uncategorized),
    )

    pickup_dataframe = create_pickup_list(
        categorized
    )

    st.subheader("📋 Pick-Up List")

    st.dataframe(
        pickup_dataframe,
        use_container_width=True,
        hide_index=True,
    )

    st.download_button(
        "⬇️ Download Pick-Up List CSV",
        data=pickup_dataframe.to_csv(
            index=False
        ).encode("utf-8"),
        file_name="pickup_list.csv",
        mime="text/csv",
    )

    if not pickup_dataframe.empty:
        st.subheader("📦 Inventory Action")

        st.warning(
            "This subtracts the extracted Total Quantity from inventory."
        )

        if st.button(
            "➖ Deduct Pick-Up List From Inventory",
            type="primary",
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

    st.subheader("📄 Download Reorganized Labels")

    if categorized or uncategorized:
        output_pdf = create_reorganized_pdf(
            categorized,
            uncategorized,
        )

        st.download_button(
            "⬇️ Download Reorganized PDF",
            data=output_pdf,
            file_name="reorganized_meesho_labels.pdf",
            mime="application/pdf",
            type="primary",
            use_container_width=True,
        )

    if uncategorized:
        st.warning(
            f"{len(uncategorized)} page(s) were left uncategorized "
            "because no sufficiently confident Master Product match was found."
        )


# ============================================================
# INVENTORY PAGE
# ============================================================

def show_inventory():
    st.title("📦 Inventory Management")

    inventory = get_inventory()

    if not inventory:
        st.info(
            "No products available. Add Master Products first."
        )
        return

    for product in inventory:
        product_id = product["id"]

        name = (
            product.get("product_name")
            or "Unnamed Product"
        )

        current_quantity = int(
            product.get("inventory_quantity", 0) or 0
        )

        minimum_stock = int(
            product.get("minimum_stock", 0) or 0
        )

        with st.expander(
            f"📦 {name}",
            expanded=False,
        ):
            col1, col2 = st.columns(2)

            with col1:
                new_quantity = st.number_input(
                    "Current Quantity",
                    min_value=0,
                    value=current_quantity,
                    step=1,
                    key=f"inventory_quantity_{product_id}",
                )

            with col2:
                new_minimum = st.number_input(
                    "Minimum Stock Limit",
                    min_value=0,
                    value=minimum_stock,
                    step=1,
                    key=f"inventory_minimum_{product_id}",
                )

            if st.button(
                "💾 Save Inventory Changes",
                key=f"save_inventory_{product_id}",
            ):
                try:
                    (
                        supabase.table("master_products")
                        .update(
                            {
                                "inventory_quantity": int(new_quantity),
                                "minimum_stock": int(new_minimum),
                            }
                        )
                        .eq("id", product_id)
                        .eq(
                            "company_id",
                            get_company_id(),
                        )
                        .execute()
                    )

                    st.success(
                        "Inventory updated successfully!"
                    )
                    st.rerun()

                except Exception as e:
                    st.error(
                        f"Could not update inventory: {e}"
                    )


# ============================================================
# SUBSCRIPTION PAGE
# ============================================================

def create_payment_record(plan, amount):
    try:
        (
            supabase.table("payments")
            .insert(
                {
                    "user_id": get_current_user_id(),
                    "company_id": get_company_id(),
                    "plan": plan,
                    "amount": amount,
                    "status": "pending",
                }
            )
            .execute()
        )

        st.info(
            "Payment record created. Connect a real payment gateway "
            "before treating this as a completed payment."
        )

    except Exception as e:
        st.error(
            f"Payment initialization failed: {e}"
        )


def show_subscription_page():
    st.title("Choose Your Plan")

    if is_admin():
        st.success(
            "🛡️ Administrator account: complete application access "
            "without a subscription."
        )
        return

    current_status = get_subscription_status()

    if current_status["access"]:
        st.success(
            f"Your {current_status['plan']} access is active."
        )
        st.info(current_status["reason"])
        return

    profile = st.session_state.get("profile") or {}
    demo_started = profile.get("demo_started_at")

    col1, col2, col3 = st.columns(3)

    with col1:
        st.subheader("🆓 Demo")
        st.write(f"⏰ {DEMO_HOURS} hours access")
        st.write(f"📄 Maximum {DEMO_PDF_LIMIT} PDFs")

        if not demo_started:
            if st.button(
                "Start Free Demo",
                use_container_width=True,
            ):
                if start_demo():
                    st.success("Demo started!")
                    st.rerun()
        else:
            st.warning("Demo already used.")

    with col2:
        st.subheader("💳 Monthly")
        st.markdown(f"## ₹{MONTHLY_PRICE}/month")

        if st.button(
            "Choose Monthly Plan",
            use_container_width=True,
            type="primary",
        ):
            create_payment_record(
                "monthly",
                MONTHLY_PRICE,
            )

    with col3:
        st.subheader("💎 Lifetime")
        st.markdown(f"## ₹{LIFETIME_PRICE}")

        if st.button(
            "Choose Lifetime Plan",
            use_container_width=True,
            type="primary",
        ):
            create_payment_record(
                "lifetime",
                LIFETIME_PRICE,
            )


# ============================================================
# MAIN APPLICATION
# ============================================================

def show_main_app():
    profile = st.session_state.get("profile") or {}

    company_name = (
        profile.get("company_name")
        or (
            "Administrator"
            if is_admin()
            else "Meesho Label Organizer"
        )
    )

    with st.sidebar:
        st.title("📦 Meesho Organizer")
        st.caption(company_name)

        st.divider()

        pages = [
            "Dashboard",
            "PDF Organizer",
            "Master Products",
            "SKU Mappings",
            "Inventory",
            "Subscription",
        ]

        for page in pages:
            if st.button(
                page,
                use_container_width=True,
                key=f"nav_{page}",
            ):
                st.session_state.current_page = page
                st.rerun()

        st.divider()

        subscription = get_subscription_status()

        st.caption(
            f"Current Plan: {subscription['plan']}"
        )

        if is_admin():
            st.success(
                "🛡️ Full administrator application access"
            )

        if st.button(
            "🚪 Logout",
            use_container_width=True,
        ):
            logout()

    page = st.session_state.current_page
    subscription = get_subscription_status()

    # Unsubscribed/expired users may stay logged in and view their dashboard
    # and plans, but paid features require an active monthly or lifetime plan
    # (or an active demo).
    restricted_pages = {
        "PDF Organizer",
        "Master Products",
        "SKU Mappings",
        "Inventory",
    }

    if page in restricted_pages and not subscription["access"]:
        st.warning(
            "Your account is logged in, but this feature requires an active "
            "subscription or demo."
        )
        st.info(subscription["reason"])
        if st.button("View Subscription Plans", type="primary"):
            st.session_state.current_page = "Subscription"
            st.rerun()
        return

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

if st.session_state.user is None:
    restored = restore_login_from_cookie()

    if restored is None:
        # IMPORTANT: Do not call st.rerun() in a loop here. The CookieManager
        # frontend component needs this run to remain alive long enough to read
        # the browser cookies and send its value back to Streamlit. Repeated
        # forced reruns can restart the component before it finishes, causing
        # the endless "Restoring your login session" -> logout cycle.
        st.info("Restoring your login session…")
        st.stop()

if st.session_state.user is None:
    show_auth_page()

# A successful login can happen during show_auth_page() in the same script run.
# Re-check the session afterwards instead of forcing an immediate rerun, because
# the current run must finish for CookieManager to write browser cookies.
if st.session_state.user is not None:
    # A missing profile is a database/profile issue, not an authentication issue.
    # Never log out or block an authenticated user because another user's profile
    # row is missing or temporarily unavailable.
    if st.session_state.profile is None:
        refresh_profile()

    show_main_app()
