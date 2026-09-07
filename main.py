import streamlit as st
from supabase import create_client, Client
import fitz  # PyMuPDF
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
# SUPABASE CONNECTION
# ============================================================

@st.cache_resource
def get_supabase():
    try:
        url = st.secrets["SUPABASE_URL"]
        key = st.secrets["SUPABASE_KEY"]
        return create_client(url, key)
    except Exception as e:
        st.error("Supabase configuration is missing.")
        st.stop()


supabase: Client = get_supabase()


# ============================================================
# CONSTANTS
# ============================================================

MONTHLY_PRICE = 399
LIFETIME_PRICE = 9999
DEMO_HOURS = 12
DEMO_PDF_LIMIT = 2
ADMIN_EMAILS = {"keyurtank8@gmail.com"}


# ============================================================
# SESSION STATE
# ============================================================

if "user" not in st.session_state:
    st.session_state.user = None

if "profile" not in st.session_state:
    st.session_state.profile = None

if "current_page" not in st.session_state:
    st.session_state.current_page = "Dashboard"

if "batch_results" not in st.session_state:
    st.session_state.batch_results = None


# ============================================================
# HELPER FUNCTIONS
# ============================================================

def now_utc():
    return datetime.now(timezone.utc)


def get_current_email():
    user = st.session_state.user
    return str(getattr(user, "email", "") or "").strip().lower()


def is_admin():
    """Administrator access is based only on an authenticated email."""
    return get_current_email() in ADMIN_EMAILS


def get_current_user_id():
    """Return the authenticated Supabase user's UUID safely."""
    user = st.session_state.get("user")
    return getattr(user, "id", None)


def format_datetime(value):
    if not value:
        return "Not available"

    try:
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))

        return value.strftime("%d %b %Y, %I:%M %p")
    except Exception:
        return str(value)


def safe_get_profile(user_id):
    try:
        response = (
            supabase
            .table("profiles")
            .select("*")
            .eq("id", user_id)
            .execute()
        )

        if response.data:
            return response.data[0]

    except Exception as e:
        st.error(f"Profile loading error: {e}")

    return None


def refresh_profile():
    if st.session_state.user:
        st.session_state.profile = safe_get_profile(
            st.session_state.user.id
        )


def get_company_id():
    profile = st.session_state.profile

    if not profile:
        return None

    # Supports different possible column names
    if "company_id" in profile and profile["company_id"]:
        return profile["company_id"]

    if "id" in profile:
        return profile["id"]

    return st.session_state.user.id


def get_subscription_status():
    # The configured administrator has complete application feature access.
    # This does NOT bypass Supabase database security/RLS.
    if is_admin():
        return {
            "access": True,
            "plan": "Administrator",
            "reason": "Administrator account with full application access"
        }

    profile = st.session_state.profile
    if not profile:
        return {
            "access": False,
            "plan": "none",
            "reason": "Profile not found"
        }

    user_id = st.session_state.user.id

    try:
        payments = (
            supabase.table("payments")
            .select("*")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .execute()
        )
        payment_data = payments.data or []
    except Exception:
        payment_data = []

    for payment in payment_data:
        status = str(payment.get("status", "")).lower()
        plan = str(payment.get("plan", "")).lower()

        if status in ["paid", "completed", "success"] and plan in ["lifetime", "permanent"]:
            return {
                "access": True,
                "plan": "Lifetime",
                "reason": "Lifetime plan active"
            }

    for payment in payment_data:
        status = str(payment.get("status", "")).lower()
        plan = str(payment.get("plan", "")).lower()
        created_at = payment.get("created_at")

        if status in ["paid", "completed", "success"] and plan == "monthly" and created_at:
            try:
                payment_date = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
                expiry = payment_date + timedelta(days=30)
                if now_utc() <= expiry:
                    return {
                        "access": True,
                        "plan": "Monthly",
                        "reason": f"Valid until {format_datetime(expiry)}"
                    }
            except Exception:
                pass

    demo_started = profile.get("demo_started_at")
    if demo_started:
        try:
            demo_start = datetime.fromisoformat(str(demo_started).replace("Z", "+00:00"))
            demo_end = demo_start + timedelta(hours=DEMO_HOURS)
            if now_utc() <= demo_end:
                return {
                    "access": True,
                    "plan": "Demo",
                    "reason": f"Demo active until {format_datetime(demo_end)}"
                }
        except Exception:
            pass

    return {
        "access": False,
        "plan": "Expired",
        "reason": "No active subscription"
    }



