"""
Module quản lý tài khoản đăng nhập & vị trí nhân sự, lưu trữ trên Google Sheets.

Cấu trúc 2 sheet (tab) trong cùng 1 Google Spreadsheet:
- "Users":     username | full_name | password_hash | salt | role | regions | active | created_at | updated_at
- "Locations": username | lat | lng | accuracy | updated_at

"""

import hashlib
import hmac
import secrets as pysecrets
from datetime import datetime

import pandas as pd
import streamlit as st
from streamlit_gsheets import GSheetsConnection

USERS_SHEET = "Users"
LOCATIONS_SHEET = "Locations"

USERS_COLUMNS = [
    "username", "full_name", "password_hash", "salt",
    "role", "regions", "active", "created_at", "updated_at",
]
LOCATIONS_COLUMNS = ["username", "lat", "lng", "accuracy", "updated_at"]

# Cấp bậc: số càng lớn càng cao. Một tài khoản xem được vị trí của TẤT CẢ
# các tài khoản có cấp bậc THẤP HƠN mình (không phân biệt khu vực), cộng với
# vị trí của chính mình. Không xem được người ngang cấp hoặc cao hơn.
ROLE_LEVELS = {
    "ky_thuat": 1,
    "dieu_phoi_khu_vuc": 2,
    "dieu_hanh": 3,
    "giam_doc": 4,
    "admin": 5,
}

ROLE_LABELS = {
    "ky_thuat": "Kỹ thuật",
    "dieu_phoi_khu_vuc": "Điều phối khu vực",
    "dieu_hanh": "Điều hành",
    "giam_doc": "Giám đốc",
    "admin": "Admin",
}


# ==========================================
# Kết nối Google Sheets
# ==========================================
def _get_conn():
    return st.connection("gsheets", type=GSheetsConnection)


def _hash_password(password, salt=None):
    if salt is None:
        salt = pysecrets.token_hex(16)
    pwd_hash = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), 100_000
    )
    return pwd_hash.hex(), salt


# ==========================================
# Đọc / Ghi dữ liệu (luôn đọc bản mới nhất, ttl=0 - không cache)
# ==========================================
def _read_users_df():
    conn = _get_conn()
    try:
        df = conn.read(worksheet=USERS_SHEET, ttl=0)
    except Exception:
        return pd.DataFrame(columns=USERS_COLUMNS)

    if df is None or df.empty:
        return pd.DataFrame(columns=USERS_COLUMNS)

    df = df.dropna(how="all")
    if "username" not in df.columns:
        return pd.DataFrame(columns=USERS_COLUMNS)
    df = df[df["username"].notna() & (df["username"].astype(str).str.strip() != "")]

    for col in USERS_COLUMNS:
        if col not in df.columns:
            df[col] = True if col == "active" else ""

    df["username"] = df["username"].astype(str).str.strip()
    df["active"] = (
        df["active"].astype(str).str.strip().str.upper().isin(["TRUE", "1", "YES", "ACTIVE"])
    )
    return df[USERS_COLUMNS].reset_index(drop=True)


def _write_users_df(df):
    conn = _get_conn()
    df = df[USERS_COLUMNS].copy()
    conn.update(
        worksheet=USERS_SHEET,
        data=df,
    )


def _read_locations_df():
    conn = _get_conn()
    try:
        df = conn.read(worksheet=LOCATIONS_SHEET, ttl=0)
    except Exception:
        return pd.DataFrame(columns=LOCATIONS_COLUMNS)

    if df is None or df.empty:
        return pd.DataFrame(columns=LOCATIONS_COLUMNS)

    df = df.dropna(how="all")
    if "username" not in df.columns:
        return pd.DataFrame(columns=LOCATIONS_COLUMNS)
    df = df[df["username"].notna() & (df["username"].astype(str).str.strip() != "")]

    for col in LOCATIONS_COLUMNS:
        if col not in df.columns:
            df[col] = None

    df["username"] = df["username"].astype(str).str.strip()
    df["lat"] = pd.to_numeric(df["lat"], errors="coerce")
    df["lng"] = pd.to_numeric(df["lng"], errors="coerce")
    df["accuracy"] = pd.to_numeric(df["accuracy"], errors="coerce")
    return df[LOCATIONS_COLUMNS].reset_index(drop=True)


def _write_locations_df(df):
    conn = _get_conn()
    df = df[LOCATIONS_COLUMNS].copy()
    conn.update(
        worksheet=LOCATIONS_SHEET,
        data=df,
    )

# ==========================================
# Khởi tạo lần đầu (bootstrap tài khoản Admin)
# ==========================================
def init_db():
    """Gọi 1 lần mỗi khi app khởi động: tạo sheet Users/Locations và tài khoản
    Admin đầu tiên (lấy từ st.secrets) nếu hệ thống chưa có tài khoản nào."""
    users_df = _read_users_df()

    if users_df.empty:
        admin_user = st.secrets.get("ADMIN_USERNAME", "admin")
        admin_pass = st.secrets.get("ADMIN_PASSWORD", "admin123")
        pwd_hash, salt = _hash_password(admin_pass)
        now = datetime.now().isoformat(timespec="seconds")
        bootstrap_row = pd.DataFrame([{
            "username": admin_user,
            "full_name": "Quản trị viên",
            "password_hash": pwd_hash,
            "salt": salt,
            "role": "admin",
            "regions": "ALL",
            "active": True,
            "created_at": now,
            "updated_at": now,
        }])
        _write_users_df(bootstrap_row)

    loc_df = _read_locations_df()
    if loc_df.empty:
        _write_locations_df(pd.DataFrame(columns=LOCATIONS_COLUMNS))


