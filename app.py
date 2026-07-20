import streamlit as st
import folium
from streamlit_folium import st_folium
import json
import pandas as pd
import re
import os
import subprocess
from datetime import datetime, timezone
from folium.plugins import Fullscreen, LocateControl
from folium.plugins import MiniMap
from api_client import CCTSClient
from utils import extract_core_station_code, parse_duration_to_hours
import auth_gsheets as auth
import traceback

# ==========================================
# 📍 Component v2: theo dõi vị trí liên tục (không cần bấm nút)
# ==========================================
# Yêu cầu streamlit >= 1.51 (custom components v2). Dùng navigator.geolocation
# .watchPosition() để tự động xin quyền định vị NGAY khi trang được tải (trình
# duyệt vẫn sẽ hiện popup xin quyền - không thể bỏ qua bước này vì lý do bảo
# mật trình duyệt), sau đó tự động gửi vị trí mới về Python mỗi khi thiết bị
# di chuyển đủ xa hoặc sau một khoảng thời gian nhất định, mô phỏng việc theo
# dõi vị trí liên tục kiểu Google Maps.
_GEO_WATCHER_JS = """
export default function(component) {
    const { data, setStateValue, parentElement } = component;
    if (window.__geoWatchStarted) return;  // Tránh khởi tạo lại nhiều lần khi component re-render
    window.__geoWatchStarted = true;

    const minIntervalMs = (data && data.min_interval_sec ? data.min_interval_sec : 15) * 1000;
    const minDistanceM = (data && data.min_distance_m) ? data.min_distance_m : 20;

    let lastSent = null; // {lat, lng, time}

    function haversine(lat1, lng1, lat2, lng2) {
        const R = 6371000;
        const toRad = (d) => d * Math.PI / 180;
        const dLat = toRad(lat2 - lat1);
        const dLng = toRad(lng2 - lng1);
        const a = Math.sin(dLat / 2) ** 2 + Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLng / 2) ** 2;
        return 2 * R * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
    }

    if (!navigator.geolocation) {
        setStateValue("error", "Trình duyệt không hỗ trợ định vị");
        return;
    }

    navigator.geolocation.watchPosition(
        (pos) => {
            const now = Date.now();
            const { latitude, longitude, accuracy } = pos.coords;
            let shouldSend = !lastSent;
            if (lastSent) {
                const dt = now - lastSent.time;
                const dist = haversine(lastSent.lat, lastSent.lng, latitude, longitude);
                shouldSend = dt >= minIntervalMs || dist >= minDistanceM;
            }
            if (shouldSend) {
                lastSent = { lat: latitude, lng: longitude, time: now };
                setStateValue("latitude", latitude);
                setStateValue("longitude", longitude);
                setStateValue("accuracy", accuracy);
                setStateValue("updated_ts", now);
                setStateValue("error", null);
            }
        },
        (err) => {
            setStateValue("error", err.message || "Không lấy được vị trí (có thể do bị chặn quyền định vị)");
        },
        { enableHighAccuracy: true, maximumAge: 5000, timeout: 20000 }
    );
}
"""

try:
    _geo_watcher_component = st.components.v2.component(
        "geo_watcher",
        html="<div></div>",
        css="div { display:none; }",
        js=_GEO_WATCHER_JS,
    )
except AttributeError:
    # Streamlit < 1.51 chưa có components.v2 -> vô hiệu hoá tính năng theo dõi vị trí tự động
    _geo_watcher_component = None


def geo_watcher(min_interval_sec=15, min_distance_m=20, key="geo_watcher"):
    """Trả về object có .latitude/.longitude/.accuracy/.updated_ts/.error -
    None nếu chưa có dữ liệu hoặc trình duyệt/Streamlit không hỗ trợ."""
    if _geo_watcher_component is None:
        return None
    return _geo_watcher_component(
        data={"min_interval_sec": min_interval_sec, "min_distance_m": min_distance_m},
        default={"latitude": None, "longitude": None, "accuracy": None, "updated_ts": None, "error": None},
        on_latitude_change=lambda: None,
        on_longitude_change=lambda: None,
        on_accuracy_change=lambda: None,
        on_updated_ts_change=lambda: None,
        on_error_change=lambda: None,
        key=key,
    )

st.set_page_config(
    page_title="CCTS Live Map",
    layout="wide",
    initial_sidebar_state="collapsed"
)

def install_playwright():
    if not os.path.exists("/home/appuser/.cache/ms-playwright"):
        print("Đang tải trình duyệt Playwright, vui lòng đợi trong vài giây...")
        subprocess.run(["playwright", "install", "chromium"])
        print("Tải xong!")

# Gọi hàm này trước khi bắt đầu logic chính của app
install_playwright()