def start_demo(user_id):

    profile = safe_get_profile(user_id)

    if not profile:
        return False

    if profile.get("demo_started_at"):
        return False

    try:

        supabase.table("profiles").update({
            "demo_started_at": now_utc().isoformat()
        }).eq("id", user_id).execute()

        refresh_profile()

        return True

    except Exception as e:

        st.error(f"Could not start demo: {e}")

        return False


def count_demo_pdfs():
    """
    Return the total number of PDF files used during the demo.

    We sum pdf_count instead of counting database rows because one batch
    can contain multiple uploaded PDFs.
    """
    if not st.session_state.user:
        return 0

    user_id = st.session_state.user.id

    try:
        response = (
            supabase
            .table("pdf_batches")
            .select("pdf_count")
            .eq("user_id", user_id)
            .execute()
        )

        return sum(
            int(batch.get("pdf_count", 0) or 0)
            for batch in (response.data or [])
        )

    except Exception:
        return 0

# ============================================================
# AUTHENTICATION
# ============================================================

def is_existing_signup_response(user):
    """
    Supabase may intentionally hide whether an email already exists when
    email confirmation is enabled. In that situation sign_up can return
    a user object with an empty identities list.

    This helper keeps that behavior in one place.
    """
    if user is None:
        return False

    identities = getattr(user, "identities", None)

    return identities == []


def login_user(email, password):

    email = email.strip().lower()

    try:

        response = supabase.auth.sign_in_with_password({
            "email": email,
            "password": password
        })

        user = getattr(response, "user", None)

        if not user:
            st.error("Login failed. Please check your email and password.")
            return

        st.session_state.user = user
        refresh_profile()

        if st.session_state.profile is None:
            st.warning(
                "Login succeeded, but your company profile could not be "
                "loaded. Please make sure the Supabase profile trigger and "
                "RLS policies have been installed."
            )
        else:
            st.success("Login successful!")

        st.rerun()

    except Exception as e:

        message = str(e).lower()

        if (
            "invalid login credentials" in message
            or "invalid credentials" in message
        ):
            st.error("Incorrect email or password.")
        elif "email not confirmed" in message:
            st.error(
                "Please confirm your email first, then try logging in."
            )
        else:
            st.error(f"Login failed: {e}")


def check_email_registered(email):
    """
    Check the Supabase database before sign-up so an existing email can
    be reported clearly instead of relying on Supabase's obfuscated
    sign_up response.
    """
    try:
        result = supabase.rpc(
            "is_email_registered",
            {"check_email": email.strip().lower()}
        ).execute()

        return bool(getattr(result, "data", False))

    except Exception as e:
        # Do not silently continue if the SQL RPC has not been installed.
        st.error(
            "Unable to verify whether this email is already registered. "
            "Please make sure the Supabase SQL setup has been run."
        )
        return None


def register_user(company_name, email, password):

    company_name = company_name.strip()
    email = email.strip().lower()

    if not company_name:
        st.error("Please enter a company name.")
        return

    if not email:
        st.error("Please enter an email address.")
        return

    # Check first. This is required because Supabase can intentionally
    # return a successful-looking response for an existing email when
    # email confirmation is enabled.
    email_registered = check_email_registered(email)

    if email_registered is None:
        return

    if email_registered:
        st.error(
            "This email is already registered. Please log in instead."
        )
        return

    try:

        response = supabase.auth.sign_up({
            "email": email,
            "password": password,
            "options": {
                "data": {
                    "company_name": company_name
                }
            }
        })

        user = getattr(response, "user", None)

        if not user:
            st.error(
                "Registration could not be completed. Please try again."
            )
            return

        # Safety check for Supabase's obfuscated duplicate-email response.
        if is_existing_signup_response(user):
            st.error(
                "This email is already registered. Please log in instead."
            )
            return

        session = getattr(response, "session", None)

        if session is None:
            st.success(
                "Account created successfully! Please check your email and "
                "confirm your account before logging in."
            )
        else:
            st.success(
                "Account created successfully! Please log in."
            )

    except Exception as e:

        error_message = str(e).lower()

        if (
            "already registered" in error_message
            or "already exists" in error_message
            or "user already registered" in error_message
            or "email_exists" in error_message
            or "duplicate" in error_message
            or "email address is already in use" in error_message
        ):
            st.error(
                "This email is already registered. Please log in instead."
            )
        elif "password" in error_message and "least" in error_message:
            st.error(
                "Password does not meet the required security requirements."
            )
        else:
            st.error(f"Registration failed: {e}")

