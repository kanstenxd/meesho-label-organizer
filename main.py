import streamlit as st
import pandas as pd
from supabase import create_client, Client
from datetime import datetime
from io import BytesIO
import re
import uuid

# =========================================================
# PAGE CONFIGURATION
# =========================================================

st.set_page_config(
    page_title="Meesho Label Organizer",
    page_icon="📦",
    layout="wide"
)


# =========================================================
# SUPABASE CONFIGURATION
# =========================================================

SUPABASE_URL = st.secrets.get("SUPABASE_URL", "")
SUPABASE_KEY = st.secrets.get("SUPABASE_KEY", "")


@st.cache_resource
def get_supabase():
    """Create and return the Supabase client."""

    if not SUPABASE_URL or not SUPABASE_KEY:
        return None

    return create_client(SUPABASE_URL, SUPABASE_KEY)


supabase = get_supabase()


# =========================================================
# SESSION STATE
# =========================================================

if "user" not in st.session_state:
    st.session_state.user = None

if "profile" not in st.session_state:
    st.session_state.profile = None

if "page" not in st.session_state:
    st.session_state.page = "Dashboard"


# =========================================================
# HELPER FUNCTIONS
# =========================================================

def show_error(message):
    st.error(message)


def show_success(message):
    st.success(message)


def get_current_user_id():
    """Return the logged-in user's ID."""

    if st.session_state.user:
        try:
            return st.session_state.user.id
        except Exception:
            pass

    return None


def normalize_text(text):
    """Normalize text for better product matching."""

    if not text:
        return ""

    text = str(text).lower().strip()

    # Remove special characters
    text = re.sub(r"[^a-z0-9\s]", " ", text)

    # Remove extra spaces
    text = re.sub(r"\s+", " ", text)

    return text.strip()


def find_matching_product(product_name, master_products):
    """
    Find a master product that closely matches
    the provided product name.
    """

    normalized_name = normalize_text(product_name)

    if not normalized_name:
        return None

    for product in master_products:

        master_name = normalize_text(
            product.get("product_name", "")
        )

        # Exact match
        if normalized_name == master_name:
            return product

        # One name contains the other
        if (
            normalized_name in master_name
            or master_name in normalized_name
        ):
            return product

    return None


# =========================================================
# AUTHENTICATION FUNCTIONS
# =========================================================

def login_user(email, password):
    """Log a user into the application."""

    try:

        result = supabase.auth.sign_in_with_password({
            "email": email,
            "password": password
        })

        st.session_state.user = result.user

        load_user_profile()

        return True, "Login successful."

    except Exception as e:
        return False, str(e)


def signup_user(email, password):
    """Create a new account."""

    try:

        result = supabase.auth.sign_up({
            "email": email,
            "password": password
        })

        return True, (
            "Account created successfully. "
            "Please verify your email if verification is enabled."
        )

    except Exception as e:
        return False, str(e)


def logout_user():

    try:
        supabase.auth.sign_out()
    except Exception:
        pass

    st.session_state.user = None
    st.session_state.profile = None

    st.rerun()


# =========================================================
# PROFILE FUNCTIONS
# =========================================================

def load_user_profile():
    """
    Load the user's profile.

    This safely handles missing permissions or
    missing profile records.
    """

    user_id = get_current_user_id()

    if not user_id:
        return None

    try:

        response = (
            supabase
            .table("profiles")
            .select("*")
            .eq("id", user_id)
            .execute()
        )

        if response.data and len(response.data) > 0:

            st.session_state.profile = response.data[0]

            return response.data[0]

        return None

    except Exception as e:

        # Don't crash the entire application
        st.session_state.profile = None

        return None


def get_company_name():

    profile = st.session_state.profile

    if not profile:
        return "My Company"

    possible_columns = [
        "company_name",
        "business_name",
        "name"
    ]

    for column in possible_columns:

        if profile.get(column):
            return profile.get(column)

    return "My Company"


# =========================================================
# MASTER PRODUCT FUNCTIONS
# =========================================================

def get_master_products():

    user_id = get_current_user_id()

    if not user_id:
        return []

    try:

        response = (
            supabase
            .table("master_products")
            .select("*")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .execute()
        )

        return response.data or []

    except Exception as e:

        st.error(f"Could not load master products: {e}")

        return []


def add_master_product(
    product_name,
    inventory_quantity,
    minimum_stock
):

    user_id = get_current_user_id()

    if not user_id:
        return False, "User is not logged in."

    try:

        data = {
            "user_id": user_id,
            "product_name": product_name,
            "inventory_quantity": int(inventory_quantity)
        }

        # Try to include minimum_stock if your table has it
        try:
            data["minimum_stock"] = int(minimum_stock)

            response = (
                supabase
                .table("master_products")
                .insert(data)
                .execute()
            )

        except Exception:

            # Remove unsupported column and try again
            data.pop("minimum_stock", None)

            response = (
                supabase
                .table("master_products")
                .insert(data)
                .execute()
            )

        return True, "Master product added successfully."

    except Exception as e:

        return False, str(e)