# ==========================================
# ⚙️ 1. CẤU HÌNH TRANG & BẢO MẬT
# ==========================================

# Lấy thông tin đăng nhập từ Streamlit Secrets (thiết lập trên Streamlit Cloud)
try:
    CCTS_USER = st.secrets["CCTS_USERNAME"]
    CCTS_PASS = st.secrets["CCTS_PASSWORD"]
except KeyError:
    st.error("⚠️ Chưa cấu hình thông tin đăng nhập trong Streamlit Secrets!")
    st.stop()

# ==========================================
# 🛠️ 2. MODULE XỬ LÝ DỮ LIỆU TĨNH
# ==========================================
@st.cache_data
def load_static_data():
    """Tải và parse file JSON và Excel 1 lần duy nhất để tối ưu bộ nhớ"""
    coords_map = {}
    tech_map = {}
    region_map = {} # Thêm biến lưu khu vực quản lý
    cp_model_map = {} # Biến mới để lưu map model
    # 1. Đọc JSON lấy tọa độ (Ưu tiên 1)
    try:
        with open("station_info.json", 'r', encoding='utf-8') as f:
            station_data = json.load(f)
            for entry in station_data:
                store_id = entry.get("store_id")
                lat = entry.get("lat")
                lng = entry.get("lng")
                if store_id and lat and lng:
                    core_code = extract_core_station_code(store_id)
                    coords_map[core_code] = {'lat': float(lat), 'lng': float(lng)}
    except Exception as e:
        st.warning(f"Lỗi đọc station_info.json: {e}")

    # 2. Đọc Excel listLongLat.xlsx làm dự phòng (Ưu tiên 2)
    try:
        if os.path.exists("listLongLat.xlsx"):
            df_coords = pd.read_excel("listLongLat.xlsx")
            # Chuẩn hóa tên cột để tránh lỗi viết hoa/thường
            col_map = {str(col).strip().lower(): col for col in df_coords.columns}
            st_col = col_map.get("station code")
            lat_col = col_map.get("lat")
            long_col = col_map.get("long")
            
            if st_col and lat_col and long_col:
                df_clean = df_coords.dropna(subset=[st_col, lat_col, long_col])
                for _, row in df_clean.iterrows():
                    core_code = extract_core_station_code(row[st_col])
                    # Chỉ cập nhật nếu trạm chưa có tọa độ từ file JSON
                    if core_code and core_code not in coords_map:
                        coords_map[core_code] = {'lat': float(row[lat_col]), 'lng': float(row[long_col])}
    except Exception as e:
        st.warning(f"Lỗi đọc listLongLat.xlsx: {e}")

    # 3. Xây dựng dictionary Kỹ thuật viên & Region từ list_Stations.json
    try:
        with open("list_Stations.json", 'r', encoding='utf-8') as f:
            list_stations = json.load(f)
            for region, engs in list_stations.items():
                for eng, stations in engs.items():
                    for st in stations:
                        core_code = extract_core_station_code(st)
                        tech_map[core_code] = eng
                        region_map[core_code] = region
    except Exception as e:
        st.warning(f"Lỗi đọc list_Stations.json: {e}")

    try:
        if os.path.exists("ChargePoint_Model.xlsx"):
            df_model = pd.read_excel("ChargePoint_Model.xlsx")
            # Tạo dictionary mapping: Model -> Name
            cp_model_map = dict(zip(df_model["Charge Point Model"], df_model["Name"]))
    except Exception as e:
        st.warning(f"Lỗi đọc ChargePoint_Model.xlsx: {e}")

    # Cập nhật return
    return coords_map, tech_map, region_map, cp_model_map