# ==========================================
# Xác thực đăng nhập
# ==========================================
def verify_login(username, password):
    username = (username or "").strip()
    if not username or not password:
        return None

    df = _read_users_df()
    if df.empty:
        return None
    match = df[df["username"].str.lower() == username.lower()]
    if match.empty:
        return None

    row = match.iloc[0]
    if not bool(row["active"]):
        return None

    pwd_hash, _ = _hash_password(password, row["salt"])
    if hmac.compare_digest(pwd_hash, row["password_hash"]):
        regions = [r.strip() for r in str(row["regions"]).split(",") if r.strip()]
        return {
            "username": row["username"],
            "full_name": row["full_name"],
            "role": row["role"],
            "regions": regions,
        }
    return None


# ==========================================
# Quản lý tài khoản (Admin dùng)
# ==========================================
def list_users():
    df = _read_users_df()
    return df.drop(columns=["password_hash", "salt"]).to_dict("records")


def create_or_update_user(username, full_name, role, regions, password=None, active=True):
    username = (username or "").strip()
    if not username:
        raise ValueError("Tên đăng nhập không được để trống")
    if not full_name or not full_name.strip():
        raise ValueError("Họ và tên không được để trống")
    if role not in ROLE_LEVELS:
        raise ValueError("Vai trò không hợp lệ")

    df = _read_users_df()
    now = datetime.now().isoformat(timespec="seconds")
    regions_str = ",".join(regions) if regions else ""

    if df.empty:
        mask = pd.Series([], dtype=bool)
    else:
        mask = df["username"].str.lower() == username.lower()

    if mask.any():
        idx = df[mask].index[0]
        df.loc[idx, "full_name"] = full_name.strip()
        df.loc[idx, "role"] = role
        df.loc[idx, "regions"] = regions_str
        df.loc[idx, "active"] = active
        df.loc[idx, "updated_at"] = now
        if password:
            pwd_hash, salt = _hash_password(password)
            df.loc[idx, "password_hash"] = pwd_hash
            df.loc[idx, "salt"] = salt
        action = "updated"
    else:
        if not password:
            raise ValueError("Cần nhập mật khẩu khi tạo tài khoản mới")
        pwd_hash, salt = _hash_password(password)
        new_row = pd.DataFrame([{
            "username": username, "full_name": full_name.strip(),
            "password_hash": pwd_hash, "salt": salt,
            "role": role, "regions": regions_str, "active": active,
            "created_at": now, "updated_at": now,
        }])
        df = pd.concat([df, new_row], ignore_index=True)
        action = "created"

    _write_users_df(df)
    return action


def delete_user(username):
    username = (username or "").strip()
    df = _read_users_df()
    if not df.empty:
        df = df[df["username"].str.lower() != username.lower()]
        _write_users_df(df)

    loc_df = _read_locations_df()
    if not loc_df.empty:
        loc_df = loc_df[loc_df["username"].str.lower() != username.lower()]
        _write_locations_df(loc_df)


# ==========================================
# Vị trí nhân sự
# ==========================================
def update_location(username, lat, lng, accuracy=None):
    username = (username or "").strip()
    if not username:
        return
    now = datetime.now().isoformat(timespec="seconds")
    df = _read_locations_df()

    if df.empty:
        mask = pd.Series([], dtype=bool)
    else:
        mask = df["username"].str.lower() == username.lower()

    if mask.any():
        idx = df[mask].index[0]
        df.loc[idx, "lat"] = lat
        df.loc[idx, "lng"] = lng
        df.loc[idx, "accuracy"] = accuracy
        df.loc[idx, "updated_at"] = now
    else:
        new_row = pd.DataFrame([{
            "username": username, "lat": lat, "lng": lng,
            "accuracy": accuracy, "updated_at": now,
        }])
        df = pd.concat([df, new_row], ignore_index=True)

    _write_locations_df(df)


def get_visible_locations(viewer):
    """Trả về vị trí của những tài khoản mà `viewer` được phép xem.
    Quy tắc: mỗi cấp bậc xem được TẤT CẢ các cấp thấp hơn mình (không phân
    biệt khu vực), cộng với vị trí của chính mình. Không xem được người
    ngang cấp hoặc cấp cao hơn."""
    viewer_level = ROLE_LEVELS.get(viewer["role"], 0)

    users_df = _read_users_df()
    loc_df = _read_locations_df().rename(columns={"updated_at": "loc_updated_at"})

    if users_df.empty:
        return []

    merged = users_df.merge(loc_df, on="username", how="left")

    result = []
    for _, r in merged.iterrows():
        is_self = r["username"].lower() == viewer["username"].lower()
        if not bool(r["active"]) and not is_self:
            continue
        r_level = ROLE_LEVELS.get(r["role"], 0)
        if not is_self and r_level >= viewer_level:
            continue
        result.append({
            "username": r["username"],
            "full_name": r["full_name"],
            "role": r["role"],
            "regions": r["regions"],
            "lat": r["lat"] if pd.notna(r["lat"]) else None,
            "lng": r["lng"] if pd.notna(r["lng"]) else None,
            "updated_at": r["loc_updated_at"] if pd.notna(r["loc_updated_at"]) else None,
        })
    return result