def delete_master_product(product_id):

    user_id = get_current_user_id()

    if not user_id:
        return False, "User is not logged in."

    try:

        (
            supabase
            .table("master_products")
            .delete()
            .eq("id", product_id)
            .eq("user_id", user_id)
            .execute()
        )

        return True, "Product deleted successfully."

    except Exception as e:

        return False, str(e)


def update_product_inventory(product_id, quantity):

    user_id = get_current_user_id()

    if not user_id:
        return False, "User is not logged in."

    try:

        (
            supabase
            .table("master_products")
            .update({
                "inventory_quantity": int(quantity)
            })
            .eq("id", product_id)
            .eq("user_id", user_id)
            .execute()
        )

        return True, "Inventory updated successfully."

    except Exception as e:

        return False, str(e)


# =========================================================
# SKU MAPPING FUNCTIONS
# =========================================================

def get_sku_mappings():

    user_id = get_current_user_id()

    if not user_id:
        return []

    try:

        response = (
            supabase
            .table("sku_mappings")
            .select("*")
            .eq("user_id", user_id)
            .execute()
        )

        return response.data or []

    except Exception as e:

        return []


def add_sku_mapping(
    master_product_id,
    sku,
    account_name=""
):

    user_id = get_current_user_id()

    if not user_id:
        return False, "User is not logged in."

    try:

        data = {
            "user_id": user_id,
            "master_product_id": master_product_id,
            "sku": sku
        }

        # Account name is optional
        if account_name:
            data["account_name"] = account_name

        supabase.table(
            "sku_mappings"
        ).insert(data).execute()

        return True, "SKU mapping added successfully."

    except Exception as e:

        return False, str(e)


def delete_sku_mapping(mapping_id):

    user_id = get_current_user_id()

    try:

        (
            supabase
            .table("sku_mappings")
            .delete()
            .eq("id", mapping_id)
            .eq("user_id", user_id)
            .execute()
        )

        return True, "SKU mapping deleted."

    except Exception as e:

        return False, str(e)


# =========================================================
# INVENTORY FUNCTIONS
# =========================================================

def update_inventory_after_order(
    product_id,
    quantity_sold
):

    user_id = get_current_user_id()

    if not user_id:
        return False, "User not logged in."

    try:

        product_response = (
            supabase
            .table("master_products")
            .select("*")
            .eq("id", product_id)
            .eq("user_id", user_id)
            .execute()
        )

        if not product_response.data:
            return False, "Product not found."

        product = product_response.data[0]

        current_quantity = int(
            product.get("inventory_quantity", 0)
        )

        new_quantity = max(
            current_quantity - int(quantity_sold),
            0
        )

        (
            supabase
            .table("master_products")
            .update({
                "inventory_quantity": new_quantity
            })
            .eq("id", product_id)
            .eq("user_id", user_id)
            .execute()
        )

        # Try saving inventory history
        try:

            history_data = {
                "user_id": user_id,
                "master_product_id": product_id,
                "quantity": int(quantity_sold),
                "type": "OUT"
            }

            supabase.table(
                "inventory_history"
            ).insert(history_data).execute()

        except Exception:
            pass

        return True, "Inventory updated."

    except Exception as e:

        return False, str(e)


# =========================================================
# LOGIN PAGE
# =========================================================

def show_login_page():

    st.title("📦 Meesho Label Organizer")

    st.markdown(
        "### Organize your Meesho labels, SKUs and inventory."
    )

    tab1, tab2 = st.tabs([
        "Login",
        "Create Account"
    ])

    with tab1:

        st.subheader("Login")

        email = st.text_input(
            "Email",
            key="login_email"
        )

        password = st.text_input(
            "Password",
            type="password",
            key="login_password"
        )

        if st.button(
            "Login",
            use_container_width=True
        ):

            if not email or not password:

                st.warning(
                    "Please enter your email and password."
                )

            else:

                success, message = login_user(
                    email,
                    password
                )

                if success:

                    st.success(message)

                    st.rerun()

                else:

                    st.error(
                        f"Login failed: {message}"
                    )

    with tab2:

        st.subheader("Create Account")

        email = st.text_input(
            "Email",
            key="signup_email"
        )

        password = st.text_input(
            "Password",
            type="password",
            key="signup_password"
        )

        confirm_password = st.text_input(
            "Confirm Password",
            type="password"
        )

        if st.button(
            "Create Account",
            use_container_width=True
        ):

            if password != confirm_password:

                st.error(
                    "Passwords do not match."
                )

            elif len(password) < 6:

                st.error(
                    "Password must contain at least 6 characters."
                )

            else:

                success, message = signup_user(
                    email,
                    password
                )

                if success:
                    st.success(message)

                else:
                    st.error(message)