# ==========================================
# 🔄 3. MODULE FETCH API (CÓ CACHE)
# ==========================================
@st.cache_data(ttl=600) # Làm mới dữ liệu tự động sau mỗi 10 phút
def fetch_live_tickets():
    """
    Khởi tạo CCTSClient, tự động đăng nhập qua Playwright và gọi trực tiếp endpoint JSON 
    để trích xuất các trường thông tin cần thiết phục vụ bản đồ Live Ticket.
    """
    # Sử dụng trực tiếp biến Global đã lấy từ st.secrets ở đầu file
    client = CCTSClient(username=CCTS_USER, password=CCTS_PASS)
    
    # Thực hiện login lượt đầu tiên để lấy Token và Cookie ssoticket ban đầu
    try:
        client.login()
    except Exception as e:
        st.error(f"Khởi động phiên đăng nhập CCTS thất bại: {e}")
        return pd.DataFrame()

    # Endpoint tìm kiếm/truy vấn danh sách ticket trực tiếp
    endpoint = "/ccts/cctsTicket/findCCTSTicket"
    
    # Payload bộ lọc tương tự như trích xuất trực tuyến
    payload = {
        "page": {"pageNum": 1, "pageSize": 2000},  # Điều chỉnh kích thước trang nếu lượng ticket lớn
        "timezoneOffset": 420,
        "createStartTime": "2026-04-30 17:00:00",
        "createStopTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "ticketStatus": ["Open", "Appointment", "Pending for ASP close", "Pending for spare parts"]
    }

    try:
        # Gọi API qua hàm _post nội bộ. Hàm này tự bắt mã 401/403/50001 để tự động 
        # gọi lại Playwright gia hạn token nếu hết hạn giữa chừng.
        res_data = client._post(endpoint, payload)
        
        # Bóc tách danh sách từ cấu trúc JSON phản hồi
        data = res_data.get("data", {})
        tickets = data.get("list", []) if isinstance(data, dict) else data
        if not isinstance(tickets, list):
            tickets = data.get("records", [])

        if not tickets:
            return pd.DataFrame()

        processed_data = []

        # Vòng lặp lấy đúng các thông tin cần thiết từ đối tượng JSON
        for item in tickets:
            processed_data.append({
                "Ticket ID": item.get("cctsTicketId"),
                "Charge Point ID": item.get("chargeBoxId"),
                "Charge Box Model": item.get("chargeBoxModel"),
                "Station Code": item.get("stationCode"),
                "Problem Description": item.get("errorDesc"),
                "Ticket Status": item.get("cctsTicketStatus"),
                "Ticket Duration": item.get("duration")
            })

        df = pd.DataFrame(processed_data)
        
        # Lọc bỏ nhiễu từ các trạm thử nghiệm BSS.No2 giống logic cũ
        if "Problem Description" in df.columns:
            df = df[~df["Problem Description"].astype(str).str.strip().str.startswith("BSS.No2")].copy()
            
        return df

    except Exception as e:
        st.error(f"Gặp lỗi khi xử lý luồng dữ liệu API: {e}")
        return pd.DataFrame()

# ==========================================
# 🎨 4. MODULE RENDER BẢN ĐỒ
# ==========================================
def create_station_popup_html(station_code, tickets_df, tech_name, lat, lng):
    """
    Popup hiển thị các Charge Point trong trạm.
    Mỗi Charge Point sẽ được tô màu theo thời gian tồn tại ticket.
    Mã trạm sẽ là 1 link, click vào sẽ mở Google Maps tới đúng tọa độ trạm.
    """

    gmap_url = f"https://www.google.com/maps?q={float(lat)},{float(lng)}"

    html_content = f"""
    <div style="font-family:Arial;font-size:12px;min-width:280px;padding:5px;">
        <h4 style="margin:0;">
            Trạm:
            <a href="{gmap_url}" target="_blank" rel="noopener noreferrer"
               style="color:#1f77b4;text-decoration:none;">
                {station_code} 🗺️
            </a>
        </h4>

        <div style="margin-top:4px;margin-bottom:6px;">
            <b>Kỹ thuật viên:</b>
            <span style="color:#2ca02c;font-weight:bold;">
                {tech_name}
            </span>
        </div>

        <hr style="margin:5px 0;">
    """

    # Hours đã được tính sẵn 1 lần ở render_map(), chỉ cần sắp xếp lại
    tickets_df = tickets_df.sort_values("Hours", ascending=False)

    for _, row in tickets_df.iterrows():

        hours = row["Hours"]

        # Chọn màu theo thời gian
        if hours >= 48:
            card_bg = "#ffe5e5"
            border = "#d62728"
            time_color = "#b30000"

        elif hours >= 24:
            card_bg = "#fff2d9"
            border = "#ff9800"
            time_color = "#c77700"

        else:
            card_bg = "#eaf8ea"
            border = "#2ca02c"
            time_color = "#2ca02c"

        cp_id = str(row["Charge Point ID"])
        model_name = row["Model Name"] # Lấy thông tin model đã map
        icon = "🔋" if cp_id.startswith("BSS") else "⚡"

        html_content += f"""
        <div style="
            background:{card_bg};
            border-left:5px solid {border};
            padding:7px;
            margin-bottom:7px;
            border-radius:5px;
        ">
            <div style="
                color:{border};
                font-weight:bold;
                font-size:12px;
            ">
                {icon} {cp_id} 
                <span style="margin-top:3px; color:#555;">{model_name}</span>
            </div>

            <div style="margin-top:3px; line-height:1.4;">
                <b>Ticket Status:</b> {row["Ticket Status"]}
                <b>Ticket ID:</b> {row["Ticket ID"]} <br>
            </div>

            <div>
                <b>Thời gian:</b>
                <span style="color:{time_color}; font-weight:bold;">
                    {row["Ticket Duration"]}
                </span>
            </div>

            <div style="
                color:#555;
                margin-top:3px;
                font-style:italic;
            ">
                {row["Problem Description"]}
            </div>
        </div>
        """

    html_content += "</div>"

    return html_content