def logout():

    try:
        supabase.auth.sign_out()
    except Exception:
        pass

    st.session_state.user = None
    st.session_state.profile = None
    st.session_state.batch_results = None
    st.session_state.current_page = "Dashboard"

    st.rerun()


# ============================================================
# PDF FUNCTIONS
# ============================================================

def extract_text_from_page(page):

    try:
        return page.get_text("text")
    except Exception:
        return ""


def normalize_text(text):

    if not text:
        return ""

    text = text.lower()
    text = re.sub(r"\s+", " ", text)

    return text.strip()


def extract_product_section(page_text):
    """Return the Product Details row(s) from a single Meesho label page."""
    match = re.search(
        r"Product\s*Details\s*(.*?)(?:TAX\s*INVOICE|BILL\s*TO\s*/\s*SHIP\s*TO|Purchase\s*Order\s*No\.)",
        page_text or "",
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return ""

    section = match.group(1)
    section = re.sub(
        r"SKU\s+Size\s+Qty\s+Color\s+Order\s*No\.?",
        "",
        section,
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", section).strip()


def parse_product_details(page_text):
    """
    Extract SKU, Size, Qty, Color and Order No. from every PDF page.
    The SKU may wrap over multiple lines, so the row is parsed from right to left.
    """
    section = extract_product_section(page_text)
    result = {
        "sku": "",
        "size": "",
        "qty": 1,
        "color": "",
        "order_no": "",
        "raw_product_details": section,
    }
    if not section:
        return result

    working = section

    order_match = re.search(r"(\d{12,}(?:_\d+)?)\s*$", working)
    if order_match:
        result["order_no"] = order_match.group(1)
        working = working[:order_match.start()].strip()

    known_colors = [
        "Multicolor", "Rose Gold", "Green", "Maroon", "Red", "Blue",
        "Black", "White", "Gold", "Silver", "Yellow", "Pink", "Purple",
        "Orange", "Brown", "Grey", "Gray", "Beige", "Cream"
    ]
    color_pattern = "|".join(re.escape(c) for c in sorted(known_colors, key=len, reverse=True))
    color_match = re.search(rf"\b({color_pattern})\s*$", working, flags=re.IGNORECASE)
    if color_match:
        result["color"] = color_match.group(1).strip()
        working = working[:color_match.start()].strip()

    qty_match = re.search(r"\b(\d+)\s*$", working)
    if qty_match:
        result["qty"] = int(qty_match.group(1))
        working = working[:qty_match.start()].strip()

    size_match = re.search(
        r"\b(Free\s+Size|XXXL|XXL|XL|XS|S|M|L|One\s+Size|Small|Medium|Large)\s*$",
        working,
        flags=re.IGNORECASE,
    )
    if size_match:
        result["size"] = re.sub(r"\s+", " ", size_match.group(1)).strip()
        working = working[:size_match.start()].strip()

    result["sku"] = re.sub(r"\s+", " ", working).strip()
    return result


def product_similarity(a, b):
    a_norm = normalize_text(a)
    b_norm = normalize_text(b)
    if not a_norm or not b_norm:
        return 0.0

    a_tokens = set(a_norm.split())
    b_tokens = set(b_norm.split())
    token_score = len(a_tokens & b_tokens) / max(1, len(a_tokens | b_tokens))
    sequence_score = SequenceMatcher(None, a_norm, b_norm).ratio()
    return max(token_score, sequence_score)


def find_matching_master_product(extracted, mappings, master_products):
    """Match exact mappings first, then product names, then high-confidence similarity."""
    sku = str(extracted.get("sku", "")).strip()
    sku_norm = normalize_text(sku)
    if not sku_norm:
        return None, "No SKU extracted"

    for mapping in mappings:
        if normalize_text(mapping.get("sku", "")) == sku_norm:
            return mapping.get("master_product_name"), "Saved SKU mapping"

    for product in master_products:
        name = product.get("product_name") or product.get("master_product_name") or ""
        name_norm = normalize_text(name)
        if name_norm and (name_norm in sku_norm or sku_norm in name_norm):
            return name, "Automatic name match"

    best_name = None
    best_score = 0.0
    for product in master_products:
        name = product.get("product_name") or product.get("master_product_name") or ""
        score = product_similarity(sku, name)
        if score > best_score:
            best_score = score
            best_name = name

    if best_name and best_score >= 0.72:
        return best_name, f"Automatic similarity match ({best_score:.0%})"

    return None, "No confident match"



def get_user_mappings():

    company_id = get_company_id()

    if not company_id:
        return []

    try:

        response = (
            supabase
            .table("sku_mappings")
            .select("*")
            .eq("company_id", company_id)
            .execute()
        )

        return response.data or []

    except Exception as e:

        st.error(f"Could not load SKU mappings: {e}")

        return []


def reorganize_pdfs(uploaded_files):
    mappings = get_user_mappings()
    master_products = get_inventory()

    categorized_pages = {}
    uncategorized_pages = []
    extracted_rows = []
    auto_mappings = []

    known_skus = {
        normalize_text(mapping.get("sku", ""))
        for mapping in mappings
        if mapping.get("sku")
    }

    for uploaded_file in uploaded_files:
        pdf_bytes = uploaded_file.getvalue()
        document = fitz.open(stream=pdf_bytes, filetype="pdf")

        try:
            for page_number in range(len(document)):
                page = document.load_page(page_number)
                page_text = extract_text_from_page(page)
                details = parse_product_details(page_text)

                master_product, match_method = find_matching_master_product(
                    details,
                    mappings,
                    master_products,
                )

                extracted_rows.append({
                    "Source PDF": uploaded_file.name,
                    "Page": page_number + 1,
                    "SKU": details.get("sku", ""),
                    "Size": details.get("size", ""),
                    "Qty": details.get("qty", 1),
                    "Color": details.get("color", ""),
                    "Order No.": details.get("order_no", ""),
                    "Master Product": master_product or "Uncategorized",
                    "Match Method": match_method,
                })

                sku_key = normalize_text(details.get("sku", ""))
                if (
                    master_product
                    and sku_key
                    and sku_key not in known_skus
                    and match_method.startswith("Automatic")
                ):
                    try:
                        supabase.table("sku_mappings").insert({
                            "company_id": get_company_id(),
                            "user_id": get_current_user_id(),
                            "master_product_name": master_product,
                            "sku": details.get("sku", "").strip(),
                        }).execute()
                        known_skus.add(sku_key)
                        mappings.append({
                            "sku": details.get("sku", "").strip(),
                            "master_product_name": master_product,
                        })
                        auto_mappings.append({
                            "SKU": details.get("sku", "").strip(),
                            "Master Product": master_product,
                        })
                    except Exception:
                        # The PDF still gets organized even if automatic mapping
                        # persistence is blocked by a database policy.
                        pass

                single_page_pdf = fitz.open()
                single_page_pdf.insert_pdf(
                    document,
                    from_page=page_number,
                    to_page=page_number,
                )

                item = {"pdf": single_page_pdf, "details": details}
                if master_product:
                    categorized_pages.setdefault(master_product, []).append(item)
                else:
                    uncategorized_pages.append(item)
        finally:
            document.close()

    return {
        "categorized": categorized_pages,
        "uncategorized": uncategorized_pages,
        "extracted_rows": extracted_rows,
        "auto_mappings": auto_mappings,
    }



def create_reorganized_pdf(categorized_pages, uncategorized_pages):
    output = fitz.open()
    try:
        for product in sorted(categorized_pages.keys(), key=lambda x: x.lower()):
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
        rows.append({
            "Master Product": product,
            "Labels / Orders": len(pages),
            "Total Quantity": sum(
                int(item["details"].get("qty", 1) or 1)
                for item in pages
            ),
        })

    if not rows:
        return pd.DataFrame(columns=[
            "Master Product", "Labels / Orders", "Total Quantity"
        ])

    return pd.DataFrame(rows).sort_values(by="Master Product")



# ============================================================
# INVENTORY FUNCTIONS
# ============================================================

def get_inventory():

    company_id = get_company_id()

    if not company_id:
        return []

    try:

        response = (
            supabase
            .table("master_products")
            .select("*")
            .eq("company_id", company_id)
            .order("product_name")
            .execute()
        )

        return response.data or []

    except Exception as e:

        st.error(f"Inventory loading error: {e}")

        return []


def update_inventory(product_id, new_quantity):

    try:

        supabase.table("master_products").update({
            "inventory_quantity": int(new_quantity)
        }).eq("id", product_id).execute()

        return True

    except Exception as e:

        st.error(f"Inventory update failed: {e}")

        return False


def deduct_inventory_from_pickup(pickup_dataframe):
    inventory = get_inventory()
    inventory_lookup = {
        normalize_text(item.get("product_name") or item.get("master_product_name")): item
        for item in inventory
    }

    successful = 0
    failed = []

    for _, row in pickup_dataframe.iterrows():
        product_name = str(row["Master Product"]).strip()
        quantity_needed = int(row.get("Total Quantity", row["Labels / Orders"]))
        inventory_item = inventory_lookup.get(normalize_text(product_name))

        if not inventory_item:
            failed.append(f"{product_name}: Not found in inventory")
            continue

        current_quantity = int(inventory_item.get("inventory_quantity", 0) or 0)
        new_quantity = max(0, current_quantity - quantity_needed)

        if update_inventory(inventory_item["id"], new_quantity):
            successful += 1
            try:
                supabase.table("inventory_history").insert({
                    "company_id": get_company_id(),
                    "master_product_id": inventory_item["id"],
                    "change_quantity": -quantity_needed,
                    "reason": "Automatic deduction from organized PDF batch"
                }).execute()
            except Exception:
                pass
        else:
            failed.append(f"{product_name}: Could not update inventory")

    return successful, failed



# ============================================================
# LOW STOCK FUNCTION
# ============================================================

def get_low_stock_products():

    inventory = get_inventory()

    low_stock = []

    for product in inventory:

        quantity = int(
            product.get("inventory_quantity", 0) or 0
        )

        minimum_stock = int(
            product.get("minimum_stock", 0) or 0
        )

        if quantity <= minimum_stock:

            low_stock.append(product)

    return low_stock


# ============================================================
# AUTH SCREEN
# ============================================================

def show_auth_page():

    st.title("📦 Meesho Label Organizer")

    st.markdown(
        """
        ### Organize your Meesho labels intelligently

        - Group multiple SKUs under Master Products
        - Generate organized PDF labels
        - Create Pick-Up Lists
        - Manage inventory
        - Track low stock
        - Access your company data from multiple devices
        """
    )

    login_tab, register_tab = st.tabs([
        "🔐 Login",
        "📝 Register"
    ])

    with login_tab:

        st.subheader("Login to your company account")

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

            if not login_email or not login_password:

                st.warning(
                    "Please enter your email and password."
                )

            else:

                login_user(
                    login_email,
                    login_password
                )

    with register_tab:

        st.subheader("Create your company account")

        company_name = st.text_input(
            "Company Name",
            placeholder="Example: Jokerwal Brothers"
        )

        register_email = st.text_input(
            "Email",
            key="register_email"
        )

        register_password = st.text_input(
            "Password",
            type="password",
            key="register_password"
        )

        confirm_password = st.text_input(
            "Confirm Password",
            type="password"
        )

        st.info(
            f"""
            💳 Monthly Plan: ₹{MONTHLY_PRICE}/month

            💎 Lifetime Plan: ₹{LIFETIME_PRICE} one-time

            🆓 Demo: {DEMO_HOURS} hours with a maximum
            of {DEMO_PDF_LIMIT} PDF uploads.
            """
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

            elif register_password != confirm_password:

                st.error(
                    "Passwords do not match."
                )

            elif len(register_password) < 6:

                st.error(
                    "Password should contain at least 6 characters."
                )

            else:

                register_user(
                    company_name,
                    register_email,
                    register_password
                )


# ============================================================
# SUBSCRIPTION SCREEN
# ============================================================

def show_subscription_page():
    st.title("Choose Your Plan")

    if is_admin():
        st.success(
            "🛡️ Administrator account: complete application access without a subscription."
        )
        return

    user_id = st.session_state.user.id
    current_status = get_subscription_status()

    if current_status["access"]:
        st.success(f"Your {current_status['plan']} access is active.")
        st.info(current_status["reason"])
        if st.button("Go to Dashboard"):
            st.session_state.current_page = "Dashboard"
            st.rerun()
        return

    demo_started = (
        st.session_state.profile.get("demo_started_at")
        if st.session_state.profile
        else None
    )

    col1, col2, col3 = st.columns(3)

    with col1:
        st.subheader("🆓 Demo")
        st.write(f"⏰ {DEMO_HOURS} hours access")
        st.write(f"📄 Maximum {DEMO_PDF_LIMIT} PDFs")
        if not demo_started:
            if st.button("Start Free Demo", use_container_width=True):
                if start_demo(user_id):
                    st.success("Demo started!")
                    st.rerun()
        else:
            st.warning("Demo already used.")

    with col2:
        st.subheader("💳 Monthly")
        st.markdown(f"# ₹{MONTHLY_PRICE}")
        if st.button("Choose Monthly Plan", use_container_width=True, type="primary"):
            create_payment_record("monthly", MONTHLY_PRICE)

    with col3:
        st.subheader("💎 Lifetime")
        st.markdown(f"# ₹{LIFETIME_PRICE}")
        if st.button("Choose Lifetime Plan", use_container_width=True, type="primary"):
            create_payment_record("lifetime", LIFETIME_PRICE)



# ============================================================
# PAYMENT RECORD
# ============================================================

def create_payment_record(plan, amount):

    """
    Temporary payment workflow.

    IMPORTANT:
    This creates a pending payment record only.

    For real automatic payments we will connect
    Razorpay next.
    """

    try:

        response = (
            supabase
            .table("payments")
            .insert({
                "user_id": st.session_state.user.id,
                "company_id": get_company_id(),
                "plan": plan,
                "amount": amount,
                "status": "pending"
            })
            .execute()
        )

        st.info(
            """
            Payment record created.

            The next step is connecting Razorpay so the
            customer can actually pay online and the payment
            can automatically activate their subscription.
            """
        )

    except Exception as e:

        st.error(
            f"Payment initialization failed: {e}"
        )


# ============================================================
# DASHBOARD
# ============================================================

def show_dashboard():

    st.title("📦 Meesho Label Organizer Dashboard")

    profile = st.session_state.profile

    company_name = (
        profile.get("company_name", "Your Company")
        if profile
        else "Your Company"
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

    with col1:
        st.metric(
            "Master Products",
            len(inventory)
        )

    with col2:

        total_inventory = sum(
            int(item.get("inventory_quantity", 0) or 0)
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

        st.error("⚠️ LOW STOCK ALERTS")

        for product in low_stock:

            name = (
                product.get("product_name")
                or product.get("master_product_name")
                or "Unnamed Product"
            )

            quantity = product.get(
                "quantity",
                0
            )

            minimum = product.get(
                "minimum_stock",
                0
            )

            st.warning(
                f"📦 **{name}** — "
                f"Current Stock: {quantity} | "
                f"Minimum Limit: {minimum}"
            )

    else:

        st.success(
            "✅ All products are currently above their minimum stock limits."
        )


# ============================================================
# MASTER PRODUCTS
# ============================================================

def show_master_products():

    st.title("🏷️ Master Products")

    company_id = get_company_id()

    with st.expander(
        "➕ Add New Master Product",
        expanded=False
    ):

        product_name = st.text_input(
            "Master Product Name"
        )

        initial_quantity = st.number_input(
            "Initial Inventory Quantity",
            min_value=0,
            step=1
        )

        minimum_stock = st.number_input(
            "Minimum Stock Alert Limit",
            min_value=0,
            step=1
        )

        if st.button(
            "Add Master Product",
            type="primary"
        ):

            if not product_name.strip():

                st.warning(
                    "Please enter a product name."
                )

            else:

                try:

                    supabase.table(
                        "master_products"
                    ).insert({
                        "company_id": company_id,
                        "user_id": get_current_user_id(),
                        "product_name": product_name.strip(),
                        "inventory_quantity": int(initial_quantity),
                        "minimum_stock": int(minimum_stock)
                    }).execute()

                    st.success(
                        "Master product added successfully!"
                    )

                    st.rerun()

                except Exception as e:

                    st.error(
                        f"Could not add product: {e}"
                    )

    inventory = get_inventory()

    if inventory:

        display_data = []

        for item in inventory:

            display_data.append({
                "ID": item.get("id"),
                "Master Product":
                    item.get("product_name")
                    or item.get("master_product_name"),
                "Quantity":
                    item.get("inventory_quantity", 0),
                "Minimum Stock":
                    item.get("minimum_stock", 0)
            })

        st.dataframe(
            pd.DataFrame(display_data),
            use_container_width=True,
            hide_index=True
        )

        # --------------------------------------------------------
        # DELETE MASTER PRODUCT
        # --------------------------------------------------------
        st.divider()
        st.subheader("🗑️ Delete Master Product")

        delete_options = {
            (
                f"{item.get('product_name') or item.get('master_product_name') or 'Unnamed Product'} "
                f"(ID: {str(item.get('id', ''))[:8]}...)"
            ): item
            for item in inventory
        }

        selected_delete_label = st.selectbox(
            "Select the Master Product you want to delete",
            [""] + list(delete_options.keys()),
            key="delete_master_product_select",
        )

        if selected_delete_label:
            selected_product = delete_options[selected_delete_label]
            selected_product_id = selected_product.get("id")
            selected_product_name = (
                selected_product.get("product_name")
                or selected_product.get("master_product_name")
                or ""
            )

            st.warning(
                f"You are about to permanently delete: **{selected_product_name}**"
            )

            confirm_delete = st.checkbox(
                f"I confirm that I want to delete {selected_product_name}",
                key="confirm_master_product_delete",
            )

            if st.button(
                "🗑️ Delete Selected Master Product",
                disabled=not confirm_delete,
                key="delete_master_product_button",
            ):
                try:
                    # Remove SKU mappings for this product first.
                    try:
                        (
                            supabase
                            .table("sku_mappings")
                            .delete()
                            .eq("company_id", company_id)
                            .eq("master_product_name", selected_product_name)
                            .execute()
                        )
                    except Exception:
                        pass

                    # Delete the Master Product.
                    (
                        supabase
                        .table("master_products")
                        .delete()
                        .eq("id", selected_product_id)
                        .eq("company_id", company_id)
                        .execute()
                    )

                    st.success(
                        f"Master Product '{selected_product_name}' was deleted successfully."
                    )
                    st.rerun()

                except Exception as e:
                    st.error(f"Could not delete Master Product: {e}")

    else:

        st.info(
            "No Master Products have been created yet."
        )


# ============================================================
# SKU MAPPINGS
# ============================================================

def show_sku_mappings():

    st.title("🔗 SKU → Master Product Mapping")

    inventory = get_inventory()

    if not inventory:

        st.warning(
            "Please create at least one Master Product first."
        )

        return

    product_names = []

    for product in inventory:

        name = (
            product.get("product_name")
            or product.get("master_product_name")
        )

        if name:
            product_names.append(name)

    with st.expander(
        "➕ Add SKU Mapping",
        expanded=True
    ):

        selected_product = st.selectbox(
            "Select Master Product",
            product_names
        )

        sku = st.text_input(
            "SKU"
        )

        if st.button(
            "Save SKU Mapping",
            type="primary"
        ):

            if not sku.strip():

                st.warning(
                    "Please enter an SKU."
                )

            else:

                try:

                    supabase.table(
                        "sku_mappings"
                    ).insert({
                        "company_id": get_company_id(),
                        "user_id": get_current_user_id(),
                        "master_product_name":
                            selected_product,
                        "sku": sku.strip()
                    }).execute()

                    st.success(
                        f"SKU '{sku}' mapped to '{selected_product}'."
                    )

                    st.rerun()

                except Exception as e:

                    st.error(
                        f"Could not save mapping: {e}"
                    )

    mappings = get_user_mappings()

    if mappings:

        dataframe = pd.DataFrame(mappings)

        columns_to_show = [
            column
            for column in [
                "master_product_name",
                "sku"
            ]
            if column in dataframe.columns
        ]

        st.dataframe(
            dataframe[columns_to_show],
            use_container_width=True,
            hide_index=True
        )

    else:

        st.info(
            "No SKU mappings have been created yet."
        )


# ============================================================
# PDF ORGANIZER
# ============================================================

def show_pdf_organizer():
    st.title("📄 PDF Label Organizer")
    st.caption(
        "Every page is read individually. SKU, Size, Qty and Color are extracted, "
        "then labels are automatically grouped under Master Products."
    )

    subscription = get_subscription_status()
    if not subscription["access"]:
        st.warning("You need an active plan or demo to use this feature.")
        if st.button("View Plans"):
            st.session_state.current_page = "Subscription"
            st.rerun()
        return

    used_pdfs = count_demo_pdfs()
    if subscription["plan"] == "Demo":
        remaining = max(0, DEMO_PDF_LIMIT - used_pdfs)
        st.info(f"Demo usage: {used_pdfs}/{DEMO_PDF_LIMIT} PDFs ({remaining} remaining)")
        if remaining <= 0:
            st.error("Your demo PDF limit has been reached.")
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
            and used_pdfs + len(uploaded_files) > DEMO_PDF_LIMIT
        ):
            st.error("This upload exceeds your remaining demo PDF limit.")
            return

        with st.spinner("Reading every PDF page and organizing labels..."):
            try:
                results = reorganize_pdfs(uploaded_files)
                st.session_state.batch_results = results

                try:
                    supabase.table("pdf_batches").insert({
                        "user_id": st.session_state.user.id,
                        "company_id": get_company_id(),
                        "pdf_count": len(uploaded_files)
                    }).execute()
                except Exception:
                    pass

                st.success("Labels extracted and organized successfully!")
            except Exception as e:
                st.error(f"PDF processing failed: {e}")
                return

    results = st.session_state.batch_results
    if not results:
        return

    categorized = results["categorized"]
    uncategorized = results["uncategorized"]

    st.divider()
    st.subheader("🔎 Extracted Product Details")
    extracted_df = pd.DataFrame(results.get("extracted_rows", []))

    if not extracted_df.empty:
        st.dataframe(extracted_df, use_container_width=True, hide_index=True)
        st.download_button(
            "⬇️ Download Extracted Data CSV",
            data=extracted_df.to_csv(index=False).encode("utf-8"),
            file_name="meesho_extracted_product_details.csv",
            mime="text/csv",
        )

    auto_mappings = results.get("auto_mappings", [])
    if auto_mappings:
        st.success(
            f"🤖 {len(auto_mappings)} new high-confidence SKU mapping(s) were automatically saved."
        )
        st.dataframe(pd.DataFrame(auto_mappings), use_container_width=True, hide_index=True)

    st.subheader("📊 Organization Results")
    total_categorized = sum(len(pages) for pages in categorized.values())
    col1, col2, col3 = st.columns(3)
    col1.metric("Master Product Categories", len(categorized))
    col2.metric("Organized Labels", total_categorized)
    col3.metric("Uncategorized Labels", len(uncategorized))

    pickup_dataframe = create_pickup_list(categorized)
    st.subheader("📋 Pick-Up List")
    st.dataframe(pickup_dataframe, use_container_width=True, hide_index=True)
    st.download_button(
        "⬇️ Download Pick-Up List CSV",
        data=pickup_dataframe.to_csv(index=False).encode("utf-8"),
        file_name="pickup_list.csv",
        mime="text/csv",
    )

    if not pickup_dataframe.empty:
        st.subheader("📦 Inventory Action")
        st.warning("This subtracts the extracted Total Quantity from your inventory.")
        if st.button("➖ Deduct Pick-Up List From Inventory", type="primary"):
            success_count, failed = deduct_inventory_from_pickup(pickup_dataframe)
            if success_count:
                st.success(f"Inventory updated for {success_count} product(s).")
            for error in failed:
                st.error(error)

    st.subheader("📄 Download Reorganized Labels")
    if categorized or uncategorized:
        output_pdf = create_reorganized_pdf(categorized, uncategorized)
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
            f"{len(uncategorized)} page(s) were left uncategorized because no exact "
            "or high-confidence Master Product match was found."
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

        name = (
            product.get("product_name")
            or product.get("master_product_name")
            or "Unnamed Product"
        )

        product_id = product["id"]

        current_quantity = int(
            product.get("inventory_quantity", 0) or 0
        )

        minimum_stock = int(
            product.get("minimum_stock", 0) or 0
        )

        with st.expander(
            f"📦 {name}",
            expanded=False
        ):

            col1, col2 = st.columns(2)

            with col1:

                new_quantity = st.number_input(
                    "Current Quantity",
                    min_value=0,
                    value=current_quantity,
                    step=1,
                    key=f"quantity_{product_id}"
                )

            with col2:

                new_minimum = st.number_input(
                    "Minimum Stock Limit",
                    min_value=0,
                    value=minimum_stock,
                    step=1,
                    key=f"minimum_{product_id}"
                )

            if st.button(
                "Save Changes",
                key=f"save_{product_id}"
            ):

                try:

                    supabase.table(
                        "master_products"
                    ).update({
                        "inventory_quantity": int(new_quantity),
                        "minimum_stock": int(new_minimum)
                    }).eq(
                        "id",
                        product_id
                    ).execute()

                    st.success(
                        "Inventory updated successfully!"
                    )

                    st.rerun()

                except Exception as e:

                    st.error(
                        f"Could not update inventory: {e}"
                    )


# ============================================================
# MAIN APP
# ============================================================

def show_main_app():

    profile = st.session_state.profile

    company_name = (
        profile.get("company_name", "Meesho Label Organizer")
        if profile
        else "Meesho Label Organizer"
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
            "Subscription"
        ]

        for page in pages:

            if st.button(
                page,
                use_container_width=True
            ):

                st.session_state.current_page = page
                st.rerun()

        st.divider()

        subscription = get_subscription_status()

        st.caption(
            f"Current Plan: {subscription['plan']}"
        )

        if is_admin():
            st.caption("🛡️ Full administrator application access")

        if st.button(
            "🚪 Logout",
            use_container_width=True
        ):

            logout()

    page = st.session_state.current_page

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

    show_auth_page()

else:

    if st.session_state.profile is None:
        refresh_profile()

    if st.session_state.profile is None and not is_admin():

        st.error(
            "Your user account exists, but no profile was found. "
            "Please fix the Supabase profile/RLS setup."
        )

        if st.button("Logout"):
            logout()

    else:

        show_main_app()