# =========================================================
# DASHBOARD
# =========================================================

def show_dashboard():

    st.title("📦 Meesho Label Organizer Dashboard")

    company_name = get_company_name()

    st.markdown(
        f"## Welcome, {company_name} 👋"
    )

    products = get_master_products()

    total_products = len(products)

    total_inventory = sum(
        int(
            product.get(
                "inventory_quantity",
                0
            ) or 0
        )
        for product in products
    )

    low_stock_products = []

    for product in products:

        quantity = int(
            product.get(
                "inventory_quantity",
                0
            ) or 0
        )

        minimum_stock = int(
            product.get(
                "minimum_stock",
                0
            ) or 0
        )

        if minimum_stock > 0:

            if quantity <= minimum_stock:

                low_stock_products.append(
                    product
                )

    col1, col2, col3 = st.columns(3)

    col1.metric(
        "Master Products",
        total_products
    )

    col2.metric(
        "Total Inventory",
        total_inventory
    )

    col3.metric(
        "Low Stock Products",
        len(low_stock_products)
    )

    st.divider()

    if low_stock_products:

        st.warning(
            "⚠️ Some products have reached "
            "their minimum stock limit."
        )

        for product in low_stock_products:

            st.write(
                f"**{product.get('product_name')}** "
                f"- Stock: "
                f"{product.get('inventory_quantity', 0)}"
            )

    else:

        st.success(
            "✅ All products are currently "
            "above their minimum stock limits."
        )


# =========================================================
# MASTER PRODUCTS PAGE
# =========================================================

def show_master_products():

    st.title("📦 Master Products")

    with st.expander(
        "➕ Add New Master Product",
        expanded=True
    ):

        product_name = st.text_input(
            "Product Name",
            placeholder="Example: Jali Combo"
        )

        col1, col2 = st.columns(2)

        with col1:

            inventory_quantity = st.number_input(
                "Initial Inventory Quantity",
                min_value=0,
                value=0,
                step=1
            )

        with col2:

            minimum_stock = st.number_input(
                "Minimum Stock Alert Limit",
                min_value=0,
                value=0,
                step=1
            )

        if st.button(
            "Add Master Product",
            type="primary"
        ):

            if not product_name.strip():

                st.error(
                    "Please enter a product name."
                )

            else:

                success, message = add_master_product(
                    product_name.strip(),
                    inventory_quantity,
                    minimum_stock
                )

                if success:

                    st.success(message)

                    st.rerun()

                else:

                    st.error(
                        f"Could not add product: {message}"
                    )

    st.divider()

    products = get_master_products()

    if not products:

        st.info(
            "No Master Products have been created yet."
        )

        return

    st.subheader("Your Master Products")

    for product in products:

        product_id = product.get("id")

        with st.container(border=True):

            col1, col2, col3 = st.columns(
                [4, 2, 1]
            )

            with col1:

                st.subheader(
                    product.get(
                        "product_name",
                        "Unnamed Product"
                    )
                )

                st.caption(
                    f"Product ID: {product_id}"
                )

            with col2:

                st.metric(
                    "Inventory",
                    product.get(
                        "inventory_quantity",
                        0
                    )
                )

            with col3:

                if st.button(
                    "🗑️",
                    key=f"delete_product_{product_id}"
                ):

                    success, message = delete_master_product(
                        product_id
                    )

                    if success:

                        st.success(message)

                        st.rerun()

                    else:

                        st.error(message)


# =========================================================
# SKU MAPPINGS PAGE
# =========================================================

def show_sku_mappings():

    st.title("🔗 SKU Mappings")

    products = get_master_products()

    if not products:

        st.warning(
            "Create a Master Product before adding SKU mappings."
        )

        return

    product_options = {
        product["product_name"]: product["id"]
        for product in products
    }

    with st.expander(
        "➕ Add SKU Mapping",
        expanded=True
    ):

        selected_product = st.selectbox(
            "Select Master Product",
            list(product_options.keys())
        )

        sku = st.text_input(
            "SKU",
            placeholder="Enter the Meesho SKU"
        )

        account_name = st.text_input(
            "Account Name (Optional)",
            placeholder="Example: Account 1"
        )

        if st.button(
            "Add SKU Mapping",
            type="primary"
        ):

            if not sku.strip():

                st.error(
                    "Please enter an SKU."
                )

            else:

                product_id = product_options[
                    selected_product
                ]

                success, message = add_sku_mapping(
                    product_id,
                    sku.strip(),
                    account_name.strip()
                )

                if success:

                    st.success(message)

                    st.rerun()

                else:

                    st.error(
                        f"Could not add SKU: {message}"
                    )

    st.divider()

    mappings = get_sku_mappings()

    if not mappings:

        st.info(
            "No SKU mappings have been created yet."
        )

        return

    product_lookup = {
        product["id"]: product.get(
            "product_name",
            "Unknown Product"
        )
        for product in products
    }

    display_data = []

    for mapping in mappings:

        display_data.append({
            "Master Product": product_lookup.get(
                mapping.get("master_product_id"),
                "Unknown Product"
            ),
            "SKU": mapping.get("sku", ""),
            "Account": mapping.get(
                "account_name",
                ""
            )
        })

    st.dataframe(
        pd.DataFrame(display_data),
        use_container_width=True,
        hide_index=True
    )