def create_station_marker(cp_count, color):
    """
    Tạo marker hình giọt nước có hiển thị số Charge Point lỗi.
    """

    color_map = {
        "green": "#5AB923",
        "orange": "#D38A14",
        "darkred": "#992127"
    }

    marker_color = color_map.get(color, "#007bff")

    html = f"""
    <div style="
        position:relative;
        width:40px;
        height:54px;
    ">

        <!-- Pin -->
        <div style="
            position:absolute;
            width:40px;
            height:40px;

            background:{marker_color};

            border-radius:50% 50% 50% 0;

            transform:rotate(-45deg);

            border:3px solid white;

            box-shadow:0 3px 8px rgba(0,0,0,.45);

        "></div>

        <!-- Number -->
        <div style="
            position:absolute;

            width:40px;
            height:40px;

            display:flex;
            align-items:center;
            justify-content:center;

            color:white;
            font-size:16px;
            font-weight:bold;

            z-index:999;

        ">
            {cp_count}
        </div>

    </div>
    """

    return folium.DivIcon(
        html=html,
        icon_size=(40,54),
        icon_anchor=(20,54)
    )
def _haversine_meters(lat1, lng1, lat2, lng2):
    """Khoảng cách thực tế (mét) giữa 2 toạ độ, dùng để phát hiện kỹ thuật
    viên có đang ở gần 1 trạm sạc hay không."""
    from math import radians, sin, cos, sqrt, atan2
    R = 6371000.0  # bán kính Trái Đất (m)
    phi1, phi2 = radians(lat1), radians(lat2)
    dphi = radians(lat2 - lat1)
    dlambda = radians(lng2 - lng1)
    a = sin(dphi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(dlambda / 2) ** 2
    return 2 * R * atan2(sqrt(a), sqrt(1 - a))


STATION_PROXIMITY_METERS = 150  # Ngưỡng coi như "đang ở tại trạm"

ROLE_MAP_COLORS = {
    "ky_thuat": "#1f77b4",
    "dieu_phoi_khu_vuc": "#2ca02c",
    "dieu_hanh": "#e67e22",
    "giam_doc": "#8e44ad",
    "admin": "#2c3e50",
}


def _initials(full_name):
    """Lấy chữ viết tắt tên từ họ tên đầy đủ, ví dụ 'Nguyễn Đức Huy' -> 'NH'
    (chữ cái đầu của từ đầu tiên + chữ cái đầu của từ cuối cùng)."""
    parts = [p for p in str(full_name).strip().split() if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def _staff_avatar_icon(initials, color):
    """Marker hình tròn nhỏ hiển thị chữ viết tắt tên, dễ quan sát trên bản đồ
    khi có nhiều nhân sự cùng lúc."""
    html = f"""
    <div style="
        width:30px;height:30px;border-radius:50%;
        background:{color};color:#ffffff;
        display:flex;align-items:center;justify-content:center;
        font-size:12px;font-weight:700;font-family:Arial,sans-serif;
        border:2px solid #ffffff;
        box-shadow:0 2px 6px rgba(0,0,0,.45);
    ">{initials}</div>
    """
    return folium.DivIcon(html=html, icon_size=(30, 30), icon_anchor=(15, 15))


def _wrench_badge_icon():
    """Tag động hình cờ lê, gắn cạnh nhân sự đang ở gần 1 trạm sạc."""
    html = """
    <div style="
        width:24px;height:24px;border-radius:50%;
        background:#ffffff;color:#e67e22;
        display:flex;align-items:center;justify-content:center;
        font-size:13px;
        border:2px solid #e67e22;
        box-shadow:0 2px 5px rgba(0,0,0,.4);
    ">🔧</div>
    """
    return folium.DivIcon(html=html, icon_size=(24, 24), icon_anchor=(12, 12))


def _find_nearby_station(lat, lng, coords_map, threshold=STATION_PROXIMITY_METERS):
    """Tìm trạm gần nhất với 1 toạ độ, trả về (mã trạm, khoảng cách mét)
    nếu nằm trong ngưỡng threshold, ngược lại trả về (None, None)."""
    nearest_code, nearest_dist = None, None
    for code, c in coords_map.items():
        d = _haversine_meters(lat, lng, c["lat"], c["lng"])
        if nearest_dist is None or d < nearest_dist:
            nearest_dist, nearest_code = d, code
    if nearest_dist is not None and nearest_dist <= threshold:
        return nearest_code, nearest_dist
    return None, None


def add_staff_markers_to_map(m, user, coords_map=None):
    """Vẽ vị trí nhân sự (mà `user` được phép xem) lên bản đồ folium `m` đã
    có sẵn, mỗi người hiển thị bằng 1 hình tròn nhỏ ghi chữ viết tắt tên.
    Nếu `coords_map` (toạ độ các trạm) được truyền vào, sẽ tự động gắn thêm
    1 tag hình cờ lê 🔧 cạnh kỹ thuật viên nào đang ở gần 1 trạm sạc, để
    quản lý dễ nhận biết ai đang sửa trạm nào.

    Trả về danh sách nhân sự chưa có toạ độ (để hiển thị cảnh báo)."""
    coords_map = coords_map or {}
    locations = auth.get_visible_locations(user)

    has_coords = [loc for loc in locations if loc["lat"] is not None and loc["lng"] is not None]
    no_coords = [loc for loc in locations if loc not in has_coords]

    for loc in has_coords:
        gmap_url = f"https://www.google.com/maps?q={loc['lat']},{loc['lng']}"
        nearby_code, nearby_dist = _find_nearby_station(loc["lat"], loc["lng"], coords_map)

        working_note = ""
        if nearby_code:
            working_note = (
                f"<div style='margin-top:4px;padding:4px 6px;background:#fff3cd;border-radius:4px;'>"
                f"🔧 Đang tại trạm <b>{nearby_code}</b> (~{nearby_dist:.0f}m)</div>"
            )

        popup_html = f"""
        <div style="font-family:Arial;font-size:12px;min-width:200px;">
            <b>{loc['full_name']}</b><br>
            {auth.ROLE_LABELS.get(loc['role'], loc['role'])}<br>
            Khu vực: {loc['regions'] or '—'}<br>
            <a href="{gmap_url}" target="_blank" rel="noopener noreferrer">Xem trên Google Maps 🗺️</a><br>
            <span style="color:#888;">Cập nhật lúc: {loc['updated_at'] or 'Chưa có'}</span>
            {working_note}
        </div>
        """
        folium.Marker(
            location=[loc["lat"], loc["lng"]],
            popup=folium.Popup(popup_html, max_width=260),
            tooltip=(f"{loc['full_name']} — đang tại trạm {nearby_code}" if nearby_code else loc["full_name"]),
            icon=_staff_avatar_icon(_initials(loc["full_name"]), ROLE_MAP_COLORS.get(loc["role"], "#7f8c8d")),
        ).add_to(m)

        # Tag động hình cờ lê, đặt lệch nhẹ cạnh marker nhân sự để không đè khít lên nhau
        if nearby_code:
            folium.Marker(
                location=[loc["lat"] + 0.00015, loc["lng"] + 0.00015],
                tooltip=f"🔧 {loc['full_name']} đang sửa trạm {nearby_code}",
                icon=_wrench_badge_icon(),
            ).add_to(m)

    return no_coords


def render_map(user):
    
    # 1. Nạp dữ liệu (cập nhật cách gọi hàm)
    with st.spinner("Đang đồng bộ dữ liệu tĩnh..."):
        coords_map, tech_map, region_map, cp_model_map = load_static_data() 
        
    with st.spinner("Đang kết nối hệ thống CCTS lấy ticket..."):
        df_tickets = fetch_live_tickets()

    # 2. Xử lý logic Map
    m = folium.Map(location=[12.25, 108.5], zoom_start=6.3) 
    Fullscreen(
        position="topright",
        title="Toàn màn hình",
        title_cancel="Thoát"
    ).add_to(m)
    LocateControl(
        auto_start=False,
        flyTo=True,
        keepCurrentZoomLevel=True
    ).add_to(m)
    MiniMap(
        toggle_display=True,
        position="bottomright"
    ).add_to(m)

    total_tickets = len(df_tickets)
    col1, col2 = st.columns(2)
    col1.metric("Tổng số sự cố đang mở", total_tickets)
    col2.info("Dữ liệu được làm mới tự động mỗi 5 phút để tối ưu hiệu năng API.")

    missing_stations = [] # Danh sách trạm thiếu tọa độ

    if df_tickets.empty:
        st.info("Hiện không có ticket sự cố nào đang mở.")
    else:
        # Map thông tin model vào DataFrame
        df_tickets = df_tickets.copy()
        df_tickets["Model Name"] = df_tickets["Charge Box Model"].map(cp_model_map).fillna("N/A")

        # Tính Hours 1 lần duy nhất cho toàn bộ dataframe (tránh tính lại nhiều lần cho mỗi trạm)
        df_tickets["Hours"] = df_tickets["Ticket Duration"].apply(parse_duration_to_hours)

        # 3. Gắn Markers (Gom nhóm theo trạm)
        grouped = df_tickets.groupby('Station Code')

        for station_code, group in grouped:
            core_code = extract_core_station_code(station_code)

            # Kiểm tra KV quản lý
            region = region_map.get(core_code, "Unknown")
            if region == "KV không quản lý":
                continue

            # Kiểm tra tọa độ
            if core_code in coords_map:
                lat = coords_map[core_code]['lat']
                lng = coords_map[core_code]['lng']
                tech_name = tech_map.get(core_code, "Unassigned")

                # Dùng lại cột Hours đã tính sẵn từ trước để chọn màu marker
                max_duration = group["Hours"].max()
                color = "darkred" if max_duration > 48 else ("orange" if max_duration >= 24 else "green")

                # Tạo popup danh sách (kèm lat/lng để tạo link Google Maps)
                popup_html = create_station_popup_html(station_code, group, tech_name, lat, lng)

                # ==========================
                # Marker hiển thị số lượng Charge Point lỗi
                # ==========================

                cp_count = len(group)

                folium.Marker(
                    location=[lat, lng],

                    popup=folium.Popup(
                        folium.IFrame(
                            html=popup_html,
                            width=300,
                            height=320
                        ),
                        max_width=300
                    ),

                    icon=folium.Icon(
                        color=color,
                        icon="info-sign"
                    )

                ).add_to(m)
            else:
                missing_stations.append({
                    "Station Code": station_code,
                    "Ticket ID": "Multiple",
                    "Problem": "Trạm có nhiều ticket lỗi"
                })

    # 3b. Gộp vị trí nhân sự lên CÙNG bản đồ (chỉ dành cho cấp quản lý trở lên)
    no_coords = []
    if user["role"] != "ky_thuat":
        show_overlay = st.checkbox(
            "🧑‍🔧 Hiện vị trí kỹ thuật viên trên bản đồ", value=True, key="overlay_staff_on_main_map"
        )
        if show_overlay:
            no_coords = add_staff_markers_to_map(m, user, coords_map)

    # 4. Render Bản đồ lên Streamlit
    st_folium(
        m,
        width="100%",
        height=700,
        returned_objects=[],
        key="ccts_live_map"
    )

    if no_coords:
        st.caption(
            f"⚠️ {len(no_coords)} người chưa chia sẻ vị trí: "
            + ", ".join(x["full_name"] for x in no_coords)
        )

    # 5. Hiển thị nút tải file trạm thiếu (nếu có)
    if missing_stations:
        st.divider()
        st.warning(f"⚠️ Có {len(set([x['Station Code'] for x in missing_stations]))} trạm chưa có tọa độ!")
        
        df_missing = pd.DataFrame(missing_stations).drop_duplicates(subset=["Station Code"])
        
        # Chuyển đổi DataFrame thành file Excel trong bộ nhớ
        import io
        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine='xlsxwriter') as writer:
            df_missing.to_excel(writer, index=False, sheet_name='Missing_Stations')
        
        st.download_button(
            label="📥 Tải file danh sách trạm thiếu tọa độ (.xlsx)",
            data=buffer.getvalue(),
            file_name="missing_stations.xlsx",
            mime="application/vnd.ms-excel"
        )

# ==========================================
# 🔐 5. MODULE ĐĂNG NHẬP & PHÂN QUYỀN
# ==========================================
def login_page():
    st.markdown("<div style='height:8vh;'></div>", unsafe_allow_html=True)
    st.markdown("<h2 style='text-align:center;'>🔐 Đăng nhập hệ thống CCTS</h2>", unsafe_allow_html=True)
    _, mid, _ = st.columns([1, 1.4, 1])
    with mid:
        with st.form("login_form"):
            username = st.text_input("Tên đăng nhập")
            password = st.text_input("Mật khẩu", type="password")
            submitted = st.form_submit_button("Đăng nhập", use_container_width=True)

        if submitted:
            user = auth.verify_login(username, password)
            if user:
                st.session_state["auth_user"] = user
                st.session_state.pop("last_sent_loc", None)
                st.rerun()
            else:
                st.error("Sai tên đăng nhập hoặc mật khẩu, hoặc tài khoản đã bị khóa.")
                with st.expander("🔍 Chẩn đoán (không lộ mật khẩu)"):
                    try:
                        status = auth.debug_user_status(username)
                        st.write(f"Tổng số tài khoản trong hệ thống: **{status['total_users']}**")
                        if not status["found"]:
                            st.warning(
                                f"Không tìm thấy tài khoản có tên đăng nhập '{username}'. "
                                "Kiểm tra lại chính tả, hoặc mở Google Sheet tab 'Users' để xem "
                                "tên đăng nhập chính xác đang được lưu."
                            )
                        elif not status["active"]:
                            st.warning(
                                f"Tài khoản '{status['matched_username']}' tồn tại nhưng đang bị khóa "
                                "(cột 'active' = FALSE trong Google Sheet)."
                            )
                        else:
                            st.warning(
                                f"Tài khoản '{status['matched_username']}' tồn tại và đang hoạt động, "
                                "nên nhiều khả năng bạn đã gõ **sai mật khẩu**. Nếu chắc chắn mật khẩu đúng, "
                                "có thể tài khoản này được tạo (bootstrap) từ một lần chạy trước với mật khẩu "
                                "khác trong Secrets — hãy xoá dòng tài khoản này (giữ lại dòng tiêu đề) trong "
                                "tab 'Users' của Google Sheet rồi tải lại trang để hệ thống tạo lại tài khoản "
                                "Admin theo đúng Secrets hiện tại."
                            )
                    except Exception as diag_err:
                        st.caption(f"Không thể chạy chẩn đoán: {diag_err}")


def render_user_bar(user):
    """Thanh trạng thái người dùng: thông tin cá nhân, tự động gửi vị trí LIÊN TỤC
    (không cần bấm nút), đăng xuất. Trình duyệt sẽ tự hiện popup xin quyền định vị
    ngay khi trang tải - đây là bước bắt buộc theo chính sách bảo mật trình duyệt,
    không thể bỏ qua. Sau khi được cấp quyền, vị trí sẽ tự động cập nhật mỗi khi
    thiết bị di chuyển đủ xa hoặc sau một khoảng thời gian nhất định, kể cả khi
    không có tương tác gì thêm từ người dùng."""
    top_l, top_m, top_r = st.columns([3, 2, 1])

    with top_l:
        regions_str = ", ".join(user["regions"]) if user["regions"] else "—"
        st.markdown(
            f"👋 **{user['full_name']}** &nbsp;·&nbsp; "
            f"_{auth.ROLE_LABELS.get(user['role'], user['role'])}_ &nbsp;·&nbsp; "
            f"Khu vực: {regions_str}"
        )

    with top_m:
        loc = geo_watcher(min_interval_sec=15, min_distance_m=20, key="geo_watcher_main")
        if loc is None:
            st.caption("⚠️ Cần Streamlit >= 1.51 để bật theo dõi vị trí tự động.")
        elif loc.error:
            st.caption(f"⚠️ Không lấy được vị trí: {loc.error}")
        elif loc.latitude is not None:
            current = (round(loc.latitude, 5), round(loc.longitude, 5))
            if current != st.session_state.get("last_sent_loc"):
                auth.update_location(user["username"], loc.latitude, loc.longitude, loc.accuracy)
                st.session_state["last_sent_loc"] = current
                st.toast("📍 Đã cập nhật vị trí của bạn")
            st.caption("📍 Đang theo dõi vị trí")
        else:
            st.caption("📍 Đang chờ trình duyệt cấp quyền định vị...")

    with top_r:
        if st.button("🚪 Đăng xuất", use_container_width=True):
            st.session_state.clear()
            st.rerun()


def render_admin_panel():
    """Trang quản lý tài khoản - chỉ Admin nhìn thấy."""
    st.subheader("👤 Quản lý tài khoản")

    existing_users = auth.list_users()
    usernames = [u["username"] for u in existing_users]

    with st.expander("➕ Tạo mới / Cập nhật tài khoản", expanded=True):
        edit_target = st.selectbox(
            "Chọn tài khoản để sửa (hoặc để trống để tạo mới)",
            options=["-- Tạo tài khoản mới --"] + usernames,
        )
        is_edit = edit_target != "-- Tạo tài khoản mới --"
        current = next((u for u in existing_users if u["username"] == edit_target), None) if is_edit else None
        role_keys = list(auth.ROLE_LEVELS.keys())

        with st.form("user_form", clear_on_submit=not is_edit):
            username = st.text_input(
                "Tên đăng nhập", value=current["username"] if current else "", disabled=is_edit
            )
            full_name = st.text_input("Họ và tên", value=current["full_name"] if current else "")
            role = st.selectbox(
                "Vai trò",
                options=role_keys,
                format_func=lambda r: auth.ROLE_LABELS[r],
                index=role_keys.index(current["role"]) if current and current["role"] in role_keys else 0,
            )
            regions_input = st.text_input(
                "Khu vực phụ trách (cách nhau bởi dấu phẩy, không bắt buộc — chỉ để hiển thị thông tin, "
                "không giới hạn phạm vi xem vị trí)",
                value=current["regions"] if current else "",
            )
            password = st.text_input(
                "Mật khẩu" + (" (để trống nếu không đổi)" if is_edit else ""),
                type="password",
            )
            active = st.checkbox("Tài khoản đang hoạt động", value=bool(current["active"]) if current else True)

            submitted = st.form_submit_button("💾 Lưu tài khoản", use_container_width=True)

        if submitted:
            regions = [r.strip() for r in regions_input.split(",") if r.strip()]
            try:
                action = auth.create_or_update_user(
                    username=username,
                    full_name=full_name,
                    role=role,
                    regions=regions,
                    password=password if password else None,
                    active=active,
                )
                st.success(f"Đã {'cập nhật' if action == 'updated' else 'tạo mới'} tài khoản '{username}'.")
                st.rerun()
            except ValueError as e:
                st.error(str(e))

    st.divider()
    st.markdown("**📋 Danh sách tài khoản hiện có**")
    if existing_users:
        df_users = pd.DataFrame(existing_users)
        df_users["role"] = df_users["role"].map(lambda r: auth.ROLE_LABELS.get(r, r))
        st.dataframe(df_users, use_container_width=True, hide_index=True)

        del_user = st.selectbox("Chọn tài khoản để xoá", options=["--"] + usernames)
        if del_user != "--":
            if st.button(f"🗑️ Xoá tài khoản '{del_user}'", type="primary"):
                auth.delete_user(del_user)
                st.success(f"Đã xoá tài khoản '{del_user}'.")
                st.rerun()
    else:
        st.info("Chưa có tài khoản nào trong hệ thống.")


def main():

    st.markdown("""
    <style>

    /* Thu nhỏ khoảng trắng, nhưng chừa đủ chỗ để không bị thanh toolbar của Streamlit che mất */
    .block-container{
        padding-top:3.5rem;
        padding-bottom:0.3rem;
        padding-left:0.6rem;
        padding-right:0.6rem;
    }

    /* Ẩn Sidebar */
    [data-testid="stSidebar"]{
        display:none;
    }

    /* Mobile */
    @media (max-width:768px){

        h3{
            font-size:22px !important;
        }

        button[kind="primary"]{
            width:100%;
            height:48px;
            font-size:18px;
        }

        [data-testid="stMetric"]{
            text-align:center;
        }

    }

    </style>
    """, unsafe_allow_html=True)

    # Khởi tạo Google Sheets (tạo sheet Users/Locations + tài khoản Admin đầu tiên nếu chưa có)
    try:
        auth.init_db()
    except PermissionError as e:
        sa_email = auth.get_service_account_email()
        st.error("⚠️ Google từ chối quyền truy cập Google Sheet (403 Forbidden).")
        if sa_email:
            st.warning(
                f"👉 Hãy mở Google Sheet của bạn, bấm **Share**, thêm email sau với quyền **Editor**:\n\n`{sa_email}`"
            )
        else:
            st.warning(
                "👉 Không đọc được `client_email` từ Secrets. Kiểm tra lại mục "
                "`[connections.gsheets]` đã điền đủ các trường của Service Account chưa."
            )
        st.info(
            "Ngoài ra, kiểm tra thêm 2 điều sau trong Google Cloud Console (cùng project với Service Account):\n"
            "- Đã bật **Google Sheets API**\n"
            "- Đã bật **Google Drive API**"
        )
        with st.expander("Chi tiết lỗi kỹ thuật"):
            st.code(traceback.format_exc())
        st.stop()
    except Exception as e:
        st.error(f"⚠️ Không thể kết nối Google Sheets: {type(e).__name__}: {e}")
        st.info("Kiểm tra lại cấu hình `[connections.gsheets]` trong Streamlit Secrets (spreadsheet URL, service account...).")
        with st.expander("Chi tiết lỗi kỹ thuật"):
            st.code(traceback.format_exc())
        st.stop()

    # Chặn truy cập nếu chưa đăng nhập
    if "auth_user" not in st.session_state:
        login_page()
        return

    user = st.session_state["auth_user"]
    render_user_bar(user)

    st.title("⚡ CCTS Live Map")

    tab_labels = ["🗺️ Bản đồ sự cố"]
    if user["role"] == "admin":
        tab_labels.append("👤 Quản lý tài khoản")

    tabs = st.tabs(tab_labels)

    with tabs[0]:
        col1, col2 = st.columns([1, 1], gap="small")
        with col1:
            if st.button("🔄 Cập nhật dữ liệu", use_container_width=True):
                st.cache_data.clear()
                st.rerun()
        with col2:
            st.caption("Tự động cập nhật mỗi 10 phút")
        render_map(user)

    if user["role"] == "admin":
        with tabs[1]:
            render_admin_panel()

if __name__ == "__main__":
    main()