# =========================================================
# INVENTORY PAGE
# =========================================================

def show_inventory():

    st.title("📊 Inventory")

    products = get_master_products()

    if not products:

        st.info(
            "No products available."
        )

        return

    rows = []

    for product in products:

        quantity = int(
            product.get(
                "inventory_quantity",
                0
            ) or 0
        )

        minimum_stock = int(
            product.get(
                "minimum_stock",
                0
            ) or 0
        )

        status = "Good"

        if minimum_stock > 0 and quantity <= minimum_stock:
            status = "Low Stock"

        rows.append({
            "Product": product.get(
                "product_name"
            ),
            "Quantity": quantity,
            "Minimum Stock": minimum_stock,
            "Status": status
        })

    st.dataframe(
        pd.DataFrame(rows),
        use_container_width=True,
        hide_index=True
    )

    st.divider()

    st.subheader("Update Inventory")

    product_options = {
        product["product_name"]: product
        for product in products
    }

    selected_name = st.selectbox(
        "Select Product",
        list(product_options.keys())
    )

    selected_product = product_options[
        selected_name
    ]

    new_quantity = st.number_input(
        "New Inventory Quantity",
        min_value=0,
        value=int(
            selected_product.get(
                "inventory_quantity",
                0
            ) or 0
        )
    )

    if st.button(
        "Update Inventory",
        type="primary"
    ):

        success, message = update_product_inventory(
            selected_product["id"],
            new_quantity
        )

        if success:

            st.success(message)

            st.rerun()

        else:

            st.error(message)


# =========================================================
# PDF ORGANIZER PAGE
# =========================================================

def show_pdf_organizer():

    st.title("📄 PDF Label Organizer")

    st.write(
        "Upload Meesho label PDFs here. "
        "This section will be used to organize "
        "labels according to your Master Product and SKU mappings."
    )

    uploaded_file = st.file_uploader(
        "Upload Meesho Label PDF",
        type=["pdf"]
    )

    if uploaded_file:

        st.success(
            f"Uploaded: {uploaded_file.name}"
        )

        st.info(
            "PDF processing can now be connected "
            "to your SKU mapping system."
        )

        # Save PDF batch record if possible
        user_id = get_current_user_id()

        if user_id:

            try:

                data = {
                    "user_id": user_id,
                    "file_name": uploaded_file.name
                }

                supabase.table(
                    "pdf_batches"
                ).insert(data).execute()

            except Exception:
                pass


# =========================================================
# SUBSCRIPTION PAGE
# =========================================================

def show_subscription():

    st.title("💳 Subscription")

    st.info(
        "Your current subscription information "
        "will appear here."
    )

    st.subheader("Current Plan")

    st.write(
        "Plan: Free / Expired"
    )

    st.caption(
        "Subscription functionality can be "
        "connected to your payments table."
    )


# =========================================================
# SIDEBAR
# =========================================================

def show_sidebar():

    with st.sidebar:

        st.title("📦 Meesho Organizer")

        st.caption(
            get_company_name()
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

        selected_page = st.radio(
            "Navigation",
            pages,
            label_visibility="collapsed"
        )

        st.divider()

        st.caption("Current Plan: Free")

        if st.button(
            "🚪 Logout",
            use_container_width=True
        ):

            logout_user()

    return selected_page


# =========================================================
# MAIN APPLICATION
# =========================================================

def main():

    # Check Supabase configuration
    if supabase is None:

        st.title("📦 Meesho Label Organizer")

        st.error(
            "Supabase configuration is missing."
        )

        st.info(
            "Add your SUPABASE_URL and SUPABASE_KEY "
            "to Streamlit Secrets."
        )

        st.code(
            '''
SUPABASE_URL = "your-supabase-url"
SUPABASE_KEY = "your-supabase-anon-key"
            '''
        )

        return

    # Check authentication
    if not st.session_state.user:

        show_login_page()

        return

    # Load profile if necessary
    if not st.session_state.profile:

        load_user_profile()

    # Sidebar navigation
    page = show_sidebar()

    # Route pages
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

        show_subscription()


# =========================================================
# RUN APPLICATION
# =========================================================

if __name__ == "__main__":
    main()
